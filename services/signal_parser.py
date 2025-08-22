import re
import logging
import unicodedata
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
        # A nova regex usa '.*?' para pular qualquer caractere (como emojis)
        # entre o início da linha (ou a keyword) e o valor que queremos capturar.
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        return match.group(1).strip() if match else None

    def find_multiple_values(pattern: str, text: str) -> List[float]:
        # Adicionamos '.*?' aqui também para maior robustez.
        matches = re.findall(pattern, text, re.IGNORECASE | re.MULTILINE)
        return [float(v.replace(',', '.')) for v in matches]

    def normalize_text(text: str) -> str:
        """Remove acentos e converte para minúsculas."""
        return unicodedata.normalize("NFKD", text).encode("ASCII", "ignore").decode("utf-8").lower()

    text_normalized = normalize_text(message_text)
    signal_type = None
    if 'ordem limite' in text_normalized:
        signal_type = SignalType.LIMIT
    elif 'ordem a mercado' in text_normalized or 'sinal entrou no preco' in text_normalized:
        signal_type = SignalType.MARKET

    # Regex atualizadas para serem mais tolerantes, buscando do início da linha (^)
    # e ignorando emojis ou texto inicial com (?:.*\s)?
    coin = find_single_value(r'^(?:.*\s)?(?:Moeda|Coin|Pair):\s*(\w+)', message_text)
    order_type = find_single_value(r'^(?:.*\s)?Tipo:\s*(LONG|SHORT)', message_text)
    entry_zone_str = find_single_value(r'^(?:.*\s)?Zona\s*de\s*Entrada:\s*([\d\.\,\s-]+)', message_text)
    stop_loss_str = find_single_value(r'^(?:.*\s)?Stop\s*Loss:\s*([\d\.\,]+)', message_text)
    targets = find_multiple_values(r'T\d+:\s*([\d\.\,]+)', message_text)
    confidence_str = find_single_value(r'^(?:.*\s)?Confiança:\s*([\d\.\,]+)%', message_text)


    # Validação essencial: Se não for um sinal de entrada completo, retorna None
    if not all([signal_type, coin, order_type, entry_zone_str, stop_loss_str]):
        # Adiciona log para depuração em caso de falha
        logger.warning(f"[Parser] Mensagem não correspondeu a um sinal completo. Detalhes:\n"
                     f"- signal_type: {signal_type}\n- coin: {coin}\n- order_type: {order_type}\n"
                     f"- entry_zone_str: {entry_zone_str}\n- stop_loss_str: {stop_loss_str}")
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