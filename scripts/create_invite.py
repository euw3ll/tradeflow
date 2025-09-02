import sys
import os

# Adiciona o diretório raiz ao path para permitir a importação de módulos do projeto
# Isso é importante para o script rodar tanto localmente quanto via `exec`
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database.session import SessionLocal
from database.models import InviteCode

def create_invite_code(code: str):
    """Cria um novo código de convite no banco de dados."""
    if not code:
        print("Erro: Nenhum código foi fornecido.")
        return

    db = SessionLocal()
    try:
        existing_code = db.query(InviteCode).filter(InviteCode.code == code).first()
        if existing_code:
            print(f"Código '{code}' já existe.")
            return

        new_code = InviteCode(code=code)
        db.add(new_code)
        db.commit()
        print(f"Código de convite '{code}' criado com sucesso!")
    except Exception as e:
        db.rollback()
        print(f"Ocorreu um erro ao criar o código: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    # O script agora lê o código a partir do argumento da linha de comando
    if len(sys.argv) > 1:
        code_to_create = sys.argv[1]
        create_invite_code(code_to_create)
    else:
        print("Erro: Por favor, forneça um código para criar.")
        print("Exemplo: python -m scripts.create_invite MEU-CODIGO-NOVO")