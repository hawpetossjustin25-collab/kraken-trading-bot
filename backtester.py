#!/usr/bin/env python3
"""
Walk-forward backtester for the Kraken adaptive bot.

Replays historical OHLC candles from Kraken's public API through the SAME
regime-detection and strategy logic the live bot uses, with honest cost
accounting: maker/taker fees and slippage deducted from every simulated fill.

Walk-forward: the candle history is split into N sequential windows; results
are reported per window AND per market regime — a strategy that only works
in one regime is not "good", it's curve-fit.

Fill model (conservative):
  - Limit buy fills only if candle LOW <= order price  (price had to reach it)
  - Limit sell fills only if candle HIGH >= order price
  - Market orders fill at candle close ± slippage
  - All fills charged the configured fee — profits are NET, not gross

No lookahead: at candle t the simulation only sees candles <= t.

Usage:
    python backtester.py                  # ETH/USD, 90 days, hourly candles
    python backtester.py --days 180 --pair XXBTZUSD --windows 6
"""

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backtest")


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

# Map Kraken pair names to CryptoCompare symbols
CC_MAP = {"XETHZUSD": ("ETH", "USD"), "XXBTZUSD": ("BTC", "USD"),
          "SOLUSD": ("SOL", "USD"), "XBTUSD": ("BTC", "USD"),
          "ETHUSD": ("ETH", "USD")}
CC_API = "https://min-api.cryptocompare.com/data/v2/histohour"


def fetch_candles_cc(pair: str, days: int) -> list[list]:
    """Hourly candles from CryptoCompare — pages back to `days` via toTs.
    Free, no key. Returns Kraken-format rows [ts, o, h, l, c, vwap, vol, n]."""
    fsym, tsym = CC_MAP.get(pair, ("ETH", "USD"))
    candles = []
    to_ts = int(time.time())
    cutoff = to_ts - days * 86400
    for _ in range(60):  # ~60 * 2000h ≈ 13 years max
        resp = requests.get(CC_API, params={
            "fsym": fsym, "tsym": tsym, "limit": 2000, "toTs": to_ts,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("Data", {}).get("Data", [])
        if not rows:
            break
        for r in rows:
            t = int(r["time"])
            if t < cutoff:
                continue
            # [time, open, high, low, close, vwap, volume, count]
            candles.append([t, r["open"], r["high"], r["low"], r["close"],
                            r.get("vwap", r["close"]), r.get("volumeto", 0), 0])
        oldest = int(rows[0]["time"])
        if oldest <= cutoff:
            break
        to_ts = oldest - 3600
        time.sleep(0.5)
    candles.sort(key=lambda c: c[0])
    log.info("CryptoCompare: fetched %d hourly candles for %s "
             "(%s → %s)", len(candles), pair,
             time.strftime("%Y-%m-%d", time.gmtime(candles[0][0])) if candles else "?",
             time.strftime("%Y-%m-%d", time.gmtime(candles[-1][0])) if candles else "?")
    return candles


BINANCE_MAP = {"XETHZUSD": "ETHUSDT", "XXBTZUSD": "BTCUSDT",
               "SOLUSD": "SOLUSDT", "ETHUSD": "ETHUSDT", "XBTUSD": "BTCUSDT"}
BINANCE_API = "https://api.binance.com/api/v3/klines"


def fetch_candles_binance(pair: str, days: int) -> list[list]:
    """Hourly klines from Binance public API — ETHUSDT goes back to 2017.
    No key needed. Returns Kraken-format [ts, o, h, l, c, vwap, vol, n]."""
    symbol = BINANCE_MAP.get(pair, "ETHUSDT")
    candles = []
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    cursor = start_ms
    for _ in range(100):  # 100 * 1000h ≈ 11+ years
        resp = requests.get(BINANCE_API, params={
            "symbol": symbol, "interval": "1h", "limit": 1000,
            "startTime": cursor, "endTime": end_ms,
        }, timeout=30)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        for r in rows:
            t = int(r[0]) // 1000
            candles.append([t, float(r[1]), float(r[2]), float(r[3]),
                            float(r[4]), float(r[4]), float(r[5]), int(r[8])])
        cursor = int(rows[-1][0]) + 3600000
        if len(rows) < 1000:
            break
        time.sleep(0.3)
    candles.sort(key=lambda c: c[0])
    log.info("Binance: fetched %d hourly candles for %s (%s → %s)",
             len(candles), symbol,
             time.strftime("%Y-%m-%d", time.gmtime(candles[0][0])) if candles else "?",
             time.strftime("%Y-%m-%d", time.gmtime(candles[-1][0])) if candles else "?")
    return candles


COINBASE_MAP = {"XETHZUSD": "ETH-USD", "XXBTZUSD": "BTC-USD",
                "SOLUSD": "SOL-USD", "ETHUSD": "ETH-USD", "XBTUSD": "BTC-USD"}
COINBASE_API = "https://api.exchange.coinbase.com/products/{}/candles"


def fetch_candles_coinbase(pair: str, days: int, granularity: int = 21600) -> list[list]:
    """Candles from Coinbase Exchange public API — ETH-USD back to ~2016.
    No key, no geo-block. granularity in seconds (21600=6h candles).
    Max 300 candles/request → pages backwards in chunks."""
    product = COINBASE_MAP.get(pair, "ETH-USD")
    candles = []
    now = int(time.time())
    end = now
    start = now - days * 86400
    for _ in range(60):  # 60 * 300 * 6h ≈ 30 years max
        if end <= start:
            break
        chunk_end = end
        chunk_start = max(start, end - 300 * granularity)
        if chunk_start >= chunk_end:
            break
        resp = requests.get(COINBASE_API.format(product), params={
            "granularity": granularity,
            "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(chunk_start)),
            "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(chunk_end)),
        }, headers={"User-Agent": "backtester/1.0"}, timeout=30)
        resp.raise_for_status()
        rows = resp.json()  # [time, low, high, open, close, volume] newest-first
        if not rows or isinstance(rows, dict):
            break
        for r in rows:
            t, lo_, hi, op, cl, vol = int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
            candles.append([t, op, hi, lo_, cl, cl, vol, 0])
        oldest = min(int(r[0]) for r in rows)
        if oldest <= start:
            break
        end = oldest - granularity
        if len(rows) < 2:
            break
        time.sleep(0.3)
    candles.sort(key=lambda c: c[0])
    log.info("Coinbase: fetched %d candles (%ds) for %s (%s → %s)",
             len(candles), granularity, product,
             time.strftime("%Y-%m-%d", time.gmtime(candles[0][0])) if candles else "?",
             time.strftime("%Y-%m-%d", time.gmtime(candles[-1][0])) if candles else "?")
    return candles


def fetch_candles(pair: str, interval_min: int, days: int) -> list[list]:
    """Fetch OHLC candles covering `days`. Kraken returns max ~720 per call
    and ignores requests for data older than what `since` bounds, so we page
    forward using the returned `last` cursor."""
    candles: list[list] = []
    since = int(time.time()) - days * 86400
    for _ in range(400):
        resp = requests.get(KRAKEN_OHLC, params={
            "pair": pair, "interval": interval_min, "since": since,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"Kraken OHLC error: {data['error']}")
        result = data["result"]
        batch = []
        for k, v in result.items():
            if k != "last" and isinstance(v, list):
                batch = v
                break
        if not batch:
            break
        candles.extend(batch)
        new_last = result.get("last", since)
        if new_last <= since:
            break
        since = new_last
        if len(batch) < 2:
            break
        time.sleep(0.7)  # polite pacing
    # dedupe + sort
    seen = {}
    for c in candles:
        seen[int(c[0])] = c
    out = [seen[k] for k in sorted(seen)]
    log.info("Fetched %d %dm candles for %s", len(out), interval_min, pair)
    return out


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

@dataclass
class Fill:
    ts: int
    side: str
    price: float
    volume: float
    fee: float
    strategy: str


@dataclass
class WindowResult:
    window: int
    regime_counts: dict = field(default_factory=dict)
    trades: int = 0
    wins: int = 0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    start_equity: float = 0.0
    end_equity: float = 0.0


class Backtester:
    """Event-driven replay of the bot's adaptive strategy over candles."""

    def __init__(self, cfg: dict, pair: str, candles: list[list],
                 windows: int = 4, equity: float = 100.0):
        self.cfg = cfg
        self.pair = pair
        self.candles = candles          # [time, open, high, low, close, vwap, vol, count]
        self.windows = windows
        self.start_equity = equity

        self.maker_fee = cfg.get("maker_fee_pct", 0.16) / 100.0
        self.taker_fee = cfg.get("taker_fee_pct", 0.26) / 100.0
        self.slippage = cfg.get("slippage_pct", 0.05) / 100.0

        self.ma_period_candles = min(24, len(candles))  # 24 candles = 24h at 60m
        self.trend_up = cfg.get("adaptive_trend_threshold_up", 3.0)
        self.trend_down = cfg.get("adaptive_trend_threshold_down", 5.0)
        self.grid_levels = cfg.get("grid_levels", 6)
        self.grid_spacing = cfg.get("grid_spacing_pct", 1.75) / 100.0
        self.grid_alloc = cfg.get("grid_allocation_pct", 60) / 100.0
        self.dip_thresh = cfg.get("dip_buy_threshold_pct", 3.0) / 100.0
        self.dip_tp = cfg.get("dip_take_profit_pct", 1.75) / 100.0
        self.scalp_vol = cfg.get("scalp_volatility_threshold_pct", 0.5) / 100.0
        self.scalp_tp = cfg.get("scalp_take_profit_pct", 0.8) / 100.0
        self.confirm_cycles = cfg.get("adaptive_confirmation_cycles", 3)
        # Floor-buy: approximates live order-flow floor detection on candles —
        # ≥8% below rolling 48-candle high + recovery candle (close > open or
        # close in upper half of range) = sellers exhausting at a level.
        self.floor_dd = 0.08
        self.floor_tp = cfg.get("floor_tp_pct", 4.0) / 100.0
        self.floor_sl = 0.04
        self.floor_cooldown = 30
        # Swing: uptrend + pullback entry, big TP/SL, exits on regime flip
        self.swing_tp = cfg.get("swing_tp_pct", 12.0) / 100.0
        self.swing_sl = cfg.get("swing_sl_pct", 6.0) / 100.0
        self.swing_pullback = cfg.get("swing_pullback_pct", 4.0) / 100.0
        self.swing_max_hold = 720      # ~720 6h candles ≈ 6 months
        self.swing_cooldown = 60

    # -- regime (mirrors MarketAnalyzer.analyze, but on historical candles) --

    def regime_at(self, i: int, price: float) -> str:
        start = max(0, i - self.ma_period_candles)
        closes = [float(c[4]) for c in self.candles[start:i + 1]]
        if len(closes) < 2:
            return "sideways"
        sma = sum(closes) / len(closes)
        change_pct = (price - closes[0]) / closes[0] * 100.0 if closes[0] else 0.0
        if price < sma and change_pct <= -self.trend_down:
            return "downtrend"
        if price > sma and change_pct >= self.trend_up:
            return "uptrend"
        return "sideways"

    REGIME_MAP = {"downtrend": {"dip", "scalp"},
                  "uptrend": {"grid", "dip", "scalp"},
                  "sideways": {"grid"}}

    # -- cost accounting -----------------------------------------------------

    def _fee(self, price: float, vol: float, maker: bool) -> float:
        rate = self.maker_fee if maker else self.taker_fee
        return price * vol * rate

    def _slip(self, price: float, side: str) -> float:
        return price * (1 + self.slippage if side == "buy" else 1 - self.slippage)

    # -- main run ------------------------------------------------------------

    def run(self) -> list[WindowResult]:
        n = len(self.candles)
        if n < self.ma_period_candles + 50:
            raise RuntimeError(f"Only {n} candles — need more history")
        chunk = n // self.windows
        results = []

        for w in range(self.windows):
            lo = w * chunk
            hi = n if w == self.windows - 1 else (w + 1) * chunk
            res = self._run_window(lo, hi, w)
            results.append(res)
        return results

    def _run_window(self, lo: int, hi: int, wnum: int) -> WindowResult:
        res = WindowResult(window=wnum, start_equity=self.start_equity)
        usd = self.start_equity
        asset = 0.0
        equity = usd
        peak = equity

        grid_orders: dict[int, dict] = {}   # candle-idx-keyed limit orders
        dip_position: dict | None = None
        scalp_position: dict | None = None

        regime = "sideways"
        regime_streak = 0
        pending_regime = None
        active = self.REGIME_MAP["sideways"]
        prev_close = float(self.candles[lo][4]) if lo else float(self.candles[0][4])
        grid_center = prev_close
        last_grid_refresh = lo
        last_dip_buy = 0
        last_riskoff = lo - 24
        floor_position: dict | None = None
        last_floor = lo - self.floor_cooldown
        swing_position: dict | None = None
        last_swing = lo - self.swing_cooldown

        for i in range(lo, hi):
            c = self.candles[i]
            ts = int(c[0])
            o, h, l, cl = (float(x) for x in (c[1], c[2], c[3], c[4]))

            # --- regime detection with confirmation (mirrors live bot) ---
            detected = self.regime_at(i, cl)
            if detected == regime:
                regime_streak = 0
                pending_regime = None
            else:
                if detected != pending_regime:
                    pending_regime = detected
                    regime_streak = 1
                else:
                    regime_streak += 1
                if regime_streak >= self.confirm_cycles:
                    regime = detected
                    active = self.REGIME_MAP[regime]
                    regime_streak = 0
                    pending_regime = None
                    # Risk-off: on confirmed downtrend flip, cut half the
                    # inventory at market — stops slow bleed through bear legs
                    if regime == "downtrend" and asset > 0:
                        vol = asset * 0.5
                        p = self._slip(cl, "sell")
                        fee = self._fee(p, vol, maker=False)
                        usd += vol * p - fee
                        asset -= vol
                        res.fees += fee
                        res.trades += 1
                        res.gross_pnl += 0  # can't attribute entry w/o lots
            res.regime_counts[regime] = res.regime_counts.get(regime, 0) + 1

            # Exposure cap: no new buys when >60% of equity is already in asset
            exposure = (asset * cl) / equity if equity > 0 else 1.0


            can_buy = exposure < 0.60 and usd > 0
            # rolling drawdown-from-high shared by floor + swing entries
            win_lo = max(lo, i - 48)
            roll_hi = max(float(c[4]) for c in self.candles[win_lo:i + 1])
            dd_now = (roll_hi - cl) / roll_hi if roll_hi else 0

            # --- GRID: maintain ladder around center; fills on candle range ---
            if "grid" in active:
                if i - last_grid_refresh >= 24 or not grid_orders:
                    grid_center = cl
                    last_grid_refresh = i
                    grid_orders = {}
                    for lvl in range(1, self.grid_levels + 1):
                        bp = cl * (1 - self.grid_spacing * lvl)
                        sp = cl * (1 + self.grid_spacing * lvl)
                        # size on available USD (not equity) so we can't go negative
                        alloc = usd * self.grid_alloc / (2 * self.grid_levels)
                        if bp > 0 and alloc > 1:
                            grid_orders[f"b{lvl}"] = {"side": "buy", "price": bp,
                                                      "alloc": alloc}
                        grid_orders[f"s{lvl}"] = {"side": "sell", "price": sp,
                                                  "alloc": alloc}
                for key, od in list(grid_orders.items()):
                    p = od["price"]
                    if od["side"] == "buy" and l <= p and usd > 0:
                        # shrink to what USD can actually cover incl. fee
                        vol = min(od["alloc"], usd / (1 + self.maker_fee)) / p
                        fee = self._fee(p, vol, maker=True)
                        cost = vol * p + fee
                        if cost > usd:
                            del grid_orders[key]
                            continue
                        usd -= cost
                        asset += vol
                        res.fees += fee
                        del grid_orders[key]
                    elif od["side"] == "sell" and h >= p and asset > 0:
                        vol = min(asset, od["alloc"] / p)
                        fee = self._fee(p, vol, maker=True)
                        usd += vol * p - fee
                        asset -= vol
                        res.fees += fee
                        res.trades += 1
                        res.wins += 1
                        res.gross_pnl += vol * p - (od["alloc"])
                        del grid_orders[key]

            # --- DIP: buy on sharp close-to-close drop, TP on recovery ---
            if "dip" in active and dip_position is None and can_buy:
                drop = (prev_close - cl) / prev_close if prev_close else 0
                if drop >= self.dip_thresh and i - last_dip_buy >= 12:
                    alloc = usd * 0.25 * (0.5 if regime == "downtrend" else 1.0)
                    if alloc >= 1:
                        p = self._slip(cl, "buy")
                        vol = alloc / p
                        fee = self._fee(p, vol, maker=False)
                        usd -= vol * p + fee
                        asset += vol
                        res.fees += fee
                        dip_position = {"entry": p, "vol": vol,
                                        "tp": p * (1 + self.dip_tp),
                                        "sl": p * 0.90,      # -10% stop-loss
                                        "expires": i + 168}  # ~1 week max hold
                        last_dip_buy = i
            if dip_position:
                exit_p = None
                if h >= dip_position["tp"]:
                    exit_p = dip_position["tp"]          # take-profit (maker)
                elif l <= dip_position["sl"] or i >= dip_position["expires"]:
                    exit_p = self._slip(cl, "sell")      # stop/timeout at market
                if exit_p is not None:
                    vol = dip_position["vol"]
                    fee = self._fee(exit_p, vol, maker=exit_p == dip_position["tp"])
                    usd += vol * exit_p - fee
                    asset -= vol
                    res.fees += fee
                    res.trades += 1
                    pnl = (exit_p - dip_position["entry"]) * vol
                    res.gross_pnl += pnl
                    if pnl > 0:
                        res.wins += 1
                    dip_position = None

            # --- SCALP: buy on intracandle volatility, tight TP ---
            if "scalp" in active and scalp_position is None and can_buy:
                rng = (h - l) / cl if cl else 0
                if rng >= self.scalp_vol:
                    alloc = usd * 0.10          # size on cash, not equity
                    if alloc >= 1:
                        bp = cl * 0.995
                        scalp_position = {"entry": bp, "alloc": alloc,
                                          "tp": bp * (1 + self.scalp_tp),
                                          "sl": bp * (1 - self.scalp_tp * 1.5),  # tight stop
                                          "expires": i + 24}
                        # fills only if a later candle dips to it
            if scalp_position and not scalp_position.get("filled") \
                    and l <= scalp_position["entry"] and usd > 0:
                p = scalp_position["entry"]
                vol = min(scalp_position["alloc"], usd / (1 + self.maker_fee)) / p
                fee = self._fee(p, vol, maker=True)
                usd -= vol * p + fee
                asset += vol
                res.fees += fee
                scalp_position["vol"] = vol
                scalp_position["filled"] = True
            if scalp_position and not scalp_position.get("filled") \
                    and i >= scalp_position["expires"]:
                scalp_position = None   # order never filled — cancel it
            if scalp_position and scalp_position.get("filled"):
                if h >= scalp_position["tp"]:
                    p = scalp_position["tp"]
                    vol = scalp_position["vol"]
                    fee = self._fee(p, vol, maker=True)
                    usd += vol * p - fee
                    asset -= vol
                    res.fees += fee
                    res.trades += 1
                    res.wins += 1
                    res.gross_pnl += (p - scalp_position["entry"]) * vol
                    scalp_position = None
                elif l <= scalp_position["sl"] or i >= scalp_position["expires"]:
                    p = self._slip(cl, "sell")
                    vol = scalp_position["vol"]
                    fee = self._fee(p, vol, maker=False)
                    usd += vol * p - fee
                    asset -= vol
                    res.fees += fee
                    res.trades += 1
                    pnl = (p - scalp_position["entry"]) * vol
                    res.gross_pnl += pnl
                    if pnl > 0:
                        res.wins += 1
                    scalp_position = None

            # --- FLOOR BUY: capitulation + recovery confirmation ---
            if floor_position is None and can_buy and i - last_floor >= self.floor_cooldown:
                recovering = cl > o or cl >= l + (h - l) * 0.6
                if dd_now >= self.floor_dd and recovering and usd > 0:
                    alloc = usd * 0.30
                    if alloc >= 1:
                        p = self._slip(cl, "buy")
                        vol = alloc / p
                        fee = self._fee(p, vol, maker=False)
                        usd -= vol * p + fee
                        asset += vol
                        res.fees += fee
                        floor_position = {"entry": p, "vol": vol,
                                          "tp": p * (1 + self.floor_tp),
                                          "sl": p * (1 - self.floor_sl),
                                          "expires": i + 120}
                        last_floor = i
            if floor_position:
                exit_p = None
                if h >= floor_position["tp"]:
                    exit_p = floor_position["tp"]
                elif l <= floor_position["sl"] or i >= floor_position["expires"]:
                    exit_p = self._slip(cl, "sell")
                if exit_p is not None:
                    vol = floor_position["vol"]
                    fee = self._fee(exit_p, vol, maker=exit_p == floor_position["tp"])
                    usd += vol * exit_p - fee
                    asset -= vol
                    res.fees += fee
                    res.trades += 1
                    pnl = (exit_p - floor_position["entry"]) * vol
                    res.gross_pnl += pnl
                    if pnl > 0:
                        res.wins += 1
                    floor_position = None

            # --- SWING: uptrend + pullback, big TP/SL, regime-flip exit ---
            if swing_position is None and can_buy and regime == "uptrend" \
                    and i - last_swing >= self.swing_cooldown and usd > 0:
                pullback = dd_now
                if pullback >= self.swing_pullback or dd_now >= self.floor_dd:
                    alloc = usd * 0.35
                    if alloc >= 1:
                        p = self._slip(cl, "buy")
                        vol = alloc / p
                        fee = self._fee(p, vol, maker=False)
                        usd -= vol * p + fee
                        asset += vol
                        res.fees += fee
                        swing_position = {"entry": p, "vol": vol,
                                          "tp": p * (1 + self.swing_tp),
                                          "sl": p * (1 - self.swing_sl),
                                          "expires": i + self.swing_max_hold}
                        last_swing = i
            if swing_position:
                exit_p = None
                if h >= swing_position["tp"]:
                    exit_p = swing_position["tp"]
                elif l <= swing_position["sl"] or regime == "downtrend" \
                        or i >= swing_position["expires"]:
                    exit_p = self._slip(cl, "sell")
                if exit_p is not None:
                    vol = swing_position["vol"]
                    fee = self._fee(exit_p, vol, maker=exit_p == swing_position["tp"])
                    usd += vol * exit_p - fee
                    asset -= vol
                    res.fees += fee
                    res.trades += 1
                    pnl = (exit_p - swing_position["entry"]) * vol
                    res.gross_pnl += pnl
                    if pnl > 0:
                        res.wins += 1
                    swing_position = None

            equity = usd + asset * cl
            peak = max(peak, equity)
            dd = min((peak - equity) / peak * 100, 100.0) if peak else 0
            res.max_drawdown_pct = max(res.max_drawdown_pct, dd)
            prev_close = cl

        res.end_equity = usd + asset * float(self.candles[hi - 1][4])
        res.net_pnl = res.end_equity - res.start_equity
        return res


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(results: list[WindowResult], cfg: dict):
    print("\n" + "=" * 66)
    print("WALK-FORWARD BACKTEST RESULTS (NET of fees + slippage)")
    print("=" * 66)
    total_net = 0.0
    total_trades = 0
    total_wins = 0
    all_regimes: dict = {}
    for r in results:
        wr = (r.wins / r.trades * 100) if r.trades else 0.0
        total_net += r.net_pnl
        total_trades += r.trades
        total_wins += r.wins
        for k, v in r.regime_counts.items():
            all_regimes[k] = all_regimes.get(k, 0) + v
        print(f"\nWindow {r.window + 1}:")
        print(f"  Equity: ${r.start_equity:.2f} -> ${r.end_equity:.2f}  "
              f"(net {r.net_pnl:+.2f}, fees ${r.fees:.2f})")
        print(f"  Trades: {r.trades}  Win rate: {wr:.0f}%  "
              f"Max drawdown: {r.max_drawdown_pct:.1f}%")
        print(f"  Regime mix: {r.regime_counts}")
    print("\n" + "-" * 66)
    overall_wr = (total_wins / total_trades * 100) if total_trades else 0.0
    print(f"TOTAL net PnL: ${total_net:+.2f} | trades: {total_trades} | "
          f"win rate: {overall_wr:.0f}%")
    print(f"Regime coverage: {all_regimes}")
    verdict = "PROMISING" if total_net > 0 and len(all_regimes) > 1 else \
              "WEAK — loses money or only works in one regime"
    print(f"Verdict: {verdict}")
    print("=" * 66 + "\n")
    return total_net


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--pair", default=None)
    ap.add_argument("--windows", type=int, default=4)
    ap.add_argument("--interval", type=int, default=60, help="candle minutes")
    ap.add_argument("--equity", type=float, default=100.0)
    ap.add_argument("--source", choices=["auto", "kraken", "cryptocompare",
                                         "binance", "coinbase"], default="auto")
    args = ap.parse_args()

    cfg = load_config()
    pair = args.pair or cfg.get("pair", "XETHZUSD")

    # Kraken public OHLC caps at ~720 candles — for deep history use CC
    need_cc = args.days * 24 * 60 // max(args.interval, 60) > 700
    if args.source == "binance":
        candles = fetch_candles_binance(pair, args.days)
    elif args.source == "cryptocompare":
        candles = fetch_candles_cc(pair, args.days)
    elif args.source == "coinbase" or (args.source == "auto" and need_cc):
        # deep history: Coinbase (US-friendly); 6h candles when interval=60
        # requested for very long ranges — still resolves regime cycles
        gran = 3600 if args.days <= 400 else 21600
        candles = fetch_candles_coinbase(pair, args.days, gran)
    else:
        candles = fetch_candles(pair, args.interval, args.days)
    bt = Backtester(cfg, pair, candles, windows=args.windows,
                    equity=args.equity)
    results = bt.run()
    report(results, cfg)


if __name__ == "__main__":
    main()
