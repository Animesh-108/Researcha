# ============================================================
# fix_db.py — One-time fix for trades stuck as "OPEN"
# Run once: python fix_db.py
# Safe to delete after running.
# ============================================================

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from logger import Trade, Base
from config import CONFIG

engine  = create_engine(f"sqlite:///{CONFIG['db_path']}", echo=False)
Session = sessionmaker(bind=engine)
session = Session()

open_trades = session.query(Trade).filter(Trade.outcome == "OPEN").all()
print(f"Found {len(open_trades)} trades stuck as OPEN")

for t in open_trades:
    # These were paper trades closed with bad data — mark as LOSS with 0 P&L
    t.outcome     = "LOSS"
    t.close_price = 0.0
    t.pnl         = 0.0
    t.pips        = 0.0
    t.balance_after = CONFIG.get("starting_balance", 500.0)
    print(f"  Fixed: {t.pair} {t.direction} @ {t.fill_price} → LOSS $0.00 (data lost)")

session.commit()
session.close()
print("Done. These trades had bad data — they won't affect future signals.")
print("You can delete fix_db.py now.")
