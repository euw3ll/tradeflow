import logging
import asyncio
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, 
    ConversationHandler, CallbackQueryHandler, ContextTypes
)
from telegram.error import TelegramError, TimedOut, RetryAfter, NetworkError, Conflict
from utils.config import TELEGRAM_TOKEN
from bot.handlers import (
    start, receive_invite_code, cancel, WAITING_CODE,
    show_no_invite_info_handler, back_to_invite_welcome_handler, enter_invite_handler,
    config_api, receive_api_key, receive_api_secret, WAITING_API_KEY, WAITING_API_SECRET,
    remove_api_prompt, remove_api_action, CONFIRM_REMOVE_API,
    my_positions_handler, pending_positions_handler, cancel_pending_prompt_handler, execute_cancel_pending_handler,
    user_dashboard_handler, user_settings_handler,
    back_to_main_menu_handler,
    ask_entry_percent, receive_entry_percent, ASKING_ENTRY_PERCENT,
    ask_max_leverage, receive_max_leverage, ASKING_MAX_LEVERAGE,
    ask_min_confidence, receive_min_confidence, ASKING_MIN_CONFIDENCE,
    toggle_stop_strategy_handler,
    signal_filters_menu_handler, toggle_ma_filter_handler, toggle_rsi_filter_handler,
    ask_ma_period, receive_ma_period, ASKING_MA_PERIOD,
    admin_menu, list_channels_handler, select_channel_to_monitor, select_topic_to_monitor,
    admin_view_targets_handler, back_to_admin_menu_handler,
    bot_config_handler, toggle_approval_mode_handler, handle_signal_approval, 
    ask_profit_target, receive_profit_target, ASKING_PROFIT_TARGET,
    ask_loss_limit, receive_loss_limit, ASKING_LOSS_LIMIT, 
    ask_pending_expiry, receive_pending_expiry, ASKING_PENDING_EXPIRY_MINUTES,
    ask_coin_whitelist, receive_coin_whitelist, ASKING_COIN_WHITELIST,
    performance_menu_handler, list_closed_trades_handler,
    prompt_manual_close_handler, execute_manual_close_handler,
    open_settings_root_handler, notifications_settings_handler, refresh_active_messages_handler, open_information_handler,
    toggle_cleanup_mode_handler, ask_cleanup_minutes, receive_cleanup_minutes, ASKING_CLEANUP_MINUTES,
    toggle_alert_cleanup_mode_handler, ask_alert_cleanup_minutes, receive_alert_cleanup_minutes, ASKING_ALERT_CLEANUP_MINUTES,
    toggle_bot_status_handler,
    ask_stop_gain_trigger, receive_stop_gain_trigger, ASKING_STOP_GAIN_TRIGGER,
    ask_stop_gain_lock, receive_stop_gain_lock, ASKING_STOP_GAIN_LOCK,
    ask_be_trigger, receive_be_trigger, ASKING_BE_TRIGGER,
    ask_ts_trigger, receive_ts_trigger, ASKING_TS_TRIGGER,
    show_initial_stop_menu_handler, toggle_initial_sl_mode_handler,
    ask_initial_sl_fixed, receive_initial_sl_fixed, ASKING_INITIAL_SL_FIXED,
    ask_risk_per_trade, receive_risk_per_trade, ASKING_RISK_PER_TRADE,
    ask_circuit_threshold, receive_circuit_threshold, ASKING_CIRCUIT_THRESHOLD,
    ask_circuit_pause, receive_circuit_pause, ASKING_CIRCUIT_PAUSE,
    ask_ma_timeframe, set_ma_timeframe,
    ask_rsi_oversold, receive_rsi_oversold, ASKING_RSI_OVERSOLD,
    ask_rsi_overbought, receive_rsi_overbought, ASKING_RSI_OVERBOUGHT,
    show_risk_menu_handler, show_stopgain_menu_handler, show_circuit_menu_handler,
    back_to_settings_menu_handler, back_from_whitelist_handler,
    show_tp_strategy_menu_handler, ask_tp_distribution, receive_tp_distribution, ASKING_TP_DISTRIBUTION,
    cycle_tp_preset_handler,
    onboard_select_preset_handler, onboard_accept_terms_handler, onboard_decline_terms_handler,
    info_learn_start_handler, info_learn_nav_handler,
)
from services.telethon_service import start_signal_monitor
from core.position_tracker import run_tracker
from services.notification_service import send_user_alert

import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="telegram.ext.conversationhandler")
logging.getLogger("telegram").setLevel(logging.ERROR)
logging.getLogger("telegram.ext").setLevel(logging.ERROR)

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

async def on_error(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Error handler global: loga a exceção com contexto e avisa o usuário (apenas em chat privado)."""
    logger = logging.getLogger(__name__)
    try:
        # Curto‑circuito para erros transitórios de rede do Telegram
        err = context.error
        if isinstance(err, (TimedOut, RetryAfter, NetworkError)) or (
            err and any(s in str(err) for s in ("ConnectTimeout", "ReadTimeout"))
        ):
            logger.debug("Transient Telegram network error suppressed: %s", repr(err))
            return
        if isinstance(err, Conflict):
            logger.error("Conflict: another getUpdates is running. Ensure single instance.")
            return
        # Extra contexto útil para diagnosticar
        ctx = {
            "chat_id": getattr(getattr(update, "effective_chat", None), "id", None),
            "chat_type": getattr(getattr(update, "effective_chat", None), "type", None),
            "user_id": getattr(getattr(update, "effective_user", None), "id", None),
            "callback_data": getattr(getattr(update, "callback_query", None), "data", None),
            "message_text": getattr(getattr(update, "message", None), "text", None),
        }
        logger.error("Unhandled error | context=%s", ctx, exc_info=context.error)
    except Exception:
        logger.error("Unhandled error (failed to log context)", exc_info=context.error)
    try:
        if update and update.effective_chat and update.effective_chat.type == "private":
            await send_user_alert(context.application, update.effective_chat.id,
                                  "⚠️ Ocorreu um erro inesperado. Já registrei aqui e vou corrigir.")
    except TelegramError:
        # Evita encadear erros caso o envio falhe
        pass

async def main():
    """Configura os handlers e inicia o PTB e o Telethon em paralelo."""
    comm_queue = asyncio.Queue()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    await comm_queue.put(application)

    # --- Handlers de Conversa ---
    register_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CallbackQueryHandler(enter_invite_handler, pattern='^enter_invite$')],
        states={ WAITING_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_invite_code)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    api_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(config_api, pattern='^config_api$')],
        states={
            WAITING_API_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_key)],
            WAITING_API_SECRET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_api_secret)],
        },
        # MUDANÇA: 'per_message' alterado para False para manter o estado da conversa.
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    remove_api_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(remove_api_prompt, pattern='^remove_api_prompt$')],
        states={ CONFIRM_REMOVE_API: [CallbackQueryHandler(remove_api_action, pattern='^remove_api_confirm|remove_api_cancel$')] },
        # MUDANÇA: 'per_message' alterado para False para manter o estado da conversa.
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    settings_entry_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_entry_percent, pattern='^set_entry_percent$')],
        states={ ASKING_ENTRY_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_entry_percent)] },
        # MUDANÇA: 'per_message' alterado para False para manter o estado da conversa.
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    settings_leverage_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_max_leverage, pattern='^set_max_leverage$')],
        states={ ASKING_MAX_LEVERAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_max_leverage)] },
        # MUDANÇA: 'per_message' alterado para False para manter o estado da conversa.
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    settings_confidence_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_min_confidence, pattern='^set_min_confidence$')],
        states={ ASKING_MIN_CONFIDENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_min_confidence)] },
        # MUDANÇA: 'per_message' alterado para False para manter o estado da conversa.
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    profit_target_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_profit_target, pattern='^set_profit_target$')],
        states={ ASKING_PROFIT_TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_profit_target)] },
        # MUDANÇA: 'per_message' alterado para False para manter o estado da conversa.
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    loss_limit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_loss_limit, pattern='^set_loss_limit$')],
        states={ ASKING_LOSS_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_loss_limit)] },
        # MUDANÇA: 'per_message' alterado para False para manter o estado da conversa.
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    whitelist_conv = ConversationHandler(
    entry_points=[CallbackQueryHandler(ask_coin_whitelist, pattern='^set_coin_whitelist$')],
    states={
        ASKING_COIN_WHITELIST: [
            # novo: permite clicar em "Voltar" enquanto está no prompt
            CallbackQueryHandler(back_from_whitelist_handler, pattern='^back_to_settings_menu$'),
            # já existia: captura o texto enviado com a lista
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_coin_whitelist),
        ]
    },
    fallbacks=[CommandHandler("cancel", cancel)],
    per_message=False, per_user=True,
    )
    stop_gain_trigger_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_stop_gain_trigger, pattern='^set_stop_gain_trigger$')],
        states={ ASKING_STOP_GAIN_TRIGGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_stop_gain_trigger)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    stop_gain_lock_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_stop_gain_lock, pattern='^set_stop_gain_lock$')],
        states={ ASKING_STOP_GAIN_LOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_stop_gain_lock)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    be_trigger_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_be_trigger, pattern='^set_be_trigger$')],
        states={ ASKING_BE_TRIGGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_be_trigger)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    ts_trigger_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_ts_trigger, pattern='^set_ts_trigger$')],
        states={ ASKING_TS_TRIGGER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ts_trigger)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    circuit_threshold_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_circuit_threshold, pattern='^set_circuit_threshold$')],
        states={ ASKING_CIRCUIT_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_circuit_threshold)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    circuit_pause_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_circuit_pause, pattern='^set_circuit_pause$')],
        states={ ASKING_CIRCUIT_PAUSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_circuit_pause)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    ma_period_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_ma_period, pattern='^set_ma_period$')],
        states={ ASKING_MA_PERIOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ma_period)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    rsi_oversold_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_rsi_oversold, pattern='^set_rsi_oversold$')],
        states={ ASKING_RSI_OVERSOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_rsi_oversold)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    rsi_overbought_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_rsi_overbought, pattern='^set_rsi_overbought$')],
        states={ ASKING_RSI_OVERBOUGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_rsi_overbought)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    tp_distribution_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_tp_distribution, pattern='^ask_tp_distribution$')],
        states={
            ASKING_TP_DISTRIBUTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_tp_distribution)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False, per_user=True,
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
    application.add_handler(stop_gain_trigger_conv)
    application.add_handler(stop_gain_lock_conv)
    application.add_handler(be_trigger_conv)
    application.add_handler(ts_trigger_conv)
    
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(CallbackQueryHandler(list_channels_handler, pattern='^admin_list_channels$'))
    application.add_handler(CallbackQueryHandler(select_channel_to_monitor, pattern='^monitor_channel_'))
    application.add_handler(CallbackQueryHandler(select_topic_to_monitor, pattern='^monitor_topic_'))
    application.add_handler(CallbackQueryHandler(admin_view_targets_handler, pattern='^admin_view_targets$'))
    application.add_handler(CallbackQueryHandler(back_to_admin_menu_handler, pattern='^back_to_admin_menu$'))

    application.add_handler(CommandHandler("start", start))
    # Menus principais do /start consolidado
    application.add_handler(CallbackQueryHandler(open_settings_root_handler, pattern='^open_settings_root$'))
    application.add_handler(CallbackQueryHandler(open_information_handler, pattern='^open_info$'))
    application.add_handler(CallbackQueryHandler(info_learn_start_handler, pattern='^info_learn_start$'))
    application.add_handler(CallbackQueryHandler(info_learn_nav_handler, pattern='^info_learn_nav_'))
    # Fluxo para usuários sem convite
    application.add_handler(CallbackQueryHandler(show_no_invite_info_handler, pattern='^no_invite_info$'))
    application.add_handler(CallbackQueryHandler(back_to_invite_welcome_handler, pattern='^back_to_invite_welcome$'))
    
    application.add_handler(CallbackQueryHandler(my_positions_handler, pattern='^user_positions$'))
    application.add_handler(CallbackQueryHandler(pending_positions_handler, pattern='^user_pending_positions$'))
    application.add_handler(CallbackQueryHandler(cancel_pending_prompt_handler, pattern='^confirm_cancel_pending_'))
    application.add_handler(CallbackQueryHandler(execute_cancel_pending_handler, pattern='^execute_cancel_pending_'))
    application.add_handler(CallbackQueryHandler(user_settings_handler, pattern='^user_settings$'))
    application.add_handler(CallbackQueryHandler(user_dashboard_handler, pattern='^user_dashboard$'))
    application.add_handler(CallbackQueryHandler(toggle_bot_status_handler, pattern='^toggle_bot_status$'))
    application.add_handler(CallbackQueryHandler(back_to_main_menu_handler, pattern='^back_to_main_menu$'))
    application.add_handler(CallbackQueryHandler(prompt_manual_close_handler, pattern='^confirm_close_'))
    application.add_handler(CallbackQueryHandler(execute_manual_close_handler, pattern='^execute_close_'))

    application.add_handler(CallbackQueryHandler(toggle_stop_strategy_handler, pattern='^set_stop_strategy$'))

    application.add_handler(CallbackQueryHandler(performance_menu_handler, pattern='^perf_'))
    
    application.add_handler(CallbackQueryHandler(list_closed_trades_handler, pattern='^list_closed_trades$'))

    application.add_handler(CallbackQueryHandler(bot_config_handler, pattern='^bot_config$'))
    application.add_handler(CallbackQueryHandler(toggle_approval_mode_handler, pattern='^toggle_approval_mode$'))
    # Notificações
    application.add_handler(CallbackQueryHandler(notifications_settings_handler, pattern='^notifications_settings$'))
    application.add_handler(CallbackQueryHandler(toggle_cleanup_mode_handler, pattern='^toggle_cleanup_mode$'))
    cleanup_minutes_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_cleanup_minutes, pattern='^ask_cleanup_minutes$')],
        states={ ASKING_CLEANUP_MINUTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_cleanup_minutes)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    application.add_handler(cleanup_minutes_conv)
    # Alert cleanup
    application.add_handler(CallbackQueryHandler(toggle_alert_cleanup_mode_handler, pattern='^toggle_alert_cleanup_mode$'))
    alert_cleanup_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_alert_cleanup_minutes, pattern='^ask_alert_cleanup_minutes$')],
        states={ ASKING_ALERT_CLEANUP_MINUTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_alert_cleanup_minutes)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    application.add_handler(alert_cleanup_conv)
    application.add_handler(CallbackQueryHandler(refresh_active_messages_handler, pattern='^refresh_active_messages$'))

    application.add_handler(CallbackQueryHandler(handle_signal_approval, pattern=r'^(approve_signal_|reject_signal_)'))

    application.add_handler(stop_gain_lock_conv)
    application.add_handler(circuit_threshold_conv)
    application.add_handler(circuit_pause_conv)

    # Expiração de pendentes
    pending_expiry_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_pending_expiry, pattern='^set_pending_expiry$')],
        states={ ASKING_PENDING_EXPIRY_MINUTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pending_expiry)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    application.add_handler(pending_expiry_conv)

    application.add_handler(CallbackQueryHandler(signal_filters_menu_handler, pattern='^signal_filters_menu$'))
    application.add_handler(CallbackQueryHandler(toggle_ma_filter_handler, pattern='^toggle_ma_filter$'))
    application.add_handler(CallbackQueryHandler(toggle_rsi_filter_handler, pattern='^toggle_rsi_filter$'))
    application.add_handler(ma_period_conv)

    application.add_handler(CallbackQueryHandler(ask_ma_timeframe, pattern='^ask_ma_timeframe$'))
    application.add_handler(CallbackQueryHandler(set_ma_timeframe, pattern='^set_ma_timeframe_'))
    application.add_handler(rsi_oversold_conv)
    application.add_handler(rsi_overbought_conv)
    application.add_handler(tp_distribution_conv)

    # Onboarding: preset e termos
    application.add_handler(CallbackQueryHandler(onboard_select_preset_handler, pattern='^onboard_risk_'))
    application.add_handler(CallbackQueryHandler(onboard_accept_terms_handler, pattern='^onboard_accept_terms$'))
    application.add_handler(CallbackQueryHandler(onboard_decline_terms_handler, pattern='^onboard_decline_terms$'))

    application.add_handler(CallbackQueryHandler(show_risk_menu_handler, pattern='^settings_risk$'))
    application.add_handler(CallbackQueryHandler(show_stopgain_menu_handler, pattern='^settings_stopgain$'))
    application.add_handler(CallbackQueryHandler(show_initial_stop_menu_handler, pattern='^settings_initial_stop$'))
    application.add_handler(CallbackQueryHandler(toggle_initial_sl_mode_handler, pattern='^toggle_initial_sl_mode$'))
    # Conversas para Stop Inicial
    initial_sl_fixed_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_initial_sl_fixed, pattern='^ask_initial_sl_fixed$')],
        states={ ASKING_INITIAL_SL_FIXED: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_initial_sl_fixed)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    application.add_handler(initial_sl_fixed_conv)
    risk_per_trade_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_risk_per_trade, pattern='^ask_risk_per_trade$')],
        states={ ASKING_RISK_PER_TRADE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_risk_per_trade)] },
        fallbacks=[CommandHandler("cancel", cancel)], per_message=False, per_user=True,
    )
    application.add_handler(risk_per_trade_conv)
    application.add_handler(CallbackQueryHandler(show_circuit_menu_handler, pattern='^settings_circuit$'))
    application.add_handler(CallbackQueryHandler(back_to_settings_menu_handler, pattern='^back_to_settings_menu$'))
    application.add_handler(CallbackQueryHandler(show_tp_strategy_menu_handler, pattern='^show_tp_strategy$'))
    application.add_handler(CallbackQueryHandler(cycle_tp_preset_handler, pattern='^cycle_tp_preset$'))

    application.add_error_handler(on_error)

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
