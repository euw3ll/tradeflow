import asyncio
import logging
from datetime import datetime, timedelta

import pytz

from database.session import SessionLocal
from database.models import User, Trade
from utils.security import decrypt_data
from services.bybit_service import get_closed_pnl_for_trade


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reconcile")


async def _recompute_for_trade(session, user: User, trade: Trade) -> bool:
    api_key = decrypt_data(user.api_key_encrypted)
    api_secret = decrypt_data(user.api_secret_encrypted)

    start_ts = trade.created_at
    if not start_ts:
        start_ts = datetime.now(pytz.utc) - timedelta(hours=12)

    agg = await get_closed_pnl_for_trade(
        api_key=api_key,
        api_secret=api_secret,
        symbol=trade.symbol,
        side=trade.side,
        start_time=start_ts,
        end_time=None,
    )

    if not agg.get("success"):
        logger.warning("skip trade_id=%s symbol=%s: %s", trade.id, trade.symbol, agg.get("error"))
        return False

    gross = float(agg.get("gross_pnl", 0) or 0)
    fees = float(agg.get("fees", 0) or 0)
    funding = float(agg.get("funding", 0) or 0)
    net = float(agg.get("net_pnl", 0) or (gross - fees - funding))

    et = (agg.get("exit_type") or "").lower()
    status = (
        "CLOSED_PROFIT" if et.startswith("take") else
        "CLOSED_LOSS" if et.startswith("stop") else
        ("CLOSED_PROFIT" if net >= 0 else "CLOSED_LOSS")
    )

    trade.closed_pnl = net
    trade.status = status
    if not trade.closed_at:
        trade.closed_at = datetime.now(pytz.utc)

    session.commit()
    logger.info("updated trade_id=%s symbol=%s side=%s net=%.6f fees=%.6f funding=%.6f",
                trade.id, trade.symbol, trade.side, net, fees, funding)
    return True


async def main(days: int = 3):
    """
    Recalcula o closed_pnl e o status (CLOSED_PROFIT/CLOSED_LOSS) dos trades fechados
    nos últimos N dias, usando a API da Bybit e o fix de mapeamento de lado.
    """
    db = SessionLocal()
    try:
        cutoff = datetime.now(pytz.utc) - timedelta(days=days)
        users = db.query(User).filter(User.api_key_encrypted.isnot(None)).all()
        total = 0
        ok = 0
        for u in users:
            closed = db.query(Trade).filter(
                Trade.user_telegram_id == u.telegram_id,
                Trade.status.like('%CLOSED%'),
                Trade.closed_at.isnot(None),
                Trade.closed_at >= cutoff,
            ).order_by(Trade.closed_at.desc()).all()
            for t in closed:
                total += 1
                try:
                    res = await _recompute_for_trade(db, u, t)
                    ok += 1 if res else 0
                except Exception as e:
                    logger.error("failed to update trade_id=%s: %s", t.id, e, exc_info=True)
        logger.info("Reconciled %d/%d trades (last %d days).", ok, total, days)
    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())

