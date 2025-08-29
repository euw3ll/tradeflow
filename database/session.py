# database/session.py
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .models import Base

# --- Caminho dinâmico (Docker vs local) ---
# Docker: a imagem monta o DB em /app/data/tradeflow.db
# Local: usa ./tradeflow.db
if os.path.isdir("/app/data"):
    DATABASE_URL = "sqlite:////app/data/tradeflow.db"
else:
    DATABASE_URL = "sqlite:///./tradeflow.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    """Cria tabelas conhecidas e garante colunas novas no 'trades'."""
    # 1) Cria tabelas definidas nos models (se não existirem)
    Base.metadata.create_all(bind=engine)

    # 2) Bootstrap de schema para colunas novas (idempotente)
    with engine.begin() as conn:
        # lista colunas atuais da tabela trades
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(trades)")).fetchall()]

        # missing_cycles
        if "missing_cycles" not in cols:
            conn.execute(text(
                "ALTER TABLE trades ADD COLUMN missing_cycles INTEGER NOT NULL DEFAULT 0"
            ))

        # last_seen_at
        if "last_seen_at" not in cols:
            conn.execute(text(
                "ALTER TABLE trades ADD COLUMN last_seen_at DATETIME"
            ))