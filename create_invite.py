from database.session import SessionLocal
from database.models import InviteCode

def create_invite_code(code: str):
    db = SessionLocal()
    try:
        # Verifica se o código já existe
        existing_code = db.query(InviteCode).filter(InviteCode.code == code).first()
        if existing_code:
            print(f"Código '{code}' já existe.")
            return

        # Cria o novo código
        new_code = InviteCode(code=code)
        db.add(new_code)
        db.commit()
        print(f"Código de convite '{code}' criado com sucesso!")
    finally:
        db.close()

if __name__ == "__main__":
    # Inicializa o banco e as tabelas caso não existam
    from database.session import init_db
    init_db()

    # Crie seu primeiro código aqui
    create_invite_code("MEU-CODIGO-SECRETO")