import sys
import os
import argparse
from sqlalchemy.orm import Session
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Adiciona o diretório raiz ao path para permitir a importação de módulos do projeto
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database.models import Base, Trade
from database.session import DATABASE_URL  # Importa a URL do banco de dados

def cleanup_trades(db: Session, status: str, user_id: int = None, dry_run: bool = True):
    """
    Busca e, opcionalmente, exclui trades com base em seu status e ID de usuário.
    """
    query = db.query(Trade).filter(Trade.status == status)
    
    if user_id:
        query = query.filter(Trade.user_telegram_id == user_id)
        
    trades_to_delete = query.all()
    count = len(trades_to_delete)
    
    user_filter_str = f" para o usuário com ID {user_id}" if user_id else ""
    
    if count == 0:
        print(f"Nenhum trade com o status '{status}' encontrado{user_filter_str}.")
        return

    print(f"Encontrados {count} trades com o status '{status}'{user_filter_str}.")

    if dry_run:
        print("\n--- MODO DE SIMULAÇÃO (DRY-RUN) ---")
        print("Os seguintes trades seriam excluídos:")
        for trade in trades_to_delete:
            print(f"  - ID: {trade.id}, Símbolo: {trade.symbol}, Usuário: {trade.user_telegram_id}, Data: {trade.closed_at}")
        print("\nNenhuma alteração foi feita no banco de dados.")
    else:
        # Confirmação final antes de excluir
        confirm = input(f"\n!!! ATENÇÃO !!! Você tem certeza que deseja excluir permanentemente estes {count} trades? (s/N): ")
        if confirm.lower() == 's':
            try:
                # Usa delete() em vez de um loop para mais performance
                query.delete(synchronize_session=False)
                db.commit()
                print(f"\n✅ Sucesso! {count} trades foram excluídos permanentemente.")
            except Exception as e:
                db.rollback()
                print(f"\n❌ Erro ao excluir trades: {e}")
        else:
            print("\nOperação cancelada pelo usuário.")

def main():
    """
    Função principal para executar o script via linha de comando.
    """
    parser = argparse.ArgumentParser(description="Script de limpeza para trades no banco de dados.")
    parser.add_argument("--status", type=str, required=True, help="O status dos trades a serem excluídos (ex: CLOSED_GHOST).")
    parser.add_argument("--user-id", type=int, help="(Opcional) ID de usuário do Telegram para filtrar a exclusão.")
    parser.add_argument("--dry-run", action="store_true", help="Apenas simula a exclusão, sem fazer alterações no banco.")
    
    args = parser.parse_args()

    # Configuração da sessão do banco de dados
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = SessionLocal()
    
    try:
        cleanup_trades(db, status=args.status, user_id=args.user_id, dry_run=args.dry_run)
    finally:
        db.close()

if __name__ == "__main__":
    main()