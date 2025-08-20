from .session import SessionLocal
from .models import User

def get_user_by_id(telegram_id: int):
    """Busca um usuário no banco de dados pelo seu ID do Telegram."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        return user
    finally:
        db.close()


def get_admin_user():
    """Retorna o primeiro usuário com papel de administrador."""
    db = SessionLocal()
    try:
        return db.query(User).filter(User.role == 'ADMIN').first()
    finally:
        db.close()