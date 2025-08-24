import asyncio
import logging
from telegram.ext import Application
from sqlalchemy.orm import Session
from database.session import SessionLocal
from database.models import Trade, User, PendingSignal
from services.bybit_service import (
    get_market_price, close_partial_position,
    modify_position_stop_loss, get_order_status,
    get_specific_position_size, modify_position_take_profit
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
    """Verifica e gerencia os trades ativos, com lógica de Stop Gain e P/L."""
    active_trades = db.query(Trade).filter(
        Trade.user_telegram_id == user.telegram_id,
        ~Trade.status.like('%CLOSED%')
    ).all()
    if not active_trades:
        return

    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)

    for trade in active_trades:
        price_result = await get_market_price(trade.symbol)
        if not price_result.get("success"):
            continue
        current_price = price_result["price"]

        live_position_size = await get_specific_position_size(api_key, api_secret, trade.symbol)

        # CENÁRIO 1: A POSIÇÃO AINDA ESTÁ ABERTA NA CORRETORA
        if live_position_size > 0:
            # 1a. VERIFICA TAKE PROFIT
            if trade.initial_targets:
                next_target_price = trade.initial_targets[0]
                if (trade.side == 'LONG' and current_price >= next_target_price) or \
                   (trade.side == 'SHORT' and current_price <= next_target_price):
                    
                    qty_to_close = live_position_size if len(trade.initial_targets) == 1 else (live_position_size / 2.0)
                    close_result = await close_partial_position(api_key, api_secret, trade.symbol, qty_to_close, trade.side)

                    if close_result.get("success") and not close_result.get("skipped"):
                        profit = abs(next_target_price - trade.entry_price) * qty_to_close
                        trade.remaining_qty -= qty_to_close
                        remaining_targets = trade.initial_targets[1:]
                        trade.initial_targets = remaining_targets
                        
                        message_text = ""
                        if not remaining_targets or trade.remaining_qty < 0.00001:
                            trade.status = 'CLOSED_PROFIT'; trade.closed_at = func.now(); trade.closed_pnl = profit
                            message_text = f"🏆 <b>Último Alvo Atingido! (LUCRO)</b> 🏆\n<b>Moeda:</b> {trade.symbol}\n<b>Lucro Realizado:</b> ${profit:,.2f}"
                        else:
                            trade.status = 'ACTIVE_TP_HIT'
                            next_tp_price = remaining_targets[0]
                            await modify_position_take_profit(api_key, api_secret, trade.symbol, next_tp_price)
                            
                            # --- LÓGICA DE STOP GAIN (MOVE TO BREAK-EVEN) ---
                            # Se o stop ainda estiver no valor original, move para a entrada
                            if trade.current_stop_loss == trade.stop_loss:
                                await modify_position_stop_loss(api_key, api_secret, trade.symbol, trade.entry_price)
                                trade.current_stop_loss = trade.entry_price
                                logger.info(f"Stop Loss para {trade.symbol} movido para o preço de entrada (Break-Even): {trade.entry_price}")
                            
                            message_text = (
                                f"💰 <b>Take Profit Atingido! (LUCRO)</b>\n"
                                f"<b>Moeda:</b> {trade.symbol}\n"
                                f"<b>Lucro Parcial:</b> ${profit:,.2f}\n"
                                f"<b>Próximo Alvo:</b> ${next_tp_price:,.4f}\n"
                                f"🛡️ <i>Stop Loss movido para a entrada.</i>"
                            )
                        
                        await application.bot.send_message(chat_id=user.telegram_id, text=message_text, parse_mode='HTML')
                        continue

            # 1b. VERIFICA STOP LOSS
            stop_hit = (
                (trade.side == 'LONG' and current_price <= trade.current_stop_loss) or
                (trade.side == 'SHORT' and current_price >= trade.current_stop_loss)
            )
            if stop_hit:
                pnl = (trade.current_stop_loss - trade.entry_price) * live_position_size if trade.side == 'LONG' else (trade.entry_price - trade.current_stop_loss) * live_position_size
                
                if pnl >= 0:
                    trade.status = 'CLOSED_STOP_GAIN'
                    message_text = f"✅ <b>Stop com Ganho Atingido!</b>\n<b>Moeda:</b> {trade.symbol}\n<b>Resultado:</b> ${pnl:,.2f}"
                else:
                    trade.status = 'CLOSED_LOSS'
                    message_text = f"🛑 <b>Stop Loss Atingido (PREJUÍZO)</b>\n<b>Moeda:</b> {trade.symbol}\n<b>Prejuízo Realizado:</b> ${pnl:,.2f}"

                trade.closed_at = func.now(); trade.closed_pnl = pnl
                trade.remaining_qty = 0.0
                await application.bot.send_message(chat_id=user.telegram_id, text=message_text, parse_mode='HTML')
                continue

        # CENÁRIO 2: A POSIÇÃO NÃO FOI ENCONTRADA NA CORRETORA (JÁ FECHOU)
        else:
            logger.info(f"[tracker] Posição para {trade.symbol} não encontrada na Bybit. Investigando motivo...")
            
            # 2a. INVESTIGA SE FOI UM TAKE PROFIT
            if trade.initial_targets:
                next_target_price = trade.initial_targets[0]
                if (trade.side == 'LONG' and current_price >= next_target_price) or \
                   (trade.side == 'SHORT' and current_price <= next_target_price):
                    
                    profit = abs(next_target_price - trade.entry_price) * trade.qty
                    trade.status = 'CLOSED_PROFIT'; trade.closed_at = func.now(); trade.closed_pnl = profit
                    trade.remaining_qty = 0.0
                    message_text = f"🏆 <b>Alvo Final Atingido! (LUCRO)</b> 🏆\n<b>Moeda:</b> {trade.symbol}\n<b>Lucro Realizado:</b> ${profit:,.2f}\n<i>(Posição fechada pela corretora)</i>"
                    await application.bot.send_message(chat_id=user.telegram_id, text=message_text, parse_mode='HTML')
                    continue

            # 2b. INVESTIGA SE FOI UM STOP LOSS
            if (trade.side == 'LONG' and current_price <= trade.current_stop_loss) or \
               (trade.side == 'SHORT' and current_price >= trade.current_stop_loss):
               
                pnl = (trade.current_stop_loss - trade.entry_price) * trade.qty if trade.side == 'LONG' else (trade.entry_price - trade.current_stop_loss) * trade.qty
                if pnl >= 0:
                    trade.status = 'CLOSED_STOP_GAIN'
                    message_text = f"✅ <b>Stop com Ganho Atingido!</b>\n<b>Moeda:</b> {trade.symbol}\n<b>Resultado:</b> ${pnl:,.2f}\n<i>(Posição fechada pela corretora)</i>"
                else:
                    trade.status = 'CLOSED_LOSS'
                    message_text = f"🛑 <b>Stop Loss Atingido (PREJUÍZO)</b>\n<b>Moeda:</b> {trade.symbol}\n<b>Prejuízo Realizado:</b> ${pnl:,.2f}\n<i>(Posição fechada pela corretora)</i>"
                
                trade.closed_at = func.now(); trade.closed_pnl = pnl
                trade.remaining_qty = 0.0
                await application.bot.send_message(chat_id=user.telegram_id, text=message_text, parse_mode='HTML')
                continue

            # 2c. SE NÃO FOI NENHUM DOS DOIS, É UM FECHAMENTO EXTERNO/FANTASMA
            trade.status = 'CLOSED_GHOST'; trade.closed_at = func.now(); trade.closed_pnl = 0.0
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
