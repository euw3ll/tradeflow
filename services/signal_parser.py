import re
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# --- DEFINIÇÃO CENTRALIZADA E SIMPLIFICADA DOS TIPOS DE SINAL ---
class SignalType:
    MARKET = 'MARKET'
    LIMIT = 'LIMIT'
    CANCELAR = 'CANCELAR'

# --- ESTRUTURA DE PADRÕES DE REGEX (VERSÃO AUTÔNOMA) ---
# A ordem é importante: o padrão mais específico (cancelamento) vem antes.
SIGNAL_PATTERNS = [
    {
        "type": SignalType.CANCELAR,
        # Aceita variações como "⚠️ BTC - Sinal Cancelado" ou "⚠️ BTC Sinal Cancelada"
        "pattern": re.compile(r'⚠️\s*(\w+)[^\n]*sinal\s*cancelad[oa]', re.IGNORECASE),
        "extractor": lambda m: {"coin": m.group(1)}
    },
    {
        "type": "FULL_SIGNAL", # Padrão para sinais completos (Ordem Limite ou a Mercado)
        "pattern": re.compile(
            r'(?=.*(?:Moeda|Coin|Pair):)(?=.*Tipo:)(?=.*Stop\s*Loss:)',
            re.IGNORECASE | re.DOTALL,
        ),
        "extractor": "full_signal_extractor"
    }
]

def _full_signal_extractor(message_text: str) -> Optional[Dict[str, Any]]:
    """
    Função dedicada para extrair todos os detalhes de um sinal de entrada completo.
    """
    def find_single_value(pattern: str, text: str) -> Optional[str]:
        match = re.search(pattern, text, re.IGNORECASE)
        return match.group(1).strip() if match else None

    def find_multiple_values(pattern: str, text: str) -> List[float]:
        matches = re.findall(pattern, text, re.IGNORECASE)
        return [float(v.replace(',', '.')) for v in matches]

    text_lower = message_text.lower()
    signal_type = None
    if 'ordem limite' in text_lower:
        signal_type = SignalType.LIMIT
    elif 'ordem à mercado' in text_lower or 'sinal entrou no preço' in text_lower:
        signal_type = SignalType.MARKET

    coin = find_single_value(r'(?:💎\s*)?(?:Moeda|Coin|Pair):\s*(\w+)', message_text)
    order_type = find_single_value(r'Tipo:\s*(LONG|SHORT)', message_text)
    entry_zone_str = find_single_value(r'Zona\s*de\s*Entrada:\s*([\d\.\,\s-]+)', message_text)
    stop_loss_str = find_single_value(r'Stop\s*Loss:\s*([\d\.\,]+)', message_text)
    targets = find_multiple_values(r'T\d+:\s*([\d\.\,]+)', message_text)
    confidence_str = find_single_value(r'Confiança:\s*([\d\.\,]+)%', message_text)

    # Validação essencial: Se não for um sinal de entrada completo, retorna None
    if not all([signal_type, coin, order_type, entry_zone_str, stop_loss_str]):
        logger.debug("[Parser] Mensagem não corresponde a um sinal de entrada completo. Ignorando.")
        return None

    entries = [float(val.replace(',', '.')) for val in re.findall(r'([\d\.\,]+)', entry_zone_str)]
    if not entries:
        logger.warning("[Parser] Nenhum preço numérico encontrado na 'Zona de Entrada' de um sinal completo.")
        return None

    return {
        "type": signal_type,
        "coin": coin,
        "order_type": order_type.upper(),
        "entries": entries,
        "stop_loss": float(stop_loss_str.replace(',', '.')),
        "targets": targets,
        "confidence": float(confidence_str.replace(',', '.')) if confidence_str else 0.0
    }


def parse_signal(message_text: str) -> Optional[Dict[str, Any]]:
    """
    Analisa a mensagem de texto e a compara com uma lista de padrões de regex
    para extrair o tipo de sinal e os dados relevantes.
    """
    for item in SIGNAL_PATTERNS:
        match = item["pattern"].search(message_text)
        if not match:
            continue

        logger.info(f"[Parser] Padrão '{item['type']}' correspondido.")
        
        if item["extractor"] == "full_signal_extractor":
            extracted_data = _full_signal_extractor(message_text)
        else:
            extracted_data = item["extractor"](match)
        
        if not extracted_data:
            continue
        
        # O tipo do sinal vem do padrão, não do extrator (exceto para FULL_SIGNAL)
        if item["type"] != "FULL_SIGNAL":
             extracted_data['type'] = item['type']

        # Adiciona o sufixo USDT à moeda, se existir
        if 'coin' in extracted_data and extracted_data['coin']:
            extracted_data['coin'] = f"{extracted_data['coin'].upper()}USDT"
        
        return extracted_data

    logger.info("[Parser] Nenhum padrão de sinal conhecido foi encontrado na mensagem.")
    return None
