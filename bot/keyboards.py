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
    
    # Lógica do botão dinâmico para Ligar/Desligar o bot
    if user.is_active:
        toggle_button_text = "Bot: Ativo ✅"
    else:
        toggle_button_text = "Bot: Pausado ⏸️"
    
    keyboard = [
        # NOVO BOTÃO ADICIONADO:
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

def settings_menu_keyboard(user_settings):
    """
    Retorna o teclado do menu de configurações, mostrando os valores atuais.
    """
    entry_percent = user_settings.entry_size_percent
    max_leverage = user_settings.max_leverage
    min_confidence = user_settings.min_confidence

    stop_gain_trigger = user_settings.stop_gain_trigger_pct
    stop_gain_lock = user_settings.stop_gain_lock_pct

    circuit_threshold = user_settings.circuit_breaker_threshold
    circuit_pause = user_settings.circuit_breaker_pause_minutes
    circuit_text = f"Disjuntor: {circuit_threshold} perdas" if circuit_threshold > 0 else "Disjuntor: Desativado"

    # --- INÍCIO DA NOVA LÓGICA ---
    # Define o texto do botão de estratégia de stop dinamicamente
    if user_settings.stop_strategy == 'TRAILING_STOP':
        strategy_text = "Estratégia de Stop: Trailing Stop 📈"
    else:
        strategy_text = "Estratégia de Stop: Break-Even 🛡️"
    # --- FIM DA NOVA LÓGICA ---

    keyboard = [
        [InlineKeyboardButton(f"Tamanho da Entrada: {entry_percent:.2f}%", callback_data='set_entry_percent')],
        [InlineKeyboardButton(f"Alavancagem Máxima: {max_leverage}x", callback_data='set_max_leverage')],
        [InlineKeyboardButton(f"Confiança Mínima (IA): {min_confidence:.2f}%", callback_data='set_min_confidence')],
        [InlineKeyboardButton(strategy_text, callback_data='set_stop_strategy')], # <-- NOVO BOTÃO
        [InlineKeyboardButton(f"Gatilho Stop-Gain: {stop_gain_trigger:.2f}%", callback_data='set_stop_gain_trigger')],
        [InlineKeyboardButton(f"Segurança Stop-Gain: {stop_gain_lock:.2f}%", callback_data='set_stop_gain_lock')],
        [InlineKeyboardButton("✅ Whitelist de Moedas", callback_data='set_coin_whitelist')],
        [InlineKeyboardButton(circuit_text, callback_data='set_circuit_threshold')],
        [InlineKeyboardButton(f"Pausa Disjuntor: {circuit_pause} min", callback_data='set_circuit_pause')],
        [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='back_to_main_menu')]
    ]
    return InlineKeyboardMarkup(keyboard)


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