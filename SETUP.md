# Trading System — Complete Setup Guide
## Exness MT5 + Claude Sonnet + Telegram

> **Important:** This system only works on Windows. The MT5 Python library requires Windows.

---

## What this system does

Scans EUR/USD, GBP/USD, USD/JPY every 5 minutes during London + NY sessions.
Python pre-filter (free, 9 checks) → Claude Sonnet analysis → Telegram signal → you tap Accept/Reject → Exness MT5 executes the trade with SL and TP attached automatically.

**Default: PAPER TRADING** — no real money touched until you change `paper_trading = False`.

---

## Step 1 — Open Exness demo account

1. Go to **exness.com**
2. Register with your NID (national ID) and selfie for KYC
3. Open a **MetaTrader 5 (MT5) Demo** account
4. Note your: **Login number**, **Password**, and **Server name**
   - Server is usually something like `Exness-MT5Trial4` or `Exness-MT5Demo`
   - Find exact server name in: Exness Personal Area → MT5 Account Details
5. Download and install **MetaTrader 5 for Windows** from exness.com
6. Open MT5 and log in with your demo credentials
7. In Market Watch, verify: `EURUSD`, `GBPUSD`, `USDJPY` are visible
   - If you see `EURUSDm` or similar, update `SYMBOL_MAP` in `config.py`

---

## Step 2 — Get Anthropic API key (~$5)

1. Go to **console.anthropic.com**
2. Sign up → Billing → Add $5 minimum credit
3. API Keys → Create key
4. Copy the key (starts with `sk-ant-...`)

At ~$0.004 per signal analysis, $5 = over 1,000 analyses. Enough for months of demo.

---

## Step 3 — Create Telegram bot (free)

1. Open Telegram → search **@BotFather**
2. Type `/newbot` → follow prompts → copy the **Bot Token**
3. Search **@userinfobot** → message it → copy your **Chat ID** (a number like `123456789`)

---

## Step 4 — Install Python packages

Open Command Prompt in the trading-system folder:

```cmd
pip install -r requirements.txt
```

---

## Step 5 — Configure your .env file

```cmd
copy .env.example .env
notepad .env
```

Fill in your values:
```
MT5_LOGIN=12345678
MT5_PASSWORD=your_mt5_password
MT5_SERVER=Exness-MT5Trial4
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_BOT_TOKEN=1234567890:ABCdef...
TELEGRAM_CHAT_ID=123456789
```

---

## Step 6 — Verify symbol names

Open `config.py` and check the `SYMBOL_MAP` section:
```python
SYMBOL_MAP = {
    "EURUSD": "EURUSD",    # ← change to "EURUSDm" if that's what Exness shows
    "GBPUSD": "GBPUSD",
    "USDJPY": "USDJPY",
}
```

Open MT5 → Market Watch → right-click → Show All. Find the exact name Exness uses.

---

## Step 7 — Verify paper trading is ON

Open `config.py` and confirm:
```python
"paper_trading": True,   # ← must be True during demo
```

---

## Step 8 — Run the system

**MT5 terminal must be open and logged in before running.**

```cmd
python main.py
```

You should see:
- "MT5 connected | Account: 12345678 | Balance: $10000.00"
- "Telegram bot polling started"
- A startup notification in your Telegram

---

## How signals work on your phone

1. System fires a signal to your Telegram app
2. You see: pair, direction (BUY/SELL), confidence %, SL, TP, R:R, reasoning
3. Tap **✅ ACCEPT** or **❌ REJECT** within 3 minutes
4. If accepted → trade placed on Exness MT5 automatically (SL + TP attached)
5. MT5 closes the trade when SL or TP is hit
6. You receive: WIN/LOSS result + P&L

---

## Telegram commands

| Command | What it does |
|---------|-------------|
| `/status` | System running? Balance? Drawdown? |
| `/balance` | Current account balance |
| `/today` | Today's trades and P&L |
| `/week` | Week's performance |
| `/month` | Month's performance |
| `/trades` | Currently open trades |
| `/history` | Last 10 completed trades |
| `/pause` | Pause new signals |
| `/resume` | Resume signals |
| `/stop` | Emergency stop (no new signals) |
| `/review` | Run AI weekly review now |
| `/health` | Check all connections |
| `/settings` | View current settings |
| `/help` | All commands |

---

## Risk rules (hardcoded)

- 1% risk per trade
- Max 3% daily loss → stops for the day
- Max 3 consecutive losses → pauses
- Max 5 trades per day
- Max 2 open trades at once
- EUR/USD and GBP/USD cannot both be open (correlated)
- Trading hours: 7am – 9pm UTC only
- No trading 30min before / 15min after high-impact news

---

## 60-Day Demo Plan

| Period | What to do |
|--------|-----------|
| Days 1–7 | Setup, test signals fire correctly |
| Days 8–21 | Paper trading, accept real-looking signals |
| Days 22–35 | Backtest on 6 months historical data |
| Days 36–60 | Full demo with logging — do not change settings |
| Day 61 | Evaluate: win rate > 52% over 150+ trades? |

**Go live only after ALL 5 criteria are met:**
- Win rate > 52%
- Total trades > 150
- Max drawdown < 20%
- Profit factor > 1.2
- System overrides < 10

---

## Going live (after 60 days)

1. Fund Exness with $10–100
2. Open `config.py`: `"paper_trading": False`
3. Trade at half size for first 2 weeks (first change will be in risk_per_trade: 0.005)
4. Compare live vs demo results

---

## Monthly costs

| Item | Cost |
|------|------|
| Exness | Free |
| MT5 | Free |
| Claude API | ~$10–15/month |
| Windows VPS (live phase) | ~$12/month |
| Everything else | Free |
| **Total demo** | **~$10–15** |
| **Total live** | **~$22–27/month** |

---

## Project files reference

```
trading-system/
├── .env.example        ← copy to .env, fill keys
├── requirements.txt    ← pip install -r this
├── config.py           ← all settings (SYMBOL_MAP here)
├── data_feed.py        ← MT5 live data
├── indicators.py       ← all indicator calculations (local, free)
├── scanner.py          ← 9-check Python pre-filter
├── news_filter.py      ← ForexFactory calendar
├── risk_manager.py     ← all risk rules
├── analyzer.py         ← Claude Sonnet signal engine
├── executor.py         ← MT5 order placement
├── logger.py           ← SQLite trade database
├── notifier.py         ← Telegram bot + all commands
├── trade_monitor.py    ← detects trade closes on MT5
├── health_monitor.py   ← checks all connections every 5min
├── weekly_review.py    ← Claude Opus Sunday analysis
├── main.py             ← everything connected
└── keep_alive.sh       ← auto-restart (Linux VPS only)
```

---

## Common issues

**"MT5 connection failed"**
→ Make sure MetaTrader 5 terminal is open and logged in first, then run main.py

**"EURUSD not found"**
→ Check exact symbol name in MT5 Market Watch, update `SYMBOL_MAP` in config.py

**"Market Watch missing symbols"**
→ In MT5: View → Market Watch → right-click → Show All

**"MT5 Python library not found"**
→ Run: `pip install MetaTrader5`
→ If still fails: you must be on **Windows** — the library does not support Linux natively
