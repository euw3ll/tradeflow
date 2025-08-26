import logging
import asyncio
import os
import random
from typing import Dict, Any, Optional
from datetime import datetime, time, timedelta
from pybit.unified_trading import HTTP
from pybit.exceptions import InvalidRequestError
from database.models import User
from decimal import Decimal, ROUND_DOWN, ROUND_CEILING
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)
INSTRUMENT_INFO_CACHE: Dict[str, Any] = {}

def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    # arredonda para baixo no múltiplo do step
    if step <= 0:
        return value
    return (value // step) * step

def _round_down_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    return (price // tick) * tick

def _round_up_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    # arredonda para o múltiplo de tick acima
    return ( (price / tick).to_integral_value(rounding=ROUND_CEILING) ) * tick

def _round_up_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Arredonda para CIMA no múltiplo de tick."""
    if tick <= 0:
        return price
    # se já está alinhado ao tick, retorna como está
    q, r = divmod(price, tick)
    if r == 0:
        return price
    return (q + 1) * tick

def _apply_safety_ticks(
    side: str,
    desired_sl: Decimal,
    last_price: Decimal,
    tick: Decimal,
    safety_ticks: int
) -> Decimal:
    """
    Garante distância mínima de N ticks do last_price, respeitando o lado:
      - LONG  (posição Buy):  SL < last_price - N*tick  (usa floor)
      - SHORT (posição Sell): SL > last_price + N*tick  (usa ceil)
    Retorna preço já alinhado ao tick.
    """
    if tick <= 0 or safety_ticks <= 0:
        return desired_sl

    n = Decimal(safety_ticks)
    if side.upper() in ("LONG", "BUY"):
        limite_max = last_price - (n * tick)   # deve ser estritamente abaixo do last
        # alinhar o limite para baixo no tick
        if limite_max > 0:
            limite_max = (limite_max // tick) * tick
        # nunca acima do limite
        return desired_sl if desired_sl <= limite_max else limite_max

    else:  # SHORT / SELL
        limite_min = last_price + (n * tick)   # deve ser estritamente acima do last
        # alinhar o limite para cima no tick
        if tick > 0:
            q, r = divmod(limite_min, tick)
            if r != 0:
                limite_min = (q + 1) * tick
        # nunca abaixo do limite
        return desired_sl if desired_sl >= limite_min else limite_min

async def get_instrument_info(symbol: str) -> Dict[str, Any]:
    """
    Busca as regras de um instrumento (símbolo) da Bybit, usando um cache em memória.
    """
    if symbol in INSTRUMENT_INFO_CACHE:
        return INSTRUMENT_INFO_CACHE[symbol]

    def _sync_call():
        try:
            # Sessão não autenticada com timeout (sem 'retries')
            session = HTTP(testnet=False, timeout=30)
            response = session.get_instruments_info(category="linear", symbol=symbol)
            
            if response.get("retCode") != 0:
                return {"success": False, "error": response.get("retMsg")}
            
            instrument_list = response.get("result", {}).get("list", [])
            if not instrument_list:
                return {"success": False, "error": f"Símbolo {symbol} não encontrado na Bybit."}

            info = instrument_list[0]
            lot_size_filter = info.get("lotSizeFilter", {})
            price_filter = info.get("priceFilter", {})

            rules = {
                "success": True,
                "status": info.get("status"),
                "qtyStep": Decimal(lot_size_filter.get("qtyStep", "0")),
                "minOrderQty": Decimal(lot_size_filter.get("minOrderQty", "0")),
                "minNotionalValue": Decimal(lot_size_filter.get("minOrderIv", "0")),
                "tickSize": Decimal(price_filter.get("tickSize", "0")),
            }
            INSTRUMENT_INFO_CACHE[symbol] = rules
            return rules
    
        except Exception as e:
            logger.error(f"Exceção em get_instrument_info para {symbol}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    return await asyncio.to_thread(_sync_call)


# Função auxiliar síncrona, não precisa de 'async'
def get_session(api_key: str, api_secret: str) -> HTTP:
    """Cria e retorna uma sessão HTTP para ser usada em threads."""
    return HTTP(
        testnet=False,
        api_key=api_key,
        api_secret=api_secret,
        timeout=30,
        recv_window=30000  # ↑ aumentamos para mitigar 10002 por drift/latência
    )

def _resolve_position_index(session, symbol: str, close_side: str) -> Dict[str, Any]:
    """
    Descobre o mode (One-Way vs Hedge) e qual positionIdx usar (ou omitir) ao reduzir posição.
    - One-Way: não enviar positionIdx (ou usar 0).
    - Hedge: usar 1 (Long/Buy) para reduzir LONG; usar 2 (Short/Sell) para reduzir SHORT.

    close_side: "Buy" ou "Sell" (lado da ORDEM de fechamento, não o 'trade.side').
    Retorna:
      {
        "mode": "one_way" | "hedge" | "unknown",
        "positionIdx": int | None,  # None => omitir no payload
        "position_found": bool,     # se há posição aberta detectada
        "details": {...}            # dados brutos mínimos para log/inspeção
      }
    """
    try:
        resp = session.get_positions(category="linear", symbol=symbol)
        if resp.get("retCode") != 0:
            logger.warning(f"[bybit_service] _resolve_position_index: falha em get_positions para {symbol}: {resp.get('retMsg')}")
            # fallback seguro: omitir positionIdx
            return {"mode": "unknown", "positionIdx": None, "position_found": False, "details": {"reason": "api_error"}}

        items = (resp.get("result", {}) or {}).get("list", []) or []
        # Normalizações úteis
        positions_nonzero = [p for p in items if float(p.get("size") or 0) > 0]
        idxs = {int(p.get("positionIdx") or 0) for p in items}

        # Heurística de detecção:
        # - One-Way geralmente retorna positionIdx 0 (ou único item), e só existe um lado efetivo.
        # - Hedge usa 1 (Long/Buy) e 2 (Short/Sell). Pode retornar dois itens para o símbolo.
        mode = "one_way"
        if 1 in idxs or 2 in idxs:
            mode = "hedge"
        elif len(items) > 1:
            # Vários itens mas sem 1/2 explícitos — trate como hedge por segurança
            mode = "hedge"

        # Mapeia qual idx usar conforme o lado que será reduzido
        # close_side = "Sell" se trade era LONG; "Buy" se trade era SHORT.
        position_idx = None
        position_found = False

        if mode == "hedge":
            # Encontre a posição correspondente ao lado que será reduzido
            # Em Hedge, Buy = LONG (idx 1), Sell = SHORT (idx 2)
            desired_idx = 1 if close_side == "Sell" else 2
            for p in items:
                if int(p.get("positionIdx") or 0) == desired_idx and float(p.get("size") or 0) > 0:
                    position_found = True
                    break
            # Mesmo que size=0, ainda usamos o idx “correto” para o lado.
            position_idx = desired_idx

        elif mode == "one_way":
            # Em One-Way, omita o campo (ou use 0). Preferimos omitir para evitar 10001.
            # Verifica se existe alguma posição aberta > 0
            position_found = any(positions_nonzero)
            position_idx = None  # omitir

        details = {
            "raw_count": len(items),
            "idxs": sorted(list(idxs)),
            "close_side": close_side,
            "positions_nonzero": len(positions_nonzero),
        }

        logger.info(f"[bybit_service] _resolve_position_index {symbol}: mode={mode}, close_side={close_side}, use_idx={position_idx}, found={position_found}, details={details}")
        return {"mode": mode, "positionIdx": position_idx, "position_found": position_found, "details": details}

    except Exception as e:
        logger.error(f"Exceção em _resolve_position_index para {symbol}: {e}", exc_info=True)
        # fallback seguro: omitir positionIdx
        return {"mode": "unknown", "positionIdx": None, "position_found": False, "details": {"reason": "exception", "error": str(e)}}


async def get_account_info(api_key: str, api_secret: str) -> dict:
    """Busca o saldo da conta, calculando o saldo disponível para Contas Unificadas."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            response = session.get_wallet_balance(accountType="UNIFIED")
            
            if response.get('retCode') == 0:
                account_data_list = response['result'].get('list', [])
                if not account_data_list:
                    return {"success": False, "data": {}, "error": "Lista de contas vazia na resposta da API."}
                
                account_data = account_data_list[0]
                equity_str = account_data.get('totalEquity')
                total_equity = float(equity_str) if equity_str else 0.0
                coin_list = account_data.get('coin', [])
                
                available_balance_usdt = 0.0
                for coin in coin_list:
                    if coin.get('coin') == 'USDT':
                        wallet_balance_str = coin.get('walletBalance', '0')
                        order_margin_str = coin.get('totalOrderIM', '0')
                        position_margin_str = coin.get('totalPositionIM', '0')

                        wallet_balance = float(wallet_balance_str) if wallet_balance_str else 0.0
                        order_margin = float(order_margin_str) if order_margin_str else 0.0
                        position_margin = float(position_margin_str) if position_margin_str else 0.0
                        
                        # Cálculo correto para Conta de Trading Unificada
                        available_balance_usdt = wallet_balance - order_margin - position_margin
                        break
                
                result_data = {
                    "total_equity": total_equity,
                    "available_balance_usdt": available_balance_usdt,
                    "coin_list": coin_list
                }
                return {"success": True, "data": result_data}
                
            return {"success": False, "data": {}, "error": response.get('retMsg', 'Erro desconhecido')}
        except Exception as e:
            logger.error(f"Exceção em get_account_info: {e}", exc_info=True)
            return {"success": False, "data": {}, "error": str(e)}

    return await asyncio.to_thread(_sync_call)

async def place_order(api_key: str, api_secret: str, signal_data: dict, user_settings: User, balance: float) -> dict:
    """Abre uma nova posição a mercado (Market) com validação completa, incluindo verificação de SL contra o preço atual."""
    symbol = signal_data['coin']
    
    # --- NOVA VERIFICAÇÃO DE PRÉ-VOO ---
    # Buscamos o preço de mercado ANTES de qualquer outra coisa
    price_check = await get_market_price(symbol)
    if not price_check.get("success"):
        return {"success": False, "error": f"Não foi possível obter o preço de mercado atual para {symbol}."}
    current_market_price = Decimal(str(price_check["price"]))
    
    # Validamos o Stop Loss do sinal contra o preço atual
    side = "Buy" if (signal_data.get('order_type') or '').upper() == 'LONG' else "Sell"
    stop_loss_price = Decimal(str(signal_data.get('stop_loss')))

    if side == 'Buy' and stop_loss_price >= current_market_price:
        return {"success": False, "error": f"Stop Loss ({stop_loss_price}) inválido para LONG. Deve ser menor que o preço atual ({current_market_price})."}
    if side == 'Sell' and stop_loss_price <= current_market_price:
        return {"success": False, "error": f"Stop Loss ({stop_loss_price}) inválido para SHORT. Deve ser maior que o preço atual ({current_market_price})."}
    
    # Se a validação passou, continuamos para a lógica de execução síncrona
    async def pre_flight_checks():
        if symbol not in INSTRUMENT_INFO_CACHE: await get_instrument_info(symbol)
        return INSTRUMENT_INFO_CACHE.get(symbol)

    def _sync_call(instrument_rules: Dict[str, Any]):
        try:
            if not instrument_rules or not instrument_rules.get("success"): return instrument_rules or {"success": False, "error": f"Regras para {symbol} não encontradas."}
            if instrument_rules["status"] != "Trading": return {"success": False, "error": f"O símbolo {symbol} não está ativo para negociação ({instrument_rules['status']})."}

            session = get_session(api_key, api_secret)
            leverage = Decimal(str(user_settings.max_leverage))
            
            # Usamos o preço de mercado que acabamos de buscar para o cálculo
            entry_price = current_market_price
            
            margin_in_dollars = Decimal(str(balance)) * (Decimal(str(user_settings.entry_size_percent)) / Decimal("100"))
            notional_value = margin_in_dollars * leverage
            
            if entry_price <= 0: return {"success": False, "error": f"Preço de entrada inválido: {entry_price}"}
            qty_raw = notional_value / entry_price
            qty_adj = _round_down_to_step(qty_raw, instrument_rules["qtyStep"])

            if qty_adj < instrument_rules["minOrderQty"]:
                return {"success": False, "error": f"Qtd. ajustada ({qty_adj:f}) é menor que a mínima permitida ({instrument_rules['minOrderQty']:f}) para {symbol}."}
            final_notional_value = qty_adj * entry_price
            if final_notional_value < instrument_rules["minNotionalValue"]:
                return {"success": False, "error": f"Valor total da ordem (${final_notional_value:.2f}) é menor que o mínimo permitido de ${instrument_rules['minNotionalValue']:.2f}."}
            
            payload = {
                "category": "linear", "symbol": symbol, "side": side, "orderType": "Market", "qty": str(qty_adj),
                "takeProfit": str((signal_data.get('targets') or [None])[0]), "stopLoss": str(stop_loss_price),
            }
            try:
                session.set_leverage(category="linear", symbol=symbol, buyLeverage=str(leverage), sellLeverage=str(leverage))
            except InvalidRequestError as e:
                if "leverage not modified" in str(e).lower(): logger.warning(f"Alavancagem para {symbol} já está correta. Continuando...")
                else: return {"success": False, "error": str(e)}
            
            _safe_log_order_payload("place_order:market_entry", payload)
            response = session.place_order(**{k: v for k, v in payload.items() if v is not None})
            if response.get('retCode') == 0: return {"success": True, "data": response['result']}
            return {"success": False, "error": response.get('retMsg')}
      
        except Exception as e:
            logger.error(f"Exceção ao abrir ordem (Market): {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    try:
        rules = await pre_flight_checks()
        return await asyncio.to_thread(_sync_call, rules)
    except Exception as e:
        logger.error(f"Exceção em place_order (async): {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_market_price(symbol: str) -> dict:
    """Busca o preço de mercado atual de forma assíncrona."""
    def _sync_call():
        try:
            session = HTTP(testnet=False, timeout=30)
            response = session.get_tickers(category="linear", symbol=symbol)
            if response.get('retCode') == 0 and response['result']['list']:
                price = float(response['result']['list'][0]['lastPrice'])
                return {"success": True, "price": price}
            else:
                return {"success": False, "error": response.get('retMsg', 'Preço não encontrado')}
        except Exception as e:
            logger.error(f"Exceção ao buscar preço de mercado para {symbol}: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    return await asyncio.to_thread(_sync_call)

async def close_partial_position(api_key: str, api_secret: str, symbol: str, qty_to_close: float, side: str, position_idx: int) -> dict:
    """Fecha parte de uma posição com Market/ReduceOnly.
    - Detecta o lado REAL da posição no exchange (não confia no `side` recebido).
    - Aplica One-Way vs Hedge automaticamente (usa/omite positionIdx conforme o modo).
    - Em 110017 (reduceOnly mesmo lado) e 10001 (idx/mode), faz retry defensivo.
    """
    async def pre_flight_checks():
        if symbol not in INSTRUMENT_INFO_CACHE:
            await get_instrument_info(symbol)
        return INSTRUMENT_INFO_CACHE.get(symbol)

    def _sync_call(instrument_rules: Dict[str, Any]):
        try:
            if not instrument_rules or not instrument_rules.get("success"):
                return instrument_rules or {"success": False, "error": f"Regras para {symbol} não encontradas."}

            session = get_session(api_key, api_secret)

            # 0) Descobre a(s) posição(ões) atual(is) na corretora
            pos_resp = session.get_positions(category="linear", symbol=symbol)
            if pos_resp.get("retCode") != 0:
                return {"success": False, "error": pos_resp.get("retMsg", "Falha ao obter posições atuais")}
            pos_items = (pos_resp.get("result", {}) or {}).get("list", []) or []
            pos_open = [p for p in pos_items if float(p.get("size") or 0) > 0]

            if not pos_open:
                logger.warning(f"[bybit_service] close_partial: nenhuma posição aberta em {symbol}. Nada a fechar.")
                return {"success": True, "skipped": True, "reason": "no_open_position"}

            # Se houver mais de uma (hedge com ambos os lados), priorizamos a primeira com size>0.
            p0 = pos_open[0]
            pos_side_api = (p0.get("side") or "").strip()  # "Buy" (LONG) ou "Sell" (SHORT)
            pos_idx_api = int(p0.get("positionIdx") or 0)

            # 1) Lado correto de fechamento é o CONTRÁRIO do lado da posição
            close_side = "Sell" if pos_side_api == "Buy" else "Buy"

            # 2) Ajuste de quantidade ao step e checagens mínimas
            qty_raw = Decimal(str(qty_to_close))
            qty_adj = _round_down_to_step(qty_raw, instrument_rules["qtyStep"])
            logger.info(
                f"[bybit_service] close_partial {symbol}: raw={qty_raw}, "
                f"step={instrument_rules['qtyStep']}, minQty={instrument_rules['minOrderQty']} => adj={qty_adj}"
            )
            if qty_adj < instrument_rules["minOrderQty"]:
                logger.warning(f"Quantidade a fechar para {symbol} ({qty_adj:f}) < mínimo permitido. Ignorando.")
                return {"success": True, "skipped": True, "reason": "qty_less_than_min_order_qty"}

            # 3) Resolve modo e índice de posição
            #    - One-Way: omitir positionIdx
            #    - Hedge: usar idx do lado da POSIÇÃO (não do lado da ordem)
            resolve = _resolve_position_index(session, symbol, close_side)
            mode = resolve.get("mode", "unknown")
            auto_idx = resolve.get("positionIdx", None)

            # Se hedge e o resolver não retornou idx, usar o índice da posição real
            if mode == "hedge":
                if auto_idx is None:
                    if pos_idx_api in (1, 2):
                        auto_idx = pos_idx_api
                    else:
                        # fallback por mapeamento do lado da POSIÇÃO
                        auto_idx = 1 if pos_side_api == "Buy" else 2
            else:
                # one-way: não enviar positionIdx
                auto_idx = None

            logger.info(
                f"[bybit_service] resolver: symbol={symbol}, mode={mode}, "
                f"pos_side_api={pos_side_api}, close_side={close_side}, "
                f"auto_idx={auto_idx}, pos_idx_api={pos_idx_api}, details={resolve.get('details')}"
            )

            # 4) Monta payload base
            payload = {
                "category": "linear",
                "symbol": symbol,
                "side": close_side,
                "orderType": "Market",
                "qty": str(qty_adj),
                "reduceOnly": True,
            }
            if auto_idx is not None:
                payload["positionIdx"] = auto_idx  # em hedge, vincula ao lado da posição aberta

            def _try_place(p):
                _safe_log_order_payload("close_partial:first_try", p)
                return session.place_order(**p)

            # 5) Primeira tentativa
            try:
                response = _try_place(payload)
                if response.get('retCode') == 0:
                    return {"success": True, "data": response['result']}
                msg = response.get('retMsg', '') or ''
                # 110017: reduce-only com mesmo lado da posição (corrida entre leitura e envio)
                if "reduce-only order has same side" in msg.lower() or "110017" in msg:
                    raise InvalidRequestError(msg)
                # 10001: idx/mode mismatch
                if "position idx not match" in msg.lower() or "10001" in msg:
                    raise InvalidRequestError(msg)
                return {"success": False, "error": msg}

            except InvalidRequestError as e:
                text = str(e).lower()
                alt_payload = dict(payload)
                alt_strategy = None

                if "reduce-only order has same side" in text or "110017" in text:
                    # Defensive: posição pode ter virado entre leitura e envio → inverter lado e ajustar idx
                    alt_payload["side"] = "Buy" if payload["side"] == "Sell" else "Sell"
                    if "positionIdx" in alt_payload:
                        # idx deve permanecer atrelado ao lado da POSIÇÃO alvo (flip de ordem não muda qual posição queremos reduzir)
                        # portanto, se flipou a ordem, manter o idx original (da posição aberta)
                        alt_payload["positionIdx"] = payload.get("positionIdx", pos_idx_api or (1 if pos_side_api == "Buy" else 2))
                    alt_strategy = f"flip_side_{payload['side']}_to_{alt_payload['side']}"
                    logger.warning(f"[bybit_service] 110017 detectado para {symbol}. Retry: {alt_strategy}")

                elif "position idx not match" in text or "10001" in text:
                    # Alternar presença do idx
                    if "positionIdx" in alt_payload:
                        alt_payload.pop("positionIdx", None)
                        alt_strategy = "remove_positionIdx"
                    else:
                        # em hedge, forçar idx coerente com a POSIÇÃO
                        alt_payload["positionIdx"] = pos_idx_api if pos_idx_api in (1, 2) else (1 if pos_side_api == "Buy" else 2)
                        alt_strategy = f"force_positionIdx_{alt_payload['positionIdx']}"
                    logger.warning(f"[bybit_service] 10001 detectado para {symbol}. Retry com '{alt_strategy}'.")

                else:
                    return {"success": False, "error": str(e)}

                try:
                    _safe_log_order_payload("close_partial:retry", alt_payload)
                    resp2 = session.place_order(**alt_payload)
                    if resp2.get('retCode') == 0:
                        logger.info(f"[bybit_service] retry sucesso para {symbol} com estratégia '{alt_strategy}'.")
                        return {
                            "success": True,
                            "data": resp2['result'],
                            "retry": True,
                            "retry_strategy": alt_strategy
                        }
                    return {
                        "success": False,
                        "error": resp2.get('retMsg', 'Falha no retry'),
                        "retry": True,
                        "retry_strategy": alt_strategy
                    }
                except InvalidRequestError as e2:
                    return {"success": False, "error": str(e2), "retry": True, "retry_strategy": alt_strategy}

        except Exception as e:
            logger.error(f"Exceção ao fechar posição parcial: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    try:
        rules = await pre_flight_checks()
        return await asyncio.to_thread(_sync_call, rules)
    except Exception as e:
        logger.error(f"Exceção em close_partial_position (async): {e}", exc_info=True)
        return {"success": False, "error": str(e)}

async def modify_position_stop_loss(
    api_key: str,
    api_secret: str,
    symbol: str,
    new_stop_loss: float,
    reason: Optional[str] = None  # "be" | "ts" | "lock" | None
) -> dict:
    """
    Modifica o Stop Loss com:
      - Arredondamento no tick (down p/ LONG, up p/ SHORT)
      - Folga mínima de N ticks vs lastPrice (TF_SAFETY_TICKS, default 2)
      - Retry inteligente para retCode 10001: até 3 tentativas (0.2s, 0.4s, 0.8s + jitter)
    Trata "not modified" como sucesso.
    """
    try:
        # --- 1) Regras do instrumento (tickSize) ---
        instrument_rules = await get_instrument_info(symbol)
        if not instrument_rules.get("success"):
            error_msg = instrument_rules.get("error", f"Regras do instrumento {symbol} não encontradas.")
            logger.error(f"Falha ao obter regras para {symbol} antes de modificar SL: {error_msg}")
            return {"success": False, "error": error_msg}

        tick_size = instrument_rules.get("tickSize", Decimal("0"))
        if tick_size <= 0:
            return {"success": False, "error": f"tickSize inválido para {symbol}."}

        # --- 2) Safety ticks do ambiente (default 2) ---
        try:
            safety_ticks = int(os.getenv("TF_SAFETY_TICKS", "2"))
        except Exception:
            safety_ticks = 2
        if safety_ticks < 0:
            safety_ticks = 0

        desired_sl = Decimal(str(new_stop_loss))

        # --- 3) Tentativas com backoff ---
        backoffs = [0.0, 0.2, 0.4, 0.8]  # primeira sem espera
        last_error = None
        last_telemetry = {}

        for attempt, delay in enumerate(backoffs, start=1):
            if delay > 0:
                await asyncio.sleep(delay + random.uniform(0.0, 0.05))

            def _sync_attempt():
                try:
                    session = get_session(api_key, api_secret)

                    # 3.1 Descobre lado da posição (Buy/Sell) p/ este símbolo
                    pos_resp = session.get_positions(category="linear", symbol=symbol)
                    pos_list = (pos_resp.get("result", {}) or {}).get("list", []) or []
                    pos = next((p for p in pos_list if float(p.get("size") or 0) > 0), pos_list[0] if pos_list else None)
                    side_api = (pos.get("side") if pos else None) or "Buy"
                    side_norm = "LONG" if side_api == "Buy" else "SHORT"

                    # 3.2 Busca lastPrice mais recente
                    t = session.get_tickers(category="linear", symbol=symbol)
                    lst = (t.get("result", {}) or {}).get("list", [])
                    if not lst:
                        return {"ok": False, "error": "Ticker vazio.", "attempt": attempt}
                    last_price = Decimal(str(lst[0].get("lastPrice")))

                    # 3.3 Arredonda ao tick conforme lado
                    if side_norm == "LONG":
                        rounded = _round_down_to_tick(desired_sl, tick_size)
                    else:
                        rounded = _round_up_to_tick(desired_sl, tick_size)

                    # 3.4 Aplica folga mínima de N ticks vs lastPrice
                    adjusted = _apply_safety_ticks(side_norm, rounded, last_price, tick_size, safety_ticks)

                    logger.info(
                        "[sl:set] symbol=%s attempt=%d reason=%s side=%s original=%s rounded=%s last=%s ticks=%d adjusted=%s",
                        symbol, attempt, (reason or "n/a"), side_api, str(desired_sl), str(rounded), str(last_price), safety_ticks, str(adjusted)
                    )

                    # 3.5 Envia para a Bybit
                    payload = {"category": "linear", "symbol": symbol, "stopLoss": str(adjusted)}
                    resp = session.set_trading_stop(**payload)

                    if resp.get("retCode") == 0:
                        return {"ok": True, "data": resp.get("result"), "attempt": attempt}

                    # Falha: empacota info p/ decisão de retry fora da thread
                    return {
                        "ok": False,
                        "error": resp.get("retMsg"),
                        "retCode": resp.get("retCode"),
                        "attempt": attempt,
                        "telemetry": {
                            "last": str(last_price),
                            "desired": str(desired_sl),
                            "rounded": str(rounded),
                            "adjusted": str(adjusted),
                            "ticks": safety_ticks,
                            "side": side_api
                        }
                    }

                except InvalidRequestError as e:
                    msg = str(e)
                    # Bybit usa 10001 e mensagens “should lower/higher than base_price”
                    rc = 10001 if "10001" in msg else None
                    return {
                        "ok": False,
                        "error": msg,
                        "retCode": rc,
                        "attempt": attempt,
                    }
                except Exception as e:
                    logger.error("Exceção em modify_position_stop_loss (attempt=%d): %s", attempt, e, exc_info=True)
                    return {"ok": False, "error": str(e), "attempt": attempt}

            result = await asyncio.to_thread(_sync_attempt)

            # Sucesso?
            if result.get("ok"):
                return {"success": True, "data": result.get("data"), "attempt": result.get("attempt")}

            # Falha: decide se reintenta
            last_error = result.get("error")
            last_telemetry = result.get("telemetry", {})

            # Tratar "not modified" como sucesso silencioso
            if last_error and "not modified" in last_error.lower():
                logger.info("[sl:set] symbol=%s attempt=%d reason=%s already-in-place -> success",
                            symbol, result.get("attempt"), (reason or "n/a"))
                return {"success": True, "data": {"note": "not modified"}, "attempt": result.get("attempt")}

            # Só reintenta em 10001 / mensagens de base price
            retryable = False
            rc = result.get("retCode")
            if rc == 10001:
                retryable = True
            elif last_error:
                le = last_error.lower()
                if ("base price" in le) or ("should lower than base_price" in le) or ("should higher than base_price" in le):
                    retryable = True

            if retryable and attempt < len(backoffs):
                logger.warning("[sl:retry] symbol=%s attempt=%d reason=%s err=%s", symbol, result.get("attempt"), (reason or "n/a"), last_error)
                continue
            else:
                break  # não-retryable ou esgotou tentativas

        # Esgotou tentativas / falha não-retryable
        logger.error("[sl:failed] symbol=%s reason=%s err=%s telemetry=%s",
                     symbol, (reason or "n/a"), last_error, last_telemetry)
        return {"success": False, "error": last_error or "unknown error", "telemetry": last_telemetry}

    except Exception as e:
        logger.error(f"Exceção na lógica de modificar Stop Loss para {symbol}: {e}", exc_info=True)
        return {"success": False, "error": str(e)}

async def get_open_positions(api_key: str, api_secret: str) -> dict:
    return await get_open_positions_with_pnl(api_key, api_secret)

async def get_pnl_for_period(api_key: str, api_secret: str, start_time: datetime, end_time: datetime) -> dict:
    """Busca o P/L (Lucro/Prejuízo) realizado para um período de tempo específico."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            
            start_timestamp_ms = int(start_time.timestamp() * 1000)
            end_timestamp_ms = int(end_time.timestamp() * 1000)

            response = session.get_closed_pnl(
                category="linear",
                startTime=start_timestamp_ms,
                endTime=end_timestamp_ms,
                limit=200 # Aumentar o limite para buscar mais trades em períodos longos
            )

            if response.get('retCode') == 0:
                pnl_list = response.get('result', {}).get('list', [])
                total_pnl = sum(float(item.get('closedPnl', 0)) for item in pnl_list)
                return {"success": True, "pnl": total_pnl}
            else:
                error_msg = response.get('retMsg', 'Erro desconhecido ao buscar P/L.')
                logger.error(f"Erro da API Bybit ao buscar P/L: {error_msg}")
                return {"success": False, "error": error_msg}

        except Exception as e:
            logger.error(f"Exceção em get_pnl_for_period: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    return await asyncio.to_thread(_sync_call)


async def get_daily_pnl(api_key: str, api_secret: str) -> dict:
    """Busca o P/L realizado para o dia atual (agora usa a função genérica)."""
    today_start = datetime.combine(datetime.today(), time.min)
    now = datetime.now()
    return await get_pnl_for_period(api_key, api_secret, today_start, now)


# --- FUNÇÃO PARA ENVIAR ORDEM LIMITE ---
async def place_limit_order(api_key: str, api_secret: str, signal_data: dict, user_settings: User, balance: float) -> dict:
    """Envia uma nova ordem limite para a Bybit com validação completa."""
    async def pre_flight_checks():
        symbol = signal_data['coin']
        if symbol not in INSTRUMENT_INFO_CACHE:
            await get_instrument_info(symbol)
        return INSTRUMENT_INFO_CACHE.get(symbol)

    def _sync_call(instrument_rules: Dict[str, Any]):
        try:
            symbol = signal_data['coin']
            
            if not instrument_rules or not instrument_rules.get("success"):
                return instrument_rules or {"success": False, "error": f"Regras para {symbol} não encontradas."}
            if instrument_rules["status"] != "Trading":
                return {"success": False, "error": f"O símbolo {symbol} não está ativo para negociação ({instrument_rules['status']})."}

            session = get_session(api_key, api_secret)
            side = "Buy" if (signal_data.get('order_type') or '').upper() == 'LONG' else "Sell"
            leverage = Decimal(str(user_settings.max_leverage))
            tick = instrument_rules["tickSize"]

            price = Decimal(str(signal_data.get('limit_price')))
            price_adj = _round_down_to_tick(price, tick)

            margin_in_dollars = Decimal(str(balance)) * (Decimal(str(user_settings.entry_size_percent)) / Decimal("100"))
            notional_value = margin_in_dollars * leverage

            if price_adj <= 0:
                return {"success": False, "error": f"Preço de entrada inválido após ajuste: {price_adj}"}
            
            qty_raw = notional_value / price_adj
            qty_adj = _round_down_to_step(qty_raw, instrument_rules["qtyStep"])
            
            if qty_adj < instrument_rules["minOrderQty"]:
                return {"success": False, "error": f"Qtd. ajustada ({qty_adj:f}) é menor que a mínima permitida ({instrument_rules['minOrderQty']:f}) para {symbol}."}
            final_notional_value = qty_adj * price_adj
            if final_notional_value < instrument_rules["minNotionalValue"]:
                return {"success": False, "error": f"Valor total da ordem (${final_notional_value:.2f}) é menor que o mínimo permitido de ${instrument_rules['minNotionalValue']:.2f}."}

            # --- Ajuste de TP/SL ao tick do instrumento ---
            tp_raw = (signal_data.get('targets') or [None])[0]
            sl_raw = signal_data.get('stop_loss')

            take_profit_adj = None
            if tp_raw is not None:
                take_profit_adj = _round_down_to_tick(Decimal(str(tp_raw)), tick)

            stop_loss_adj = None
            if sl_raw is not None:
                stop_loss_adj = _round_down_to_tick(Decimal(str(sl_raw)), tick)

            payload = {
                "category": "linear", "symbol": symbol, "side": side,
                "orderType": "Limit", "qty": str(qty_adj), "price": str(price_adj),
                "takeProfit": str(take_profit_adj) if take_profit_adj is not None else None,
                "stopLoss": str(stop_loss_adj) if stop_loss_adj is not None else None,
            }

            try:
                session.set_leverage(category="linear", symbol=symbol, buyLeverage=str(leverage), sellLeverage=str(leverage))
            except InvalidRequestError as e:
                if "leverage not modified" in str(e).lower():
                    logger.warning(f"Alavancagem para {symbol} já está correta. Continuando...")
                else:
                    return {"success": False, "error": str(e)}

            _safe_log_order_payload("place_limit_order:first_try", payload)
            response = session.place_order(**{k: v for k, v in payload.items() if v is not None})
            if response.get('retCode') == 0:
                return {"success": True, "data": response['result']}
            return {"success": False, "error": response.get('retMsg')}

        except Exception as e:
            logger.error(f"Exceção ao abrir ordem (Limit): {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    try:
        rules = await pre_flight_checks()
        return await asyncio.to_thread(_sync_call, rules)
    except Exception as e:
        logger.error(f"Exceção em place_limit_order (async): {e}", exc_info=True)
        return {"success": False, "error": str(e)}

# --- FUNÇÃO PARA VERIFICAR STATUS DE UMA ORDEM ---
async def get_order_status(api_key: str, api_secret: str, order_id: str, symbol: str) -> dict:
    """Verifica o status de uma ordem específica na Bybit, procurando em ordens abertas."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            # --- CORREÇÃO: MUDAMOS PARA get_open_orders ---
            response = session.get_open_orders(
                category="linear",
                symbol=symbol,
                orderId=order_id,
            )
            if response.get('retCode') == 0:
                order_list = response.get('result', {}).get('list', [])
                if order_list:
                    # A ordem foi encontrada na lista de ordens abertas
                    return {"success": True, "data": order_list[0]}
                else:
                    # Se não está nas ordens abertas, pode já ter sido executada ou cancelada.
                    # Por segurança, vamos verificar o histórico também.
                    hist_response = session.get_order_history(category="linear", orderId=order_id)
                    if hist_response.get('retCode') == 0:
                        hist_list = hist_response.get('result', {}).get('list', [])
                        if hist_list:
                            return {"success": True, "data": hist_list[0]}
                    
                    return {"success": False, "error": "Ordem não encontrada nem nas abertas nem no histórico."}
            else:
                return {"success": False, "error": response.get('retMsg')}
        except Exception as e:
            logger.error(f"Exceção ao verificar status da ordem: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    return await asyncio.to_thread(_sync_call)


# --- FUNÇÃO PARA CANCELAR UMA ORDEM ---
async def cancel_order(api_key: str, api_secret: str, order_id: str, symbol: str) -> dict:
    """Cancela uma ordem limite pendente na Bybit."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            response = session.cancel_order(
                category="linear",
                symbol=symbol,
                orderId=order_id
            )
            if response.get('retCode') == 0:
                return {"success": True, "data": response['result']}
            else:
                return {"success": False, "error": response.get('retMsg')}
        except Exception as e:
            logger.error(f"Exceção ao cancelar ordem: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    return await asyncio.to_thread(_sync_call)

# --- PNL FECHADO (PERFORMANCE) ---
async def get_closed_pnl_breakdown(api_key: str, api_secret: str, start_time: datetime, end_time: datetime) -> dict:
    """
    Retorna o P/L total e contagem de ganhos/perdas no período informado.
    Usa o endpoint oficial de closed PnL e pagina os resultados se o período for > 7 dias.
    """
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)

            total_pnl = 0.0
            total_wins = 0
            total_losses = 0
            all_items = []

            current_start = start_time

            while current_start < end_time:
                # Calcula o fim da janela atual, limitado a 7 dias ou ao fim do período total
                current_end = min(current_start + timedelta(days=7), end_time)

                logger.info(f"[bybit_service] Buscando PnL de {current_start.strftime('%Y-%m-%d')} a {current_end.strftime('%Y-%m-%d')}")

                resp = session.get_closed_pnl(
                    category="linear",
                    startTime=int(current_start.timestamp() * 1000),
                    endTime=int(current_end.timestamp() * 1000),
                    limit=200,
                )

                if resp.get("retCode") != 0:
                    error_msg = resp.get("retMsg", f"Erro desconhecido na paginação de PnL (start={current_start})")
                    logger.error(f"Erro da API Bybit em get_closed_pnl_breakdown: {error_msg}")
                    # Retorna o erro da primeira falha
                    return {"success": False, "error": error_msg}

                items = resp.get("result", {}).get("list", []) or []
                all_items.extend(items)

                # Avança o início da próxima janela
                current_start += timedelta(days=7)

            # Processa a lista completa de itens coletados
            for it in all_items:
                pnl = float(it.get("closedPnl", 0) or 0)
                total_pnl += pnl
                if pnl > 0:
                    total_wins += 1
                elif pnl < 0:
                    total_losses += 1

            return {
                "success": True,
                "total_pnl": total_pnl,
                "wins": total_wins,
                "losses": total_losses,
                "trades": len(all_items),
            }
        except Exception as e:
            logger.error(f"Exceção em get_closed_pnl_breakdown: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    return await asyncio.to_thread(_sync_call)


# --- POSIÇÕES ABERTAS COM PNL ATUAL ---
async def get_open_positions_with_pnl(api_key: str, api_secret: str) -> dict:
    """
    Lista posições abertas com avgPrice, markPrice e P/L atual (valor e fração),
    deduplicando por (symbol, side, positionIdx). Se houver duplicatas, mantém a de maior size.
    """
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            resp = session.get_positions(category="linear", settleCoin="USDT")
            if resp.get("retCode") != 0:
                return {"success": False, "error": resp.get("retMsg", "erro")}

            seen = {}  # key: (symbol, side, positionIdx) -> item
            positions = (resp.get("result", {}).get("list", []) or [])
            for pos in positions:
                size = float(pos.get("size", 0) or 0)
                if size <= 0:
                    continue

                symbol = pos.get("symbol")
                pos_side_api = (pos.get("side") or "").strip()  # "Buy" | "Sell"
                side = "LONG" if pos_side_api == "Buy" else "SHORT"
                entry = float(pos.get("avgPrice", 0) or 0)
                mark = float((pos.get("markPrice") or 0) or 0)
                pos_idx = int(pos.get("positionIdx", 0))
                key = (symbol, side, pos_idx)

                # Fallback de preço se mark vier 0
                if not mark and symbol:
                    try:
                        t = session.get_tickers(category="linear", symbol=symbol)
                        mark = float(t["result"]["list"][0]["lastPrice"])
                    except Exception:
                        pass

                if entry > 0 and mark > 0:
                    diff = (mark - entry) if side == "LONG" else (entry - mark)
                    pnl = diff * size
                    pnl_frac = (diff / entry) if entry else 0.0  # fração (ex.: 0.015 = 1.5%)
                else:
                    pnl = 0.0
                    pnl_frac = 0.0

                item = {
                    "symbol": symbol,
                    "side": side,
                    "size": size,
                    "entry": entry,
                    "mark": mark,
                    "unrealized_pnl": pnl,
                    "unrealized_pnl_frac": pnl_frac,  # padronizado em FRAÇÃO
                    "position_idx": pos_idx,
                }

                if key in seen:
                    # mantém a maior posição e loga para auditoria
                    if size > float(seen[key]["size"]):
                        logger.info(
                            "[positions:dedupe:replace] key=%s old_size=%.8f new_size=%.8f",
                            key, float(seen[key]["size"]), size
                        )
                        seen[key] = item
                    else:
                        logger.info(
                            "[positions:dedupe:skip] key=%s keep_size=%.8f skip_size=%.8f",
                            key, float(seen[key]["size"]), size
                        )
                else:
                    seen[key] = item

            out = list(seen.values())
            return {"success": True, "data": out}
        except Exception as e:
            logger.error(f"Exceção em get_open_positions_with_pnl: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    return await asyncio.to_thread(_sync_call)

async def get_specific_position_size(api_key: str, api_secret: str, symbol: str) -> float:
    """
    Busca o tamanho (size) de uma posição específica aberta na Bybit.
    Retorna sempre um float (0.0 em caso de inexistência ou erro).
    """
    def _sync_call() -> float:
        try:
            session = get_session(api_key, api_secret)
            response = session.get_positions(category="linear", symbol=symbol)

            if response.get('retCode') == 0:
                position_list = (response.get('result', {}) or {}).get('list', []) or []
                if position_list and position_list[0]:
                    # Retorna o tamanho da primeira posição na lista (mantém comportamento atual)
                    size_val = float(position_list[0].get('size', 0.0) or 0.0)
                    return size_val
                # Lista vazia: posição não existe
                return 0.0

            # retCode != 0: loga e retorna 0.0
            logger.warning(f"get_specific_position_size: Bybit retornou retCode={response.get('retCode')} para {symbol} - {response.get('retMsg')}")
            return 0.0

        except Exception as e:
            logger.error(f"Exceção em get_specific_position_size para {symbol}: {e}", exc_info=True)
            # Nunca retorne None: padroniza para 0.0
            return 0.0

    return await asyncio.to_thread(_sync_call)

    
async def get_order_history(api_key: str, api_secret: str, order_id: str) -> dict:
    """Busca os detalhes de uma ordem específica no histórico."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            response = session.get_order_history(category="linear", orderId=order_id, limit=1)
            
            if response.get('retCode') == 0:
                order_list = response.get('result', {}).get('list', [])
                if order_list:
                    return {"success": True, "data": order_list[0]}
                return {"success": False, "error": "Ordem não encontrada no histórico."}
            return {"success": False, "error": response.get('retMsg')}
        
        except Exception as e:
            logger.error(f"Exceção em get_order_history: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    return await asyncio.to_thread(_sync_call)

async def modify_position_take_profit(api_key: str, api_secret: str, symbol: str, new_take_profit: float) -> dict:
    """Modifica o Take Profit de uma posição aberta, garantindo a precisão do preço (tick size)."""
    try:
        # Busca regras/tick do instrumento (com cache)
        instrument_rules = await get_instrument_info(symbol)
        if not instrument_rules.get("success"):
            error_msg = instrument_rules.get("error", f"Regras do instrumento {symbol} não encontradas.")
            logger.error(f"Falha ao obter regras para {symbol} antes de modificar TP: {error_msg}")
            return {"success": False, "error": error_msg}

        tick_size = instrument_rules.get("tickSize", Decimal("0"))
        tp_price_decimal = Decimal(str(new_take_profit))
        rounded_tp_price = _round_down_to_tick(tp_price_decimal, tick_size)

        logger.info(f"Modificando TP para {symbol}: Original: {tp_price_decimal}, Arredondado ({tick_size}): {rounded_tp_price}")

        def _sync_call():
            try:
                session = get_session(api_key, api_secret)
                payload = {"category": "linear", "symbol": symbol, "takeProfit": str(rounded_tp_price)}
                _safe_log_order_payload("modify_tp", payload)
                response = session.set_trading_stop(**payload)
                if response.get('retCode') == 0:
                    return {"success": True, "data": response['result']}
                return {"success": False, "error": response.get('retMsg')}
            except InvalidRequestError as e:
                if "not modified" in str(e).lower():
                    logger.info(f"TP para {symbol} já está em {rounded_tp_price}. Tratando como sucesso.")
                    return {"success": True, "data": {"note": "not modified"}}
                raise e

        return await asyncio.to_thread(_sync_call)

    except Exception as e:
        logger.error(f"Exceção na lógica de modificar Take Profit para {symbol}: {e}", exc_info=True)
        return {"success": False, "error": str(e)}

async def get_last_closed_trade_info(api_key: str, api_secret: str, symbol: str) -> dict:
    """
    Função "Detetive" aprimorada: cruza dados de PnL e histórico de ordens
    para determinar com mais precisão o resultado de um trade que já fechou.
    """
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            end_time = datetime.now()
            start_time = end_time - timedelta(hours=2) # Aumenta a janela para 2h por segurança
            
            # 1. Busca o PnL fechado mais recente para obter o PnL e o ID da ordem de fechamento
            pnl_response = session.get_closed_pnl(
                category="linear",
                symbol=symbol,
                startTime=int(start_time.timestamp() * 1000),
                endTime=int(end_time.timestamp() * 1000),
                limit=1
            )

            if pnl_response.get('retCode') != 0 or not pnl_response.get('result', {}).get('list'):
                return {"success": False, "error": "Nenhum PnL fechado encontrado para o símbolo recentemente."}
            
            pnl_data = pnl_response['result']['list'][0]
            closing_order_id = pnl_data.get('orderId')
            
            # Prepara o resultado final com os dados do PnL
            final_data = {
                "closedPnl": pnl_data.get('closedPnl', 0.0),
                "exitType": "Unknown" # Começa com 'Unknown' como fallback
            }

            if not closing_order_id:
                logger.warning(f"Detetive: PnL encontrado para {symbol}, mas sem orderId. Retornando 'Unknown'.")
                return {"success": True, "data": final_data}

            # 2. Busca os detalhes da ordem de fechamento para obter o motivo real
            order_hist_response = session.get_order_history(
                category="linear",
                orderId=closing_order_id
            )

            if order_hist_response.get('retCode') == 0 and order_hist_response.get('result', {}).get('list'):
                order_data = order_hist_response['result']['list'][0]
                stop_order_type = order_data.get('stopOrderType', '').strip()

                if stop_order_type == 'TakeProfit':
                    final_data['exitType'] = 'TakeProfit'
                elif stop_order_type == 'StopLoss':
                    final_data['exitType'] = 'StopLoss'
                # Outros tipos de ordens (como fechamento manual via ordem a mercado) não terão stopOrderType.
                # Nesses casos, o fallback 'Unknown' será mantido, o que é o comportamento esperado.
            
            return {"success": True, "data": final_data}

        except Exception as e:
            logger.error(f"Exceção em get_last_closed_trade_info: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    return await asyncio.to_thread(_sync_call)

def _safe_log_order_payload(context: str, payload: Dict[str, Any]) -> None:
    """
    Loga, de forma segura, os campos relevantes de um payload de ordem.
    Não loga credenciais nem headers. Apenas dados não sensíveis.
    """
    try:
        preview = {
            "category": payload.get("category"),
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "orderType": payload.get("orderType"),
            "qty": payload.get("qty"),
            "price": payload.get("price", "omitted"),
            "takeProfit": payload.get("takeProfit", "omitted"),
            "stopLoss": payload.get("stopLoss", "omitted"),
            "reduceOnly": payload.get("reduceOnly", False),
            "positionIdx": payload.get("positionIdx", "omitted"),
        }
        logger.info(f"[order_payload:{context}] {preview}")
    except Exception as e:
        logger.warning(f"[order_payload:{context}] falha ao logar preview: {e}")
