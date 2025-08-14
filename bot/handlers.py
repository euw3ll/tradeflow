import logging
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler
from telegram.error import BadRequest
from database.session import SessionLocal
from database.models import User, InviteCode, MonitoredTarget, Trade, SignalForApproval
from .keyboards import main_menu_keyboard, confirm_remove_keyboard, admin_menu_keyboard, dashboard_menu_keyboard, settings_menu_keyboard, view_targets_keyboard, bot_config_keyboard
from utils.security import encrypt_data, decrypt_data
from services.bybit_service import get_open_positions, get_account_info, close_partial_position
from utils.config import ADMIN_ID
from core.report_service import generate_performance_report
from database.crud import get_user_by_id
from core.trade_manager import _execute_trade

# Estados para as conversas
(WAITING_CODE, WAITING_API_KEY, WAITING_API_SECRET, CONFIRM_REMOVE_API) = range(4)
(ASKING_RISK_PERCENT, ASKING_MAX_LEVERAGE, ASKING_MIN_CONFIDENCE) = range(10, 13)
(ASKING_PROFIT_TARGET, ASKING_LOSS_LIMIT) = range(13, 15)

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
        # --- NOVA LINHA DE AVISO ---
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
    # Apaga a mensagem do usuário que contém a chave
    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id
    )

    api_key = update.message.text
    context.user_data['api_key'] = api_key
    
    # Envia a próxima pergunta e guarda a mensagem para apagar depois
    prompt_message = await update.message.reply_text(
        "Chave API recebida com segurança. Agora, por favor, envie sua *API Secret*.",
        parse_mode='Markdown'
    )
    # Guarda o ID da mensagem do bot para o próximo passo
    context.user_data['prompt_message_id'] = prompt_message.message_id
    
    return WAITING_API_SECRET

async def receive_api_secret(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe a API Secret, apaga as mensagens, criptografa e salva no banco."""
    # Apaga a mensagem do usuário que contém o segredo
    await context.bot.delete_message(
        chat_id=update.effective_chat.id,
        message_id=update.message.message_id
    )

    # Apaga a pergunta anterior do bot ("...envie sua API Secret")
    prompt_message_id = context.user_data.get('prompt_message_id')
    if prompt_message_id:
        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=prompt_message_id
        )

    api_secret = update.message.text
    api_key = context.user_data.get('api_key')
    telegram_id = update.effective_user.id

    # Criptografa e salva as chaves no banco (lógica existente)
    encrypted_key = encrypt_data(api_key)
    encrypted_secret = encrypt_data(api_secret)

    db = SessionLocal()
    try:
        user_to_update = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user_to_update:
            user_to_update.api_key_encrypted = encrypted_key
            user_to_update.api_secret_encrypted = encrypted_secret
            db.commit()
            
            # Edita a mensagem original do menu para a confirmação final
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=context.user_data['entry_message_id'], # ID da mensagem do menu
                text="✅ Suas chaves de API foram salvas com sucesso!",
            )
            # Envia um novo menu principal
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

    # Envia um novo menu principal atualizado
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
        active_trades = db.query(Trade).filter(Trade.user_telegram_id == user_id, ~Trade.status.like('%CLOSED%')).all()
        message = "<b>📊 Suas Posições Ativas (Gerenciadas pelo Bot)</b>\n\n"
        keyboard = []
        if active_trades:
            for trade in active_trades:
                side_emoji = "🔼" if trade.side == 'LONG' else "🔽"
                message += f"- {side_emoji} {trade.symbol} ({trade.qty} unid.)\n  Entrada: ${trade.entry_price:,.4f} | Status: {trade.status}\n\n"
                keyboard.append([InlineKeyboardButton(f"Fechar {trade.symbol} ❌", callback_data=f"manual_close_{trade.id}")])
        else:
            message += "Nenhuma posição sendo gerenciada no momento."
        
        keyboard.append([InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='back_to_main_menu')])
        await query.edit_message_text(message, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    finally:
        db.close()

async def user_dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe o painel com saldo focado em USDT, outras moedas relevantes e posições."""
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
            await query.edit_message_text(
                "Você precisa configurar sua API primeiro.",
                reply_markup=main_menu_keyboard(telegram_id=user_id)
            )
            return

        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)

        account_info, positions_info = await asyncio.gather(
            get_account_info(api_key, api_secret),
            get_open_positions(api_key, api_secret)
        )
        
        message = "<b>ℹ️ Seu Painel de Controle</b>\n\n"
        
        # --- MELHORIA DE LAYOUT APLICADA AQUI ---
        message += "<b>Saldos na Carteira:</b>\n"
        
        if account_info.get("success"):
            balances = account_info.get("data", [])
            
            if balances and isinstance(balances, list) and len(balances) > 0:
                coin_list = balances[0].get('coin', [])
                
                usdt_balance_value = 0.0
                other_coins_lines = []
                found_relevant_coins = False

                for coin_balance in coin_list:
                    coin_name = coin_balance.get('coin')
                    wallet_balance = float(coin_balance.get('walletBalance', '0'))

                    if coin_name == 'USDT':
                        usdt_balance_value = wallet_balance
                        found_relevant_coins = True
                    elif wallet_balance >= 0.1:
                        balance_str = f"{wallet_balance:.2f}"
                        other_coins_lines.append(f"- {coin_name}: {balance_str}")
                        found_relevant_coins = True
                
                usdt_display = f"{usdt_balance_value:.2f}"
                message += f"<b>- USDT: {usdt_display}</b>\n"

                if other_coins_lines:
                    message += "\n".join(other_coins_lines)
                
                if not found_relevant_coins and usdt_balance_value == 0.0:
                    message += "Nenhum saldo relevante encontrado.\n"

            else:
                 message += "Nenhuma moeda encontrada na carteira.\n"
        else:
            message += f"Erro ao buscar saldo: {account_info.get('error')}\n"
        
        # --- MELHORIA DE LAYOUT APLICADA AQUI ---
        message += "\n\n" # Adiciona espaço extra antes da próxima seção
        
        message += "<b>Posições Abertas:</b>\n"
        if positions_info.get("success") and positions_info.get("data"):
            for pos in positions_info["data"]:
                try:
                    pnl = float(pos.get('unrealisedPnl', '0'))
                    side_emoji = "🔼" if pos['side'] == 'Buy' else "🔽"
                    message += f"- {side_emoji} {pos['symbol']}: {pos['size']} | P/L: ${pnl:,.2f}\n"
                except (ValueError, TypeError):
                    message += f"- {pos.get('symbol', '???')}: Dados de P/L inválidos.\n"
        else:
            message += "- Nenhuma posição aberta no momento."

        message += "\n\n<i>⚠️ Este bot opera exclusivamente com pares USDT.</i>"

        await query.edit_message_text(
            message, 
            parse_mode='HTML', 
            reply_markup=dashboard_menu_keyboard()
        )

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
    
    # --- MENSAGEM MODIFICADA ---
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
    
    # Encontra o nome do canal a partir do botão clicado
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
        "channel_name": channel_name # --- Enviando o nome do canal ---
    }
    
    await comm_queue.put(request_data)
    await query.edit_message_text("Processando...")

async def select_topic_to_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Salva/remove o tópico e pede para a fila recarregar o menu de tópicos."""
    query = update.callback_query
    await query.answer() # Responde ao clique imediatamente para o ícone de 'carregando' sumir

    comm_queue = context.application.bot_data.get('comm_queue')
    if not comm_queue:
        logger.error("Fila de comunicação não encontrada no contexto do bot.")
        return

    user_id = update.effective_user.id
    if user_id != ADMIN_ID: return

    # Extrai os IDs do callback_data
    _, _, channel_id_str, topic_id_str = query.data.split('_')
    channel_id = int(channel_id_str)
    topic_id = int(topic_id_str)
    
    db = SessionLocal()
    try:
        existing_target = db.query(MonitoredTarget).filter_by(channel_id=channel_id, topic_id=topic_id).first()
        
        if existing_target:
            # Se já existe, remove da lista
            db.delete(existing_target)
        else:
            # Se não existe, adiciona na lista
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

    # --- LÓGICA DE RECARREGAMENTO ---
    # Cria um novo "pedido" para a fila, para listar os tópicos do mesmo canal novamente.
    # O processador da fila vai receber isso e redesenhar o menu.
    request_data = {
        "action": "list_topics",
        "chat_id": query.message.chat_id,
        "message_id": query.message.message_id,
        "channel_id": channel_id,
        "channel_name": "" # Não é necessário aqui, pois estamos apenas listando tópicos
    }
    await comm_queue.put(request_data)

async def back_to_channels_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retorna o usuário para a lista de canais/grupos."""
    # Simplesmente chama a função que já lista os canais
    await list_channels_handler(update, context)

    async def my_dashboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Exibe um painel completo com informações da conta, posições e monitoramentos."""
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("Buscando informações do seu painel, aguarde...")

        user_id = update.effective_user.id
        db = SessionLocal()
        
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            if not user or not user.api_key_encrypted:
                await query.edit_message_text("Você precisa configurar suas chaves de API primeiro.", reply_markup=main_menu_keyboard(user_id))
                return

            api_key = decrypt_data(user.api_key_encrypted)
            api_secret = decrypt_data(user.api_secret_encrypted)

            # 1. Buscar Saldo da Conta
            account_info = get_account_info(api_key, api_secret)
            
            # 2. Buscar Posições Abertas na Bybit
            positions_info = get_open_positions(api_key, api_secret)
            
            # 3. Buscar Alvos Monitorados no nosso DB
            monitored_targets = db.query(MonitoredTarget).all()

            # --- Montagem da Mensagem ---
            message = "<b>Seu Painel de Controle</b>\n\n"

            # Seção de Saldo
            if account_info.get("success"):
                balance = float(account_info['data']['totalEquity'])
                message += f"<b>Conta Bybit:</b>\n- Saldo Total: ${balance:,.2f}\n\n"
            else:
                message += "<b>Conta Bybit:</b>\n- Erro ao buscar saldo.\n\n"

            # Seção de Posições Abertas
            message += "<b>Posições Abertas:</b>\n"
            if positions_info.get("success") and positions_info.get("data"):
                for pos in positions_info["data"]:
                    pnl_percent = float(pos.get('unrealisedPnl', '0')) / (float(pos.get('avgPrice', '1')) * float(pos.get('size', '1'))) * 100 if pos.get('avgPrice') and pos.get('size') else 0
                    message += f"- {pos['symbol']} ({pos['side']}): {pos['size']} | P/L: ${float(pos.get('unrealisedPnl', '0')):,.2f} ({pnl_percent:.2f}%)\n"
            else:
                message += "- Nenhuma posição aberta no momento.\n\n"

            # Seção de Monitoramentos
            message += "<b>Alvos Monitorados:</b>\n"
            if monitored_targets:
                for target in monitored_targets:
                    if target.topic_id:
                        message += f"- {target.channel_name or 'Grupo'} | Tópico: {target.topic_name}\n"
                    else:
                        message += f"- Canal/Grupo: {target.channel_name}\n"
            else:
                message += "- Nenhum alvo sendo monitorado."

            await query.edit_message_text(message, parse_mode='HTML')

        except Exception as e:
            logger.error(f"Erro ao montar o painel: {e}")
            await query.edit_message_text("Ocorreu um erro ao buscar os dados do seu painel.")
        finally:
            db.close()

async def my_positions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Exibe as posições ativas com botões para gerenciamento manual."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Buscando suas posições gerenciadas...")

    user_id = update.effective_user.id
    db = SessionLocal()
    try:
        # Busca os trades que o bot abriu e que não estão fechados
        active_trades = db.query(Trade).filter(
            Trade.user_telegram_id == user_id, 
            ~Trade.status.like('%CLOSED%')
        ).all()
        
        message = "<b>📊 Suas Posições Ativas (Gerenciadas pelo Bot)</b>\n\n"
        keyboard = [] # Vamos construir o teclado dinamicamente

        if active_trades:
            for trade in active_trades:
                side_emoji = "🔼" if trade.side == 'LONG' else "🔽"
                message += f"- {side_emoji} {trade.symbol} ({trade.qty} unid.)\n"
                message += f"  Entrada: ${trade.entry_price:,.4f} | Status: {trade.status}\n\n"
                
                # Adiciona um botão para cada trade, passando o ID do trade no callback
                keyboard.append([
                    InlineKeyboardButton(f"Fechar {trade.symbol} ❌", callback_data=f"manual_close_{trade.id}")
                ])
        else:
            message += "Nenhuma posição sendo gerenciada no momento."
        
        # Adiciona o botão de voltar ao menu principal
        keyboard.append([InlineKeyboardButton("⬅️ Voltar ao Menu", callback_data='back_to_main_menu')])
        
        reply_markup=main_menu_keyboard(telegram_id=user_id)
        await query.edit_message_text(message, parse_mode='HTML', reply_markup=reply_markup)
    finally:
        db.close()

async def back_to_main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retorna o usuário para o menu principal."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    # Simplesmente edita a mensagem para mostrar o menu principal correto
    await query.edit_message_text(
        "Menu Principal:",
        reply_markup=main_menu_keyboard(telegram_id=user_id)
    )

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

async def ask_risk_percent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Pergunta ao usuário qual o novo percentual de risco."""
    query = update.callback_query
    await query.answer()
    
    # Guarda o ID da mensagem para podermos editá-la ou apagá-la depois
    context.user_data['settings_message_id'] = query.message.message_id
    
    await query.edit_message_text(
        "Por favor, envie o novo percentual de risco por trade.\n"
        "Envie apenas o número (ex: `1.5` para 1.5%)."
    )
    return ASKING_RISK_PERCENT

async def receive_risk_percent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recebe, valida e salva o novo percentual de risco."""
    user_id = update.effective_user.id
    message_id_to_edit = context.user_data.get('settings_message_id')

    try:
        risk_value = float(update.message.text.replace(',', '.'))
        if not (0.1 <= risk_value <= 100):
            raise ValueError("Valor fora do range permitido")

        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            user.risk_per_trade_percent = risk_value
            db.commit()
            
            # Edita a mensagem original para mostrar o menu de configurações atualizado
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id_to_edit,
                text=f"✅ Risco por trade atualizado para {risk_value:.2f}%.\n\n"
                     "Selecione outra opção para editar ou volte.",
                reply_markup=settings_menu_keyboard(user)
            )
        finally:
            db.close()

    except (ValueError, TypeError):
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id_to_edit,
            text="❌ Valor inválido. Por favor, tente novamente com um número (ex: 1.5)."
        )
        # Permite que o usuário tente novamente sem sair da conversa
        return ASKING_RISK_PERCENT
    finally:
        # Apaga a mensagem do usuário com o número
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)

    return ConversationHandler.END # Finaliza a conversa

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
        # Tenta converter o texto para um número inteiro
        leverage_value = int(update.message.text)
        # Limites da Bybit, por segurança
        if not (1 <= leverage_value <= 125):
            raise ValueError("Alavancagem fora do limite (1-125)")

        db = SessionLocal()
        try:
            user = db.query(User).filter_by(telegram_id=user_id).first()
            user.max_leverage = leverage_value
            db.commit()
            
            # Edita a mensagem original para mostrar o menu de configurações atualizado
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
        # Apaga a mensagem do usuário com o número
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
    
    # Apaga a mensagem do usuário (seja ela válida ou não) para manter o chat limpo
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
            
            # Edita a mensagem do bot para mostrar o menu de configurações atualizado
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=message_id_to_edit,
                text=f"✅ Confiança mínima atualizada para {confidence_value:.2f}%.\n\n"
                     "Selecione outra opção para editar ou volte.",
                reply_markup=settings_menu_keyboard(user)
            )
        finally:
            db.close()

        return ConversationHandler.END # Finaliza a conversa APENAS se deu tudo certo

    except (ValueError, TypeError):
        # --- LÓGICA DE TRATAMENTO DE ERRO ---
        logger.warning(f"Usuário {user_id} enviou um valor inválido para confiança: {update.message.text}")
        
        # Edita a mensagem do bot para avisar do erro e pedir para tentar de novo
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=message_id_to_edit,
            text="❌ <b>Valor inválido.</b>\nPor favor, envie apenas um número entre 0 e 100 (ex: 75).",
            parse_mode='HTML'
        )
        
        # Mantém o usuário na mesma etapa para que ele possa tentar de novo
        return ASKING_MIN_CONFIDENCE
    
async def report_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Envia o relatório de performance para o usuário."""
    user_id = update.effective_user.id
    
    # Gera a mensagem do relatório chamando nossa nova função
    report_text = generate_performance_report(user_id)
    
    await update.message.reply_text(
        text=report_text,
        parse_mode='HTML'
    )

async def manual_close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lida com o fechamento manual de uma posição pelo usuário."""
    query = update.callback_query
    await query.answer("Processando fechamento...")

    # Extrai o ID do trade do callback_data (ex: "manual_close_12")
    trade_id = int(query.data.split('_')[-1])
    user_id = update.effective_user.id

    db = SessionLocal()
    try:
        # Busca o trade específico no banco de dados
        trade_to_close = db.query(Trade).filter_by(id=trade_id, user_telegram_id=user_id).first()

        if not trade_to_close:
            await query.edit_message_text("Erro: Trade não encontrado ou já fechado.")
            return

        # Pega as credenciais do usuário
        user = db.query(User).filter_by(telegram_id=user_id).first()
        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)

        # Chama a função para fechar a quantidade restante da posição
        close_result = close_partial_position(
            api_key, 
            api_secret, 
            trade_to_close.symbol, 
            trade_to_close.remaining_qty, 
            trade_to_close.side
        )

        if close_result.get("success"):
            trade_to_close.status = 'CLOSED_MANUAL'
            db.commit()
            await query.edit_message_text(f"✅ Posição para {trade_to_close.symbol} fechada manualmente com sucesso!")
            # Opcional: Recarregar o menu de posições para mostrar a lista atualizada
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
        # --- CORREÇÃO APLICADA AQUI ---
        # Busca o usuário usando a sessão local da função, em vez de get_user_by_id
        user = db.query(User).filter(User.telegram_id == user_id).first()
        
        if user:
            # Lógica para alternar o modo
            if user.approval_mode == 'AUTOMATIC':
                user.approval_mode = 'MANUAL'
            else:
                user.approval_mode = 'AUTOMATIC'
            
            db.commit() # Agora o commit salvará o objeto 'user' que pertence a esta sessão 'db'
            
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
    """
    Lida com a decisão do usuário (Aprovar/Rejeitar) para um sinal manual.
    """
    query = update.callback_query
    await query.answer()

    action, signal_id_str = query.data.split('_', 1)[-1].rsplit('_', 1)
    signal_id = int(signal_id_str)
    
    db = SessionLocal()
    try:
        # Busca o sinal que está aguardando aprovação no banco de dados
        signal_to_process = db.query(SignalForApproval).filter_by(id=signal_id).first()

        if not signal_to_process:
            await query.edit_message_text("Este sinal já foi processado ou expirou.")
            return

        if action == 'approve':
            await query.edit_message_text("✅ **Entrada Aprovada!** Processando ordem...")
            
            user = get_user_by_id(signal_to_process.user_telegram_id)
            signal_data = signal_to_process.signal_data
            source_name = signal_to_process.source_name
            
            # Chama a função que executa o trade (que já criamos)
            await _execute_trade(signal_data, user, context.application, db, source_name)
            
        elif action == 'reject':
            await query.edit_message_text("❌ **Entrada Rejeitada.** O sinal foi descartado.")
        
        # Remove o sinal da tabela de aprovação em ambos os casos
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
