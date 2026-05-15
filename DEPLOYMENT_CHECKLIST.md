# IMMEDIATE ACTION ITEMS — Before Phase 1 Deployment

## CRITICAL (Do These NOW)

### 1. Create `.gitignore` File
```gitignore
# Environment
.env
.env.local
.env.*.local

# Database & Logs
data/
logs/
backups/
*.db
*.db-shm
*.db-wal

# Python
__pycache__/
*.py[cod]
*$py.class
venv/
ENV/

# IDE
.vscode/
.idea/
*.swp
```

### 2. Test MetaTrader 5 Connection
```bash
# Before running main.py:
python -c "import MetaTrader5; print('MT5 OK')"
```

Requirements:
- ✅ MetaTrader 5 terminal OPEN on your Windows PC
- ✅ Logged into Exness demo account
- ✅ EUR/USD, GBP/USD, USD/JPY visible in Market Watch

### 3. Verify .env File
Must have these 6 variables set:
```
MT5_LOGIN=your_demo_login
MT5_PASSWORD=your_password
MT5_SERVER=Exness-MT5-Demo
ANTHROPIC_API_KEY=sk-ant-...your_key...
TELEGRAM_BOT_TOKEN=123456789:ABCdef...
TELEGRAM_CHAT_ID=your_chat_id
```

### 4. Run Backtester Before Going Live
```bash
python backtester.py
```

Must see results like:
- Win rate: >50%
- Profit factor: >1.2
- Max drawdown: <20%
- Trades simulated: >200

If NOT passing → adjust config.py thresholds before proceeding.

### 5. 48-Hour Paper Trading Test
```bash
# Ensure config.py has:
PAPER_TRADING = True  # MUST BE TRUE FOR DEMO

# Then run:
python main.py
```

Monitor for 48 hours:
- ✅ Signals fire every 5-10 minutes
- ✅ Telegram alerts arrive correctly
- ✅ Accept/Reject buttons work
- ✅ Paper trades log to database
- ✅ Daily P&L summary appears in Telegram

---

## HIGHLY RECOMMENDED (Do This Week)

### 6. Create README.md
Add quick-start guide:
- Installation steps
- .env setup
- First run command
- Troubleshooting

### 7. Test Telegram Commands
From Telegram:
```
/status       → Should show system running, balance, drawdown
/balance      → Shows current balance
/today        → Shows today's P&L
/pause        → Pause signals
/resume       → Resume signals
/health       → Show system health (MT5, Claude, Telegram, RAM)
```

### 8. First 24 Hours Monitoring
Watch logs and Telegram:
- ✅ Monitor no "Connection lost" errors
- ✅ Monitor no "Claude timeout" errors
- ✅ Monitor no "Database error" messages
- ✅ Confirm daily summary appears at 22:00 UTC

---

## QUALITY IMPROVEMENTS (Nice to Have)

### 9. Add Type Hints
```python
# Example improvement:
def can_trade(self, pair: Optional[str] = None) -> Tuple[bool, str]:
    """Returns (allowed: bool, reason: str)"""
```

### 10. Add Unit Tests
```bash
pip install pytest
# Create tests/ folder with test_risk_manager.py, etc.
pytest tests/
```

### 11. Add Inline Comments to Complex Logic
- analyzer.py lines 62-69 (ATR calculation)
- risk_manager.py lines 90-121 (position sizing)

---

## DEPLOYMENT TIMELINE

### Week 1 (Items 1-5)
- [ ] Create .gitignore
- [ ] Test MT5 connection
- [ ] Verify .env file
- [ ] Run backtester
- [ ] 48-hour paper trade test

### Week 2 (Items 6-8)
- [ ] Create README.md
- [ ] Test all Telegram commands
- [ ] First 24 hours monitoring complete

### Month 2 (Items 9-11)
- [ ] Add type hints
- [ ] Add unit tests
- [ ] Improve code comments

---

## DEPLOYMENT GO/NO-GO CHECKLIST

Before going LIVE (Phase 4), verify:

### Code Quality
- [ ] No unhandled exceptions in logs
- [ ] All timeouts working (20s Claude, 10s MT5, 15s Telegram)
- [ ] Database backups created daily
- [ ] Memory usage stable (<300MB)

### Trading Metrics (60-day demo target)
- [ ] ≥150 completed trades
- [ ] Win rate ≥52%
- [ ] Profit factor ≥1.2
- [ ] Max drawdown <20%
- [ ] System uptime ≥99%

### Risk Management Confirmed
- [ ] Daily loss limit enforced (3% max)
- [ ] Consecutive loss pause working (3 limit)
- [ ] Drawdown protections active (5%, 10%, 15%, 20%)
- [ ] Position sizing correct (1% risk per trade)
- [ ] Correlation lock working (EUR/USD blocks GBP/USD)

### Telegram & Monitoring
- [ ] Signals arrive on phone
- [ ] Accept/Reject buttons work
- [ ] Trade notifications sent
- [ ] Daily P&L summary received
- [ ] Health alerts trigger on failures

### Database & Backups
- [ ] All trades logged correctly
- [ ] Drawdown calculated correctly
- [ ] Daily backups created
- [ ] Database recovers from simulated crash

---

## TROUBLESHOOTING QUICK REFERENCE

| Problem | Solution |
|---|---|
| "MT5 connection failed" | Open MetaTrader 5, verify logged in, check .env credentials |
| "Claude timeout" | Check internet, verify API key valid, normal if rate-limited |
| "No signals firing" | Check trading hours (07:00-21:00 UTC), check ADX (market trending?) |
| "Telegram not responding" | Verify bot token, check TELEGRAM_CHAT_ID, test /health command |
| "Database locked" | Normal (WAL mode), system will retry automatically |
| "High RAM usage" | Memory leak unlikely (gc.collect() every scan), restart system |

---

## SUCCESS METRICS

### Phase 1 (60-day demo) Target
- ✅ System runs continuously without crashes
- ✅ Win rate ≥52% (breakeven + 2%)
- ✅ Max drawdown <20%
- ✅ Telegram monitoring works reliably
- ✅ 150+ trades completed

### Phase 2 (Move to small live account)
- ✅ Verify live fills match demo
- ✅ Track slippage (typically 2-5 pips worse)
- ✅ Run at 0.5% position size for 2 weeks
- ✅ Confirm P&L matches expected

### Phase 3 (Scale to full 1% position)
- ✅ Maintain ≥52% win rate
- ✅ Maintain profit factor ≥1.2
- ✅ Reach monthly return target (2-5%)

---

Created: May 15, 2026 | Status: READY FOR DEPLOYMENT ✅
