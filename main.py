import logging
import asyncio
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, 
    ConversationHandler, CallbackQueryHandler
)
from utils.config import TELEGRAM_TOKEN
from bot.handlers import (
    start, receive_invite_code, cancel, WAITING_CODE,
    config_api, receive_api_key, receive_api_secret, WAITING_API_KEY, WAITING_API_SECRET,
    remove_api_prompt, remove_api_action, CONFIRM_REMOVE_API,
    my_positions_handler, user_dashboard_handler, user_settings_handler,
    back_to_main_menu_handler,
    ask_entry_percent, receive_entry_percent, ASKING_ENTRY_PERCENT,
    ask_max_leverage, receive_max_leverage, ASKING_MAX_LEVERAGE,
    ask_min_confidence, receive_min_confidence, ASKING_MIN_CONFIDENCE,
    admin_menu, list_channels_handler, select_channel_to_monitor, select_topic_to_monitor,
    admin_view_targets_handler, back_to_admin_menu_handler,
    bot_config_handler, toggle_approval_mode_handler, handle_signal_approval, 
    ask_profit_target, receive_profit_target, ASKING_PROFIT_TARGET,
    ask_loss_limit, receive_loss_limit, ASKING_LOSS_LIMIT, 
    ask_coin_whitelist, receive_coin_whitelist, ASKING_COIN_WHITELIST,
    performance_menu_handler, list_closed_trades_handler,
    prompt_manual_close_handler, execute_manual_close_handler
)
from database.session import init_db
from services.telethon_service import start_signal_monitor
from core.position_tracker import run_tracker

# --- Configuração do Logging ---
logging.basicConfig(
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

async def run_ptb(application: Application, queue: asyncio.Queue):
    """Inicializa e roda a aplicação python-telegram-bot."""
    application.bot_data['comm_queue'] = queue
    logger.info("Inicializando o bot do Telegram (PTB)...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    logger.info("✅ Bot do Telegram (PTB) ativo.")

async def main():
    """Configura os handlers e inicia o PTB e o Telethon em paralelo."""
    init_db()
    comm_queue = asyncio.Queue()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    await comm_queue.put(application)

    # --- Handlers de Conversa ---
    register_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={ WAITING_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_invite_code)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    api_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(config_api, pattern='^config_api$')],
        states={
            WAITING_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_key)],
            WAITING_API_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_secret)],
        },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    remove_api_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(remove_api_prompt, pattern='^remove_api_prompt$')],
        states={ CONFIRM_REMOVE_API: [CallbackQueryHandler(remove_api_action, pattern='^remove_api_confirm|remove_api_cancel$')] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    settings_entry_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_entry_percent, pattern='^set_entry_percent$')],
        states={ ASKING_ENTRY_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_entry_percent)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    settings_leverage_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_max_leverage, pattern='^set_max_leverage$')],
        states={ ASKING_MAX_LEVERAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_max_leverage)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    settings_confidence_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_min_confidence, pattern='^set_min_confidence$')],
        states={ ASKING_MIN_CONFIDENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_min_confidence)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    profit_target_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_profit_target, pattern='^set_profit_target$')],
        states={ ASKING_PROFIT_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_profit_target)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    loss_limit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_loss_limit, pattern='^set_loss_limit$')],
        states={ ASKING_LOSS_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_loss_limit)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    whitelist_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_coin_whitelist, pattern='^set_coin_whitelist$')],
        states={ ASKING_COIN_WHITELIST: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_coin_whitelist)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )

    # Adicionando todos os handlers
    application.add_handler(register_conv)
    application.add_handler(api_conv)
    application.add_handler(remove_api_conv)
    application.add_handler(settings_entry_conv)
    application.add_handler(settings_leverage_conv)
    application.add_handler(settings_confidence_conv)
    application.add_handler(profit_target_conv)
    application.add_handler(loss_limit_conv)
    application.add_handler(whitelist_conv)
    
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(CallbackQueryHandler(list_channels_handler, pattern='^admin_list_channels$'))
    application.add_handler(CallbackQueryHandler(select_channel_to_monitor, pattern='^monitor_channel_'))
    application.add_handler(CallbackQueryHandler(select_topic_to_monitor, pattern='^monitor_topic_'))
    application.add_handler(CallbackQueryHandler(admin_view_targets_handler, pattern='^admin_view_targets$'))
    application.add_handler(CallbackQueryHandler(back_to_admin_menu_handler, pattern='^back_to_admin_menu$'))

    application.add_handler(CommandHandler("start", start))
    
    application.add_handler(CallbackQueryHandler(my_positions_handler, pattern='^user_positions$'))
    application.add_handler(CallbackQueryHandler(user_settings_handler, pattern='^user_settings$'))
    application.add_handler(CallbackQueryHandler(user_dashboard_handler, pattern='^user_dashboard$'))
    application.add_handler(CallbackQueryHandler(back_to_main_menu_handler, pattern='^back_to_main_menu$'))
    application.add_handler(CallbackQueryHandler(prompt_manual_close_handler, pattern='^confirm_close_'))
    application.add_handler(CallbackQueryHandler(execute_manual_close_handler, pattern='^execute_close_'))

    application.add_handler(CallbackQueryHandler(performance_menu_handler, pattern='^perf_'))
    
    application.add_handler(CallbackQueryHandler(list_closed_trades_handler, pattern='^list_closed_trades$'))

    application.add_handler(CallbackQueryHandler(bot_config_handler, pattern='^bot_config$'))
    application.add_handler(CallbackQueryHandler(toggle_approval_mode_handler, pattern='^toggle_approval_mode$'))

    application.add_handler(CallbackQueryHandler(handle_signal_approval, pattern=r'^(approve_signal_|reject_signal_)'))


    logger.info("Bot configurado. Iniciando todos os serviços...")

    await asyncio.gather(
        run_ptb(application, comm_queue),
        start_signal_monitor(comm_queue),
        run_tracker(application)
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot desligado pelo usuário.")
    except Exception as e:
        logger.critical(f"Erro crítico não tratado: {e}", exc_info=True)