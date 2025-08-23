import logging
import asyncio
from database.models import PendingSignal
from services.bybit_service import place_limit_order, get_account_info
from datetime import datetime, time, timedelta 
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest
from database.session import SessionLocal
from database.models import User, InviteCode, MonitoredTarget, Trade, SignalForApproval
from .keyboards import (
    main_menu_keyboard, confirm_remove_keyboard, admin_menu_keyboard, 
    dashboard_menu_keyboard, settings_menu_keyboard, view_targets_keyboard, 
    bot_config_keyboard, performance_menu_keyboard, confirm_manual_close_keyboard)
from utils.security import encrypt_data, decrypt_data
from services.bybit_service import (
    get_open_positions, 
    get_account_info, 
    close_partial_position, 
    get_open_positions_with_pnl,
    get_market_price
)
from utils.config import ADMIN_ID
from database.crud import get_user_by_id
from core.trade_manager import _execute_trade
from core.performance_service import generate_performance_report
from core.trade_manager import execute_signal_for_all_users
from sqlalchemy.sql import func


# Estados para as conversas
(WAITING_CODE, WAITING_API_KEY, WAITING_API_SECRET, CONFIRM_REMOVE_API) = range(4)
(ASKING_ENTRY_PERCENT, ASKING_MAX_LEVERAGE, ASKING_MIN_CONFIDENCE) = range(10, 13)
(ASKING_PROFIT_TARGET, ASKING_LOSS_LIMIT) = range(13, 15)
ASKING_COIN_WHITELIST = 15

logger = logging.getLogger(__name__)

# --- FLUXO DE USUÁRIO (START, CADASTRO, MENUS) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    telegram_user = update.effective_user
    user_in_db = get_user_by_id(telegram_user.id)
    if user_in_db:
        await update.message.reply_text(
            "Menu Principal:",
            reply_markup=main_menu_keyboard(telegram_id=telegram_user.id)
        )
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            f"Olá, {telegram_user.first_name}! Para usar o TradeFlow, insira seu código de convite."
        )
        return WAITING_CODE

async def receive_invite_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code_text = update.message.text
    telegram_user = update.effective_user
    db = SessionLocal()
    try:
        invite_code = db.query(InviteCode).filter(InviteCode.code == code_text, InviteCode.is_used == False).first()
        if invite_code:
            new_user = User(telegram_id=telegram_user.id, first_name=telegram_user.first_name)
            db.add(new_user)
            invite_code.is_used = True
            db.commit()
            await update.message.reply_text(
                "✅ Cadastro realizado com sucesso! O próximo passo é configurar sua API.",
                reply_markup=main_menu_keyboard(telegram_id=telegram_user.id)
            )
            return ConversationHandler.END
        else:
            await update.message.reply_text("❌ Código de convite inválido ou já utilizado. Tente novamente.")
            return WAITING_CODE
    finally:
        db.close()

async def back_to_main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "Menu Principal:",
        reply_markup=main_menu_keyboard(telegram_id=update.effective_user.id)
    )

# --- FLUXO DE CONFIGURAÇÃO DE API ---
async def config_api(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inicia o fluxo de configuração de API com um tutorial melhorado."""
    query = update.callback_query
    await query.answer()
    
    context.user_data['entry_message_id'] = query.message.message_id
    
    tutorial_text = (
        "🔑 <b>Como Criar suas Chaves de API na Bybit</b> 🔑\n\n"
        "Siga estes passos com atenção para conectar sua conta:\n\n"
        "1️⃣  Faça login em <b>Bybit.com</b> e vá para <i>Perfil > API</i>.\n\n"
        "2️⃣  Clique em <b>'Criar Nova Chave'</b> e selecione <i>'Chaves Geradas pelo Sistema'</i>.\n\n"
        "3️⃣  Dê um nome para sua chave (ex: `TradeFlowBot`) e selecione as permissões de <b>'Leitura e Escrita'</b>.\n\n"
        "4️⃣  Nas permissões, marque <b>APENAS</b> as seguintes caixas:\n"
        "   - <b>Contrato</b> (`Contract`): ✅ `Ordens` e ✅ `Posições`\n"
        "   - <b>Trading Unificado</b> (`UTA`): ✅ `Trade`\n\n"
        "5️⃣  🛡️ <b>MUITO IMPORTANTE:</b> Por segurança, <b>NÃO</b> marque a permissão de <i>'Saque' (Withdraw)</i>.\n\n"
        "⚠️ <b>Atenção:</b> Este bot opera exclusivamente com pares de trade terminados em **USDT**.\n\n"
        "6️⃣  Conclua a verificação de segurança e copie sua <b>API Key</b> e <b>API Secret</b>.\n\n"
        "-------------------------------------\n"
        "Pronto! Agora, por favor, envie sua <b>API Key</b>."
    )
    
    await query.edit_message_text(
        text=tutorial_text,
        parse_mode='HTML'
    )
    return WAITING_API_KEY

async def receive_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a API Key, apaga a mensagem do usuário e pede a API Secret."""
    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id
    )

    api_key = update.message.text
    context.user_data['api_key'] = api_key
    
    prompt_message = await update.message.reply_text(
        "Chave API recebida com segurança. Agora, por favor, envie sua *API Secret*.",
        parse_mode='Markdown'
    )
    context.user_data['prompt_message_id'] = prompt_message.message_id
    
    return WAITING_API_SECRET

async def receive_api_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a API Secret, apaga as mensagens, criptografa e salva no banco."""
    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id
    )

    prompt_message_id = context.user_data.get('prompt_message_id')
    if prompt_message_id:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=prompt_message_id
        )

    api_secret = update.message.text
    api_key = context.user_data.get('api_key')
    telegram_id = update.effective_user.id

    encrypted_key = encrypt_data(api_key)
    encrypted_secret = encrypt_data(api_secret)

    db = SessionLocal()
    try:
        user_to_update = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user_to_update:
            user_to_update.api_key_encrypted = encrypted_key
            user_to_update.api_secret_encrypted = encrypted_secret
            db.commit()
            
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['entry_message_id'],
                text="✅ Suas chaves de API foram salvas com sucesso!",
            )
            await context.bot.send_message(
                chat_id=telegram_id,
                text="Menu Principal:",
                reply_markup=main_menu_keyboard(telegram_id=telegram_id)
            )
        else:
            await update.message.reply_text("Ocorreu um erro. Usuário não encontrado.")
    finally:
        db.close()
        context.user_data.clear()

    return ConversationHandler.END

# --- FLUXO DE REMOÇÃO DE API ---
async def remove_api_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text="⚠️ Você tem certeza que deseja remover suas chaves de API?",
        reply_markup=confirm_remove_keyboard()
    )
    return CONFIRM_REMOVE_API

async def remove_api_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id

    if query.data == 'remove_api_confirm':
        db = SessionLocal()
        try:
            user_to_update = db.query(User).filter(User.telegram_id == telegram_id).first()
            if user_to_update:
                user_to_update.api_key_encrypted = None
                user_to_update.api_secret_encrypted = None
                db.commit()
            await query.edit_message_text("✅ Suas chaves de API foram removidas.")
        finally:
            db.close()
    else: # Cancelou
        await query.edit_message_text("Operação cancelada.")

    await context.bot.send_message(
        chat_id=telegram_id,
        text="Menu Principal:",
        reply_markup=main_menu_keyboard(telegram_id=telegram_id)
    )
    return ConversationHandler.END

# --- PAINÉIS DO USUÁRIO ---
async def my_positions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Buscando suas posições gerenciadas...")

    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if not user or not user.api_key_encrypted:
            await query.edit_message_text("Você ainda não configurou suas chaves de API.")
            return

        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)

        # Busca as posições ativas que o bot está gerenciando no nosso DB
        active_trades = db.query(Trade).filter(
            Trade.user_telegram_id == user_id,
            ~Trade.status.like('%CLOSED%')
        ).all()

        if not active_trades:
            await query.edit_message_text(
                "<b>📊 Suas Posições Ativas</b>\n\nNenhuma posição sendo gerenciada no momento.",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='back_to_main_menu')]])
            )
            return

        # Busca os dados ao vivo (P/L) da Bybit para essas posições
        live_pnl_data = {}
        live_positions_result = await get_open_positions_with_pnl(api_key, api_secret)
        if live_positions_result.get("success"):
            for pos in live_positions_result.get("data", []):
                live_pnl_data[pos["symbol"]] = pos

        lines = ["<b>📊 Suas Posições Ativas (Gerenciadas pelo Bot)</b>", ""]
        keyboard = []

        for trade in active_trades:
            arrow = "⬆️" if trade.side == "LONG" else "⬇️"
            pnl_info = ""
            
            # Adiciona os dados de P/L ao vivo, se disponíveis
            if trade.symbol in live_pnl_data:
                live_data = live_pnl_data[trade.symbol]
                pnl = live_data["unrealized_pnl"]
                pnl_pct = live_data["unrealized_pnl_pct"]
                mark_price = live_data["mark"]
                pnl_info = (
                    f"  Preço Atual: ${mark_price:,.4f}\n"
                    f"  P/L: <b>{pnl:+.2f} USDT ({pnl_pct:+.2f}%)</b>\n"
                )
            else:
                pnl_info = f"  Status: {trade.status}\n"

            # --- NOVA LÓGICA PARA EXIBIR ALVOS ---
            targets_info = ""
            if trade.initial_targets:
                next_target = trade.initial_targets[0]
                remaining_count = len(trade.initial_targets)
                targets_info = f"  🎯 Próximo Alvo: ${next_target:,.4f} ({remaining_count} restantes)\n"

            lines.append(
                f"- {arrow} <b>{trade.symbol}</b> ({trade.qty:g} unid.)\n"
                f"  Entrada: ${trade.entry_price:.4f}\n"
                f"{pnl_info}"
                f"{targets_info}"
            )

            keyboard.append([
                InlineKeyboardButton(f"Fechar {trade.symbol} ❌", callback_data=f"confirm_close_{trade.id}")
            ])

        keyboard.append([InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='back_to_main_menu')])
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    finally:
        db.close()

async def user_dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o painel com um resumo visual dos saldos da carteira."""
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest as e:
        logger.warning(f"Não foi possível responder ao callback_query (pode ser antigo): {e}")
        return

    await query.edit_message_text("Buscando informações do painel...")
    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        user = get_user_by_id(user_id)
        if not user or not user.api_key_encrypted:
            await query.edit_message_text("Você precisa configurar sua API primeiro.", reply_markup=main_menu_keyboard(telegram_id=user_id))
            return

        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)

        account_info = await get_account_info(api_key, api_secret)

        message = "<b>Dashboard Financeiro</b> 📊\n\n"
        
        if account_info.get("success"):
            balance_data = account_info.get("data", {})
            total_equity = balance_data.get("total_equity", 0.0)
            available_balance = balance_data.get("available_balance_usdt", 0.0)
            
            # Formatação visual dos saldos
            message += (
                f"💰 <b>Patrimônio Total:</b> ${total_equity:,.2f} USDT\n"
                f"💸 <b>Saldo Disponível para Trade:</b> ${available_balance:,.2f} USDT\n\n"
            )
            
            # Lista de outras moedas relevantes
            coin_list = balance_data.get("coin_list", [])
            other_coins_lines = []
            for c in coin_list:
                coin = (c.get("coin") or "").upper()
                wallet_balance_str = c.get("walletBalance")
                wallet_balance = float(wallet_balance_str) if wallet_balance_str else 0.0
                usd_value_str = c.get("usdValue")
                usd_value = float(usd_value_str) if usd_value_str else 0.0

                # Exibe outras moedas se o valor em USD for maior que $1.00
                if coin != "USDT" and usd_value > 1.0:
                    other_coins_lines.append(f"  - {coin}: {wallet_balance:g} (~${usd_value:,.2f})")
            
            if other_coins_lines:
                message += "<b>Outros Ativos em Carteira:</b>\n"
                message += "\n".join(other_coins_lines)
            
        else:
            message += f"❌ Erro ao buscar saldo: {account_info.get('error')}\n"

        message += "\n\n<i>Use o menu 'Minhas Posições' para ver os detalhes dos seus trades. Este bot opera exclusivamente com pares USDT.</i>"

        await query.edit_message_text(message, parse_mode="HTML", reply_markup=dashboard_menu_keyboard())

    except Exception as e:
        logger.error(f"Erro ao montar o painel do usuário: {e}", exc_info=True)
        await query.edit_message_text("Ocorreu um erro ao buscar os dados do seu painel.")
    finally:
        db.close()

# --- CANCELAMENTO ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancela a operação atual."""
    await update.message.reply_text("Operação cancelada.")
    return ConversationHandler.END

# --- FLUXO DE ADMINISTRAÇÃO ---
async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra o menu de administrador, se o usuário for o admin."""
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("Você não tem permissão para usar este comando.")
        return

    await update.message.reply_text(
        "Bem-vindo ao painel de administração.",
        reply_markup=admin_menu_keyboard()
    )


async def admin_view_targets_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Busca e exibe a lista de todos os canais e tópicos sendo monitorados."""
    query = update.callback_query
    await query.answer()
    
    db = SessionLocal()
    try:
        targets = db.query(MonitoredTarget).all()
        
        message = "<b>👁️ Alvos Atualmente Monitorados</b>\n\n"
        
        if targets:
            for target in targets:
                if target.topic_name:
                    message += f"- <b>Grupo:</b> {target.channel_name}\n  - <b>Tópico:</b> {target.topic_name}\n"
                else:
                    message += f"- <b>Canal:</b> {target.channel_name}\n"
        else:
            message += "Nenhum alvo sendo monitorado no momento."
            
        await query.edit_message_text(
            text=message,
            parse_mode='HTML',
            reply_markup=view_targets_keyboard()
        )
    finally:
        db.close()

async def back_to_admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retorna o usuário para o menu de administração principal."""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "Bem-vindo ao painel de administração.",
        reply_markup=admin_menu_keyboard()
    )

async def list_channels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Coloca um pedido na fila para listar os grupos e canais do usuário."""
    query = update.callback_query
    await query.answer()
    
    comm_queue = context.application.bot_data.get('comm_queue')
    if not comm_queue:
        await query.edit_message_text("Erro: Fila de comunicação não encontrada.")
        return
    
    request_data = {
        "action": "list_channels",
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
    }
    
    await comm_queue.put(request_data)
    
    await query.edit_message_text("Buscando sua lista de canais... Se você tiver muitos grupos, isso pode levar até um minuto. Por favor, aguarde.")
    
async def select_channel_to_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Coloca um pedido na fila para listar tópicos (ou gerenciar um canal plano)."""
    query = update.callback_query
    await query.answer()
    comm_queue = context.application.bot_data.get('comm_queue')
    if not comm_queue: return

    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return

    channel_id = int(query.data.split('_')[-1])
    
    channel_name = ""
    for row in query.message.reply_markup.inline_keyboard:
        for button in row:
            if button.callback_data == query.data:
                channel_name = button.text.replace(" ✅", "")
                break

    request_data = {
        "action": "list_topics",
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
        "channel_id": channel_id,
        "channel_name": channel_name
    }
    
    await comm_queue.put(request_data)
    await query.edit_message_text("Processando...")

async def select_topic_to_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Salva/remove o tópico e pede para a fila recarregar o menu de tópicos."""
    query = update.callback_query
    await query.answer() 

    comm_queue = context.application.bot_data.get('comm_queue')
    if not comm_queue:
        logger.error("Fila de comunicação não encontrada no contexto do bot.")
        return

    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return

    _, _, channel_id_str, topic_id_str = query.data.split('_')
    channel_id = int(channel_id_str)
    topic_id = int(topic_id_str)
    
    db = SessionLocal()
    try:
        existing_target = db.query(MonitoredTarget).filter_by(channel_id=channel_id, topic_id=topic_id).first()
        
        if existing_target:
            db.delete(existing_target)
        else:
            topic_name = ""
            for row in query.message.reply_markup.inline_keyboard:
                for button in row:
                    if button.callback_data == query.data:
                        topic_name = button.text.replace(" ✅", "")
                        break
            new_target = MonitoredTarget(channel_id=channel_id, topic_id=topic_id, topic_name=topic_name)
            db.add(new_target)
        
        db.commit()
    finally:
        db.close()

    request_data = {
        "action": "list_topics",
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
        "channel_id": channel_id,
        "channel_name": ""
    }
    await comm_queue.put(request_data)

async def back_to_channels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retorna o usuário para a lista de canais/grupos."""
    await list_channels_handler(update, context)

# --- FUNÇÕES DUPLICADAS REMOVIDAS PARA LIMPEZA ---
# my_dashboard_handler, my_positions_handler, back_to_main_menu_handler
# já estavam definidas acima.

async def user_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o menu de configurações de trade com os valores atuais do usuário."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if user:
            await query.edit_message_text(
                "<b>⚙️ Configurações de Trade</b>\n\n"
                "Aqui você pode definir seus parâmetros de risco e automação.",
                parse_mode='HTML',
                reply_markup=settings_menu_keyboard(user)
            )
    finally:
        db.close()

async def ask_entry_percent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pergunta ao usuário qual a nova porcentagem da banca por entrada."""
    query = update.callback_query
    await query.answer()
    
    context.user_data['settings_message_id'] = query.message.message_id
    
    await query.edit_message_text(
        "Envie a porcentagem da sua banca em USDT que você deseja usar para cada entrada.\n\n"
        "Exemplo: se você tem $100 e define `10`, cada entrada terá o valor de $10.\n"
        "Envie apenas o número (ex: `10` para 10%)."
    )
    return ASKING_ENTRY_PERCENT

async def receive_entry_percent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe, valida e salva a nova porcentagem de entrada."""
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)

    try:
        percent_value = float(update.message.text.replace(',', '.'))
        if not (0.1 <= percent_value <= 100):
            raise ValueError("Valor fora do range permitido (0.1 a 100)")

        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            user.entry_size_percent = percent_value
            db.commit()
            
            api_key = decrypt_data(user.api_key_encrypted)
            api_secret = decrypt_data(user.api_secret_encrypted)
            account_info = await get_account_info(api_key, api_secret)
            
            usdt_balance = 0.0
            if account_info.get("success"):
                balances = account_info.get("data", [])
                if balances:
                    coin_list = balances[0].get('coin', [])
                    for coin in coin_list:
                        if coin.get('coin') == 'USDT':
                            usdt_balance = float(coin.get('walletBalance', 0))
                            break
            
            entry_value = usdt_balance * (percent_value / 100)
            
            feedback_text = (
                f"✅ Tamanho da entrada atualizado para <b>{percent_value:.2f}%</b>.\n\n"
                f"Com seu saldo atual, cada entrada será de aprox. <b>${entry_value:,.2f} USDT</b>."
            )

            if percent_value > 25:
                feedback_text += "\n\n⚠️ <b>Atenção:</b> Uma porcentagem acima de 25% é considerada de altíssimo risco!"

            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id_to_edit,
                text=feedback_text,
                parse_mode='HTML',
                reply_markup=settings_menu_keyboard(user)
            )
        finally:
            db.close()

    except (ValueError, TypeError):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id_to_edit,
            text="❌ Valor inválido. Por favor, tente novamente com um número entre 0.1 e 100 (ex: 10)."
        )
        return ASKING_ENTRY_PERCENT

    return ConversationHandler.END        

async def ask_max_leverage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pergunta ao usuário qual a nova alavancagem máxima."""
    query = update.callback_query
    await query.answer()
    
    context.user_data['settings_message_id'] = query.message.message_id
    
    await query.edit_message_text(
        "Qual a alavancagem máxima que o bot deve usar?\n"
        "Envie apenas o número (ex: `10` para 10x)."
    )
    return ASKING_MAX_LEVERAGE

async def receive_max_leverage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe, valida e salva a nova alavancagem máxima."""
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')

    try:
        leverage_value = int(update.message.text)
        if not (1 <= leverage_value <= 125):
            raise ValueError("Alavancagem fora do limite (1-125)")

        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            user.max_leverage = leverage_value
            db.commit()
            
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id_to_edit,
                text=f"✅ Alavancagem máxima atualizada para {leverage_value}x.\n\n"
                     "Selecione outra opção para editar ou volte.",
                reply_markup=settings_menu_keyboard(user)
            )
        finally:
            db.close()

    except (ValueError, TypeError):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id_to_edit,
            text="❌ Valor inválido. Por favor, tente novamente com um número inteiro (ex: 10)."
        )
        return ASKING_MAX_LEVERAGE
    finally:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)

    return ConversationHandler.END

async def ask_min_confidence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pergunta ao usuário qual o novo valor de confiança mínima."""
    query = update.callback_query
    await query.answer()
    context.user_data['settings_message_id'] = query.message.message_id
    await query.edit_message_text("Envie o valor da confiança mínima da IA (ex: 75 para 75%).\nSinais com confiança abaixo disso serão ignorados.")
    return ASKING_MIN_CONFIDENCE

async def receive_min_confidence(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe, valida e salva o novo valor de confiança."""
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')
    
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)

    try:
        confidence_value = float(update.message.text.replace(',', '.'))
        if not (0 <= confidence_value <= 100):
            raise ValueError("Valor fora do range permitido (0-100)")

        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            user.min_confidence = confidence_value
            db.commit()
            
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id_to_edit,
                text=f"✅ Confiança mínima atualizada para {confidence_value:.2f}%.\n\n"
                     "Selecione outra opção para editar ou volte.",
                reply_markup=settings_menu_keyboard(user)
            )
        finally:
            db.close()

        return ConversationHandler.END

    except (ValueError, TypeError):
        logger.warning(f"Usuário {user_id} enviou um valor inválido para confiança: {update.message.text}")
        
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id_to_edit,
            text="❌ <b>Valor inválido.</b>\nPor favor, envie apenas um número entre 0 e 100 (ex: 75).",
            parse_mode='HTML'
        )
        
        return ASKING_MIN_CONFIDENCE
    
async def execute_manual_close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lida com a EXECUÇÃO do fechamento manual após a confirmação."""
    query = update.callback_query
    await query.answer("Processando fechamento...")

    # O callback_data agora será 'execute_close_123'
    trade_id = int(query.data.split('_')[-1])
    user_id = update.effective_user.id

    db = SessionLocal()
    try:
        trade_to_close = db.query(Trade).filter_by(id=trade_id, user_telegram_id=user_id).first()

        if not trade_to_close:
            await query.edit_message_text("Erro: Trade não encontrado ou já fechado.")
            return

        user = db.query(User).filter_by(telegram_id=user_id).first()
        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)

        price_result = await get_market_price(trade_to_close.symbol)
        current_price = price_result["price"] if price_result.get("success") else trade_to_close.entry_price

        close_result = await close_partial_position(
            api_key, api_secret, 
            trade_to_close.symbol, 
            trade_to_close.remaining_qty, 
            trade_to_close.side
        )

        if close_result.get("success"):
            pnl = (current_price - trade_to_close.entry_price) * trade_to_close.remaining_qty if trade_to_close.side == 'LONG' else (trade_to_close.entry_price - current_price) * trade_to_close.remaining_qty
            
            trade_to_close.status = 'CLOSED_MANUAL'
            trade_to_close.closed_at = func.now()
            trade_to_close.closed_pnl = pnl
            db.commit()

            resultado_str = "LUCRO" if pnl >= 0 else "PREJUÍZO"
            emoji = "✅" if pnl >= 0 else "🔻"
            message_text = (
                f"{emoji} <b>Posição Fechada Manualmente ({resultado_str})</b>\n"
                f"<b>Moeda:</b> {trade_to_close.symbol}\n"
                f"<b>Resultado:</b> ${pnl:,.2f}"
            )
            
            await query.edit_message_text(message_text, parse_mode='HTML')
            await asyncio.sleep(2)
            await my_positions_handler(update, context)
        else:
            error_msg = close_result.get('error')
            await context.bot.send_message(
                chat_id=user_id,
                text=f"❌ Erro ao fechar a posição para {trade_to_close.symbol}: {error_msg}"
            )
    finally:
        db.close()


async def bot_config_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o menu de configuração do bot com o modo de aprovação atual."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    
    db = SessionLocal()
    try:
        user = get_user_by_id(user_id)
        if user:
            await query.edit_message_text(
                "<b>🤖 Configuração do Bot</b>\n\n"
                "Ajuste o comportamento geral do bot.",
                parse_mode='HTML',
                reply_markup=bot_config_keyboard(user)
            )
    finally:
        db.close()

async def toggle_approval_mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alterna o modo de aprovação de ordens entre Manual e Automático."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == user_id).first()
        
        if user:
            if user.approval_mode == 'AUTOMATIC':
                user.approval_mode = 'MANUAL'
            else:
                user.approval_mode = 'AUTOMATIC'
            
            db.commit() 
            
            try:
                await query.edit_message_text(
                    "<b>🤖 Configuração do Bot</b>\n\n"
                    "Ajuste o comportamento geral do bot.",
                    parse_mode='HTML',
                    reply_markup=bot_config_keyboard(user)
                )
            except BadRequest as e:
                if "Message is not modified" in str(e):
                    pass
                else:
                    logger.error(f"Erro ao editar mensagem em toggle_approval_mode: {e}")
    finally:
        db.close()

async def handle_signal_approval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    ### ALTERAÇÃO INICIADA ###
    # A linha abaixo estava incorreta e não extraía a ação ('approve') corretamente.
    # A nova linha usa `partition` para dividir a string de forma mais confiável.
    # Ex: 'approve_signal_123' se torna ('approve', '_signal_', '123')
    action, _, signal_id_str = query.data.partition('_signal_')
    ### ALTERAÇÃO FINALIZADA ###

    signal_id = int(signal_id_str)
    
    db = SessionLocal()
    try:
        signal_to_process = db.query(SignalForApproval).filter_by(id=signal_id).first()
        if not signal_to_process:
            await query.edit_message_text("Este sinal já foi processado ou expirou.")
            return

        if action == 'approve':
            await query.edit_message_text("✅ **Entrada Aprovada!** Replicando a ordem para todos os usuários...")
            
            await execute_signal_for_all_users(
                signal_data=signal_to_process.signal_data,
                application=context.application,
                db=db,
                source_name=signal_to_process.source_name
            )
            
        elif action == 'reject':
            await query.edit_message_text("❌ **Entrada Rejeitada.** O sinal foi descartado.")
        
        db.delete(signal_to_process)
        db.commit()
    finally:
        db.close()

# --- FLUXO DE CONFIGURAÇÃO DE METAS DIÁRIAS ---

async def ask_profit_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pergunta ao usuário a nova meta de lucro diário."""
    query = update.callback_query
    await query.answer()
    
    context.user_data['settings_message_id'] = query.message.message_id
    
    await query.edit_message_text(
        "Envie a sua meta de **lucro diário** em USDT.\n"
        "O bot irá parar de abrir novas ordens quando o lucro do dia atingir este valor.\n\n"
        "Envie apenas o número (ex: `100` para $100) ou `0` para desativar.",
        parse_mode='Markdown'
    )
    return ASKING_PROFIT_TARGET

async def receive_profit_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe, valida e salva a nova meta de lucro."""
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')

    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)

    try:
        target_value = float(update.message.text.replace(',', '.'))
        if target_value < 0:
            raise ValueError("Valor não pode ser negativo")

        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            user.daily_profit_target = target_value
            db.commit()
            
            feedback_text = f"✅ Meta de lucro diário atualizada para ${target_value:.2f}."
            if target_value == 0:
                feedback_text = "✅ Meta de lucro diário foi desativada."

            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id_to_edit,
                text=f"{feedback_text}\n\nAjuste outra configuração ou volte.",
                reply_markup=bot_config_keyboard(user)
            )
        finally:
            db.close()

    except (ValueError, TypeError):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id_to_edit,
            text="❌ Valor inválido. Por favor, tente novamente com um número (ex: 100)."
        )
        return ASKING_PROFIT_TARGET

    return ConversationHandler.END

async def ask_loss_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pergunta ao usuário o novo limite de perda diário."""
    query = update.callback_query
    await query.answer()
    
    context.user_data['settings_message_id'] = query.message.message_id
    
    await query.edit_message_text(
        "Envie o seu limite de **perda diária** em USDT.\n"
        "O bot irá parar de abrir novas ordens se a perda do dia atingir este valor.\n\n"
        "Envie um número positivo (ex: `50` para um limite de $50) ou `0` para desativar.",
        parse_mode='Markdown'
    )
    return ASKING_LOSS_LIMIT

async def receive_loss_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe, valida e salva o novo limite de perda."""
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')

    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)

    try:
        limit_value = float(update.message.text.replace(',', '.'))
        if limit_value < 0:
            raise ValueError("Valor não pode ser negativo")

        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            user.daily_loss_limit = limit_value
            db.commit()

            feedback_text = f"✅ Limite de perda diário atualizado para ${limit_value:.2f}."
            if limit_value == 0:
                feedback_text = "✅ Limite de perda diário foi desativado."

            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id_to_edit,
                text=f"{feedback_text}\n\nAjuste outra configuração ou volte.",
                reply_markup=bot_config_keyboard(user)
            )
        finally:
            db.close()

    except (ValueError, TypeError):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id_to_edit,
            text="❌ Valor inválido. Por favor, tente novamente com um número positivo (ex: 50)."
        )
        return ASKING_LOSS_LIMIT

    return ConversationHandler.END

# --- MENU DE DESEMPENHO ---

async def performance_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o painel de desempenho e lida com a seleção de período."""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    
    callback_data = query.data
    now = datetime.now()
    start_dt, end_dt = None, None

    if callback_data == 'perf_today':
        start_dt = datetime.combine(now.date(), time.min)
        end_dt = now
    elif callback_data == 'perf_yesterday':
        yesterday = now.date() - timedelta(days=1)
        start_dt = datetime.combine(yesterday, time.min)
        end_dt = datetime.combine(yesterday, time.max)
    elif callback_data == 'perf_7_days':
        start_dt = datetime.combine(now.date() - timedelta(days=6), time.min)
        end_dt = now
    elif callback_data == 'perf_30_days':
        start_dt = datetime.combine(now.date() - timedelta(days=29), time.min)
        end_dt = now

    if start_dt and end_dt:
        await query.edit_message_text(
            text="⏳ Calculando desempenho para o período selecionado...",
            reply_markup=performance_menu_keyboard()
        )
        
        report_text = await generate_performance_report(user_id, start_dt, end_dt)
        
        await query.edit_message_text(
            text=report_text,
            parse_mode='HTML',
            reply_markup=performance_menu_keyboard()
        )

# --- FLUXO DE CONFIGURAÇÃO DE WHITELIST ---

async def ask_coin_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pergunta ao usuário sua nova whitelist de moedas."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    
    context.user_data['settings_message_id'] = query.message.message_id
    
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        current_whitelist = user.coin_whitelist if user else 'todas'
    finally:
        db.close()

    instructions = (
        f"<b>✅ Whitelist de Moedas</b>\n\n"
        f"Sua configuração atual é: <code>{current_whitelist}</code>\n\n"
        f"Envie uma lista de moedas e/ou categorias separadas por vírgula.\n\n"
        f"<b>Exemplos:</b>\n"
        f"• <code>todas</code> (para operar todos os sinais)\n"
        f"• <code>btcusdt, ethusdt, solusdt</code>\n"
        f"• <code>memecoins, btcusdt</code> (opera moedas meme + BTC)\n\n"
        f"<b>Categorias disponíveis:</b> <code>memecoins</code>, <code>altcoins</code>, <code>defi</code>."
    )
    
    await query.edit_message_text(text=instructions, parse_mode='HTML')
    return ASKING_COIN_WHITELIST

async def receive_coin_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe, valida e salva a nova whitelist."""
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')
    
    # Apaga a mensagem do usuário para manter o chat limpo
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)

    # Normaliza a entrada: remove espaços extras e converte para minúsculas
    whitelist_text = update.message.text.lower().strip()
    
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if user:
            user.coin_whitelist = whitelist_text
            db.commit()
            
            feedback_text = (
                f"✅ Whitelist de moedas atualizada para: <code>{whitelist_text}</code>"
            )

            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id_to_edit,
                text=feedback_text,
                parse_mode='HTML',
                reply_markup=settings_menu_keyboard(user)
            )
    finally:
        db.close()

    return ConversationHandler.END

async def list_closed_trades_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Busca no DB e lista os últimos trades fechados do usuário."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    
    await query.edit_message_text("Buscando seu histórico de trades...")

    db = SessionLocal()
    try:
        # Busca os últimos 15 trades fechados, ordenados do mais recente para o mais antigo
        closed_trades = db.query(Trade).filter(
            Trade.user_telegram_id == user_id,
            Trade.status.like('%CLOSED%')
        ).order_by(Trade.closed_at.desc()).limit(15).all()

        message = "<b>📜 Seus Últimos Trades Fechados</b>\n\n"

        if not closed_trades:
            message += "Nenhum trade fechado encontrado no seu histórico."
        else:
            for trade in closed_trades:
                # Define o emoji e o texto do resultado com base no status e no P/L
                pnl = trade.closed_pnl if trade.closed_pnl is not None else 0.0
                resultado_str = f"<b>Resultado: ${pnl:,.2f}</b>"
                
                emoji = "❔"
                if trade.status == 'CLOSED_PROFIT':
                    emoji = "🏆"
                elif trade.status == 'CLOSED_LOSS':
                    emoji = "🛑"
                elif trade.status == 'CLOSED_MANUAL':
                    emoji = "✅" if pnl >= 0 else "🔻"
                elif trade.status == 'CLOSED_GHOST':
                    emoji = "ℹ️"
                    resultado_str = "<i>Fechado externamente</i>"

                # Formata a data de fechamento
                data_fechamento = trade.closed_at.strftime('%d/%m %H:%M') if trade.closed_at else 'N/A'

                message += (
                    f"{emoji} <b>{trade.symbol}</b> ({trade.side})\n"
                    f"  - Fechado em: {data_fechamento}\n"
                    f"  - {resultado_str}\n\n"
                )
        
        # Cria um teclado com o botão para voltar ao menu de desempenho
        keyboard = [[InlineKeyboardButton("⬅️ Voltar ao Desempenho", callback_data='perf_today')]]
        
        await query.edit_message_text(
            text=message,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    finally:
        db.close()

async def prompt_manual_close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe a tela de confirmação para o fechamento manual de uma posição."""
    query = update.callback_query
    await query.answer()
    trade_id = int(query.data.split('_')[-1])
    
    db = SessionLocal()
    try:
        trade = db.query(Trade).filter_by(id=trade_id).first()
        if not trade:
            await query.edit_message_text("Erro: Trade não encontrado ou já fechado.")
            return

        message = (
            f"⚠️ <b>Confirmar Fechamento</b> ⚠️\n\n"
            f"Você tem certeza que deseja fechar manualmente sua posição em <b>{trade.symbol}</b>?\n\n"
            f"Esta ação é irreversível."
        )
        await query.edit_message_text(
            text=message,
            parse_mode='HTML',
            reply_markup=confirm_manual_close_keyboard(trade_id)
        )
    finally:
        db.close()

