import asyncio
import logging
import time
import math
from telegram.ext import Application
from sqlalchemy.orm import Session
from database.session import SessionLocal
from database.models import Trade, User, PendingSignal
from services.bybit_service import (
    get_market_price, close_partial_position,
    modify_position_stop_loss, get_order_status,
    get_specific_position_size, modify_position_take_profit,
    get_last_closed_trade_info, get_open_positions_with_pnl,
    cancel_order
)
from services.notification_service import send_notification
from utils.security import decrypt_data
from sqlalchemy.sql import func
from telegram.error import BadRequest
from typing import Optional, Callable, Awaitable, Dict, Any, Set, Tuple, List
import pytz

logger = logging.getLogger(__name__)

# cache em memória para evitar reedições repetidas
# chave: trade.id, valor: {"sync_notified": bool}
_SYNC_CACHE = {}

def _compute_tp_distribution(strategy: str, total_tps: int) -> list[float]:
    """Gera uma distribuição de porcentagens (soma ~100) para N TPs.
    - 'EQUAL' => divide igualmente.
    - Lista (ex.: "50,30,20"): usa como âncoras e extrapola cauda em ordem decrescente,
      normalizando para 100% mesmo quando houver mais TPs que âncoras.
    """
    if total_tps <= 0:
        return []
    # Equal simples
    if not strategy or str(strategy).strip().upper() == 'EQUAL':
        return [100.0 / total_tps] * total_tps

    # Parseia lista de âncoras
    try:
        anchors = [max(0.0, float(x)) for x in str(strategy).replace('%', '').split(',') if x.strip()]
    except Exception:
        return [100.0 / total_tps] * total_tps
    if not anchors:
        return [100.0 / total_tps] * total_tps

    # Garante monotonicidade decrescente nas âncoras
    for i in range(1, len(anchors)):
        if anchors[i] > anchors[i-1]:
            anchors[i] = anchors[i-1]

    # Define fator de decaimento da cauda baseado nas duas últimas âncoras se possível, senão 0.66
    if len(anchors) >= 2 and anchors[-2] > 0:
        decay = min(0.95, max(0.3, anchors[-1] / anchors[-2]))  # clamp para estabilidade
    else:
        decay = 0.66

    # Constrói sequência base (monótona decrescente)
    base = []
    for i in range(total_tps):
        if i < len(anchors):
            base.append(anchors[i])
        else:
            nxt = base[-1] * decay if base else 1.0
            # Evita estagnar muito perto de zero com muitos TPs
            if nxt < 1e-6:
                nxt = 1e-6
            base.append(nxt)

    s = sum(base)
    if s <= 0:
        return [100.0 / total_tps] * total_tps
    # Normaliza para 100 e preserva ordem
    dist = [x * (100.0 / s) for x in base]
    # Pequena correção para somar exatamente 100: ajusta o último
    total = sum(dist)
    if total != 100.0:
        dist[-1] += (100.0 - total)
    # Garante não-crescente por segurança
    for i in range(1, len(dist)):
        if dist[i] > dist[i-1]:
            dist[i] = dist[i-1]
    return dist

def _generate_trade_status_message(trade: Trade, status_title: str, pnl_data: dict = None, current_price: float = None) -> str:
    """Dashboard compacto e rico para a mensagem de status do trade (HTML)."""
    arrow = "⬆️" if trade.side == "LONG" else "⬇️"

    # --- Dados base ---
    entry = float(trade.entry_price or 0.0)
    curr  = float(current_price or 0.0)
    qty   = float(trade.qty or 0.0)
    rem   = float(trade.remaining_qty if trade.remaining_qty is not None else qty)

    # --- P/L ao vivo (fração → sempre formatar x100 na exibição) ---
    unreal_val = float((pnl_data or {}).get("unrealized_pnl", 0.0))
    unreal_frac = float((pnl_data or {}).get("unrealized_pnl_frac", 0.0))  # ex.: 0.015 = 1.5%
    unreal_pct = unreal_frac * 100.0

    # --- TP progress / próximo alvo ---
    total_tps = int(trade.total_initial_targets or 0)
    remaining_targets = list(trade.initial_targets or [])
    hit_tps = max(0, total_tps - len(remaining_targets))
    next_tp = remaining_targets[0] if remaining_targets else None

    # Barrinha de progresso de TPs (ex.: ■■□□ para 2/4)
    filled = "■" * min(hit_tps, total_tps)
    empty  = "□" * max(0, total_tps - hit_tps)
    tp_bar = f"{filled}{empty}" if total_tps > 0 else "—"

    # --- Stop Loss (rótulos úteis) ---
    sl = trade.current_stop_loss
    sl_badge = []
    if trade.is_breakeven:
        sl_badge.append("BE")
    if trade.is_stop_gain_active:
        sl_badge.append("LOCK")
    if trade.trail_high_water_mark is not None:
        sl_badge.append("TS")
    sl_tag = f" [{' / '.join(sl_badge)}]" if sl_badge else ""

    # --- Datas/metadata ---
    created_str = ""
    try:
        if trade.created_at:
            created_str = trade.created_at.strftime("%d/%m %H:%M")
    except Exception:
        pass

    # --- Montagem da mensagem ---
    lines = []
    lines.append(f"{arrow} <b>{trade.symbol} — {trade.side}</b>")
    if status_title:
        lines.append(f"🟦 <b>{status_title}</b>")
    lines.append("")

    # Preços e tamanhos
    lines.append(f"➡️ <b>Entrada:</b> ${entry:,.4f}")
    if curr:
        lines.append(f"📊 <b>Atual:</b> ${curr:,.4f}")
    lines.append(f"📦 <b>Qtd. Total:</b> {qty:g} | <b>Restante:</b> {rem:g}")
    notional = entry * qty
    lines.append(f"💵 <b>Notional (aprox.):</b> ${notional:,.2f}")
    lines.append("")

    # P/L
    lines.append(f"📈 <b>P/L Atual:</b> {unreal_val:+.2f} USDT ({unreal_pct:+.2f}%)")

    # Stop
    if sl:
        lines.append(f"🛡️ <b>Stop Loss:</b> ${float(sl):,.4f}{sl_tag}")
    else:
        lines.append("🛡️ <b>Stop Loss:</b> —")
    lines.append("")

    # TPs
    if total_tps > 0:
        lines.append(f"🎯 <b>TPs:</b> {hit_tps}/{total_tps}  {tp_bar}")
        if next_tp is not None:
            lines.append(f"   ↳ <i>Próximo:</i> ${float(next_tp):,.4f}")
        lines.append("")

    if created_str:
        lines.append(f"⏱ <i>Aberto em:</i> {created_str}")

    return "\n".join(lines)

async def check_pending_orders_for_user(application: Application, user: User, db: Session):
    """Verifica as ordens limite pendentes e envia notificação na execução.
    OFF: cancela todas as pendentes e encerra. ON: acompanha e promove para Trade quando 'Filled'.
    """

    pending_orders = db.query(PendingSignal).filter_by(user_telegram_id=user.telegram_id).all()
    if not pending_orders:
        return

    # 🔑 DECRIPTA UMA ÚNICA VEZ (antes do branch ON/OFF)
    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)

    # Se o bot estiver OFF, cancela todas as pendentes e sai
    if not user.is_active:
        for order in pending_orders:
            try:
                await cancel_order(api_key, api_secret, order.order_id, order.symbol)
            except Exception as e:
                logger.error(f"[tracker:OFF] Exceção ao cancelar {order.order_id} ({order.symbol}): {e}", exc_info=True)
            db.delete(order)
        db.commit()
        logger.info(f"[tracker:OFF] PendingSignals do usuário {user.telegram_id} cancelados/limpos.")
        return

    # Bot ON: segue o fluxo normal
    for order in pending_orders:
        status_result = await get_order_status(api_key, api_secret, order.order_id, order.symbol)
        if not status_result.get("success"):
            logger.error(f"Falha ao obter status da ordem {order.order_id}: {status_result.get('error')}")
            continue

        order_data = status_result.get("data") or {}
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

            message_id_to_update = order.notification_message_id
            sent_message = None
            
            if message_id_to_update:
                try:
                    sent_message = await application.bot.edit_message_text(
                        chat_id=user.telegram_id,
                        message_id=message_id_to_update,
                        text=message,
                        parse_mode='HTML'
                    )
                except BadRequest as e:
                    logger.warning(f"Não foi possível editar a mensagem {message_id_to_update}. Enviando uma nova. Erro: {e}")
                    sent_message = await application.bot.send_message(chat_id=user.telegram_id, text=message, parse_mode='HTML')
            else:
                # Fallback para ordens antigas que não tinham o ID da mensagem salvo.
                sent_message = await application.bot.send_message(chat_id=user.telegram_id, text=message, parse_mode='HTML')

            # --- DEDUPE: se já existir trade ativo para este símbolo, atualiza-o; caso contrário cria um novo ---
            existing = db.query(Trade).filter(
                Trade.user_telegram_id == order.user_telegram_id,
                Trade.symbol == order.symbol,
                ~Trade.status.like('%CLOSED%')
            ).order_by(Trade.created_at.desc()).first()

            if existing:
                # Atualiza o trade existente com os dados confirmados da execução
                existing.order_id = existing.order_id or order.order_id
                existing.side = side
                existing.qty = qty
                existing.remaining_qty = qty
                existing.entry_price = entry_price
                existing.stop_loss = stop_loss
                existing.current_stop_loss = stop_loss
                existing.initial_targets = all_targets
                existing.total_initial_targets = num_targets
                # Prioriza a mensagem recém-enviada para manter o histórico coerente
                existing.notification_message_id = sent_message.message_id
                logger.info(
                    "[order->trade:merge] %s %s qty=%.6f entry=%.6f -> trade_id=%s",
                    existing.symbol, existing.side, existing.qty, existing.entry_price, str(existing.id)
                )
            else:
                new_trade = Trade(
                    user_telegram_id=order.user_telegram_id, order_id=order.order_id,
                    notification_message_id=sent_message.message_id, # Passa o ID correto para o trade
                    symbol=order.symbol, side=side, qty=qty, entry_price=entry_price,
                    stop_loss=stop_loss, current_stop_loss=stop_loss,
                    initial_targets=all_targets,
                    total_initial_targets=num_targets,
                    status='ACTIVE', remaining_qty=qty
                )
                db.add(new_trade)
                logger.info(
                    "[order->trade:new] %s %s qty=%.6f entry=%.6f msg_id=%s",
                    new_trade.symbol, new_trade.side, new_trade.qty, new_trade.entry_price,
                    str(getattr(new_trade, "notification_message_id", None))
                )

            # Em qualquer dos casos, remove o PendingSignal correspondente
            db.delete(order)


async def check_active_trades_for_user(application: Application, user: User, db: Session):
    """
    Verifica e gerencia os trades ativos, com edição de mensagem para atualizações.
    """
    active_trades = db.query(Trade).filter(
        Trade.user_telegram_id == user.telegram_id,
        ~Trade.status.like('%CLOSED%')
    ).all()
    if not active_trades:
        return

    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)

    live_pnl_result = await get_open_positions_with_pnl(api_key, api_secret)
    if not live_pnl_result.get("success"):
        logger.warning(f"[tracker] Falha temporária ao buscar P/L para {user.telegram_id}. Ignorando ciclo.")
        return

    live_pnl_map = {p['symbol']: p for p in (live_pnl_result.get('data') or [])}

    be_trigger_pct = float(getattr(user, "be_trigger_pct", 0) or 0.0)
    ts_trigger_pct = float(getattr(user, "ts_trigger_pct", 0) or 0.0)

    for trade in active_trades:
        position_data = live_pnl_map.get(trade.symbol)
        live_position_size = float(position_data['size']) if position_data else 0.0

        message_was_edited = False
        status_title_update = ""
        current_price = 0.0

        if position_data:
            trade.unrealized_pnl_pct = position_data.get("unrealized_pnl_frac", 0.0)

        if live_position_size > 0:
            price_result = await get_market_price(trade.symbol)
            if not price_result.get("success"):
                continue
            current_price = price_result["price"]

            pnl_data = live_pnl_map.get(trade.symbol) or {}
            pnl_frac = float(pnl_data.get("unrealized_pnl_frac") or 0.0)
            pnl_pct = pnl_frac * 100.0

            # --- Stop-Gain com degraus (ladder) ---
            # Em cada múltiplo do gatilho, avança a trava proporcionalmente ao número de degraus.
            sg_trig = float(getattr(user, 'stop_gain_trigger_pct', 0) or 0.0)
            sg_lock = float(getattr(user, 'stop_gain_lock_pct', 0) or 0.0)
            if sg_trig > 0 and sg_lock > 0:
                steps = int(math.floor(pnl_pct / sg_trig))
                if steps >= 1:
                    log_prefix = f"[Stop-Gain {trade.symbol}]"
                    if trade.side == 'LONG':
                        new_sl = float(trade.entry_price) * (1.0 + (sg_lock / 100.0) * steps)
                    else:
                        new_sl = float(trade.entry_price) * (1.0 - (sg_lock / 100.0) * steps)

                    is_improvement = (trade.side == 'LONG' and new_sl > (trade.current_stop_loss or float('-inf'))) or \
                                     (trade.side == 'SHORT' and new_sl < (trade.current_stop_loss or float('inf')))
                    is_valid_to_set = (trade.side == 'LONG' and new_sl < current_price) or \
                                      (trade.side == 'SHORT' and new_sl > current_price)

                    if is_improvement and is_valid_to_set:
                        sl_result = await modify_position_stop_loss(api_key, api_secret, trade.symbol, new_sl, reason="lock")
                        if sl_result.get("success"):
                            trade.is_stop_gain_active = True  # marca que foi ativado pelo menos uma vez
                            trade.current_stop_loss = new_sl
                            message_was_edited = True
                            status_title_update = f"💰 Stop-Gain Avançado (+{sg_lock:.2f}% x {steps})"
                            logger.info(f"{log_prefix} SL → ${new_sl:.4f} (steps={steps})")
                        else:
                            logger.error(f"{log_prefix} Falha ao mover SL (lock): {sl_result.get('error', 'desconhecido')}")

            # --- TAKE PROFIT (com a nova lógica de distribuição) ---
            targets_executados_este_ciclo = []
            if trade.initial_targets:
                for target_price in list(trade.initial_targets):
                    hit = (trade.side == 'LONG' and current_price >= target_price) or \
                          (trade.side == 'SHORT' and current_price <= target_price)
                    if not hit:
                        continue

                    if not trade.total_initial_targets or trade.total_initial_targets <= 0:
                        logger.warning(f"{trade.symbol}: total_initial_targets inválido ({trade.total_initial_targets}).")
                        continue

                    # --- Nova lógica de distribuição: adaptativa, decrescente e normalizada ---
                    strategy = getattr(user, 'tp_distribution', 'EQUAL')
                    dist = _compute_tp_distribution(strategy, int(trade.total_initial_targets))
                    # Índice do TP atual (0-based)
                    target_index = trade.total_initial_targets - len(trade.initial_targets)
                    try:
                        current_tp_percent = float(dist[target_index])
                    except Exception:
                        current_tp_percent = 100.0 / float(trade.total_initial_targets)
                    qty_to_close = trade.qty * (current_tp_percent / 100.0)
                    logger.info(f"[TP Distrib] %s: TP#%d = %.4f%% -> qty %.8f",
                                trade.symbol, target_index + 1, current_tp_percent, qty_to_close)
                    
                    position_idx_to_close = 1 if trade.side == 'LONG' else 2

                    logger.info("[tp:crossed] %s %s TP=%.4f last=%.4f -> tentando reduzir %.6f",
                                trade.symbol, trade.side, float(target_price), float(current_price), qty_to_close)

                    close_result = await close_partial_position(
                        api_key, api_secret, trade.symbol, qty_to_close, trade.side, position_idx_to_close
                    )
                    
                    if close_result.get("success"):
                        targets_executados_este_ciclo.append(target_price)
                        trade.remaining_qty = max(0.0, (trade.remaining_qty or trade.qty) - qty_to_close)
                        message_was_edited = True
                        status_title_update = "🎯 Take Profit EXECUTADO!"
                        logger.info("[tp:executed] %s %s TP=%.4f closed=%.6f remaining=%.6f",
                                    trade.symbol, trade.side, float(target_price),
                                    float(qty_to_close), float(trade.remaining_qty or 0.0))
                    else:
                        logger.error("[tp:failed] %s %s TP=%.4f reason=%s",
                                     trade.symbol, trade.side, float(target_price),
                                     close_result.get("error", "desconhecido"))

            if targets_executados_este_ciclo:
                trade.initial_targets = [t for t in trade.initial_targets if t not in targets_executados_este_ciclo]
                message_was_edited = True
                if not status_title_update:
                    status_title_update = "🎯 Take Profit EXECUTADO!"

            # --- BREAK-EVEN ---
            be_trigger_hit = False
            if be_trigger_pct > 0 and not trade.is_breakeven:
                # Opcional: ativa BE por PnL, sem depender de 1º TP
                if pnl_pct >= be_trigger_pct:
                    desired_sl = float(trade.entry_price)
                    be_trigger_hit = True
            # Modo padrão: 1º TP move para BE / TPs seguintes avançam
            if user.stop_strategy == 'BREAK_EVEN':
                if targets_executados_este_ciclo or be_trigger_hit:
                    if targets_executados_este_ciclo:
                        tp_ref = max(targets_executados_este_ciclo) if trade.side == 'LONG' else min(targets_executados_este_ciclo)
                        if trade.is_breakeven:
                            desired_sl = float(tp_ref)  # avança para o TP atingido
                            reason = f"Break-Even Avançado (TP {tp_ref:.4f})"
                        else:
                            desired_sl = float(trade.entry_price)
                            reason = "Break-Even Ativado (1º TP)"
                    else:
                        # veio do gatilho por PnL
                        reason = f"Break-Even por PnL ({pnl_pct:.2f}%)"

                    is_improvement = (trade.side == 'LONG' and desired_sl > (trade.current_stop_loss or float('-inf'))) or \
                                     (trade.side == 'SHORT' and desired_sl < (trade.current_stop_loss or float('inf')))
                    is_valid_to_set = (trade.side == 'LONG' and desired_sl < current_price) or \
                                      (trade.side == 'SHORT' and desired_sl > current_price)

                    if is_improvement and is_valid_to_set:
                        sl_result = await modify_position_stop_loss(api_key, api_secret, trade.symbol, desired_sl, reason="be")
                        if sl_result.get("success"):
                            trade.is_breakeven = True
                            trade.current_stop_loss = desired_sl
                            message_was_edited = True
                            status_title_update = f"🛡️ {reason}"
                            logger.info("[be:set] %s %s SL=%.4f last=%.4f", trade.symbol, trade.side, desired_sl, float(current_price))
                        else:
                            logger.error("[be:failed] %s SL=%.4f reason=%s", trade.symbol, desired_sl, sl_result.get('error', 'desconhecido'))

            # --- TRAILING STOP ---
            if user.stop_strategy == 'TRAILING_STOP':
                # Começo do TS: (A) após 1º TP (padrão) ou (B) por PnL opcional
                first_tp_hit = trade.total_initial_targets is not None and \
                                 trade.initial_targets is not None and \
                               len(trade.initial_targets) < trade.total_initial_targets
                ts_started = first_tp_hit or (ts_trigger_pct > 0 and pnl_pct >= ts_trigger_pct)

                if ts_started:
                    log_prefix = f"[Trailing Stop {trade.symbol}]"
                    if not trade.is_breakeven:
                        # Primeiro passo do TS = mover para BE
                        new_sl = float(trade.entry_price)
                        sl_result = await modify_position_stop_loss(api_key, api_secret, trade.symbol, new_sl, reason="ts")
                        if sl_result.get("success"):
                            trade.is_breakeven = True
                            trade.current_stop_loss = new_sl
                            trade.trail_high_water_mark = new_sl
                            message_was_edited = True
                            status_title_update = "🛡️ Stop Movido (Break-Even)"
                            logger.info(f"{log_prefix} SL → BE (${new_sl:.4f}) (gatilho: {'TP' if first_tp_hit else f'PnL {pnl_pct:.2f}%'})")
                        else:
                            logger.error(f"{log_prefix} Falha ao mover SL para BE: {sl_result.get('error', 'desconhecido')}")
                    else:
                        # Atualiza HWM e recalcula SL "seguindo" o preço
                        if trade.trail_high_water_mark is None:
                            trade.trail_high_water_mark = trade.entry_price
                        new_hwm = trade.trail_high_water_mark
                        if trade.side == 'LONG' and current_price > new_hwm:
                            new_hwm = current_price
                        elif trade.side == 'SHORT' and current_price < new_hwm:
                            new_hwm = current_price

                        if new_hwm != trade.trail_high_water_mark:
                            logger.info(f"{log_prefix} Novo pico: ${new_hwm:.4f}")
                            trade.trail_high_water_mark = new_hwm

                        # Distância do rastro: usa SL inicial se houver; fallback 2% da entrada
                        trail_distance = abs(trade.entry_price - (trade.stop_loss or trade.entry_price * 0.98)) \
                                         if trade.stop_loss is not None else trade.entry_price * 0.02
                        potential_new_sl = new_hwm - trail_distance if trade.side == 'LONG' else new_hwm + trail_distance

                        is_improvement = (trade.side == 'LONG' and potential_new_sl > (trade.current_stop_loss or float('-inf'))) or \
                                         (trade.side == 'SHORT' and potential_new_sl < (trade.current_stop_loss or float('inf')))
                        if is_improvement:
                            is_valid_to_set = (trade.side == 'LONG' and potential_new_sl < current_price) or \
                                              (trade.side == 'SHORT' and potential_new_sl > current_price)
                            if is_valid_to_set:
                                sl_result = await modify_position_stop_loss(api_key, api_secret, trade.symbol, potential_new_sl, reason="ts")
                                if sl_result.get("success"):
                                    trade.current_stop_loss = potential_new_sl
                                    message_was_edited = True
                                    status_title_update = "📈 Trailing Stop Ajustado"
                                else:
                                    logger.error(f"{log_prefix} Falha ao mover Trailing SL: {sl_result.get('error', 'desconhecido')}")

            # --- Mensagem viva (status em aberto) ---
            if message_was_edited:
                pnl_data_for_msg = live_pnl_map.get(trade.symbol)
                msg_text = _generate_trade_status_message(trade, status_title_update, pnl_data_for_msg, current_price)
                await _send_or_edit_trade_message(application, user, trade, db, msg_text)

async def run_tracker(application: Application):
    """Função principal que roda o verificador em loop para TODOS os usuários."""
    logger.info("Iniciando Rastreador de Posições e Ordens (Modo Multiusuário)...")
    while True:
        cycle_started = time.perf_counter()
        total_users = 0
        adopted_count = 0

        db = SessionLocal()
        try:
            # --- LÓGICA DE SINCRONIZAÇÃO APRIMORADA ---
            all_api_users_for_sync = db.query(User).filter(User.api_key_encrypted.isnot(None)).all()
            for user in all_api_users_for_sync:
                total_users += 1
                sync_api_key = decrypt_data(user.api_key_encrypted)
                sync_api_secret = decrypt_data(user.api_secret_encrypted)

                # Wrapper para o detetive: usa suas credenciais e adapta o formato
                async def _fetch_closed_info(symbol: str) -> Optional[Dict[str, Any]]:
                    res = await get_last_closed_trade_info(sync_api_key, sync_api_secret, symbol)
                    if not res or not res.get("success"):
                        return None
                    d = res.get("data") or {}
                    # padroniza campos esperados pelo detetive
                    return {
                        "pnl": float(d.get("closedPnl", 0.0)) if d.get("closedPnl") is not None else None,
                        "exit_type": d.get("exitType"),
                        "exit_price": d.get("exitPrice"),
                        "closed_at": d.get("closedAt"),
                    }

                bybit_positions_result = await get_open_positions_with_pnl(sync_api_key, sync_api_secret)
                if not bybit_positions_result.get("success"):
                    logger.error(f"Sincronização: Falha ao buscar posições da Bybit para o usuário {user.telegram_id}. Pulando.")
                    continue

                bybit_list = bybit_positions_result.get("data", []) or []
                bybit_keys = {(p["symbol"], p["side"]) for p in bybit_list}
                bybit_map = {(p["symbol"], p["side"]): p for p in bybit_list}

                # [NOVO] conjunto só por símbolo (ignora side)
                bybit_symbols = {p["symbol"] for p in bybit_list}

                # [NOVO] mapa por símbolo -> se tiver mais de uma entrada do mesmo símbolo,
                # fica com a de maior tamanho absoluto (mais relevante)
                bybit_map_by_symbol: Dict[str, Dict[str, Any]] = {}
                for p in bybit_list:
                    sym = p["symbol"]
                    if sym not in bybit_map_by_symbol:
                        bybit_map_by_symbol[sym] = p
                    else:
                        prev = bybit_map_by_symbol[sym]
                        if abs(float(p.get("size") or 0)) > abs(float(prev.get("size") or 0)):
                            bybit_map_by_symbol[sym] = p

                db_active_trades = db.query(Trade).filter(
                    Trade.user_telegram_id == user.telegram_id,
                    ~Trade.status.like('%CLOSED%')
                ).all()
            
                db_pending_signals = db.query(PendingSignal).filter(
                    PendingSignal.user_telegram_id == user.telegram_id
                ).all()
                db_pending_symbols = {s.symbol for s in db_pending_signals}

                # [NOVO] Adotar órfãs por SÍMBOLO (ignora side)
                db_active_symbols = {t.symbol for t in db_active_trades}
                # db_pending_symbols você JÁ construiu acima e é um set de strings: {s.symbol for s in db_pending_signals}

                symbols_to_adopt = bybit_symbols - db_active_symbols - db_pending_symbols
                for symbol in symbols_to_adopt:
                    adopted_count += 1
                    pos = bybit_map_by_symbol.get(symbol)
                    if not pos:
                        continue  # segurança

                    # Safeguard: se aparecer um trade ativo para o mesmo símbolo (concorrência), não duplique
                    exists_active = db.query(Trade).filter(
                        Trade.user_telegram_id == user.telegram_id,
                        Trade.symbol == symbol,
                        ~Trade.status.like('%CLOSED%')
                    ).first()
                    if exists_active:
                        logger.info("[sync:safe-skip] %s já tem trade ativo (id=%s).", symbol, str(exists_active.id))
                        continue

                    side = pos.get("side")
                    entry = float(pos.get("entry", 0) or 0)
                    size = float(pos.get("size", 0) or 0)
                    curr_sl = pos.get("stop_loss") or None

                    new_trade = Trade(
                        user_telegram_id=user.telegram_id,
                        order_id=f"sync_{symbol}_{int(time.time())}",
                        symbol=symbol,
                        side=side,
                        qty=size,
                        remaining_qty=size,
                        entry_price=entry,
                        status='ACTIVE_SYNCED',
                        stop_loss=curr_sl,
                        current_stop_loss=curr_sl,
                        initial_targets=[],
                        total_initial_targets=0
                    )

                    cand = db.query(PendingSignal).filter_by(
                        user_telegram_id=user.telegram_id, symbol=symbol
                    ).order_by(PendingSignal.id.desc()).first()
                    if cand and cand.signal_data:
                        try:
                            tps = cand.signal_data.get('targets') or []
                            new_trade.initial_targets = tps
                            new_trade.total_initial_targets = len(tps)
                            if not curr_sl and cand.signal_data.get('stop_loss'):
                                new_trade.stop_loss = cand.signal_data['stop_loss']
                                new_trade.current_stop_loss = new_trade.stop_loss
                            db.delete(cand)
                            logger.info("[sync:recover-signal] %s: recuperados %d TP(s) e SL.", symbol, len(tps))
                        except Exception:
                            logger.exception("[sync:recover-signal] falhou ao mapear sinal para %s", symbol)

                    db.add(new_trade)

                    msg = (
                        f"⚠️ <b>Posição Sincronizada</b>\n"
                        f"Moeda: <b>{symbol}</b> | Lado: <b>{side}</b>\n"
                        f"A posição foi encontrada aberta na Bybit e adotada pelo bot.\n"
                        f"{'Alvos/SL recuperados.' if new_trade.total_initial_targets else 'Sem alvos conhecidos.'}"
                    )
                    await application.bot.send_message(chat_id=user.telegram_id, text=msg, parse_mode='HTML')

                # [NOVO] Fechar fantasmas com tolerância (aplica janela de 3 ciclos)
                await apply_missing_cycles_policy(
                    application=application,
                    user=user,
                    db=db,
                    db_active_trades=db_active_trades,
                    bybit_keys=bybit_keys,
                    threshold=3,  # configurável no futuro via env/setting se necessário
                    get_last_closed_trade_info=_fetch_closed_info,
                )

            duration = time.perf_counter() - cycle_started
            logger.info("[cycle] resumo: usuarios=%d, adotadas=%d, duracao=%.2fs",
            total_users, adopted_count, duration)

            db.commit()

            # --- Lógica de verificação normal ---
            all_users = db.query(User).filter(User.api_key_encrypted.isnot(None)).all()
            if all_users:
                logger.info(f"Rastreador: Verificando assets para {len(all_users)} usuário(s).")
                for user in all_users:
                    await check_pending_orders_for_user(application, user, db)
                    await check_active_trades_for_user(application, user, db)
                db.commit()
            else:
                logger.info("Rastreador: Nenhum usuário com API para verificar.")

        except Exception as e:
            logger.critical(f"Erro crítico no loop do rastreador: {e}", exc_info=True)
            db.rollback()
        finally:
            db.close()

        await asyncio.sleep(15)

async def notify_sync_status(application, user, trade, text: Optional[str] = None) -> None:
    """
    Edita o card para estado 'sincronizando' no 2º ciclo ausente.
    Evita repetir a mesma edição em ciclos seguintes.
    """
    if trade is None or not getattr(trade, "notification_message_id", None):
        return

    cache = _SYNC_CACHE.setdefault(trade.id, {"sync_notified": False})
    if cache["sync_notified"]:
        return  # já notificou este estado; não spammar

    sync_text = text or (
        "⏳ <b>Sincronizando com a corretora…</b>\n"
        "Estamos confirmando o status desta posição. O card será atualizado automaticamente."
    )
    try:
        await application.bot.edit_message_text(
            chat_id=user.telegram_id,
            message_id=trade.notification_message_id,
            text=sync_text,
            parse_mode="HTML",
        )
        cache["sync_notified"] = True
        logger.info("[sync] %s/%s marcado como 'sincronizando' (2º ciclo ausente).",
                    trade.symbol, trade.side)
    except Exception:
        logger.exception("[sync] Falha ao editar mensagem para estado 'sincronizando' (%s).", trade.symbol)

def clear_sync_flag(trade_id: int) -> None:
    """Reseta a flag de sync para quando a posição reaparece ou fecha definitivamente."""
    state = _SYNC_CACHE.get(trade_id)
    if state:
        state["sync_notified"] = False

async def confirm_and_close_trade(
    *,
    application,
    user,
    trade,
    db,  # sessão do banco
    get_last_closed_trade_info: Optional[Callable[[str], Awaitable[Optional[Dict[str, Any]]]]] = None,
    attempts: int = 3,
    delay_seconds: float = 6.0,
    fallback_text: Optional[str] = None,
) -> bool:
    """
    Antes de marcar CLOSED_GHOST, tenta confirmar fechamento real.
    Se encontrar dados, edita o card com resumo e FECHA/PERSISTE no DB.
    Retorna True se persistiu fechamento com dados; False caso contrário.
    """
    info = None
    # 1) Tenta calcular PnL do trade por símbolo+lado dentro da janela
    try:
        from services.bybit_service import get_closed_pnl_for_trade
        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)
        start_ts = getattr(trade, "created_at", None)
        if start_ts is None:
            from datetime import datetime, timedelta
            # Use timezone-aware UTC to avoid naive/aware comparison issues downstream
            start_ts = datetime.now(pytz.utc) - timedelta(hours=6)
        for i in range(1, attempts + 1):
            agg = await get_closed_pnl_for_trade(
                api_key, api_secret, trade.symbol, trade.side, start_ts
            )
            if agg.get("success"):
                # Se não achou itens, gross/fees/funding devem ser 0 e exit_type Unknown — considera sem dados
                gross = float(agg.get("gross_pnl", 0) or 0)
                fees = float(agg.get("fees", 0) or 0)
                funding = float(agg.get("funding", 0) or 0)
                etype = agg.get("exit_type") or ""
                if gross != 0 or fees != 0 or funding != 0 or (etype and etype.lower() != "unknown"):
                    info = {
                        "pnl": float(agg.get("net_pnl", 0) or 0),
                        "exit_type": agg.get("exit_type"),
                        "exit_price": None,
                        "closed_at": None,
                    }
                    logger.debug("[close-confirm] agg info obtida para %s: %s", trade.symbol, str(info))
                    break
            await asyncio.sleep(delay_seconds)
    except Exception:
        logger.exception("[close-confirm] falha no cálculo agregado do PnL para %s. Usando fallback.", trade.symbol)

    # 2) Fallback: usa detetive simples (último closedPnL do símbolo)
    if not info and get_last_closed_trade_info:
        for i in range(1, attempts + 1):
            try:
                info = await get_last_closed_trade_info(trade.symbol)
                if info:
                    logger.debug("[close-confirm:fallback] info obtida para %s na tentativa %d: %s", trade.symbol, i, str(info))
                    break
                logger.debug("[close-confirm:fallback] sem info para %s na tentativa %d.", trade.symbol, i)
            except Exception:
                logger.exception("[close-confirm:fallback] tentativa %d falhou para %s", i, trade.symbol)
            await asyncio.sleep(delay_seconds)

    def _fmt_money(v):
        try:
            return f"${float(v):,.2f}"
        except Exception:
            return str(v)
    def _fmt_money_signed(v):
        try:
            f = float(v)
            return f"-${abs(f):,.2f}" if f < 0 else f"+${f:,.2f}"
        except Exception:
            return str(v)

    side = getattr(trade, "side", "") or ""
    qty  = getattr(trade, "qty", None)
    entry = getattr(trade, "entry_price", None)

    # Monta texto final (mesmo corpo da Etapa 1)
    if info:
        pnl = info.get("pnl")
        exit_type = (info.get("exit_type") or "Fechamento").strip()
        exit_price = info.get("exit_price")
        closed_at_val = info.get("closed_at")

        # Cabeçalho claro: LUCRO / PREJUÍZO
        result_emoji = "✅" if (pnl is not None and float(pnl) >= 0) else "🔻"
        result_label = "LUCRO" if (pnl is not None and float(pnl) >= 0) else "PREJUÍZO"
        reason = (
            "Take Profit" if str(exit_type).lower().startswith("take") else
            "Stop" if str(exit_type).lower().startswith("stop") else
            "Fechamento"
        )
        lines = [f"{result_emoji} <b>{result_label}</b> — <b>{trade.symbol}</b> {side}", f"• Tipo: <b>{reason}</b>"]
        if qty is not None:
            lines.append(f"• Quantidade: <b>{qty:g}</b>")
        if entry is not None:
            lines.append(f"• Entrada: <b>{_fmt_money(entry)}</b>")
        if exit_price is not None:
            lines.append(f"• Saída: <b>{_fmt_money(exit_price)}</b>")
        if pnl is not None:
            lines.append(f"• P/L: <b>{_fmt_money_signed(pnl)}</b>")
        if closed_at_val:
            # Tenta converter para America/Sao_Paulo
            from datetime import datetime
            br = pytz.timezone("America/Sao_Paulo")
            try:
                dt = None
                if isinstance(closed_at_val, (int, float)):
                    ts = float(closed_at_val)
                    if ts > 10_000_000_000:
                        ts = ts / 1000.0
                    dt = datetime.utcfromtimestamp(ts).replace(tzinfo=pytz.utc)
                elif isinstance(closed_at_val, str):
                    # Tenta ISO first
                    try:
                        dt = datetime.fromisoformat(closed_at_val.replace("Z", "+00:00"))
                        if not dt.tzinfo:
                            dt = pytz.utc.localize(dt)
                    except Exception:
                        dt = None
                elif hasattr(closed_at_val, 'tzinfo'):
                    dt = closed_at_val
                    if not dt.tzinfo:
                        dt = pytz.utc.localize(dt)
                if dt is not None:
                    lines.append(f"• Horário: <b>{dt.astimezone(br).strftime('%d/%m %H:%M')}</b>")
                else:
                    lines.append(f"• Horário: <b>{closed_at_val}</b>")
            except Exception:
                lines.append(f"• Horário: <b>{closed_at_val}</b>")
        final_text = "\n".join(lines)
    else:
        lines = [
            f"ℹ️ <b>Posição Encerrada</b> — <b>{trade.symbol}</b> {side}",
        ]
        if qty is not None:
            lines.append(f"• Quantidade: <b>{qty:g}</b>")
        if entry is not None:
            lines.append(f"• Entrada: <b>{_fmt_money(entry)}</b>")
        lines.append("• Detalhes de saída/PnL não disponíveis no momento.")
        lines.append("• O resumo pode aparecer nas próximas sincronizações.")
        final_text = "\n".join(lines)

    # Edita a mensagem no Telegram
    try:
        if getattr(trade, "notification_message_id", None):
            await application.bot.edit_message_text(
                chat_id=user.telegram_id,
                message_id=trade.notification_message_id,
                text=final_text,
                parse_mode="HTML",
            )
    except Exception:
        logger.exception("[close-confirm] Falha ao editar mensagem final para %s.", trade.symbol)

    # --- Persistência no DB quando houver info confirmada ---
    if info:
        try:
            pnl = info.get("pnl")
            exit_type = (info.get("exit_type") or "").lower()
            status = (
                "CLOSED_PROFIT" if exit_type.startswith("take")
                else "CLOSED_LOSS" if exit_type.startswith("stop")
                else ("CLOSED_PROFIT" if (pnl is not None and float(pnl) >= 0) else "CLOSED_LOSS" if pnl is not None else "CLOSED")
            )

            trade.status = status
            if pnl is not None:
                try:
                    trade.closed_pnl = float(pnl)
                except Exception:
                    logger.warning("[close-confirm] PnL inválido para %s: %s", trade.symbol, pnl)

            closed_at_val = info.get("closed_at")
            if closed_at_val:
                try:
                    from datetime import datetime
                    if isinstance(closed_at_val, (int, float)):
                        ts = float(closed_at_val)
                        if ts > 10_000_000_000:
                            ts = ts / 1000.0
                        # Store timezone-aware UTC timestamps in DB columns configured with timezone=True
                        trade.closed_at = datetime.utcfromtimestamp(ts).replace(tzinfo=pytz.utc)
                    elif isinstance(closed_at_val, str):
                        trade.closed_at = datetime.fromisoformat(closed_at_val.replace("Z", "+00:00"))
                    else:
                        trade.closed_at = func.now()
                except Exception:
                    logger.debug("[close-confirm] Falha ao parsear closed_at (%s) para %s; usando now().",
                                 str(closed_at_val), trade.symbol, exc_info=True)
                    trade.closed_at = func.now()
            else:
                trade.closed_at = func.now()

            trade.remaining_qty = 0.0
            db.commit()

            logger.info(
                "[close-confirm] fechamento_real_persistido symbol=%s side=%s status=%s pnl=%s exit_type=%s exit_price=%s closed_at=%s",
                trade.symbol, side, trade.status, str(getattr(trade, "closed_pnl", None)),
                info.get("exit_type"), str(info.get("exit_price")), str(getattr(trade, "closed_at", None))
            )
            return True
        except Exception:
            db.rollback()
            logger.exception("[close-confirm] Falha ao persistir fechamento real para %s.", trade.symbol)
            return False

    # Sem info confirmada
    logger.info("[close-confirm] sem_dados_confirmados symbol=%s side=%s -> manter fallback", trade.symbol, side)
    return False

# [ATUALIZAÇÃO] política de tolerância + UX etapa 2
async def apply_missing_cycles_policy(
    application,
    user,
    db,
    db_active_trades,
    bybit_keys,
    threshold: int = 3,
    get_last_closed_trade_info: Optional[Callable[[str], Awaitable[Optional[Dict[str, Any]]]]] = None,
):
    bybit_symbols = {k[0] for k in bybit_keys}

    for t in db_active_trades:
        if t.symbol in bybit_symbols:
            if getattr(t, "missing_cycles", 0) != 0:
                logger.info("[sync] visto_novamente symbol=%s side=%s reset_missing=%d->0", t.symbol, t.side, t.missing_cycles)
            t.missing_cycles = 0
            t.last_seen_at = func.now()
            if getattr(t, "id", None) is not None:
                clear_sync_flag(t.id)

    for t in db_active_trades:
        if t.symbol in bybit_symbols:
            continue

        prev = int(getattr(t, "missing_cycles", 0) or 0)
        t.missing_cycles = prev + 1
        logger.warning("[sync] ausente symbol=%s side=%s ciclo=%d/%d", t.symbol, t.side, t.missing_cycles, threshold)

        if t.missing_cycles == 2:
            await notify_sync_status(application, user, t)
            logger.info("[sync] estado_sincronizando symbol=%s side=%s ciclo=2/%d", t.symbol, t.side, threshold)

        if t.missing_cycles >= threshold:
            logger.info("[sync] limiar_fechamento symbol=%s side=%s ciclo=%d/%d iniciando_detetive",
                        t.symbol, t.side, t.missing_cycles, threshold)

            persisted = await confirm_and_close_trade(
                application=application,
                user=user,
                trade=t,
                db=db,
                get_last_closed_trade_info=get_last_closed_trade_info,
            )

            if persisted:
                try:
                    if not getattr(t, "status", None) or not str(t.status).startswith("CLOSED"):
                        t.status = "CLOSED"
                    if getattr(t, "remaining_qty", None) is None:
                        t.remaining_qty = 0.0
                    db.commit()
                except Exception:
                    db.rollback()
                    logger.exception("[sync] Falha ao consolidar fechamento real de %s.", t.symbol)

                logger.info("[sync] fechamento_real_consolidado symbol=%s side=%s status=%s pnl=%s",
                            t.symbol, t.side, t.status, str(getattr(t, "closed_pnl", None)))
                if getattr(t, "id", None) is not None:
                    clear_sync_flag(t.id)
                continue

            # Fallback -> CLOSED_GHOST
            t.status = "CLOSED_GHOST"
            t.closed_at = func.now()
            t.closed_pnl = t.closed_pnl or 0.0
            t.remaining_qty = 0.0
            logger.info("[sync] fallback_ghost symbol=%s side=%s motivo=no-info", t.symbol, t.side)
            if getattr(t, "id", None) is not None:
                clear_sync_flag(t.id)

async def _send_or_edit_trade_message(
    application: Application,
    user: User,
    trade: Trade,
    db: Session,
    text: str
) -> None:
    """
    Atualiza a 'mensagem viva' do trade de forma resiliente:
    - Se existe message_id → tenta editar.
    - Se a edição falhar (mensagem apagada/não editável) → envia nova
      e atualiza trade.notification_message_id no banco.
    """
    # 1) Tenta editar se já temos uma mensagem anterior
    if getattr(trade, "notification_message_id", None):
        try:
            await application.bot.edit_message_text(
                chat_id=user.telegram_id,
                message_id=trade.notification_message_id,
                text=text,
                parse_mode="HTML",
            )
            return  # sucesso, nada mais a fazer
        except BadRequest:
            # Qualquer falha típica de edição (apagada, muito antiga, etc.) → recriar
            pass
        except Exception:
            # Falha inesperada → também tenta recriar como fallback
            pass

    # 2) Não havia mensagem ou edição falhou → envia nova
    new_msg = await application.bot.send_message(
        chat_id=user.telegram_id,
        text=text,
        parse_mode="HTML",
    )
    trade.notification_message_id = new_msg.message_id
    logger.info("[msg:new] %s/%s nova_msg_id=%s",
            trade.symbol, trade.side, str(trade.notification_message_id))

    # 3) Persiste o novo ID no banco
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
