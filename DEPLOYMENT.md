TradeFlow — Implantação na VPS

Versão atual
- TradeFlow v1.0.0 (2025-09-30)
- Escopo: bot Telegram em produção (polling), Postgres via Docker Compose, deploy automatizado (GitHub Actions), utilidades /admin (criar convite, listar canais/tópicos com botão Voltar, ver alvos ativos).

Visão geral
- VPS: Ubuntu 24.04 (Hostinger KVM1)
- Proxy reverso: Traefik v3 em /opt/traefik (TLS Let’s Encrypt HTTP‑01)
- Firewall: UFW (22/80/443), Fail2ban ativo
- Docker/Compose instalados; rede padrão do Compose

Diretórios
- Código do TradeFlow: /opt/tradeflow
- Sessão do bot: persistida em /opt/tradeflow/data (montada em /data no container)
- Logs da aplicação: por padrão ficam dentro do container; caso queira persistir, mapeie /app/logs para /opt/tradeflow/logs
- Dados do Postgres: volume docker nomeado tradeflow_postgres_data

Como o serviço roda
- docker-compose.yml define dois serviços:
  - db: postgres:15-alpine (porta 5432 exposta para compatibilidade com clientes externos)
  - bot: imagem construída a partir do Dockerfile; aplica migrações Alembic e inicia o bot do Telegram (polling)
- Restart policy: always (como no compose original)
- Persistência: volume bind ./data -> /data no serviço "bot" para guardar tradeflow_user.session

Funcionalidades administrativas (v1.0.0)
- Menu /admin (somente ADMIN_TELEGRAM_ID):
  - 📡 Listar Grupos/Canais → agora com “⬅️ Voltar ao Menu Admin”.
  - 🎟️ Criar Código de Convite (gera e salva InviteCode e exibe no chat).
  - 👁️ Ver Alvos Ativos (canais/tópicos monitorados).

Variáveis de ambiente (.env em /opt/tradeflow)
- TELEGRAM_BOT_TOKEN=<token do bot>
- ADMIN_TELEGRAM_ID=<id numérico do admin>
- ERROR_CHANNEL_ID=<opcional: chat id para erros>
- ENCRYPTION_KEY=<Fernet urlsafe base64 de 44 chars>
- API_ID=<api id do Telegram>
- API_HASH=<api hash>
- POSTGRES_USER=tradeflow_user
- POSTGRES_PASSWORD=<senha URL‑safe>
- POSTGRES_DB=tradeflow_db
- LOGS_DIR=logs
- MIGRATIONS_MAX_TRIES=20
- MIGRATIONS_RETRY_SLEEP=3
- (fora do compose) DATABASE_URL=postgresql://USER:PASS@localhost:5432/DB

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

Diagnóstico rápido (app "não está online")
- Ver containers: `docker compose ps`
- Logs do bot: `docker logs -f tradeflow_bot`
- Erros comuns e correções:
  - Token/API inválidos: confira `/opt/tradeflow/.env` (TELEGRAM_BOT_TOKEN, API_ID, API_HASH).
  - Falha ao aplicar migrações: aguarde os retries do `start.sh` ou verifique a saúde do DB (`docker inspect --format='{{json .State.Health}}' tradeflow_db | jq`).
  - Quebra por versão do PTB: o código já está compatível com python-telegram-bot v22+ via `run_polling()` com fallback. Se ainda ver erro “Application has no attribute updater”, redeploy após `git push`.
  - Permissões do `start.sh`: o workflow ajusta antes do build; confirme dentro do container com `docker exec -it tradeflow_bot ls -l /app/start.sh`.

Comandos úteis
- Recriar apenas o bot: `docker compose up -d --build bot`
- Limpar imagens antigas: `docker image prune -f`
- Acessar shell do bot: `docker exec -it tradeflow_bot bash` (ou `sh` na imagem slim)

Como visualizar/copiar o .env da VPS
- Exibir no terminal: `ssh deploy@SEU_IP "sed -n '1,200p' /opt/tradeflow/.env"`
- Copiar para local: `scp deploy@SEU_IP:/opt/tradeflow/.env ./tradeflow.env.vps`
- Conferir variáveis em execução: `docker exec tradeflow_bot env | egrep 'TELEGRAM|API_|ENCRYPTION|POSTGRES|DATABASE_URL'`

Release v1.0.0 — Resumo
- Infra: Postgres 15-alpine; bot Python 3.12; Alembic no boot com retries.
- Segurança: senha Postgres URL‑safe; ENCRYPTION_KEY válida na VPS; .env não versionado.
- Operação: deploy por GitHub Actions (rsync + compose up -d); sessão Telethon persistida em /opt/tradeflow/data.
- Admin: criar código de convite no /admin; listagem com botão Voltar; visualizar alvos ativos.
