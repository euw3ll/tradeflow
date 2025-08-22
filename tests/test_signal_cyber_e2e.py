# tests/test_signal_cyber_e2e.py
import sys
import types
import textwrap
import pytest

# ======== STUBS mínimos (telegram e keyboards) ========
telegram = types.ModuleType("telegram")
telegram_ext = types.ModuleType("telegram.ext")
telegram_constants = types.ModuleType("telegram.constants")

class InlineKeyboardButton:
    def __init__(self, text, callback_data=None, url=None):
        self.text = text; self.callback_data = callback_data; self.url = url

class InlineKeyboardMarkup:
    def __init__(self, keyboard): self.keyboard = keyboard

class Application:
    def __init__(self): self.bot = None

telegram.InlineKeyboardButton = InlineKeyboardButton
telegram.InlineKeyboardMarkup = InlineKeyboardMarkup
telegram.constants = telegram_constants
telegram_ext.Application = Application

sys.modules.setdefault("telegram", telegram)
sys.modules.setdefault("telegram.ext", telegram_ext)
sys.modules.setdefault("telegram.constants", telegram_constants)

bot_pkg = types.ModuleType("bot")
bot_keyboards = types.ModuleType("bot.keyboards")
def signal_approval_keyboard(signal_id: int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("Aprovar", callback_data=f"approve:{signal_id}")]])
bot_keyboards.signal_approval_keyboard = signal_approval_keyboard
sys.modules.setdefault("bot", bot_pkg)
sys.modules.setdefault("bot.keyboards", bot_keyboards)

# ======== Imports reais do projeto ========
from services.signal_parser import parse_signal, SignalType
import core.trade_manager as tm

# ======== Fakes utilitários ========
class FakeUser:
    def __init__(self, telegram_id=111, api_key_encrypted="enc_k", api_secret_encrypted="enc_s"):
        self.telegram_id = telegram_id
        self.api_key_encrypted = api_key_encrypted
        self.api_secret_encrypted = api_secret_encrypted

class _QueryList:
    def __init__(self, data_list): self._data = data_list
    def filter(self, *a, **k): return self
    def filter_by(self, **k):
        def ok(obj): return all(getattr(obj, kk, None) == vv for kk, vv in k.items())
        return _QueryList([x for x in self._data if ok(x)])
    def all(self): return list(self._data)
    def first(self): return self._data[0] if self._data else None

class FakeDB:
    def __init__(self, users=None, pendings=None):
        self._users = users or []
        self._pendings = pendings or []
        self.added = []; self.deleted = []; self.commits = 0
    def query(self, Model):
        if Model is tm.User: return _QueryList(self._users)
        if Model is tm.PendingSignal: return _QueryList(self._pendings)
        return _QueryList([])
    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, tm.PendingSignal) or getattr(obj, "__class__", None).__name__ == "DummyPending":
            self._pendings.append(obj)
    def delete(self, obj):
        self.deleted.append(obj)
        if obj in self._pendings: self._pendings.remove(obj)
    def commit(self): self.commits += 1

class FakeBot:
    def __init__(self): self.sent = []
    async def send_message(self, **kwargs): self.sent.append(kwargs)

class FakeApplication(Application):
    def __init__(self): super().__init__(); self.bot = FakeBot()

# ======== Sinal CYBER (LIMIT/SHORT) ========
CYBER_SIGNAL = textwrap.dedent("""
⏳ #38792 - Ordem Limite

💎 Moeda: CYBER
📊 Tipo: SHORT (Futures)

💰 Zona de Entrada: 2.52500000 - 2.64000000
🛑 Stop Loss: 2.90000000 (12.2943%)
🎯 Alvos:
T1: 2.44000000 (5.52%)
T2: 2.37000000 (8.23%)
T3: 2.29000000 (11.33%)
T4: 2.20000000 (14.81%)
""")

@pytest.mark.asyncio
async def test_cyber_limit_short_positions_limit_order(monkeypatch):
    parsed = parse_signal(CYBER_SIGNAL)
    assert parsed and parsed["type"] == SignalType.LIMIT and parsed["order_type"] == "SHORT"
    assert parsed["coin"] == "CYBERUSDT"
    assert parsed["entries"] == [2.525, 2.64]
    assert parsed["stop_loss"] == 2.9

    app = FakeApplication()
    db = FakeDB(users=[FakeUser(telegram_id=777)])

    # dummy PendingSignal para não depender de SQLAlchemy real
    class DummyPending:
        def __init__(self, **kw):
            self.user_telegram_id = kw.get("user_telegram_id")
            self.symbol = kw.get("symbol")
            self.order_id = kw.get("order_id")
            self.signal_data = kw.get("signal_data")

    # Capturas
    called = {"market": False, "limit_payload": None}

    async def fake_place_order(*a, **k):
        called["market"] = True
        return {"success": True}

    async def fake_place_limit_order(api_key, api_secret, signal_data, user, balance):
        # guardamos o payload passado — deve conter limit_price = 2.64
        called["limit_payload"] = dict(signal_data)
        return {"success": True, "data": {"orderId": "CYB-LIM-001"}}

    async def fake_get_account_info(api_key, api_secret):
        return {"success": True, "data": [{"totalEquity": "123.45"}]}

    def fake_decrypt(data): return "DECRYPTED-" + (data or "")

    # patches
    monkeypatch.setattr(tm, "place_order", fake_place_order)  # não deve ser chamado
    monkeypatch.setattr(tm, "place_limit_order", fake_place_limit_order)
    monkeypatch.setattr(tm, "get_account_info", fake_get_account_info)
    monkeypatch.setattr(tm, "decrypt_data", fake_decrypt)
    monkeypatch.setattr(tm, "PendingSignal", DummyPending)

    await tm.execute_signal_for_all_users(parsed, app, db, source_name="TEST-CHANNEL")

    # 1) NÃO abriu a mercado
    assert not called["market"], "LIMIT/SHORT não deve abrir ordem a mercado"

    # 2) Chamou limit com limit_price = 2.64 (maior da faixa)
    assert called["limit_payload"] is not None, "place_limit_order não foi chamado"
    assert called["limit_payload"].get("limit_price") == pytest.approx(2.64, rel=1e-6)

    # 3) Criou PendingSignal
    assert any(isinstance(x, DummyPending) for x in db.added), "PendingSignal não foi criado"

    # 4) Enviou mensagem ao usuário
    assert app.bot.sent, "Nenhuma mensagem foi enviada ao usuário"
    texts = [m.get("text", "").lower() for m in app.bot.sent]
    assert any("limite" in t or "monitorando" in t for t in texts), texts
