# database/bootstrap_schema.py
from sqlalchemy import text
from .session import SessionLocal

def ensure_trades_columns():
    s = SessionLocal()
    try:
        conn = s.connection()
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(trades)")).fetchall()]

        if "missing_cycles" not in cols:
            conn.execute(text(
                "ALTER TABLE trades ADD COLUMN missing_cycles INTEGER NOT NULL DEFAULT 0"
            ))

        if "last_seen_at" not in cols:
            conn.execute(text(
                "ALTER TABLE trades ADD COLUMN last_seen_at DATETIME"
            ))

        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()