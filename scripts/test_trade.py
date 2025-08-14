import os
from dotenv import load_dotenv
from services.bybit_service import get_account_info
import json

load_dotenv()

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

def run_auth_test():
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        print("❌ Por favor, defina BYBIT_API_KEY e BYBIT_API_SECRET no seu arquivo .env")
        return

    # --- LINHAS DE DIAGNÓSTICO ADICIONADAS ---
    print("="*40)
    print("VERIFICAÇÃO DAS CHAVES CARREGADAS PELO SCRIPT:")
    print(f"API Key lida....: {BYBIT_API_KEY[:4]}...{BYBIT_API_KEY[-4:]}")
    print(f"API Secret lida..: {BYBIT_API_SECRET[:4]}...{BYBIT_API_SECRET[-4:]}")
    print("="*40)
    # ---------------------------------------------

    print("\n▶️  Tentando autenticar e buscar o saldo da conta de testes...")
    
    result = get_account_info(BYBIT_API_KEY, BYBIT_API_SECRET)
    
    print("\n" + "="*30)
    if result.get("success"):
        print("✅ Autenticação bem-sucedida!")
        print(json.dumps(result.get("data"), indent=2))
    else:
        print("❌ Falha na autenticação.")
        print(f"Motivo: {result.get('error')}")
    print("="*30)

if __name__ == "__main__":
    run_auth_test()