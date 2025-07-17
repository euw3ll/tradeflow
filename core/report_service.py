import logging
from database.session import SessionLocal
from database.models import Trade

logger = logging.getLogger(__name__)

def generate_performance_report(user_telegram_id: int) -> str:
    """
    Busca os trades fechados de um usuário e gera um relatório de texto.
    """
    db = SessionLocal()
    try:
        # Busca todos os trades que foram fechados (com lucro ou prejuízo)
        closed_trades = db.query(Trade).filter(
            Trade.user_telegram_id == user_telegram_id,
            Trade.status.like('%CLOSED%')
        ).all()

        if not closed_trades:
            return "Nenhum trade fechado encontrado para gerar um relatório."

        # --- Cálculos ---
        total_trades = len(closed_trades)
        winning_trades = [t for t in closed_trades if t.status == 'CLOSED_PROFIT']
        losing_trades = [t for t in closed_trades if t.status == 'CLOSED_LOSS']
        
        win_rate = (len(winning_trades) / total_trades) * 100 if total_trades > 0 else 0

        # --- Montagem da Mensagem ---
        report_message = "<b>📊 Relatório de Performance do Bot</b>\n\n"
        report_message += f"<b>Total de Trades Fechados:</b> {total_trades}\n"
        report_message += f"<b>Trades Vencedores:</b> {len(winning_trades)}\n"
        report_message += f"<b>Trades Perdedores:</b> {len(losing_trades)}\n"
        report_message += f"<b>Taxa de Acerto:</b> {win_rate:.2f}%\n\n"
        report_message += "Este é um relatório inicial. Futuramente, podemos adicionar o P/L (Lucro/Prejuízo) total."

        return report_message

    except Exception as e:
        logger.error(f"Erro ao gerar relatório de performance: {e}")
        return "Ocorreu um erro ao gerar seu relatório."
    finally:
        db.close()