import logging
from telegram.ext import Application
from utils.config import ADMIN_ID, ERROR_CHANNEL_ID
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


async def send_error_report(application: Application, text: str, parse_mode: str = 'HTML') -> int | None:
    """
    Envia um relatório de erro para o canal/grupo configurado em ERROR_CHANNEL_ID;
    faz fallback para ADMIN_ID. Não lança exceções.
    """
    if not application:
        return None
    dest = ERROR_CHANNEL_ID if ERROR_CHANNEL_ID else ADMIN_ID
    if not dest:
        # Nenhum destino configurado
        return None
    try:
        msg = await application.bot.send_message(chat_id=dest, text=text[:4000], parse_mode=parse_mode)
        return getattr(msg, 'message_id', None)
    except Exception as e:
        logger.error(f"Falha ao enviar relatório de erro para canal: {e}")
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
