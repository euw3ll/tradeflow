import sys
from database.session import SessionLocal, init_db
from database.models import InviteCode

def create_invite_code(code: str):
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
    finally:
        db.close()

if __name__ == "__main__":
    init_db()
    # Agora ele lê o código a partir do seu comando no terminal
    if len(sys.argv) > 1:
        code_to_create = sys.argv[1]
        create_invite_code(code_to_create)
    else:
        print("Erro: Por favor, forneça um código para criar. Exemplo: python create_invite.py MEU-CODIGO-NOVO")