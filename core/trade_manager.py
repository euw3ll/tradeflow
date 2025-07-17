import os
import logging
from telegram.ext import Application
from database.session import SessionLocal
from database.models import User, Trade
from services.bybit_service import place_order, get_account_info
from services.notification_service import send_notification
from utils.security import decrypt_data

logger = logging.getLogger(__name__)
ADMIN_ID = int(os.getenv('ADMIN_TELEGRAM_ID', 0))

async def process_new_signal(signal_data: dict, application: Application):
    logger.info(f"Processando sinal para abrir ordem: {signal_data}")
    await send_notification(application, f"🔔 <b>Sinal Recebido</b>\n<b>Moeda:</b> {signal_data['coin']} | <b>Tipo:</b> {signal_data['order_type']}")
    
    db = SessionLocal()
    try:
        admin_user = db.query(User).filter_by(telegram_id=ADMIN_ID).first()
        if not admin_user or not admin_user.api_key_encrypted:
            logger.error("Admin não encontrado ou sem API configurada.")
            await send_notification(application, "❌ Falha: Chaves de API não configuradas.")
            return

        api_key = decrypt_data(admin_user.api_key_encrypted)
        api_secret = decrypt_data(admin_user.api_secret_encrypted)
        
        account_info = get_account_info(api_key, api_secret)
        if not account_info.get("success"):
            logger.error("Não foi possível buscar o saldo da conta. Abortando trade.")
            await send_notification(application, "❌ Falha ao buscar saldo da Bybit. Verifique as chaves de API.")
            return
        balance = float(account_info['data']['totalEquity'])
        
        result = place_order(api_key, api_secret, signal_data, admin_user, balance)

        min_confidence_setting = admin_user.min_confidence
        signal_confidence = signal_data.get('confidence')

        if signal_confidence is not None and signal_confidence < min_confidence_setting:
            rejection_msg = (
                f"⚠️ <b>Sinal para {signal_data['coin']} Ignorado</b>\n"
                f"<b>Motivo:</b> Confiança do sinal ({signal_confidence:.2f}%) "
                f"é menor que o seu mínimo configurado ({min_confidence_setting:.2f}%)."
            )
            logger.warning(rejection_msg.replace('<b>', '').replace('</b>', ''))
            await send_notification(application, rejection_msg)
            return # Aborta o processamento do trade
        
        logger.info("✅ Sinal aprovado pelos seus critérios. Prosseguindo para abrir ordem...")
        await send_notification(application, "✅ Sinal aprovado pelos seus critérios. Abrindo ordem...")
        
        if result.get("success"):
            order_data = result['data']
            order_id = order_data['orderId']
            logger.info(f"✅ Ordem {order_id} aberta com sucesso!")
            
            # --- NOVA LÓGICA: SALVAR O TRADE NO BANCO ---
            new_trade = Trade(
                user_telegram_id=ADMIN_ID,
                order_id=order_id,
                symbol=signal_data['coin'],
                side=signal_data['order_type'],
                qty=float(result['data']['qty']), # Pega a quantidade real da resposta da Bybit
                entry_price=signal_data['entries'][0], # Idealmente, pegaríamos o preço real de execução
                stop_loss=signal_data['stop_loss'],
                initial_targets=signal_data['targets'],
                status='ACTIVE',
                remaining_qty=float(result['data']['qty'])
            )
            db.add(new_trade)
            db.commit()
            logger.info(f"Trade {order_id} salvo no banco de dados para rastreamento.")
            
            await send_notification(
                application,
                f"✅ <b>Ordem Aberta com Sucesso!</b>\n<b>Moeda:</b> {signal_data['coin']}\n<b>ID:</b> {order_id}"
            )
        else:
            error_msg = result.get('error')
            logger.error(f"❌ Falha ao abrir ordem na Bybit: {error_msg}")
            await send_notification(
                application,
                f"❌ <b>Falha ao Abrir Ordem</b>\n<b>Moeda:</b> {signal_data['coin']}\n<b>Motivo:</b> {error_msg}"
            )
    finally:
        db.close()