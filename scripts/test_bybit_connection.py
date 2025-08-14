import os
import logging
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

# Configura o logging para vermos mais detalhes em caso de erro
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def test_connection():
    """
    Script de teste isolado para validar a conexão e as credenciais da Bybit.
    """
    # 1. Carrega as credenciais do seu arquivo .env
    load_dotenv()
    api_key = os.getenv("BYBIT_API_KEY")
    api_secret = os.getenv("BYBIT_API_SECRET")

    if not api_key or not api_secret:
        print("\n❌ ERRO: Verifique se BYBIT_API_KEY e BYBIT_API_SECRET estão no seu arquivo .env")
        return

    print("\n--- INICIANDO TESTE DE CONEXÃO COM A BYBIT ---")
    print(f"Usando API Key que termina em: ...{api_key[-4:]}")

    try:
        # 2. Tenta criar a sessão e buscar o saldo (exatamente como o bot faz)
        session = HTTP(
            testnet=False, # Conectando na conta REAL
            api_key=api_key,
            api_secret=api_secret
        )
        
        print("\nEtapa 1: Conectando e buscando saldo da Carteira Unificada...")
        response = session.get_wallet_balance(accountType="UNIFIED")

        # 3. Analisa a resposta da Bybit
        if response.get('retCode') == 0:
            print("\n✅ SUCESSO! Conexão bem-sucedida e permissões corretas.")
            balance = response['result']['list'][0]['totalEquity']
            print(f"   - Saldo Total da Conta Unificada: {balance} USDT")
        else:
            print("\n❌ FALHA! A Bybit retornou um erro.")
            print(f"   - Código do Erro: {response.get('retCode')}")
            print(f"   - Mensagem da API: {response.get('retMsg')}")

    except Exception as e:
        print("\n❌ FALHA CRÍTICA! Ocorreu uma exceção ao tentar conectar.")
        print("   Isso geralmente indica um problema de rede (bloqueio de IP) ou de configuração do ambiente.")
        print(f"\n   Detalhes do Erro:\n   {e}")

    print("\n--- TESTE FINALIZADO ---")

if __name__ == "__main__":
    test_connection()