TradeFlow — Implantação na VPS

Visão geral
- VPS: Ubuntu 24.04 (Hostinger KVM1)
- Proxy reverso: Traefik v3 em /opt/traefik (TLS Let’s Encrypt HTTP‑01)
- Firewall: UFW (22/80/443), Fail2ban ativo
- Docker/Compose instalados; rede padrão do Compose

Diretórios
- Código do TradeFlow: /opt/tradeflow
- Sessão do bot: tradeflow_user.session permanece no diretório do projeto (copiada para a imagem durante o build — opção B)
- Logs da aplicação: por padrão ficam dentro do container; caso queira persistir, mapeie /app/logs para /opt/tradeflow/logs
- Dados do Postgres: volume docker nomeado tradeflow_postgres_data

Como o serviço roda
- docker-compose.yml define dois serviços:
  - db: postgres:15-alpine (porta 5432 exposta para compatibilidade com clientes externos)
  - bot: imagem construída a partir do Dockerfile; aplica migrações Alembic e inicia o bot do Telegram (polling)
- Restart policy: always (como no compose original)

Variáveis de ambiente (.env em /opt/tradeflow)
- POSTGRES_USER=tradeflow
- POSTGRES_PASSWORD=<senha-forte>
- POSTGRES_DB=tradeflow
- TELEGRAM_BOT_TOKEN=<token do bot>
- API_ID=<api id do Telegram>
- API_HASH=<api hash>
- ADMIN_TELEGRAM_ID=<id numérico do admin>
- ERROR_CHANNEL_ID=0 (ou um chat id para erros)
- ENCRYPTION_KEY=<chave gerada: python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'>
- MIGRATIONS_MAX_TRIES=20
- MIGRATIONS_RETRY_SLEEP=3

Deploy automático (GitHub Actions)
- Workflow: .github/workflows/deploy.yml
- Disparo: push na branch main ou manual (workflow_dispatch)
- Requisitos de Secrets no repositório:
  - SSH_HOST=72.60.159.107
  - SSH_PORT=22
  - SSH_USER=deploy
  - SSH_PRIVATE_KEY=(chave privada correspondente à pública em /home/deploy/.ssh/authorized_keys)
- O workflow executa:
  1) checkout do repositório
  2) rsync do código para /opt/tradeflow (exclui .env e diretórios não necessários)
  3) no servidor: garante logs/, dá permissão ao start.sh, build/pull e docker compose up -d

Operações comuns
- Ver status dos containers: docker ps
- Logs do bot: docker logs -f tradeflow_bot
- Logs do Traefik (TLS): docker logs -f traefik
- Entrar no psql: docker exec -it tradeflow_db psql -U $POSTGRES_USER -d $POSTGRES_DB
- Reiniciar apenas o bot: docker compose restart bot

Backup
- Banco: docker exec tradeflow_db pg_dump -U $POSTGRES_USER $POSTGRES_DB > /opt/tradeflow/backup_$(date +%F).sql
- Sessão do Telethon: tradeflow_user.session (mantida no repositório nesta opção). Sugestão futura: mover para /opt/tradeflow/data e montar volume.

Segurança
- UFW limita portas a 22/80/443; Fail2ban ativo
- Observação: a porta 5432 está exposta para conveniência; para maior segurança, remova o mapeamento de porta no compose.

Notas
- Este projeto usa polling do Telegram; não requer domínio nem roteamento Traefik.
- Para serviços HTTP futuros (ex.: webhook de pagamentos), crie um novo serviço e exponha via Traefik com labels.
