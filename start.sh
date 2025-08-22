#!/bin/sh

echo "Aplicando migrações do banco de dados..."
# Este comando garante que o DB esteja sempre na versão mais recente
alembic upgrade head

echo "Iniciando o bot TradeFlow..."
python main.py