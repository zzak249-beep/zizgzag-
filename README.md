# 🎯 Sniper Bot V50.6 — High Winrate ATR Breakout Bot

> Fully automated crypto trading bot for BingX Perpetual Futures.
> Multi-timeframe ATR breakout strategy with Kelly Criterion position sizing, Telegram alerts, and Railway deployment.

---

## 🏗️ Architecture

```
main.py
  └─ SniperBot (src/bot.py)         ← Orchestrator / main loop
       ├─ SniperStrategy (src/strategy.py)  ← Signal engine
       ├─ ExchangeClient (src/exchange.py)  ← BingX via CCXT
       ├─ TelegramNotifier (src/telegram.py)← Alerts
       └─ TradeJournal (src/analytics.py)   ← JSON trade log
```

---

## ⚡ Strategy Logic

### Core (from Pine Script V50.6)
| Filter | Value |
|--------|-------|
| ATR Fast/Slow | 5 / 20 periods |
| Breakout Level | `open ± ATR_slow × 2.5` |
| EMA Macro Trend | 200-period EMA |
| Magic Slope | EMA7 derivative / ATR7 × 100 |
| Min Slope | ±40 |
| Relative Volume | ≥ 1.5× 50-bar average |
| TP / SL | ATR14 × 2.0 / ATR14 × 1.2 |

### 🔮 Special Edge (Python additions)
| Feature | Description |
|---------|-------------|
| **Multi-TF confluence** | 4h EMA200 + ADX must agree with 2h signal |
| **ADX filter** | Min 22 — only trade trending markets |
| **RSI guard** | No longs above 70, no shorts below 30 |
| **VWAP filter** | Entry must be on correct side of session VWAP |
| **BB Squeeze** | Prefers entries right after compression |
| **Kelly Criterion** | Dynamic position size adapts to recent win rate |
| **Session filter** | Skips 0-5 UTC dead-liquidity window |
| **Trailing SL** | Auto-moves to breakeven at 50% TP hit |
| **Force-close** | Exits stale positions after `MAX_HOLD_BARS` |
| **Daily report** | Midnight UTC Telegram summary |

---

## 🚀 Quick Start

### 1. Clone & install
```bash
git clone https://github.com/YOUR_USERNAME/sniper-bot.git
cd sniper-bot
pip install -r requirements.txt
```

### 2. Configure
```bash
cp .env.example .env
# Edit .env with your keys
```

### 3. Run locally
```bash
python main.py
```

---

## ☁️ Deploy to Railway (Recommended)

1. Push repo to GitHub
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
3. Select your repo
4. Add environment variables in **Variables** tab (from `.env.example`)
5. Railway auto-builds from `Dockerfile` and runs `python main.py`
6. Bot runs 24/7 — no server management needed

> 💡 Railway free tier gives 5 USD/month credit — enough for this bot.

---

## 🔑 Getting API Keys

### BingX
1. Register: [bingx.com](https://bingx.com)
2. Account → API Management → Create API
3. Enable: **Read**, **Trade** (NOT withdraw)
4. Whitelist your Railway IP or leave open

### Telegram Bot
1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the token
3. Message [@userinfobot](https://t.me/userinfobot) to get your Chat ID
4. Start your bot (send it `/start`)

---

## ⚙️ Key Parameters

| Variable | Default | Description |
|----------|---------|-------------|
| `SYMBOLS` | 8 pairs | Pairs to scan |
| `PRIMARY_TF` | `2h` | Signal timeframe |
| `CONFIRM_TF` | `4h` | MTF confirmation |
| `RISK_PER_TRADE_PCT` | `2.0` | Max risk per trade |
| `LEVERAGE` | `5` | Exchange leverage |
| `MAX_OPEN_POSITIONS` | `3` | Concurrent trades |
| `SCAN_INTERVAL_SECONDS` | `120` | Scan frequency |
| `ADX_MIN` | `22` | Trend strength cutoff |
| `RVOL_THRESHOLD` | `1.5` | Volume filter |

---

## 📊 Telegram Signal Format

```
🟢🚀 SNIPER SIGNAL — GWEIUSDT
━━━━━━━━━━━━━━━━━━━━━
📌 Direction:   LONG
💲 Entry:       0.14800
🎯 Take Profit: 0.15240
🛑 Stop Loss:   0.14368
📊 R:R Ratio:   1.67
━━━━━━━━━━━━━━━━━━━━━
📈 RVOL:  2.34x
⚡ ADX:   28.4
📡 RSI:   58.2
📐 Slope: 67.3
```

---

## ⚠️ Risk Disclaimer

This bot trades real money on live markets. Past backtest results do not guarantee future performance. Crypto futures carry extreme risk. Never risk more than you can afford to lose. This is NOT financial advice.

---

## 📁 File Structure

```
sniper-bot/
├── main.py                  # Entry point
├── config/
│   └── settings.py          # All configuration
├── src/
│   ├── bot.py               # Orchestrator
│   ├── strategy.py          # Signal engine
│   ├── exchange.py          # BingX (CCXT)
│   ├── telegram.py          # Notifications
│   └── analytics.py         # Trade journal
├── logs/                    # Auto-created
│   ├── bot.log
│   └── trades.json
├── Dockerfile
├── railway.toml
├── requirements.txt
├── .env.example
└── .gitignore
```
