from services.bybit_service import get_closed_pnl_breakdown, get_account_info
from utils.security import decrypt_data
from database.session import SessionLocal
from database.models import Trade, User
from datetime import datetime
import logging
import asyncio

logger = logging.getLogger(__name__)

async def generate_performance_report(user_id: int, start_dt: datetime, end_dt: datetime) -> str:
    """Gera relatório de desempenho, incluindo a rentabilidade sobre o patrimônio."""
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if not user or not user.api_key_encrypted:
            return "Você precisa ter uma chave de API configurada para ver o desempenho."

        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)

        # Busca os dados de P/L e o saldo da conta em paralelo
        pnl_result, account_info = await asyncio.gather(
            get_closed_pnl_breakdown(api_key, api_secret, start_dt, end_dt),
            get_account_info(api_key, api_secret)
        )

        if not pnl_result.get("success"):
            return f"Não foi possível calcular seu desempenho: {pnl_result.get('error')}"

        total_pnl = pnl_result["total_pnl"]
        wins = pnl_result["wins"]
        losses = pnl_result["losses"]
        trades = pnl_result["trades"]
        hit_rate = (wins / trades * 100.0) if trades else 0.0
        
        # --- NOVO CÁLCULO DE RENTABILIDADE ---
        rentabilidade_str = ""
        if account_info.get("success"):
            total_equity = account_info.get("data", {}).get("total_equity", 0.0)
            if total_equity > 0:
                rentabilidade = (total_pnl / total_equity) * 100
                rentabilidade_str = f"🚀 <b>Rentabilidade:</b> {rentabilidade:+.2f}%\n\n"
        
        lucro_str = f"📈 <b>Lucro:</b> ${total_pnl:,.2f}" if total_pnl >= 0 else f"📉 <b>Prejuízo:</b> ${abs(total_pnl):,.2f}"

        msg = (
            f"<b>📊 Desempenho do Período</b>\n"
            f"<i>De {start_dt:%d/%m/%Y} a {end_dt:%d/%m/%Y}</i>\n\n"
            f"{rentabilidade_str}"
            f"{lucro_str}\n\n"
            f"🎯 <b>Taxa de Acerto:</b> {hit_rate:.2f}%\n"
            f"📦 <b>Total de Trades:</b> {trades}\n"
            f"  - Ganhos: {wins}\n"
            f"  - Perdas: {losses}\n"
        )
        return msg

    except Exception as e:
        logger.error(f"Erro ao gerar relatório de performance para {user_id}: {e}", exc_info=True)
        return "Ocorreu um erro ao gerar seu relatório."
    finally:
        db.close()
