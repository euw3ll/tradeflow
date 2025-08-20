from cryptography.fernet import Fernet
import bcrypt
from .config import ENCRYPTION_KEY

# Inicializa o 'cofre' com a sua chave
cipher_suite = Fernet(ENCRYPTION_KEY.encode())

def encrypt_data(data: str) -> str:
    """Criptografa um texto e retorna a versão em string."""
    if not data:
        return None
    encrypted_bytes = cipher_suite.encrypt(data.encode())
    return encrypted_bytes.decode()

def decrypt_data(encrypted_data: str) -> str:
    """Descriptografa um texto e retorna a versão original."""
    if not encrypted_data:
        return None
    decrypted_bytes = cipher_suite.decrypt(encrypted_data.encode())
    return decrypted_bytes.decode()


def hash_invite_code(code: str) -> str:
    """Gera um hash seguro para o código de convite usando bcrypt."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(code.encode(), salt).decode()


def verify_invite_code(code: str, hashed: str) -> bool:
    """Verifica se um código corresponde ao hash armazenado."""
    try:
        return bcrypt.checkpw(code.encode(), hashed.encode())
    except ValueError:
        return False