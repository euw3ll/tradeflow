import logging
import asyncio
from typing import Dict
from datetime import datetime, time
from pybit.unified_trading import HTTP
from pybit.exceptions import InvalidRequestError
from database.models import User
from decimal import Decimal

logger = logging.getLogger(__name__)

def _round_down_to_step(value: Decimal, step: Decimal) -> Decimal:
    # arredonda para baixo no múltiplo do step
    if step <= 0:
        return value
    return (value // step) * step

def _round_down_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return price
    return (price // tick) * tick

def _get_symbol_filters(session: HTTP, symbol: str):
    """
    Busca os filtros de preço/quantidade do símbolo (tickSize, qtyStep, minOrderQty).
    Retorna (tick: Decimal, step: Decimal, min_qty: Decimal)
    """
    resp = session.get_instruments_info(category="linear", symbol=symbol)
    if resp.get("retCode") != 0:
        raise RuntimeError(f"Falha ao obter instruments_info: {resp.get('retMsg')}")
    lst = ((resp.get("result") or {}).get("list") or [])
    if not lst:
        raise RuntimeError("instruments_info vazio para símbolo")
    info = lst[0]
    price_filter = (info.get("priceFilter") or {})
    lot_filter = (info.get("lotSizeFilter") or {})
    tick = Decimal(str(price_filter.get("tickSize", "0.0001")))
    step = Decimal(str(lot_filter.get("qtyStep", "0.001")))
    min_qty = Decimal(str(lot_filter.get("minOrderQty", "0")))
    return tick, step, min_qty

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
    """Abre uma nova posição a mercado (Market) respeitando qtyStep/minOrderQty da Bybit."""
    def _sync_call():
        from decimal import Decimal
        try:
            session = get_session(api_key, api_secret)

            symbol = signal_data['coin']
            order_type = (signal_data.get('order_type') or '').upper()
            side = "Buy" if order_type == 'LONG' else "Sell"
            leverage = str(user_settings.max_leverage)

            # preço de referência do sinal para sizing (não é enviado na ordem Market)
            entry_price = Decimal(str(signal_data['entries'][0]))
            stop_loss_price = signal_data.get('stop_loss')
            take_profit_price = (signal_data.get('targets') or [None])[0]

            # ----- filtros do símbolo (tick/step/minQty) -----
            try:
                tick, step, min_qty = _get_symbol_filters(session, symbol)
            except Exception as e:
                logger.warning(f"[bybit_service] Falha ao obter filtros para {symbol}, usando defaults. Erro: {e}")
                tick, step, min_qty = Decimal("0.0001"), Decimal("0.001"), Decimal("0")

            # ----- sizing em dólares -> qty (contratos) -----
            entry_percent = Decimal(str(user_settings.entry_size_percent))
            balance_dec = Decimal(str(balance))
            position_size_dollars = balance_dec * (entry_percent / Decimal("100"))

            if entry_price <= 0:
                return {"success": False, "error": f"Preço de entrada inválido para sizing: {entry_price}"}

            qty_raw = position_size_dollars / entry_price
            qty_dec = _round_down_to_step(qty_raw, step)

            if qty_dec <= 0:
                return {"success": False, "error": f"Quantidade após ajuste ficou zero (raw={qty_raw}, step={step})."}

            if qty_dec < min_qty:
                # eleva ao mínimo permitido
                qty_dec = min_qty

            logger.info(
                f"Calculando ORDEM A MERCADO para {symbol}: "
                f"Side={side}, Qty={qty_dec} (step={step}, minQty={min_qty}), "
                f"posSize=${position_size_dollars}, refPrice={entry_price}"
            )

            # ----- alavancagem -----
            try:
                session.set_leverage(category="linear", symbol=symbol, buyLeverage=leverage, sellLeverage=leverage)
            except InvalidRequestError as e:
                if "leverage not modified" in str(e).lower():
                    logger.warning(f"Alavancagem para {symbol} já está correta. Continuando...")
                else:
                    raise

            # ----- envio da Market -----
            payload = {
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": "Market",
                "qty": str(qty_dec),
                "isLeverage": 1,
            }
            if take_profit_price is not None:
                payload["takeProfit"] = str(take_profit_price)
            if stop_loss_price is not None:
                payload["stopLoss"] = str(stop_loss_price)

            logger.info(f"[bybit_service] Enviando MARKET {payload}")
            response = session.place_order(**payload)

            if response.get('retCode') == 0:
                return {"success": True, "data": response['result']}
            else:
                return {"success": False, "error": response.get('retMsg')}

        except Exception as e:
            logger.error(f"Exceção ao abrir ordem (Market): {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    return await asyncio.to_thread(_sync_call)


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
    """Fecha uma parte de uma posição aberta de forma assíncrona."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            close_side = "Sell" if side == 'LONG' else "Buy"
            
            response = session.place_order(
                category="linear", symbol=symbol, side=close_side,
                orderType="Market", qty=str(qty_to_close), reduceOnly=True
            )
            if response.get('retCode') == 0:
                return {"success": True, "data": response['result']}
            else:
                return {"success": False, "error": response.get('retMsg')}
        except Exception as e:
            logger.error(f"Exceção ao fechar posição parcial: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    return await asyncio.to_thread(_sync_call)

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
    """Busca todas as posições abertas de forma assíncrona."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            response = session.get_positions(category="linear", settleCoin="USDT")
            if response.get('retCode') == 0:
                open_positions = [p for p in response['result']['list'] if float(p['size']) > 0]
                return {"success": True, "data": open_positions}
            else:
                return {"success": False, "data": [], "error": response.get('retMsg')}
        except Exception as e:
            logger.error(f"Exceção ao buscar posições abertas: {e}", exc_info=True)
            return {"success": False, "data": [], "error": str(e)}
    return await asyncio.to_thread(_sync_call)

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
    """Envia uma nova ordem limite para a Bybit (respeita limit_price + aplica tick/step)."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            symbol = signal_data['coin']
            order_type = (signal_data.get('order_type') or '').upper()
            side = "Buy" if order_type == 'LONG' else "Sell"
            leverage = str(user_settings.max_leverage)

            # ---------- (A) DEFINIÇÃO DO PREÇO LIMIT ----------
            # 1) Usa limit_price vindo do trade_manager se presente:
            price = signal_data.get('limit_price')
            # 2) Senão, decide aqui com base na faixa de entries:
            if price is None:
                entries = (signal_data.get('entries') or [])[:2]
                if len(entries) == 0:
                    return {"success": False, "error": "Sem preços de entrada válidos para LIMIT"}
                elif len(entries) == 1:
                    price = float(entries[0])
                else:
                    lo = float(min(entries[0], entries[1]))
                    hi = float(max(entries[0], entries[1]))
                    price = lo if order_type == "LONG" else hi

            # TP/SL (opcionais)
            stop_loss_price = signal_data.get('stop_loss')
            take_profit_price = (signal_data.get('targets') or [None])[0]

            # ---------- (B) BUSCA DE FILTROS (tick/step) E AJUSTES ----------
            try:
                tick, step, min_qty = _get_symbol_filters(session, symbol)
            except Exception as e:
                logger.warning(f"[bybit_service] Falha ao obter filtros do símbolo, usando defaults. Erro: {e}")
                # Defaults conservadores para não travar (ajuste se preferir forçar erro)
                tick, step, min_qty = Decimal("0.0001"), Decimal("0.001"), Decimal("0")

            # Ajuste do preço ao tick
            price_dec = _round_down_to_tick(Decimal(str(price)), tick)

            # ---------- Cálculo de quantidade (usa preço já ajustado) ----------
            entry_percent = user_settings.entry_size_percent
            position_size_dollars = Decimal(str(balance)) * (Decimal(str(entry_percent)) / Decimal("100"))
            if price_dec <= 0:
                return {"success": False, "error": f"Preço inválido após ajuste: {price_dec}"}

            qty_raw = position_size_dollars / price_dec
            qty_dec = _round_down_to_step(qty_raw, step)

            # Garante minQty e > 0
            if qty_dec <= 0:
                return {"success": False, "error": f"Quantidade calculada é zero/negativa (raw={qty_raw}, step={step})."}
            if qty_dec < min_qty:
                # tenta elevar ao mínimo permitido
                qty_dec = min_qty

            # ---------- Logs e alavancagem ----------
            logger.info(
                f"Calculando ORDEM LIMITE para {symbol}: "
                f"Side={side}, Qty={qty_dec}, Price={price_dec} "
                f"(tick={tick}, step={step}, minQty={min_qty}, posSize=${position_size_dollars})"
            )

            try:
                session.set_leverage(category="linear", symbol=symbol, buyLeverage=leverage, sellLeverage=leverage)
            except InvalidRequestError as e:
                if "leverage not modified" in str(e).lower():
                    logger.warning(f"Alavancagem para {symbol} já está correta. Continuando...")
                else:
                    raise

            # ---------- Envio da ordem ----------
            payload = {
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": "Limit",
                "qty": str(qty_dec),
                "price": str(price_dec),
                "isLeverage": 1,
            }
            if take_profit_price is not None:
                payload["takeProfit"] = str(take_profit_price)
            if stop_loss_price is not None:
                payload["stopLoss"] = str(stop_loss_price)

            logger.info(f"[bybit_service] Enviando LIMIT {payload}")
            response = session.place_order(**payload)

            if response.get('retCode') == 0:
                return {"success": True, "data": response['result']}
            else:
                return {"success": False, "error": response.get('retMsg')}

        except Exception as e:
            logger.error(f"Exceção ao enviar ordem limite: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    return await asyncio.to_thread(_sync_call)

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