import os
import asyncio
import logging
from typing import Tuple
from telegram.ext import Application
from sqlalchemy.orm import Session
from database.session import SessionLocal
from database.models import User, Trade, PendingSignal, SignalForApproval
from services.bybit_service import (
    place_order, get_account_info,
    place_limit_order, cancel_order,
    get_order_history
)
from services.notification_service import send_notification
from utils.security import decrypt_data
from utils.config import ADMIN_ID
from bot.keyboards import signal_approval_keyboard
from services.signal_parser import SignalType
from core.whitelist_service import is_coin_in_whitelist

logger = logging.getLogger(__name__)


def _avaliar_sinal(signal_data: dict, user_settings: User) -> Tuple[bool, str]:
    min_confidence = user_settings.min_confidence
    signal_confidence = signal_data.get('confidence', 0.0)
    if signal_confidence is not None and signal_confidence < min_confidence:
        motivo = f"Confiança ({signal_confidence:.2f}%) é menor que o seu mínimo ({min_confidence:.2f}%)"
        return False, motivo
    return True, "Sinal aprovado pelos seus critérios."

async def _execute_trade(signal_data: dict, user: User, application: Application, db: Session, source_name: str):
    """Executa uma ordem a MERCADO, busca os detalhes da execução e envia uma notificação detalhada."""
    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)
    
    account_info = await get_account_info(api_key, api_secret)
    if not account_info.get("success"):
        await application.bot.send_message(chat_id=user.telegram_id, text=f"❌ Falha ao buscar seu saldo Bybit para operar {signal_data['coin']}.")
        return

    balance_data = account_info.get("data", {})
    balance = float(balance_data.get('available_balance_usdt', 0))

    order_result = await place_order(api_key, api_secret, signal_data, user, balance)
    
    if order_result.get("success"):
        order_data = order_result['data']
        order_id = order_data['orderId']
        
        await asyncio.sleep(2)
        final_order_data_result = await get_order_history(api_key, api_secret, order_id)
        if not final_order_data_result.get("success"):
            await application.bot.send_message(chat_id=user.telegram_id, text=f"⚠️ Ordem {signal_data['coin']} enviada, mas falha ao confirmar detalhes. Verifique na corretora.")
            return
        final_order_data = final_order_data_result['data']
        
        symbol = signal_data['coin']
        side = signal_data['order_type']
        leverage = user.max_leverage
        qty = float(final_order_data.get('cumExecQty', 0))
        entry_price = float(final_order_data.get('avgPrice', 0))
        
        if qty == 0 or entry_price == 0:
            await application.bot.send_message(chat_id=user.telegram_id, text=f"⚠️ Ordem {symbol} enviada, mas a execução reportou quantidade/preço zerado.")
            return
            
        margin = (qty * entry_price) / leverage if leverage > 0 else 0
        stop_loss = signal_data['stop_loss']
        
        # --- LÓGICA DE NOTIFICAÇÃO CORRIGIDA ---
        all_targets = signal_data.get('targets') or []
        take_profit_1 = all_targets[0] if all_targets else "N/A"
        num_targets = len(all_targets)

        tp_text = f"${float(take_profit_1):,.4f}" if isinstance(take_profit_1, (int, float)) else take_profit_1
        
        if num_targets > 1:
            tp_text += f" (de {num_targets} alvos)"

        message = (
            f"📈 <b>Ordem a Mercado Aberta!</b>\n\n"
            f"  - 📊 <b>Tipo:</b> {side} | <b>Alavancagem:</b> {leverage}x\n"
            f"  - 💎 <b>Moeda:</b> {symbol}\n"
            f"  - 🔢 <b>Quantidade:</b> {qty:g}\n"
            f"  - 💵 <b>Preço de Entrada:</b> ${entry_price:,.4f}\n"
            f"  - 💰 <b>Margem:</b> ${margin:,.2f}\n"
            f"  - 🛡️ <b>Stop Loss:</b> ${stop_loss:,.4f}\n"
            f"  - 🎯 <b>Take Profit 1:</b> {tp_text}"
        )
        # 1. ENVIAMOS A MENSAGEM E CAPTURAMOS O OBJETO 'sent_message'
        sent_message = await application.bot.send_message(chat_id=user.telegram_id, text=message, parse_mode='HTML')

        # 2. CRIAMOS O TRADE E JÁ INCLUÍMOS O ID DA MENSAGEM
        new_trade = Trade(
            user_telegram_id=user.telegram_id, order_id=order_id,
            notification_message_id=sent_message.message_id, # <-- MUDANÇA AQUI
            symbol=symbol, side=side, qty=qty, entry_price=entry_price,
            stop_loss=stop_loss, current_stop_loss=stop_loss,
            initial_targets=all_targets,
            total_initial_targets=num_targets,
            status='ACTIVE',
            remaining_qty=qty
        )
        db.add(new_trade)
        logger.info(f"Trade {order_id} para o usuário {user.telegram_id} salvo no DB com dados de execução.")

async def process_new_signal(signal_data: dict, application: Application, source_name: str):
    """Processa um novo sinal, verificando a preferência de cada usuário individualmente."""
    signal_type = signal_data.get("type")
    symbol = signal_data.get("coin")
    db = SessionLocal()
    try:
        if signal_type == SignalType.CANCELAR:
            # A lógica de cancelamento permanece a mesma
            logger.info(f"Recebido sinal de cancelamento para {symbol}.")
            # ... (código de cancelamento que você já tem)
            db.commit()
            return

        elif signal_type in [SignalType.MARKET, SignalType.LIMIT]:
            all_users = db.query(User).filter(User.api_key_encrypted.isnot(None)).all()
            if not all_users:
                logger.info("Nenhum usuário com API para processar o sinal.")
                return

            logger.info(f"Sinal para {symbol} recebido. Verificando preferências de {len(all_users)} usuário(s)...")

            for user in all_users:
                # 1. Avalia o sinal contra os filtros do usuário
                aprovado, motivo = _avaliar_sinal(signal_data, user)
                if not aprovado:
                    logger.info(f"Sinal para {symbol} ignorado para o usuário {user.telegram_id}: {motivo}")
                    continue
                
                # 2. Verifica a whitelist do usuário
                if not is_coin_in_whitelist(symbol, user.coin_whitelist):
                    logger.info(f"Sinal para {symbol} ignorado para o usuário {user.telegram_id} devido à whitelist.")
                    continue

                # 3. Verifica o modo de aprovação individual do usuário
                if user.approval_mode == 'AUTOMATIC':
                    logger.info(f"Usuário {user.telegram_id} em modo AUTOMÁTICO. Executando trade para {symbol}.")
                    if signal_type == SignalType.MARKET:
                        await _execute_trade(signal_data, user, application, db, source_name)
                    elif signal_type == SignalType.LIMIT:
                        await _execute_limit_order_for_user(signal_data, user, application, db)

                elif user.approval_mode == 'MANUAL':
                    logger.info(f"Usuário {user.telegram_id} em modo MANUAL. Enviando sinal para sua aprovação.")
                    
                    new_signal_for_approval = SignalForApproval(
                        user_telegram_id=user.telegram_id,  # <-- Agora salva o ID do usuário correto
                        symbol=symbol,
                        source_name=source_name,
                        signal_data=signal_data
                    )
                    db.add(new_signal_for_approval)
                    db.commit() # Commit para obter o ID

                    signal_details = (
                        f"<b>Sinal Recebido para Aprovação</b>\n\n"
                        f"<b>Moeda:</b> {signal_data['coin']}\n"
                        f"<b>Tipo:</b> {signal_data['order_type']}\n<b>Entrada:</b> {signal_data['entries'][0]}\n"
                        f"<b>Stop:</b> {signal_data['stop_loss']}\n<b>Alvo 1:</b> {signal_data['targets'][0]}\n\n"
                        f"O sinal passou nos seus filtros. Você aprova a entrada?"
                    )
                    sent_message = await application.bot.send_message(
                        chat_id=user.telegram_id, # <-- Envia para o usuário específico
                        text=signal_details, parse_mode='HTML',
                        reply_markup=signal_approval_keyboard(new_signal_for_approval.id)
                    )
                    new_signal_for_approval.approval_message_id = sent_message.message_id
        
        db.commit()
    finally:
        db.close()

async def _execute_limit_order_for_user(signal_data: dict, user: User, application: Application, db: Session):
    """Função auxiliar para posicionar uma ordem limite para um único usuário."""
    symbol = signal_data.get("coin")
    existing_pending = db.query(PendingSignal).filter_by(user_telegram_id=user.telegram_id, symbol=symbol).first()
    if existing_pending:
        await application.bot.send_message(chat_id=user.telegram_id, text=f"ℹ️ Você já tem uma ordem limite pendente para <b>{symbol}</b>.", parse_mode='HTML')
        return

    entries = (signal_data.get('entries') or [])[:2]
    if not entries:
        logger.warning(f"Sinal LIMIT para {symbol} sem preços de entrada válidos.")
        return

    limit_price = float(min(entries)) if (signal_data.get('order_type') or '').upper() == 'LONG' else float(max(entries))
    signal_data['limit_price'] = limit_price

    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)
    account_info = await get_account_info(api_key, api_secret)
    if not account_info.get("success"):
        logger.error(f"Falha ao buscar saldo para usuário {user.telegram_id} ao posicionar LIMIT em {symbol}.")
        return

    balance = float(account_info.get("data", {}).get('available_balance_usdt', 0))
    limit_order_result = await place_limit_order(api_key, api_secret, signal_data, user, balance)

    if limit_order_result.get("success"):
        order_id = limit_order_result["data"]["orderId"]
        db.add(PendingSignal(user_telegram_id=user.telegram_id, symbol=symbol, order_id=order_id, signal_data=signal_data))
        
        # --- LÓGICA DE NOTIFICAÇÃO COMPLETA ---
        all_targets = signal_data.get('targets') or []
        take_profit_1 = all_targets[0] if all_targets else "N/A"
        num_targets = len(all_targets)
        tp_text = f"${float(take_profit_1):,.4f}" if isinstance(take_profit_1, (int, float)) else take_profit_1
        if num_targets > 1:
            tp_text += f" (de {num_targets} alvos)"
        
        message = (
            f"✅ <b>Ordem Limite Posicionada!</b>\n\n"
            f"  - 📊 <b>Tipo:</b> {signal_data.get('order_type')} | <b>Alavancagem:</b> {user.max_leverage}x\n"
            f"  - 💎 <b>Moeda:</b> {symbol}\n"
            f"  - 🎯 <b>Preço de Entrada:</b> ${limit_price:,.4f}\n"
            f"  - 🛡️ <b>Stop Loss:</b> ${signal_data.get('stop_loss'):,.4f}\n"
            f"  - 🎯 <b>Take Profit 1:</b> {tp_text}\n\n"
            f"👀 Monitorando a execução…"
        )
        await application.bot.send_message(chat_id=user.telegram_id, text=message, parse_mode='HTML')
    else:
        error = limit_order_result.get('error') or "Erro desconhecido"
        await application.bot.send_message(chat_id=user.telegram_id, text=f"❌ Falha ao posicionar sua ordem limite para <b>{symbol}</b>.\n<b>Motivo:</b> {error}", parse_mode='HTML')