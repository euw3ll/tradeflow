import logging
import asyncio
import os
import re
from telegram.ext import Application
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telethon.sync import TelegramClient
from telethon import events
from telethon.errors.rpcerrorlist import ChannelForumMissingError, ChannelInvalidError
from telethon.tl.functions.channels import GetForumTopicsRequest
from utils.config import API_ID, API_HASH
from database.session import SessionLocal
from database.models import MonitoredTarget
from .signal_parser import parse_signal

logger = logging.getLogger(__name__)

# --- LÓGICA DE CAMINHO DINÂMICO ---
if os.path.isdir('/data'):
    SESSION_PATH = '/data/tradeflow_user'
else:
    SESSION_PATH = 'tradeflow_user'

# --- DEFINIÇÃO ÚNICA E CORRETA DO CLIENTE ---
client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
comm_queue = None
# Cache em memória para armazenar os IDs das mensagens já processadas e evitar duplicidade.
PROCESSED_MESSAGE_IDS = set()


# --- Funções de Busca (Helpers) ---

def get_monitored_targets():
    """Busca no DB a lista de todos os alvos (canal/tópico) monitorados."""
    db = SessionLocal()
    try:
        return db.query(MonitoredTarget).all()
    finally:
        db.close()

async def list_channels():
    """Lista todos os canais e supergrupos com logging detalhado."""
    logger.info("[list_channels] Iniciando busca de diálogos...")
    channels = []
    count = 0
    try:
        async for dialog in client.iter_dialogs():
            count += 1
            if count % 50 == 0:
                logger.info(f"[list_channels] ... processou {count} diálogos...")
            
            if dialog.is_channel:
                channels.append((dialog.name, dialog.id))
        
        logger.info(f"[list_channels] Busca de diálogos finalizada. Total de {count} diálogos processados.")
    except Exception as e:
        logger.error(f"[list_channels] Erro durante iter_dialogs: {e}", exc_info=True)
        
    return channels

async def list_channel_topics(channel_id: int):
    """Busca os tópicos de um canal específico."""
    topics = []
    try:
        entity = await client.get_entity(channel_id)
        result = await client(GetForumTopicsRequest(
            channel=entity, offset_date=0, offset_id=0, offset_topic=0, limit=100
        ))
        for topic in result.topics:
            topics.append((topic.title, topic.id))
            
    except (ChannelForumMissingError, ChannelInvalidError):
        logger.warning(f"Canal {channel_id} não possui tópicos (não é um fórum).")
        
    except Exception as e:
        logger.error(f"Exceção em list_channel_topics para o canal {channel_id}: {e}", exc_info=True)
        
    return topics

# --- Listener de Sinais ---
# Em vez de filtrar por regex no decorator, ouvimos TUDO e deixamos o parser decidir.
from telethon import events

@client.on(events.NewMessage)
@client.on(events.MessageEdited)
async def signal_listener(event):
    """
    Ouve TODAS as mensagens e processa APENAS as dos alvos monitorados.
    """
    global comm_queue
    if not comm_queue: return

    chat_id = getattr(event, "chat_id", None)
    message_id = getattr(getattr(event, "message", None), "id", None)
    topic_id = event.reply_to.reply_to_msg_id if getattr(event, "reply_to", None) else None
    text = (getattr(event, "raw_text", None) or getattr(getattr(event, "message", None), "message", None) or "")

    # 1. Primeiro, verifica se a mensagem é de um alvo monitorado
    monitored_targets = get_monitored_targets()
    is_target = any(
        (t.channel_id == chat_id and ((t.topic_id is None and topic_id is None) or t.topic_id == topic_id))
        for t in monitored_targets
    )

    # 2. Se não for um alvo, a função termina silenciosamente.
    if not is_target:
        return

    # --- LÓGICA DE LOG MOVIDA PARA CÁ ---
    # 3. Agora que sabemos que a mensagem é importante, nós a registramos.
    preview = text.replace("\n", " ")[:120]
    logger.info(f"📨 [Telethon] Mensagem RELEVANTE recebida | chat_id={chat_id} | msg_id={message_id} | preview={preview!r}")

    # O resto da lógica para evitar duplicidade e processar o sinal continua a mesma...
    if message_id in PROCESSED_MESSAGE_IDS:
        logger.info(f"⏭️ [Telethon] Mensagem {message_id} já processada. Ignorando.")
        return

    from services.signal_parser import parse_signal
    parsed = parse_signal(text)

    if parsed:
        logger.info(
            "✅ [Telethon] É sinal! "
            f"type={parsed.get('type')} coin={parsed.get('coin')} "
            f"order={parsed.get('order_type')} entries={parsed.get('entries')} sl={parsed.get('stop_loss')}"
        )
        if message_id is not None:
            PROCESSED_MESSAGE_IDS.add(message_id)

        await comm_queue.put({
            "action": "process_signal",
            "signal_text": text,
            "source_name": f"telegram:{chat_id}"
        })

# --- Processador da Fila ---

async def queue_processor(queue: asyncio.Queue, ptb_app: Application):
    """Processa pedidos da fila, agora passando o 'source_name' adiante."""
    global comm_queue
    comm_queue = queue
    from core.trade_manager import process_new_signal

    while True:
        request = await queue.get()
        action = request.get("action")
        logger.info(f"[Queue Processor] ==> Pedido recebido! Ação: '{action}'")
        
        try:
            if action == "list_channels":
                logger.info("[Queue Processor] ... Entrou no bloco de 'list_channels'.")
                chat_id = request.get("chat_id")
                message_id = request.get("message_id")
                channels = await list_channels()
                db = SessionLocal()
                monitored_channels_ids = {target.channel_id for target in db.query(MonitoredTarget).all()}
                db.close()
                keyboard = []
                
                if channels:
                    for channel_name, channel_id in channels:
                        suffix = " ✅" if channel_id in monitored_channels_ids else ""
                        keyboard.append([InlineKeyboardButton(f"{channel_name}{suffix}", callback_data=f"monitor_channel_{channel_id}")])
                
                if keyboard:
                    await ptb_app.bot.edit_message_text(
                        chat_id=chat_id, message_id=message_id,
                        text="Selecione um grupo/canal (✅ = algum monitoramento ativo):",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                else:
                    await ptb_app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="Nenhum canal ou supergrupo encontrado.")

            elif action == "list_topics":
                logger.info("[Queue Processor] ... Entrou no bloco de 'list_topics'.")
                channel_id = request.get("channel_id")
                chat_id = request.get("chat_id")
                message_id = request.get("message_id")
                channel_name = request.get("channel_name")
                
                topics = await list_channel_topics(channel_id)
                db = SessionLocal()
                
                try:
                    if topics:
                        monitored_topic_ids = {t.topic_id for t in db.query(MonitoredTarget).filter_by(channel_id=channel_id).all() if t.topic_id}
                        keyboard = [[InlineKeyboardButton("⬅️ Voltar para Grupos", callback_data="admin_list_channels")]]
                        for name, topic_id in topics:
                            suffix = " ✅" if topic_id in monitored_topic_ids else ""
                            keyboard.append([InlineKeyboardButton(f"{name}{suffix}", callback_data=f"monitor_topic_{channel_id}_{topic_id}")])
                        
                        await ptb_app.bot.edit_message_text(
                            chat_id=chat_id, message_id=message_id,
                            text="Selecione o tópico para monitorar (✅ = já monitorado):",
                            reply_markup=InlineKeyboardMarkup(keyboard)
                        )
                    else:
                        existing = db.query(MonitoredTarget).filter_by(channel_id=channel_id, topic_id=None).first()
                        if existing:
                            db.delete(existing)
                            feedback_msg = f"❌ Canal '{channel_name}' removido da lista de monitoramento."
                        else:
                            new_target = MonitoredTarget(channel_id=channel_id, channel_name=channel_name)
                            db.add(new_target)
                            feedback_msg = f"✅ Canal '{channel_name}' adicionado à lista de monitoramento."
                        
                        db.commit()
                        await ptb_app.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=feedback_msg)
                finally:
                    db.close()

            elif action == "process_signal":
                logger.info("[Queue Processor] ... Entrou no bloco de 'process_signal'.")
                signal_text = request.get("signal_text")
                source_name = request.get("source_name", "Fonte Desconhecida")
                
                signal_data = parse_signal(signal_text)
                if signal_data:
                    await process_new_signal(signal_data, ptb_app, source_name)
                else:
                    logger.info("Mensagem da fila não é um sinal válido.")
            
            else:
                logger.warning(f"[Queue Processor] Ação desconhecida ou nula recebida: '{action}'")

        except Exception as e:
            logger.error(f"Erro CRÍTICO no processador da fila ao manusear a ação '{action}': {e}", exc_info=True)
        finally:
            queue.task_done()
            logger.info(f"[Queue Processor] <== Pedido '{action}' finalizado.")

# --- Função Principal do Serviço ---

async def start_signal_monitor(queue: asyncio.Queue):
    """Inicia o cliente Telethon, o ouvinte de sinais e o processador da fila."""
    logger.info("Iniciando monitor de sinais com Telethon...")
    
    await client.start()
    
    ptb_app = await queue.get()

    logger.info("✅ Monitor de sinais e processador de fila ativos.")
    
    asyncio.create_task(queue_processor(queue, ptb_app))
    
    await client.run_until_disconnected()