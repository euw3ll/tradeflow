import re
from typing import Dict, Any, List, Optional

def parse_signal(message_text: str) -> Optional[Dict[str, Any]]:
    """
    Analisa uma mensagem de texto no formato específico do canal de sinais.
    Retorna um dicionário com os dados do sinal ou None se não for um sinal válido.
    """
    
    # --- Funções auxiliares para extração com regex ---
    def find_single_value(pattern: str, text: str) -> Optional[str]:
        """Encontra o primeiro valor para um padrão regex."""
        match = re.search(pattern, text, re.IGNORECASE)
        return match.group(1).strip() if match else None

    def find_multiple_values(pattern: str, text: str) -> List[float]:
        """Encontra múltiplos valores numéricos para um padrão regex."""
        matches = re.findall(pattern, text, re.IGNORECASE)
        return [float(v) for v in matches]

    # --- Extração dos Dados Essenciais ---
    coin = find_single_value(r'💎 Moeda: (\w+)', message_text)
    order_type = find_single_value(r'📊 Tipo: (LONG|SHORT)', message_text)
    leverage_str = find_single_value(r'📈 Alavancagem: (\d+)x', message_text)
    confidence_str = find_single_value(r'Confiança: ([\d\.]+)%', message_text)
    confidence = float(confidence_str) if confidence_str else None
    
    # Zona de Entrada: pode ser um valor ou uma faixa "valor1 - valor2"
    entry_zone_str = find_single_value(r'💰 Zona de Entrada: ([\d\.\s-]+)', message_text)
    
    stop_loss_str = find_single_value(r'🛑 Stop Loss: ([\d\.]+)', message_text)
    
    # Alvos: captura todos os números que seguem "T\d:"
    targets = find_multiple_values(r'T\d+:\s*([\d\.]+)', message_text)

    # --- Validação e Limpeza dos Dados ---
    if not all([coin, order_type, leverage_str, entry_zone_str, stop_loss_str, targets]):
        # Se algum campo essencial não for encontrado, não é um sinal válido para nós.
        return None

    # Processa a zona de entrada para pegar o primeiro valor
    entries = [float(val) for val in re.findall(r'([\d\.]+)', entry_zone_str)]
    if not entries:
        return None # Precisa de pelo menos um preço de entrada

    # --- Monta o dicionário de retorno ---
    signal_data = {
        "coin": f"{coin.upper()}USDT", # Adiciona USDT por padrão
        "order_type": order_type.upper(),
        "leverage": int(leverage_str),
        "entries": entries, # Retorna uma lista de preços de entrada
        "stop_loss": float(stop_loss_str),
        "targets": targets,
        "confidence": confidence,
    }
    
    return signal_data

# --- Exemplo de como usar e testar ---
if __name__ == '__main__':
    # Cole exatamente a mensagem de sinal que você enviou
    sample_signal_message = """
    🏁 #32978 - Ordem à Mercado

    📢 Canal: GRE - 46
    🌐 Plataforma: telegram

    💎 Moeda: GRT
    📊 Tipo: LONG (Futures)
    📈 Alavancagem: 10x

    💰 Zona de Entrada: 0.10077000 - 0.10077000
    🛑 Stop Loss: 0.09069000 (10.003%)
    🎯 Alvos:
    T1: 0.10178000 (1.00%)
    T2: 0.10279000 (2.00%)
    T3: 0.10379000 (3.00%)
    T4: 0.10480000 (4.00%)
    T5: 0.10581000 (5.00%)
    T6: 0.11589000 (15.00%)
    T7: 0.12596000 (25.00%)
    T8: 0.13604000 (35.00%)
    T9: 0.15116000 (50.00%)
    ☯️ R/R ratio: 0.1

    📊 Status: Sinal aberto
    """

    parsed_data = parse_signal(sample_signal_message)

    if parsed_data:
        print("✅ Sinal extraído com sucesso!")
        # Imprime de uma forma mais legível
        import json
        print(json.dumps(parsed_data, indent=4))
    else:
        print("❌ A mensagem não parece ser um sinal válido.")