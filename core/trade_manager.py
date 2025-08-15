import os
import logging
from typing import Tuple
from telegram.ext import Application
from sqlalchemy.orm import Session
from database.session import SessionLocal
from database.models import User, Trade, PendingSignal, SignalForApproval
from services.bybit_service import place_order, get_account_info, get_daily_pnl, place_limit_order, cancel_order
from services.notification_service import send_notification
from utils.security import decrypt_data
from utils.config import ADMIN_ID
from bot.keyboards import signal_approval_keyboard

logger = logging.getLogger(__name__)

def _avaliar_sinal(signal_data: dict, user_settings: User) -> Tuple[bool, str]:
    """
    Função interna para aplicar todos os filtros configurados pelo usuário.
    Retorna (True, "Motivo") se aprovado, ou (False, "Motivo") se rejeitado.
    """
    # Filtro 1: Confiança Mínima
    min_confidence = user_settings.min_confidence
    signal_confidence = signal_data.get('confidence')
    if signal_confidence is not None and signal_confidence < min_confidence:
        motivo = f"Confiança ({signal_confidence:.2f}%) é menor que o seu mínimo ({min_confidence:.2f}%)"
        return False, motivo
    return True, "Sinal aprovado pelos seus critérios."

async def process_new_signal(signal_data: dict, application: Application, source_name: str):
    """
    Roteador de sinais: verifica metas, filtros, tipo de ordem e modo de aprovação.
    """
    signal_type = signal_data.get("type")
    symbol = signal_data.get("coin")
    db = SessionLocal()
    try:
        admin_user = db.query(User).filter_by(telegram_id=ADMIN_ID).first()
        if not admin_user:
            logger.error("Admin não encontrado.")
            return
            
        api_key = decrypt_data(admin_user.api_key_encrypted)
        api_secret = decrypt_data(admin_user.api_secret_encrypted)

        # --- LÓGICA DE CANCELAMENTO ATUALIZADA ---
        if signal_type == 'CANCELLED':
            pending_order = db.query(PendingSignal).filter_by(symbol=symbol, user_telegram_id=ADMIN_ID).first()
            if pending_order:
                logger.info(f"Recebido sinal de cancelamento para {symbol}. Cancelando ordem {pending_order.order_id} na Bybit.")
                cancel_result = await cancel_order(api_key, api_secret, pending_order.order_id, symbol)
                
                if cancel_result.get("success"):
                    db.delete(pending_order)
                    db.commit()
                    await send_notification(application, f"✅ Ordem limite para <b>{symbol}</b> cancelada com sucesso na corretora.")
                else:
                    await send_notification(application, f"⚠️ Falha ao cancelar ordem limite para <b>{symbol}</b> na corretora: {cancel_result.get('error')}")
            else:
                 await send_notification(application, f"ℹ️ Recebido sinal de cancelamento para <b>{symbol}</b>, mas nenhuma ordem pendente foi encontrada no bot.")
            return

        # --- VERIFICAÇÃO DE METAS DIÁRIAS ---
        pnl_result = await get_daily_pnl(api_key, api_secret)
        if pnl_result.get("success"):
            current_pnl = pnl_result["pnl"]
            logger.info(f"P/L realizado hoje: ${current_pnl:.2f}")

            profit_target = admin_user.daily_profit_target
            if profit_target > 0 and current_pnl >= profit_target:
                msg = f"🎯 Meta de lucro diária de ${profit_target:.2f} atingida (P/L atual: ${current_pnl:.2f}). Novas ordens pausadas por hoje."
                logger.info(msg)
                await send_notification(application, msg)
                return

            loss_limit = admin_user.daily_loss_limit
            if loss_limit > 0 and current_pnl <= -loss_limit:
                msg = f"🛑 Limite de perda diário de ${loss_limit:.2f} atingido (P/L atual: ${current_pnl:.2f}). Novas ordens pausadas por hoje."
                logger.info(msg)
                await send_notification(application, msg)
                return
        else:
            logger.error("Não foi possível verificar o P/L diário. Abertura de trade cancelada por segurança.")
            await send_notification(application, "⚠️ Falha ao verificar metas diárias. A operação não foi aberta.")
            return

        # --- AVALIAÇÃO DO SINAL ---
        aprovado, motivo = _avaliar_sinal(signal_data, admin_user)
        if not aprovado:
            rejection_msg = f"⚠️ <b>Sinal para {symbol} Ignorado</b>\n<b>Fonte:</b> {source_name}\n<b>Motivo:</b> {motivo}"
            await send_notification(application, rejection_msg)
            return
        
        # --- ROTEADOR DE TIPO DE ORDEM ---
        account_info = await get_account_info(api_key, api_secret)
        balance = float(account_info.get("data", [{}])[0].get('totalEquity', 0))

        if signal_type == 'MARKET':
            logger.info(f"Sinal A MERCADO para {symbol}. Verificando modo de aprovação...")
            if admin_user.approval_mode == 'AUTOMATIC':
                await _execute_trade(signal_data, admin_user, application, db, source_name)
            elif admin_user.approval_mode == 'MANUAL':
                logger.info(f"Modo MANUAL. Enviando sinal A MERCADO para aprovação: {symbol}")
                # (Sua lógica de aprovação manual para ordens a mercado)
                new_signal_for_approval = SignalForApproval(
                    user_telegram_id=ADMIN_ID, symbol=symbol,
                    source_name=source_name, signal_data=signal_data
                )
                db.add(new_signal_for_approval)
                db.commit()
                signal_details = (
                    f"<b>Sinal A MERCADO de: {source_name}</b>\n\n"
                    f"<b>Moeda:</b> {signal_data['coin']}\n"
                    f"<b>Tipo:</b> {signal_data['order_type']}\n"
                    f"<b>Stop:</b> {signal_data['stop_loss']}\n"
                    f"<b>Alvo 1:</b> {signal_data['targets'][0]}\n\n"
                    f"O sinal passou nos seus filtros. Você aprova a entrada?"
                )
                sent_message = await application.bot.send_message(
                    chat_id=ADMIN_ID, text=signal_details, parse_mode='HTML',
                    reply_markup=signal_approval_keyboard(new_signal_for_approval.id)
                )
                new_signal_for_approval.approval_message_id = sent_message.message_id
                db.commit()

        elif signal_type == 'LIMIT':
            logger.info(f"Sinal LIMITE para {symbol}. Posicionando ordem na corretora...")
            limit_order_result = await place_limit_order(api_key, api_secret, signal_data, admin_user, balance)

            if limit_order_result.get("success"):
                order_id = limit_order_result["data"]["orderId"]
                
                new_pending_signal = PendingSignal(
                    user_telegram_id=ADMIN_ID,
                    symbol=symbol,
                    order_id=order_id,
                    signal_data=signal_data
                )
                db.add(new_pending_signal)
                db.commit()
                await send_notification(application, f"✅ Ordem Limite para <b>{symbol}</b> (ID: ...{order_id[-6:]}) foi posicionada. Monitorando execução...")
            else:
                error = limit_order_result.get('error')
                await send_notification(application, f"❌ Falha ao posicionar ordem limite para <b>{symbol}</b>: {error}")
    
    finally:
        db.close()

async def _execute_trade(signal_data: dict, user: User, application: Application, db: Session, source_name: str):
    """Função interna que contém a lógica para abrir uma posição na Bybit."""
    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)
    
    # --- AWAIT ADICIONADO ---
    account_info = await get_account_info(api_key, api_secret)
    if not account_info.get("success"):
        await send_notification(application, f"❌ Falha ao buscar saldo da Bybit para operar {signal_data['coin']}.")
        return
    
    balances = account_info.get("data", [])
    if not balances:
        await send_notification(application, f"❌ Falha: Nenhuma informação de saldo recebida da Bybit para operar {signal_data['coin']}.")
        return

    # --- CORREÇÃO: Pega o saldo total do primeiro item da lista ---
    balance = float(balances[0].get('totalEquity', 0))
    
    # --- AWAIT ADICIONADO ---
    result = await place_order(api_key, api_secret, signal_data, user, balance)
    
    if result.get("success"):
        order_data = result['data']
        order_id = order_data['orderId']

        # Salva o trade bem-sucedido no banco de dados
        new_trade = Trade(
            user_telegram_id=user.telegram_id,
            order_id=order_id,
            symbol=signal_data['coin'],
            side=signal_data['order_type'],
            qty=float(order_data.get('qty', 0)),
            entry_price=signal_data['entries'][0],
            stop_loss=signal_data['stop_loss'],
            current_stop_loss=signal_data['stop_loss'],
            initial_targets=signal_data['targets'],
            status='ACTIVE',
            remaining_qty=float(order_data.get('qty', 0))
        )
        db.add(new_trade)
        db.commit()
        logger.info(f"Trade {order_id} salvo no banco de dados para rastreamento.")

        await send_notification(application, f"📈 <b>Ordem Aberta com Sucesso!</b>\n<b>Moeda:</b> {signal_data['coin']}\n<b>ID:</b> {order_id}")
    else:
        error_msg = result.get('error')
        await send_notification(application, f"❌ <b>Falha ao Abrir Ordem</b>\n<b>Moeda:</b> {signal_data['coin']}\n<b>Motivo:</b> {error_msg}")