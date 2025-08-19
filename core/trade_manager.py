import os
import logging
from typing import Tuple
from telegram.ext import Application
from sqlalchemy.orm import Session
from database.session import SessionLocal
from database.models import User, Trade, PendingSignal, SignalForApproval
from services.bybit_service import (
    place_order, get_account_info, get_daily_pnl,
    place_limit_order, cancel_order, close_partial_position, modify_position_stop_loss
)
from services.notification_service import send_notification
from utils.security import decrypt_data
from utils.config import ADMIN_ID
from bot.keyboards import signal_approval_keyboard
# Importa a nova classe de tipos de sinal do parser refatorado
from services.signal_parser import SignalType

logger = logging.getLogger(__name__)


def _avaliar_sinal(signal_data: dict, user_settings: User) -> Tuple[bool, str]:
    min_confidence = user_settings.min_confidence
    signal_confidence = signal_data.get('confidence')
    if signal_confidence is not None and signal_confidence < min_confidence:
        motivo = f"Confiança ({signal_confidence:.2f}%) é menor que o seu mínimo ({min_confidence:.2f}%)"
        return False, motivo
    return True, "Sinal aprovado pelos seus critérios."

async def _execute_trade(signal_data: dict, user: User, application: Application, db: Session, source_name: str):
    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)
    
    account_info = await get_account_info(api_key, api_secret)
    if not account_info.get("success"):
        await application.bot.send_message(chat_id=user.telegram_id, text=f"❌ Falha ao buscar seu saldo Bybit para operar {signal_data['coin']}.")
        return

    balances = account_info.get("data", [])
    if not balances:
        await application.bot.send_message(chat_id=user.telegram_id, text=f"❌ Falha: Nenhuma info de saldo recebida da Bybit para operar {signal_data['coin']}.")
        return

    balance = float(balances[0].get('totalEquity', 0))
    result = await place_order(api_key, api_secret, signal_data, user, balance)
    
    if result.get("success"):
        order_data = result['data']
        order_id = order_data['orderId']
        new_trade = Trade(
            user_telegram_id=user.telegram_id, order_id=order_id,
            symbol=signal_data['coin'], side=signal_data['order_type'],
            qty=float(order_data.get('qty', 0)), entry_price=signal_data['entries'][0],
            stop_loss=signal_data['stop_loss'], current_stop_loss=signal_data['stop_loss'],
            initial_targets=signal_data['targets'], status='ACTIVE',
            remaining_qty=float(order_data.get('qty', 0))
        )
        db.add(new_trade)
        logger.info(f"Trade {order_id} para o usuário {user.telegram_id} salvo no DB.")
        await application.bot.send_message(chat_id=user.telegram_id, text=f"📈 <b>Ordem Aberta com Sucesso!</b>\n<b>Moeda:</b> {signal_data['coin']}", parse_mode='HTML')
    else:
        error_msg = result.get('error')
        await application.bot.send_message(chat_id=user.telegram_id, text=f"❌ <b>Falha ao Abrir Ordem</b>\n<b>Moeda:</b> {signal_data['coin']}\n<b>Motivo:</b> {error_msg}", parse_mode='HTML')


async def execute_signal_for_all_users(signal_data: dict, application: Application, db: Session, source_name: str):
    signal_type = signal_data.get("type")
    symbol = signal_data.get("coin")

    all_users_to_trade = db.query(User).filter(User.api_key_encrypted.isnot(None)).all()
    if not all_users_to_trade:
        logger.info("Nenhum usuário com API configurada para replicar o trade.")
        return

    logger.info(f"Sinal ({signal_type}) aprovado. Replicando para {len(all_users_to_trade)} usuário(s)...")

    if signal_type == SignalType.MARKET:
        for user in all_users_to_trade:
            await _execute_trade(signal_data, user, application, db, source_name)
    
    elif signal_type == SignalType.LIMIT:
        for user in all_users_to_trade:
            existing_pending = db.query(PendingSignal).filter_by(user_telegram_id=user.telegram_id, symbol=symbol).first()
            if existing_pending:
                await application.bot.send_message(chat_id=user.telegram_id, text=f"ℹ️ Você já tem uma ordem limite pendente para <b>{symbol}</b>. O novo sinal foi ignorado.", parse_mode='HTML')
                continue
            
            user_api_key = decrypt_data(user.api_key_encrypted)
            user_api_secret = decrypt_data(user.api_secret_encrypted)
            account_info = await get_account_info(user_api_key, user_api_secret)
            balance = float(account_info.get("data", [{}])[0].get('totalEquity', 0))
            limit_order_result = await place_limit_order(user_api_key, user_api_secret, signal_data, user, balance)

            if limit_order_result.get("success"):
                order_id = limit_order_result["data"]["orderId"]
                db.add(PendingSignal(user_telegram_id=user.telegram_id, symbol=symbol, order_id=order_id, signal_data=signal_data))
                await application.bot.send_message(chat_id=user.telegram_id, text=f"✅ Ordem Limite para <b>{symbol}</b> foi posicionada. Monitorando...", parse_mode='HTML')
            else:
                error = limit_order_result.get('error')
                await application.bot.send_message(chat_id=user.telegram_id, text=f"❌ Falha ao posicionar sua ordem limite para <b>{symbol}</b>: {error}", parse_mode='HTML')
    db.commit()


async def process_new_signal(signal_data: dict, application: Application, source_name: str):
    """
    Processa todos os tipos de sinais (Entrada, Cancelamento, Gerenciamento),
    validando para o admin e executando as ações apropriadas.
    """
    signal_type = signal_data.get("type")
    symbol = signal_data.get("coin")
    db = SessionLocal()
    try:
        admin_user = db.query(User).filter_by(telegram_id=ADMIN_ID).first()
        if not admin_user or not admin_user.api_key_encrypted:
            logger.error("Admin não configurado, não é possível processar sinais.")
            return

        ### ALTERAÇÃO INICIADA: Lógica de Gerenciamento e Cancelamento ###

        # Ações que não dependem de aprovação (gerenciamento de posições existentes)
        if signal_type == SignalType.CANCELAR:
            pending_orders = db.query(PendingSignal).filter_by(symbol=symbol).all()
            if not pending_orders:
                await send_notification(application, f"ℹ️ Recebido sinal de cancelamento para <b>{symbol}</b>, mas nenhuma ordem pendente foi encontrada.")
                return
            
            for order in pending_orders:
                user_keys = db.query(User).filter_by(telegram_id=order.user_telegram_id).first()
                if not user_keys: continue
                user_api_key = decrypt_data(user_keys.api_key_encrypted)
                user_api_secret = decrypt_data(user_keys.api_secret_encrypted)
                cancel_result = await cancel_order(user_api_key, user_api_secret, order.order_id, symbol)
                if cancel_result.get("success"):
                    await application.bot.send_message(chat_id=order.user_telegram_id, text=f"✅ Sua ordem limite para <b>{symbol}</b> foi cancelada com sucesso.", parse_mode='HTML')
                    db.delete(order)
                else:
                    await application.bot.send_message(chat_id=order.user_telegram_id, text=f"⚠️ Falha ao cancelar sua ordem limite para <b>{symbol}</b>.", parse_mode='HTML')
            db.commit()
            return

        if signal_type == SignalType.FECHAR_PARCIAL:
            active_trades = db.query(Trade).filter(Trade.symbol == symbol, ~Trade.status.like('%CLOSED%')).all()
            for trade in active_trades:
                user = db.query(User).filter_by(telegram_id=trade.user_telegram_id).first()
                api_key = decrypt_data(user.api_key_encrypted)
                api_secret = decrypt_data(user.api_secret_encrypted)
                qty_to_close = trade.remaining_qty / 2 # Padrão de 50%
                result = await close_partial_position(api_key, api_secret, symbol, qty_to_close, trade.side)
                if result.get("success"):
                    trade.remaining_qty -= qty_to_close
                    await application.bot.send_message(chat_id=user.telegram_id, text=f"✅ Lucro parcial de <b>{symbol}</b> realizado com sucesso!", parse_mode='HTML')
            db.commit()
            return

        if signal_type == SignalType.MOVER_STOP_ENTRADA:
            active_trades = db.query(Trade).filter(Trade.symbol == symbol, ~Trade.status.like('%CLOSED%')).all()
            for trade in active_trades:
                user = db.query(User).filter_by(telegram_id=trade.user_telegram_id).first()
                api_key = decrypt_data(user.api_key_encrypted)
                api_secret = decrypt_data(user.api_secret_encrypted)
                result = await modify_position_stop_loss(api_key, api_secret, symbol, trade.entry_price)
                if result.get("success"):
                    trade.current_stop_loss = trade.entry_price
                    await application.bot.send_message(chat_id=user.telegram_id, text=f"🛡️ Stop loss de <b>{symbol}</b> movido para a entrada. Seu trade está protegido!", parse_mode='HTML')
            db.commit()
            return
        
        ### FIM DA ALTERAÇÃO ###

        # Lógica de validação para SINAIS DE ENTRADA (Market/Limit)
        aprovado, motivo = _avaliar_sinal(signal_data, admin_user)
        if not aprovado:
            await send_notification(application, f"ℹ️ Sinal para {symbol} ignorado pelo admin: {motivo}")
            return
        
        if admin_user.approval_mode == 'AUTOMATIC':
            await execute_signal_for_all_users(signal_data, application, db, source_name)
        
        elif admin_user.approval_mode == 'MANUAL':
            logger.info(f"Modo MANUAL. Enviando sinal ({signal_type}) para aprovação do Admin.")
            
            new_signal_for_approval = SignalForApproval(
                user_telegram_id=ADMIN_ID, symbol=symbol,
                source_name=source_name, signal_data=signal_data
            )
            db.add(new_signal_for_approval)
            db.commit()

            signal_details = (
                f"<b>Sinal ({signal_type}) de: {source_name}</b>\n\n<b>Moeda:</b> {signal_data['coin']}\n"
                f"<b>Tipo:</b> {signal_data['order_type']}\n<b>Entrada:</b> {signal_data['entries'][0]}\n"
                f"<b>Stop:</b> {signal_data['stop_loss']}\n<b>Alvo 1:</b> {signal_data['targets'][0]}\n\n"
                f"O sinal passou nos seus filtros. Você aprova a entrada?"
            )
            sent_message = await application.bot.send_message(
                chat_id=ADMIN_ID, text=signal_details, parse_mode='HTML',
                reply_markup=signal_approval_keyboard(new_signal_for_approval.id)
            )
            new_signal_for_approval.approval_message_id = sent_message.message_id
            db.commit()
            
    finally:
        db.close()