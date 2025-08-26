import logging
import aiohttp
from typing import Optional

logger = logging.getLogger(__name__)

async def get_usd_to_brl_rate() -> Optional[float]:
    """
    Busca a taxa de conversão de USD para BRL de uma API pública.
    Retorna a taxa como float ou None em caso de falha.
    """
    # Usamos uma API simples e gratuita que não requer chave.
    url = "https://api.exchangerate-api.com/v4/latest/USD"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as response:
                if response.status == 200:
                    data = await response.json()
                    rate = data.get("rates", {}).get("BRL")
                    if rate:
                        logger.info(f"Taxa de conversão USD-BRL obtida: {rate}")
                        return float(rate)
                    else:
                        logger.warning("Campo 'BRL' não encontrado na resposta da API de cotação.")
                        return None
                else:
                    logger.error(f"Falha ao buscar cotação BRL. Status: {response.status}")
                    return None
    except Exception as e:
        logger.error(f"Exceção ao buscar cotação BRL: {e}", exc_info=True)
        return None