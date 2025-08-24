import asyncio
import logging
from telegram.ext import Application
from sqlalchemy.orm import Session
from database.session import SessionLocal
from database.models import Trade, User, PendingSignal
from services.bybit_service import (
    get_market_price, close_partial_position,
    modify_position_stop_loss, get_order_status,
    get_specific_position_size, modify_position_take_profit,
    get_last_closed_trade_info
)
from services.notification_service import send_notification
from utils.security import decrypt_data
from sqlalchemy.sql import func

logger = logging.getLogger(__name__)


async def check_pending_orders_for_user(application: Application, user: User, db: Session):
    """Verifica as ordens limite pendentes e envia notificação detalhada na execução."""
    pending_orders = db.query(PendingSignal).filter_by(user_telegram_id=user.telegram_id).all()
    if not pending_orders:
        return

    logger.info(f"Rastreador: Verificando {len(pending_orders)} ordem(ns) pendente(s) para o usuário {user.telegram_id}.")
    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)

    for order in pending_orders:
        status_result = await get_order_status(api_key, api_secret, order.order_id, order.symbol)
        if not status_result.get("success"):
            logger.error(f"Falha ao obter status da ordem {order.order_id}: {status_result.get('error')}")
            continue

        order_data = status_result["data"] or {}
        order_status = (order_data.get("orderStatus") or "").strip()

        if order_status == 'Filled':
            logger.info(f"Ordem Limite {order.order_id} EXECUTADA para o usuário {user.telegram_id}.")
            signal_data = order.signal_data or {}
            
            qty = float(order_data.get('cumExecQty', 0.0))
            entry_price = float(order_data.get('avgPrice', 0.0))
            
            if qty <= 0 or entry_price <= 0:
                logger.warning(f"Ordem {order.order_id} Filled, mas com qty/preço zerado. Removendo.")
                db.delete(order)
                await application.bot.send_message(chat_id=user.telegram_id, text=f"ℹ️ Sua ordem limite para <b>{order.symbol}</b> foi finalizada sem execução reportada.", parse_mode='HTML')
                continue

            # --- LÓGICA DE NOTIFICAÇÃO DETALHADA ---
            side = signal_data.get('order_type')
            leverage = user.max_leverage
            margin = (qty * entry_price) / leverage if leverage > 0 else 0
            stop_loss = signal_data.get('stop_loss')
            all_targets = signal_data.get('targets') or []
            take_profit_1 = all_targets[0] if all_targets else "N/A"
            num_targets = len(all_targets)
            tp_text = f"${float(take_profit_1):,.4f}" if isinstance(take_profit_1, (int, float)) else take_profit_1
            if num_targets > 1:
                tp_text += f" (de {num_targets} alvos)"
            
            new_trade = Trade(
                user_telegram_id=order.user_telegram_id, order_id=order.order_id,
                symbol=order.symbol, side=side, qty=qty, entry_price=entry_price,
                stop_loss=stop_loss, current_stop_loss=stop_loss,
                initial_targets=all_targets, status='ACTIVE', remaining_qty=qty
            )
            db.add(new_trade)
            db.delete(order)
            
            message = (
                f"📈 <b>Ordem Limite Executada!</b>\n\n"
                f"  - 📊 <b>Tipo:</b> {side} | <b>Alavancagem:</b> {leverage}x\n"
                f"  - 💎 <b>Moeda:</b> {order.symbol}\n"
                f"  - 🔢 <b>Quantidade:</b> {qty:g}\n"
                f"  - 💵 <b>Preço de Entrada:</b> ${entry_price:,.4f}\n"
                f"  - 💰 <b>Margem:</b> ${margin:,.2f}\n"
                f"  - 🛡️ <b>Stop Loss:</b> ${stop_loss:,.4f}\n"
                f"  - 🎯 <b>Take Profit 1:</b> {tp_text}"
            )
            await application.bot.send_message(chat_id=user.telegram_id, text=message, parse_mode='HTML')

        elif order_status in {'Cancelled', 'Deactivated', 'Rejected'}:
            logger.info(f"Ordem Limite {order.order_id} do usuário {user.telegram_id} foi '{order_status}'. Removendo.")
            db.delete(order)
            await application.bot.send_message(chat_id=user.telegram_id, text=f"ℹ️ Sua ordem limite para <b>{order.symbol}</b> foi '<b>{order_status}</b>' e removida do monitoramento.", parse_mode='HTML')

async def check_active_trades_for_user(application: Application, user: User, db: Session):
    """Verifica e gerencia os trades ativos, incluindo lógica de TP parcial e Break-Even."""
    active_trades = db.query(Trade).filter(
        Trade.user_telegram_id == user.telegram_id,
        ~Trade.status.like('%CLOSED%')
    ).all()
    if not active_trades:
        return

    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)

    for trade in active_trades:
        live_position_size = await get_specific_position_size(api_key, api_secret, trade.symbol)

        if live_position_size > 0:
            # --- CENÁRIO 1: POSIÇÃO AINDA ABERTA NA CORRETORA ---
            price_result = await get_market_price(trade.symbol)
            if not price_result.get("success"):
                logger.warning(f"Não foi possível obter preço para {trade.symbol}. Pulando verificação.")
                continue
            
            current_price = price_result["price"]
            
            # --- LÓGICA DE TAKE PROFIT (PARCIAL) E BREAK-EVEN ---
            if trade.initial_targets:
                targets_hit = []
                # Verifica cada alvo pendente
                for target_price in trade.initial_targets:
                    is_target_hit = False
                    if trade.side == 'LONG' and current_price >= target_price:
                        is_target_hit = True
                    elif trade.side == 'SHORT' and current_price <= target_price:
                        is_target_hit = True

                    if is_target_hit:
                        logger.info(f"TRADE {trade.symbol}: Alvo de TP em ${target_price:.4f} atingido!")
                        
                        # Define a quantidade a ser fechada. Ex: 4 alvos, fecha 25% em cada.
                        num_remaining_targets = len(trade.initial_targets)
                        qty_to_close = trade.qty / (num_remaining_targets + len(targets_hit))
                        
                        close_result = await close_partial_position(api_key, api_secret, trade.symbol, qty_to_close, trade.side)
                        
                        if close_result.get("success"):
                            # Marca o alvo como atingido para ser removido do DB
                            targets_hit.append(target_price)
                            trade.remaining_qty -= qty_to_close

                            # Envia notificação ao usuário
                            message = (
                                f"🎯 <b>Take Profit Atingido!</b>\n\n"
                                f"<b>Moeda:</b> {trade.symbol}\n"
                                f"<b>Alvo:</b> ${target_price:.4f}\n"
                                f"Uma parte da sua posição foi fechada com lucro."
                            )
                            await application.bot.send_message(chat_id=user.telegram_id, text=message, parse_mode='HTML')
                        else:
                            logger.error(f"TRADE {trade.symbol}: Falha ao fechar posição parcial para o alvo ${target_price:.4f}. Erro: {close_result.get('error')}")

                # Remove os alvos que foram atingidos da lista do trade
                if targets_hit:
                    trade.initial_targets = [t for t in trade.initial_targets if t not in targets_hit]
                
                # --- LÓGICA DE BREAK-EVEN (EXECUTADA APÓS O PRIMEIRO TP) ---
                # Se algum alvo foi atingido nesta verificação E o stop ainda não foi movido
                if targets_hit and not trade.is_breakeven:
                    new_stop_loss = trade.entry_price
                    logger.info(f"TRADE {trade.symbol}: Primeiro TP atingido. Movendo Stop Loss para Break-Even em ${new_stop_loss:.4f}")
                    
                    sl_result = await modify_position_stop_loss(api_key, api_secret, trade.symbol, new_stop_loss)
                    
                    if sl_result.get("success"):
                        trade.is_breakeven = True
                        trade.current_stop_loss = new_stop_loss
                        
                        be_message = (
                            f"🛡️ <b>Stop Loss Ajustado (Break-Even)</b>\n\n"
                            f"A posição em <b>{trade.symbol}</b> atingiu o primeiro alvo.\n"
                            f"Seu Stop Loss foi movido para o preço de entrada (<b>${new_stop_loss:.4f}</b>) para proteger a operação contra perdas."
                        )
                        await application.bot.send_message(chat_id=user.telegram_id, text=be_message, parse_mode='HTML')
                    else:
                        logger.error(f"TRADE {trade.symbol}: Falha ao mover SL para Break-Even. Erro: {sl_result.get('error', 'desconhecido')}")
        else:
            # --- CENÁRIO 2: POSIÇÃO FECHADA - USANDO O DETETIVE ---
            logger.info(f"[tracker] Posição para {trade.symbol} não encontrada na corretora. Usando o detetive...")
            
            closed_info_result = await get_last_closed_trade_info(api_key, api_secret, trade.symbol)

            if closed_info_result.get("success"):
                closed_data = closed_info_result["data"]
                pnl = float(closed_data.get("closedPnl", 0.0))
                closing_reason = closed_data.get("exitType", "Unknown")

                trade.closed_at = func.now()
                trade.closed_pnl = pnl
                trade.remaining_qty = 0.0
                
                message_text = ""
                if closing_reason == "TakeProfit":
                    trade.status = 'CLOSED_PROFIT'
                    message_text = f"🏆 <b>Posição Fechada (Take Profit)!</b> 🏆\n<b>Moeda:</b> {trade.symbol}\n<b>Lucro Total Realizado:</b> ${pnl:,.2f}"
                elif closing_reason == "StopLoss":
                    if pnl >= 0:
                        trade.status = 'CLOSED_STOP_GAIN'
                        message_text = f"✅ <b>Posição Fechada (Stop com Ganho)!</b>\n<b>Moeda:</b> {trade.symbol}\n<b>Resultado:</b> ${pnl:,.2f}"
                    else:
                        trade.status = 'CLOSED_LOSS'
                        message_text = f"🛑 <b>Posição Fechada (Stop Loss)</b>\n<b>Moeda:</b> {trade.symbol}\n<b>Prejuízo Realizado:</b> ${pnl:,.2f}"
                else: # Outros motivos (Liquidação, Manual, etc.)
                    trade.status = 'CLOSED_GHOST'
                    message_text = f"ℹ️ Posição em <b>{trade.symbol}</b> foi fechada na corretora.\n<b>Resultado:</b> ${pnl:,.2f}"

                await application.bot.send_message(chat_id=user.telegram_id, text=message_text, parse_mode='HTML')
            else:
                # Fallback se o detetive falhar
                trade.status = 'CLOSED_GHOST'
                trade.closed_at = func.now()
                trade.closed_pnl = 0.0
                trade.remaining_qty = 0.0
                message_text = f"ℹ️ Posição em <b>{trade.symbol}</b> não foi encontrada na Bybit e foi removida do monitoramento."
                await application.bot.send_message(chat_id=user.telegram_id, text=message_text, parse_mode='HTML')
                
async def run_tracker(application: Application):
    """Função principal que roda o verificador em loop para TODOS os usuários."""
    logger.info("Iniciando Rastreador de Posições e Ordens (Modo Multiusuário)...")
    while True:
        db = SessionLocal()
        try:
            all_users = db.query(User).filter(User.api_key_encrypted.isnot(None)).all()
            if not all_users:
                logger.info("Rastreador: Nenhum usuário com API para verificar.")
            else:
                logger.info(f"Rastreador: Verificando assets para {len(all_users)} usuário(s).")
                for user in all_users:
                    await check_pending_orders_for_user(application, user, db)
                    await check_active_trades_for_user(application, user, db)

                # commit das modificações (Trades atualizados, remoção de Pending, etc.)
                db.commit()

        except Exception as e:
            logger.critical(f"Erro crítico no loop do rastreador: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()

        await asyncio.sleep(60)
