import asyncio
from telethon.sync import TelegramClient
from dotenv import load_dotenv
import os

# Carrega as variáveis de ambiente (API_ID, API_HASH)
load_dotenv()
API_ID = os.getenv('API_ID')
API_HASH = os.getenv('API_HASH')

# --- CORREÇÃO PRINCIPAL ---
# Define o caminho completo para salvar o arquivo dentro do volume persistente
SESSION_NAME = '/data/tradeflow_user'

async def main():
    print(f"Gerando o arquivo de sessão em '{SESSION_NAME}.session'...")
    # Usa 'with' para garantir que o cliente se desconecte corretamente
    async with TelegramClient(SESSION_NAME, API_ID, API_HASH) as client:
        me = await client.get_me()
        print(f"Login bem-sucedido como: {me.first_name}")
        print("Arquivo de sessão foi criado/atualizado com sucesso no local correto.")

if __name__ == "__main__":
    asyncio.run(main())