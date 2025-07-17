from pybit.unified_trading import HTTP
from database.models import User
import logging

logger = logging.getLogger(__name__)

def get_session(api_key: str, api_secret: str) -> HTTP:
    """Cria e retorna uma sessão HTTP autenticada com a Bybit."""
    return HTTP(
        testnet=False,
        api_key=api_key,
        api_secret=api_secret,
        recv_window=20000  # Aumenta o prazo de validade da requisição para 20s
    )

def get_account_info(api_key: str, api_secret: str) -> dict:
    """Busca informações da conta na Bybit, como o saldo."""
    try:
        session = get_session(api_key, api_secret)
        response = session.get_wallet_balance(accountType="UNIFIED")
        
        if response.get('retCode') == 0:
            return {"success": True, "data": response['result']['list'][0]}
        else:
            return {"success": False, "error": response.get('retMsg', 'Erro desconhecido da API')}
    except Exception as e:
        logger.error(f"Exceção ao buscar informações da conta: {e}")
        return {"success": False, "error": str(e)}

def place_order(api_key: str, api_secret: str, signal_data: dict, user_settings: User, balance: float) -> dict:
    """
    Abre uma nova posição usando as configurações de risco do usuário.
    """
    try:
        session = get_session(api_key, api_secret)
        
        # 1. Extrai dados do sinal
        symbol = signal_data['coin']
        side = "Buy" if signal_data['order_type'] == 'LONG' else "Sell"
        leverage = str(user_settings.max_leverage) # Usa a alavancagem do usuário
        entry_price = signal_data['entries'][0]
        stop_loss = str(signal_data['stop_loss'])
        take_profit = str(signal_data['targets'][0])

        # 2. LÓGICA DE RISCO DINÂMICA
        risk_percent = user_settings.risk_per_trade_percent
        dollar_amount_to_risk = balance * (risk_percent / 100)
        
        # Calcula a distância do stop em %
        stop_loss_distance_percent = abs(entry_price - float(stop_loss)) / entry_price
        
        # Calcula o tamanho total da posição
        position_size_dollars = dollar_amount_to_risk / stop_loss_distance_percent
        
        # Calcula a quantidade de moedas (qty)
        qty = round(position_size_dollars / entry_price, 3) 

        logger.info(f"Cálculo de Risco: Saldo=${balance:,.2f}, Risco={risk_percent}%, Valor em Risco=${dollar_amount_to_risk:,.2f}")
        logger.info(f"Calculando ordem para {symbol}: Side={side}, Qty={qty}, Leverage={leverage}")


        # 3. Definir a alavancagem para o Símbolo
        session.set_leverage(category="linear", symbol=symbol, buyLeverage=leverage, sellLeverage=leverage)

        # 4. Abrir a Ordem a Mercado
        response = session.place_order(
            category="linear",
            symbol=symbol,
            side=side,
            orderType="Market",
            qty=str(qty),
            takeProfit=take_profit,
            stopLoss=stop_loss,
            isLeverage=1 # Informa que é uma ordem com alavancagem
        )

        logger.info(f"Resposta da Bybit ao abrir ordem: {response}")
        
        if response.get('retCode') == 0:
            return {"success": True, "data": response['result']}
        else:
            # Erros comuns: "insufficient balance", "order cost not match", etc.
            return {"success": False, "error": response.get('retMsg')}

    except Exception as e:
        logger.error(f"Exceção ao abrir ordem: {e}")
        return {"success": False, "error": str(e)}
    
def get_market_price(symbol: str) -> dict:
    """Busca o preço de mercado atual para um símbolo específico."""
    try:
        # Usamos uma sessão não autenticada para dados de mercado públicos
        session = HTTP(testnet=True)
        response = session.get_tickers(category="linear", symbol=symbol)
        
        if response.get('retCode') == 0 and response['result']['list']:
            price = float(response['result']['list'][0]['lastPrice'])
            return {"success": True, "price": price}
        else:
            return {"success": False, "error": response.get('retMsg', 'Não foi possível buscar o preço')}
    except Exception as e:
        logger.error(f"Exceção ao buscar preço de mercado para {symbol}: {e}")
        return {"success": False, "error": str(e)}
    

def close_partial_position(api_key: str, api_secret: str, symbol: str, qty_to_close: float, side: str) -> dict:
    """
    Fecha uma parte de uma posição aberta.
    Para fechar um LONG, nós vendemos (side="Sell").
    Para fechar um SHORT, nós compramos (side="Buy").
    """
    logger.info(f"Tentando fechar {qty_to_close} de {symbol}...")
    try:
        session = get_session(api_key, api_secret)
        # O lado da ordem de fechamento é o oposto da posição original
        close_side = "Sell" if side == 'LONG' else "Buy"
        
        response = session.place_order(
            category="linear",
            symbol=symbol,
            side=close_side,
            orderType="Market",
            qty=str(qty_to_close),
            reduceOnly=True # Essencial: garante que a ordem apenas reduza sua posição
        )
        
        if response.get('retCode') == 0:
            return {"success": True, "data": response['result']}
        else:
            return {"success": False, "error": response.get('retMsg')}
    except Exception as e:
        logger.error(f"Exceção ao fechar posição parcial: {e}")
        return {"success": False, "error": str(e)}


def modify_position_stop_loss(api_key: str, api_secret: str, symbol: str, new_stop_loss: float) -> dict:
    """Modifica o Stop Loss de uma posição aberta."""
    logger.info(f"Tentando mover o Stop Loss de {symbol} para {new_stop_loss}...")
    try:
        session = get_session(api_key, api_secret)
        response = session.set_trading_stop(
            category="linear",
            symbol=symbol,
            stopLoss=str(new_stop_loss)
        )
        
        if response.get('retCode') == 0:
            return {"success": True, "data": response['result']}
        else:
            return {"success": False, "error": response.get('retMsg')}
    except Exception as e:
        logger.error(f"Exceção ao modificar Stop Loss: {e}")
        return {"success": False, "error": str(e)}

def get_open_positions(api_key: str, api_secret: str) -> dict:
    """Busca todas as posições abertas na conta de derivativos."""
    logger.info("Buscando posições abertas na Bybit...")
    try:
        session = get_session(api_key, api_secret)
        response = session.get_positions(
            category="linear", # Para futuros USDT
            settleCoin="USDT"
        )
        
        if response.get('retCode') == 0:
            # Filtra para retornar apenas posições que estão de fato abertas (com size > 0)
            open_positions = [p for p in response['result']['list'] if float(p['size']) > 0]
            return {"success": True, "data": open_positions}
        else:
            return {"success": False, "error": response.get('retMsg')}
    except Exception as e:
        logger.error(f"Exceção ao buscar posições abertas: {e}")
        return {"success": False, "error": str(e)}