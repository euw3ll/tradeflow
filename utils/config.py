import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY')

API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')
ADMIN_ID = int(os.getenv('ADMIN_TELEGRAM_ID', 0)) # Converte para int