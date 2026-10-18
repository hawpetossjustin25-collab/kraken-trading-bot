#!/usr/bin/env python3
"""
Order Flow Engine — Kraken L2 order book + trade tape analysis.

Maintains a live order book from Kraken's public WebSocket (no API keys
needed), computes order-flow metrics the trading bot uses to make
smarter entries/exits, and writes signals to order_flow_state.json
every cycle so bot.py / the terminal can consume them.

Metrics produced:
  - bid/ask depth (USD value at each level)
  - Order Book Imbalance (OBI): -1 (ask heavy) .. +1 (bid heavy)
  - CVD proxy: cumulative volume delta from trade-side tape
  - spread, mid, microprice
  - wall detection (abnormally large resting orders)
  - whale trade detection (large prints on the tape)
  - depth slope / support & resistance walls
  - composite order-flow score in [-1, +1] (bullish/bearish pressure)

Usage:
    python order_flow.py            # run standalone, prints live metrics
    python order_flow.py --once     # fetch one REST depth snapshot, print, exit

Integration: bot.py calls OrderFlowFeed.latest_state() or reads
order_flow_state.json each cycle.
"""

import json
import logging
import math
import os
import statistics
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "order_flow_state.json")
FLOOR_MEM_PATH = os.path.join(BASE_DIR, "floor_memory.json")
LOG_PATH = os.path.join(BASE_DIR, "order_flow.log")

KRAKEN_WS = "wss://ws.kraken.com"
REST_DEPTH = "https://api.kraken.com/0/public/Depth"
DEFAULT_PAIR = "ETH/USD"          # WS format
REST_PAIR = "XETHZUSD"            # REST format
BOOK_DEPTH = 25                   # levels kept per side
TAPE_WINDOW = 300                 # seconds of trades kept
STATE_WRITE_SEC = 5               # how often state file is written


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _logger() -> logging.Logger:
    log = logging.getLogger("order_flow")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    log.addHandler(ch)
    return log


# ---------------------------------------------------------------------------
# Order book container
# ---------------------------------------------------------------------------

class OrderBook:
    """Price-time agnostic L2 book: {price: volume} per side, sorted views."""

    def __init__(self, depth: int = BOOK_DEPTH):
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.depth = depth
        self.last_update = 0.0

    def apply_snapshot(self, bids: list, asks: list):
        self.bids = {float(p): float(v) for p, v, *_ in bids}
        self.asks = {float(p): float(v) for p, v, *_ in asks}
        self.last_update = time.time()

    def apply_update(self, bids: list, asks: list):
        for p, v, *_ in bids:
            p, v = float(p), float(v)
            if v == 0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = v
        for p, v, *_ in asks:
            p, v = float(p), float(v)
            if v == 0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = v
        self._trim()
        self.last_update = time.time()

    def _trim(self):
        if len(self.bids) > self.depth * 2:
            keep = sorted(self.bids, reverse=True)[: self.depth * 2]
            self.bids = {p: self.bids[p] for p in keep}
        if len(self.asks) > self.depth * 2:
            keep = sorted(self.asks)[: self.depth * 2]
            self.asks = {p: self.asks[p] for p in keep}

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def mid(self) -> Optional[float]:
        b, a = self.best_bid(), self.best_ask()
        return (b + a) / 2 if b and a else None

    def spread(self) -> Optional[float]:
        b, a = self.best_bid(), self.best_ask()
        return a - b if b and a else None

    def depth_usd(self, side: str, levels: int = BOOK_DEPTH) -> float:
        """Total USD resting on one side within `levels` levels."""
        if side == "bid":
            px = sorted(self.bids, reverse=True)[:levels]
            return sum(self.bids[p] * p for p in px)
        px = sorted(self.asks)[:levels]
        return sum(self.asks[p] * p for p in px)

    def imbalance(self, levels: int = 10) -> float:
        """OBI in [-1, +1]; positive = more bid depth (bullish pressure)."""
        bid_usd = self.depth_usd("bid", levels)
        ask_usd = self.depth_usd("ask", levels)
        total = bid_usd + ask_usd
        return (bid_usd - ask_usd) / total if total else 0.0

    def microprice(self) -> Optional[float]:
        """Volume-weighted mid — better fair-value estimate than plain mid."""
        b, a = self.best_bid(), self.best_ask()
        if not b or not a:
            return None
        bv, av = self.bids[b], self.asks[a]
        if bv + av == 0:
            return (a + b) / 2
        return (a * bv + b * av) / (bv + av)

    def walls(self, side: str, multiple: float = 4.0) -> list[dict]:
        """Detect resting 'walls': levels with volume > multiple x median."""
        book = self.bids if side == "bid" else self.asks
        if len(book) < 5:
            return []
        vols = list(book.values())
        med = statistics.median(vols)
        if med == 0:
            return []
        out = []
        prices = sorted(book, reverse=(side == "bid"))
        for p in prices:
            if book[p] > med * multiple:
                out.append({"price": p, "volume": book[p],
                            "usd": p * book[p], "x_median": book[p] / med})
        return out


# ---------------------------------------------------------------------------
# Trade tape
# ---------------------------------------------------------------------------

class Tape:
    """Rolling window of prints: (ts, price, volume, side)."""

    def __init__(self, window_sec: int = TAPE_WINDOW):
        self.window = window_sec
        self.trades: deque = deque()

    def add(self, price: float, volume: float, side: str):
        now = time.time()
        self.trades.append((now, price, volume, side))
        cutoff = now - self.window
        while self.trades and self.trades[0][0] < cutoff:
            self.trades.popleft()

    def cvd(self, lookback_sec: Optional[int] = None) -> float:
        """Cumulative volume delta: buy-taker vol minus sell-taker vol."""
        cutoff = time.time() - (lookback_sec or self.window)
        delta = 0.0
        for ts, _, vol, side in self.trades:
            if ts >= cutoff:
                delta += vol if side == "b" else -vol
        return delta

    def whales(self, min_usd: float = 100_000) -> list[dict]:
        out = []
        for ts, price, vol, side in self.trades:
            if price * vol >= min_usd:
                out.append({"ts": ts, "price": price, "usd": price * vol,
                            "side": "buy" if side == "b" else "sell"})
        return out

    def buy_sell_ratio(self, lookback_sec: int = 60) -> float:
        cutoff = time.time() - lookback_sec
        buy = sum(v for t, _, v, s in self.trades if t >= cutoff and s == "b")
        sell = sum(v for t, _, v, s in self.trades if t >= cutoff and s == "s")
        tot = buy + sell
        return buy / tot if tot else 0.5


# ---------------------------------------------------------------------------
# WebSocket feed (stdlib-only via raw socket + TLS would be heavy; use
# 'websocket-client' if available, else REST depth polling fallback)
# ---------------------------------------------------------------------------

class OrderFlowFeed:
    """
    Live order-flow feed. Uses websocket-client if installed; otherwise
    falls back to REST Depth polling every poll_sec — still fully real data.
    """

    def __init__(self, pair_ws: str = DEFAULT_PAIR, pair_rest: str = REST_PAIR,
                 poll_sec: int = 5):
        self.pair_ws = pair_ws
        self.pair_rest = pair_rest
        self.poll_sec = poll_sec
        self.log = _logger()
        self.book = OrderBook()
        self.tape = Tape()
        self.running = False
        self.mode = "rest"
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        self._last_state_write = 0.0
        # rolling price history (~sampled each state write; 2h horizon)
        self._price_hist: deque = deque(maxlen=1440)
        # persistent floor memory — levels that held before, with outcomes
        self._floor_mem = self._load_floor_memory()

    # -- Persistent floor memory ---------------------------------------------

    def _load_floor_memory(self) -> dict:
        try:
            with open(FLOOR_MEM_PATH) as f:
                return json.load(f)
        except Exception:
            return {"levels": []}

    def _save_floor_memory(self):
        try:
            with open(FLOOR_MEM_PATH, "w") as f:
                json.dump(self._floor_mem, f, indent=2)
        except OSError:
            pass

    def record_floor_outcome(self, price: float, won: bool, ret_pct: float):
        """Called by the bot when a floor-buy exits. Learns which floor levels
        actually held — remembered floors that won before boost future scores."""
        lvl = {"price": round(price, 2), "ts": time.time(),
               "won": won, "ret": round(ret_pct, 3), "hits": 1}
        # merge into an existing level within ±1%
        for L in self._floor_mem["levels"]:
            if abs(L["price"] - price) / price < 0.01:
                L["hits"] += 1
                L["won"] = won
                L["ret"] = round((L.get("ret", 0) + ret_pct) / 2, 3)
                L["ts"] = time.time()
                break
        else:
            self._floor_mem["levels"].append(lvl)
        # cap at 100 levels, oldest first
        self._floor_mem["levels"].sort(key=lambda x: x["ts"])
        self._floor_mem["levels"] = self._floor_mem["levels"][-100:]
        self._save_floor_memory()

    def remembered_floor_boost(self, mid: float) -> float:
        """0–1 boost when price sits near a floor level that previously held.
        More hits + winning outcomes → stronger boost; stale floors decay."""
        if not mid or not self._floor_mem["levels"]:
            return 0.0
        best = 0.0
        now = time.time()
        for L in self._floor_mem["levels"]:
            dist = abs(mid - L["price"]) / mid
            if dist > 0.03:
                continue
            age_days = (now - L["ts"]) / 86400
            decay = max(0.0, 1.0 - age_days / 90.0)      # fades over ~3 months
            strength = min(1.0, (L.get("hits", 1) / 3) * (1.0 if L.get("won") else 0.3))
            proximity = 1.0 - dist / 0.03
            best = max(best, strength * decay * proximity)
        return round(best, 3)

    # -- REST fallback: full snapshot each poll (real data, slower) --

    def _rest_snapshot(self):
        url = f"{REST_DEPTH}?pair={self.pair_rest}&count={BOOK_DEPTH}"
        req = urllib.request.Request(url, headers={"User-Agent": "OrderFlow/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read())["result"]
        data = next(iter(result.values()))
        self.book.apply_snapshot(data["bids"], data["asks"])

    def _rest_loop(self):
        self.mode = "rest-poll"
        self.log.info("Order flow running in REST poll mode (%ss interval)", self.poll_sec)
        while self.running:
            try:
                self._rest_snapshot()
                self._maybe_write_state()
            except Exception as exc:
                self.log.warning("Depth poll failed: %s", exc)
                time.sleep(min(self.poll_sec * 2, 30))
            time.sleep(self.poll_sec)

    # -- WebSocket mode --

    def _ws_loop(self):
        try:
            import websocket  # websocket-client
        except ImportError:
            self.log.warning("websocket-client not installed; falling back to REST polling")
            self._rest_loop()
            return

        self.mode = "websocket"
        sub = json.dumps({
            "event": "subscribe",
            "pair": [self.pair_ws],
            "subscription": {"name": "book", "depth": BOOK_DEPTH},
        })
        sub_trade = json.dumps({
            "event": "subscribe",
            "pair": [self.pair_ws],
            "subscription": {"name": "trade"},
        })

        while self.running:
            try:
                self._ws = websocket.create_connection(KRAKEN_WS, timeout=30)
                self._ws.send(sub)
                self._ws.send(sub_trade)
                self.log.info("WebSocket connected — book depth %d, tape live", BOOK_DEPTH)
                while self.running:
                    msg = json.loads(self._ws.recv())
                    if isinstance(msg, dict):
                        continue  # heartbeat/status events
                    # array payloads: [channelID, data, channelName, pair]
                    payload = msg[1]
                    channel = msg[-2] if len(msg) >= 4 else ""
                    if isinstance(payload, dict):
                        if "bs" in payload or "as" in payload:
                            self.book.apply_snapshot(payload.get("bs", []),
                                                     payload.get("as", []))
                        else:
                            self.book.apply_update(payload.get("b", []),
                                                   payload.get("a", []))
                    elif isinstance(payload, list) and "trade" in str(channel):
                        for t in payload:
                            self.tape.add(float(t[0]), float(t[1]), t[3])
                    self._maybe_write_state()
            except Exception as exc:
                self.log.warning("WS disconnected (%s) — reconnecting in 5s", exc)
                time.sleep(5)

    # -- Lifecycle --

    def start(self):
        self.running = True
        target = self._rest_loop if self.poll_sec else self._ws_loop
        # Prefer WS when websocket-client is available
        try:
            import websocket  # noqa
            target = self._ws_loop
        except ImportError:
            target = self._rest_loop
        self._thread = threading.Thread(target=target, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # -- Metrics & output --

    def composite_score(self) -> float:
        """
        Blend of OBI (40%), CVD-normalized (30%), microprice-vs-mid (15%),
        and 60s buy/sell ratio (15%). Range [-1, +1].
        """
        obi = self.book.imbalance(10)
        mid = self.book.mid()
        micro = self.book.microprice() or mid or 0
        micro_edge = 0.0
        if mid:
            micro_edge = max(-1.0, min(1.0, (micro - mid) / mid * 1000))
        cvd = self.tape.cvd(120)
        cvd_norm = max(-1.0, min(1.0, cvd / 50.0))   # ~50 ETH scale
        ratio = (self.tape.buy_sell_ratio(60) - 0.5) * 2.0
        return round(0.4 * obi + 0.3 * cvd_norm + 0.15 * micro_edge + 0.15 * ratio, 4)

    def latest_state(self) -> dict:
        mid = self.book.mid()
        return {
            "timestamp": datetime.now().isoformat(),
            "mode": self.mode,
            "pair": self.pair_ws,
            "mid": mid,
            "spread": self.book.spread(),
            "microprice": self.book.microprice(),
            "obi_10lvl": round(self.book.imbalance(10), 4),
            "bid_depth_usd": round(self.book.depth_usd("bid"), 0),
            "ask_depth_usd": round(self.book.depth_usd("ask"), 0),
            "cvd_5min_eth": round(self.tape.cvd(300), 4),
            "buy_ratio_60s": round(self.tape.buy_sell_ratio(60), 3),
            "bid_walls": self.book.walls("bid"),
            "ask_walls": self.book.walls("ask"),
            "whale_trades": self.tape.whales(),
            "composite_score": self.composite_score(),
            "floor_score": self.floor_score(),
            "floor": self.is_floor(),
            "signal": ("bullish" if self.composite_score() > 0.15 else
                       "bearish" if self.composite_score() < -0.15 else "neutral"),
            "book_age_sec": round(time.time() - self.book.last_update, 1),
        }

    # -- Floor detection --
    # "Buy the floor": fires when price has dropped hard AND order flow shows
    # sellers exhausting at a defended level. Combines the three signals the
    # research validates:
    #   1. capitulation  — price ≥ drop_pct below rolling high
    #   2. bullish CVD divergence — price near lows but CVD stopped falling
    #   3. bid-wall support — large resting bids below current price
    #   4. absorption — sell-side flow hitting the wall without price falling

    def floor_score(self) -> float:
        """0.0–1.0 confidence that price is at/near a local floor."""
        mid = self.book.mid()
        if not mid:
            return 0.0

        # 1) Capitulation: how far below the rolling session high
        ph = list(self._price_hist)
        if len(ph) < 30:
            return 0.0
        hi = max(ph)
        drawdown = (hi - mid) / hi
        cap_score = min(1.0, drawdown / 0.08)          # ≥8% drop → full marks
        if cap_score <= 0.3:
            return 0.0                                # nowhere near a dip

        # 2) CVD divergence: price still near lows but CVD recovering
        cvd_5m = self.tape.cvd(300)
        cvd_1m = self.tape.cvd(60)
        div_score = 0.0
        if cvd_1m > cvd_5m / 5 and cvd_1m > 0:        # recent delta flipping up
            div_score = min(1.0, cvd_1m / 10 + 0.5)

        # 3) Bid wall below price (within 1.5%)
        wall_score = 0.0
        for w in self.book.walls("bid"):
            dist = (mid - w["price"]) / mid
            if -0.002 <= dist <= 0.015:
                wall_score = min(1.0, w.get("x_median", 1) / 10)
                break

        # 4) Absorption: strong sell pressure but book imbalance positive
        obi = self.book.imbalance(10)
        ratio = self.tape.buy_sell_ratio(60)
        abs_score = 0.0
        if ratio < 0.45 and obi > 0.1:                # sellers hitting bids, book holds
            abs_score = min(1.0, obi * 3)

        # 5) Memory: floors that held before get remembered — learned edge
        mem_score = self.remembered_floor_boost(mid)

        score = round(0.25 * cap_score + 0.20 * div_score +
                      0.20 * wall_score + 0.15 * abs_score +
                      0.20 * mem_score, 4)
        return score

    def is_floor(self, threshold: float = 0.65) -> bool:
        return self.floor_score() >= threshold

    def _maybe_write_state(self):
        now = time.time()
        if now - self._last_state_write >= STATE_WRITE_SEC:
            mid = self.book.mid()
            if mid:
                self._price_hist.append(mid)
            try:
                with open(STATE_PATH, "w") as f:
                    json.dump(self.latest_state(), f, indent=2)
            except OSError as e:
                self.log.warning("State write failed: %s", e)
            self._last_state_write = now


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    log = _logger()
    feed = OrderFlowFeed()

    if "--once" in os.sys.argv:
        feed._rest_snapshot()
        print(json.dumps(feed.latest_state(), indent=2))
        return

    feed.start()
    log.info("Order flow engine started. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(10)
            s = feed.latest_state()
            log.info("mid=$%s spread=$%s OBI=%+.3f CVD=%+.2f score=%+.3f (%s) [%s]",
                     f"{s['mid']:.2f}" if s["mid"] else "?",
                     f"{s['spread']:.2f}" if s["spread"] else "?",
                     s["obi_10lvl"], s["cvd_5min_eth"],
                     s["composite_score"], s["signal"], s["mode"])
    except KeyboardInterrupt:
        feed.stop()
        log.info("Stopped.")


if __name__ == "__main__":
    main()
