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
    """Verifica as ordens limite pendentes de UM usuário específico."""
    pending_orders = db.query(PendingSignal).filter_by(user_telegram_id=user.telegram_id).all()
    if not pending_orders:
        return

    logger.info(f"Rastreador: Verificando {len(pending_orders)} ordem(ns) pendente(s) para o usuário {user.telegram_id}.")
    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)

    for order in pending_orders:
        status_result = await get_order_status(api_key, api_secret, order.order_id, order.symbol)
        if not status_result.get("success"):
            logger.error(
                f"Falha ao obter status da ordem {order.order_id} "
                f"para o usuário {user.telegram_id}: {status_result.get('error')}"
            )
            continue

        order_data = status_result["data"] or {}
        order_status = (order_data.get("orderStatus") or "").strip()

        if order_status == 'Filled':
            logger.info(f"Ordem Limite {order.order_id} EXECUTADA para o usuário {user.telegram_id}.")
            signal_data = order.signal_data or {}

            # Fallbacks robustos
            avg_price = order_data.get('avgPrice')
            if avg_price:
                entry_price = float(avg_price)
            else:
                # usa limit_price salvo no signal_data (trade_manager já injeta) ou a 1ª entry
                entry_price = float(signal_data.get('limit_price') or signal_data.get('entries', [0])[0])

            cum_exec_qty = float(order_data.get('cumExecQty', 0.0))
            if cum_exec_qty <= 0:
                # Segurança: se por algum motivo a exchange marcou Filled mas qty veio 0,
                # tratamos como cancelado para não criar Trade inconsistente.
                logger.warning(
                    f"[tracker] Ordem {order.order_id} marcada como Filled, "
                    f"mas cumExecQty=0. Removendo ordem pendente sem criar Trade."
                )
                db.delete(order)
                await application.bot.send_message(
                    chat_id=user.telegram_id,
                    text=(
                        f"ℹ️ Sua ordem limite para <b>{order.symbol}</b> foi finalizada na corretora, "
                        f"mas sem execução reportada. Removida do monitoramento."
                    ),
                    parse_mode='HTML'
                )
                continue

            new_trade = Trade(
                user_telegram_id=order.user_telegram_id,
                order_id=order.order_id,
                symbol=signal_data.get('coin', order.symbol),
                side=signal_data.get('order_type'),
                qty=cum_exec_qty,
                entry_price=entry_price,
                stop_loss=signal_data.get('stop_loss'),
                current_stop_loss=signal_data.get('stop_loss'),
                initial_targets=signal_data.get('targets') or [],
                status='ACTIVE',
                remaining_qty=cum_exec_qty
            )
            db.add(new_trade)
            db.delete(order)
            await application.bot.send_message(
                chat_id=user.telegram_id,
                text=(
                    f"📈 <b>Ordem Limite Executada!</b>\n"
                    f"<b>Moeda:</b> {order.symbol}\n"
                    f"<b>Preço médio:</b> {entry_price}"
                ),
                parse_mode='HTML'
            )

        elif order_status in {'Cancelled', 'Deactivated', 'Rejected'}:
            logger.info(f"Ordem Limite {order.order_id} do usuário {user.telegram_id} foi '{order_status}'. Removendo.")
            db.delete(order)
            await application.bot.send_message(
                chat_id=user.telegram_id,
                text=(
                    f"ℹ️ Sua ordem limite para <b>{order.symbol}</b> foi "
                    f"'<b>{order_status}</b>' pela corretora e removida do monitoramento."
                ),
                parse_mode='HTML'
            )
        else:
            # Estados como 'New', 'PartiallyFilled', etc.: apenas seguir monitorando.
            logger.debug(
                f"[tracker] Ordem {order.order_id} estado='{order_status}' para {user.telegram_id}. "
                f"Seguir monitorando."
            )

async def check_active_trades_for_user(application: Application, user: User, db: Session):
    """Verifica e gerencia os trades ativos, salvando o P/L no fechamento."""
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

        # 1. VERIFICA TAKE PROFIT
        if trade.initial_targets:
            next_target_price = trade.initial_targets[0]
            if (trade.side == 'LONG' and current_price >= next_target_price) or \
               (trade.side == 'SHORT' and current_price <= next_target_price):
                
                live_position_size = await get_specific_position_size(api_key, api_secret, trade.symbol)
                if live_position_size <= 0:
                    continue

                qty_to_close = live_position_size if len(trade.initial_targets) == 1 else (live_position_size / 2.0)
                close_result = await close_partial_position(api_key, api_secret, trade.symbol, qty_to_close, trade.side)

                if close_result.get("success") and not close_result.get("skipped"):
                    profit = abs(next_target_price - trade.entry_price) * qty_to_close
                    trade.remaining_qty -= qty_to_close
                    remaining_targets = trade.initial_targets[1:]
                    trade.initial_targets = remaining_targets
                    
                    message_text = ""
                    if not remaining_targets or trade.remaining_qty < 0.00001:
                        trade.status = 'CLOSED_PROFIT'
                        trade.closed_at = func.now()
                        trade.closed_pnl = profit
                        message_text = f"🏆 <b>Último Alvo Atingido! (LUCRO)</b> 🏆\n<b>Moeda:</b> {trade.symbol}\n<b>Lucro Realizado:</b> ${profit:,.2f}"
                    else:
                        trade.status = 'ACTIVE_TP_HIT'
                        next_tp_price = remaining_targets[0]
                        tp_update_result = await modify_position_take_profit(api_key, api_secret, trade.symbol, next_tp_price)
                        if tp_update_result.get("success"):
                            logger.info(f"Take Profit para {trade.symbol} atualizado para o próximo alvo: {next_tp_price}")
                        else:
                            logger.error(f"Falha ao atualizar Take Profit para {trade.symbol}: {tp_update_result.get('error')}")
                        
                        message_text = (
                            f"💰 <b>Take Profit Atingido! (LUCRO)</b>\n"
                            f"<b>Moeda:</b> {trade.symbol}\n"
                            f"<b>Lucro Parcial:</b> ${profit:,.2f}\n"
                            f"<b>Próximo Alvo:</b> ${next_tp_price:,.4f}"
                        )
                    
                    await application.bot.send_message(chat_id=user.telegram_id, text=message_text, parse_mode='HTML')
                    continue
        
        # 2. VERIFICA STOP LOSS
        stop_hit = (
            (trade.side == 'LONG' and current_price <= trade.current_stop_loss) or
            (trade.side == 'SHORT' and current_price >= trade.current_stop_loss)
        )
        if stop_hit:
            live_position_size = await get_specific_position_size(api_key, api_secret, trade.symbol)
            if live_position_size <= 0:
                continue

            loss = abs(trade.current_stop_loss - trade.entry_price) * live_position_size
            trade.status = 'CLOSED_LOSS'
            trade.closed_at = func.now()
            trade.closed_pnl = -loss
            trade.remaining_qty = 0.0
            
            message_text = f"🛑 <b>Stop Loss Atingido (PREJUÍZO)</b>\n<b>Moeda:</b> {trade.symbol}\n<b>Prejuízo Realizado:</b> ${-loss:,.2f}"
            await application.bot.send_message(chat_id=user.telegram_id, text=message_text, parse_mode='HTML')
            continue

        # 3. VERIFICA POSIÇÃO FANTASMA
        live_position_size = await get_specific_position_size(api_key, api_secret, trade.symbol)
        if live_position_size <= 0:
            logger.info(f"[tracker] Posição fantasma para {trade.symbol} detectada e limpa.")
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
