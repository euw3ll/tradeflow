# tests/test_trade_manager_e2e.py

import sys
import types
import textwrap
import pytest

# ===========================
# STUBS: telegram e bot.keyboards
# ===========================
telegram = types.ModuleType("telegram")
telegram_ext = types.ModuleType("telegram.ext")
telegram_constants = types.ModuleType("telegram.constants")

class InlineKeyboardButton:
    def __init__(self, text, callback_data=None, url=None):
        self.text = text
        self.callback_data = callback_data
        self.url = url

class InlineKeyboardMarkup:
    def __init__(self, keyboard):
        self.keyboard = keyboard

class Application:
    def __init__(self):
        self.bot = None

# expõe no módulo stub
telegram.InlineKeyboardButton = InlineKeyboardButton
telegram.InlineKeyboardMarkup = InlineKeyboardMarkup
telegram.constants = telegram_constants
telegram_ext.Application = Application

# registra stubs
sys.modules.setdefault("telegram", telegram)
sys.modules.setdefault("telegram.ext", telegram_ext)
sys.modules.setdefault("telegram.constants", telegram_constants)

# bot.keyboards stub (para satisfazer "from bot.keyboards import signal_approval_keyboard")
bot_pkg = types.ModuleType("bot")
bot_keyboards = types.ModuleType("bot.keyboards")
def signal_approval_keyboard(signal_id: int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("Aprovar", callback_data=f"approve:{signal_id}")]])
bot_keyboards.signal_approval_keyboard = signal_approval_keyboard
sys.modules.setdefault("bot", bot_pkg)
sys.modules.setdefault("bot.keyboards", bot_keyboards)

# ===========================
# Imports reais do projeto
# ===========================
from services.signal_parser import parse_signal, SignalType
import core.trade_manager as tm


# ===========================
# Fakes utilitários
# ===========================
class FakeUser:
    def __init__(self, telegram_id=111, api_key_encrypted="enc_k", api_secret_encrypted="enc_s", min_confidence=0.0, approval_mode="AUTOMATIC"):
        self.telegram_id = telegram_id
        self.api_key_encrypted = api_key_encrypted
        self.api_secret_encrypted = api_secret_encrypted
        self.min_confidence = min_confidence
        self.approval_mode = approval_mode

class _QueryList:
    def __init__(self, data_list):
        self._data = data_list
    def filter(self, *args, **kwargs):  # compat simples
        return self
    def filter_by(self, **kwargs):
        # Implementação leve para casos .filter_by(user_telegram_id=..., symbol=...)
        def match(obj):
            return all(getattr(obj, k, None) == v for k, v in kwargs.items())
        return _QueryList([x for x in self._data if match(x)])
    def all(self):
        return list(self._data)
    def first(self):
        return self._data[0] if self._data else None

class FakeDB:
    """DB fake que suporta .query(Model) e listas de users/pendings."""
    def __init__(self, users=None, pendings=None):
        self._users = users or []
        self._pendings = pendings or []
        self.added = []
        self.deleted = []
        self.commits = 0
        self.closed = False
    def query(self, Model):
        if Model is tm.User:
            return _QueryList(self._users)
        if Model is tm.PendingSignal:
            return _QueryList(self._pendings)
        if Model is tm.Trade:
            # raramente consultado em testes; devolve vazio
            return _QueryList([])
        if Model is tm.SignalForApproval:
            return _QueryList([])
        return _QueryList([])
    def add(self, obj):
        self.added.append(obj)
        # se for PendingSignal "persistido", também aparece em consultas subsequentes
        if isinstance(obj, tm.PendingSignal) or getattr(obj, "__class__", None).__name__ == "DummyPending":
            self._pendings.append(obj)
    def delete(self, obj):
        self.deleted.append(obj)
        if obj in self._pendings:
            self._pendings.remove(obj)
    def commit(self):
        self.commits += 1
    def close(self):
        self.closed = True

class FakeBot:
    def __init__(self):
        self.sent = []
    async def send_message(self, **kwargs):
        self.sent.append(kwargs)

class FakeApplication(Application):
    def __init__(self):
        super().__init__()
        self.bot = FakeBot()


# ===========================
# Textos de sinais
# ===========================
MARKET_SIGNAL = textwrap.dedent("""
🏁 #39170 - Ordem à Mercado

💎 Moeda: AVAX
📊 Tipo: SHORT (Futures)

💰 Zona de Entrada: 22.85000000 - 22.85000000
🛑 Stop Loss: 24.22000000
Alvos:
T1: 22.69000000
T2: 22.55000000
""")

LIMIT_SIGNAL_SHORT = textwrap.dedent("""
⏳ #38792 - Ordem Limite

💎 Moeda: CYBER
📊 Tipo: SHORT (Futures)

💰 Zona de Entrada: 2.52500000 - 2.64000000
🛑 Stop Loss: 2.90000000
Alvos:
T1: 2.44000000
T2: 2.37000000
T3: 2.29000000
T4: 2.20000000
""")

LIMIT_SIGNAL_LONG = textwrap.dedent("""
⏳ #50001 - Ordem Limite

💎 Moeda: XRP
📊 Tipo: LONG (Futures)

💰 Zona de Entrada: 0.4500 - 0.4510
🛑 Stop Loss: 0.4400
Alvos:
T1: 0.4600
T2: 0.4700
""")

CANCEL_SIGNAL_XRP = "⚠️ XRP sinal cancelado"


# ===========================
# TESTES
# ===========================
@pytest.mark.asyncio
async def test_market_flow_calls_place_order(monkeypatch):
    parsed = parse_signal(MARKET_SIGNAL)
    assert parsed and parsed["type"] == SignalType.MARKET

    app = FakeApplication()
    db = FakeDB(users=[FakeUser(telegram_id=999)])

    class DummyTrade:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    called = {}
    async def fake_place_order(api_key, api_secret, signal_data, user, balance):
        called["api_key"] = api_key
        called["api_secret"] = api_secret
        called["signal_data"] = signal_data
        called["user_id"] = user.telegram_id
        called["balance"] = balance
        return {"success": True, "data": {"orderId": "fake-123", "qty": "1.0"}}

    async def fake_get_account_info(api_key, api_secret):
        return {"success": True, "data": [{"totalEquity": "100.0"}]}

    def fake_decrypt(data):
        return "DECRYPTED-" + (data or "")

    monkeypatch.setattr(tm, "place_order", fake_place_order)
    monkeypatch.setattr(tm, "get_account_info", fake_get_account_info)
    monkeypatch.setattr(tm, "decrypt_data", fake_decrypt)
    monkeypatch.setattr(tm, "Trade", DummyTrade)

    await tm.execute_signal_for_all_users(parsed, app, db, source_name="TEST-CHANNEL")

    # Validações
    assert called, "place_order não foi chamado"
    assert called["user_id"] == 999
    assert called["signal_data"]["coin"] == "AVAXUSDT"
    assert called["signal_data"]["order_type"] == "SHORT"
    assert called["signal_data"]["entries"][0] == pytest.approx(22.85, rel=1e-6)
    assert called["signal_data"]["stop_loss"] == pytest.approx(24.22, rel=1e-6)
    assert called["balance"] == 100.0

    # Mensagem ao usuário
    assert app.bot.sent, "Nenhuma mensagem foi enviada"
    texts = [m.get("text", "").lower() for m in app.bot.sent]
    assert any(("ordem" in t) or ("aberta" in t) or ("sucesso" in t) for t in texts), texts


@pytest.mark.asyncio
async def test_limit_short_creates_pending_and_uses_upper_bound(monkeypatch):
    parsed = parse_signal(LIMIT_SIGNAL_SHORT)
    assert parsed and parsed["type"] == SignalType.LIMIT and parsed["order_type"] == "SHORT"

    app = FakeApplication()
    db = FakeDB(users=[FakeUser(telegram_id=777)])

    class DummyPending:
        def __init__(self, **kw):
            self.user_telegram_id = kw.get("user_telegram_id")
            self.symbol = kw.get("symbol")
            self.order_id = kw.get("order_id")
            self.signal_data = kw.get("signal_data")

    called = {"market": False, "limit": None}

    async def fake_place_order(*a, **k):
        called["market"] = True
        return {"success": True}

    async def fake_place_limit_order(api_key, api_secret, signal_data, user, balance):
        called["limit"] = dict(signal_data)  # capturar signal_data com limit_price
        return {"success": True, "data": {"orderId": "LIM-001"}}

    async def fake_get_account_info(api_key, api_secret):
        return {"success": True, "data": [{"totalEquity": "50.0"}]}

    def fake_decrypt(data):
        return "DECRYPTED-" + (data or "")

    monkeypatch.setattr(tm, "place_order", fake_place_order)  # não deve ser chamado
    monkeypatch.setattr(tm, "place_limit_order", fake_place_limit_order)
    monkeypatch.setattr(tm, "get_account_info", fake_get_account_info)
    monkeypatch.setattr(tm, "decrypt_data", fake_decrypt)
    monkeypatch.setattr(tm, "PendingSignal", DummyPending)

    await tm.execute_signal_for_all_users(parsed, app, db, source_name="TEST-CHANNEL")

    # Não deve ter chamado market
    assert not called["market"], "LIMIT (SHORT) não deve abrir ordem a mercado"
    # Deve ter chamado limit com limit_price == high (2.64000000)
    assert called["limit"] and called["limit"].get("limit_price") == pytest.approx(2.64, rel=1e-6)
    # Deve ter criado PendingSignal e avisado usuário
    assert any(isinstance(x, DummyPending) for x in db.added), "PendingSignal não criado"
    assert app.bot.sent, "Sem mensagem de confirmação ao usuário"


@pytest.mark.asyncio
async def test_limit_long_uses_lower_bound(monkeypatch):
    parsed = parse_signal(LIMIT_SIGNAL_LONG)
    assert parsed and parsed["type"] == SignalType.LIMIT and parsed["order_type"] == "LONG"

    app = FakeApplication()
    db = FakeDB(users=[FakeUser(telegram_id=888)])

    class DummyPending:
        def __init__(self, **kw):
            self.user_telegram_id = kw.get("user_telegram_id")
            self.symbol = kw.get("symbol")
            self.order_id = kw.get("order_id")
            self.signal_data = kw.get("signal_data")

    called = {"limit": None}

    async def fake_place_limit_order(api_key, api_secret, signal_data, user, balance):
        called["limit"] = dict(signal_data)
        return {"success": True, "data": {"orderId": "LIM-002"}}

    async def fake_get_account_info(api_key, api_secret):
        return {"success": True, "data": [{"totalEquity": "80.0"}]}

    def fake_decrypt(data):
        return "DECRYPTED-" + (data or "")

    monkeypatch.setattr(tm, "place_limit_order", fake_place_limit_order)
    monkeypatch.setattr(tm, "get_account_info", fake_get_account_info)
    monkeypatch.setattr(tm, "decrypt_data", fake_decrypt)
    monkeypatch.setattr(tm, "PendingSignal", DummyPending)

    await tm.execute_signal_for_all_users(parsed, app, db, source_name="TEST-CHANNEL")

    assert called["limit"] is not None, "place_limit_order não foi chamado"
    assert called["limit"]["limit_price"] == pytest.approx(0.4500, rel=1e-6), "LONG deve usar menor preço da faixa"
    assert any(isinstance(x, DummyPending) for x in db.added), "PendingSignal não criado"
    assert app.bot.sent, "Sem mensagem ao usuário"


@pytest.mark.asyncio
async def test_limit_skips_when_existing_pending(monkeypatch):
    parsed = parse_signal(LIMIT_SIGNAL_LONG)
    assert parsed and parsed["type"] == SignalType.LIMIT

    app = FakeApplication()

    class DummyPending:
        def __init__(self, user_telegram_id, symbol, order_id="old", signal_data=None):
            self.user_telegram_id = user_telegram_id
            self.symbol = symbol
            self.order_id = order_id
            self.signal_data = signal_data or {}

    # já existe um pendente para o mesmo símbolo e usuário
    existing = DummyPending(user_telegram_id=777, symbol="XRPUSDT")
    db = FakeDB(users=[FakeUser(telegram_id=777)], pendings=[existing])

    called = {"limit": False}
    async def fake_place_limit_order(*a, **k):
        called["limit"] = True
        return {"success": True}

    def fake_decrypt(data): return "DECRYPTED-" + (data or "")
    async def fake_get_account_info(api_key, api_secret): return {"success": True, "data": [{"totalEquity": "10"}]}

    monkeypatch.setattr(tm, "place_limit_order", fake_place_limit_order)
    monkeypatch.setattr(tm, "decrypt_data", fake_decrypt)
    monkeypatch.setattr(tm, "get_account_info", fake_get_account_info)

    await tm.execute_signal_for_all_users(parsed, app, db, source_name="TEST-CHANNEL")

    # não deve chamar place_limit_order
    assert not called["limit"], "Não deveria tentar posicionar nova LIMIT com pendente existente"
    # deve ter avisado usuário
    assert app.bot.sent and any("já tem uma ordem limite pendente" in (m.get("text","")) for m in app.bot.sent)


@pytest.mark.asyncio
async def test_cancel_removes_pending_and_notifies(monkeypatch):
    # preparar db com pending XRP para user 123
    class DummyPending:
        def __init__(self, user_telegram_id, symbol, order_id="LIM-XYZ", signal_data=None):
            self.user_telegram_id = user_telegram_id
            self.symbol = symbol
            self.order_id = order_id
            self.signal_data = signal_data or {}

    pend = DummyPending(user_telegram_id=123, symbol="XRPUSDT")
    users = [FakeUser(telegram_id=123)]
    base_db = FakeDB(users=users, pendings=[pend])

    # monkeypatch SessionLocal para devolver nosso FakeDB
    def fake_SessionLocal():
        # devolve uma "nova" instância por chamada, clonando o estado base
        return FakeDB(users=list(base_db._users), pendings=list(base_db._pendings))

    app = FakeApplication()

    async def fake_cancel_order(api_key, api_secret, order_id, symbol):
        return {"success": True}

    def fake_decrypt(data): return "DECRYPTED-" + (data or "")

    monkeypatch.setattr(tm, "SessionLocal", fake_SessionLocal)
    monkeypatch.setattr(tm, "cancel_order", fake_cancel_order)
    monkeypatch.setattr(tm, "decrypt_data", fake_decrypt)

    parsed_cancel = parse_signal(CANCEL_SIGNAL_XRP)
    assert parsed_cancel and parsed_cancel["type"] == SignalType.CANCELAR

    await tm.process_new_signal(parsed_cancel, app, source_name="TEST-CHANNEL")

    # Como usamos uma instância nova de FakeDB dentro do process, não temos referência direta
    # mas podemos validar pelo envio de mensagem de sucesso
    assert app.bot.sent and any("foi cancelada com sucesso" in (m.get("text","").lower()) for m in app.bot.sent)


@pytest.mark.asyncio
async def test_cancel_without_pending_sends_info_notification(monkeypatch):
    # DB sem pendentes
    base_db = FakeDB(users=[FakeUser(telegram_id=1)], pendings=[])
    def fake_SessionLocal():
        return FakeDB(users=list(base_db._users), pendings=list(base_db._pendings))

    app = FakeApplication()
    captured = {"msg": None}

    async def fake_send_notification(application, text):
        captured["msg"] = text

    monkeypatch.setattr(tm, "SessionLocal", fake_SessionLocal)
    monkeypatch.setattr(tm, "send_notification", fake_send_notification)

    parsed_cancel = parse_signal(CANCEL_SIGNAL_XRP)
    await tm.process_new_signal(parsed_cancel, app, source_name="TEST-CHANNEL")

    assert captured["msg"] is not None
    assert "nenhuma ordem pendente foi encontrada" in captured["msg"].lower()


@pytest.mark.asyncio
async def test_market_fails_on_balance_fetch(monkeypatch):
    parsed = parse_signal(MARKET_SIGNAL)
    app = FakeApplication()
    db = FakeDB(users=[FakeUser(telegram_id=321)])

    async def fake_get_account_info(api_key, api_secret):
        return {"success": False}  # falha

    def fake_decrypt(data): return "DECRYPTED-" + (data or "")

    called = {"market": False}
    async def fake_place_order(*a, **k):
        called["market"] = True
        return {"success": True}

    monkeypatch.setattr(tm, "get_account_info", fake_get_account_info)
    monkeypatch.setattr(tm, "decrypt_data", fake_decrypt)
    monkeypatch.setattr(tm, "place_order", fake_place_order)

    await tm.execute_signal_for_all_users(parsed, app, db, source_name="TEST-CHANNEL")

    assert not called["market"], "Não deve tentar abrir ordem sem saldo"
    assert app.bot.sent and any("falha ao buscar seu saldo bybit" in (m.get("text","").lower()) for m in app.bot.sent)
