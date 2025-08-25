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
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

def _generate_trade_status_message(trade: Trade, status_title: str, pnl_data: dict = None, current_price: float = None) -> str:
    """Gera o texto completo e atualizado para a mensagem de status de um trade no formato Dashboard."""
    side_emoji = "⬆️" if trade.side == "LONG" else "⬇️"
    
    # --- Cabeçalho ---
    message = f"{side_emoji} <b>{status_title}: {trade.side}</b>\n\n"
    message += f"💎 <b>MOEDA:</b> {trade.symbol}\n\n"

    # --- Seção de P/L e Margem ---
    if pnl_data:
        pnl = pnl_data.get("unrealized_pnl", 0.0)
        pnl_pct = pnl_data.get("unrealized_pnl_pct", 0.0)
        margin = (trade.entry_price * trade.qty) / 10 # Assumindo alavancagem de 10x para margem
        message += f"📈 <b>P/L Atual:</b> ${pnl:+.2f} ({pnl_pct:+.2f}%)\n"
        message += f"💰 <b>Margem:</b> ${margin:,.2f}\n"
    
    message += " - - - - - - - - - - - - - - - - \n"
    
    # --- Seção da Posição ---
    message += f"➡️ <b>Entrada:</b> ${trade.entry_price:,.4f}\n"
    if current_price:
        message += f"📊 <b>Preço Atual:</b> ${current_price:,.4f}\n"
    message += f"📦 <b>Qtd. Restante:</b> {trade.remaining_qty:g}\n"
    
    message += " - - - - - - - - - - - - - - - - \n"

    # --- Seção de Risco ---
    if trade.initial_targets and trade.total_initial_targets:
        targets_hit = trade.total_initial_targets - len(trade.initial_targets)
        next_target_num = targets_hit + 1
        next_target_price = trade.initial_targets[0]
        message += f"🎯 <b>Próximo Alvo (TP{next_target_num}):</b> ${next_target_price:,.4f}\n"
    
    sl_note = ""
    if trade.is_breakeven:
        sl_note = " (Break-Even)"
    
    message += f"🛡️ <b>Stop Loss:</b> ${trade.current_stop_loss:,.4f}{sl_note}\n"
    
    message += " - - - - - - - - - - - - - - - - \n"

    # --- Seção de Progresso ---
    if trade.total_initial_targets:
        targets_hit = trade.total_initial_targets - len(trade.initial_targets)
        message += f"📊 <b>Alvos Atingidos:</b> {targets_hit} de {trade.total_initial_targets}\n"

    return message

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
            # 1. ENVIAMOS A MENSAGEM E CAPTURAMOS O OBJETO 'sent_message'
            sent_message = await application.bot.send_message(chat_id=user.telegram_id, text=message, parse_mode='HTML')

            # 2. CRIAMOS O TRADE E JÁ INCLUÍMOS O ID DA MENSAGEM
            new_trade = Trade(
                user_telegram_id=order.user_telegram_id, order_id=order.order_id,
                notification_message_id=sent_message.message_id, # <-- MUDANÇA AQUI
                symbol=order.symbol, side=side, qty=qty, entry_price=entry_price,
                stop_loss=stop_loss, current_stop_loss=stop_loss,
                initial_targets=all_targets,
                total_initial_targets=num_targets,
                status='ACTIVE', remaining_qty=qty
            )
            db.add(new_trade)
            db.delete(order)

async def check_active_trades_for_user(application: Application, user: User, db: Session):
    """Verifica e gerencia os trades ativos, com edição de mensagem para atualizações."""
    active_trades = db.query(Trade).filter(
        Trade.user_telegram_id == user.telegram_id,
        ~Trade.status.like('%CLOSED%')
    ).all()
    if not active_trades:
        return

    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)
    
    # Busca P/L de todas as posições do usuário de uma vez para otimizar
    live_pnl_result = await get_open_positions_with_pnl(api_key, api_secret)
    live_pnl_map = {p['symbol']: p for p in live_pnl_result.get('data', [])} if live_pnl_result.get("success") else {}

    for trade in active_trades:
        # Usamos o P/L já buscado em vez de uma nova chamada de API
        position_data = live_pnl_map.get(trade.symbol)
        live_position_size = position_data['size'] if position_data else 0.0
        
        # Correção para o bug de sincronização: se a busca de P/L falhou, não podemos ter certeza.
        if not live_pnl_result.get("success"):
             logger.warning(f"[tracker] Falha temporária ao buscar P/L para {user.telegram_id}. Ignorando ciclo de verificação de trades.")
             return # Retorna para evitar fechar posições por engano

        message_was_edited = False
        status_title_update = ""

        if live_position_size > 0:
            price_result = await get_market_price(trade.symbol)
            if not price_result.get("success"): continue
            current_price = price_result["price"]
            
            # Lógica de Take Profit
            targets_hit_this_run = []
            if trade.initial_targets:
                for target_price in trade.initial_targets:
                    is_target_hit = (trade.side == 'LONG' and current_price >= target_price) or \
                                    (trade.side == 'SHORT' and current_price <= target_price)
                    
                    if is_target_hit:
                        logger.info(f"TRADE {trade.symbol}: Alvo de TP em ${target_price:.4f} atingido!")
                        # O cálculo da quantidade a fechar deve ser sobre o total original
                        num_original_targets = len(trade.initial_targets) + len(targets_hit_this_run)
                        qty_to_close = trade.qty / num_original_targets
                        
                        close_result = await close_partial_position(api_key, api_secret, trade.symbol, qty_to_close, trade.side)
                        if close_result.get("success"):
                            targets_hit_this_run.append(target_price)
                            trade.remaining_qty -= qty_to_close
                        else:
                            logger.error(f"TRADE {trade.symbol}: Falha ao fechar posição parcial para o alvo ${target_price:.4f}. Erro: {close_result.get('error')}")

            if targets_hit_this_run:
                trade.initial_targets = [t for t in trade.initial_targets if t not in targets_hit_this_run]
                message_was_edited = True
                status_title_update = "🎯 Take Profit Atingido!"

            # Lógica de Estratégia de Stop
            # (O código interno do break-even e trailing stop não muda, apenas a notificação no final)
            if user.stop_strategy == 'BREAK_EVEN':
                if targets_hit_this_run and not trade.is_breakeven:
                    new_stop_loss = trade.entry_price
                    sl_result = await modify_position_stop_loss(api_key, api_secret, trade.symbol, new_stop_loss)
                    if sl_result.get("success"):
                        trade.is_breakeven = True
                        trade.current_stop_loss = new_stop_loss
                        message_was_edited = True
                        status_title_update = "🛡️ Stop Movido (Break-Even)"
                    else:
                        logger.error(f"TRADE {trade.symbol}: Falha ao mover SL para Break-Even. Erro: {sl_result.get('error', 'desconhecido')}")
            
            elif user.stop_strategy == 'TRAILING_STOP':
                # ... (toda a lógica de cálculo do trailing stop permanece aqui, como antes)
                log_prefix = f"[Trailing Stop {trade.symbol}]"
                if trade.trail_high_water_mark is None: trade.trail_high_water_mark = trade.entry_price
                new_hwm = trade.trail_high_water_mark
                if trade.side == 'LONG' and current_price > new_hwm: new_hwm = current_price
                elif trade.side == 'SHORT' and current_price < new_hwm: new_hwm = current_price
                if new_hwm != trade.trail_high_water_mark:
                    logger.info(f"{log_prefix} Novo pico de preço: ${new_hwm:.4f}")
                    trade.trail_high_water_mark = new_hwm
                trail_distance = abs(trade.entry_price - trade.stop_loss) if trade.stop_loss is not None else trade.entry_price * 0.02
                potential_new_sl = trade.trail_high_water_mark - trail_distance if trade.side == 'LONG' else trade.trail_high_water_mark + trail_distance
                is_improvement = (trade.side == 'LONG' and potential_new_sl > trade.current_stop_loss) or \
                                 (trade.side == 'SHORT' and potential_new_sl < trade.current_stop_loss)
                
                if is_improvement:
                    is_valid_to_set = (trade.side == 'LONG' and potential_new_sl < current_price) or \
                                      (trade.side == 'SHORT' and potential_new_sl > current_price)
                    if is_valid_to_set:
                        sl_result = await modify_position_stop_loss(api_key, api_secret, trade.symbol, potential_new_sl)
                        if sl_result.get("success"):
                            trade.current_stop_loss = potential_new_sl
                            message_was_edited = True
                            status_title_update = "📈 Trailing Stop Ajustado"
                        else:
                            logger.error(f"{log_prefix} Falha ao mover Trailing SL. Erro: {sl_result.get('error', 'desconhecido')}")

            # --- LÓGICA DE EDIÇÃO DE MENSAGEM CENTRALIZADA ---
            if message_was_edited and trade.notification_message_id:
                try:
                    pnl_data = live_pnl_map.get(trade.symbol)
                    msg_text = _generate_trade_status_message(trade, status_title_update, pnl_data)
                    await application.bot.edit_message_text(
                        chat_id=user.telegram_id,
                        message_id=trade.notification_message_id,
                        text=msg_text,
                        parse_mode='HTML'
                    )
                except BadRequest as e:
                    logger.warning(f"Falha ao editar mensagem {trade.notification_message_id} para o trade {trade.symbol}: {e}")

        else:
            # --- Lógica "Detetive" para Posições Fechadas ---
            logger.info(f"[tracker] Posição para {trade.symbol} não encontrada. Usando o detetive...")
            closed_info_result = await get_last_closed_trade_info(api_key, api_secret, trade.symbol)
            final_message = ""
            
            if closed_info_result.get("success"):
                closed_data = closed_info_result["data"]
                pnl = float(closed_data.get("closedPnl", 0.0))
                closing_reason = closed_data.get("exitType", "Unknown")
                trade.closed_at = func.now()
                trade.closed_pnl = pnl
                trade.remaining_qty = 0.0
                
                if closing_reason == "TakeProfit":
                    trade.status = 'CLOSED_PROFIT'
                    final_message = f"🏆 <b>Posição Fechada (LUCRO)</b> 🏆\n<b>Moeda:</b> {trade.symbol}\n<b>Resultado Final:</b> ${pnl:,.2f}"
                elif closing_reason == "StopLoss":
                    trade.status = 'CLOSED_LOSS' if pnl < 0 else 'CLOSED_STOP_GAIN'
                    emoji = "🛑" if pnl < 0 else "✅"
                    final_message = f"{emoji} <b>Posição Fechada (STOP)</b>\n<b>Moeda:</b> {trade.symbol}\n<b>Resultado Final:</b> ${pnl:,.2f}"
                else: 
                    trade.status = 'CLOSED_GHOST'
                    final_message = f"ℹ️ Posição em <b>{trade.symbol}</b> foi fechada na corretora.\n<b>Resultado:</b> ${pnl:,.2f}"
            else:
                trade.status = 'CLOSED_GHOST'; trade.closed_at = func.now(); trade.closed_pnl = 0.0
                trade.remaining_qty = 0.0
                final_message = f"ℹ️ Posição em <b>{trade.symbol}</b> não foi encontrada na Bybit e foi removida do monitoramento."

            # Edita a mensagem uma última vez com o resultado final
            if trade.notification_message_id:
                try:
                    await application.bot.edit_message_text(
                        chat_id=user.telegram_id,
                        message_id=trade.notification_message_id,
                        text=final_message,
                        parse_mode='HTML'
                    )
                except BadRequest as e:
                    logger.warning(f"Não foi possível editar mensagem final para o trade {trade.symbol} (pode ter sido removida): {e}")
                    await application.bot.send_message(chat_id=user.telegram_id, text=final_message, parse_mode='HTML')
            else: # Fallback para trades antigos sem ID de mensagem
                await application.bot.send_message(chat_id=user.telegram_id, text=final_message, parse_mode='HTML')

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
                # CORREÇÃO: Laço 'for' indentado para pertencer ao bloco 'else'.
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
