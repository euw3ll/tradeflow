from sqlalchemy import (Column, Integer, String, BigInteger, Boolean, Float, JSON, DateTime, UniqueConstraint)
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    first_name = Column(String)
    api_key_encrypted = Column(String)
    api_secret_encrypted = Column(String)
    entry_size_percent = Column(Float, default=5.0)
    max_leverage = Column(Integer, default=10)
    min_confidence = Column(Float, default=0.0)
    approval_mode = Column(String, default='AUTOMATIC', nullable=False)
    daily_profit_target = Column(Float, default=0.0, nullable=False)
    daily_loss_limit = Column(Float, default=0.0, nullable=False)
    coin_whitelist = Column(String, default='todas', nullable=False)
    stop_strategy = Column(String(20), default='BREAK_EVEN', nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    stop_gain_trigger_pct = Column(Float, default=0.0, nullable=False)
    stop_gain_lock_pct = Column(Float, default=0.0, nullable=False)
    # Gatilhos opcionais por PnL para antecipar BE/TS
    be_trigger_pct = Column(Float, default=0.0, nullable=False)
    ts_trigger_pct = Column(Float, default=0.0, nullable=False)
    circuit_breaker_threshold = Column(Integer, default=0, nullable=False)
    circuit_breaker_pause_minutes = Column(Integer, default=60, nullable=False)
    long_trades_paused_until = Column(DateTime(timezone=True), nullable=True)
    short_trades_paused_until = Column(DateTime(timezone=True), nullable=True)
    is_sleep_mode_enabled = Column(Boolean, default=False, nullable=False)
    is_ma_filter_enabled = Column(Boolean, default=False, nullable=False)
    ma_period = Column(Integer, default=50, nullable=False)
    ma_timeframe = Column(String(10), default='60', nullable=False) # '60' para 1 hora
    is_rsi_filter_enabled = Column(Boolean, default=False, nullable=False)
    rsi_timeframe = Column(String(10), default='60', nullable=False)
    rsi_oversold_threshold = Column(Integer, default=30, nullable=False)
    rsi_overbought_threshold = Column(Integer, default=70, nullable=False)
    tp_distribution = Column(String, default='EQUAL', nullable=False)
    # Notificações: política de limpeza das mensagens de trades fechados
    msg_cleanup_mode = Column(String(20), default='OFF', nullable=False)  # OFF | AFTER | EOD
    msg_cleanup_delay_minutes = Column(Integer, default=30, nullable=False)

class InviteCode(Base):
    __tablename__ = 'invite_codes'
    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, nullable=False)
    is_used = Column(Boolean, default=False)

class MonitoredTarget(Base):
    __tablename__ = 'monitored_targets'
    id = Column(Integer, primary_key=True)
    channel_id = Column(BigInteger, nullable=False)
    channel_name = Column(String)
    topic_id = Column(BigInteger, unique=True, nullable=True)
    topic_name = Column(String)

class Trade(Base):
    __tablename__ = 'trades'
    id = Column(Integer, primary_key=True)
    user_telegram_id = Column(BigInteger, nullable=False)
    order_id = Column(String, unique=True, nullable=False)
    notification_message_id = Column(BigInteger, nullable=True)
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)
    qty = Column(Float, nullable=False)
    entry_price = Column(Float)
    stop_loss = Column(Float)
    current_stop_loss = Column(Float)
    initial_targets = Column(JSON)
    total_initial_targets = Column(Integer, nullable=True)
    status = Column(String, default='ACTIVE')
    remaining_qty = Column(Float)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    closed_at = Column(DateTime(timezone=True), nullable=True)
    closed_pnl = Column(Float, nullable=True)
    is_breakeven = Column(Boolean, default=False, nullable=False)
    trail_high_water_mark = Column(Float, nullable=True)
    is_stop_gain_active = Column(Boolean, default=False, nullable=False)
    unrealized_pnl_pct = Column(Float, nullable=True)
    missing_cycles = Column(Integer, default=0, nullable=False)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)

class PendingSignal(Base):
    __tablename__ = 'pending_signals'
    id = Column(Integer, primary_key=True)
    user_telegram_id = Column(BigInteger, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True) 
    order_id = Column(String, unique=True, nullable=False)
    signal_data = Column(JSON, nullable=False)
    notification_message_id = Column(BigInteger, nullable=True)
    __table_args__ = (UniqueConstraint('user_telegram_id', 'symbol', name='_user_symbol_uc'),)

class SignalForApproval(Base):
    __tablename__ = 'signals_for_approval'
    id = Column(Integer, primary_key=True)
    user_telegram_id = Column(BigInteger, nullable=False, index=True)
    symbol = Column(String, nullable=False)
    source_name = Column(String)
    signal_data = Column(JSON, nullable=False)
    approval_message_id = Column(BigInteger)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
