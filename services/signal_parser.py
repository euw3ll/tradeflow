import re
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# --- DEFINIÇÃO CENTRALIZADA DOS TIPOS DE SINAL ---
class SignalType:
    MARKET = 'MARKET'
    LIMIT = 'LIMIT'
    CANCELAR = 'CANCELAR'
    FECHAR_PARCIAL = 'FECHAR_PARCIAL'
    MOVER_STOP_ENTRADA = 'MOVER_STOP_ENTRADA'

# --- ESTRUTURA DE PADRÕES DE REGEX ---
# Uma lista de dicionários onde cada um representa um padrão de sinal a ser detectado.
# A ordem é importante: os padrões mais específicos devem vir antes dos mais genéricos.
SIGNAL_PATTERNS = [
    # --- Padrões de Gerenciamento ---
    {
        "type": SignalType.FECHAR_PARCIAL,
        "pattern": re.compile(r'(?:fechar|realizar)\s+(?:parcial|50%)\s+de\s+([\w]+)', re.IGNORECASE),
        "extractor": lambda m: {"coin": m.group(1)}
    },
    {
        "type": SignalType.MOVER_STOP_ENTRADA,
        "pattern": re.compile(r'mover\s+stop\s+(?:de\s+)?([\w]+)\s+para\s+a\s+entrada', re.IGNORECASE),
        "extractor": lambda m: {"coin": m.group(1)}
    },
    {
        "type": SignalType.MOVER_STOP_ENTRADA,
        "pattern": re.compile(r'stop\s+([\w]+)\s+no\s+(?:pre[çc]o\s+de\s+)?entrada', re.IGNORECASE),
        "extractor": lambda m: {"coin": m.group(1)}
    },
    # --- Padrões de Cancelamento (mais flexíveis) ---
    {
        "type": SignalType.CANCELAR,
        "pattern": re.compile(r'([\w]+)\s+Sinal\s+Cancelado', re.IGNORECASE),
        "extractor": lambda m: {"coin": m.group(1)}
    },
    {
        "type": SignalType.CANCELAR,
        "pattern": re.compile(r'sinal\s+cancelado\s+para\s+([\w]+)', re.IGNORECASE),
        "extractor": lambda m: {"coin": m.group(1)}
    },
    # --- Padrão de Sinal Completo (Market ou Limit) ---
    {
        "type": "FULL_SIGNAL", # Tipo genérico para ser detalhado depois
        "pattern": re.compile(r'💎\s*Moeda:\s*(\w+)', re.IGNORECASE),
        "extractor": "full_signal_extractor" # Usa uma função dedicada
    }
]

def _full_signal_extractor(message_text: str) -> Optional[Dict[str, Any]]:
    """
    Função dedicada para extrair todos os detalhes de um sinal de entrada
    (Market ou Limit), que é mais complexo.
    """
    def find_single_value(pattern: str, text: str) -> Optional[str]:
        match = re.search(pattern, text, re.IGNORECASE)
        return match.group(1).strip() if match else None

    def find_multiple_values(pattern: str, text: str) -> List[float]:
        matches = re.findall(pattern, text, re.IGNORECASE)
        # Garante que mesmo com vírgula, o número seja convertido corretamente
        return [float(v.replace(',', '.')) for v in matches]

    text_lower = message_text.lower()
    signal_type = None
    if 'ordem limite' in text_lower:
        signal_type = SignalType.LIMIT
    elif 'ordem à mercado' in text_lower or 'sinal entrou no preço' in text_lower:
        signal_type = SignalType.MARKET

    coin = find_single_value(r'💎\s*Moeda:\s*(\w+)', message_text)
    order_type = find_single_value(r'Tipo:\s*(LONG|SHORT)', message_text)
    leverage_str = find_single_value(r'Alavancagem:\s*(\d+)x', message_text)
    entry_zone_str = find_single_value(r'Zona de Entrada:\s*([\d\.\,\s-]+)', message_text)
    stop_loss_str = find_single_value(r'Stop Loss:\s*([\d\.\,]+)', message_text)
    targets = find_multiple_values(r'T\d+:\s*([\d\.\,]+)', message_text)
    confidence_str = find_single_value(r'Confiança:\s*([\d\.\,]+)%', message_text)

    if not all([signal_type, coin, order_type, entry_zone_str, stop_loss_str]):
        logger.warning("[Parser] Sinal completo detectado, mas faltam campos essenciais (Tipo, Moeda, Entrada, Stop).")
        return None

    entries = [float(val.replace(',', '.')) for val in re.findall(r'([\d\.\,]+)', entry_zone_str)]
    if not entries:
        logger.warning("[Parser] Nenhum preço numérico encontrado na 'Zona de Entrada'.")
        return None

    return {
        "type": signal_type,
        "coin": coin,
        "order_type": order_type.upper(),
        "leverage": int(leverage_str) if leverage_str else 10,
        "entries": entries,
        "stop_loss": float(stop_loss_str.replace(',', '.')),
        "targets": targets,
        "confidence": float(confidence_str.replace(',', '.')) if confidence_str else None
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
        
        # Se o extrator for uma função dedicada
        if item["extractor"] == "full_signal_extractor":
            extracted_data = _full_signal_extractor(message_text)
        # Se for uma função lambda simples
        else:
            extracted_data = item["extractor"](match)
        
        if not extracted_data:
            continue
            
        # Adiciona o tipo de sinal e formata a moeda com sufixo USDT
        final_data = {"type": item["type"], **extracted_data}
        if 'coin' in final_data:
            final_data['coin'] = f"{final_data['coin'].upper()}USDT"
        
        return final_data

    logger.info("[Parser] Nenhum padrão de sinal conhecido foi encontrado na mensagem.")
    return None