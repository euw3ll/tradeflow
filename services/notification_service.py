import logging
from telegram.ext import Application
from utils.config import ADMIN_ID
from database.session import SessionLocal
from database.models import AlertMessage

logger = logging.getLogger(__name__)

async def send_notification(application: Application, message: str):
    """
    Envia uma mensagem de notificação para o administrador do bot.
    """
    if not application:
        logger.warning("Tentativa de enviar notificação sem a instância da aplicação.")
        return
    try:
        await application.bot.send_message(
            chat_id=ADMIN_ID,
            text=message,
            parse_mode='HTML'
        )
        logger.info(f"Notificação enviada para o admin: {message[:50]}...")
    except Exception as e:
        logger.error(f"Falha ao enviar notificação para o admin: {e}")


async def send_user_alert(application: Application, user_id: int, text: str, parse_mode: str = 'HTML') -> int | None:
    """
    Envia um alerta geral ao usuário e registra para limpeza posterior
    conforme as preferências de 'alert_cleanup_*'. Retorna o message_id
    ou None em caso de falha.
    """
    if not application:
        return None
    try:
        msg = await application.bot.send_message(chat_id=user_id, text=text, parse_mode=parse_mode)
        message_id = getattr(msg, 'message_id', None)
        if message_id is not None:
            db = SessionLocal()
            try:
                db.add(AlertMessage(user_telegram_id=user_id, message_id=message_id))
                db.commit()
            finally:
                db.close()
        return message_id
    except Exception as e:
        logging.getLogger(__name__).error(f"Falha ao enviar alerta ao usuário {user_id}: {e}")
        return None
