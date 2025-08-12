import os
import logging
from typing import Tuple
from telegram.ext import Application
from database.session import SessionLocal
from database.models import User, Trade, PendingSignal
from services.bybit_service import place_order, get_account_info
from services.notification_service import send_notification
from utils.security import decrypt_data
from utils.config import ADMIN_ID

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

    # Adicione outros filtros aqui no futuro (ex: Margem recomendada, etc.)

    return True, "Sinal aprovado pelos seus critérios."


async def process_new_signal(signal_data: dict, application: Application, source_name: str):
    """
    Roteador de sinais: decide se abre, monitora ou cancela um trade
    com base no tipo do sinal.
    """
    signal_type = signal_data.get("type")
    symbol = signal_data.get("coin")
    db = SessionLocal()
    try:
        # --- ROTA 1: SINAL CANCELADO ---
        if signal_type == 'CANCELLED':
            pending = db.query(PendingSignal).filter_by(symbol=symbol, user_telegram_id=ADMIN_ID).first()
            if pending:
                db.delete(pending)
                db.commit()
                await send_notification(application, f"⚠️ <b>Monitoramento Cancelado</b>\nO sinal limite para <b>{symbol}</b> foi cancelado pela fonte '{source_name}'.")
                logger.info(f"Sinal pendente para {symbol} foi cancelado e removido.")
            return

        # Para LIMITE e MERCADO, primeiro buscamos o usuário e aplicamos os filtros
        admin_user = db.query(User).filter_by(telegram_id=ADMIN_ID).first()
        if not admin_user or not admin_user.api_key_encrypted:
            logger.error("Admin não encontrado ou sem API configurada.")
            return

        # --- AVALIAÇÃO DO SINAL (FILTRO) ---
        aprovado, motivo = _avaliar_sinal(signal_data, admin_user)
        if not aprovado:
            rejection_msg = f"⚠️ <b>Sinal para {symbol} Ignorado</b>\n<b>Fonte:</b> {source_name}\n<b>Motivo:</b> {motivo}"
            logger.warning(rejection_msg.replace('<b>', '').replace('</b>', ''))
            await send_notification(application, rejection_msg)
            return

        # Se o sinal foi aprovado, o bot notifica e continua
        await send_notification(application, f"✅ <b>Sinal Aprovado</b>\n<b>Fonte:</b> {source_name}\n<b>Moeda:</b> {symbol}")

        # --- ROTA 2: ORDEM LIMITE (ARMAZENAR E VIGIAR) ---
        if signal_type == 'LIMIT':
            existing = db.query(PendingSignal).filter_by(symbol=symbol, user_telegram_id=ADMIN_ID).first()
            if not existing:
                new_pending = PendingSignal(user_telegram_id=ADMIN_ID, symbol=symbol, signal_data=signal_data)
                db.add(new_pending)
                db.commit()
                await send_notification(application, f"⏳ <b>Sinal Limite Armazenado</b>\nO bot está agora monitorando <b>{symbol}</b> para uma possível entrada.")
            return
            
        # --- ROTA 3: ORDEM A MERCADO (AÇÃO IMEDIATA) ---
        elif signal_type == 'MARKET':
            pending = db.query(PendingSignal).filter_by(symbol=symbol, user_telegram_id=ADMIN_ID).first()
            if pending:
                db.delete(pending)
                db.commit()
                await send_notification(application, f"🚀 <b>Sinal Limite Ativado para {symbol}!</b>\nIniciando processo de abertura de ordem...")

            api_key = decrypt_data(admin_user.api_key_encrypted)
            api_secret = decrypt_data(admin_user.api_secret_encrypted)
            
            account_info = get_account_info(api_key, api_secret)
            if not account_info.get("success"):
                await send_notification(application, f"❌ Falha ao buscar saldo da Bybit para operar {symbol}.")
                return
            balance = float(account_info['data']['totalEquity'])
            
            result = place_order(api_key, api_secret, signal_data, admin_user, balance)
            
            if result.get("success"):
                order_id = result['data']['orderId']
                # ... (código para salvar o trade na tabela 'trades') ...
                await send_notification(application, f"📈 <b>Ordem Aberta com Sucesso!</b>\n<b>Moeda:</b> {symbol}\n<b>ID:</b> {order_id}")
            else:
                error_msg = result.get('error')
                await send_notification(application, f"❌ <b>Falha ao Abrir Ordem</b>\n<b>Moeda:</b> {symbol}\n<b>Motivo:</b> {error_msg}")
    
    finally:
        db.close()