from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.crud import get_user_by_id

def main_menu_keyboard(telegram_id: int):
    """
    Menu principal com ações do dia a dia e uma entrada única para Configurações.
    """
    user = get_user_by_id(telegram_id)
    has_api_keys = user and user.api_key_encrypted is not None

    keyboard = []
    if has_api_keys:
        keyboard.append([InlineKeyboardButton("💼 Meu Painel", callback_data='user_dashboard')])
        keyboard.append([InlineKeyboardButton("📊 Minhas Posições", callback_data='user_positions')])
        keyboard.append([InlineKeyboardButton("📈 Desempenho", callback_data='perf_today')])
        keyboard.append([InlineKeyboardButton("⚙️ Configurações", callback_data='open_settings_root')])
        keyboard.append([InlineKeyboardButton("ℹ️ Informações", callback_data='open_info')])
    else:
        keyboard.append([InlineKeyboardButton("⚙️ Configurar API Bybit", callback_data='config_api')])
        keyboard.append([InlineKeyboardButton("ℹ️ Informações", callback_data='open_info')])

    return InlineKeyboardMarkup(keyboard)

def invite_welcome_keyboard():
    """Teclado inicial para usuários sem cadastro/convite."""
    keyboard = [
        [InlineKeyboardButton("ℹ️ Como funciona e acesso", callback_data='no_invite_info')],
        [InlineKeyboardButton("🎟️ Eu tenho um convite", callback_data='enter_invite')],
    ]
    return InlineKeyboardMarkup(keyboard)

def invite_info_keyboard():
    """Teclado para a tela de explicação de acesso por convite."""
    keyboard = [
        [InlineKeyboardButton("🎟️ Eu tenho um convite", callback_data='enter_invite')],
        [InlineKeyboardButton("⬅️ Voltar", callback_data='back_to_invite_welcome')],
    ]
    return InlineKeyboardMarkup(keyboard)

def dashboard_menu_keyboard(user):
    """Retorna o teclado para o painel do usuário (sem o toggle do bot)."""
    keyboard = [
        [InlineKeyboardButton("🗑️ Remover API", callback_data='remove_api_prompt')],
        [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='back_to_main_menu')]
    ]
    return InlineKeyboardMarkup(keyboard)

def admin_menu_keyboard():
    """Retorna o teclado do menu de administrador."""
    keyboard = [
        [InlineKeyboardButton("📡 Listar Grupos/Canais", callback_data='admin_list_channels')],
        # --- NOVO BOTÃO ---
        [InlineKeyboardButton("👁️ Ver Alvos Ativos", callback_data='admin_view_targets')]
    ]
    return InlineKeyboardMarkup(keyboard)

def view_targets_keyboard():
    """Retorna o teclado para a tela de visualização de alvos, com um botão de voltar."""
    keyboard = [
        [InlineKeyboardButton("⬅️ Voltar ao Menu Admin", callback_data='back_to_admin_menu')]
    ]
    return InlineKeyboardMarkup(keyboard)

def confirm_remove_keyboard():
    """Retorna o teclado de confirmação para remover a API."""
    keyboard = [
        [InlineKeyboardButton("✅ Sim, remover", callback_data='remove_api_confirm')],
        [InlineKeyboardButton("❌ Não, cancelar", callback_data='remove_api_cancel')],
    ]
    return InlineKeyboardMarkup(keyboard)

def settings_menu_keyboard(user) -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("🧮 Risco & Tamanho", callback_data="settings_risk"),
            InlineKeyboardButton("🛡️ Stop-Gain", callback_data="settings_stopgain"),
        ],
        [InlineKeyboardButton("🛑 Stop Inicial", callback_data="settings_initial_stop")],
        [
            InlineKeyboardButton("🚫 Disjuntor", callback_data="settings_circuit"),
            InlineKeyboardButton("✅ Whitelist", callback_data="set_coin_whitelist"),
        ],
        [
            InlineKeyboardButton("🔬 Filtros de Sinais", callback_data="signal_filters_menu"),
            InlineKeyboardButton("🎯 Estratégia de TP", callback_data="show_tp_strategy"),
        ],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="open_settings_root")],
    ]
    return InlineKeyboardMarkup(kb)

def initial_stop_menu_keyboard(user) -> InlineKeyboardMarkup:
    mode_raw = (getattr(user, 'initial_sl_mode', 'ADAPTIVE') or 'ADAPTIVE').upper()
    if mode_raw == 'FIXED':
        mode_text = "Modo: Fixo (%)"
    elif mode_raw in ('FOLLOW', 'FOLLOW_SIGNAL', 'SIGNAL'):
        mode_text = "Modo: Seguir SL do Sinal"
    else:
        mode_text = "Modo: Adaptativo (Risco por Trade)"
    fixed_pct = float(getattr(user, 'initial_sl_fixed_pct', 1.0) or 1.0)
    risk_pct = float(getattr(user, 'risk_per_trade_pct', 1.0) or 1.0)
    max_manual = float(getattr(user, 'adaptive_sl_max_pct', 0.0) or 0.0)
    tighten_pct = float(getattr(user, 'adaptive_sl_tighten_pct', 0.0) or 0.0)
    timeout_min = int(getattr(user, 'adaptive_sl_timeout_minutes', 0) or 0)

    try:
        entry_pct = float(getattr(user, 'entry_size_percent', 0) or 0) / 100.0
        lev = float(getattr(user, 'max_leverage', 0) or 0)
        max_sl_pct = (risk_pct / 100.0) / (entry_pct * lev) * 100.0 if entry_pct > 0 and lev > 0 else None
    except Exception:
        max_sl_pct = None

    kb = []
    kb.append([InlineKeyboardButton(mode_text, callback_data='toggle_initial_sl_mode')])
    if mode_raw == 'FIXED':
        kb.append([InlineKeyboardButton(f"% Fixo: {fixed_pct:.2f}%", callback_data='ask_initial_sl_fixed')])
    elif mode_raw not in ('FOLLOW', 'FOLLOW_SIGNAL', 'SIGNAL'):
        # ADAPTIVE
        if max_sl_pct is not None and max_sl_pct > 0:
            label = f"Risco por Trade: {risk_pct:.2f}% (SL máx ~ {max_sl_pct:.2f}%)"
        else:
            label = f"Risco por Trade: {risk_pct:.2f}% (SL máx ~ —)"
        kb.append([InlineKeyboardButton(label, callback_data='ask_risk_per_trade')])

        if max_manual > 0:
            manual_label = f"SL Máx Manual: {max_manual:.2f}%"
        else:
            manual_label = "SL Máx Manual: Automático"
        kb.append([InlineKeyboardButton(manual_label, callback_data='ask_adaptive_sl_max')])

        if tighten_pct > 0:
            tighten_label = f"Corte Dinâmico: {tighten_pct:.2f}%"
        else:
            tighten_label = "Corte Dinâmico: Desativado"
        kb.append([InlineKeyboardButton(tighten_label, callback_data='ask_adaptive_sl_tighten')])

        timeout_label = "Tempo Máx. Negativo: Desativado" if timeout_min <= 0 else f"Tempo Máx. Negativo: {timeout_min} min"
        kb.append([InlineKeyboardButton(timeout_label, callback_data='ask_adaptive_sl_timeout')])
    kb.append([InlineKeyboardButton("⬅️ Voltar", callback_data='back_to_settings_menu')])
    return InlineKeyboardMarkup(kb)


def risk_menu_keyboard(user) -> InlineKeyboardMarkup:
    entry_pct = f"{float(getattr(user, 'entry_size_percent', 0) or 0):.1f}%"
    leverage  = f"{int(getattr(user, 'max_leverage', 0) or 0)}x"
    min_conf  = f"{float(getattr(user, 'min_confidence', 0) or 0):.1f}%"
    kb = [
        [InlineKeyboardButton(f"📥 Tamanho de Entrada ({entry_pct})", callback_data="set_entry_percent")],
        [InlineKeyboardButton(f"⚙️ Alavancagem Máx. ({leverage})", callback_data="set_max_leverage")],
        [InlineKeyboardButton(f"🎯 Confiança Mín. ({min_conf})", callback_data="set_min_confidence")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="back_to_settings_menu")],
    ]
    return InlineKeyboardMarkup(kb)

def _read_stop_strategy_label(user) -> str:
    raw = (getattr(user, "stop_strategy", None)
           or getattr(user, "stop_strategy_mode", None)
           or getattr(user, "stop_strategy_type", None)
           or "breakeven")
    raw = str(raw).lower()
    return "Breakeven" if raw.startswith("b") else "Trailing"

def stopgain_menu_keyboard(user) -> InlineKeyboardMarkup:
    trigger = f"{float(getattr(user, 'stop_gain_trigger_pct', 0) or 0):.2f}%"
    lock    = f"{float(getattr(user, 'stop_gain_lock_pct', 0) or 0):.2f}%"
    be_trig = f"{float(getattr(user, 'be_trigger_pct', 0) or 0):.2f}%"
    ts_trig = f"{float(getattr(user, 'ts_trigger_pct', 0) or 0):.2f}%"
    strategy_label = _read_stop_strategy_label(user)

    kb = [[InlineKeyboardButton(f"🧭 Estratégia: {strategy_label}", callback_data="set_stop_strategy")]]
    if strategy_label == 'Breakeven':
        kb.append([InlineKeyboardButton(f"🎯 Gatilho BE por PnL ({be_trig})", callback_data="set_be_trigger")])
    else:
        kb.append([InlineKeyboardButton(f"📈 Gatilho TS por PnL ({ts_trig})", callback_data="set_ts_trigger")])
    kb.append([InlineKeyboardButton(f"🚀 Gatilho Stop-Gain ({trigger})", callback_data="set_stop_gain_trigger")])
    kb.append([InlineKeyboardButton(f"🔒 Trava Stop-Gain ({lock})", callback_data="set_stop_gain_lock")])
    kb.append([InlineKeyboardButton("⬅️ Voltar", callback_data="back_to_settings_menu")])
    return InlineKeyboardMarkup(kb)

def circuit_menu_keyboard(user) -> InlineKeyboardMarkup:
    threshold = f"{int(getattr(user, 'circuit_breaker_threshold', 0) or 0)}"
    pause     = f"{int(getattr(user, 'circuit_breaker_pause_minutes', 0) or 0)} min"
    scope = (getattr(user,'circuit_breaker_scope','SIDE') or 'SIDE').upper()
    scope_label = 'Global' if scope == 'GLOBAL' else ('Símbolo' if scope == 'SYMBOL' else 'Direção')
    override_on = bool(getattr(user,'reversal_override_enabled', False))
    override_label = 'On' if override_on else 'Off'
    probe_pct = int(round(float(getattr(user,'probe_size_factor',0.5) or 0.5) * 100))
    kb = [
        [InlineKeyboardButton(f"⚡ Limite do Disjuntor ({threshold})", callback_data="set_circuit_threshold")],
        [InlineKeyboardButton(f"⏸️ Pausa após Disparo ({pause})", callback_data="set_circuit_pause")],
        [InlineKeyboardButton(f"🛰️ Escopo ({scope_label})", callback_data="toggle_circuit_scope")],
        [InlineKeyboardButton(f"🔁 Override Reversão ({override_label})", callback_data="toggle_reversal_override")],
        [InlineKeyboardButton(f"🧪 Probe Size ({probe_pct}%)", callback_data="ask_probe_size")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="back_to_settings_menu")],
    ]
    return InlineKeyboardMarkup(kb)

def bot_config_keyboard(user_settings):
    """Submenu de preferências gerais do bot (sem toggle)."""
    mode = getattr(user_settings, 'approval_mode', 'AUTOMATIC')
    approval_button_text = "Entrada de Sinais: Automático ⚡" if mode == 'AUTOMATIC' else "Entrada de Sinais: Manual 👋"

    profit_target = float(getattr(user_settings, 'daily_profit_target', 0) or 0)
    profit_text = f"Meta de Lucro Diária: ${profit_target:.2f}" if profit_target > 0 else "Meta de Lucro Diária: Desativada"

    loss_limit = float(getattr(user_settings, 'daily_loss_limit', 0) or 0)
    loss_text = f"Limite de Perda Diário: ${loss_limit:.2f}" if loss_limit > 0 else "Limite de Perda Diário: Desativado"

    pend_exp = int(getattr(user_settings, 'pending_expiry_minutes', 0) or 0)
    pend_text = f"⏱️ Expirar Pendentes: {pend_exp} min" if pend_exp > 0 else "⏱️ Expirar Pendentes: Desativado"

    keyboard = [
        [InlineKeyboardButton(approval_button_text, callback_data='toggle_approval_mode')],
        [InlineKeyboardButton(pend_text, callback_data='set_pending_expiry')],
        [InlineKeyboardButton(profit_text, callback_data='set_profit_target')],
        [InlineKeyboardButton(loss_text, callback_data='set_loss_limit')],
        [InlineKeyboardButton("⬅️ Voltar", callback_data='bot_config')]
    ]
    return InlineKeyboardMarkup(keyboard)

def signal_approval_keyboard(signal_for_approval_id: int):
    """
    Retorna o teclado com os botões de Aprovar/Rejeitar para um sinal manual.
    """
    keyboard = [
        [
            InlineKeyboardButton("✅ Aprovar Entrada", callback_data=f'approve_signal_{signal_for_approval_id}'),
            InlineKeyboardButton("❌ Rejeitar", callback_data=f'reject_signal_{signal_for_approval_id}')
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def performance_menu_keyboard():
    """
    Retorna o teclado para o menu de análise de desempenho com filtros de período.
    """
    keyboard = [
        [
            InlineKeyboardButton("Hoje", callback_data='perf_today'),
            InlineKeyboardButton("Ontem", callback_data='perf_yesterday')
        ],
        [
            InlineKeyboardButton("Últimos 7 Dias", callback_data='perf_7_days'),
            InlineKeyboardButton("Últimos 30 Dias", callback_data='perf_30_days')
        ],
        [InlineKeyboardButton("📜 Histórico de Trades", callback_data='list_closed_trades')],
        [InlineKeyboardButton("⬅️ Voltar ao Menu Principal", callback_data='back_to_main_menu')]
    ]
    return InlineKeyboardMarkup(keyboard)

def confirm_manual_close_keyboard(trade_id: int):
    """Retorna o teclado de confirmação para o fechamento manual de um trade."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Sim, fechar", callback_data=f'execute_close_{trade_id}'),
            InlineKeyboardButton("❌ Cancelar", callback_data='user_positions') # Volta para a lista de posições
        ],
    ]
    return InlineKeyboardMarkup(keyboard)

def signal_filters_keyboard(user_settings):
    """
    Retorna o teclado para o menu de configuração dos filtros de análise técnica.
    """
    # Botão para o filtro de Média Móvel (MA)
    ma_status_icon = "✅" if user_settings.is_ma_filter_enabled else "❌"
    ma_text = f"{ma_status_icon} Filtro de Média Móvel"

    # Botão para o filtro de RSI
    rsi_status_icon = "✅" if user_settings.is_rsi_filter_enabled else "❌"
    rsi_text = f"{rsi_status_icon} Filtro de RSI"
    
    keyboard: list[list[InlineKeyboardButton]] = []

    # Toggle MA e parâmetros somente quando habilitado
    keyboard.append([InlineKeyboardButton(ma_text, callback_data='toggle_ma_filter')])
    if user_settings.is_ma_filter_enabled:
        keyboard.append([InlineKeyboardButton(f"Período MA: {user_settings.ma_period}", callback_data='set_ma_period')])

    # Toggle RSI e parâmetros somente quando habilitado
    keyboard.append([InlineKeyboardButton(rsi_text, callback_data='toggle_rsi_filter')])
    if user_settings.is_rsi_filter_enabled:
        keyboard.append([InlineKeyboardButton(f"Sobrecompra: {user_settings.rsi_overbought_threshold}", callback_data='set_rsi_overbought')])
        keyboard.append([InlineKeyboardButton(f"Sobrevenda: {user_settings.rsi_oversold_threshold}", callback_data='set_rsi_oversold')])

    # Timeframe útil para ambos – mantém acessível
    keyboard.append([InlineKeyboardButton(f"Timeframe: {user_settings.ma_timeframe} min", callback_data='ask_ma_timeframe')])
    # Voltar sempre por último
    keyboard.append([InlineKeyboardButton("Voltar para Configurações ⬅️", callback_data='user_settings')])
    return InlineKeyboardMarkup(keyboard)

def ma_timeframe_keyboard(user_settings):
    """
    Retorna o teclado com as opções de timeframe para a Média Móvel.
    """
    # Marca o timeframe atual com um emoji
    timeframes = {'15': '15 min', '60': '1 hora', '240': '4 horas', 'D': 'Diário'}
    keyboard_buttons = []
    
    for tf_value, tf_text in timeframes.items():
        prefix = "✅ " if user_settings.ma_timeframe == tf_value else ""
        keyboard_buttons.append(
            InlineKeyboardButton(f"{prefix}{tf_text}", callback_data=f"set_ma_timeframe_{tf_value}")
        )
    
    # Organiza os botões em duas colunas
    keyboard = [keyboard_buttons[i:i + 2] for i in range(0, len(keyboard_buttons), 2)]
    keyboard.append([InlineKeyboardButton("⬅️ Voltar para Filtros", callback_data='signal_filters_menu')])
    return InlineKeyboardMarkup(keyboard)

def tp_strategy_menu_keyboard(user) -> InlineKeyboardMarkup:
    """Retorna o teclado para o menu de estratégia de Take Profit (sem botão informativo)."""
    kb = [
        [InlineKeyboardButton("📋 Escolher Preset", callback_data="cycle_tp_preset")],
        [InlineKeyboardButton("✏️ Personalizar (lista)", callback_data="ask_tp_distribution")],
        [InlineKeyboardButton("⬅️ Voltar para Configurações", callback_data="back_to_settings_menu")],
    ]
    return InlineKeyboardMarkup(kb)

def tp_presets_keyboard() -> InlineKeyboardMarkup:
    # Mantido para compatibilidade, embora o fluxo atual use 'cycle_tp_preset'.
    kb = [
        [InlineKeyboardButton("Divisão Igual", callback_data="set_tp_preset_EQUAL")],
        [InlineKeyboardButton("Mais cedo (frente)", callback_data="set_tp_preset_FRONT_HEAVY")],
        [InlineKeyboardButton("Mais tarde (traseira)", callback_data="set_tp_preset_BACK_HEAVY")],
        [InlineKeyboardButton("Exponencial cedo", callback_data="set_tp_preset_EXP_FRONT")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="show_tp_strategy")],
    ]
    return InlineKeyboardMarkup(kb)

def onboarding_risk_keyboard():
    """Teclado com as opções de modo inicial do bot."""
    kb = [
        [InlineKeyboardButton("🟢 Conservador", callback_data='onboard_risk_conservative')],
        [InlineKeyboardButton("🟠 Mediano", callback_data='onboard_risk_moderate')],
        [InlineKeyboardButton("🔴 Agressivo", callback_data='onboard_risk_aggressive')],
        [InlineKeyboardButton("✍️ Configuração Manual", callback_data='onboard_risk_manual')],
    ]
    return InlineKeyboardMarkup(kb)

def onboarding_terms_keyboard():
    """Teclado para aceitar o termo de responsabilidade."""
    kb = [
        [InlineKeyboardButton("✅ Li e concordo", callback_data='onboard_accept_terms')],
        [InlineKeyboardButton("❌ Cancelar", callback_data='onboard_decline_terms')],
    ]
    return InlineKeyboardMarkup(kb)

def settings_root_keyboard() -> InlineKeyboardMarkup:
    """Menu raiz de Configurações agrupando seções."""
    kb = [
        [InlineKeyboardButton("⚙️ Configurações de Trade", callback_data='user_settings')],
        [InlineKeyboardButton("🤖 Configuração do Bot", callback_data='bot_config')],
        [InlineKeyboardButton("⚡ Padrões & Assistentes", callback_data='settings_presets')],
        [InlineKeyboardButton("⬅️ Voltar", callback_data='back_to_main_menu')],
    ]
    return InlineKeyboardMarkup(kb)

def bot_settings_keyboard(user=None) -> InlineKeyboardMarkup:
    is_active = bool(getattr(user, 'is_active', False)) if user else False
    toggle_label = "🟢 Bot Ativo (toque para alternar)" if is_active else "🔴 Bot Pausado (toque para ativar)"
    notify_mode = getattr(user, 'msg_cleanup_mode', 'OFF') if user else 'OFF'
    notify_delay = int(getattr(user, 'msg_cleanup_delay_minutes', 30) or 30) if user else 30
    notify_alert_mode = getattr(user, 'alert_cleanup_mode', 'OFF') if user else 'OFF'
    notify_alert_delay = int(getattr(user, 'alert_cleanup_delay_minutes', 30) or 30) if user else 30
    notify_text = 'Desativada' if notify_mode == 'OFF' else ('Após ' + str(notify_delay) + ' min' if notify_mode == 'AFTER' else 'Fim do dia')
    alert_text = 'Desativada' if notify_alert_mode == 'OFF' else ('Após ' + str(notify_alert_delay) + ' min' if notify_alert_mode == 'AFTER' else 'Fim do dia')

    kb = [
        [InlineKeyboardButton(toggle_label, callback_data='toggle_bot_status', )],
        [InlineKeyboardButton("⚙️ Preferências Gerais", callback_data='bot_config_submenu_general')],
        [InlineKeyboardButton(f"🔔 Notificações: {notify_text} / {alert_text}", callback_data='bot_config_notifications')],
        [InlineKeyboardButton("⬅️ Voltar", callback_data='open_settings_root')],
    ]
    return InlineKeyboardMarkup(kb)

def notifications_menu_keyboard(user=None) -> InlineKeyboardMarkup:
    """Menu de Configurações de Notificações dentro do submenu do bot."""
    mode = getattr(user, 'msg_cleanup_mode', 'OFF') if user is not None else 'OFF'
    delay = int(getattr(user, 'msg_cleanup_delay_minutes', 30) or 30) if user is not None else 30

    if mode == 'AFTER':
        mode_text = f"🧹 Fechados: Após {delay} min"
    elif mode == 'EOD':
        mode_text = "🧹 Fechados: Fim do dia"
    else:
        mode_text = "🧹 Fechados: Desativada"

    alert_mode = getattr(user, 'alert_cleanup_mode', 'OFF') if user is not None else 'OFF'
    alert_delay = int(getattr(user, 'alert_cleanup_delay_minutes', 30) or 30) if user is not None else 30
    if alert_mode == 'AFTER':
        alert_text = f"🔔 Alertas: Após {alert_delay} min"
    elif alert_mode == 'EOD':
        alert_text = "🔔 Alertas: Fim do dia"
    else:
        alert_text = "🔔 Alertas: Desativada"

    kb = [
        [InlineKeyboardButton(mode_text, callback_data='toggle_cleanup_mode')],
        [InlineKeyboardButton("⏱️ Minutos (fechados)", callback_data='ask_cleanup_minutes')],
        [InlineKeyboardButton(alert_text, callback_data='toggle_alert_cleanup_mode')],
        [InlineKeyboardButton("⏱️ Minutos (alertas)", callback_data='ask_alert_cleanup_minutes')],
        [InlineKeyboardButton("♻️ Recriar mensagens ativas", callback_data='refresh_active_messages')],
        [InlineKeyboardButton("⬅️ Voltar", callback_data='bot_config')],
    ]
    return InlineKeyboardMarkup(kb)

def info_menu_keyboard() -> InlineKeyboardMarkup:
    """Menu para a seção Informações (status + aprender)."""
    kb = [
        [InlineKeyboardButton("📖 Quero aprender", callback_data='info_learn_start')],
        [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='back_to_main_menu')],
    ]
    return InlineKeyboardMarkup(kb)


def presets_menu_keyboard() -> InlineKeyboardMarkup:
    """Menu para exportar/importar e usar assistentes de configuração."""
    kb = [
        [InlineKeyboardButton("📤 Exportar Configurações", callback_data='settings_presets_export')],
        [InlineKeyboardButton("📥 Importar Configurações", callback_data='settings_presets_import')],
        [InlineKeyboardButton("🎛️ Assistente por Banca", callback_data='settings_presets_bankroll')],
        [InlineKeyboardButton("⬅️ Voltar", callback_data='open_settings_root')],
    ]
    return InlineKeyboardMarkup(kb)
