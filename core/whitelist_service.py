import logging
from typing import Set

logger = logging.getLogger(__name__)

# --- CATEGORIAS DE MOEDAS ---
# Usamos Sets para performance e para evitar duplicatas.
# As listas foram expandidas para incluir o máximo de ativos relevantes dentro das categorias originais.

# Moedas "blue chips" clássicas, de altíssima capitalização.
BLUECHIPS: Set[str] = {
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT'
}

# Moedas de grande capitalização e projetos estabelecidos (Layer 1 e Layer 2)
ALTCOINS_L1_L2: Set[str] = {
    'ADAUSDT', 'ALGOUSDT', 'APTUSDT', 'ARBUSDT', 'ATOMUSDT', 'AVAXUSDT', 'DOTUSDT', 
    'EGLDUSDT', 'EOSUSDT', 'ETCUSDT', 'FTMUSDT', 'HBARUSDT', 'ICPUSDT', 'IMXUSDT', 
    'INJUSDT', 'KASUSDT', 'KSMUSDT', 'LTCUSDT', 'MATICUSDT', 'MINAUSDT', 'NEARUSDT', 
    'OPUSDT', 'SEIUSDT', 'STXUSDT', 'SUIUSDT', 'TIAUSDT', 'TONUSDT', 'TRXUSDT', 
    'VETUSDT', 'XLMUSDT', 'XMRUSDT', 'XRPUSDT', 'XTZUSDT', 'ZECUSDT', 'ZENUSDT'
}

# Moedas relacionadas a Finanças Descentralizadas (DeFi)
DEFI: Set[str] = {
    '1INCHUSDT', 'AAVEUSDT', 'BALUSDT', 'CAKEUSDT', 'COMPUSDT', 'CRVUSDT', 
    'CVXUSDT', 'DYDXUSDT', 'GMXUSDT', 'JUPUSDT', 'KNCUSDT', 'LDOUSDT', 'LINKUSDT', 
    'LRCUSDT', 'MKRUSDT', 'PENDLEUSDT', 'RUNEUSDT', 'SNXUSDT', 'SUSHIUSDT', 
    'UMAUSDT', 'UNIUSDT', 'WOOUSDT', 'YFIUSDT', 'ZRXUSDT'
}

# Moedas de "memes" com alta volatilidade
MEMECOINS: Set[str] = {
    'BONKUSDT', 'DOGEUSDT', 'FLOKIUSDT', 'MEMEUSDT', 'ORDIUSDT', 'PEPEUSDT', 
    'SATSUSDT', 'SHIBUSDT', 'WIFUSDT'
}

# Camada de infraestrutura, oráculos e DePIN (Redes de Infraestrutura Física Descentralizada)
INFRA: Set[str] = {
    'ANKRUSDT', 'ARUSDT', 'BTTUSDT', 'FILUSDT', 'GRTUSDT', 'HNTUSDT', 'LINKUSDT', 
    'OCEANUSDT', 'RNDRUSDT', 'STORJUSDT', 'THETAUSDT'
}


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
        if category_keyword in CATEGORIES and symbol.upper() in CATEGORIES[category_keyword]:
            return True
            
    # Se nenhuma das condições acima for atendida, a moeda não está na whitelist
    return False