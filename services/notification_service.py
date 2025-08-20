import logging
from telegram.ext import Application
from database.crud import get_admin_user

logger = logging.getLogger(__name__)

async def send_notification(application: Application, message: str):
    """
    Envia uma mensagem de notificação para o administrador do bot.
    """
    if not application:
        logger.warning("Tentativa de enviar notificação sem a instância da aplicação.")
        return
    admin = get_admin_user()
    if not admin:
        logger.warning("Nenhum administrador configurado para receber notificações.")
        return
    try:
        await application.bot.send_message(
            chat_id=admin.telegram_id,
            text=message,
            parse_mode='HTML'
        )
        logger.info(f"Notificação enviada para o admin: {message[:50]}...")
    except Exception as e:
        logger.error(f"Falha ao enviar notificação para o admin: {e}")