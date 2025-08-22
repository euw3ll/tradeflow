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



# --- NOVO TESTE: MARKET (Ordem à Mercado) ---
def test_parse_market_signal_with_accent():
    message = textwrap.dedent(
        """
        🏁 #39170 - Ordem à Mercado

        💎 Moeda: AVAX
        📊 Tipo: SHORT (Futures)

        💰 Zona de Entrada: 22.85000000 - 22.85000000
        🛑 Stop Loss: 24.22000000
        Alvos:
        T1: 22.69000000
        T2: 22.55000000
        """
    )

    data = parse_signal(message)

    assert data is not None
    assert data["type"] == SignalType.MARKET
    assert data["coin"] == "AVAXUSDT"
    assert data["order_type"] == "SHORT"
    assert data["entries"][0] == 22.85
    assert data["stop_loss"] == 24.22
    assert 22.69 in data["targets"]
    assert 22.55 in data["targets"]


# --- NOVO TESTE: CANCELAR ---
def test_parse_cancel_signal():
    message = textwrap.dedent(
        """
        ⚠️ BTC sinal cancelado
        """
    )

    data = parse_signal(message)

    assert data is not None
    assert data["type"] == SignalType.CANCELAR
    assert data["coin"] == "BTCUSDT"
