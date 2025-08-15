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
    Roteador de sinais: Valida o sinal para o admin e replica para todos os usuários.
    """
    signal_type = signal_data.get("type")
    symbol = signal_data.get("coin")
    db = SessionLocal()
    try:
        # --- VALIDAÇÃO CENTRALIZADA NO ADMIN ---
        # O sinal só é processado para todos se for válido para a conta mestre (Admin)
        admin_user = db.query(User).filter_by(telegram_id=ADMIN_ID).first()
        if not admin_user or not admin_user.api_key_encrypted:
            logger.error("Admin não configurado para validar o sinal. Nenhuma ação será tomada.")
            return
            
        api_key = decrypt_data(admin_user.api_key_encrypted)
        api_secret = decrypt_data(admin_user.api_secret_encrypted)

        if signal_type == 'CANCELLED':
            # O cancelamento também é replicado para todos
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
                    await application.bot.send_message(chat_id=order.user_telegram_id, text=f"✅ Sua ordem limite para <b>{symbol}</b> foi cancelada com sucesso pela fonte do sinal.", parse_mode='HTML')
                    db.delete(order)
                else:
                    await application.bot.send_message(chat_id=order.user_telegram_id, text=f"⚠️ Falha ao cancelar sua ordem limite para <b>{symbol}</b> na corretora.", parse_mode='HTML')
            db.commit()
            return

        # Validações de P/L e filtros baseadas na conta do Admin
        pnl_result = await get_daily_pnl(api_key, api_secret)
        if not pnl_result.get("success") or (pnl_result.get("pnl") >= admin_user.daily_profit_target > 0) or (pnl_result.get("pnl") <= -admin_user.daily_loss_limit > 0):
            logger.info("Sinal ignorado devido às metas de P/L do Admin.")
            await send_notification(application, "ℹ️ Sinal ignorado pois as metas de P/L do dia já foram atingidas.")
            return

        aprovado, motivo = _avaliar_sinal(signal_data, admin_user)
        if not aprovado:
            logger.info(f"Sinal para {symbol} ignorado pelo filtro do Admin: {motivo}")
            await send_notification(application, f"ℹ️ Sinal para {symbol} ignorado pelo filtro do Admin: {motivo}")
            return
        
        # --- ROTEADOR DE TIPO DE ORDEM E REPLICAÇÃO ---
        
        # Busca todos os usuários que têm chaves de API e estão prontos para operar
        all_users_to_trade = db.query(User).filter(User.api_key_encrypted.isnot(None)).all()
        
        if not all_users_to_trade:
            logger.info("Nenhum usuário com API configurada para replicar o trade.")
            return

        if signal_type == 'MARKET':
            if admin_user.approval_mode == 'AUTOMATIC':
                logger.info(f"Sinal A MERCADO aprovado. Replicando para {len(all_users_to_trade)} usuário(s)...")
                for user in all_users_to_trade:
                    await _execute_trade(signal_data, user, application, db, source_name)
                db.commit()
            elif admin_user.approval_mode == 'MANUAL':
                # A lógica de aprovação manual precisará ser refatorada no futuro para replicar a ação.
                # Por enquanto, ela apenas notificará o admin.
                logger.info(f"Modo MANUAL. Enviando sinal A MERCADO para aprovação do Admin.")
                # (Sua lógica de notificação de aprovação manual...)

        elif signal_type == 'LIMIT':
            logger.info(f"Sinal LIMITE aprovado. Replicando para {len(all_users_to_trade)} usuário(s)...")
            for user in all_users_to_trade:
                user_api_key = decrypt_data(user.api_key_encrypted)
                user_api_secret = decrypt_data(user.api_secret_encrypted)
                account_info = await get_account_info(user_api_key, user_api_secret)
                balance = float(account_info.get("data", [{}])[0].get('totalEquity', 0))

                limit_order_result = await place_limit_order(user_api_key, user_api_secret, signal_data, user, balance)

                if limit_order_result.get("success"):
                    order_id = limit_order_result["data"]["orderId"]
                    new_pending_signal = PendingSignal(
                        user_telegram_id=user.telegram_id,
                        symbol=symbol, order_id=order_id, signal_data=signal_data
                    )
                    db.add(new_pending_signal)
                    await application.bot.send_message(chat_id=user.telegram_id, text=f"✅ Ordem Limite para <b>{symbol}</b> foi posicionada. Monitorando...", parse_mode='HTML')
                else:
                    error = limit_order_result.get('error')
                    await application.bot.send_message(chat_id=user.telegram_id, text=f"❌ Falha ao posicionar sua ordem limite para <b>{symbol}</b>: {error}", parse_mode='HTML')
            db.commit()
    
    finally:
        db.close()

async def _execute_trade(signal_data: dict, user: User, application: Application, db: Session, source_name: str):
    """Função interna que abre uma posição na Bybit PARA UM USUÁRIO ESPECÍFICO."""
    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)
    
    account_info = await get_account_info(api_key, api_secret)
    if not account_info.get("success"):
        await application.bot.send_message(chat_id=user.telegram_id, text=f"❌ Falha ao buscar seu saldo Bybit para operar {signal_data['coin']}.")
        return

    balances = account_info.get("data", [])
    if not balances:
        await application.bot.send_message(chat_id=user.telegram_id, text=f"❌ Falha: Nenhuma informação de saldo recebida da Bybit para operar {signal_data['coin']}.")
        return

    balance = float(balances[0].get('totalEquity', 0))
    
    result = await place_order(api_key, api_secret, signal_data, user, balance)
    
    if result.get("success"):
        order_data = result['data']
        order_id = order_data['orderId']

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
        
        logger.info(f"Trade {order_id} para o usuário {user.telegram_id} salvo no DB.")
        await application.bot.send_message(chat_id=user.telegram_id, text=f"📈 <b>Ordem Aberta com Sucesso!</b>\n<b>Moeda:</b> {signal_data['coin']}", parse_mode='HTML')
    else:
        error_msg = result.get('error')
        await application.bot.send_message(chat_id=user.telegram_id, text=f"❌ <b>Falha ao Abrir Ordem</b>\n<b>Moeda:</b> {signal_data['coin']}\n<b>Motivo:</b> {error_msg}", parse_mode='HTML')