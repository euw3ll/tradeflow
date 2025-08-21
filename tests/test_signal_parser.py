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
