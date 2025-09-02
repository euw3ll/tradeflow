# generate_session.py
import asyncio
from telethon.sync import TelegramClient
from utils.config import API_ID, API_HASH

async def main():
    # Usamos os mesmos parâmetros do seu código original
    client = TelegramClient('tradeflow_user', API_ID, API_HASH)
    await client.start()
    print("Arquivo 'tradeflow_user.session' gerado com sucesso!")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())