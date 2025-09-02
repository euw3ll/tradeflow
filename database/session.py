# database/session.py
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base
from dotenv import load_dotenv

# Carrega variáveis de ambiente de um arquivo .env (útil para desenvolvimento local)
load_dotenv()

# --- LÓGICA DE CONEXÃO CENTRALIZADA ---
# A aplicação agora depende 100% da variável de ambiente DATABASE_URL.
# Isso desacopla o código do ambiente (local, staging, produção na VPS).
DATABASE_URL = os.getenv('DATABASE_URL')

if not DATABASE_URL:
    raise ValueError("A variável de ambiente DATABASE_URL não foi definida. A aplicação não pode iniciar.")

engine = create_engine(
    DATABASE_URL,
    # Pool de conexões é recomendado para produção com PostgreSQL
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# A função init_db() foi removida.
# A responsabilidade de criar/atualizar o schema é EXCLUSIVA do Alembic.