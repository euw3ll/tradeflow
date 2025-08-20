#!/bin/sh

# Inicia o Tailscale em segundo plano
/usr/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state --socket=/var/run/tailscale/tailscaled.sock &

# Espera um pouco para o Tailscale iniciar
sleep 2

# Conecta à rede Tailscale usando a chave de autenticação
/usr/bin/tailscale up --authkey=${TAILSCALE_AUTHKEY} --hostname="tradeflow-bot"

# Executa migrações pendentes
alembic upgrade head

# Inicia a aplicação principal do bot
echo "Iniciando o bot TradeFlow..."
python main.py
