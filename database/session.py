import os # Importa a biblioteca 'os'
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base

# --- LÓGICA DE CAMINHO DINÂMICO ---
if os.path.isdir('/data'):
    # Caminho para o banco de dados no servidor Fly.io
    DATABASE_URL = "sqlite:////data/tradeflow.db"
else:
    # Caminho para o banco de dados local (no seu Mac)
    DATABASE_URL = "sqlite:///./tradeflow.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    Base.metadata.create_all(bind=engine)