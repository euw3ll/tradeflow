import logging
from typing import Set

logger = logging.getLogger(__name__)

# --- CATEGORIAS DE MOEDAS ---
# Estas listas podem ser expandidas no futuro. Usamos Sets para performance.

# Moedas de grande capitalização e projetos estabelecidos (excluindo BTC e ETH)
ALTCOINS_L1_L2: Set[str] = {
    'SOLUSDT', 'XRPUSDT', 'ADAUSDT', 'AVAXUSDT', 'DOTUSDT', 'MATICUSDT',
    'LINKUSDT', 'TRXUSDT', 'ATOMUSDT', 'NEARUSDT', 'APTUSDT', 'OPUSDT',
    'ARBUSDT', 'LDOUSDT', 'SUIUSDT'
}

# Moedas relacionadas a Finanças Descentralizadas
DEFI: Set[str] = {
    'UNIUSDT', 'AAVEUSDT', 'MKRUSDT', 'SNXUSDT', 'COMPUSDT', 'CRVUSDT',
    'SUSHIUSDT', 'YFIUSDT'
}

# Moedas de "memes" com alta volatilidade
MEMECOINS: Set[str] = {
    'DOGEUSDT', 'SHIBUSDT', 'PEPEUSDT', 'WIFUSDT', 'FLOKIUSDT', 'BONKUSDT'
}

# Moedas "blue chips" clássicas
BLUECHIPS: Set[str] = {'BTCUSDT', 'ETHUSDT', 'BNBUSDT'}

# Camada de infraestrutura / oráculos
INFRA: Set[str] = {'LINKUSDT', 'GRTUSDT', 'FILUSDT'}

# Dicionário que mapeia a palavra-chave da categoria para o Set de moedas
CATEGORIES = {
    'bluechips': BLUECHIPS,
    'altcoins': ALTCOINS_L1_L2,
    'defi': DEFI,
    'infra': INFRA,
    'memecoins': MEMECOINS,
}

def is_coin_in_whitelist(symbol: str, user_whitelist_str: str) -> bool:
    """
    Verifica se um símbolo de moeda está na whitelist de um usuário.

    A whitelist pode conter:
    - O keyword 'todas'.
    - Símbolos específicos (ex: 'btcusdt').
    - Keywords de categorias (ex: 'memecoins').
    """
    if not user_whitelist_str or 'todas' in user_whitelist_str.lower():
        return True

    # Normaliza a entrada do usuário: minúsculas, remove espaços, divide por vírgula
    user_list = {item.strip() for item in user_whitelist_str.lower().split(',')}
    
    # 1. Verifica se o símbolo exato está na lista do usuário
    if symbol.lower() in user_list:
        return True

    # 2. Verifica se alguma das categorias da lista do usuário contém o símbolo
    for category_keyword in user_list:
        if category_keyword in CATEGORIES and symbol in CATEGORIES[category_keyword]:
            return True
            
    # Se nenhuma das condições acima for atendida, a moeda não está na whitelist
    return False