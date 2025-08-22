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
    entry_size_percent = Column(Float, default=5.0) # Define 5% como padrão
    max_leverage = Column(Integer, default=10)
    min_confidence = Column(Float, default=0.0)
    approval_mode = Column(String, default='AUTOMATIC', nullable=False)
    # Meta de lucro diário. 0.0 significa desativado.
    daily_profit_target = Column(Float, default=0.0, nullable=False)
    # Limite de perda diário (valor positivo). 0.0 significa desativado.
    daily_loss_limit = Column(Float, default=0.0, nullable=False)

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
    symbol = Column(String, nullable=False)
    side = Column(String, nullable=False)
    qty = Column(Float, nullable=False)
    entry_price = Column(Float)
    stop_loss = Column(Float)
    current_stop_loss = Column(Float)
    initial_targets = Column(JSON)
    status = Column(String, default='ACTIVE')
    remaining_qty = Column(Float)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class PendingSignal(Base):
    __tablename__ = 'pending_signals'
    id = Column(Integer, primary_key=True)
    user_telegram_id = Column(BigInteger, nullable=False, index=True)
    
    # --- MUDANÇA APLICADA AQUI ---
    # Removemos o unique=True do symbol
    symbol = Column(String, nullable=False, index=True) 
    
    order_id = Column(String, unique=True, nullable=False)
    signal_data = Column(JSON, nullable=False)

    # Adicionamos uma restrição de unicidade composta
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