import textwrap

from services.signal_parser import parse_signal, SignalType


def test_parse_signal_without_diamond():
    message = textwrap.dedent(
        """
        ⏳ #1 - Ordem Limite
        Moeda: SOL
        Tipo: SHORT (Futures)
        Zona de Entrada: 182.66 - 182.66
        Stop Loss: 186.36
        Alvos:
        T1: 181.18
        """
    )

    data = parse_signal(message)

    assert data["coin"] == "SOLUSDT"
    assert data["type"] == SignalType.LIMIT


def test_parse_signal_with_coin_synonym():
    message = textwrap.dedent(
        """
        ⏳ #2 - Ordem Limite
        Coin: NMR
        Tipo: SHORT (Futures)
        Zona de Entrada: 8.04 - 8.28
        Stop Loss: 8.55
        Alvos:
        T1: 7.99
        """
    )

    data = parse_signal(message)

    assert data["coin"] == "NMRUSDT"
    assert data["type"] == SignalType.LIMIT

# --- NOVO TESTE ADICIONADO ---
def test_parse_complex_signal_with_emojis_and_extra_text():
    message = textwrap.dedent(
        """
        ⏳ #38792 - Ordem Limite

        📢 Canal: GRE - 58
        🌐 Plataforma: telegram

        💎 Moeda: CYBER
        📊 Tipo: SHORT (Futures)
        📈 Alavancagem: 10x

        💰 Zona de Entrada: 2.52500000 - 2.64000000
        🛑 Stop Loss: 2.90000000 (12.2943%)
        🎯 Alvos:
        T1: 2.44000000 (5.52%)
        T2: 2.37000000 (8.23%)
        T3: 2.29000000 (11.33%)
        T4: 2.20000000 (14.81%)
        ☯️ R/R ratio: 0.4

        📊 Status: Sinal aberto

        📝 Notas: Sinal aguardando condições de entrada

        🔍 Análise de Risco:
        💰 Margem Recomendada: 2.00%
        📈 Exposição Total: 12.00%
        💀 Preço de Liquidação: 3.01291700
        ✅ Stop Loss Seguro: Sim

        📊 Análise de Mercado (IA):
        📈 Tendência: Baixa (bearish)
        🟢 Confiança: 66.67%  🧭 Consenso: 4/6
        """
    )

    data = parse_signal(message)

    assert data is not None, "O parser não deveria retornar None para este sinal"
    assert data["type"] == SignalType.LIMIT
    assert data["coin"] == "CYBERUSDT"
    assert data["order_type"] == "SHORT"
    assert data["entries"] == [2.525, 2.64]
    assert data["stop_loss"] == 2.9
    assert data["targets"] == [2.44, 2.37, 2.29, 2.2]
    assert data["confidence"] == 66.67


def test_parse_market_signal_without_accent():
    message = textwrap.dedent(
        """
        🏁 #123 - Ordem a Mercado
        Moeda: AVAX
        Tipo: SHORT (Futures)
        Zona de Entrada: 22.85 - 22.85
        Stop Loss: 24.22
        Alvos:
        T1: 22.69
        """
    )

    data = parse_signal(message)

    assert data["type"] == SignalType.MARKET
    assert data["coin"] == "AVAXUSDT"
