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
    get_last_closed_trade_info, get_open_positions_with_pnl,
    cancel_order
)
from services.notification_service import send_notification
from utils.security import decrypt_data
from sqlalchemy.sql import func
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

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
            db.delete(order)


async def check_active_trades_for_user(application: Application, user: User, db: Session):
    """
    Verifica e gerencia os trades ativos, com edição de mensagem para atualizações.
    Regras:
    - TP só é considerado 'executado' após sucesso na redução (retCode == 0).
    - BREAK_EVEN/TRAILING_STOP podem ser ativados de duas formas:
        (A) Padrão: após o 1º TP.
        (B) Opcional por PnL: se user.be_trigger_pct / user.ts_trigger_pct > 0 (sem depender do 1º TP).
    Esses campos são opcionais no modelo; se não existirem ou forem 0/None, ignora-se o gatilho por PnL.
    - Semântica do OFF (user.is_active == False): não abre novas posições nem deixa ordens pendentes,
      mas ESTE gerenciador continua atuando normalmente nas posições abertas.
    """
    active_trades = db.query(Trade).filter(
        Trade.user_telegram_id == user.telegram_id,
        ~Trade.status.like('%CLOSED%')
    ).all()
    if not active_trades:
        return

    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)

    # Posições ao vivo + PnL
    live_pnl_result = await get_open_positions_with_pnl(api_key, api_secret)
    if not live_pnl_result.get("success"):
        logger.warning(f"[tracker] Falha temporária ao buscar P/L para {user.telegram_id}. Ignorando ciclo.")
        return

    live_pnl_map = {p['symbol']: p for p in (live_pnl_result.get('data') or [])}

    # Gatilhos opcionais por PnL (se o modelo não tiver os campos, getattr devolve 0)
    be_trigger_pct = float(getattr(user, "be_trigger_pct", 0) or 0.0)      # ativa BE quando PnL% >= X
    ts_trigger_pct = float(getattr(user, "ts_trigger_pct", 0) or 0.0)      # inicia TS quando PnL% >= Y

    for trade in active_trades:
        position_data = live_pnl_map.get(trade.symbol)
        live_position_size = float(position_data['size']) if position_data else 0.0

        message_was_edited = False
        status_title_update = ""
        current_price = 0.0

        # Cache de P/L no DB (fração, ex.: 0.015 = 1.5%)
        if position_data:
            trade.unrealized_pnl_pct = position_data.get("unrealized_pnl_frac", 0.0)

        if live_position_size > 0:
            # Preço de mercado
            price_result = await get_market_price(trade.symbol)
            if not price_result.get("success"):
                continue
            current_price = price_result["price"]

            # Dados de PnL atuais (fração → % só para comparação/exibição)
            pnl_data = live_pnl_map.get(trade.symbol) or {}
            pnl_frac = float(pnl_data.get("unrealized_pnl_frac") or 0.0)
            pnl_pct = pnl_frac * 100.0

            # --- STOP-GAIN por gatilho (independente de BE/TS) ---
            if (user.stop_gain_trigger_pct or 0) > 0 and not trade.is_stop_gain_active and not trade.is_breakeven:
                if pnl_pct >= float(user.stop_gain_trigger_pct):
                    log_prefix = f"[Stop-Gain {trade.symbol}]"
                    if trade.side == 'LONG':
                        new_sl = trade.entry_price * (1 + (float(user.stop_gain_lock_pct or 0) / 100))
                    else:
                        new_sl = trade.entry_price * (1 - (float(user.stop_gain_lock_pct or 0) / 100))

                    is_improvement = (trade.side == 'LONG' and new_sl > (trade.current_stop_loss or float('-inf'))) or \
                                     (trade.side == 'SHORT' and new_sl < (trade.current_stop_loss or float('inf')))
                    is_valid_to_set = (trade.side == 'LONG' and new_sl < current_price) or \
                                      (trade.side == 'SHORT' and new_sl > current_price)

                    if is_improvement and is_valid_to_set:
                        sl_result = await modify_position_stop_loss(api_key, api_secret, trade.symbol, new_sl, reason="lock")
                        if sl_result.get("success"):
                            trade.is_stop_gain_active = True
                            trade.current_stop_loss = new_sl
                            message_was_edited = True
                            status_title_update = f"💰 Stop-Gain Ativado (+{float(user.stop_gain_lock_pct or 0):.2f}%)"
                            logger.info(f"{log_prefix} SL → ${new_sl:.4f}")
                        else:
                            logger.error(f"{log_prefix} Falha ao mover SL (lock): {sl_result.get('error', 'desconhecido')}")

            # --- TAKE PROFIT (confirmação só após redução bem-sucedida) ---
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

                    qty_to_close = trade.qty / trade.total_initial_targets
                    position_idx_to_close = 1 if trade.side == 'LONG' else 2  # em one-way a Bybit ignora

                    logger.info("[tp:crossed] %s %s TP=%.4f last=%.4f -> tentando reduzir",
                                trade.symbol, trade.side, float(target_price), float(current_price))

                    close_result = await close_partial_position(
                        api_key, api_secret, trade.symbol, qty_to_close, trade.side, position_idx_to_close
                    )
                    if close_result.get("success"):
                        targets_executados_este_ciclo.append(target_price)
                        try:
                            trade.remaining_qty = (trade.remaining_qty or trade.qty) - qty_to_close
                            if trade.remaining_qty < 0:
                                trade.remaining_qty = 0.0
                        except Exception:
                            trade.remaining_qty = max(0.0, (trade.remaining_qty or 0.0) - qty_to_close)

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

        else:
            # --- DETETIVE DE FECHAMENTO COM RETENTATIVAS ---
            # COMENTÁRIO: Lógica de fechamento refatorada para ser mais resiliente e precisa.
            logger.info(f"[tracker] Posição para {trade.symbol} não encontrada. Ativando detetive paciente...")
            
            closed_info_result = {"success": False}  # Começa como falha
            final_message = ""

            # Tenta buscar os detalhes do fechamento por até 3 vezes, com pausas
            for attempt in range(3):
                logger.info(f"[detetive] Tentativa {attempt + 1}/3 para obter detalhes de fechamento de {trade.symbol}...")
                result = await get_last_closed_trade_info(api_key, api_secret, trade.symbol)
                
                if result.get("success"):
                    closed_info_result = result
                    logger.info(f"[detetive] Sucesso na tentativa {attempt + 1}. Detalhes obtidos.")
                    break  # Se obteve sucesso, sai do loop
                
                if attempt < 2: # Se não for a última tentativa
                    logger.info("[detetive] Falha na tentativa. Aguardando 20 segundos antes de tentar novamente...")
                    await asyncio.sleep(20)
            
            # Prossegue com a lógica, usando o resultado final das tentativas
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
                else: # Manual, Liquidação, etc.
                    trade.status = 'CLOSED_GHOST' # Mantém GHOST mas com PNL
                    resultado_str = "LUCRO" if pnl >= 0 else "PREJUÍZO"
                    emoji = "✅" if pnl >= 0 else "🔻"
                    final_message = f"{emoji} <b>Posição Fechada ({resultado_str})</b>\n<b>Moeda:</b> {trade.symbol}\n<b>Resultado:</b> ${pnl:,.2f}"
            else:
                # Se mesmo após as tentativas falhar, mantém o comportamento original de fallback.
                logger.error(f"[detetive] Falha ao obter detalhes de fechamento para {trade.symbol} após 3 tentativas.")
                trade.status = 'CLOSED_GHOST'
                trade.closed_at = func.now()
                trade.closed_pnl = 0.0
                trade.remaining_qty = 0.0
                final_message = f"ℹ️ Posição em <b>{trade.symbol}</b> foi fechada na corretora. Detalhes de P/L não puderam ser obtidos via API."

            await _send_or_edit_trade_message(application, user, trade, db, final_message)

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

                bybit_list = bybit_positions_result.get("data", []) or []
                bybit_keys = {(p["symbol"], p["side"]) for p in bybit_list}
                bybit_map = {(p["symbol"], p["side"]): p for p in bybit_list}

                db_active_trades = db.query(Trade).filter(
                    Trade.user_telegram_id == user.telegram_id,
                    ~Trade.status.like('%CLOSED%')
                ).all()
                db_active_keys = {(t.symbol, t.side) for t in db_active_trades}

                db_pending_signals = db.query(PendingSignal).filter(
                    PendingSignal.user_telegram_id == user.telegram_id
                ).all()
                db_pending_symbols = {s.symbol for s in db_pending_signals}

                # Adotar órfãs (Bybit → DB)
                keys_to_adopt = bybit_keys - db_active_keys - {(sym, "LONG") for sym in db_pending_symbols} - {(sym, "SHORT") for sym in db_pending_symbols}
                for key in keys_to_adopt:
                    symbol, side = key
                    pos = bybit_map[key]
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

                # Fechar fantasmas (DB → Bybit)
                keys_to_close = db_active_keys - bybit_keys
                for t in db_active_trades:
                    if (t.symbol, t.side) in keys_to_close:
                        t.status = 'CLOSED_GHOST'
                        t.closed_at = func.now()
                        t.closed_pnl = 0.0
                        t.remaining_qty = 0.0
                        try:
                            if t.notification_message_id:
                                await application.bot.edit_message_text(
                                    chat_id=user.telegram_id,
                                    message_id=t.notification_message_id,
                                    text=f"ℹ️ Posição em <b>{t.symbol}</b> não foi encontrada na Bybit e foi removida.",
                                    parse_mode='HTML'
                                )
                        except Exception:
                            pass

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

        await asyncio.sleep(60)

from telegram.error import BadRequest

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

    # 3) Persiste o novo ID no banco
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
