import logging
import asyncio
from typing import Dict, Any
from datetime import datetime, time, timedelta
from pybit.unified_trading import HTTP
from pybit.exceptions import InvalidRequestError
from database.models import User
from decimal import Decimal, ROUND_DOWN

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

async def get_instrument_info(symbol: str) -> Dict[str, Any]:
    """
    Busca as regras de um instrumento (símbolo) da Bybit, usando um cache em memória.
    Retorna um dicionário com as regras ou um erro.
    """
    if symbol in INSTRUMENT_INFO_CACHE:
        return INSTRUMENT_INFO_CACHE[symbol]

    def _sync_call():
        try:
            session = HTTP(testnet=False)
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
        api_secret=api_secret
    )

async def get_account_info(api_key: str, api_secret: str) -> dict:
    """Busca informações da conta de forma assíncrona."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            response = session.get_wallet_balance(accountType="UNIFIED")
            if response.get('retCode') == 0:
                return {"success": True, "data": response['result']['list']}
            return {"success": False, "data": [], "error": response.get('retMsg', 'Erro desconhecido')}
        except Exception as e:
            logger.error(f"Exceção em get_account_info: {e}", exc_info=True)
            return {"success": False, "data": [], "error": str(e)}
    return await asyncio.to_thread(_sync_call)

async def place_order(api_key: str, api_secret: str, signal_data: dict, user_settings: User, balance: float) -> dict:
    """Abre uma nova posição a mercado (Market) com validação completa."""
    async def pre_flight_checks():
        # Garante que as regras do instrumento estão no cache antes de ir para a thread
        symbol = signal_data['coin']
        if symbol not in INSTRUMENT_INFO_CACHE:
            await get_instrument_info(symbol)
        return INSTRUMENT_INFO_CACHE.get(symbol)

    def _sync_call(instrument_rules: Dict[str, Any]):
        try:
            symbol = signal_data['coin']

            # 1. VALIDAÇÃO DAS REGRAS (JÁ BUSCADAS)
            if not instrument_rules or not instrument_rules.get("success"):
                 return instrument_rules or {"success": False, "error": f"Regras para {symbol} não encontradas."}

            if instrument_rules["status"] != "Trading":
                return {"success": False, "error": f"O símbolo {symbol} não está ativo para negociação ({instrument_rules['status']})."}

            # 2. CÁLCULO DE QUANTIDADE (Com Alavancagem)
            session = get_session(api_key, api_secret)
            side = "Buy" if (signal_data.get('order_type') or '').upper() == 'LONG' else "Sell"
            leverage = Decimal(str(user_settings.max_leverage))
            entry_price = Decimal(str(signal_data['entries'][0]))
            
            margin_in_dollars = Decimal(str(balance)) * (Decimal(str(user_settings.entry_size_percent)) / Decimal("100"))
            notional_value = margin_in_dollars * leverage
            
            if entry_price <= 0: return {"success": False, "error": f"Preço de entrada inválido: {entry_price}"}

            qty_raw = notional_value / entry_price
            qty_adj = _round_down_to_step(qty_raw, instrument_rules["qtyStep"])

            # 3. VALIDAÇÃO DA ORDEM
            if qty_adj < instrument_rules["minOrderQty"]:
                return {"success": False, "error": f"Qtd. ajustada ({qty_adj:f}) é menor que a mínima permitida ({instrument_rules['minOrderQty']:f}) para {symbol}."}
            
            final_notional_value = qty_adj * entry_price
            if final_notional_value < instrument_rules["minNotionalValue"]:
                return {"success": False, "error": f"Valor total da ordem (${final_notional_value:.2f}) é menor que o mínimo permitido de ${instrument_rules['minNotionalValue']:.2f}."}

            # 4. EXECUÇÃO
            session.set_leverage(category="linear", symbol=symbol, buyLeverage=str(leverage), sellLeverage=str(leverage))
            
            payload = {
                "category": "linear", "symbol": symbol, "side": side,
                "orderType": "Market", "qty": str(qty_adj), "isLeverage": 1,
                "takeProfit": str((signal_data.get('targets') or [None])[0]),
                "stopLoss": str(signal_data.get('stop_loss')),
            }
            response = session.place_order(**{k: v for k, v in payload.items() if v is not None})

            if response.get('retCode') == 0:
                return {"success": True, "data": response['result']}
            return {"success": False, "error": response.get('retMsg')}

        except InvalidRequestError as e:
            if "leverage not modified" in str(e).lower(): logger.warning(f"Alavancagem para {symbol} já está correta. Continuando...")
            else: raise e # Re-levanta a exceção para ser tratada abaixo
        
        # O bloco de execução da ordem é repetido aqui para tratar o 'leverage not modified'
        # sem chamar set_leverage novamente.
        payload['qty'] = str(qty_adj) # Garante que a qty correta seja usada
        response_retry = session.place_order(**{k: v for k, v in payload.items() if v is not None})
        if response_retry.get('retCode') == 0: return {"success": True, "data": response_retry['result']}
        return {"success": False, "error": response_retry.get('retMsg')}

    try:
        rules = await pre_flight_checks()
        return await asyncio.to_thread(_sync_call, rules)
    except Exception as e:
        logger.error(f"Exceção ao abrir ordem (Market): {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def get_market_price(symbol: str) -> dict:
    """Busca o preço de mercado atual de forma assíncrona."""
    def _sync_call():
        try:
            session = HTTP(testnet=False)
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

async def close_partial_position(api_key: str, api_secret: str, symbol: str, qty_to_close: float, side: str) -> dict:
    """Fecha parte de uma posição com Market/ReduceOnly, usando o novo sistema de regras."""
    async def pre_flight_checks():
        if symbol not in INSTRUMENT_INFO_CACHE:
            await get_instrument_info(symbol)
        return INSTRUMENT_INFO_CACHE.get(symbol)

    def _sync_call(instrument_rules: Dict[str, Any]):
        try:
            # 1. VALIDAÇÃO DAS REGRAS
            if not instrument_rules or not instrument_rules.get("success"):
                return instrument_rules or {"success": False, "error": f"Regras para {symbol} não encontradas."}

            session = get_session(api_key, api_secret)
            close_side = "Sell" if side == 'LONG' else "Buy"

            # 2. CÁLCULO DE QUANTIDADE
            qty_raw = Decimal(str(qty_to_close))
            qty_adj = _round_down_to_step(qty_raw, instrument_rules["qtyStep"])

            logger.info(f"[bybit_service] close_partial {symbol}: raw={qty_raw}, step={instrument_rules['qtyStep']}, minQty={instrument_rules['minOrderQty']} => adj={qty_adj}")

            if qty_adj < instrument_rules["minOrderQty"]:
                # Se a quantidade a ser fechada for menor que o mínimo, ignoramos a operação
                # Isso não é um erro, apenas não há o que fazer.
                logger.warning(f"Quantidade a fechar para {symbol} ({qty_adj:f}) é menor que o mínimo permitido. Ignorando fechamento parcial.")
                return {"success": True, "skipped": True, "reason": "qty_less_than_min_order_qty"}

            # 3. EXECUÇÃO
            response = session.place_order(
                category="linear", symbol=symbol, side=close_side,
                orderType="Market", qty=str(qty_adj), reduceOnly=True
            )
            if response.get('retCode') == 0:
                return {"success": True, "data": response['result']}
            else:
                return {"success": False, "error": response.get('retMsg')}

        except Exception as e:
            logger.error(f"Exceção ao fechar posição parcial: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    try:
        rules = await pre_flight_checks()
        return await asyncio.to_thread(_sync_call, rules)
    except Exception as e:
        logger.error(f"Exceção em close_partial_position (async): {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def modify_position_stop_loss(api_key: str, api_secret: str, symbol: str, new_stop_loss: float) -> dict:
    """Modifica o Stop Loss de uma posição aberta de forma assíncrona."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            response = session.set_trading_stop(
                category="linear", symbol=symbol, stopLoss=str(new_stop_loss)
            )
            if response.get('retCode') == 0:
                return {"success": True, "data": response['result']}
            else:
                return {"success": False, "error": response.get('retMsg')}
        except Exception as e:
            logger.error(f"Exceção ao modificar Stop Loss: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    return await asyncio.to_thread(_sync_call)

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
            
            # 1. VALIDAÇÃO DAS REGRAS
            if not instrument_rules or not instrument_rules.get("success"):
                return instrument_rules or {"success": False, "error": f"Regras para {symbol} não encontradas."}

            if instrument_rules["status"] != "Trading":
                return {"success": False, "error": f"O símbolo {symbol} não está ativo para negociação ({instrument_rules['status']})."}

            # 2. CÁLCULO DE PREÇO E QUANTIDADE
            session = get_session(api_key, api_secret)
            side = "Buy" if (signal_data.get('order_type') or '').upper() == 'LONG' else "Sell"
            leverage = Decimal(str(user_settings.max_leverage))
            price = Decimal(str(signal_data.get('limit_price')))
            
            price_adj = (price // instrument_rules["tickSize"]) * instrument_rules["tickSize"]

            margin_in_dollars = Decimal(str(balance)) * (Decimal(str(user_settings.entry_size_percent)) / Decimal("100"))
            notional_value = margin_in_dollars * leverage

            if price_adj <= 0: return {"success": False, "error": f"Preço de entrada inválido após ajuste: {price_adj}"}
            
            qty_raw = notional_value / price_adj
            qty_adj = _round_down_to_step(qty_raw, instrument_rules["qtyStep"])
            
            # 3. VALIDAÇÃO DA ORDEM
            if qty_adj < instrument_rules["minOrderQty"]:
                return {"success": False, "error": f"Qtd. ajustada ({qty_adj:f}) é menor que a mínima permitida ({instrument_rules['minOrderQty']:f}) para {symbol}."}

            final_notional_value = qty_adj * price_adj
            if final_notional_value < instrument_rules["minNotionalValue"]:
                return {"success": False, "error": f"Valor total da ordem (${final_notional_value:.2f}) é menor que o mínimo permitido de ${instrument_rules['minNotionalValue']:.2f}."}

            # 4. EXECUÇÃO
            session.set_leverage(category="linear", symbol=symbol, buyLeverage=str(leverage), sellLeverage=str(leverage))

            payload = {
                "category": "linear", "symbol": symbol, "side": side,
                "orderType": "Limit", "qty": str(qty_adj), "price": str(price_adj), "isLeverage": 1,
                "takeProfit": str((signal_data.get('targets') or [None])[0]),
                "stopLoss": str(signal_data.get('stop_loss')),
            }
            response = session.place_order(**{k: v for k, v in payload.items() if v is not None})
            
            if response.get('retCode') == 0:
                return {"success": True, "data": response['result']}
            return {"success": False, "error": response.get('retMsg')}
        
        except InvalidRequestError as e:
            if "leverage not modified" in str(e).lower(): logger.warning(f"Alavancagem para {symbol} já está correta. Retentando a ordem...")
            else: raise e

            payload['qty'] = str(qty_adj)
            response_retry = session.place_order(**{k: v for k, v in payload.items() if v is not None})
            if response_retry.get('retCode') == 0: return {"success": True, "data": response_retry['result']}
            return {"success": False, "error": response_retry.get('retMsg')}

    try:
        rules = await pre_flight_checks()
        return await asyncio.to_thread(_sync_call, rules)
    except Exception as e:
        logger.error(f"Exceção ao abrir ordem (Limit): {e}", exc_info=True)
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
    Lista posições abertas com avgPrice, markPrice e P/L atual (valor e %).
    """
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            resp = session.get_positions(category="linear", settleCoin="USDT")
            if resp.get("retCode") != 0:
                return {"success": False, "error": resp.get("retMsg", "erro")}
            out = []
            for p in (resp.get("result", {}).get("list", []) or []):
                size = float(p.get("size", 0) or 0)
                if size <= 0:
                    continue
                symbol = p.get("symbol")
                side = "LONG" if (p.get("side") == "Buy") else "SHORT"
                entry = float(p.get("avgPrice", 0) or 0)
                mark = float((p.get("markPrice") or 0) or 0)
                # se mark vier 0, tenta buscar via tickers
                if not mark and symbol:
                    try:
                        t = session.get_tickers(category="linear", symbol=symbol)
                        mark = float(t["result"]["list"][0]["lastPrice"])
                    except Exception:
                        pass
                if not entry or not mark:
                    # não dá pra calcular PnL sem preço
                    pnl = 0.0
                    pnl_pct = 0.0
                else:
                    diff = (mark - entry) if side == "LONG" else (entry - mark)
                    pnl = diff * size
                    pnl_pct = (diff / entry) * 100.0 if entry else 0.0
                out.append({
                    "symbol": symbol,
                    "side": side,
                    "size": size,
                    "entry": entry,
                    "mark": mark,
                    "unrealized_pnl": pnl,
                    "unrealized_pnl_pct": pnl_pct,
                })
            return {"success": True, "data": out}
        except Exception as e:
            logger.error(f"Exceção em get_open_positions_with_pnl: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    return await asyncio.to_thread(_sync_call)

# Adicione esta nova função em services/bybit_service.py
# Pode ser adicionada após a função modify_position_stop_loss

async def get_specific_position_size(api_key: str, api_secret: str, symbol: str) -> float:
    """
    Busca o tamanho (size) de uma posição específica aberta na Bybit.
    Retorna 0.0 se a posição não existir.
    """
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            # Usamos o filtro de símbolo para buscar apenas a posição de interesse
            response = session.get_positions(category="linear", symbol=symbol)
            
            if response.get('retCode') == 0:
                position_list = response.get('result', {}).get('list', [])
                if position_list and position_list[0]:
                    # Retorna o tamanho da primeira (e única) posição na lista
                    return float(position_list[0].get('size', 0.0))
            # Se a lista estiver vazia ou houver erro, a posição não existe ou não foi encontrada
            return 0.0
        except Exception as e:
            logger.error(f"Exceção em get_specific_position_size para {symbol}: {e}", exc_info=True)
            return 0.0 # Em caso de erro, assumimos que não há posição para evitar fechamentos indevidos

    return await asyncio.to_thread(_sync_call)