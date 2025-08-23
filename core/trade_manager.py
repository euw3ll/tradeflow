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

        new_trade = Trade(
            user_telegram_id=user.telegram_id, order_id=order_id,
            symbol=symbol, side=side, qty=qty, entry_price=entry_price,
            stop_loss=stop_loss, current_stop_loss=stop_loss,
            initial_targets=all_targets, status='ACTIVE', # Garante que todos os alvos sejam salvos
            remaining_qty=qty
        )
        db.add(new_trade)
        logger.info(f"Trade {order_id} para o usuário {user.telegram_id} salvo no DB com dados de execução.")
        
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
        await application.bot.send_message(chat_id=user.telegram_id, text=message, parse_mode='HTML')
    else:
        error_msg = order_result.get('error')
        await application.bot.send_message(chat_id=user.telegram_id, text=f"❌ <b>Falha ao Abrir Ordem</b>\n<b>Moeda:</b> {signal_data['coin']}\n<b>Motivo:</b> {error_msg}", parse_mode='HTML')

async def execute_signal_for_all_users(signal_data: dict, application: Application, db: Session, source_name: str):
    signal_type = signal_data.get("type")
    symbol = signal_data.get("coin")

    all_users_to_trade = db.query(User).filter(User.api_key_encrypted.isnot(None)).all()
    if not all_users_to_trade:
        logger.info("Nenhum usuário com API configurada para replicar o trade.")
        return

    logger.info(f"Sinal ({signal_type}) aprovado. Replicando para {len(all_users_to_trade)} usuário(s)...")

    for user in all_users_to_trade:
        if not is_coin_in_whitelist(symbol, user.coin_whitelist):
            logger.info(f"Sinal para {symbol} ignorado para o usuário {user.telegram_id} devido à sua whitelist ('{user.coin_whitelist}').")
            # Opcional: notificar o usuário que o sinal foi ignorado
            # await application.bot.send_message(chat_id=user.telegram_id, text=f"ℹ️ Sinal para {symbol} ignorado devido à sua whitelist.")
            continue # Pula para o próximo usuário
        if signal_type == SignalType.MARKET:
            await _execute_trade(signal_data, user, application, db, source_name)
        elif signal_type == SignalType.LIMIT:
            existing_pending = db.query(PendingSignal).filter_by(user_telegram_id=user.telegram_id, symbol=symbol).first()
            if existing_pending:
                await application.bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"ℹ️ Você já tem uma ordem limite pendente para <b>{symbol}</b>. O novo sinal foi ignorado.",
                    parse_mode='HTML'
                )
                continue

            # ---- NOVO: escolher um único preço para a ordem limite ----
            entries = (signal_data.get('entries') or [])[:2]
            if not entries:
                await application.bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"❌ Sinal LIMIT para <b>{symbol}</b> sem preços de entrada válidos.",
                    parse_mode='HTML'
                )
                continue

            if len(entries) == 1:
                limit_price = float(entries[0])
            else:
                lo = float(min(entries[0], entries[1]))
                hi = float(max(entries[0], entries[1]))
                if (signal_data.get('order_type') or '').upper() == 'LONG':
                    limit_price = lo   # LONG -> comprar no mais baixo da faixa
                else:
                    limit_price = hi   # SHORT -> vender no mais alto da faixa

            # injeta no payload para a bybit_service
            signal_data_with_price = dict(signal_data)
            signal_data_with_price['limit_price'] = limit_price
            # -----------------------------------------------------------

            user_api_key = decrypt_data(user.api_key_encrypted)
            user_api_secret = decrypt_data(user.api_secret_encrypted)

            account_info = await get_account_info(user_api_key, user_api_secret)
            if not account_info.get("success"):
                await application.bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"❌ Falha ao buscar seu saldo Bybit para posicionar LIMIT em <b>{symbol}</b>.",
                    parse_mode='HTML'
                )
                continue

            balance_data = account_info.get("data", {})
            balance = float(balance_data.get('available_balance_usdt', 0))

            # >>> IMPORTANTE: agora enviamos signal_data_with_price <<<
            limit_order_result = await place_limit_order(user_api_key, user_api_secret, signal_data_with_price, user, balance)

            if limit_order_result.get("success"):
                order_id = limit_order_result["data"]["orderId"]
                db.add(PendingSignal(
                    user_telegram_id=user.telegram_id,
                    symbol=symbol,
                    order_id=order_id,
                    signal_data=signal_data_with_price
                ))
                await application.bot.send_message(
                    chat_id=user.telegram_id,
                    text=(
                        f"✅ Ordem <b>Limite</b> posicionada para <b>{symbol}</b> ({signal_data.get('order_type')}).\n"
                        f"🎯 Preço: <b>{limit_price}</b>\n"
                        f"🛑 Stop: <b>{signal_data.get('stop_loss')}</b>\n"
                        f"👀 Monitorando a execução…"
                    ),
                    parse_mode='HTML'
                )
            else:
                error = limit_order_result.get('error') or limit_order_result
                await application.bot.send_message(
                    chat_id=user.telegram_id,
                    text=f"❌ Falha ao posicionar sua ordem limite para <b>{symbol}</b>.\n<b>Motivo:</b> {error}",
                    parse_mode='HTML'
                )
    db.commit()

async def process_new_signal(signal_data: dict, application: Application, source_name: str):
    """
    Processa apenas sinais de ENTRADA e CANCELAMENTO, ignorando todos os outros.
    """
    signal_type = signal_data.get("type")
    symbol = signal_data.get("coin")
    db = SessionLocal()
    try:
        # --- LÓGICA DE CANCELAMENTO ---
        if signal_type == SignalType.CANCELAR:
            logger.info(f"Recebido sinal de cancelamento para {symbol}.")
            pending_orders = db.query(PendingSignal).filter_by(symbol=symbol).all()
            if not pending_orders:
                await send_notification(application, f"ℹ️ Recebido sinal de cancelamento para <b>{symbol}</b>, mas nenhuma ordem pendente foi encontrada.")
                return
            
            for order in pending_orders:
                user_keys = db.query(User).filter_by(telegram_id=order.user_telegram_id).first()
                if not user_keys: continue
                api_key = decrypt_data(user_keys.api_key_encrypted)
                api_secret = decrypt_data(user_keys.api_secret_encrypted)
                cancel_result = await cancel_order(api_key, api_secret, order.order_id, symbol)
                if cancel_result.get("success"):
                    await application.bot.send_message(chat_id=order.user_telegram_id, text=f"✅ Sua ordem limite para <b>{symbol}</b> foi cancelada com sucesso.", parse_mode='HTML')
                    db.delete(order)
                else:
                    await application.bot.send_message(chat_id=order.user_telegram_id, text=f"⚠️ Falha ao cancelar sua ordem limite para <b>{symbol}</b>.", parse_mode='HTML')
            db.commit()
            return

        # --- LÓGICA DE SINAIS DE ENTRADA ---
        elif signal_type in [SignalType.MARKET, SignalType.LIMIT]:
            admin_user = db.query(User).filter_by(telegram_id=ADMIN_ID).first()
            if not admin_user or not admin_user.api_key_encrypted:
                logger.error("Admin não configurado, não é possível processar sinais de entrada.")
                return

            aprovado, motivo = _avaliar_sinal(signal_data, admin_user)
            if not aprovado:
                await send_notification(application, f"ℹ️ Sinal para {symbol} ignorado pelo admin: {motivo}")
                return
            
            if admin_user.approval_mode == 'AUTOMATIC':
                await execute_signal_for_all_users(signal_data, application, db, source_name)
            
            elif admin_user.approval_mode == 'MANUAL':
                logger.info(f"Modo MANUAL. Enviando sinal ({signal_type}) de entrada para aprovação do Admin.")
                
                new_signal_for_approval = SignalForApproval(
                    user_telegram_id=ADMIN_ID, symbol=symbol,
                    source_name=source_name, signal_data=signal_data
                )
                db.add(new_signal_for_approval)
                db.commit()

                signal_details = (
                    f"<b>Sinal ({signal_type}) de: {source_name}</b>\n\n<b>Moeda:</b> {signal_data['coin']}\n"
                    f"<b>Tipo:</b> {signal_data['order_type']}\n<b>Entrada:</b> {signal_data['entries'][0]}\n"
                    f"<b>Stop:</b> {signal_data['stop_loss']}\n<b>Alvo 1:</b> {signal_data['targets'][0]}\n\n"
                    f"O sinal passou nos seus filtros. Você aprova a entrada?"
                )
                sent_message = await application.bot.send_message(
                    chat_id=ADMIN_ID, text=signal_details, parse_mode='HTML',
                    reply_markup=signal_approval_keyboard(new_signal_for_approval.id)
                )
                new_signal_for_approval.approval_message_id = sent_message.message_id
                db.commit()
        else:
            logger.info(f"Sinal do tipo '{signal_type}' recebido e ignorado conforme a estratégia de autonomia.")
            
    finally:
        db.close()