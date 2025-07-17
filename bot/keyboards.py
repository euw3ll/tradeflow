from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.crud import get_user_by_id

def main_menu_keyboard(telegram_id: int):
    """
    Retorna o teclado do menu principal de forma inteligente,
    verificando o status do usuário diretamente no banco de dados.
    """
    # Busca o usuário no DB para verificar se ele tem chaves
    user = get_user_by_id(telegram_id)
    has_api_keys = user and user.api_key_encrypted is not None

    keyboard = []
    if has_api_keys:
        keyboard.append([InlineKeyboardButton("📊 Minhas Posições", callback_data='user_positions')])
        keyboard.append([InlineKeyboardButton("⚙️ Configurações de Trade", callback_data='user_settings')])
        keyboard.append([InlineKeyboardButton("ℹ️ Meu Painel", callback_data='user_dashboard')])
    else:
        # Se não tem chaves, mostra APENAS a opção de configurar
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
    keyboard = [[InlineKeyboardButton("📡 Listar Grupos/Canais", callback_data='admin_list_channels')]]
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