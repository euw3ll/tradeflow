import asyncio
import types

class FakeBot:
    def __init__(self): self.edits = []
    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None):
        self.edits.append((chat_id, message_id, text))

class FakeApp:
    def __init__(self): self.bot = FakeBot()

class T:
    # Mock minimal de Trade
    def __init__(self, symbol, side, missing_cycles=0, notification_message_id=123):
        self.symbol = symbol; self.side = side
        self.status = "ACTIVE"; self.closed_pnl = None
        self.remaining_qty = 1.0; self.notification_message_id = notification_message_id
        self.missing_cycles = missing_cycles
        self.last_seen_at = None

async def _run(policy, bybit_keys, trades, threshold=3):
    app = FakeApp()
    user = types.SimpleNamespace(telegram_id=111)
    db = object()
    await policy(app, user, db, trades, bybit_keys, threshold)
    return app.bot.edits

def test_nao_fecha_em_1_ou_2_ciclos(event_loop=None):
    from main import apply_missing_cycles_policy  # ajuste o import conforme onde você adicionou a função

    trades = [T("TIAUSDT", "LONG")]
    # 1º ciclo ausente
    edits = asyncio.get_event_loop().run_until_complete(_run(apply_missing_cycles_policy, set(), trades))
    assert trades[0].missing_cycles == 1
    assert trades[0].status == "ACTIVE"
    assert edits == []

    # 2º ciclo ausente
    edits = asyncio.get_event_loop().run_until_complete(_run(apply_missing_cycles_policy, set(), trades))
    assert trades[0].missing_cycles == 2
    assert trades[0].status == "ACTIVE"
    assert edits == []

def test_fecha_no_3o_ciclo(event_loop=None):
    from main import apply_missing_cycles_policy

    trades = [T("TIAUSDT", "LONG", missing_cycles=2)]
    edits = asyncio.get_event_loop().run_until_complete(_run(apply_missing_cycles_policy, set(), trades))
    assert trades[0].missing_cycles == 3
    assert trades[0].status == "CLOSED_GHOST"
    assert trades[0].remaining_qty == 0.0
    assert len(edits) == 1  # mensagem de remoção enviada

def test_reset_quando_volta_a_aparecer(event_loop=None):
    from main import apply_missing_cycles_policy

    trades = [T("TIAUSDT", "LONG", missing_cycles=2)]
    edits = asyncio.get_event_loop().run_until_complete(_run(apply_missing_cycles_policy, {("TIAUSDT","LONG")}, trades))
    assert trades[0].missing_cycles == 0
    assert trades[0].status == "ACTIVE"
    assert len(edits) == 0