from sqlalchemy import (create_engine, Column, Integer, String, BigInteger, Boolean, Float, JSON, DateTime)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    first_name = Column(String)
    api_key_encrypted = Column(String)
    api_secret_encrypted = Column(String)
    risk_per_trade_percent = Column(Float, default=1.0)
    max_leverage = Column(Integer, default=10)
    min_confidence = Column(Float, default=0.0)
    approval_mode = Column(String, default='AUTOMATIC', nullable=False)

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
    topic_id = Column(Integer, nullable=True) # <-- Campo para o ID do tópico
    topic_name = Column(String, nullable=True) # <-- Campo para o nome do tópico
    
    # Podemos adicionar um __repr__ para facilitar a visualização
    def __repr__(self):
        return f"<MonitoredTarget(channel='{self.channel_name}', topic='{self.topic_name}')>"

class PendingSignal(Base):
    """Tabela para armazenar sinais de Ordem Limite que aguardam ativação/cancelamento."""
    __tablename__ = 'pending_signals'
    id = Column(Integer, primary_key=True)
    user_telegram_id = Column(BigInteger, nullable=False)
    symbol = Column(String, nullable=False, unique=True, index=True) # Apenas um sinal pendente por moeda
    signal_data = Column(JSON, nullable=False)

class Trade(Base):
    __tablename__ = 'trades'
    id = Column(Integer, primary_key=True)
    user_telegram_id = Column(BigInteger, nullable=False)
    
    # Dados da Bybit
    order_id = Column(String, unique=True, nullable=False)
    symbol = Column(String, nullable=False)
    
    # Dados do Trade
    side = Column(String) # 'LONG' ou 'SHORT'
    qty = Column(Float)
    entry_price = Column(Float)
    
    # Gerenciamento
    stop_loss = Column(Float)
    initial_targets = Column(JSON) # Armazenaremos a lista de todos os alvos aqui
    
    # Status Ativo
    status = Column(String, default='ACTIVE') # Ex: ACTIVE, TP1_HIT, CLOSED
    remaining_qty = Column(Float) # Quantidade que ainda está aberta

    # Armazena o valor atual do Stop Loss, que será modificado pelo tracker.
    current_stop_loss = Column(Float, nullable=True)

class SignalForApproval(Base):
    """Armazena sinais que estão aguardando a aprovação manual do usuário."""
    __tablename__ = 'signals_for_approval'
    id = Column(Integer, primary_key=True)
    user_telegram_id = Column(BigInteger, nullable=False, index=True)
    symbol = Column(String, nullable=False)
    source_name = Column(String)
    
    # Armazena todos os dados do sinal parseado em um campo JSON
    signal_data = Column(JSON, nullable=False)
    
    # Armazena o ID da mensagem de aprovação para futura edição
    approval_message_id = Column(BigInteger)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())