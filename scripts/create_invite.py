import sys
from datetime import datetime, timedelta
from database.session import SessionLocal, init_db
from database.models import InviteCode
from utils.security import hash_invite_code, verify_invite_code


def create_invite_code(code: str, days_valid: int = 7):
    """Cria um novo código de convite com validade limitada."""
    db = SessionLocal()
    try:
        existing_codes = db.query(InviteCode).filter(InviteCode.is_used == False).all()
        for existing in existing_codes:
            if verify_invite_code(code, existing.code_hash):
                print(f"Código '{code}' já existe ou foi gerado anteriormente.")
                return

        expires_at = datetime.utcnow() + timedelta(days=days_valid)
        new_code = InviteCode(code_hash=hash_invite_code(code), expires_at=expires_at)
        db.add(new_code)
        db.commit()
        print(f"Código de convite '{code}' criado com sucesso! Expira em {expires_at} UTC.")
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
    if len(sys.argv) > 1:
        code_to_create = sys.argv[1]
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
        create_invite_code(code_to_create, days)
    else:
        print(
            "Erro: Forneça um código para criar. Exemplo: python create_invite.py MEU-CODIGO 30"
        )

