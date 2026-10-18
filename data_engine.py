# data_engine.py — Paper-trading data engine (SEPARATE from bot.py)
#
# Day-trades every Kraken USD pair with $5,000 fake equity per asset using
# REAL live market data. Runs continuously in the background, learns which
# assets/regimes/times/floors win and which lose, and writes a report the
# main bot (and operator) can use to choose what to trade for real.
#
# No API keys needed — uses Kraken public REST only.
# Files: data_engine_trades.jsonl (ledger), data_engine_state.json (equity),
#        data_engine_report.json + DATA_ENGINE.md (analytics/suggestions)

import json
import math
import os
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER_FILE = os.path.join(BASE, "data_engine_trades.jsonl")
STATE_FILE = os.path.join(BASE, "data_engine_state.json")
REPORT_FILE = os.path.join(BASE, "data_engine_report.json")
REPORT_MD = os.path.join(BASE, "DATA_ENGINE.md")

EQUITY_PER_ASSET = 5000.0
TAKER_FEE = 0.0026
MAKER_FEE = 0.0016
SLIPPAGE = 0.0005
CYCLE_SEC = 30
REPORT_EVERY = 120          # cycles between report rebuilds (~1 hr)
MAX_HISTORY = 720           # 6h of 30s ticks per asset for signals

# Strategy knobs (mirrors the tuned bot config)
DIP_DROP_PCT = 0.03
DIP_TP_PCT = 0.0175
DIP_SL_PCT = 0.10
DIP_ALLOC_PCT = 0.20
SCALP_TP_PCT = 0.0075
SCALP_SL_PCT = 0.01
SCALP_ALLOC_PCT = 0.10
FLOOR_DD_PCT = 0.08         # 8% below rolling 6h high = candidate floor
FLOOR_TP_PCT = 0.04
FLOOR_SL_PCT = 0.06
FLOOR_ALLOC_PCT = 0.30
FLOOR_COOLDOWN = 120        # ticks
GRID_LEVELS = 6
GRID_SPACING_PCT = 0.0175
GRID_ALLOC_PCT = 0.60
SWING_TP_PCT = 0.12
SWING_SL_PCT = 0.06
SWING_ALLOC_PCT = 0.25
SWING_DD_PCT = 0.05         # pullback depth for swing entry in uptrend
EXPOSURE_CAP = 0.60         # max fraction of asset equity deployed
RISK_OFF_SELL = 0.50        # sell half on confirmed downtrend
TREND_UP_PCT = 0.03         # +3% vs 2h MA = uptrend
TREND_DN_PCT = 0.05         # -5% vs 2h MA = downtrend


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "data-engine/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def list_usd_pairs():
    """All tradeable Kraken pairs quoted in USD."""
    d = fetch_json("https://api.kraken.com/0/public/AssetPairs")
    pairs = {}
    for name, p in d.get("result", {}).items():
        ws = p.get("wsname", "")
        if not ws.endswith("/USD") or p.get("status") != "online":
            continue
        base = p.get("base", "")
        if base.startswith(("X", "Z")) and len(base) > 3:
            base = base[1:]
        if base in ("USD", "USDT", "USDC", "DAI", "EUR", "GBP"):
            continue
        pairs[name] = {"wsname": ws, "asset": base}
    return pairs


def fetch_tickers(pair_names):
    """One call returns all tickers."""
    q = ",".join(pair_names)
    d = fetch_json(f"https://api.kraken.com/0/public/Ticker?pair={q}")
    return {k: float(v["c"][0]) for k, v in d.get("result", {}).items()}


class AssetBook:
    """Paper-trading account + signal state for one asset."""

    def __init__(self, name, asset):
        self.name = name
        self.asset = asset
        self.usd = EQUITY_PER_ASSET
        self.units = 0.0
        self.history = []          # recent prices
        self.position = None       # {strategy, entry, tp, sl, vol}
        self.grid_orders = []      # [{side, price, vol, filled}]
        self.swing = None          # {entry, tp, sl, vol}
        self.floor_memory = {}     # price_level -> {"hits": n, "fails": n}
        self.strat_stats = defaultdict(lambda: [0, 0])   # strategy -> [n, wins]
        self.ticks = 0
        self.last_grid_refresh = -96
        self.last_floor = -FLOOR_COOLDOWN
        self.last_swing = -120
        self.regime = "sideways"
        self.prev_regime = "sideways"
        self.fees = 0.0
        self.realized = 0.0
        self.trades = 0
        self.wins = 0

    def strat_mult(self, strategy):
        """Learning: scale sizing by this strategy's historical win rate."""
        n, w = self.strat_stats[strategy]
        if n < 10:
            return 1.0
        wr = w / n
        return max(0.25, min(1.5, 0.5 + wr))   # 50% WR -> 1.0

    def floor_boost(self, price):
        """Remembered-floor boost: prior floors that held strengthen score."""
        best = 0.0
        for lvl, m in self.floor_memory.items():
            prox = max(0.0, 1.0 - abs(price - lvl) / (lvl * 0.02))
            strength = (m["hits"] + 1) / (m["hits"] + m["fails"] + 2)
            best = max(best, prox * strength)
        return best

    def equity(self, price):
        return self.usd + self.units * price

    def regime_of(self, price):
        if len(self.history) < 240:      # need ~2h
            return "sideways"
        ma = sum(self.history[-240:]) / 240
        chg = (price - ma) / ma if ma else 0
        if chg >= TREND_UP_PCT:
            return "uptrend"
        if chg <= -TREND_DN_PCT:
            return "downtrend"
        return "sideways"

    def floor_dd(self, price):
        """Drawdown below rolling 6h high — candle-level floor proxy."""
        if len(self.history) < 60:
            return 0.0
        hi = max(self.history)
        return (hi - price) / hi if hi else 0.0


class DataEngine:
    def __init__(self):
        self.pairs = list_usd_pairs()
        print(f"[Engine] Tracking {len(self.pairs)} USD pairs")
        self.books = {n: AssetBook(n, p["asset"]) for n, p in self.pairs.items()}
        self.cycle = 0
        self._load_state()

    # ---------------- persistence ----------------

    def _load_state(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            s = json.load(open(STATE_FILE))
            for n, b in s.get("books", {}).items():
                if n in self.books:
                    bk = self.books[n]
                    bk.usd, bk.units = b["usd"], b["units"]
                    bk.realized, bk.fees = b["realized"], b["fees"]
                    bk.trades, bk.wins = b["trades"], b["wins"]
                    bk.history = b.get("history", [])[-MAX_HISTORY:]
                    bk.grid_orders = b.get("grid_orders", [])
                    bk.swing = b.get("swing")
                    bk.floor_memory = b.get("floor_memory", {})
                    for k, v in b.get("strat_stats", {}).items():
                        bk.strat_stats[k] = v
                    if b.get("position"):
                        bk.position = b["position"]
            self.cycle = s.get("cycle", 0)
            print(f"[Engine] Restored state at cycle {self.cycle}")
        except Exception as e:
            print(f"[Engine] State restore failed: {e}")

    def _save_state(self):
        s = {"cycle": self.cycle, "books": {}}
        for n, bk in self.books.items():
            s["books"][n] = {
                "usd": bk.usd, "units": bk.units, "realized": bk.realized,
                "fees": bk.fees, "trades": bk.trades, "wins": bk.wins,
                "history": bk.history[-MAX_HISTORY:], "position": bk.position,
                "grid_orders": bk.grid_orders, "swing": bk.swing,
                "floor_memory": bk.floor_memory,
                "strat_stats": dict(bk.strat_stats)}
        tmp = STATE_FILE + ".tmp"
        json.dump(s, open(tmp, "w"))
        os.replace(tmp, STATE_FILE)

    def _log_trade(self, bk, strategy, side, price, vol, pnl, extra=None):
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "pair": bk.name, "asset": bk.asset, "strategy": strategy,
            "side": side, "price": round(price, 8), "vol": vol,
            "pnl": round(pnl, 4), "regime": bk.regime,
            "floor_dd": round(bk.floor_dd(price), 4),
            "hour": datetime.now(timezone.utc).hour,
            "dow": datetime.now(timezone.utc).weekday(),
            "day": datetime.now(timezone.utc).day,
        }
        if extra:
            rec.update(extra)
        with open(LEDGER_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    # ---------------- paper execution ----------------

    def _buy(self, bk, strategy, price, alloc_pct):
        eq = bk.equity(price)
        exposure = bk.units * price / eq if eq > 0 else 1.0
        if exposure >= EXPOSURE_CAP or bk.position is not None:
            return
        spend = min(bk.usd, eq * alloc_pct * bk.strat_mult(strategy))
        is_limit = (strategy == "scalp")
        fee = spend * (MAKER_FEE if is_limit else TAKER_FEE)
        entry_p = price if is_limit else price * (1 + SLIPPAGE)
        vol = (spend - fee) / entry_p
        if vol <= 0:
            return
        bk.usd -= spend
        bk.units += vol
        bk.fees += fee
        bk.position = {"strategy": strategy, "entry": entry_p,
                       "entry_fee": fee, "vol": vol}
        if strategy == "dip":
            bk.position["tp"] = price * (1 + DIP_TP_PCT)
            bk.position["sl"] = price * (1 - DIP_SL_PCT)
        elif strategy == "scalp":
            bk.position["tp"] = price * (1 + SCALP_TP_PCT)
            bk.position["sl"] = price * (1 - SCALP_SL_PCT)
        else:  # floor
            bk.position["tp"] = price * (1 + FLOOR_TP_PCT)
            bk.position["sl"] = price * (1 - FLOOR_SL_PCT)
        self._log_trade(bk, strategy, "buy", price, vol, 0.0)

    def _sell(self, bk, price, reason):
        pos = bk.position
        if pos is None:
            return
        pos["vol"] = min(pos["vol"], bk.units)
        if pos["vol"] <= 0:
            bk.position = None
            return
        is_tp = (reason == "tp")
        p = price if is_tp else price * (1 - SLIPPAGE)
        fee = pos["vol"] * p * (MAKER_FEE if is_tp else TAKER_FEE)
        bk.usd += pos["vol"] * p - fee
        bk.units -= pos["vol"]
        bk.fees += fee
        pnl = (p - pos["entry"]) * pos["vol"] - fee - pos.get("entry_fee", 0.0)
        bk.realized += pnl
        bk.trades += 1
        if pnl > 0:
            bk.wins += 1
        bk.strat_stats[pos["strategy"]][0] += 1
        bk.strat_stats[pos["strategy"]][1] += pnl > 0
        # Floor memory: record whether this floor level held
        if pos["strategy"] == "floor":
            lvl = round(pos["entry"], -int(math.floor(
                math.log10(pos["entry"]))) + 3) if pos["entry"] > 0 else pos["entry"]
            m = bk.floor_memory.setdefault(lvl, {"hits": 0, "fails": 0})
            m["hits" if pnl > 0 else "fails"] += 1
        self._log_trade(bk, pos["strategy"], "sell", p, pos["vol"], pnl,
                        {"exit_reason": reason, "entry": pos["entry"]})
        bk.position = None

    # ---------------- grid + swing (parity with real bot) ----------------

    def _grid_tick(self, bk, price):
        # Rebuild ladder when empty or stale in sideways/uptrend
        if (not bk.grid_orders or bk.ticks - bk.last_grid_refresh >= 96) \
                and bk.regime != "downtrend":
            bk.grid_orders = []
            bk.last_grid_refresh = bk.ticks
            eq = bk.equity(price)
            if bk.units * price / eq < EXPOSURE_CAP and bk.usd > 0:
                per = bk.usd * GRID_ALLOC_PCT / GRID_LEVELS * \
                    bk.strat_mult("grid")
                for k in range(1, GRID_LEVELS + 1):
                    buy_p = price * (1 - GRID_SPACING_PCT * k)
                    bk.grid_orders.append(
                        {"side": "buy", "price": buy_p, "usd": per})
                # sell ladder on held units
                if bk.units > 0:
                    per_v = bk.units * 0.8 / GRID_LEVELS
                    for k in range(1, GRID_LEVELS + 1):
                        bk.grid_orders.append(
                            {"side": "sell", "price": price *
                             (1 + GRID_SPACING_PCT * k), "vol": per_v})
            return
        if bk.regime == "downtrend":
            bk.grid_orders = []   # pull grid in downtrends
            return
        for o in list(bk.grid_orders):
            if o["side"] == "buy" and price <= o["price"]:
                fee = o["usd"] * MAKER_FEE
                if bk.usd >= o["usd"]:
                    vol = (o["usd"] - fee) / o["price"]
                    bk.usd -= o["usd"]
                    bk.units += vol
                    bk.fees += fee
                    bk.grid_orders.remove(o)
                    # queue the paired sell above
                    bk.grid_orders.append({"side": "sell",
                                           "price": o["price"] *
                                           (1 + GRID_SPACING_PCT),
                                           "vol": vol})
                    self._log_trade(bk, "grid", "buy", o["price"], vol, 0.0)
            elif o["side"] == "sell" and price >= o["price"] \
                    and bk.units >= o["vol"]:
                fee = o["vol"] * o["price"] * MAKER_FEE
                bk.usd += o["vol"] * o["price"] - fee
                bk.units -= o["vol"]
                bk.fees += fee
                bk.grid_orders.remove(o)
                bk.grid_orders.append({"side": "buy",
                                       "price": o["price"] *
                                       (1 - GRID_SPACING_PCT),
                                       "usd": o["vol"] * o["price"]})
                bk.realized += o["vol"] * o["price"] * GRID_SPACING_PCT - fee
                bk.trades += 1
                bk.wins += 1
                bk.strat_stats["grid"][0] += 1
                bk.strat_stats["grid"][1] += 1
                self._log_trade(bk, "grid", "sell", o["price"], o["vol"],
                                o["vol"] * o["price"] * GRID_SPACING_PCT - fee,
                                {"exit_reason": "grid_tp"})

    def _swing_tick(self, bk, price):
        if bk.swing is not None:
            s = bk.swing
            exit_p = None
            if price >= s["tp"]:
                exit_p = s["tp"]
            elif price <= s["sl"] or bk.regime == "downtrend":
                exit_p = price
            if exit_p is not None:
                s["vol"] = min(s["vol"], bk.units)
                if s["vol"] <= 0:
                    bk.swing = None
                    return
                is_tp = (exit_p == s["tp"])
                p = exit_p if is_tp else exit_p * (1 - SLIPPAGE)
                fee = s["vol"] * p * (MAKER_FEE if is_tp else TAKER_FEE)
                bk.usd += s["vol"] * p - fee
                bk.units -= s["vol"]
                bk.fees += fee
                pnl = (p - s["entry"]) * s["vol"] - fee
                bk.realized += pnl
                bk.trades += 1
                bk.wins += pnl > 0
                bk.strat_stats["swing"][0] += 1
                bk.strat_stats["swing"][1] += pnl > 0
                self._log_trade(bk, "swing", "sell", p, s["vol"], pnl,
                                {"exit_reason": "tp" if exit_p == s["tp"]
                                 else "sl_regime", "entry": s["entry"]})
                bk.swing = None
            return
        # Entry: uptrend + pullback >= 5% + bounce confirmation + cooldown
        if (bk.regime == "uptrend" and bk.floor_dd(price) >= SWING_DD_PCT
                and bk.ticks - bk.last_swing >= 120
                and len(bk.history) >= 4 and price > bk.history[-2]):
            eq = bk.equity(price)
            if bk.units * price / eq < EXPOSURE_CAP:
                spend = min(bk.usd, eq * SWING_ALLOC_PCT *
                            bk.strat_mult("swing"))
                fee = spend * TAKER_FEE
                vol = (spend - fee) / (price * (1 + SLIPPAGE))
                if vol > 0:
                    bk.usd -= spend
                    bk.units += vol
                    bk.fees += fee
                    bk.last_swing = bk.ticks
                    bk.swing = {"entry": price * (1 + SLIPPAGE),
                                "tp": price * (1 + SWING_TP_PCT),
                                "sl": price * (1 - SWING_SL_PCT),
                                "vol": vol}
                    self._log_trade(bk, "swing", "buy", price, vol, 0.0)

    # ---------------- per-asset cycle ----------------

    def _tick(self, bk, price):
        if not price or price <= 0:
            return
        bk.history.append(price)
        if len(bk.history) > MAX_HISTORY:
            bk.history.pop(0)
        bk.ticks += 1
        i = bk.ticks
        bk.prev_regime = bk.regime
        bk.regime = bk.regime_of(price)

        # Risk-off: confirmed downtrend flip -> cut half inventory at market
        if bk.regime == "downtrend" and bk.prev_regime != "downtrend" \
                and bk.units > 0:
            vol = bk.units * RISK_OFF_SELL
            p = price * (1 - SLIPPAGE)
            fee = vol * p * TAKER_FEE
            bk.usd += vol * p - fee
            bk.units -= vol
            bk.fees += fee
            if bk.position is not None and vol >= bk.position["vol"]:
                bk.position = None
            self._log_trade(bk, "risk_off", "sell", p, vol, 0.0,
                            {"note": "regime flip liquidation"})
            bk.grid_orders = []

        # Grid + swing run alongside the single-position dip/scalp/floor slot
        self._grid_tick(bk, price)
        self._swing_tick(bk, price)

        # Exit management
        if bk.position is not None:
            pos = bk.position
            if price >= pos["tp"]:
                self._sell(bk, pos["tp"], "tp")
            elif price <= pos["sl"]:
                self._sell(bk, price, "sl")
            elif pos["strategy"] != "floor" and bk.regime == "downtrend":
                self._sell(bk, price, "regime_exit")
            return

        # Entries
        if len(bk.history) < 30:
            return
        prev = bk.history[-2]
        recent_hi = max(bk.history[-16:])
        drop = (recent_hi - price) / recent_hi if recent_hi else 0
        dd = bk.floor_dd(price)

        if bk.regime == "uptrend":
            if drop >= DIP_DROP_PCT:
                self._buy(bk, "dip", price, DIP_ALLOC_PCT)
            elif abs(price - prev) / prev >= 0.005 and price < prev:
                self._buy(bk, "scalp", price, SCALP_ALLOC_PCT)
        elif bk.regime == "sideways":
            if drop >= DIP_DROP_PCT * 0.7:
                self._buy(bk, "dip", price, DIP_ALLOC_PCT)
            elif abs(price - prev) / prev >= 0.005 and price < prev:
                self._buy(bk, "scalp", price, SCALP_ALLOC_PCT)
        else:  # downtrend — floor hunting only
            if i - bk.last_floor >= FLOOR_COOLDOWN:
                is_local_low = price <= min(bk.history[-20:])
                # floor score = drawdown depth (55%) + local-low capitulation (25%)
                # + remembered-floor boost (20%)
                floor_score = (0.55 * min(1.0, dd / (FLOOR_DD_PCT * 1.5))
                               + (0.25 if is_local_low else 0.0)
                               + 0.20 * bk.floor_boost(price))
                if dd >= FLOOR_DD_PCT and floor_score >= 0.65 and is_local_low:
                    self._buy(bk, "floor", price, FLOOR_ALLOC_PCT)
                    bk.last_floor = i

    # ---------------- analytics ----------------

    def _report(self, tickers):
        trades = []
        if os.path.exists(LEDGER_FILE):
            with open(LEDGER_FILE, encoding="utf-8") as f:
                for line in f:
                    try:
                        trades.append(json.loads(line))
                    except Exception:
                        pass
        sells = [t for t in trades if t["side"] == "sell"
                 and t.get("exit_reason")]

        by_asset, by_hour, by_dow, by_day, by_strat, by_regime = \
            (defaultdict(lambda: [0.0, 0, 0]) for _ in range(6))
        floor_hits = defaultdict(lambda: [0, 0])
        for t in sells:
            for d, k in ((by_asset, t["asset"]), (by_strat, t["strategy"]),
                         (by_regime, t["regime"]), (by_hour, t["hour"]),
                         (by_dow, t["dow"]), (by_day, t["day"])):
                d[k][0] += t["pnl"]
                d[k][1] += 1
                d[k][2] += t["pnl"] > 0
            if t["strategy"] == "floor":
                floor_hits[t["asset"]][0] += 1
                floor_hits[t["asset"]][1] += t["pnl"] > 0

        rankings = sorted(by_asset.items(), key=lambda kv: -kv[1][0])
        report = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "cycle": self.cycle,
            "assets_tracked": len(self.books),
            "ledger_trades": len(trades),
            "closed_positions": len(sells),
            "asset_rankings": [
                {"asset": a, "pnl": round(v[0], 2), "trades": v[1],
                 "win_rate": round(v[2] / v[1], 3) if v[1] else 0,
                 "equity": round(self.books[[k for k, b in self.books.items()
                                           if b.asset == a][0]].equity(
                     tickers.get([k for k, b in self.books.items()
                                  if b.asset == a][0], 0)), 2)}
                for a, v in rankings],
            "pnl_by_hour_utc": {str(k): round(v[0], 2)
                                for k, v in sorted(by_hour.items())},
            "pnl_by_weekday": {str(k): round(v[0], 2)
                               for k, v in sorted(by_dow.items())},
            "pnl_by_day_of_month": {str(k): round(v[0], 2)
                                    for k, v in sorted(by_day.items())},
            "pnl_by_strategy": {k: {"pnl": round(v[0], 2), "trades": v[1],
                                    "wr": round(v[2] / v[1], 3) if v[1] else 0}
                                for k, v in by_strat.items()},
            "pnl_by_regime": {k: round(v[0], 2)
                              for k, v in by_regime.items()},
            "floor_accuracy": {a: {"floors": v[0],
                                   "held": v[1],
                                   "rate": round(v[1] / v[0], 3) if v[0] else 0}
                               for a, v in floor_hits.items()},
        }
        tmp = REPORT_FILE + ".tmp"
        json.dump(report, open(tmp, "w"), indent=2)
        os.replace(tmp, REPORT_FILE)
        self._write_md(report)

    def _write_md(self, r):
        lines = ["# Data Engine Report",
                 f"Generated: {r['generated']} | Cycle {r['cycle']} | "
                 f"{r['assets_tracked']} assets | {r['closed_positions']} "
                 f"closed trades", "",
                 "## Asset Rankings (net PnL per $5000 paper account)", ""]
        for a in r["asset_rankings"][:20]:
            lines.append(f"- **{a['asset']}**: ${a['pnl']:+.2f} "
                         f"({a['trades']} trades, {a['win_rate']*100:.0f}% WR)")
        lines += ["", "## PnL by Strategy", ""]
        for s, v in r["pnl_by_strategy"].items():
            lines.append(f"- {s}: ${v['pnl']:+.2f} "
                         f"({v['trades']} trades, {v['wr']*100:.0f}% WR)")
        lines += ["", "## PnL by Regime", ""]
        for g, v in r["pnl_by_regime"].items():
            lines.append(f"- {g}: ${v:+.2f}")
        lines += ["", "## PnL by Hour (UTC)", ""]
        for h, v in sorted(r["pnl_by_hour_utc"].items(), key=lambda x: int(x[0])):
            lines.append(f"- {h}:00: ${v:+.2f}")
        lines += ["", "## PnL by Day of Month", ""]
        for d, v in sorted(r["pnl_by_day_of_month"].items(),
                         key=lambda x: int(x[0])):
            lines.append(f"- {d}: ${v:+.2f}")
        lines += ["", "## Floor Accuracy per Asset", ""]
        for a, v in sorted(r["floor_accuracy"].items(),
                           key=lambda x: -x[1]["rate"]):
            lines.append(f"- {a['asset']}: {v['rate']*100:.0f}% floors held "
                         f"({v['held']}/{v['floors']})")
        open(REPORT_MD, "w", encoding="utf-8").write("\n".join(lines))

    # ---------------- main loop ----------------

    def run(self):
        names = list(self.pairs)
        while True:
            self.cycle += 1
            try:
                tickers = fetch_tickers(names)
            except Exception as e:
                print(f"[Engine] Ticker fetch failed: {e}; retrying")
                time.sleep(CYCLE_SEC)
                continue
            for n, price in tickers.items():
                if n in self.books:
                    self._tick(self.books[n], price)
            if self.cycle % 20 == 0:
                self._save_state()
            if self.cycle % REPORT_EVERY == 0:
                self._report(tickers)
                eq = sum(b.equity(tickers.get(n, 0))
                         for n, b in self.books.items())
                print(f"[Engine] Cycle {self.cycle} | paper equity "
                      f"${eq:,.0f} | report written")
            time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    DataEngine().run()
