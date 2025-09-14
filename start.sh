#!/bin/sh
set -e

echo "Aplicando migrações do banco de dados..."
# Tenta aplicar migrações com pequenos retries para aguardar o DB/start lento
MAX_TRIES=${MIGRATIONS_MAX_TRIES:-20}
SLEEP_SECS=${MIGRATIONS_RETRY_SLEEP:-3}
TRY=1
until alembic upgrade head; do
  if [ "$TRY" -ge "$MAX_TRIES" ]; then
    echo "[FATAL] Falha ao aplicar migrações após $TRY tentativas. Abortando."
    exit 1
  fi
  echo "[WARN] Alembic falhou (tentativa $TRY/$MAX_TRIES). Aguardando $SLEEP_SECS s e tentando novamente..."
  TRY=$((TRY+1))
  sleep "$SLEEP_SECS"
done

echo "Iniciando o bot TradeFlow..."
exec python main.py
