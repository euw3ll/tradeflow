import re
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# --- DEFINIÇÃO CENTRALIZADA DOS TIPOS DE SINAL ---
class SignalType:
    MARKET = 'MARKET'
    LIMIT = 'LIMIT'
    CANCELAR = 'CANCELAR'


# -----------------------
# Helpers de normalização
# -----------------------
_FLOAT = r'[-+]?\d+(?:[.,]\d+)?'

def _to_float(x: str) -> float:
    """Converte string com vírgula ou ponto para float."""
    if x is None:
        return 0.0
    x = x.strip().replace(' ', '').replace(',', '.')
    # remove percentuais e símbolos residuais
    x = re.sub(r'[^0-9.+-]', '', x)
    try:
        return float(x)
    except Exception:
        return 0.0

def _normalize_symbol(coin_raw: str) -> str:
    coin = (coin_raw or '').strip().upper()
    # remove emojis e lixo
    coin = re.sub(r'[^A-Z0-9]', '', coin)
    # alguns sinais usam par completo (ex.: AVAXUSDT)
    if coin.endswith('USDT') or coin.endswith('USD'):
        return coin if coin.endswith('USDT') else f'{coin}T'  # USD -> USDT (fail-safe)
    return f'{coin}USDT' if coin else coin

def _pick_first_number(text: str) -> Optional[float]:
    m = re.search(_FLOAT, text)
    return _to_float(m.group(0)) if m else None

def _findall_numbers(text: str) -> List[float]:
    return [_to_float(g) for g in re.findall(_FLOAT, text or '')]


# ----------------------------------------------------
# Padrões de alto nível (ordem importa: específicos 1º)
# ----------------------------------------------------
CANCEL_PATTERN = re.compile(r'⚠️\s*([A-Za-z0-9]+)[^\n]*sinal\s*cancelad[oa]', re.IGNORECASE)

# “Ordem Limite” / “Ordem a/à Mercado” podem aparecer em qualquer lugar
IS_MARKET_PATTERN = re.compile(r'Ordem\s*(?:à|a)?\s*Mercado', re.IGNORECASE)
IS_LIMIT_PATTERN  = re.compile(r'Ordem\s*Limite', re.IGNORECASE)

# Verificador de “sinal completo”
FULL_SIGNAL_GUARD = re.compile(r'(?=.*(?:Moeda|Coin|Pair)\s*:)(?=.*Tipo\s*:)(?=.*Stop\s*Loss\s*:)', re.IGNORECASE | re.DOTALL)


# ---------------------------
# Extrator de sinal “completo”
# ---------------------------
def _full_signal_extractor(message_text: str) -> Optional[Dict[str, Any]]:

    def find_single_value(pattern: str, text: str) -> Optional[str]:
        # Usa .*? para pular emojis e rótulos adicionais na linha
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        return m.group(1).strip() if m else None

    # --- Campos básicos ---
    coin_raw = find_single_value(r'(?:Moeda|Coin|Pair)\s*:\s*([A-Za-z0-9 ._-]+)', message_text)
    order_type_raw = find_single_value(r'Tipo\s*:\s*([A-Za-z ]+)', message_text)
    entry_raw = find_single_value(r'Zona\s*de\s*Entrada\s*:\s*([^\n\r]+)', message_text)
    sl_raw = find_single_value(r'Stop\s*Loss\s*:\s*([^\n\r]+)', message_text)

    # targets: T1:, T2:, ...
    targets = []
    for tlabel, val in re.findall(r'(?:^|\n)\s*T(\d+)\s*:\s*([^\n\r]+)', message_text, flags=re.IGNORECASE):
        n = _pick_first_number(val)
        if n is not None:
            targets.append(n)

    # confiança (se existir)
    conf_raw = find_single_value(r'Confian[çc]a\s*:\s*([0-9.,]+)\s*%', message_text)
    confidence = _to_float(conf_raw) if conf_raw else None

    # normalizações
    coin = _normalize_symbol(coin_raw or '')
    order_type = 'LONG'
    if order_type_raw:
        if 'SHORT' in order_type_raw.upper():
            order_type = 'SHORT'
        elif 'LONG' in order_type_raw.upper():
            order_type = 'LONG'

    # entradas
    entries: List[float] = []
    if entry_raw:
        nums = _findall_numbers(entry_raw)
        # muitos sinais colocam "x - y"; se só tem um número, trata como lista única
        if len(nums) == 1:
            entries = [nums[0]]
        elif len(nums) >= 2:
            entries = [nums[0], nums[1]]
        else:
            entries = []

    # stop
    stop_loss = _pick_first_number(sl_raw or '') or 0.0

    # --- Determinação do tipo (MARKET x LIMIT) ---
    # 1) texto explícito
    is_market_text = bool(IS_MARKET_PATTERN.search(message_text))
    is_limit_text  = bool(IS_LIMIT_PATTERN.search(message_text))

    # 2) heurística: “entrada única” OU faixa idêntica => MARKET
    entries_imply_market = False
    if entries:
        if len(entries) == 1:
            entries_imply_market = True
        elif len(entries) >= 2 and abs(entries[0] - entries[1]) < 1e-10:
            entries_imply_market = True

    # decisão final
    if is_market_text or (not is_limit_text and entries_imply_market):
        signal_kind = SignalType.MARKET
        # para MARKET garantimos entries[0] preenchida (usa o primeiro número visto no bloco de entrada)
        if not entries and entry_raw:
            n = _pick_first_number(entry_raw)
            entries = [n] if n is not None else []
    else:
        signal_kind = SignalType.LIMIT

    if not coin or not entries or stop_loss == 0.0:
        logger.debug("Parser: campos essenciais ausentes: coin=%s entries=%s stop=%s", coin, entries, stop_loss)
        return None

    return {
        "type": signal_kind,
        "coin": coin,
        "order_type": order_type,          # LONG | SHORT
        "entries": entries,                # [preço] ou [min, max]
        "stop_loss": stop_loss,
        "targets": targets,                # [t1, t2, ...]
        "confidence": confidence,          # opcional (float ou None)
    }


# -----------------
# Função de entrada
# -----------------
def parse_signal(message_text: str) -> Optional[Dict[str, Any]]:
    """
    Identifica e extrai sinais:
      - CANCELAMENTO: '⚠️ <COIN> ... sinal cancelad(o/a)'
      - ENTRADA COMPLETA: campos Moeda/Coin/Pair, Tipo, Stop Loss (com 'Ordem Limite' ou 'Ordem à Mercado')
    Retorna um dicionário com os campos normalizados ou None se não reconhecer.
    """
    if not message_text or not isinstance(message_text, str):
        return None

    text = message_text.strip()

    # 1) Cancelamento
    m_cancel = CANCEL_PATTERN.search(text)
    if m_cancel:
        coin = _normalize_symbol(m_cancel.group(1))
        return {"type": SignalType.CANCELAR, "coin": coin}

    # 2) Sinal de entrada (guarda)
    if not FULL_SIGNAL_GUARD.search(text):
        return None

    data = _full_signal_extractor(text)
    return data