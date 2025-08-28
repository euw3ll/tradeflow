from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.crud import get_user_by_id

def main_menu_keyboard(telegram_id: int):
    """
    Retorna o teclado do menu principal de forma inteligente,
    verificando o status do usuário diretamente no banco de dados.
    """
    user = get_user_by_id(telegram_id)
    has_api_keys = user and user.api_key_encrypted is not None

    keyboard = []
    if has_api_keys:
        keyboard.append([InlineKeyboardButton("ℹ️ Meu Painel", callback_data='user_dashboard')])
        keyboard.append([InlineKeyboardButton("📊 Minhas Posições", callback_data='user_positions')])
        
        # --- BOTÃO ADICIONADO AQUI ---
        keyboard.append([InlineKeyboardButton("📈 Desempenho", callback_data='perf_today')])
        
        keyboard.append([InlineKeyboardButton("⚙️ Configurações de Trade", callback_data='user_settings')])
        keyboard.append([InlineKeyboardButton("🤖 Configuração do Bot", callback_data='bot_config')])
    else:
        keyboard.append([InlineKeyboardButton("⚙️ Configurar API Bybit", callback_data='config_api')])

    return InlineKeyboardMarkup(keyboard)

def dashboard_menu_keyboard(user):
    """Retorna o teclado para o painel do usuário, com a opção de remover a API e ligar/desligar o bot."""
    
    # Lógica do botão único de 3 estados
    if not user.is_active:
        # Estado 1: Pausado
        toggle_button_text = "Bot: Pausado ⏸️"
    elif user.is_active and not user.is_sleep_mode_enabled:
        # Estado 2: Ativo 24h
        toggle_button_text = "Bot: Ativo ☀️"
    else: # user.is_active and user.is_sleep_mode_enabled
        # Estado 3: Ativo com Modo Dormir
        toggle_button_text = "Bot: Ativo com Modo Dormir 😴"
    
    keyboard = [
        [InlineKeyboardButton(toggle_button_text, callback_data='toggle_bot_status')],
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
        [
            InlineKeyboardButton("🚫 Disjuntor", callback_data="settings_circuit"),
            InlineKeyboardButton("✅ Whitelist", callback_data="set_coin_whitelist"),
        ],
        [InlineKeyboardButton("🔬 Filtros de Sinais", callback_data="signal_filters_menu")],
        [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data="back_to_main_menu")],
    ]
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
    strategy_label = _read_stop_strategy_label(user)

    kb = [
        [InlineKeyboardButton(f"🧭 Estratégia: {strategy_label}", callback_data="set_stop_strategy")],
        [InlineKeyboardButton(f"🚀 Gatilho Stop-Gain ({trigger})", callback_data="set_stop_gain_trigger")],
        [InlineKeyboardButton(f"🔒 Trava Stop-Gain ({lock})", callback_data="set_stop_gain_lock")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="back_to_settings_menu")],
    ]
    return InlineKeyboardMarkup(kb)

def circuit_menu_keyboard(user) -> InlineKeyboardMarkup:
    threshold = f"{int(getattr(user, 'circuit_breaker_threshold', 0) or 0)}"
    pause     = f"{int(getattr(user, 'circuit_breaker_pause_minutes', 0) or 0)} min"
    kb = [
        [InlineKeyboardButton(f"⚡ Limite do Disjuntor ({threshold})", callback_data="set_circuit_threshold")],
        [InlineKeyboardButton(f"⏸️ Pausa após Disparo ({pause})", callback_data="set_circuit_pause")],
        [InlineKeyboardButton("⬅️ Voltar", callback_data="back_to_settings_menu")],
    ]
    return InlineKeyboardMarkup(kb)

def bot_config_keyboard(user_settings):
    """
    Retorna o teclado para o menu de configuração do bot, mostrando o modo de aprovação e as metas.
    """
    # Botão de Modo de Aprovação (lógica existente)
    mode = user_settings.approval_mode
    if mode == 'AUTOMATIC':
        approval_button_text = "Entrada de Sinais: Automático ⚡"
    else:
        approval_button_text = "Entrada de Sinais: Manual 👋"

    # --- NOVOS BOTÕES DE METAS ---
    # Formata a meta de lucro para exibição
    profit_target = user_settings.daily_profit_target
    profit_text = f"Meta de Lucro Diária: ${profit_target:.2f}" if profit_target > 0 else "Meta de Lucro Diária: Desativada"

    # Formata o limite de perda para exibição
    loss_limit = user_settings.daily_loss_limit
    loss_text = f"Limite de Perda Diário: ${loss_limit:.2f}" if loss_limit > 0 else "Limite de Perda Diário: Desativado"

    keyboard = [
        [InlineKeyboardButton(approval_button_text, callback_data='toggle_approval_mode')],
        # --- NOVAS LINHAS ADICIONADAS AO TECLADO ---
        [InlineKeyboardButton(profit_text, callback_data='set_profit_target')],
        [InlineKeyboardButton(loss_text, callback_data='set_loss_limit')],
        [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='back_to_main_menu')]
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
    
    keyboard = [
        [InlineKeyboardButton("Voltar para Configurações ⬅️", callback_data='user_settings')],
        [
            InlineKeyboardButton(ma_text, callback_data='toggle_ma_filter'),
            InlineKeyboardButton(f"Período MA: {user_settings.ma_period}", callback_data='set_ma_period')
        ],
        [
            InlineKeyboardButton(rsi_text, callback_data='toggle_rsi_filter'),
            InlineKeyboardButton(f"Sobrecompra: {user_settings.rsi_overbought_threshold}", callback_data='set_rsi_overbought')
        ],
        [
            InlineKeyboardButton(f"Timeframe: {user_settings.ma_timeframe} min", callback_data='ask_ma_timeframe'),
            InlineKeyboardButton(f"Sobrevenda: {user_settings.rsi_oversold_threshold}", callback_data='set_rsi_oversold')
        ],
    ]
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