import logging
from datetime import datetime, timedelta
from database.session import SessionLocal
from database.models import Trade, User
from services.bybit_service import get_pnl_for_period
from utils.security import decrypt_data
from utils.config import ADMIN_ID

logger = logging.getLogger(__name__)

async def generate_performance_report(start_dt: datetime, end_dt: datetime) -> str:
    """
    Busca os trades fechados de um usuário em um período e gera um relatório de texto.
    """
    db = SessionLocal()
    try:
        # Busca os trades que foram fechados no período
        closed_trades = db.query(Trade).filter(
            Trade.user_telegram_id == ADMIN_ID,
            Trade.status.like('%CLOSED%'),
            Trade.created_at >= start_dt,
            Trade.created_at <= end_dt
        ).all()

        if not closed_trades:
            return "Nenhum trade fechado encontrado para o período selecionado."

        # --- Cálculos das estatísticas ---
        wins = [t for t in closed_trades if t.status == 'CLOSED_PROFIT']
        losses = [t for t in closed_trades if t.status == 'CLOSED_LOSS']
        
        total_trades_for_rate = len(wins) + len(losses)
        total_signals = len(closed_trades)

        win_rate = (len(wins) / total_trades_for_rate) * 100 if total_trades_for_rate > 0 else 0
        
        # Busca o P/L na Bybit para o mesmo período
        user = db.query(User).filter_by(telegram_id=ADMIN_ID).first()
        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)
        
        pnl_result = await get_pnl_for_period(api_key, api_secret, start_dt, end_dt)
        pnl_value = pnl_result.get("pnl", 0.0) if pnl_result.get("success") else 0.0
        pnl_display = f"📈 <b>Lucro:</b> ${pnl_value:,.2f}" if pnl_value >= 0 else f"📉 <b>Prejuízo:</b> ${abs(pnl_value):,.2f}"

        # --- Montagem da Mensagem ---
        report_message = (
            f"<b>📊 Desempenho do Período</b>\n"
            f"<i>De {start_dt.strftime('%d/%m/%Y')} a {end_dt.strftime('%d/%m/%Y')}</i>\n\n"
            f"{pnl_display}\n\n"
            f"<b>Taxa de Acerto:</b> {win_rate:.2f}%\n"
            f"<b>Total de Trades:</b> {total_signals}\n"
            f"  - Ganhos: {len(wins)} ({ (len(wins)/total_signals)*100 if total_signals > 0 else 0 :.1f}%)\n"
            f"  - Perdas: {len(losses)} ({ (len(losses)/total_signals)*100 if total_signals > 0 else 0 :.1f}%)"
        )
        # Nota: As estatísticas de "Cancelados" e "Média de Alvos" podem ser adicionadas futuramente.

        return report_message

    except Exception as e:
        logger.error(f"Erro ao gerar relatório de performance: {e}", exc_info=True)
        return "Ocorreu um erro ao gerar seu relatório."
    finally:
        db.close()