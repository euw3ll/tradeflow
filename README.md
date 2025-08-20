# TradeFlow

Automação de negociações com integração ao Telegram e Bybit.

## Requisitos
- Python 3.11+
- PostgreSQL 14+

## Instalação
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

Crie um arquivo `.env` baseado em `.env.example` e defina a variável `DATABASE_URL` no formato `postgresql://usuario:senha@host:5432/tradeflow`.

## Migrações de Banco
O projeto utiliza **Alembic** com estratégia **expand/contract**.

1. **Expand**: mudanças aditivas que não quebram o schema.
   ```bash
   alembic revision -m "add nova coluna" --version-path alembic/versions/expand
   ```
2. **Contract**: mudanças destrutivas após o código estar em produção.
   ```bash
   alembic revision -m "drop coluna antiga" --version-path alembic/versions/contract
   ```
3. Aplicação de migrações:
   ```bash
   alembic upgrade head
   ```

As migrations são idempotentes e podem ser executadas múltiplas vezes sem efeitos colaterais.

## Backup e Restore
Antes de aplicar migrations em produção, realize backup:
```bash
pg_dump $DATABASE_URL > backup.sql
```
Para restaurar:
```bash
psql $DATABASE_URL < backup.sql
```

## Execução
```bash
./start.sh
```
O script executa as migrations pendentes (`alembic upgrade head`) e inicia o bot.

## Códigos de Convite
Gere códigos com expiração usando o script:

```bash
python scripts/create_invite.py MEU-CODIGO 30  # 30 dias de validade
```

Os códigos são armazenados de forma criptográfica e expiram automaticamente.

