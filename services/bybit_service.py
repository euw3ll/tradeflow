import logging
import asyncio
from typing import Dict
from datetime import datetime, time
from pybit.unified_trading import HTTP
from database.models import User

logger = logging.getLogger(__name__)

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
    """Abre uma nova posição de forma assíncrona."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            symbol = signal_data['coin']
            side = "Buy" if signal_data['order_type'] == 'LONG' else "Sell"
            leverage = str(user_settings.max_leverage)
            entry_price = signal_data['entries'][0]
            stop_loss_price = float(signal_data['stop_loss'])
            take_profit_price = str(signal_data['targets'][0]) if signal_data.get('targets') else None
            
            risk_percent = user_settings.risk_per_trade_percent
            dollar_amount_to_risk = balance * (risk_percent / 100)
            
            stop_loss_distance_percent = abs(entry_price - stop_loss_price) / entry_price
            if stop_loss_distance_percent == 0:
                return {"success": False, "error": "Distância do Stop Loss é zero."}

            position_size_dollars = dollar_amount_to_risk / stop_loss_distance_percent
            qty = round(position_size_dollars / entry_price, 3) 
            
            logger.info(f"Calculando ordem para {symbol}: Side={side}, Qty={qty}, Leverage={leverage}")

            session.set_leverage(category="linear", symbol=symbol, buyLeverage=leverage, sellLeverage=leverage)

            response = session.place_order(
                category="linear", symbol=symbol, side=side, orderType="Market",
                qty=str(qty), takeProfit=take_profit_price, stopLoss=str(stop_loss_price), isLeverage=1
            )
            if response.get('retCode') == 0:
                return {"success": True, "data": response['result']}
            else:
                return {"success": False, "error": response.get('retMsg')}
        except Exception as e:
            logger.error(f"Exceção ao abrir ordem: {e}", exc_info=True)
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

async def get_daily_pnl(api_key: str, api_secret: str) -> dict:
    """Busca o P/L (Lucro/Prejuízo) realizado para o dia atual."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            
            # Define o início do dia de hoje (meia-noite) em milissegundos
            today_start_dt = datetime.combine(datetime.today(), time.min)
            start_timestamp_ms = int(today_start_dt.timestamp() * 1000)

            response = session.get_closed_pnl(
                category="linear",
                startTime=start_timestamp_ms,
                limit=100 # Limite de trades fechados para buscar
            )

            if response.get('retCode') == 0:
                pnl_list = response.get('result', {}).get('list', [])
                total_pnl = sum(float(item.get('closedPnl', 0)) for item in pnl_list)
                return {"success": True, "pnl": total_pnl}
            else:
                error_msg = response.get('retMsg', 'Erro desconhecido ao buscar P/L.')
                logger.error(f"Erro da API Bybit ao buscar P/L diário: {error_msg}")
                return {"success": False, "error": error_msg}

        except Exception as e:
            logger.error(f"Exceção em get_daily_pnl: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    return await asyncio.to_thread(_sync_call)