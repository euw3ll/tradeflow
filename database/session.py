from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base

# Define o caminho do arquivo do banco de dados
DATABASE_URL = "sqlite:///./tradeflow.db"

# Cria o motor de conexão com o banco
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

# Cria uma fábrica de sessões
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    # Cria todas as tabelas no banco de dados
    Base.metadata.create_all(bind=engine)