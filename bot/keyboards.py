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
        keyboard.append([InlineKeyboardButton("📊 Minhas Posições", callback_data='user_positions')])
        keyboard.append([InlineKeyboardButton("⚙️ Configurações de Trade", callback_data='user_settings')])
        # --- NOVO BOTÃO ---
        keyboard.append([InlineKeyboardButton("🤖 Configuração do Bot", callback_data='bot_config')])
        keyboard.append([InlineKeyboardButton("ℹ️ Meu Painel", callback_data='user_dashboard')])
    else:
        keyboard.append([InlineKeyboardButton("⚙️ Configurar API Bybit", callback_data='config_api')])

    return InlineKeyboardMarkup(keyboard)

def dashboard_menu_keyboard():
    """Retorna o teclado para o painel do usuário, com a opção de remover a API."""
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

def settings_menu_keyboard(user_settings):
    """
    Retorna o teclado do menu de configurações, mostrando os valores atuais.
    'user_settings' é o objeto User vindo do banco de dados.
    """
    # Pega os valores do objeto do usuário
    risk_percent = user_settings.risk_per_trade_percent
    max_leverage = user_settings.max_leverage
    min_confidence = user_settings.min_confidence # <-- ESTA LINHA FALTAVA

    keyboard = [
        [InlineKeyboardButton(f"Risco por Trade: {risk_percent:.2f}%", callback_data='set_risk_percent')],
        [InlineKeyboardButton(f"Alavancagem Máxima: {max_leverage}x", callback_data='set_max_leverage')],
        [InlineKeyboardButton(f"Confiança Mínima (IA): {min_confidence:.2f}%", callback_data='set_min_confidence')],
        [InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='back_to_main_menu')]
    ]
    return InlineKeyboardMarkup(keyboard)

def bot_config_keyboard(user_settings):
    """
    Retorna o teclado para o menu de configuração do bot, mostrando o modo de aprovação.
    """
    mode = user_settings.approval_mode
    
    # Define o texto e o emoji com base no modo atual
    if mode == 'AUTOMATIC':
        button_text = "Modo de Aprovação: Automático ⚡"
    else:
        button_text = "Modo de Aprovação: Manual 👋"

    keyboard = [
        # Botão que vai alternar o modo
        [InlineKeyboardButton(button_text, callback_data='toggle_approval_mode')],
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