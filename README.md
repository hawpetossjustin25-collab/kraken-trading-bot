# Kraken Adaptive Multi-Strategy Trading Bot & 434-Pair Data Engine

100% free forever. Local-first, no subscriptions or paid APIs. Uses real Kraken public/private REST & WebSocket feeds with honest maker/taker fee (`0.16%` / `0.26%`) and slippage accounting.

---

## 📱 Phone App (Live Mobile Terminal — No Install Needed)

👉 **[Launch Phone App: https://hawpetossjustin25-collab.github.io/kraken-trading-bot/](https://hawpetossjustin25-collab.github.io/kraken-trading-bot/)**

Open that link on your phone to view live Kraken L2 order books, Order Book Imbalance (OBI), live Floor Scores, the 2-Month Data Engine Winners across 434 Kraken USD pairs (95,041 closed trades), UTC time-risk windows, and an interactive `$5,000` mobile paper trader.

**To install it on your phone's home screen like a native app:**
- **Android (Chrome):** Open the link → tap Menu (**⋮**) → **Add to Home screen**
- **iPhone (Safari):** Open the link → tap **Share** → **Add to Home Screen**

---

## 💻 Installation (All Methods)

### Option 1 — `pip install` directly from GitHub
```bash
pip install git+https://github.com/hawpetossjustin25-collab/kraken-trading-bot.git
```
This installs five CLI commands globally:
- `kraken-bot` — Run the adaptive trading bot (`bot.py`)
- `kraken-dashboard` — Launch the live Rich terminal dashboard (`dashboard.py`)
- `kraken-backtester` — Run the walk-forward historical backtester (`backtester.py`)
- `kraken-data-engine` — Run the 24/7 background paper-trading data engine across all Kraken USD pairs (`data_engine.py`)
- `kraken-trainer` — Run the fast multi-month historical replay trainer (`trainer.py`)

### Option 2 — Clone & Install with `requirements.txt`
```bash
git clone https://github.com/hawpetossjustin25-collab/kraken-trading-bot.git
cd kraken-trading-bot
pip install -r requirements.txt
python bot.py
```

### Option 3 — One-Click Windows Download (`.zip` + `.bat` Launchers)
1. **[Click here to download the ZIP](https://github.com/hawpetossjustin25-collab/kraken-trading-bot/archive/refs/heads/main.zip)** and extract it.
2. Double-click any launcher:
   - `start_bot.bat` — Starts the trading bot with auto-restart supervisor
   - `start_dashboard.bat` — Opens the live read-only terminal dashboard
   - `start_data_engine.bat` — Starts the `$5,000/asset` background paper-trading data engine

---

## 🔗 Quick Links

- **Live Phone / Web App:** [https://hawpetossjustin25-collab.github.io/kraken-trading-bot/](https://hawpetossjustin25-collab.github.io/kraken-trading-bot/)
- **GitHub Repository:** [https://github.com/hawpetossjustin25-collab/kraken-trading-bot](https://github.com/hawpetossjustin25-collab/kraken-trading-bot)
- **Direct ZIP Download:** [https://github.com/hawpetossjustin25-collab/kraken-trading-bot/archive/refs/heads/main.zip](https://github.com/hawpetossjustin25-collab/kraken-trading-bot/archive/refs/heads/main.zip)
- **2-Month Data Engine Report:** [DATA_ENGINE.md](https://github.com/hawpetossjustin25-collab/kraken-trading-bot/blob/main/DATA_ENGINE.md)

---

## 🧠 Architecture & Capabilities

- **5 Adaptive Strategies (`bot.py`):**
  - **Grid:** 6-level maker ladder around mid-price, pulled automatically in downtrends
  - **Dip Buy:** Buys sharp pullbacks with maker take-profit and `-10%` stop-loss
  - **Scalp:** Volatility-triggered limit entries with tight maker take-profit
  - **Floor Buy:** Live L2 order-book bottom detector (capitulation drawdown + bullish CVD divergence + bid-wall support + tape absorption) with persistent `floor_memory.json`
  - **Swing:** Uptrend pullback entries (`+12%` TP / `-6%` SL / regime-flip exit) persisted across restarts
- **Risk Controls:**
  - `60%` maximum asset exposure gate
  - Automatic `50%` inventory risk-off liquidation on confirmed downtrend flip
  - Per-pair Kraken `AssetPairs` decimal precision (`pair_decimals`, `lot_decimals`) and minimum order size (`ordermin`) enforcement
- **Self-Improvement & RL Loop (`improvement_engine.py`):**
  - Q-learning parameter & strategy weight tuning
  - Grades order-flow vetoes and writes human-readable `SELF_IMPROVEMENT.md`
- **434-Pair Background Data Engine (`data_engine.py` & `trainer.py`):**
  - Gives every Kraken USD pair a `$5,000` paper account
  - Ranks consistent winners across months, strategies, UTC hours, days of the month, and floor hold rates
  - Feeds `data_engine_report.json` automatically into `bot.py` each cycle

---

## ✅ Verification Suites

Run the built-in audits anytime:
```bash
python test_system.py
python test_kraken_navigation.py
```
