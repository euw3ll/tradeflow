# imports no topo
from services.bybit_service import get_closed_pnl_breakdown
from utils.security import decrypt_data
from database.session import SessionLocal
from database.models import Trade, User
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

async def generate_performance_report(user_id: int, start_dt: datetime, end_dt: datetime) -> str:
    """
    Gera relatório do período usando o closed PnL da Bybit (fonte de verdade)
    e mostra contagem de ganhos/perdas e hit rate. Usa DB apenas como apoio.
    """
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(telegram_id=user_id).first()
        if not user or not user.api_key_encrypted:
            return "Você precisa ter uma chave de API configurada para ver o desempenho financeiro."

        api_key = decrypt_data(user.api_key_encrypted)
        api_secret = decrypt_data(user.api_secret_encrypted)

        # dados oficiais da corretora
        br = await get_closed_pnl_breakdown(api_key, api_secret, start_dt, end_dt)
        if not br.get("success"):
            logger.error(f"get_closed_pnl_breakdown falhou: {br.get('error')}")
            return "Não foi possível calcular seu desempenho agora."

        total = br["total_pnl"]
        wins = br["wins"]
        losses = br["losses"]
        trades = br["trades"]
        hit_rate = (wins / trades * 100.0) if trades else 0.0

        # (opcional) também buscamos no DB só para exibir um “Total de Trades” local, se quiser
        closed_trades_db = db.query(Trade).filter(
            Trade.user_telegram_id == user_id,
            Trade.status.like('%CLOSED%'),
            Trade.created_at >= start_dt,
            Trade.created_at <= end_dt
        ).count()

        lucro_str = f"📈 <b>Lucro:</b> ${total:.2f}" if total >= 0 else f"📉 <b>Prejuízo:</b> ${abs(total):.2f}"

        msg = (
            f"<b>📊 Desempenho do Período</b>\n"
            f"<i>De {start_dt:%d/%m/%Y} a {end_dt:%d/%m/%Y}</i>\n\n"
            f"{lucro_str}\n\n"
            f"📊 <b>Taxa de Acerto:</b> {hit_rate:.2f}%\n"
            f"📦 <b>Total de Trades:</b> {trades} "
            f"(local: {closed_trades_db})\n"
            f"  - Ganhos: {wins}\n"
            f"  - Perdas: {losses}\n"
        )
        return msg

    except Exception as e:
        logger.error(f"Erro ao gerar relatório de performance para {user_id}: {e}", exc_info=True)
        return "Ocorreu um erro ao gerar seu relatório."
    finally:
        db.close()
