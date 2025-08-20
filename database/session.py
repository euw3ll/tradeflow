import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base


def _get_database_url() -> str:
    """Retorna a URL do banco a partir da variável de ambiente.

    Caso não esteja definida, usa SQLite local para facilitar o
    desenvolvimento. Em produção recomenda‑se fornecer uma URL
    `postgresql://` via `DATABASE_URL`.
    """

    return os.getenv("DATABASE_URL", "sqlite:///./tradeflow.db")


DATABASE_URL = _get_database_url()

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Cria as tabelas definidas em `models`.

    Em ambientes que utilizam Alembic, recomenda‑se executar as
    migrations com `alembic upgrade head` em vez deste helper.
    """

    Base.metadata.create_all(bind=engine)