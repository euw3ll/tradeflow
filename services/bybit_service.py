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
            stop_loss_price = str(signal_data['stop_loss'])
            take_profit_price = str(signal_data['targets'][0]) if signal_data.get('targets') else None
            
            # --- LÓGICA DE CÁLCULO DE TAMANHO DA ORDEM ATUALIZADA ---
            entry_percent = user_settings.entry_size_percent
            position_size_dollars = balance * (entry_percent / 100)
            
            # Arredonda a quantidade para o número de casas decimais correto para a Bybit
            qty = round(position_size_dollars / entry_price, 3) 
            
            logger.info(f"Calculando ordem para {symbol}: Side={side}, Qty={qty}, Leverage={leverage}, Size=${position_size_dollars:.2f}")

            session.set_leverage(category="linear", symbol=symbol, buyLeverage=leverage, sellLeverage=leverage)

            response = session.place_order(
                category="linear", symbol=symbol, side=side, orderType="Market",
                qty=str(qty), takeProfit=take_profit_price, stopLoss=stop_loss_price, isLeverage=1
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
    """Envia uma nova ordem limite para a Bybit."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            symbol = signal_data['coin']
            side = "Buy" if signal_data['order_type'] == 'LONG' else "Sell"
            leverage = str(user_settings.max_leverage)
            entry_price = float(signal_data['entries'][0])
            stop_loss_price = str(signal_data['stop_loss'])
            take_profit_price = str(signal_data['targets'][0]) if signal_data.get('targets') else None

            # Lógica de cálculo de tamanho da ordem (a mesma que já usamos)
            entry_percent = user_settings.entry_size_percent
            position_size_dollars = balance * (entry_percent / 100)
            qty = round(position_size_dollars / entry_price, 3)
            
            logger.info(f"Calculando ORDEM LIMITE para {symbol}: Side={side}, Qty={qty}, Price={entry_price}")

            session.set_leverage(category="linear", symbol=symbol, buyLeverage=leverage, sellLeverage=leverage)

            response = session.place_order(
                category="linear",
                symbol=symbol,
                side=side,
                orderType="Limit", # <-- TIPO DE ORDEM
                qty=str(qty),
                price=str(entry_price), # <-- PREÇO DE ENTRADA
                takeProfit=take_profit_price,
                stopLoss=stop_loss_price,
                isLeverage=1
            )
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
    """Verifica o status de uma ordem específica na Bybit."""
    def _sync_call():
        try:
            session = get_session(api_key, api_secret)
            response = session.get_order_history(
                category="linear",
                orderId=order_id,
                # symbol=symbol # Opcional, mas ajuda a refinar a busca
            )
            if response.get('retCode') == 0:
                order_list = response.get('result', {}).get('list', [])
                if order_list:
                    # Retorna o primeiro resultado, que deve ser nossa ordem
                    return {"success": True, "data": order_list[0]}
                return {"success": False, "error": "Ordem não encontrada no histórico."}
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