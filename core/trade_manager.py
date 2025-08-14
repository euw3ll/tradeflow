import os
import logging
from typing import Tuple
from telegram.ext import Application
from sqlalchemy.orm import Session # <-- Adicionado 'Session' para a anotação de tipo
from database.session import SessionLocal
from database.models import User, Trade, PendingSignal, SignalForApproval
from services.bybit_service import place_order, get_account_info
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
    Roteador de sinais: verifica o modo de aprovação do usuário e decide se
    abre a ordem automaticamente ou se envia para aprovação manual.
    """
    signal_type = signal_data.get("type")
    symbol = signal_data.get("coin")
    db = SessionLocal()
    try:
        if signal_type == 'CANCELLED':
            pending = db.query(PendingSignal).filter_by(symbol=symbol, user_telegram_id=ADMIN_ID).first()
            if pending:
                db.delete(pending)
                db.commit()
                await send_notification(application, f"⚠️ <b>Monitoramento Cancelado</b>\nO sinal limite para <b>{symbol}</b> foi cancelado pela fonte '{source_name}'.")
            return

        admin_user = db.query(User).filter_by(telegram_id=ADMIN_ID).first()
        if not admin_user:
            logger.error("Admin não encontrado.")
            return

        aprovado, motivo = _avaliar_sinal(signal_data, admin_user)
        if not aprovado:
            rejection_msg = f"⚠️ <b>Sinal para {symbol} Ignorado</b>\n<b>Fonte:</b> {source_name}\n<b>Motivo:</b> {motivo}"
            await send_notification(application, rejection_msg)
            return

        if admin_user.approval_mode == 'AUTOMATIC':
            logger.info(f"Modo AUTOMÁTICO. Tentando abrir ordem para {symbol}...")
            await _execute_trade(signal_data, admin_user, application, db, source_name)
        
        elif admin_user.approval_mode == 'MANUAL':
            logger.info(f"Modo MANUAL. Enviando sinal para aprovação: {symbol}")
            
            new_signal_for_approval = SignalForApproval(
                user_telegram_id=ADMIN_ID,
                symbol=symbol,
                source_name=source_name,
                signal_data=signal_data
            )
            db.add(new_signal_for_approval)
            db.commit()

            signal_details = (
                f"<b>Sinal Recebido de: {source_name}</b>\n\n"
                f"<b>Moeda:</b> {signal_data['coin']}\n"
                f"<b>Tipo:</b> {signal_data['order_type']}\n"
                f"<b>Entrada:</b> {signal_data['entries'][0]}\n"
                f"<b>Stop:</b> {signal_data['stop_loss']}\n"
                f"<b>Alvo 1:</b> {signal_data['targets'][0]}\n\n"
                f"O sinal passou nos seus filtros. Você aprova a entrada?"
            )
            
            sent_message = await application.bot.send_message(
                chat_id=ADMIN_ID,
                text=signal_details,
                parse_mode='HTML',
                reply_markup=signal_approval_keyboard(new_signal_for_approval.id)
            )
            
            new_signal_for_approval.approval_message_id = sent_message.message_id
            db.commit()
    finally:
        db.close()

# --- ANOTAÇÃO DE TIPO CORRIGIDA E CÓDIGO FALTANDO ADICIONADO ---
async def _execute_trade(signal_data: dict, user: User, application: Application, db: Session, source_name: str):
    """Função interna que contém a lógica para abrir uma posição na Bybit."""
    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)
    
    account_info = get_account_info(api_key, api_secret)
    if not account_info.get("success"):
        await send_notification(application, f"❌ Falha ao buscar saldo da Bybit para operar {signal_data['coin']}.")
        return
        
    balance = float(account_info['data']['totalEquity'])
    result = place_order(api_key, api_secret, signal_data, user, balance)
    
    if result.get("success"):
        order_data = result['data']
        order_id = order_data['orderId']

        # --- CÓDIGO FALTANDO PARA SALVAR O TRADE ---
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
        # ------------------------------------

        await send_notification(application, f"📈 <b>Ordem Aberta com Sucesso!</b>\n<b>Moeda:</b> {signal_data['coin']}\n<b>ID:</b> {order_id}")
    else:
        error_msg = result.get('error')
        await send_notification(application, f"❌ <b>Falha ao Abrir Ordem</b>\n<b>Moeda:</b> {signal_data['coin']}\n<b>Motivo:</b> {error_msg}")