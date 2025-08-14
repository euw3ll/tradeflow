# Usa uma imagem oficial do Python como base
FROM python:3.11-slim

# Define o diretório de trabalho dentro do container
WORKDIR /app

# --- NOVA SEÇÃO: INSTALAÇÃO DO TAILSCALE ---
# Adiciona pacotes necessários para a instalação e funcionamento do Tailscale
RUN apt-get update && apt-get install -y ca-certificates curl gnupg && \
    # Adiciona o repositório do Tailscale
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg | tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null && \
    curl -fsSL https://pkgs.tailscale.com/stable/debian/bookworm.tailscale-keyring.list | tee /etc/apt/sources.list.d/tailscale.list && \
    # Instala o Tailscale
    apt-get update && apt-get install -y tailscale && \
    # Limpa o cache
    rm -rf /var/lib/apt/lists/*
# ----------------------------------------------

# Copia o arquivo de dependências primeiro
COPY requirements.txt .

# Instala as dependências do Python
RUN pip install --no-cache-dir -r requirements.txt

# Copia todo o resto do código do projeto
COPY . .

# Comando que será executado quando o container iniciar
CMD ["./start.sh"]