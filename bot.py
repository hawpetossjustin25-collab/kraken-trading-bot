#!/usr/bin/env python3
# ─── SECURITY: Set env vars instead of storing keys in config.json ───
# export KRAKEN_API_KEY=your_api_key_here
# export KRAKEN_API_SECRET=your_api_secret_here
# Env vars take priority over config.json values.
# ─────────────────────────────────────────────────────────────────────
"""
Kraken ETH/USD Multi-Strategy Trading Bot

Strategies (layered, independently toggleable):
  1. Grid Trading  — buy/sell limit orders above/below mid-price
  2. Dip Buying    — market buy on 3%+ drops within 5 min, take-profit sell
  3. Scalping      — quick limit buy/sell on short-term volatility

Usage:
    python bot.py              # run continuously
    python bot.py --once       # run one cycle then exit
"""

import hashlib
import hmac
import base64
import json
import logging
import math
import os
import random
import re
import sys
import time
import urllib.parse
from collections import deque
from datetime import datetime
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def load_config() -> dict:
    """Load configuration from config.json. API keys from env vars take priority."""
    if not os.path.exists(CONFIG_PATH):
        print(f"ERROR: {CONFIG_PATH} not found. Copy config.json.example and fill in your keys.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    # Environment variables override config.json for security
    if os.environ.get("KRAKEN_API_KEY"):
        cfg["api_key"] = os.environ["KRAKEN_API_KEY"]
    if os.environ.get("KRAKEN_API_SECRET"):
        cfg["api_secret"] = os.environ["KRAKEN_API_SECRET"]
    if cfg.get("api_key", "").startswith("YOUR_"):
        print("ERROR: Set KRAKEN_API_KEY/KRAKEN_API_SECRET env vars or replace placeholders in config.json.")
        sys.exit(1)
    return cfg


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_file: str) -> logging.Logger:
    logger = logging.getLogger("kraken_bot")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(os.path.join(BASE_DIR, log_file))
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger


# ---------------------------------------------------------------------------
# Kraken API Client
# ---------------------------------------------------------------------------

API_URL = "https://api.kraken.com"


# ---------------------------------------------------------------------------
# Trade Rate Limiter & Daily Loss Tracker
# ---------------------------------------------------------------------------

class TradeRateLimiter:
    """
    Enforces:
      - Max 1 trade per 5 seconds
      - Max 50 trades per hour (rolling window)
      - Daily max loss limit (stops trading for the day if exceeded)
    """

    def __init__(self, config: dict, logger: logging.Logger):
        self.log = logger
        self.min_interval_sec = 5  # 1 trade per 5 seconds
        self.max_per_hour = 50
        self.daily_max_loss = config.get("daily_max_loss_usd", 30.0)
        self._last_trade_time: float = 0.0
        self._hourly_trades: deque = deque()  # timestamps of trades within last hour
        self._daily_loss: float = 0.0
        self._daily_loss_date: str = ""
        self._stopped_for_day = False

    def _reset_daily_if_needed(self):
        today = datetime.now().strftime("%Y-%m-%d")
        if self._daily_loss_date != today:
            self._daily_loss_date = today
            self._daily_loss = 0.0
            self._stopped_for_day = False

    def record_loss(self, usd_loss: float):
        """Record a loss amount (positive number = loss)."""
        self._reset_daily_if_needed()
        if usd_loss > 0:
            self._daily_loss += usd_loss
            if self._daily_loss >= self.daily_max_loss:
                self._stopped_for_day = True
                self.log.critical(
                    "⛔ DAILY LOSS LIMIT reached ($%.2f / $%.2f). Stopping trades for today.",
                    self._daily_loss, self.daily_max_loss,
                )

    def can_trade(self) -> bool:
        """Check if a trade is allowed right now. Returns False if rate limited or daily loss exceeded."""
        self._reset_daily_if_needed()

        if self._stopped_for_day:
            self.log.warning("[RateLimit] Daily loss limit reached. No more trades today.")
            return False

        now = time.time()

        # Min interval check
        if now - self._last_trade_time < self.min_interval_sec:
            self.log.warning("[RateLimit] Too fast — min %ds between trades.", self.min_interval_sec)
            return False

        # Hourly cap check
        cutoff = now - 3600
        while self._hourly_trades and self._hourly_trades[0] < cutoff:
            self._hourly_trades.popleft()
        if len(self._hourly_trades) >= self.max_per_hour:
            self.log.warning("[RateLimit] Hourly limit reached (%d/%d).", len(self._hourly_trades), self.max_per_hour)
            return False

        return True

    def record_trade(self):
        """Record that a trade was placed."""
        now = time.time()
        self._last_trade_time = now
        self._hourly_trades.append(now)


# Module-level rate limiter — initialized in TradingBot.__init__
_trade_rate_limiter: Optional[TradeRateLimiter] = None


class KrakenAPI:
    """Minimal Kraken REST API client using requests + stdlib only."""

    def __init__(self, api_key: str, api_secret: str, logger: logging.Logger):
        self.api_key = api_key
        self.api_secret = api_secret
        self.log = logger
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "KrakenMultiBot/2.0"})

    # -- Authentication helpers --

    def _sign(self, urlpath: str, data: dict) -> dict:
        postdata = urllib.parse.urlencode(data)
        encoded = (str(data["nonce"]) + postdata).encode()
        message = urlpath.encode() + hashlib.sha256(encoded).digest()
        mac = hmac.new(base64.b64decode(self.api_secret), message, hashlib.sha512)
        return {
            "API-Key": self.api_key,
            "API-Sign": base64.b64encode(mac.digest()).decode(),
        }

    @staticmethod
    def _nonce() -> int:
        return int(time.time() * 1000)

    # -- Request methods --

    def _public(self, endpoint: str, params: Optional[dict] = None) -> dict:
        url = f"{API_URL}/0/public/{endpoint}"
        for attempt in range(3):
            try:
                resp = self.session.get(url, params=params, timeout=15)
                resp.raise_for_status()
                body = resp.json()
                if body.get("error"):
                    errs = body["error"]
                    if any("Rate limit" in e for e in errs):
                        self.log.warning("Rate limited (public). Backing off %ds…", 2 ** attempt)
                        time.sleep(2 ** attempt)
                        continue
                    raise RuntimeError(f"Kraken API error: {errs}")
                return body["result"]
            except requests.RequestException as exc:
                self.log.warning("Request failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Public endpoint {endpoint} failed after 3 attempts")

    def _private(self, endpoint: str, data: Optional[dict] = None) -> dict:
        urlpath = f"/0/private/{endpoint}"
        url = f"{API_URL}{urlpath}"
        if data is None:
            data = {}
        data["nonce"] = self._nonce()
        headers = self._sign(urlpath, data)

        for attempt in range(3):
            try:
                resp = self.session.post(url, data=data, headers=headers, timeout=15)
                resp.raise_for_status()
                body = resp.json()
                if body.get("error"):
                    errs = body["error"]
                    if any("Rate limit" in e for e in errs):
                        self.log.warning("Rate limited (private). Backing off %ds…", 3 * (attempt + 1))
                        time.sleep(3 * (attempt + 1))
                        data["nonce"] = self._nonce()
                        headers = self._sign(urlpath, data)
                        continue
                    if any("Insufficient" in e for e in errs):
                        self.log.error("Insufficient funds: %s", errs)
                        return {}
                    raise RuntimeError(f"Kraken API error: {errs}")
                return body["result"]
            except requests.RequestException as exc:
                self.log.warning("Request failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(2 ** attempt)
                data["nonce"] = self._nonce()
                headers = self._sign(urlpath, data)
        raise RuntimeError(f"Private endpoint {endpoint} failed after 3 attempts")

    # -- Public endpoints --

    def ticker(self, pair: str) -> dict:
        return self._public("Ticker", {"pair": pair})

    # -- Private endpoints --

    def balance(self) -> dict:
        return self._private("Balance")

    def trade_balance(self, asset: str = "ZUSD") -> dict:
        return self._private("TradeBalance", {"asset": asset})

    def open_orders(self) -> dict:
        return self._private("OpenOrders")

    def add_order(self, pair: str, side: str, volume: str, price: str,
                  ordertype: str = "limit", oflags: str = "post") -> dict:
        """Place an order. Returns order result dict or {} on failure."""
        global _trade_rate_limiter
        if _trade_rate_limiter and not _trade_rate_limiter.can_trade():
            self.log.warning("Order blocked by rate limiter.")
            return {}
        self.log.info("Placing %s %s order: %s %s @ $%s", ordertype, side, volume, pair, price)
        data = {
            "pair": pair,
            "type": side,
            "ordertype": ordertype,
            "volume": volume,
        }
        if ordertype == "limit":
            data["price"] = price
            if oflags:
                data["oflags"] = oflags
        # For market orders, no price needed
        result = self._private("AddOrder", data)
        if result and _trade_rate_limiter:
            _trade_rate_limiter.record_trade()
        return result

    def add_market_order(self, pair: str, side: str, volume: str) -> dict:
        """Place a market order (no price)."""
        global _trade_rate_limiter
        if _trade_rate_limiter and not _trade_rate_limiter.can_trade():
            self.log.warning("Market order blocked by rate limiter.")
            return {}
        self.log.info("Placing market %s order: %s %s", side, volume, pair)
        result = self._private("AddOrder", {
            "pair": pair,
            "type": side,
            "ordertype": "market",
            "volume": volume,
        })
        if result and _trade_rate_limiter:
            _trade_rate_limiter.record_trade()
        return result

    def cancel_order(self, txid: str) -> dict:
        return self._private("CancelOrder", {"txid": txid})

    def cancel_all(self) -> dict:
        return self._private("CancelAll")


# ---------------------------------------------------------------------------
# Price Tracker — in-memory rolling price history
# ---------------------------------------------------------------------------

class PriceTracker:
    """
    Maintains a rolling window of (timestamp, price) tuples for the last
    N seconds (default 600 = 10 minutes). Used by dip-buying and scalping
    strategies to detect price movements without external data sources.
    """

    def __init__(self, window_sec: int = 600):
        self.window_sec = window_sec
        self.prices: deque[tuple[float, float]] = deque()  # (timestamp, price)

    def record(self, price: float):
        """Record the current price and prune old entries."""
        now = time.time()
        self.prices.append((now, price))
        cutoff = now - self.window_sec
        while self.prices and self.prices[0][0] < cutoff:
            self.prices.popleft()

    def recent_high(self, lookback_sec: int = 300) -> Optional[float]:
        """Return the highest price in the last `lookback_sec` seconds."""
        cutoff = time.time() - lookback_sec
        highs = [p for t, p in self.prices if t >= cutoff]
        return max(highs) if highs else None

    def recent_low(self, lookback_sec: int = 300) -> Optional[float]:
        """Return the lowest price in the last `lookback_sec` seconds."""
        cutoff = time.time() - lookback_sec
        lows = [p for t, p in self.prices if t >= cutoff]
        return min(lows) if lows else None

    def volatility(self, lookback_sec: int = 120) -> Optional[float]:
        """
        Return the price swing as a percentage over `lookback_sec`.
        Calculated as (high - low) / low * 100.
        Returns None if insufficient data.
        """
        cutoff = time.time() - lookback_sec
        window = [p for t, p in self.prices if t >= cutoff]
        if len(window) < 2:
            return None
        lo, hi = min(window), max(window)
        if lo == 0:
            return None
        return (hi - lo) / lo * 100.0

    def latest(self) -> Optional[float]:
        """Return the most recent recorded price, or None."""
        return self.prices[-1][1] if self.prices else None

    def count(self) -> int:
        return len(self.prices)


# ---------------------------------------------------------------------------
# Polymarket Sentiment Scanner
# ---------------------------------------------------------------------------

POLYMARKET_API = "https://gamma-api.polymarket.com"

# Keywords that suggest bullish sentiment when odds are high
BULLISH_PATTERNS = [
    r"(?:ETH|ethereum|ether)\b.*(?:above|over|exceed|reach|hit|break)\b.*\$?\d",
    r"(?:ETH|ethereum)\b.*(?:all.time.high|ath|new.high|rally|surge|moon)",
    r"(?:ETH|ethereum)\b.*(?:price)\b.*(?:increase|rise|gain|up)",
    r"(?:crypto|bitcoin|btc)\b.*(?:bull|rally|surge|above|over|reach)",
]

# Keywords that suggest bearish sentiment when odds are high
BEARISH_PATTERNS = [
    r"(?:ETH|ethereum|ether)\b.*(?:below|under|drop|fall|crash|dump)",
    r"(?:ETH|ethereum)\b.*(?:new.low|bear|collapse|decline)",
    r"(?:crypto|bitcoin|btc)\b.*(?:bear|crash|collapse|below|under|drop)",
]


class PolymarketScanner:
    """
    Fetches crypto-related prediction markets from Polymarket's public API,
    extracts odds/probabilities, and calculates a sentiment score from
    -1.0 (bearish) to +1.0 (bullish). Volume-weighted.

    Caches results to avoid excessive API calls. Falls back to neutral (0.0)
    on any error.
    """

    def __init__(self, config: dict, logger: logging.Logger):
        self.log = logger
        self.enabled = config.get("polymarket_enabled", False)
        self.weight = config.get("polymarket_weight", 0.3)
        self.cache_minutes = config.get("polymarket_cache_minutes", 15)
        self.keywords = config.get("polymarket_keywords",
                                   ["ethereum", "ETH", "crypto", "bitcoin", "BTC"])
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "KrakenMultiBot/2.0"})

        # Cache
        self._cached_score: float = 0.0
        self._cache_time: float = 0.0

        # Compile patterns
        self._bullish_re = [re.compile(p, re.IGNORECASE) for p in BULLISH_PATTERNS]
        self._bearish_re = [re.compile(p, re.IGNORECASE) for p in BEARISH_PATTERNS]

    def get_sentiment(self) -> float:
        """
        Return a sentiment score between -1.0 and +1.0.
        Uses cached value if within cache window. Returns 0.0 on failure.
        """
        if not self.enabled:
            return 0.0

        now = time.time()
        if now - self._cache_time < self.cache_minutes * 60:
            return self._cached_score

        try:
            score = self._fetch_and_score()
        except Exception as exc:
            self.log.warning("[Polymarket] API error, returning neutral: %s", exc)
            score = 0.0

        self._cached_score = score
        self._cache_time = now
        return score

    def _fetch_and_score(self) -> float:
        """Fetch markets from Polymarket and compute volume-weighted sentiment."""
        markets = self._search_markets()
        if not markets:
            self.log.info("[Polymarket] No relevant markets found")
            return 0.0

        weighted_sum = 0.0
        total_weight = 0.0

        for market in markets:
            question = market.get("question", "") or market.get("title", "")
            # outcomePrices is a JSON string like "[\"0.65\",\"0.35\"]" for Yes/No
            outcome_prices_raw = market.get("outcomePrices", "")
            volume_str = market.get("volume", "0") or market.get("volumeNum", "0") or "0"

            try:
                volume = float(volume_str)
            except (ValueError, TypeError):
                volume = 0.0

            if volume < 100:  # skip very low volume markets
                continue

            # Parse outcome prices
            try:
                if isinstance(outcome_prices_raw, str):
                    prices = json.loads(outcome_prices_raw)
                else:
                    prices = outcome_prices_raw
                yes_price = float(prices[0]) if prices else 0.5
            except (json.JSONDecodeError, IndexError, TypeError, ValueError):
                yes_price = 0.5

            # Classify the market as bullish or bearish signal
            direction = self._classify_market(question)
            if direction == 0:
                continue  # can't classify, skip

            # Signal: direction * (yes_price - 0.5) * 2 → maps [0,1] to [-1,1]
            signal = direction * (yes_price - 0.5) * 2.0
            vol_weight = min(volume, 1_000_000)  # cap influence of mega-markets

            weighted_sum += signal * vol_weight
            total_weight += vol_weight

            self.log.debug("[Polymarket] Market: '%s' | Yes=%.2f | Vol=$%.0f | Dir=%+d | Sig=%+.3f",
                           question[:80], yes_price, volume, direction, signal)

        if total_weight == 0:
            return 0.0

        raw_score = weighted_sum / total_weight
        # Clamp to [-1, 1]
        score = max(-1.0, min(1.0, raw_score))
        self.log.info("[Polymarket] Sentiment score: %+.3f (from %d markets)",
                      score, len(markets))
        return score

    def _search_markets(self) -> list:
        """Search Polymarket for crypto-related active markets."""
        all_markets = []
        seen_ids = set()

        for keyword in self.keywords:
            try:
                resp = self.session.get(
                    f"{POLYMARKET_API}/markets",
                    params={
                        "tag": "crypto",
                        "limit": 50,
                        "active": "true",
                        "closed": "false",
                        "order": "volume",
                        "ascending": "false",
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()

                # API may return a list or a dict with results
                markets = data if isinstance(data, list) else data.get("data", data.get("markets", []))

                for m in markets:
                    mid = m.get("id") or m.get("conditionId", "")
                    question = (m.get("question", "") or m.get("title", "")).lower()
                    # Filter to keyword-relevant markets
                    if keyword.lower() in question and mid not in seen_ids:
                        seen_ids.add(mid)
                        all_markets.append(m)
            except Exception as exc:
                self.log.debug("[Polymarket] Search failed for '%s': %s", keyword, exc)
                continue

            # Also try text search
            try:
                resp = self.session.get(
                    f"{POLYMARKET_API}/markets",
                    params={
                        "limit": 20,
                        "active": "true",
                        "closed": "false",
                        "slug_contains": keyword.lower(),
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                markets = data if isinstance(data, list) else data.get("data", data.get("markets", []))
                for m in markets:
                    mid = m.get("id") or m.get("conditionId", "")
                    if mid not in seen_ids:
                        seen_ids.add(mid)
                        all_markets.append(m)
            except Exception:
                pass

        return all_markets

    def _classify_market(self, question: str) -> int:
        """
        Classify a market question as bullish (+1), bearish (-1), or unknown (0).
        If "Yes" outcome is bullish, return +1. If "Yes" outcome is bearish, return -1.
        """
        for pat in self._bullish_re:
            if pat.search(question):
                return 1
        for pat in self._bearish_re:
            if pat.search(question):
                return -1
        return 0


# ---------------------------------------------------------------------------
# Messari Metrics Scanner
# ---------------------------------------------------------------------------

MESSARI_API = "https://data.messari.io/api/v1"


class MessariScanner:
    """
    Fetches ETH asset metrics from Messari's free public API.
    Calculates a sentiment score from -1.0 to +1.0 based on momentum
    indicators (price change %, volatility trends, volume trends).
    Caches for 15 minutes. Graceful fallback to 0.0.
    """

    def __init__(self, config: dict, logger: logging.Logger):
        self.log = logger
        sources = config.get("sentiment_sources", {})
        self.enabled = sources.get("messari", True)
        self.cache_minutes = 15
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "KrakenMultiBot/2.0"})
        self._cached_score: float = 0.0
        self._cache_time: float = 0.0

    def get_sentiment(self) -> float:
        if not self.enabled:
            return 0.0
        now = time.time()
        if now - self._cache_time < self.cache_minutes * 60:
            return self._cached_score
        try:
            score = self._fetch_and_score()
        except Exception as exc:
            self.log.warning("[Messari] API error, returning neutral: %s", exc)
            score = 0.0
        self._cached_score = score
        self._cache_time = now
        return score

    def _fetch_and_score(self) -> float:
        resp = self.session.get(
            f"{MESSARI_API}/assets/ethereum/metrics",
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        metrics = data.get("data", {})

        market = metrics.get("market_data", {})
        roi = metrics.get("roi_data", {})

        # Extract momentum signals
        signals = []

        # 24h price change percent
        pct_24h = market.get("percent_change_usd_last_24_hours")
        if pct_24h is not None:
            # Normalize: ±10% maps to ±1.0
            signals.append(max(-1.0, min(1.0, pct_24h / 10.0)))

        # 1h price change
        pct_1h = market.get("percent_change_usd_last_1_hour")
        if pct_1h is not None:
            signals.append(max(-1.0, min(1.0, pct_1h / 3.0)))

        # Volume change (24h vs previous): positive = increasing volume = bullish signal
        vol_24h = market.get("real_volume_last_24_hours")
        vol_prev = market.get("volume_last_24_hours_overstatement_multiple")
        if vol_24h and vol_prev and vol_prev > 0:
            # If overstatement multiple is low, real volume is relatively high → bullish
            vol_signal = max(-1.0, min(1.0, (1.0 - vol_prev) * 0.5))
            signals.append(vol_signal)

        # 7d ROI
        pct_7d = roi.get("percent_change_last_1_week")
        if pct_7d is not None:
            signals.append(max(-1.0, min(1.0, pct_7d / 15.0)))

        if not signals:
            self.log.info("[Messari] No usable metrics found")
            return 0.0

        score = sum(signals) / len(signals)
        score = max(-1.0, min(1.0, score))
        self.log.info("[Messari] Sentiment score: %+.3f (from %d signals)", score, len(signals))
        return score


# ---------------------------------------------------------------------------
# News / Fear & Greed Sentiment Scanner
# ---------------------------------------------------------------------------

FEAR_GREED_API = "https://api.alternative.me/fng/"


class NewsSentimentScanner:
    """
    Fetches the Crypto Fear & Greed Index from alternative.me.
    Converts the 0-100 index to a -1.0 to +1.0 sentiment scale.
    Caches for 30 minutes. Graceful fallback to 0.0.
    """

    def __init__(self, config: dict, logger: logging.Logger):
        self.log = logger
        sources = config.get("sentiment_sources", {})
        self.enabled = sources.get("fear_greed", True)
        self.cache_minutes = 30
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "KrakenMultiBot/2.0"})
        self._cached_score: float = 0.0
        self._cache_time: float = 0.0

    def get_sentiment(self) -> float:
        if not self.enabled:
            return 0.0
        now = time.time()
        if now - self._cache_time < self.cache_minutes * 60:
            return self._cached_score
        try:
            score = self._fetch_and_score()
        except Exception as exc:
            self.log.warning("[FearGreed] API error, returning neutral: %s", exc)
            score = 0.0
        self._cached_score = score
        self._cache_time = now
        return score

    def _fetch_and_score(self) -> float:
        resp = self.session.get(FEAR_GREED_API, params={"limit": 1}, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        entries = data.get("data", [])
        if not entries:
            self.log.info("[FearGreed] No data returned")
            return 0.0

        value = int(entries[0].get("value", 50))
        classification = entries[0].get("value_classification", "Neutral")

        # Convert 0-100 to -1.0 to +1.0: (value - 50) / 50
        score = (value - 50) / 50.0
        score = max(-1.0, min(1.0, score))

        self.log.info("[FearGreed] Index=%d (%s) → score=%+.3f", value, classification, score)
        return score


# ---------------------------------------------------------------------------
# CoinAPI / CoinGecko Market Data Scanner
# ---------------------------------------------------------------------------

COINGECKO_API = "https://api.coingecko.com/api/v3"


class CoinAPIScanner:
    """
    Fetches ETH market data from CoinGecko's free API.
    Compares global average price vs Kraken price, tracks volume trends,
    and calculates market dominance signals.
    Caches for 10 minutes. Graceful fallback to 0.0.
    """

    def __init__(self, config: dict, logger: logging.Logger):
        self.log = logger
        sources = config.get("sentiment_sources", {})
        self.enabled = sources.get("market_data", True)
        self.cache_minutes = 10
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "KrakenMultiBot/2.0"})
        self._cached_score: float = 0.0
        self._cache_time: float = 0.0
        self._cached_global_price: Optional[float] = None
        self._prev_volume: Optional[float] = None

    def get_sentiment(self) -> float:
        if not self.enabled:
            return 0.0
        now = time.time()
        if now - self._cache_time < self.cache_minutes * 60:
            return self._cached_score
        try:
            score = self._fetch_and_score()
        except Exception as exc:
            self.log.warning("[MarketData] API error, returning neutral: %s", exc)
            score = 0.0
        self._cached_score = score
        self._cache_time = now
        return score

    def get_global_price(self) -> Optional[float]:
        """Return the last fetched global average ETH price, or None."""
        return self._cached_global_price

    def _fetch_and_score(self) -> float:
        # Fetch ETH market data from CoinGecko
        resp = self.session.get(
            f"{COINGECKO_API}/coins/ethereum",
            params={
                "localization": "false",
                "tickers": "false",
                "community_data": "false",
                "developer_data": "false",
                "sparkline": "false",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        market = data.get("market_data", {})
        signals = []

        # 1. 24h price change %
        pct_24h = market.get("price_change_percentage_24h")
        if pct_24h is not None:
            signals.append(max(-1.0, min(1.0, pct_24h / 10.0)))

        # 2. Global average price for Kraken comparison
        current_price = market.get("current_price", {}).get("usd")
        if current_price:
            self._cached_global_price = current_price

        # 3. Volume trend: compare current 24h volume to market cap ratio
        total_volume = market.get("total_volume", {}).get("usd")
        market_cap = market.get("market_cap", {}).get("usd")
        if total_volume and market_cap and market_cap > 0:
            vol_ratio = total_volume / market_cap
            # Typical vol/mcap ratio ~0.03-0.10; higher = more activity = stronger signal
            if self._prev_volume is not None:
                vol_change = (total_volume - self._prev_volume) / self._prev_volume if self._prev_volume > 0 else 0
                # Increasing volume is bullish signal
                signals.append(max(-1.0, min(1.0, vol_change * 2.0)))
            self._prev_volume = total_volume

        # 4. Market dominance change (7d)
        pct_7d = market.get("price_change_percentage_7d")
        if pct_7d is not None:
            signals.append(max(-1.0, min(1.0, pct_7d / 15.0)))

        # 5. ATH proximity (closer to ATH = more bullish momentum)
        ath = market.get("ath", {}).get("usd")
        if ath and current_price and ath > 0:
            ath_ratio = current_price / ath
            # Map: at ATH (1.0) → +1.0, at 50% of ATH → 0.0, at 0 → -1.0
            ath_signal = (ath_ratio - 0.5) * 2.0
            signals.append(max(-1.0, min(1.0, ath_signal)))

        if not signals:
            self.log.info("[MarketData] No usable data")
            return 0.0

        score = sum(signals) / len(signals)
        score = max(-1.0, min(1.0, score))
        self.log.info("[MarketData] Sentiment score: %+.3f (from %d signals, vol=$%.0f)",
                      score, len(signals), total_volume or 0)
        return score


# ---------------------------------------------------------------------------
# Sentiment Aggregator — combines all sentiment sources
# ---------------------------------------------------------------------------

class SentimentAggregator:
    """
    Combines multiple sentiment sources into a single weighted score.
    Each source produces -1.0 to +1.0; the aggregator returns a weighted
    average. Disabled sources are excluded from the weight normalization.
    """

    def __init__(self, config: dict, logger: logging.Logger,
                 polymarket: PolymarketScanner,
                 messari: MessariScanner,
                 fear_greed: NewsSentimentScanner,
                 market_data: CoinAPIScanner):
        self.log = logger
        self.polymarket = polymarket
        self.messari = messari
        self.fear_greed = fear_greed
        self.market_data = market_data

        weights = config.get("sentiment_weights", {})
        self.weights = {
            "polymarket": weights.get("polymarket", 0.3),
            "messari": weights.get("messari", 0.25),
            "fear_greed": weights.get("fear_greed", 0.25),
            "market_data": weights.get("market_data", 0.2),
        }
        self.sources = {
            "polymarket": self.polymarket,
            "messari": self.messari,
            "fear_greed": self.fear_greed,
            "market_data": self.market_data,
        }

    def get_aggregate_sentiment(self) -> float:
        """
        Fetch sentiment from all enabled sources and return a weighted
        average score from -1.0 to +1.0. Logs individual + aggregate scores.
        """
        weighted_sum = 0.0
        total_weight = 0.0
        scores = {}

        for name, scanner in self.sources.items():
            if not scanner.enabled:
                continue
            score = scanner.get_sentiment()
            weight = self.weights.get(name, 0.0)
            scores[name] = score
            weighted_sum += score * weight
            total_weight += weight

        if total_weight == 0:
            self.log.info("[SentimentAgg] No enabled sources, returning neutral")
            return 0.0

        aggregate = weighted_sum / total_weight
        aggregate = max(-1.0, min(1.0, aggregate))

        parts = " | ".join(f"{k}={v:+.3f}" for k, v in scores.items())
        self.log.info("[SentimentAgg] %s → aggregate=%+.3f", parts, aggregate)
        return aggregate

    def get_global_price(self) -> Optional[float]:
        """Return global average ETH price from CoinGecko, if available."""
        return self.market_data.get_global_price()


# ---------------------------------------------------------------------------
# Strategy helpers — parse the "strategy" config string
# ---------------------------------------------------------------------------

def parse_strategies(strategy_str: str) -> set[str]:
    """
    Parse a strategy string like "grid+dip+scalp" into a set of
    enabled strategy names: {"grid", "dip", "scalp"}.
    """
    return {s.strip().lower() for s in strategy_str.split("+")} - {""}


# ---------------------------------------------------------------------------
# Grid Strategy (original, unchanged logic)
# ---------------------------------------------------------------------------

class GridStrategy:
    """Grid trading: buy below, sell above, rebuild on fills."""

    def __init__(self, api: KrakenAPI, pair: str, config: dict, logger: logging.Logger):
        self.api = api
        self.pair = pair
        self.cfg = config
        self.log = logger
        self.grid_levels = config.get("grid_levels", 6)
        self.spacing_pct = config.get("grid_spacing_pct", 1.75) / 100.0
        self.active_orders: dict[str, dict] = {}  # txid -> {side, price, volume}

    def compute_grid(self, mid_price: float, balance_usd: float) -> list[dict]:
        levels = self.grid_levels
        half = levels // 2
        orders = []
        usd_per_level = balance_usd / levels

        for i in range(1, half + 1):
            buy_price = mid_price * (1 - self.spacing_pct * i)
            volume = usd_per_level / buy_price
            orders.append({"side": "buy", "price": f"{buy_price:.2f}", "volume": f"{volume:.6f}"})

        for i in range(1, half + 1):
            sell_price = mid_price * (1 + self.spacing_pct * i)
            volume = usd_per_level / sell_price
            orders.append({"side": "sell", "price": f"{sell_price:.2f}", "volume": f"{volume:.6f}"})

        if levels % 2 == 1:
            extra_price = mid_price * (1 - self.spacing_pct * 0.5)
            volume = usd_per_level / extra_price
            orders.append({"side": "buy", "price": f"{extra_price:.2f}", "volume": f"{volume:.6f}"})

        return orders

    def cancel_all_orders(self):
        self.log.info("[Grid] Cancelling all open orders…")
        self.api.cancel_all()
        self.active_orders.clear()

    def place_grid(self, mid_price: float, balance_usd: float):
        grid = self.compute_grid(mid_price, balance_usd)
        self.log.info("[Grid] Placing %d orders around $%.2f (spacing %.2f%%)",
                      len(grid), mid_price, self.spacing_pct * 100)
        for order in grid:
            result = self.api.add_order(self.pair, order["side"], order["volume"], order["price"])
            if result and "txid" in result:
                for txid in result["txid"]:
                    self.active_orders[txid] = order
                    self.log.info("  → %s %s ETH @ $%s  [%s]",
                                  order["side"], order["volume"], order["price"], txid)
            else:
                self.log.warning("  ✗ Failed to place %s @ $%s", order["side"], order["price"])
            time.sleep(1)

    def check_and_refresh(self, mid_price: float, balance_usd: float) -> bool:
        """Returns True if grid was refreshed."""
        if not self.active_orders:
            self.place_grid(mid_price, balance_usd)
            return True

        result = self.api.open_orders()
        current_open = set(result.get("open", {}).keys()) if result else set()
        tracked = set(self.active_orders.keys())
        filled = tracked - current_open

        if filled:
            self.log.info("[Grid] Orders filled: %s", ", ".join(filled))
            for txid in filled:
                info = self.active_orders.pop(txid, {})
                self.log.info("  Filled: %s %s ETH @ $%s",
                              info.get("side"), info.get("volume"), info.get("price"))
            self.cancel_all_orders()
            self.place_grid(mid_price, balance_usd)
            return True

        return False


# ---------------------------------------------------------------------------
# Dip Buying Strategy
# ---------------------------------------------------------------------------

class DipBuyStrategy:
    """
    Dip Buying: when price drops ≥ dip_threshold_pct (default 3%) from the
    recent 5-min high, market-buy with dip_buy_pct (default 20%) of available
    balance, then place a take-profit limit sell at dip_tp_pct above entry.

    Cooldown prevents repeated buys on the same dip.
    """

    def __init__(self, api: KrakenAPI, pair: str, config: dict, logger: logging.Logger):
        self.api = api
        self.pair = pair
        self.log = logger
        # Config with defaults
        self.dip_threshold_pct = config.get("dip_threshold_pct", 3.0)
        self.dip_buy_pct = config.get("dip_buy_pct", 20.0) / 100.0  # fraction of balance
        self.dip_tp_pct = config.get("dip_tp_pct", 1.75) / 100.0    # take-profit above entry
        self.cooldown_sec = config.get("dip_cooldown_sec", 300)       # 5 min cooldown
        self.last_buy_time = 0.0
        # Track take-profit orders placed by this strategy
        self.tp_orders: dict[str, dict] = {}  # txid -> {price, volume}

    def evaluate(self, current_price: float, tracker: PriceTracker, available_usd: float):
        """
        Check if a dip has occurred and execute a buy if conditions are met.
        Returns the USD amount committed (0 if no action).
        """
        # Cooldown check
        if time.time() - self.last_buy_time < self.cooldown_sec:
            return 0.0

        # Need price history to compare against
        recent_high = tracker.recent_high(lookback_sec=300)  # 5 min
        if recent_high is None:
            return 0.0

        # Calculate drop percentage from recent high
        drop_pct = (recent_high - current_price) / recent_high * 100.0
        if drop_pct < self.dip_threshold_pct:
            return 0.0

        # --- DIP DETECTED ---
        self.log.info("[Dip] 🔻 Price dropped %.2f%% from recent high $%.2f → $%.2f",
                      drop_pct, recent_high, current_price)

        # Calculate position size
        buy_usd = available_usd * self.dip_buy_pct
        if buy_usd < 5.0:  # Kraken minimum ~$5 equivalent
            self.log.warning("[Dip] Available balance too low ($%.2f) for dip buy", available_usd)
            return 0.0

        volume = buy_usd / current_price

        # Place market buy
        result = self.api.add_market_order(self.pair, "buy", f"{volume:.6f}")
        if not result or "txid" not in result:
            self.log.warning("[Dip] Market buy failed")
            return 0.0

        self.last_buy_time = time.time()
        self.log.info("[Dip] ✅ Market bought %.6f ETH (~$%.2f)", volume, buy_usd)

        # Store entry price for RL tracking
        self._last_entry_price = current_price

        # Place take-profit limit sell
        tp_price = current_price * (1 + self.dip_tp_pct)
        tp_result = self.api.add_order(
            self.pair, "sell", f"{volume:.6f}", f"{tp_price:.2f}",
            ordertype="limit", oflags="post"
        )
        if tp_result and "txid" in tp_result:
            for txid in tp_result["txid"]:
                self.tp_orders[txid] = {
                    "price": f"{tp_price:.2f}",
                    "volume": f"{volume:.6f}",
                    "entry_price": current_price,
                }
                self.log.info("[Dip] 🎯 Take-profit sell placed @ $%.2f  [%s]", tp_price, txid)
        else:
            self.log.warning("[Dip] Failed to place take-profit sell")

        return buy_usd

    def check_tp_fills(self) -> list[dict]:
        """Check if any TP orders filled. Returns list of {entry_price, exit_price}."""
        if not self.tp_orders:
            return []

        result = self.api.open_orders()
        current_open = set(result.get("open", {}).keys()) if result else set()
        filled = set(self.tp_orders.keys()) - current_open
        trades = []
        for txid in filled:
            info = self.tp_orders.pop(txid)
            entry = info.get("entry_price", 0.0)
            exit_p = float(info.get("price", 0.0))
            if entry and exit_p:
                trades.append({"entry_price": entry, "exit_price": exit_p})
                self.log.info("[Dip] ✅ TP filled: entry=$%.2f exit=$%.2f", entry, exit_p)
        return trades


# ---------------------------------------------------------------------------
# Scalping Strategy
# ---------------------------------------------------------------------------

class ScalpStrategy:
    """
    Scalping: detect short-term volatility (≥ scalp_vol_pct swing in last
    1-2 min), place a limit buy slightly below current price, with a tight
    take-profit sell. Auto-cancel unfilled orders after scalp_timeout_sec.

    Position size: scalp_size_pct of available balance per scalp.
    """

    def __init__(self, api: KrakenAPI, pair: str, config: dict, logger: logging.Logger):
        self.api = api
        self.pair = pair
        self.log = logger
        # Config
        self.vol_threshold_pct = config.get("scalp_vol_pct", 0.5)
        self.vol_lookback_sec = config.get("scalp_vol_lookback_sec", 120)  # 2 min
        self.buy_offset_pct = config.get("scalp_buy_offset_pct", 0.15) / 100.0  # buy slightly below
        self.tp_pct = config.get("scalp_tp_pct", 0.75) / 100.0  # take-profit above entry
        self.size_pct = config.get("scalp_size_pct", 10.0) / 100.0
        self.timeout_sec = config.get("scalp_timeout_sec", 120)  # auto-cancel after 2 min
        self.cooldown_sec = config.get("scalp_cooldown_sec", 180)  # 3 min between scalps
        self.last_scalp_time = 0.0
        # Track pending scalp buy orders: {txid: {price, volume, placed_at}}
        self.pending_buys: dict[str, dict] = {}
        # Track take-profit sells: {txid: {price, volume}}
        self.tp_orders: dict[str, dict] = {}

    def evaluate(self, current_price: float, tracker: PriceTracker, available_usd: float):
        """
        Check volatility and place a scalp if conditions met.
        Also handles auto-cancellation of stale orders.
        Returns USD committed (0 if no action).
        """
        # --- Auto-cancel stale scalp buy orders ---
        self._cancel_stale_orders()

        # Cooldown
        if time.time() - self.last_scalp_time < self.cooldown_sec:
            return 0.0

        # Check volatility
        vol = tracker.volatility(lookback_sec=self.vol_lookback_sec)
        if vol is None or vol < self.vol_threshold_pct:
            return 0.0

        # --- VOLATILITY DETECTED ---
        self.log.info("[Scalp] ⚡ Volatility %.2f%% detected (threshold %.2f%%)",
                      vol, self.vol_threshold_pct)

        # Position sizing
        buy_usd = available_usd * self.size_pct
        if buy_usd < 5.0:
            self.log.warning("[Scalp] Available balance too low ($%.2f)", available_usd)
            return 0.0

        # Place limit buy slightly below current price
        buy_price = current_price * (1 - self.buy_offset_pct)
        volume = buy_usd / buy_price

        result = self.api.add_order(
            self.pair, "buy", f"{volume:.6f}", f"{buy_price:.2f}",
            ordertype="limit", oflags="post"
        )
        if not result or "txid" not in result:
            self.log.warning("[Scalp] Limit buy failed")
            return 0.0

        self.last_scalp_time = time.time()
        for txid in result["txid"]:
            self.pending_buys[txid] = {
                "price": buy_price,
                "volume": volume,
                "placed_at": time.time(),
            }
            self.log.info("[Scalp] 📥 Limit buy %.6f ETH @ $%.2f  [%s]", volume, buy_price, txid)

        return buy_usd

    def check_fills_and_place_tp(self):
        """
        Check if any pending scalp buys have been filled.
        If filled, place a tight take-profit sell.
        """
        if not self.pending_buys:
            return

        result = self.api.open_orders()
        current_open = set(result.get("open", {}).keys()) if result else set()

        filled = set(self.pending_buys.keys()) - current_open
        for txid in filled:
            info = self.pending_buys.pop(txid)
            entry_price = info["price"]
            volume = info["volume"]
            self.log.info("[Scalp] ✅ Buy filled: %.6f ETH @ $%.2f  [%s]",
                          volume, entry_price, txid)

            # Place take-profit sell
            tp_price = entry_price * (1 + self.tp_pct)
            tp_result = self.api.add_order(
                self.pair, "sell", f"{volume:.6f}", f"{tp_price:.2f}",
                ordertype="limit", oflags="post"
            )
            if tp_result and "txid" in tp_result:
                for tp_txid in tp_result["txid"]:
                    self.tp_orders[tp_txid] = {
                        "price": f"{tp_price:.2f}",
                        "volume": f"{volume:.6f}",
                        "entry_price": entry_price,
                    }
                    self.log.info("[Scalp] 🎯 Take-profit sell @ $%.2f  [%s]", tp_price, tp_txid)
            else:
                self.log.warning("[Scalp] Failed to place take-profit sell for %s", txid)

    def check_tp_fills(self) -> list[dict]:
        """Check if any scalp TP orders filled. Returns list of {entry_price, exit_price}."""
        if not self.tp_orders:
            return []

        result = self.api.open_orders()
        current_open = set(result.get("open", {}).keys()) if result else set()
        filled = set(self.tp_orders.keys()) - current_open
        trades = []
        for txid in filled:
            info = self.tp_orders.pop(txid)
            entry = info.get("entry_price", 0.0)
            exit_p = float(info.get("price", 0.0))
            if entry and exit_p:
                trades.append({"entry_price": entry, "exit_price": exit_p})
                self.log.info("[Scalp] ✅ TP filled: entry=$%.2f exit=$%.2f", entry, exit_p)
        return trades

    def _cancel_stale_orders(self):
        """Cancel scalp buy orders older than timeout_sec that haven't filled."""
        now = time.time()
        stale = [txid for txid, info in self.pending_buys.items()
                 if now - info["placed_at"] > self.timeout_sec]

        for txid in stale:
            self.log.info("[Scalp] ⏰ Cancelling stale order [%s] (unfilled after %ds)",
                          txid, self.timeout_sec)
            self.api.cancel_order(txid)
            self.pending_buys.pop(txid, None)


# ---------------------------------------------------------------------------
# Market Analyzer — detects market regime from 24h OHLC data
# ---------------------------------------------------------------------------

class MarketAnalyzer:
    """
    Fetches 24h OHLC data from Kraken's public API and determines the
    current market regime: "downtrend", "uptrend", or "sideways".

    Uses a simple moving average over the configured period and checks
    price change percentage over the last 24h.
    """

    def __init__(self, api: KrakenAPI, pair: str, config: dict, logger: logging.Logger):
        self.api = api
        self.pair = pair
        self.log = logger
        # ma_period in minutes (default 1440 = 24h); used for SMA calculation
        self.ma_period = config.get("adaptive_ma_period", 1440)
        self.trend_up_pct = config.get("adaptive_trend_threshold_up", 3.0)
        self.trend_down_pct = config.get("adaptive_trend_threshold_down", 5.0)
        # Cache OHLC data to avoid excessive API calls
        self._last_fetch_time = 0.0
        self._cache_ttl = 300  # refresh OHLC every 5 min
        self._cached_regime: Optional[str] = None
        self._cached_sma: Optional[float] = None
        self._cached_change_pct: Optional[float] = None

    def _fetch_ohlc(self) -> list:
        """Fetch 1-hour OHLC candles for the last ~24h from Kraken public API."""
        since = int(time.time()) - 86400  # 24h ago
        result = self.api._public("OHLC", {"pair": self.pair, "interval": 60, "since": since})
        # Result keys vary; pick the first non-"last" key
        for key, val in result.items():
            if key != "last" and isinstance(val, list):
                return val
        return []

    def analyze(self, current_price: float) -> str:
        """
        Determine market regime based on 24h OHLC data.
        Returns "uptrend", "downtrend", or "sideways".
        """
        now = time.time()
        if now - self._last_fetch_time < self._cache_ttl and self._cached_regime is not None:
            return self._cached_regime

        try:
            candles = self._fetch_ohlc()
        except Exception as exc:
            self.log.warning("[MarketAnalyzer] Failed to fetch OHLC: %s", exc)
            return self._cached_regime or "sideways"

        if len(candles) < 2:
            self.log.warning("[MarketAnalyzer] Insufficient OHLC data (%d candles)", len(candles))
            return self._cached_regime or "sideways"

        # Each candle: [time, open, high, low, close, vwap, volume, count]
        closes = [float(c[4]) for c in candles]

        # 24h simple moving average
        sma = sum(closes) / len(closes)
        self._cached_sma = sma

        # 24h price change: earliest close vs current price
        earliest_close = closes[0]
        if earliest_close == 0:
            earliest_close = closes[1] if len(closes) > 1 else current_price
        change_pct = ((current_price - earliest_close) / earliest_close) * 100.0
        self._cached_change_pct = change_pct

        # Determine regime
        above_sma = current_price > sma
        below_sma = current_price < sma
        near_sma = abs(current_price - sma) / sma * 100.0 < self.trend_up_pct  # within threshold

        if below_sma and change_pct <= -self.trend_down_pct:
            regime = "downtrend"
        elif above_sma and change_pct >= self.trend_up_pct:
            regime = "uptrend"
        else:
            regime = "sideways"

        self._last_fetch_time = now
        self._cached_regime = regime
        self.log.info("[MarketAnalyzer] SMA=$%.2f | 24h change=%.2f%% | Regime=%s",
                      sma, change_pct, regime)
        return regime


# ---------------------------------------------------------------------------
# Adaptive Strategy — switches active strategies based on market regime
# ---------------------------------------------------------------------------

class AdaptiveStrategy:
    """
    Wraps Grid, DipBuy, and Scalp strategies. Uses MarketAnalyzer to detect
    the current regime and activates/deactivates strategies accordingly.

    Regime-to-strategy mapping:
      - downtrend → DIP + SCALP
      - uptrend   → GRID + DIP + SCALP
      - sideways  → GRID only

    Smooth transitions: regime must persist for `confirmation_cycles` consecutive
    cycles before the bot actually switches strategies (prevents flip-flopping).
    """

    # Regime → set of strategy names to enable
    REGIME_MAP = {
        "downtrend": {"dip", "scalp"},
        "uptrend":   {"grid", "dip", "scalp"},
        "sideways":  {"grid"},
    }

    def __init__(self, api: KrakenAPI, pair: str, config: dict, logger: logging.Logger):
        self.api = api
        self.pair = pair
        self.cfg = config
        self.log = logger
        self.confirmation_cycles = config.get("adaptive_regime_confirmation_cycles", 3)

        # Market analyzer
        self.analyzer = MarketAnalyzer(api, pair, config, logger)

        # Sentiment scanners
        self.polymarket = PolymarketScanner(config, logger)
        self.messari = MessariScanner(config, logger)
        self.fear_greed = NewsSentimentScanner(config, logger)
        self.market_data = CoinAPIScanner(config, logger)

        # Sentiment aggregator — combines all sources
        self.sentiment = SentimentAggregator(
            config, logger,
            polymarket=self.polymarket,
            messari=self.messari,
            fear_greed=self.fear_greed,
            market_data=self.market_data,
        )

        # Regime tracking for smooth transitions
        self._current_regime = "sideways"  # start conservative
        self._pending_regime: Optional[str] = None
        self._pending_count = 0

        # Strategy instances (all created, activated/deactivated dynamically)
        self.grid = GridStrategy(api, pair, config, logger)
        self.dip = DipBuyStrategy(api, pair, config, logger)
        self.scalp = ScalpStrategy(api, pair, config, logger)

        # Currently active strategy names
        self.active: set[str] = self.REGIME_MAP[self._current_regime].copy()
        self.log.info("[Adaptive] Starting in '%s' regime → strategies: %s",
                      self._current_regime, self.active)

    def get_sentiment_size_multiplier(self) -> float:
        """
        Return a position size multiplier based on aggregate sentiment.
        Strong bullish (>0.5) → up to 1.2x, strong bearish (<-0.5) → down to 0.8x.
        """
        sentiment = self.sentiment.get_aggregate_sentiment()
        if sentiment > 0.5:
            return 1.0 + 0.2 * min(sentiment, 1.0)
        elif sentiment < -0.5:
            return 1.0 + 0.2 * max(sentiment, -1.0)  # reduces size
        return 1.0

    def get_sentiment_tp_multiplier(self) -> float:
        """
        Return a take-profit tightening multiplier for bearish sentiment.
        Strong bearish → tighter TP (0.85x), else 1.0x.
        """
        sentiment = self.sentiment.get_aggregate_sentiment()
        if sentiment < -0.5:
            return 0.85
        return 1.0

    # Keep backward-compatible aliases
    def get_polymarket_size_multiplier(self) -> float:
        return self.get_sentiment_size_multiplier()

    def get_polymarket_tp_multiplier(self) -> float:
        return self.get_sentiment_tp_multiplier()

    def _update_regime(self, current_price: float):
        """Run market analysis and handle regime transitions with confirmation."""
        detected = self.analyzer.analyze(current_price)

        # Aggregate sentiment can influence regime detection:
        # If analyzer says sideways but sentiment is strongly directional, lean toward trend
        agg_sentiment = self.sentiment.get_aggregate_sentiment()
        self.log.info("[Sentiment] Aggregate: %+.3f", agg_sentiment)

        if detected == "sideways" and agg_sentiment > 0.5:
            self.log.info("[Adaptive] Aggregate sentiment strongly bullish (%+.3f) — overriding sideways → uptrend",
                          agg_sentiment)
            detected = "uptrend"
        elif detected == "sideways" and agg_sentiment < -0.5:
            self.log.info("[Adaptive] Aggregate sentiment strongly bearish (%+.3f) — overriding sideways → downtrend",
                          agg_sentiment)
            detected = "downtrend"

        if detected == self._current_regime:
            # Reset any pending transition
            self._pending_regime = None
            self._pending_count = 0
            return

        # Different regime detected — track confirmation
        if detected == self._pending_regime:
            self._pending_count += 1
        else:
            self._pending_regime = detected
            self._pending_count = 1

        self.log.info("[Adaptive] Detected '%s' (pending %d/%d, current='%s')",
                      detected, self._pending_count, self.confirmation_cycles,
                      self._current_regime)

        # Switch if confirmed
        if self._pending_count >= self.confirmation_cycles:
            old_regime = self._current_regime
            self._current_regime = detected
            self._pending_regime = None
            self._pending_count = 0
            new_active = self.REGIME_MAP[self._current_regime].copy()

            # Log the switch
            deactivated = self.active - new_active
            activated = new_active - self.active

            self.log.info("=" * 50)
            self.log.info("[Adaptive] 🔄 REGIME SWITCH: %s → %s", old_regime, self._current_regime)
            if deactivated:
                self.log.info("[Adaptive]   Deactivating: %s", deactivated)
            if activated:
                self.log.info("[Adaptive]   Activating: %s", activated)
            self.log.info("=" * 50)

            # If grid is being deactivated, cancel its orders
            if "grid" in deactivated:
                self.grid.cancel_all_orders()

            self.active = new_active

    def emergency_stop(self):
        """Cancel all orders across all strategies."""
        self.api.cancel_all()
        self.grid.active_orders.clear()
        self.scalp.pending_buys.clear()
        self.scalp.tp_orders.clear()
        self.dip.tp_orders.clear()


# ---------------------------------------------------------------------------
# Reinforcement Learning Layer
# ---------------------------------------------------------------------------

class ReinforcementLearner:
    """
    RL layer that sits on top of all strategies. Tracks trade outcomes,
    scores strategies via multi-armed bandit, auto-tunes parameters with
    Q-learning, and remembers winning patterns. Uses only stdlib + json.
    """

    # Parameter definitions: name → (min, max, 5 buckets)
    PARAM_DEFS = {
        "grid_spacing_pct":  (1.0, 4.0),
        "scalp_tp_pct":      (0.3, 2.0),
        "dip_threshold_pct": (1.5, 5.0),
        "position_size_pct": (5.0, 30.0),
    }
    NUM_BUCKETS = 5

    def __init__(self, config: dict, logger: logging.Logger):
        self.log = logger
        self.enabled = config.get("rl_enabled", False)
        self.lr = config.get("rl_learning_rate", 0.1)
        self.gamma = config.get("rl_discount_factor", 0.95)
        self.epsilon = config.get("rl_exploration_rate", 0.2)
        self.min_trades = config.get("rl_min_trades_before_tuning", 20)
        self.history_path = os.path.join(BASE_DIR, config.get("rl_history_file", "trade_history.json"))
        self.qtable_path = os.path.join(BASE_DIR, config.get("rl_qtable_file", "q_table.json"))
        self.max_history = 500

        # Strategy scores (EMA-based)
        self.strategy_scores = {"grid": 0.0, "dip": 0.0, "scalp": 0.0}
        self.strategy_wins = {"grid": 0, "dip": 0, "scalp": 0}
        self.strategy_trades = {"grid": 0, "dip": 0, "scalp": 0}
        self.strategy_total_pnl = {"grid": 0.0, "dip": 0.0, "scalp": 0.0}
        self.strategy_max_dd = {"grid": 0.0, "dip": 0.0, "scalp": 0.0}

        # EMA decay factor for strategy scoring
        self.ema_alpha = 0.1

        # Trade history
        self.trade_history: list[dict] = []

        # Q-table: param_name → list of 5 Q-values (one per bucket)
        self.q_table: dict[str, list[float]] = {}

        # Pattern memory: "regime|sentiment_bucket|vol_bucket|hour_bucket|dow" → {strategy: {wins, total}}
        self.patterns: dict[str, dict[str, dict[str, int]]] = {}

        # Load persisted state
        self._load_history()
        self._load_qtable()
        self._rebuild_stats()

        if self.enabled:
            self.log.info("[RL] Reinforcement learner initialized — %d historical trades loaded", len(self.trade_history))

    # -- Persistence --

    def _load_history(self):
        if os.path.exists(self.history_path):
            try:
                with open(self.history_path) as f:
                    self.trade_history = json.load(f)
                # Keep rolling window
                self.trade_history = self.trade_history[-self.max_history:]
            except (json.JSONDecodeError, IOError):
                self.trade_history = []

    def _save_history(self):
        self.trade_history = self.trade_history[-self.max_history:]
        try:
            with open(self.history_path, "w") as f:
                json.dump(self.trade_history, f)
        except IOError as e:
            self.log.warning("[RL] Failed to save history: %s", e)

    def _load_qtable(self):
        if os.path.exists(self.qtable_path):
            try:
                with open(self.qtable_path) as f:
                    data = json.load(f)
                self.q_table = data.get("q_table", {})
                self.patterns = data.get("patterns", {})
                self.strategy_scores = data.get("strategy_scores", self.strategy_scores)
            except (json.JSONDecodeError, IOError):
                pass
        # Ensure all params have Q-values
        for param in self.PARAM_DEFS:
            if param not in self.q_table:
                self.q_table[param] = [0.0] * self.NUM_BUCKETS

    def _save_qtable(self):
        try:
            with open(self.qtable_path, "w") as f:
                json.dump({
                    "q_table": self.q_table,
                    "patterns": self.patterns,
                    "strategy_scores": self.strategy_scores,
                }, f)
        except IOError as e:
            self.log.warning("[RL] Failed to save Q-table: %s", e)

    def _rebuild_stats(self):
        """Rebuild strategy stats from loaded history."""
        for strat in ("grid", "dip", "scalp"):
            self.strategy_wins[strat] = 0
            self.strategy_trades[strat] = 0
            self.strategy_total_pnl[strat] = 0.0
            self.strategy_max_dd[strat] = 0.0

        running_pnl = {"grid": 0.0, "dip": 0.0, "scalp": 0.0}
        peak_pnl = {"grid": 0.0, "dip": 0.0, "scalp": 0.0}

        for t in self.trade_history:
            strat = t.get("strategy", "grid")
            pnl = t.get("pnl", 0.0)
            self.strategy_trades[strat] = self.strategy_trades.get(strat, 0) + 1
            self.strategy_total_pnl[strat] = self.strategy_total_pnl.get(strat, 0.0) + pnl
            if pnl > 0:
                self.strategy_wins[strat] = self.strategy_wins.get(strat, 0) + 1
            running_pnl[strat] = running_pnl.get(strat, 0.0) + pnl
            peak_pnl[strat] = max(peak_pnl.get(strat, 0.0), running_pnl[strat])
            dd = peak_pnl[strat] - running_pnl[strat]
            self.strategy_max_dd[strat] = max(self.strategy_max_dd.get(strat, 0.0), dd)

        # Rebuild patterns
        self.patterns = {}
        for t in self.trade_history:
            key = self._pattern_key(t)
            strat = t.get("strategy", "grid")
            if key not in self.patterns:
                self.patterns[key] = {}
            if strat not in self.patterns[key]:
                self.patterns[key][strat] = {"wins": 0, "total": 0}
            self.patterns[key][strat]["total"] += 1
            if t.get("pnl", 0) > 0:
                self.patterns[key][strat]["wins"] += 1

    # -- Bucketing helpers --

    def _param_to_bucket(self, param: str, value: float) -> int:
        lo, hi = self.PARAM_DEFS[param]
        value = max(lo, min(hi, value))
        bucket = int((value - lo) / (hi - lo) * (self.NUM_BUCKETS - 1) + 0.5)
        return max(0, min(self.NUM_BUCKETS - 1, bucket))

    def _bucket_to_value(self, param: str, bucket: int) -> float:
        lo, hi = self.PARAM_DEFS[param]
        return lo + (hi - lo) * bucket / (self.NUM_BUCKETS - 1)

    def _sentiment_bucket(self, sentiment: float) -> str:
        if sentiment < -0.5:
            return "extreme_fear"
        elif sentiment < -0.15:
            return "fear"
        elif sentiment <= 0.15:
            return "neutral"
        elif sentiment <= 0.5:
            return "greed"
        else:
            return "extreme_greed"

    def _volatility_bucket(self, vol: float) -> str:
        if vol < 0.3:
            return "low"
        elif vol < 0.8:
            return "medium"
        else:
            return "high"

    def _hour_bucket(self, hour: int) -> str:
        if hour < 6:
            return "night"
        elif hour < 12:
            return "morning"
        elif hour < 18:
            return "afternoon"
        else:
            return "evening"

    def _pattern_key(self, trade: dict) -> str:
        regime = trade.get("regime", "sideways")
        sent = self._sentiment_bucket(trade.get("sentiment", 0.0))
        vol = self._volatility_bucket(trade.get("volatility", 0.5))
        hour = self._hour_bucket(trade.get("hour", 12))
        dow = trade.get("day_of_week", 0)
        return f"{regime}|{sent}|{vol}|{hour}|{dow}"

    # -- Trade recording --

    def record_trade(self, strategy: str, entry_price: float, exit_price: float,
                     regime: str, sentiment: float, volatility: float):
        """Record a completed trade and update all RL components."""
        if not self.enabled:
            return

        now = datetime.now()
        pnl = (exit_price - entry_price) / entry_price * 100.0  # % return

        trade = {
            "strategy": strategy,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": pnl,
            "regime": regime,
            "sentiment": sentiment,
            "volatility": volatility,
            "hour": now.hour,
            "day_of_week": now.weekday(),
            "timestamp": now.isoformat(),
        }

        self.trade_history.append(trade)
        self.trade_history = self.trade_history[-self.max_history:]

        # Update strategy stats
        self.strategy_trades[strategy] = self.strategy_trades.get(strategy, 0) + 1
        self.strategy_total_pnl[strategy] = self.strategy_total_pnl.get(strategy, 0.0) + pnl
        if pnl > 0:
            self.strategy_wins[strategy] = self.strategy_wins.get(strategy, 0) + 1

        # Update max drawdown tracking
        running = self.strategy_total_pnl[strategy]
        # Simple approximation: track peak and current
        if running < -self.strategy_max_dd.get(strategy, 0.0):
            self.strategy_max_dd[strategy] = abs(running)

        # Update EMA strategy score
        reward = 1.0 if pnl > 0 else -1.0
        old_score = self.strategy_scores.get(strategy, 0.0)
        self.strategy_scores[strategy] = old_score + self.ema_alpha * (reward - old_score)

        # Update pattern memory
        key = self._pattern_key(trade)
        if key not in self.patterns:
            self.patterns[key] = {}
        if strategy not in self.patterns[key]:
            self.patterns[key][strategy] = {"wins": 0, "total": 0}
        self.patterns[key][strategy]["total"] += 1
        if pnl > 0:
            self.patterns[key][strategy]["wins"] += 1

        # Update Q-table if enough trades
        total_trades = sum(self.strategy_trades.values())
        if total_trades >= self.min_trades:
            self._update_qtable(strategy, pnl)

        # Persist
        self._save_history()
        self._save_qtable()

        self.log.info("[RL] Recorded %s trade: entry=$%.2f exit=$%.2f pnl=%+.3f%%",
                      strategy, entry_price, exit_price, pnl)

    def _update_qtable(self, strategy: str, reward: float):
        """Update Q-values for all parameters based on trade reward."""
        for param in self.PARAM_DEFS:
            q_vals = self.q_table[param]
            # Find the bucket that was "active" for this trade
            # Use current best bucket as the state
            best_bucket = q_vals.index(max(q_vals))
            # Q-learning update
            max_future = max(q_vals)
            old_q = q_vals[best_bucket]
            q_vals[best_bucket] = old_q + self.lr * (reward + self.gamma * max_future - old_q)

    # -- Strategy selection (epsilon-greedy) --

    def select_strategy_weights(self) -> dict[str, float]:
        """
        Return capital allocation weights for each strategy.
        Uses epsilon-greedy: 80% exploit best, 20% explore.
        Returns weights that sum to 1.0.
        """
        if not self.enabled or sum(self.strategy_trades.values()) < self.min_trades:
            return {"grid": 0.34, "dip": 0.33, "scalp": 0.33}

        if random.random() < self.epsilon:
            # Explore: equal weights
            return {"grid": 0.34, "dip": 0.33, "scalp": 0.33}

        # Exploit: weight by score (shifted to positive)
        scores = dict(self.strategy_scores)
        min_score = min(scores.values())
        shifted = {k: v - min_score + 0.01 for k, v in scores.items()}
        total = sum(shifted.values())
        return {k: v / total for k, v in shifted.items()}

    # -- Parameter recommendations --

    def get_recommended_params(self) -> dict[str, float]:
        """Return recommended parameter values from Q-table."""
        if not self.enabled or sum(self.strategy_trades.values()) < self.min_trades:
            return {}

        recommended = {}
        for param, q_vals in self.q_table.items():
            best_bucket = q_vals.index(max(q_vals))
            recommended[param] = self._bucket_to_value(param, best_bucket)
        return recommended

    # -- Pattern-based position size adjustment --

    def get_pattern_multiplier(self, strategy: str, regime: str,
                               sentiment: float, volatility: float) -> float:
        """
        Look up current conditions in pattern memory and return a position
        size multiplier (0.5 to 1.5). Only adjusts if pattern has enough data.
        """
        if not self.enabled or sum(self.strategy_trades.values()) < self.min_trades:
            return 1.0

        now = datetime.now()
        trade_proxy = {
            "regime": regime,
            "sentiment": sentiment,
            "volatility": volatility,
            "hour": now.hour,
            "day_of_week": now.weekday(),
        }
        key = self._pattern_key(trade_proxy)

        if key not in self.patterns or strategy not in self.patterns[key]:
            return 1.0

        stats = self.patterns[key][strategy]
        if stats["total"] < 5:  # need minimum sample
            return 1.0

        win_rate = stats["wins"] / stats["total"]

        # Map win rate to multiplier: 50% → 1.0, 75% → 1.25, 25% → 0.75
        multiplier = 0.5 + win_rate  # range [0.5, 1.5]
        return max(0.5, min(1.5, multiplier))

    # -- Performance dashboard --

    # -- Accessor methods for MemoryPruner --

    def get_trade_history(self) -> list:
        return self.trade_history

    def set_trade_history(self, history: list):
        self.trade_history = history
        self._save_history()
        self._rebuild_stats()

    def get_patterns(self) -> dict:
        return self.patterns

    def set_patterns(self, patterns: dict):
        self.patterns = patterns
        self._save_qtable()

    def get_qtable(self) -> dict:
        return self.q_table

    def set_qtable(self, qtable: dict):
        self.q_table = qtable
        self._save_qtable()

    def log_dashboard(self):
        """Log RL performance dashboard."""
        if not self.enabled:
            return

        total = sum(self.strategy_trades.values())
        total_pnl = sum(self.strategy_total_pnl.values())

        self.log.info("=" * 60)
        self.log.info("[RL] 📊 REINFORCEMENT LEARNING DASHBOARD")
        self.log.info("-" * 60)
        self.log.info("[RL] Total trades: %d | Total PnL: %+.3f%%", total, total_pnl)

        # Sharpe-like ratio (mean return / std dev of returns)
        if len(self.trade_history) > 1:
            returns = [t.get("pnl", 0.0) for t in self.trade_history]
            mean_ret = sum(returns) / len(returns)
            variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
            std_ret = math.sqrt(variance) if variance > 0 else 0.001
            sharpe = mean_ret / std_ret
            self.log.info("[RL] Sharpe-like ratio: %.3f", sharpe)

        # Strategy win rates
        self.log.info("-" * 60)
        for strat in ("grid", "dip", "scalp"):
            t = self.strategy_trades[strat]
            if t > 0:
                wr = self.strategy_wins[strat] / t * 100
                avg = self.strategy_total_pnl[strat] / t
                self.log.info("[RL] %s: %d trades | Win rate: %.1f%% | Avg PnL: %+.3f%% | Score: %+.3f",
                              strat.upper(), t, wr, avg, self.strategy_scores[strat])

        # Q-table preferred parameters
        recommended = self.get_recommended_params()
        if recommended:
            self.log.info("-" * 60)
            self.log.info("[RL] Q-table preferred parameters:")
            for param, val in recommended.items():
                q_vals = self.q_table[param]
                self.log.info("[RL]   %s = %.2f (Q-values: %s)",
                              param, val, [f"{v:.3f}" for v in q_vals])

        # Top 3 winning patterns
        winning_patterns = []
        for key, strats in self.patterns.items():
            for strat, stats in strats.items():
                if stats["total"] >= 5:
                    wr = stats["wins"] / stats["total"]
                    winning_patterns.append((key, strat, wr, stats["total"]))

        winning_patterns.sort(key=lambda x: x[2], reverse=True)
        if winning_patterns:
            self.log.info("-" * 60)
            self.log.info("[RL] Top winning patterns:")
            for key, strat, wr, n in winning_patterns[:3]:
                self.log.info("[RL]   %s + %s → %.0f%% win rate (%d trades)",
                              key, strat, wr * 100, n)

        self.log.info("=" * 60)


# ---------------------------------------------------------------------------
# Memory Pruning / Garbage Collection
# ---------------------------------------------------------------------------

PRUNE_LOG_PATH = os.path.join(BASE_DIR, "prune_log.json")


class MemoryPruner:
    """
    Garbage collector for bot memory files. Prunes trade history, Q-table,
    and pattern memory to keep file sizes manageable and data quality high.
    """

    def __init__(self, config: dict, logger: logging.Logger, rl: 'ReinforcementLearner'):
        self.cfg = config
        self.log = logger
        self.rl = rl
        self.enabled = config.get("pruning_enabled", True)
        self.max_trades = config.get("pruning_max_trades", 500)
        self.max_file_mb = config.get("pruning_max_file_mb", 5)
        self.cycle_interval = config.get("pruning_cycle_interval", 50)
        self.keep_recent_days = config.get("pruning_keep_recent_days", 7)
        self.pattern_min_occurrences = config.get("pruning_pattern_min_occurrences", 5)
        self.qtable_stale_days = config.get("pruning_qtable_stale_days", 30)

        # Track Q-table access metadata: key -> {last_accessed, access_count, stale_cycle}
        self._qtable_meta: dict[str, dict] = {}
        self._cycle_count = 0

    # -- File size monitoring --

    def _file_size_mb(self, path: str) -> float:
        try:
            return os.path.getsize(path) / (1024 * 1024) if os.path.exists(path) else 0.0
        except OSError:
            return 0.0

    def check_file_sizes(self) -> list[str]:
        """Check file sizes and return list of files exceeding limit."""
        oversized = []
        files = {
            "trade_history.json": self.rl.history_path,
            "q_table.json": self.rl.qtable_path,
            "bot_state.json": os.path.join(BASE_DIR, "bot_state.json"),
        }
        for name, path in files.items():
            size = self._file_size_mb(path)
            if size > self.max_file_mb:
                self.log.warning("[Pruner] File %s exceeds size limit (%.2f MB). Running emergency prune.", name, size)
                oversized.append(name)
        return oversized

    # -- Trade history pruning --

    def _score_trade(self, trade: dict, now_ts: float) -> float:
        """Score a trade for retention. Higher = more valuable to keep."""
        pnl = trade.get("pnl", 0.0)
        ts_str = trade.get("timestamp", "")
        try:
            trade_ts = datetime.fromisoformat(ts_str).timestamp()
        except (ValueError, TypeError):
            trade_ts = 0.0

        age_days = (now_ts - trade_ts) / 86400.0 if trade_ts else 999.0
        sentiment = abs(trade.get("sentiment", 0.0))
        volatility = trade.get("volatility", 0.0)
        regime = trade.get("regime", "sideways")

        score = 0.0

        # Recent trades are very valuable
        if age_days < self.keep_recent_days:
            score += 1000.0  # never delete

        # Recency bonus (exponential decay)
        score += max(0, 100.0 - age_days)

        # Winners are always valuable
        if pnl > 0:
            score += 500.0 + pnl * 10.0

        # Large losses are important lessons
        if pnl < -2.0:
            score += 300.0 + abs(pnl) * 5.0

        # Unusual conditions are rare and valuable
        unusual = False
        if sentiment > 0.5:
            score += 200.0
            unusual = True
        if volatility > 0.8:
            score += 150.0
            unusual = True
        if regime in ("uptrend", "downtrend"):
            score += 100.0
            unusual = True

        # Small losses in normal conditions = low value
        if -2.0 <= pnl <= 0 and not unusual:
            score -= 50.0

        # Breakeven in normal conditions = low value
        if -0.1 <= pnl <= 0.1 and not unusual:
            score -= 80.0

        # Old losing trades with common patterns
        if age_days > 60 and pnl < 0 and not unusual:
            score -= 100.0

        return score

    def prune_trade_history(self) -> dict:
        """Prune trade history, removing bottom 20% by score. Returns stats."""
        history = self.rl.get_trade_history()
        before_count = len(history)

        if before_count <= self.max_trades * 0.8:
            return {"pruned": 0, "kept": before_count}

        now_ts = time.time()
        scored = [(self._score_trade(t, now_ts), i, t) for i, t in enumerate(history)]
        scored.sort(key=lambda x: x[0])

        # Remove bottom 20%
        remove_count = max(1, int(before_count * 0.2))
        to_remove = set()
        for score, idx, trade in scored[:remove_count]:
            # Never remove protected trades
            pnl = trade.get("pnl", 0.0)
            ts_str = trade.get("timestamp", "")
            try:
                trade_ts = datetime.fromisoformat(ts_str).timestamp()
            except (ValueError, TypeError):
                trade_ts = 0.0
            age_days = (now_ts - trade_ts) / 86400.0 if trade_ts else 999.0

            if age_days < self.keep_recent_days:
                continue
            if pnl > 0:
                continue
            if pnl < -2.0:
                continue
            if abs(trade.get("sentiment", 0.0)) > 0.5:
                continue
            if trade.get("volatility", 0.0) > 0.8:
                continue
            to_remove.add(idx)

        new_history = [t for i, t in enumerate(history) if i not in to_remove]
        self.rl.set_trade_history(new_history)

        after_count = len(new_history)
        winners = sum(1 for t in new_history if t.get("pnl", 0.0) > 0)
        lessons = sum(1 for t in new_history if t.get("pnl", 0.0) < -2.0)
        freed = before_count - after_count

        self.log.info("[Pruner] Pruned %d trades. Kept %d winners, %d lessons. Freed %d entries.",
                      freed, winners, lessons, freed)
        return {"pruned": freed, "kept": after_count, "winners": winners, "lessons": lessons}

    # -- Q-table pruning --

    def track_qtable_access(self, param: str):
        """Call this when a Q-table entry is accessed."""
        now = time.time()
        if param not in self._qtable_meta:
            self._qtable_meta[param] = {"last_accessed": now, "access_count": 0, "stale_cycle": 0}
        self._qtable_meta[param]["last_accessed"] = now
        self._qtable_meta[param]["access_count"] += 1

    def prune_qtable(self) -> dict:
        """Prune stale and uninformative Q-table entries. Returns stats."""
        qtable = self.rl.get_qtable()
        removed = 0
        reset = 0
        now = time.time()
        stale_threshold = self.qtable_stale_days * 86400

        to_delete = []
        for param, q_vals in qtable.items():
            meta = self._qtable_meta.get(param, {})
            last_accessed = meta.get("last_accessed", 0.0)
            access_count = meta.get("access_count", 0)
            stale_cycle = meta.get("stale_cycle", 0)

            # Check staleness: not accessed in N days AND low access count
            if now - last_accessed > stale_threshold and access_count < 5:
                if stale_cycle == 0:
                    # First cycle: decay by 0.5
                    qtable[param] = [v * 0.5 for v in q_vals]
                    self._qtable_meta.setdefault(param, {})["stale_cycle"] = 1
                    self.log.debug("[Pruner] Q-table entry '%s' marked stale, decaying.", param)
                else:
                    # Second cycle: mark for deletion (only non-core params)
                    if param not in self.rl.PARAM_DEFS:
                        to_delete.append(param)
                        removed += 1
                    else:
                        # Core params: reset to defaults instead of deleting
                        qtable[param] = [0.0] * self.rl.NUM_BUCKETS
                        reset += 1
                        if param in self._qtable_meta:
                            self._qtable_meta[param]["stale_cycle"] = 0
            else:
                # Not stale, reset stale counter
                if param in self._qtable_meta:
                    self._qtable_meta[param]["stale_cycle"] = 0

            # Check for uninformative entries (low variance = hasn't learned)
            if len(q_vals) > 1:
                mean_q = sum(q_vals) / len(q_vals)
                variance = sum((v - mean_q) ** 2 for v in q_vals) / len(q_vals)
                if variance < 0.001 and abs(mean_q) > 0.001:
                    # All buckets nearly equal — reset to defaults
                    qtable[param] = [0.0] * self.rl.NUM_BUCKETS
                    reset += 1

        for param in to_delete:
            del qtable[param]
            self._qtable_meta.pop(param, None)

        self.rl.set_qtable(qtable)
        self.log.info("[Pruner] Q-table pruned: removed %d stale entries, reset %d uninformative entries",
                      removed, reset)
        return {"removed": removed, "reset": reset}

    # -- Pattern memory pruning --

    def prune_patterns(self) -> dict:
        """Prune noise and stale patterns. Returns stats."""
        patterns = self.rl.get_patterns()
        total_trades = sum(self.rl.strategy_trades.values())
        now = time.time()
        noise_removed = 0
        stale_removed = 0
        kept = 0

        to_delete = []
        for key, strats in patterns.items():
            delete_key = True
            strats_to_delete = []

            for strat, stats in strats.items():
                total = stats.get("total", 0)
                wins = stats.get("wins", 0)
                last_seen = stats.get("last_seen", 0.0)
                win_rate = wins / total if total > 0 else 0.5

                should_delete = False

                # Noise: too few occurrences after enough total trades
                if total < self.pattern_min_occurrences and total_trades >= 100:
                    should_delete = True
                    noise_removed += 1

                # No edge: win rate 40-60% after 20+ occurrences
                elif total >= 20 and 0.40 <= win_rate <= 0.60:
                    should_delete = True
                    noise_removed += 1

                # Stale: no activity in 90 days
                elif last_seen > 0 and (now - last_seen) > 90 * 86400:
                    should_delete = True
                    stale_removed += 1

                # Strong patterns: keep
                elif win_rate > 0.65 or win_rate < 0.35 or total >= 20:
                    delete_key = False
                    kept += 1
                else:
                    delete_key = False
                    kept += 1

                if should_delete:
                    strats_to_delete.append(strat)
                else:
                    delete_key = False

            for strat in strats_to_delete:
                del strats[strat]

            if delete_key or not strats:
                to_delete.append(key)

        for key in to_delete:
            del patterns[key]

        self.rl.set_patterns(patterns)
        self.log.info("[Pruner] Pattern memory pruned: removed %d noise patterns, %d stale patterns. Kept %d high-value patterns.",
                      noise_removed, stale_removed, kept)
        return {"noise_removed": noise_removed, "stale_removed": stale_removed, "kept": kept}

    # -- Prune report --

    def _write_prune_report(self, trade_stats: dict, qtable_stats: dict, pattern_stats: dict,
                            before_sizes: dict, after_sizes: dict, before_trade_count: int):
        """Append a prune report to prune_log.json."""
        history = self.rl.get_trade_history()
        wins = sum(1 for t in history if t.get("pnl", 0.0) > 0)
        total = len(history)
        avg_win_rate = wins / total if total > 0 else 0.0

        report = {
            "timestamp": datetime.now().isoformat(),
            "trade_history": trade_stats,
            "qtable": qtable_stats,
            "patterns": pattern_stats,
            "before_sizes_mb": before_sizes,
            "after_sizes_mb": after_sizes,
            "before_trade_count": before_trade_count,
            "after_trade_count": total,
            "quality_score_avg_win_rate": round(avg_win_rate, 4),
        }

        # Append to log
        log_data = []
        if os.path.exists(PRUNE_LOG_PATH):
            try:
                with open(PRUNE_LOG_PATH) as f:
                    log_data = json.load(f)
            except (json.JSONDecodeError, IOError):
                log_data = []

        log_data.append(report)
        try:
            with open(PRUNE_LOG_PATH, "w") as f:
                json.dump(log_data, f, indent=2)
        except IOError as e:
            self.log.warning("[Pruner] Failed to write prune log: %s", e)

    def _get_file_sizes(self) -> dict:
        return {
            "trade_history.json": round(self._file_size_mb(self.rl.history_path), 4),
            "q_table.json": round(self._file_size_mb(self.rl.qtable_path), 4),
            "bot_state.json": round(self._file_size_mb(os.path.join(BASE_DIR, "bot_state.json")), 4),
        }

    # -- Main prune orchestration --

    def run_full_prune(self):
        """Run a full prune cycle across all memory stores."""
        if not self.enabled:
            return

        self.log.info("[Pruner] Running full memory prune...")
        before_sizes = self._get_file_sizes()
        before_trade_count = len(self.rl.get_trade_history())

        trade_stats = self.prune_trade_history()
        qtable_stats = self.prune_qtable()
        pattern_stats = self.prune_patterns()

        after_sizes = self._get_file_sizes()
        self._write_prune_report(trade_stats, qtable_stats, pattern_stats,
                                 before_sizes, after_sizes, before_trade_count)

    def lightweight_check(self) -> bool:
        """Quick check: should we run a full prune? Returns True if yes."""
        if not self.enabled:
            return False

        # Check trade count
        if len(self.rl.get_trade_history()) >= self.max_trades:
            return True

        # Check file sizes
        if self.check_file_sizes():
            return True

        return False

    def maybe_prune(self, force: bool = False):
        """Called each cycle. Runs prune if conditions are met."""
        if not self.enabled:
            return

        self._cycle_count += 1

        if force or self.cfg.get("pruning_force_next", False):
            self.run_full_prune()
            return

        # Lightweight check every N cycles
        if self._cycle_count % self.cycle_interval == 0:
            if self.lightweight_check():
                self.run_full_prune()


# ---------------------------------------------------------------------------
# Warm-Up / Catch-Up Engine
# ---------------------------------------------------------------------------

BOT_STATE_PATH = os.path.join(BASE_DIR, "bot_state.json")


class WarmupEngine:
    """
    Detects how long the bot has been offline and runs a catch-up sequence:
      1. Staleness detection from bot_state.json / trade_history.json
      2. Historical OHLC catch-up (rebuilds MarketAnalyzer state)
      3. Simulated backtest on missed period (feeds RL system)
      4. Sentiment recalibration (compares old vs current)
      5. Q-table decay for prolonged offline periods
      6. Gradual ramp-up of position sizes after warm-up
      7. State persistence every cycle
    """

    def __init__(self, config: dict, logger: logging.Logger, api: KrakenAPI,
                 pair: str, rl: 'ReinforcementLearner',
                 sentiment_agg: Optional['SentimentAggregator'] = None,
                 analyzer: Optional['MarketAnalyzer'] = None):
        self.cfg = config
        self.log = logger
        self.api = api
        self.pair = pair
        self.rl = rl
        self.sentiment_agg = sentiment_agg
        self.analyzer = analyzer
        self.enabled = config.get("warmup_enabled", True)
        self.stale_hours = config.get("warmup_stale_hours", 24)
        self.rampup_hours = config.get("warmup_rampup_hours", 48)
        self.qtable_decay_30d = config.get("warmup_qtable_decay_30d", 0.8)
        self.qtable_decay_90d = config.get("warmup_qtable_decay_90d", 0.5)
        self.qtable_reset_days = config.get("warmup_qtable_reset_days", 180)

        # Ramp-up state
        self._warmup_completed_at: Optional[float] = None
        self._rampup_active = False

    # -- State persistence --

    def load_state(self) -> dict:
        """Load bot_state.json, return empty dict if missing."""
        if os.path.exists(BOT_STATE_PATH):
            try:
                with open(BOT_STATE_PATH) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {}

    def save_state(self, regime: str = "sideways", sentiment: float = 0.0,
                   position_size_pct: float = 100.0):
        """Save current bot state for staleness detection on next startup."""
        state = {
            "last_run_timestamp": time.time(),
            "last_run_iso": datetime.now().isoformat(),
            "regime": regime,
            "sentiment": sentiment,
            "position_size_pct": position_size_pct,
        }
        try:
            with open(BOT_STATE_PATH, "w") as f:
                json.dump(state, f, indent=2)
        except IOError as e:
            self.log.warning("[Warmup] Failed to save state: %s", e)

    # -- Staleness detection --

    def _get_last_active_timestamp(self) -> Optional[float]:
        """Get the most recent timestamp from bot_state.json or trade_history.json."""
        # Try bot_state.json first
        state = self.load_state()
        ts = state.get("last_run_timestamp")
        if ts:
            return float(ts)

        # Fall back to trade_history.json
        history_path = os.path.join(BASE_DIR, self.cfg.get("rl_history_file", "trade_history.json"))
        if os.path.exists(history_path):
            try:
                with open(history_path) as f:
                    trades = json.load(f)
                if trades:
                    last = trades[-1]
                    ts_str = last.get("timestamp", "")
                    if ts_str:
                        dt = datetime.fromisoformat(ts_str)
                        return dt.timestamp()
            except (json.JSONDecodeError, IOError, ValueError):
                pass
        return None

    def detect_staleness(self) -> Optional[float]:
        """
        Return hours offline, or None if not stale.
        """
        last_ts = self._get_last_active_timestamp()
        if last_ts is None:
            self.log.info("[Warmup] No previous state found — treating as fresh start")
            return None
        hours_offline = (time.time() - last_ts) / 3600.0
        if hours_offline >= self.stale_hours:
            return hours_offline
        return None

    # -- Historical OHLC catch-up --

    def _fetch_ohlc_candles(self, interval: int, since: int) -> list:
        """Fetch OHLC candles from Kraken. interval in minutes, since as unix timestamp."""
        try:
            result = self.api._public("OHLC", {
                "pair": self.pair,
                "interval": interval,
                "since": since,
            })
            for key, val in result.items():
                if key != "last" and isinstance(val, list):
                    return val
        except Exception as exc:
            self.log.warning("[Warmup] OHLC fetch failed (interval=%d): %s", interval, exc)
        return []

    def _catch_up_historical(self, hours_offline: float) -> dict:
        """
        Fetch OHLC candles covering the offline period.
        Returns dict with candles, regime summary, and rebuilt price data.
        """
        now = int(time.time())
        max_seconds = min(hours_offline * 3600, 180 * 86400)  # cap at 6 months
        start = int(now - max_seconds)
        two_weeks_ago = now - 14 * 86400

        all_candles = []

        # Daily candles for older period (> 2 weeks ago)
        if start < two_weeks_ago - 86400:
            self.log.info("[Warmup] Fetching daily candles for older period...")
            daily = self._fetch_ohlc_candles(1440, start)
            older = [c for c in daily if int(c[0]) < two_weeks_ago]
            all_candles.extend(older)
            self.log.info("[Warmup] Got %d daily candles", len(older))
            time.sleep(1)

        # 4h candles for 2-week transition zone
        four_h_start = max(start, two_weeks_ago - 86400)
        if four_h_start < two_weeks_ago:
            self.log.info("[Warmup] Fetching 4h candles for transition period...")
            four_h = self._fetch_ohlc_candles(240, four_h_start)
            transition = [c for c in four_h if int(c[0]) < two_weeks_ago]
            all_candles.extend(transition)
            self.log.info("[Warmup] Got %d 4h candles", len(transition))
            time.sleep(1)

        # 1h candles for recent period (last 2 weeks)
        self.log.info("[Warmup] Fetching 1h candles for recent period...")
        hourly = self._fetch_ohlc_candles(60, max(start, two_weeks_ago))
        all_candles.extend(hourly)
        self.log.info("[Warmup] Got %d 1h candles", len(hourly))

        # Sort by timestamp
        all_candles.sort(key=lambda c: int(c[0]))

        if not all_candles:
            self.log.warning("[Warmup] No historical candles retrieved")
            return {"candles": [], "regime_days": {}, "closes": []}

        # Analyze regime distribution over missed period
        # Each candle: [time, open, high, low, close, vwap, volume, count]
        closes = [float(c[4]) for c in all_candles]
        regime_days = {"bullish": 0, "bearish": 0, "sideways": 0}

        # Analyze in daily chunks
        chunk_size = max(1, len(closes) // max(1, int(hours_offline / 24)))
        for i in range(0, len(closes) - chunk_size, chunk_size):
            chunk = closes[i:i + chunk_size]
            if len(chunk) < 2:
                continue
            change_pct = (chunk[-1] - chunk[0]) / chunk[0] * 100 if chunk[0] else 0
            if change_pct >= 3.0:
                regime_days["bullish"] += 1
            elif change_pct <= -5.0:
                regime_days["bearish"] += 1
            else:
                regime_days["sideways"] += 1

        self.log.info("[Warmup] Regime during offline: bullish=%d days, bearish=%d days, sideways=%d days",
                      regime_days["bullish"], regime_days["bearish"], regime_days["sideways"])

        return {"candles": all_candles, "regime_days": regime_days, "closes": closes}

    def _rebuild_analyzer(self, closes: list):
        """Rebuild MarketAnalyzer's cached state from historical closes."""
        if not self.analyzer or not closes:
            return
        sma = sum(closes) / len(closes)
        self.analyzer._cached_sma = sma
        earliest = closes[0] if closes[0] != 0 else closes[1] if len(closes) > 1 else 1
        change_pct = (closes[-1] - earliest) / earliest * 100 if earliest else 0
        self.analyzer._cached_change_pct = change_pct

        if change_pct >= self.analyzer.trend_up_pct and closes[-1] > sma:
            self.analyzer._cached_regime = "uptrend"
        elif change_pct <= -self.analyzer.trend_down_pct and closes[-1] < sma:
            self.analyzer._cached_regime = "downtrend"
        else:
            self.analyzer._cached_regime = "sideways"

        self.analyzer._last_fetch_time = time.time()
        self.log.info("[Warmup] Rebuilt MarketAnalyzer: SMA=$%.2f, change=%.2f%%, regime=%s",
                      sma, change_pct, self.analyzer._cached_regime)

    # -- Simulated backtest --

    def _simulate_backtest(self, candles: list) -> dict:
        """
        Run bot strategies against historical candles in simulation mode.
        Feed outcomes into RL system. No real trades placed.
        """
        if not candles or len(candles) < 10:
            return {"trades": 0, "wins": 0, "win_rate": 0.0}

        trades = 0
        wins = 0
        closes = [float(c[4]) for c in candles]

        # Simulate dip-buy strategy
        window = 5  # look back 5 candles for recent high
        for i in range(window, len(closes)):
            recent_high = max(closes[i - window:i])
            current = closes[i]
            drop_pct = (recent_high - current) / recent_high * 100 if recent_high else 0

            dip_threshold = self.cfg.get("dip_threshold_pct", 3.0)
            dip_tp = self.cfg.get("dip_tp_pct", 1.75) / 100.0

            if drop_pct >= dip_threshold:
                # Simulate dip buy: look ahead for take-profit hit
                entry = current
                tp_target = entry * (1 + dip_tp)
                hit_tp = False
                for j in range(i + 1, min(i + 24, len(closes))):  # look 24 candles ahead
                    if closes[j] >= tp_target:
                        hit_tp = True
                        exit_price = tp_target
                        break
                if not hit_tp:
                    exit_price = closes[min(i + 24, len(closes) - 1)]

                pnl_pct = (exit_price - entry) / entry * 100
                is_win = pnl_pct > 0
                trades += 1
                if is_win:
                    wins += 1

                # Determine regime at this point
                local_sma = sum(closes[max(0, i - 24):i]) / min(24, i) if i > 0 else current
                local_change = (current - closes[max(0, i - 24)]) / closes[max(0, i - 24)] * 100 if closes[max(0, i - 24)] else 0
                if local_change >= 3.0:
                    sim_regime = "uptrend"
                elif local_change <= -5.0:
                    sim_regime = "downtrend"
                else:
                    sim_regime = "sideways"

                # Feed into RL
                if self.rl and self.rl.enabled:
                    self.rl.record_trade("dip", entry, exit_price, sim_regime, 0.0,
                                         drop_pct / 100.0)

            # Simulate scalp strategy on volatility
            if i >= 2:
                lo = min(closes[i - 2:i + 1])
                hi = max(closes[i - 2:i + 1])
                vol = (hi - lo) / lo * 100 if lo else 0
                scalp_threshold = self.cfg.get("scalp_vol_pct", 0.5)
                scalp_tp = self.cfg.get("scalp_tp_pct", 0.75) / 100.0

                if vol >= scalp_threshold and i + 1 < len(closes):
                    entry = current * (1 - 0.0015)  # simulated offset
                    tp_target = entry * (1 + scalp_tp)
                    next_price = closes[min(i + 1, len(closes) - 1)]
                    exit_price = tp_target if next_price >= tp_target else next_price
                    pnl_pct = (exit_price - entry) / entry * 100
                    is_win = pnl_pct > 0
                    trades += 1
                    if is_win:
                        wins += 1

                    if self.rl and self.rl.enabled:
                        self.rl.record_trade("scalp", entry, exit_price, sim_regime if 'sim_regime' in dir() else "sideways",
                                             0.0, vol / 100.0)

        win_rate = (wins / trades * 100) if trades > 0 else 0.0
        self.log.info("[Warmup] Simulated %d trades over missed period. Win rate: %.1f%%. Updated RL parameters.",
                      trades, win_rate)
        return {"trades": trades, "wins": wins, "win_rate": win_rate}

    # -- Sentiment recalibration --

    def _recalibrate_sentiment(self) -> float:
        """Fetch fresh sentiment and compare to last known value."""
        old_state = self.load_state()
        old_sentiment = old_state.get("sentiment", 0.0)

        current_sentiment = 0.0
        if self.sentiment_agg:
            # Force fresh fetch by resetting caches
            for name, scanner in self.sentiment_agg.sources.items():
                scanner._cache_time = 0.0
            current_sentiment = self.sentiment_agg.get_aggregate_sentiment()

        self.log.info("[Warmup] Sentiment shifted from %+.3f to %+.3f while offline",
                      old_sentiment, current_sentiment)
        return current_sentiment

    # -- Q-table decay --

    def _apply_qtable_decay(self, hours_offline: float):
        """Apply decay to Q-table values based on offline duration."""
        if not self.rl or not self.rl.enabled:
            return

        days_offline = hours_offline / 24.0

        if days_offline >= self.qtable_reset_days:
            self.log.info("[Warmup] Offline > %d days (%.0f days). Resetting Q-table entirely.",
                          self.qtable_reset_days, days_offline)
            for param in self.rl.q_table:
                self.rl.q_table[param] = [0.0] * self.rl.NUM_BUCKETS
            self.rl.patterns = {}
            self.rl.strategy_scores = {"grid": 0.0, "dip": 0.0, "scalp": 0.0}
            self.rl._save_qtable()
            return

        if days_offline >= 90:
            decay = self.qtable_decay_90d
        elif days_offline >= 30:
            decay = self.qtable_decay_30d
        else:
            return  # No decay needed

        self.log.info("[Warmup] Offline %.0f days. Applying %.1fx decay to Q-table.", days_offline, decay)
        for param in self.rl.q_table:
            self.rl.q_table[param] = [v * decay for v in self.rl.q_table[param]]

        # Also decay pattern memory confidence
        for key in self.rl.patterns:
            for strat in self.rl.patterns[key]:
                # Reduce total count to reflect lower confidence
                self.rl.patterns[key][strat]["total"] = max(1, int(self.rl.patterns[key][strat]["total"] * decay))
                self.rl.patterns[key][strat]["wins"] = max(0, int(self.rl.patterns[key][strat]["wins"] * decay))

        # Decay strategy scores
        for strat in self.rl.strategy_scores:
            self.rl.strategy_scores[strat] *= decay

        self.rl._save_qtable()

    # -- Ramp-up multiplier --

    def get_rampup_multiplier(self) -> float:
        """
        Return position size multiplier based on time since warm-up completion.
        0-24h: 0.50, 24-48h: 0.75, 48h+: 1.0
        """
        if not self._rampup_active or self._warmup_completed_at is None:
            return 1.0

        hours_since = (time.time() - self._warmup_completed_at) / 3600.0

        if hours_since >= self.rampup_hours:
            self._rampup_active = False
            self.log.info("[Warmup] Ramp-up complete. Running at 100%% capacity.")
            return 1.0
        elif hours_since >= self.rampup_hours / 2.0:
            return 0.75
        else:
            return 0.50

    # -- Main warm-up sequence --

    def run_warmup(self, pruner: Optional['MemoryPruner'] = None) -> bool:
        """
        Run the full warm-up sequence. Returns True if warm-up was performed.
        Called once at startup before the main trading loop.
        """
        if not self.enabled:
            self.log.info("[Warmup] Warm-up disabled in config")
            return False

        hours_offline = self.detect_staleness()
        if hours_offline is None:
            self.log.info("[Warmup] Bot is fresh (offline < %dh). Skipping warm-up.", self.stale_hours)
            return False

        days_offline = hours_offline / 24.0
        self.log.info("=" * 60)
        self.log.info("[Warmup] Bot was offline for %.1f days. Running warm-up sequence...", days_offline)
        self.log.info("=" * 60)

        # Step 0: Memory pruning (clean house before restarting)
        if pruner:
            self.log.info("[Warmup] Step 0: Memory pruning before warm-up...")
            pruner.run_full_prune()

        # Step 1: Q-table decay (before we add new data)
        self._apply_qtable_decay(hours_offline)

        # Step 2: Historical data catch-up
        self.log.info("[Warmup] Step 2/5: Historical data catch-up...")
        hist = self._catch_up_historical(hours_offline)

        # Step 3: Rebuild analyzer from historical data
        if hist["closes"]:
            self._rebuild_analyzer(hist["closes"])

        # Step 4: Simulated backtest
        self.log.info("[Warmup] Step 3/5: Simulated backtest on missed period...")
        self._simulate_backtest(hist["candles"])

        # Step 5: Sentiment recalibration
        self.log.info("[Warmup] Step 4/5: Sentiment recalibration...")
        self._recalibrate_sentiment()

        # Step 6: Activate ramp-up
        self.log.info("[Warmup] Step 5/5: Activating gradual ramp-up...")
        self._warmup_completed_at = time.time()
        self._rampup_active = True

        mult = self.get_rampup_multiplier()
        remaining_hours = self.rampup_hours
        self.log.info("[Warmup] Warm-up complete. Running at %.0f%% capacity for next %.0f hours.",
                      mult * 100, remaining_hours)
        self.log.info("=" * 60)

        # Save fresh state
        regime = self.analyzer._cached_regime if self.analyzer and self.analyzer._cached_regime else "sideways"
        self.save_state(regime=regime, sentiment=0.0, position_size_pct=mult * 100)

        return True


# ---------------------------------------------------------------------------
# Main Bot — orchestrates all strategies
# ---------------------------------------------------------------------------

class TradingBot:
    """
    Multi-strategy trading bot. Runs enabled strategies each cycle while
    respecting overall balance limits and safety stops.
    """

    def __init__(self, config: dict, logger: logging.Logger):
        global _trade_rate_limiter
        self.cfg = config
        self.log = logger
        self.api = KrakenAPI(config["api_key"], config["api_secret"], logger)
        self.pair = config["pair"]
        self.stop_loss = config.get("stop_loss_equity", 60.0)
        self.take_profit = config.get("take_profit_equity", 500.0)
        self.stopped = False

        # Initialize trade rate limiter (max 1 trade/5s, 50/hour, daily loss limit)
        _trade_rate_limiter = TradeRateLimiter(config, logger)

        # Parse enabled strategies
        strategy_str = config.get("strategy", "grid")
        self.adaptive_mode = (strategy_str.strip().lower() == "adaptive")
        self.enabled = parse_strategies(strategy_str) if not self.adaptive_mode else {"adaptive"}
        self.log.info("Strategy mode: %s", "ADAPTIVE" if self.adaptive_mode else ", ".join(sorted(self.enabled)))

        # Price tracker (10 min rolling window)
        self.tracker = PriceTracker(window_sec=600)

        # Reinforcement learning layer
        self.rl = ReinforcementLearner(config, logger)
        self._cycle_count = 0

        if self.adaptive_mode:
            # Adaptive mode: single orchestrator handles everything
            self.adaptive = AdaptiveStrategy(self.api, self.pair, config, logger)
            self.grid = None
            self.dip = None
            self.scalp = None
        else:
            # Manual mode: individual strategies
            self.adaptive = None
            self.grid = GridStrategy(self.api, self.pair, config, logger) if "grid" in self.enabled else None
            self.dip = DipBuyStrategy(self.api, self.pair, config, logger) if "dip" in self.enabled else None
            self.scalp = ScalpStrategy(self.api, self.pair, config, logger) if "scalp" in self.enabled else None

        # Memory pruner
        self.pruner = MemoryPruner(config, logger, self.rl)

        # Warm-up engine
        sentiment_agg = self.adaptive.sentiment if self.adaptive else None
        analyzer = self.adaptive.analyzer if self.adaptive else None
        self.warmup = WarmupEngine(config, logger, self.api, self.pair, self.rl,
                                   sentiment_agg=sentiment_agg, analyzer=analyzer)

    # -- Helpers --

    def get_mid_price(self) -> float:
        tick = self.api.ticker(self.pair)
        info = next(iter(tick.values()))
        ask = float(info["a"][0])
        bid = float(info["b"][0])
        mid = (ask + bid) / 2.0
        self.log.debug("Ticker — bid=%.2f ask=%.2f mid=%.2f", bid, ask, mid)
        return mid

    def get_equity(self) -> float:
        tb = self.api.trade_balance(asset="ZUSD")
        if not tb:
            return 0.0
        equity = float(tb.get("eb", 0))
        self.log.debug("Account equity: $%.2f", equity)
        return equity

    # -- Main cycle --

    def run_cycle(self) -> bool:
        """
        Execute one full cycle across all enabled strategies.
        Returns False if the bot should stop.
        """
        # 1. Safety checks
        equity = self.get_equity()
        if equity <= 0:
            self.log.warning("Could not fetch equity; skipping cycle.")
            return True

        if equity <= self.stop_loss:
            self.log.critical("⛔ STOP-LOSS triggered! Equity $%.2f ≤ $%.2f", equity, self.stop_loss)
            self._emergency_stop()
            return False

        if equity >= self.take_profit:
            self.log.critical("🎯 TAKE-PROFIT reached! Equity $%.2f ≥ $%.2f", equity, self.take_profit)
            self._emergency_stop()
            return False

        # 2. Fetch current price and record it
        mid_price = self.get_mid_price()
        self.tracker.record(mid_price)
        self.log.info("Price: $%.2f | Equity: $%.2f | History: %d samples | RL trades: %d",
                      mid_price, equity, self.tracker.count(),
                      sum(self.rl.strategy_trades.values()))

        # 3. Calculate available balance (90% of equity for safety margin)
        total_available = equity * 0.90

        # --- Adaptive mode ---
        if self.adaptive_mode and self.adaptive:
            # Pass the tracker to the adaptive strategy's dip/scalp sub-strategies
            self.adaptive.dip.tracker_ref = self.tracker
            self.adaptive.scalp.tracker_ref = self.tracker
            # Override the None tracker in run_cycle calls
            self._run_adaptive_cycle(mid_price, equity, total_available)
            return True

        # --- Manual mode ---
        # Apply warm-up ramp-up multiplier
        rampup_mult = self.warmup.get_rampup_multiplier()
        if rampup_mult < 1.0:
            self.log.info("[Warmup] Ramp-up multiplier: %.0f%%", rampup_mult * 100)
            total_available *= rampup_mult

        committed = 0.0

        # --- Grid Strategy ---
        if self.grid:
            grid_alloc_pct = self.cfg.get("grid_alloc_pct", 60.0) / 100.0
            grid_budget = total_available * grid_alloc_pct
            self.grid.check_and_refresh(mid_price, grid_budget)
            committed += grid_budget

        # --- Scalp Strategy (check fills from previous cycle first) ---
        if self.scalp:
            self.scalp.check_fills_and_place_tp()

        remaining = total_available - committed

        # --- Dip Buy Strategy ---
        if self.dip and remaining > 5.0:
            used = self.dip.evaluate(mid_price, self.tracker, remaining)
            committed += used
            remaining -= used

        # --- Scalp Strategy (new entries) ---
        if self.scalp and remaining > 5.0:
            used = self.scalp.evaluate(mid_price, self.tracker, remaining)
            committed += used

        # Memory pruning check
        self.pruner.maybe_prune()

        # Save state for warm-up staleness detection
        self.warmup.save_state(regime="manual", sentiment=0.0,
                               position_size_pct=rampup_mult * 100)

        return True

    def _run_adaptive_cycle(self, mid_price: float, equity: float, total_available: float):
        """Run one adaptive cycle, passing tracker to sub-strategies."""
        adaptive = self.adaptive
        adaptive._update_regime(mid_price)

        # Get sentiment-based position size multiplier
        size_mult = adaptive.get_polymarket_size_multiplier()

        # Apply warm-up ramp-up multiplier on top
        rampup_mult = self.warmup.get_rampup_multiplier()
        size_mult *= rampup_mult
        if rampup_mult < 1.0:
            self.log.info("[Warmup] Ramp-up multiplier: %.0f%%", rampup_mult * 100)
        if size_mult != 1.0:
            self.log.info("[Sentiment] Position size multiplier: %.2fx", size_mult)

        # RL: get pattern-based multiplier for current conditions
        regime = adaptive._current_regime
        sentiment = adaptive.sentiment.get_aggregate_sentiment()
        vol = self.tracker.volatility(lookback_sec=120) or 0.5

        # RL: check for completed trades and record them
        if self.rl.enabled:
            dip_fills = adaptive.dip.check_tp_fills()
            for fill in dip_fills:
                self.rl.record_trade("dip", fill["entry_price"], fill["exit_price"],
                                     regime, sentiment, vol)
            scalp_fills = adaptive.scalp.check_tp_fills()
            for fill in scalp_fills:
                self.rl.record_trade("scalp", fill["entry_price"], fill["exit_price"],
                                     regime, sentiment, vol)

        # RL: get strategy capital allocation weights
        rl_weights = self.rl.select_strategy_weights()

        # RL: get recommended parameters and apply them (within safety limits)
        rl_params = self.rl.get_recommended_params()
        if rl_params:
            if "grid_spacing_pct" in rl_params:
                adaptive.grid.spacing_pct = rl_params["grid_spacing_pct"] / 100.0
            if "scalp_tp_pct" in rl_params:
                adaptive.scalp.tp_pct = rl_params["scalp_tp_pct"] / 100.0
            if "dip_threshold_pct" in rl_params:
                adaptive.dip.dip_threshold_pct = rl_params["dip_threshold_pct"]

        committed = 0.0

        # --- Grid Strategy ---
        if "grid" in adaptive.active:
            grid_alloc_pct = self.cfg.get("grid_alloc_pct", 60.0) / 100.0
            # RL adjusts capital allocation
            rl_grid_mult = rl_weights.get("grid", 0.34) / 0.34  # relative to baseline
            pattern_mult = self.rl.get_pattern_multiplier("grid", regime, sentiment, vol)
            grid_budget = total_available * grid_alloc_pct * size_mult * max(0.5, min(1.5, rl_grid_mult * pattern_mult))
            adaptive.grid.check_and_refresh(mid_price, grid_budget)
            committed += grid_budget

        # --- Scalp: check previous fills ---
        if "scalp" in adaptive.active:
            adaptive.scalp.check_fills_and_place_tp()

        remaining = (total_available - committed) * size_mult

        # --- Dip Buy ---
        if "dip" in adaptive.active and remaining > 5.0:
            pattern_mult = self.rl.get_pattern_multiplier("dip", regime, sentiment, vol)
            rl_dip_mult = rl_weights.get("dip", 0.33) / 0.33
            adjusted_remaining = remaining * max(0.5, min(1.5, rl_dip_mult * pattern_mult))
            used = adaptive.dip.evaluate(mid_price, self.tracker, adjusted_remaining)
            committed += used
            remaining -= used

        # --- Scalp: new entries ---
        if "scalp" in adaptive.active and remaining > 5.0:
            pattern_mult = self.rl.get_pattern_multiplier("scalp", regime, sentiment, vol)
            rl_scalp_mult = rl_weights.get("scalp", 0.33) / 0.33
            adjusted_remaining = remaining * max(0.5, min(1.5, rl_scalp_mult * pattern_mult))
            used = adaptive.scalp.evaluate(mid_price, self.tracker, adjusted_remaining)
            committed += used

        # RL dashboard every 10 cycles
        self._cycle_count += 1
        if self._cycle_count % 10 == 0:
            self.rl.log_dashboard()

        # Memory pruning check (end of cycle, no active trades in progress)
        self.pruner.maybe_prune()

        # Save bot state every cycle for warm-up staleness detection
        self.warmup.save_state(
            regime=regime,
            sentiment=sentiment,
            position_size_pct=rampup_mult * 100,
        )

    def _emergency_stop(self):
        """Cancel everything across all strategies."""
        self.log.info("Emergency stop — cancelling all orders…")
        if self.adaptive_mode and self.adaptive:
            self.adaptive.emergency_stop()
        else:
            self.api.cancel_all()
            if self.grid:
                self.grid.active_orders.clear()
            if self.scalp:
                self.scalp.pending_buys.clear()
                self.scalp.tp_orders.clear()
            if self.dip:
                self.dip.tp_orders.clear()
        self.stopped = True

    def run(self, once: bool = False):
        """Main loop."""
        self.log.info("=" * 60)
        self.log.info("Kraken Multi-Strategy Bot starting — pair=%s", self.pair)
        if self.adaptive_mode:
            self.log.info("Strategy: ADAPTIVE (auto-switching based on market regime)")
        else:
            self.log.info("Strategies: %s", " + ".join(sorted(self.enabled)))
        self.log.info("Stop-loss=$%.0f  Take-profit=$%.0f", self.stop_loss, self.take_profit)
        self.log.info("=" * 60)

        # Run warm-up sequence before main loop
        self.warmup.run_warmup(pruner=self.pruner)

        try:
            while True:
                try:
                    keep_going = self.run_cycle()
                    if not keep_going:
                        self.log.info("Bot stopped due to safety trigger.")
                        break
                except RuntimeError as exc:
                    self.log.error("Cycle error: %s", exc)
                except Exception as exc:
                    self.log.exception("Unexpected error in cycle: %s", exc)

                if once:
                    break

                interval = self.cfg.get("check_interval_sec", 60)
                self.log.debug("Sleeping %ds…", interval)
                time.sleep(interval)

        except KeyboardInterrupt:
            self.log.info("Interrupted — cancelling all orders…")
            self._emergency_stop()
            self.log.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    config = load_config()
    logger = setup_logging(config.get("log_file", "trades.log"))
    bot = TradingBot(config, logger)
    once = "--once" in sys.argv
    bot.run(once=once)


# --- Render free tier: tiny web server so it counts as a "Web Service" ---
from flask import Flask
import threading

web = Flask(__name__)

@web.route("/")
def health():
    return "Kraken trading bot is running!", 200

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    main()
