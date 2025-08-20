# TradeFlow

## Requisitos
- Python 3.11+
- Pip
- Dependências listadas em [`requirements.txt`](requirements.txt)

## Configuração Local
1. Clone o repositório e acesse a pasta do projeto.
2. Crie e ative um ambiente virtual:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```
3. Instale as dependências:
   ```bash
   pip install -r requirements.txt
   ```
4. Copie o arquivo `.env.example` para `.env` e preencha as variáveis de ambiente necessárias.
5. Inicialize o banco de dados (se necessário):
   ```bash
   python -c "from database.session import init_db; init_db()"
   ```
6. Execute o bot:
   ```bash
   python main.py
   ```

## Testes
Execute a suíte de testes com:
```bash
pytest
```

## Build
Para gerar a imagem Docker:
```bash
docker build -t tradeflow .
```

## Deploy
O projeto pode ser implantado na Fly.io:
```bash
fly deploy
```

## Variáveis de Ambiente
As variáveis a seguir devem ser definidas no arquivo `.env`:

- `TELEGRAM_BOT_TOKEN`
- `ENCRYPTION_KEY`
- `API_ID`
- `API_HASH`
- `ADMIN_TELEGRAM_ID`
- `BYBIT_API_KEY`
- `BYBIT_API_SECRET`
- `TAILSCALE_AUTHKEY`

Consulte o arquivo `.env.example` para um modelo.
