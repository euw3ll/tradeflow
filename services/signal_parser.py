import re
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

def parse_signal(message_text: str) -> Optional[Dict[str, Any]]:
    """
    Analisa a mensagem para extrair dados e, crucialmente, o TIPO de sinal
    (Limite, Mercado, Cancelado).
    """
    
    def find_single_value(pattern: str, text: str) -> Optional[str]:
        match = re.search(pattern, text, re.IGNORECASE)
        return match.group(1).strip() if match else None

    def find_multiple_values(pattern: str, text: str) -> List[float]:
        matches = re.findall(pattern, text, re.IGNORECASE)
        return [float(v) for v in matches]

    # --- Etapa 1: Análise de Tipo/Status ---
    text_lower = message_text.lower()
    signal_type = None
    if 'sinal cancelado' in text_lower:
        signal_type = 'CANCELLED'
    elif 'ordem limite' in text_lower:
        signal_type = 'LIMIT'
    elif 'ordem à mercado' in text_lower or 'sinal entrou no preço' in text_lower:
        signal_type = 'MARKET'

    # --- Etapa 2: Extração dos Dados ---
    coin = find_single_value(r'.*Moeda:\s*(\w+)', message_text)
    
    # Para um cancelamento, tentamos extrair a moeda da linha de cancelamento se não encontrarmos no formato padrão
    if signal_type == 'CANCELLED' and not coin:
        coin = find_single_value(r'(\w+)\s*Sinal Cancelado', message_text)

    order_type = find_single_value(r'Tipo:\s*(LONG|SHORT)', message_text)
    leverage_str = find_single_value(r'Alavancagem:\s*(\d+)x', message_text)
    entry_zone_str = find_single_value(r'Zona de Entrada:\s*([\d\.\s-]+)', message_text)
    stop_loss_str = find_single_value(r'Stop Loss:\s*([\d\.]+)', message_text)
    targets = find_multiple_values(r'T\d+:\s*([\d\.]+)', message_text)
    confidence_str = find_single_value(r'Confiança:\s*([\d\.]+)%', message_text)

    # --- Etapa 3: Validação e Retorno por Tipo ---
    if not coin:
        logger.warning("[Parser] Campo 'Moeda' não encontrado no sinal.")
        return None

    if signal_type == 'CANCELLED':
        # Para um cancelamento, só precisamos do tipo e da moeda.
        return {"type": signal_type, "coin": f"{coin.upper()}USDT"}

    # Validação para ordens de mercado/limite
    if not order_type or not entry_zone_str or not stop_loss_str:
        logger.warning("[Parser] Sinal não contém todos os campos necessários (Tipo, Entrada, Stop).")
        return None

    entries = [float(val) for val in re.findall(r'([\d\.]+)', entry_zone_str)]
    if not entries:
        logger.warning("[Parser] Nenhum preço numérico encontrado na 'Zona de Entrada'.")
        return None

    # --- Etapa 4: Montagem do Dicionário Final ---
    signal_data = {
        "type": signal_type,
        "coin": f"{coin.upper()}USDT",
        "order_type": order_type.upper(),
        "leverage": int(leverage_str) if leverage_str else 10,
        "entries": entries,
        "stop_loss": float(stop_loss_str),
        "targets": targets,
        "confidence": float(confidence_str) if confidence_str else None
    }
    
    return signal_data