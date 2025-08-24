import asyncio
import logging
import time
from telegram.ext import Application
from sqlalchemy.orm import Session
from database.session import SessionLocal
from database.models import Trade, User, PendingSignal
from services.bybit_service import (
    get_market_price, close_partial_position,
    modify_position_stop_loss, get_order_status,
    get_specific_position_size, modify_position_take_profit,
    get_last_closed_trade_info, get_open_positions_with_pnl
)
from services.notification_service import send_notification
from utils.security import decrypt_data
from sqlalchemy.sql import func

logger = logging.getLogger(__name__)


async def check_pending_orders_for_user(application: Application, user: User, db: Session):
    """Verifica as ordens limite pendentes e envia notificação detalhada na execução."""
    # (Esta função permanece inalterada)
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
    """Verifica e gerencia os trades ativos, com logs detalhados e lógica de stop aprimorada."""
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
            price_result = await get_market_price(trade.symbol)
            if not price_result.get("success"): continue
            current_price = price_result["price"]
            
            # Lógica de Take Profit (inalterada)
            targets_hit_this_run = []
            if trade.initial_targets:
                for target_price in trade.initial_targets:
                    is_target_hit = False
                    if trade.side == 'LONG' and current_price >= target_price: is_target_hit = True
                    elif trade.side == 'SHORT' and current_price <= target_price: is_target_hit = True
                    
                    if is_target_hit:
                        logger.info(f"TRADE {trade.symbol}: Alvo de TP em ${target_price:.4f} atingido!")
                        num_remaining_targets = len(trade.initial_targets)
                        num_already_hit = len(targets_hit_this_run)
                        qty_to_close = trade.qty / (num_remaining_targets + num_already_hit)
                        
                        close_result = await close_partial_position(api_key, api_secret, trade.symbol, qty_to_close, trade.side)
                        if close_result.get("success"):
                            targets_hit_this_run.append(target_price)
                            trade.remaining_qty -= qty_to_close
                            msg = f"🎯 <b>Take Profit Atingido!</b>\n\n<b>Moeda:</b> {trade.symbol}\n<b>Alvo:</b> ${target_price:.4f}\nUma parte da sua posição foi fechada com lucro."
                            await application.bot.send_message(chat_id=user.telegram_id, text=msg, parse_mode='HTML')
                        else:
                            logger.error(f"TRADE {trade.symbol}: Falha ao fechar posição parcial para o alvo ${target_price:.4f}. Erro: {close_result.get('error')}")

            if targets_hit_this_run:
                trade.initial_targets = [t for t in trade.initial_targets if t not in targets_hit_this_run]

            # Lógica de Estratégia de Stop
            if user.stop_strategy == 'BREAK_EVEN':
                if targets_hit_this_run and not trade.is_breakeven:
                    new_stop_loss = trade.entry_price
                    # ... (lógica de break-even inalterada) ...
                    sl_result = await modify_position_stop_loss(api_key, api_secret, trade.symbol, new_stop_loss)
                    if sl_result.get("success"):
                        trade.is_breakeven = True
                        trade.current_stop_loss = new_stop_loss
                        msg = f"🛡️ <b>Stop Loss Ajustado (Break-Even)</b>\n\nSua posição em <b>{trade.symbol}</b> foi protegida..."
                        await application.bot.send_message(chat_id=user.telegram_id, text=msg, parse_mode='HTML')
                    else:
                        logger.error(f"TRADE {trade.symbol}: Falha ao mover SL para Break-Even. Erro: {sl_result.get('error', 'desconhecido')}")
            
            elif user.stop_strategy == 'TRAILING_STOP':
                log_prefix = f"[Trailing Stop {trade.symbol}]"

                if trade.trail_high_water_mark is None:
                    trade.trail_high_water_mark = trade.entry_price
                
                new_hwm = trade.trail_high_water_mark
                if trade.side == 'LONG' and current_price > new_hwm: new_hwm = current_price
                elif trade.side == 'SHORT' and current_price < new_hwm: new_hwm = current_price
                
                if new_hwm != trade.trail_high_water_mark:
                    logger.info(f"{log_prefix} Novo pico de preço: ${new_hwm:.4f}")
                    trade.trail_high_water_mark = new_hwm
                
                trail_distance = 0
                if trade.stop_loss is not None: trail_distance = abs(trade.entry_price - trade.stop_loss)
                else: trail_distance = trade.entry_price * 0.02
                
                potential_new_sl = 0.0
                if trade.side == 'LONG': potential_new_sl = trade.trail_high_water_mark - trail_distance
                else: potential_new_sl = trade.trail_high_water_mark + trail_distance

                is_improvement = False
                if trade.side == 'LONG' and potential_new_sl > trade.current_stop_loss: is_improvement = True
                elif trade.side == 'SHORT' and potential_new_sl < trade.current_stop_loss: is_improvement = True
                
                logger.info(
                    f"{log_prefix} Preço Atual: ${current_price:.4f} | Stop Atual: ${trade.current_stop_loss:.4f} | "
                    f"Pico: ${trade.trail_high_water_mark:.4f} | Distância: ${trail_distance:.4f} | "
                    f"Novo SL Potencial: ${potential_new_sl:.4f} | É Melhoria?: {is_improvement}"
                )
                
                if is_improvement:
                    # --- INÍCIO DA NOVA VERIFICAÇÃO DE SEGURANÇA ---
                    is_valid_to_set = True
                    if trade.side == 'LONG' and potential_new_sl >= current_price:
                        is_valid_to_set = False
                        logger.warning(
                            f"{log_prefix} O preço atual (${current_price:.4f}) já ultrapassou o novo SL ({potential_new_sl:.4f}). "
                            f"A posição deveria ter sido parada. Ignorando o ajuste para evitar erro na API."
                        )
                    elif trade.side == 'SHORT' and potential_new_sl <= current_price:
                        is_valid_to_set = False
                        logger.warning(
                            f"{log_prefix} O preço atual (${current_price:.4f}) já ultrapassou o novo SL ({potential_new_sl:.4f}). "
                            f"A posição deveria ter sido parada. Ignorando o ajuste para evitar erro na API."
                        )
                    # --- FIM DA NOVA VERIFICAÇÃO DE SEGURANÇA ---

                    if is_valid_to_set:
                        logger.info(f"{log_prefix} MELHORIA DETECTADA! Movendo Stop Loss para ${potential_new_sl:.4f}")
                        sl_result = await modify_position_stop_loss(api_key, api_secret, trade.symbol, potential_new_sl)
                        if sl_result.get("success"):
                            trade.current_stop_loss = potential_new_sl
                            msg = f"📈 <b>Trailing Stop Ajustado</b>\n\nO Stop Loss de <b>{trade.symbol}</b> foi atualizado para <b>${potential_new_sl:.4f}</b> para proteger seus lucros."
                            await application.bot.send_message(chat_id=user.telegram_id, text=msg, parse_mode='HTML')
                        else:
                            logger.error(f"{log_prefix} Falha ao mover Trailing SL. Erro: {sl_result.get('error', 'desconhecido')}")
        else:
            # Lógica "detetive" (inalterada)
            logger.info(f"[tracker] Posição para {trade.symbol} não encontrada. Usando o detetive...")
            # ... (código do detetive permanece o mesmo) ...
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
                    message_text = f"🏆 <b>Take Profit Atingido!</b> 🏆\n<b>Moeda:</b> {trade.symbol}\n<b>Lucro Realizado:</b> ${pnl:,.2f}"
                elif closing_reason == "StopLoss":
                    if pnl >= 0:
                        trade.status = 'CLOSED_STOP_GAIN'
                        message_text = f"✅ <b>Stop com Ganho Atingido!</b>\n<b>Moeda:</b> {trade.symbol}\n<b>Resultado:</b> ${pnl:,.2f}"
                    else:
                        trade.status = 'CLOSED_LOSS'
                        message_text = f"🛑 <b>Stop Loss Atingido</b>\n<b>Moeda:</b> {trade.symbol}\n<b>Prejuízo Realizado:</b> ${pnl:,.2f}"
                else: 
                    trade.status = 'CLOSED_GHOST'
                    message_text = f"ℹ️ Posição em <b>{trade.symbol}</b> foi fechada na corretora.\n<b>Resultado:</b> ${pnl:,.2f}"
                await application.bot.send_message(chat_id=user.telegram_id, text=message_text, parse_mode='HTML')
            else:
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
            # --- LÓGICA DE SINCRONIZAÇÃO APRIMORADA ---
            all_api_users_for_sync = db.query(User).filter(User.api_key_encrypted.isnot(None)).all()
            for user in all_api_users_for_sync:
                sync_api_key = decrypt_data(user.api_key_encrypted)
                sync_api_secret = decrypt_data(user.api_secret_encrypted)
                
                bybit_positions_result = await get_open_positions_with_pnl(sync_api_key, sync_api_secret)
                if not bybit_positions_result.get("success"):
                    logger.error(f"Sincronização: Falha ao buscar posições da Bybit para o usuário {user.telegram_id}. Pulando.")
                    continue
                
                bybit_positions = {pos['symbol']: pos for pos in bybit_positions_result.get('data', [])}
                
                # --- INÍCIO DA LÓGICA CORRIGIDA ---
                # 1. Busca trades ativos no banco de dados
                db_active_trades = db.query(Trade).filter(Trade.user_telegram_id == user.telegram_id, ~Trade.status.like('%CLOSED%')).all()
                db_active_symbols = {trade.symbol for trade in db_active_trades}

                # 2. Busca ordens PENDENTES no banco de dados
                db_pending_signals = db.query(PendingSignal).filter(PendingSignal.user_telegram_id == user.telegram_id).all()
                db_pending_symbols = {signal.symbol for signal in db_pending_signals}

                # 3. Une os dois conjuntos para ter uma visão completa dos símbolos conhecidos pelo bot
                all_known_symbols = db_active_symbols.union(db_pending_symbols)
                
                # 4. Compara e identifica posições órfãs (apenas as que não estão em nenhuma das listas)
                symbols_to_add = set(bybit_positions.keys()) - all_known_symbols
                # --- FIM DA LÓGICA CORRIGIDA ---
                
                if symbols_to_add:
                    logger.warning(f"Sincronização: {len(symbols_to_add)} posições órfãs encontradas para o usuário {user.telegram_id}. Adotando-as.")
                    for symbol in symbols_to_add:
                        pos_data = bybit_positions[symbol]
                        
                        new_trade = Trade(
                            user_telegram_id=user.telegram_id,
                            order_id=f"sync_{symbol}_{int(time.time())}",
                            symbol=symbol,
                            side=pos_data['side'],
                            qty=pos_data['size'],
                            remaining_qty=pos_data['size'],
                            entry_price=pos_data['entry'],
                            status='ACTIVE_SYNCED',
                            stop_loss=None,
                            initial_targets=[],
                            current_stop_loss=pos_data.get('stop_loss', 0.0)
                        )
                        db.add(new_trade)
                        
                        message = (
                            f"⚠️ <b>Posição Sincronizada</b> ⚠️\n\n"
                            f"Uma posição em <b>{symbol}</b> foi encontrada aberta na Bybit sem um registro local e foi adicionada ao monitoramento.\n\n"
                            f"<b>Atenção:</b> O bot não conhece os alvos ou o stop loss originais do sinal. O gerenciamento será feito com base na sua estratégia de stop loss configurada."
                        )
                        await application.bot.send_message(chat_id=user.telegram_id, text=message, parse_mode='HTML')
            
            db.commit()

            # Lógica principal de verificação (inalterada)
            all_users = db.query(User).filter(User.api_key_encrypted.isnot(None)).all()
            if not all_users:
                logger.info("Rastreador: Nenhum usuário com API para verificar.")
            else:
                logger.info(f"Rastreador: Verificando assets para {len(all_users)} usuário(s).")
                for user in all_users:
                    await check_pending_orders_for_user(application, user, db)
                    await check_active_trades_for_user(application, user, db)
                
                db.commit()

        except Exception as e:
            logger.critical(f"Erro crítico no loop do rastreador: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()

        await asyncio.sleep(60)
