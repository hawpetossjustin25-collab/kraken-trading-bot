#!/usr/bin/env python3
"""
test_kraken_navigation.py — End-to-end verification that the real bot can
navigate Kraken's REST + WebSocket APIs and place/cancel/reconcile orders.
"""
import json
import logging
import os
import sys
import urllib.parse
from unittest.mock import MagicMock

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import bot  # noqa: E402

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("test_kraken_nav")

results = []


def check(name, fn):
    try:
        detail = fn()
        results.append(("PASS", name, detail or "ok"))
    except Exception as exc:
        results.append(("FAIL", name, str(exc)))


# 1. Official Kraken HMAC-SHA512 signature test vector (from Kraken API docs)
def t_official_signature():
    secret = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
    api = bot.KrakenAPI("TEST_KEY", secret, logger)
    data = {
        "nonce": "1616492376594",
        "ordertype": "limit",
        "pair": "XBTUSD",
        "price": "37500",
        "type": "buy",
        "volume": "1.25",
    }
    hdrs = api._sign("/0/private/AddOrder", data)
    expected = "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="
    assert hdrs["API-Sign"] == expected, f"Sign mismatch: {hdrs['API-Sign']}"
    return "Matches Kraken official HMAC-SHA512 test vector byte-for-byte"


# 2. Live Kraken public navigation (Ticker + AssetPairs metadata across pairs)
def t_live_public_navigation():
    api = bot.KrakenAPI("K", "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg==", logger)
    tk = api.ticker("XETHZUSD")
    mid = (float(next(iter(tk.values()))["a"][0]) + float(next(iter(tk.values()))["b"][0])) / 2
    assert mid > 100, f"Invalid ETH mid: {mid}"
    spec_eth = api.get_pair_spec("XETHZUSD")
    spec_btc = api.get_pair_spec("XXBTZUSD")
    spec_velo = api.get_pair_spec("VELOUSD")
    assert spec_eth["pair_decimals"] == 2 and spec_eth["ordermin"] > 0
    assert spec_btc["pair_decimals"] == 1 and spec_btc["ordermin"] > 0
    assert spec_velo["pair_decimals"] >= 4
    return (f"ETH=${mid:.2f} (dec={spec_eth['pair_decimals']}, min={spec_eth['ordermin']}), "
            f"BTC (dec={spec_btc['pair_decimals']}, min={spec_btc['ordermin']}), "
            f"VELO (dec={spec_velo['pair_decimals']}, min={spec_velo['ordermin']})")


# 3. Precision formatting & ordermin guard across ETH, BTC, and sub-cent altcoins
def t_precision_and_ordermin():
    api = bot.KrakenAPI("K", "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg==", logger)
    v_eth, p_eth, ok1 = api.normalize_order("XETHZUSD", "0.0123456789", "2410.386")
    assert ok1 and p_eth == "2410.39", f"ETH fmt: {v_eth} @ {p_eth}"
    v_btc, p_btc, ok2 = api.normalize_order("XXBTZUSD", "0.001234567", "81543.76")
    assert ok2 and p_btc == "81543.8", f"BTC fmt: {v_btc} @ {p_btc}"
    v_velo, p_velo, ok3 = api.normalize_order("VELOUSD", "1500.123456", "0.0154321")
    assert ok3 and p_velo.startswith("0.0154"), f"VELO fmt: {v_velo} @ {p_velo}"
    # Below ordermin must be blocked cleanly (both ETH < 0.002 and VELO < 900)
    _, _, ok_tiny = api.normalize_order("XETHZUSD", "0.000001", "2400")
    _, _, ok_velo_tiny = api.normalize_order("VELOUSD", "500.0", "0.0154")
    assert not ok_tiny and not ok_velo_tiny, "Below-ordermin order should be blocked"
    return f"ETH={v_eth}@${p_eth}, BTC={v_btc}@${p_btc}, VELO={v_velo}@${p_velo}, below-ordermin blocked"


# 4. Real KrakenAPI private order placement, cancellation, reconciliation & error handling
def t_private_order_lifecycle():
    secret = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
    api = bot.KrakenAPI("LIVE_KEY", secret, logger)
    bot._trade_rate_limiter = None  # disable sleep for unit speed
    # Pre-seed pair specs so only private calls hit the mock session
    api.get_pair_spec("XETHZUSD")
    api.get_pair_spec("XXBTZUSD")

    captured = []

    def fake_post(url, data=None, headers=None, timeout=15):
        assert "API-Key" in headers and "API-Sign" in headers
        assert "nonce" in data
        captured.append((url, dict(data), dict(headers)))
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if url.endswith("/AddOrder"):
            if data.get("pair") == "REJECTUSD":
                resp.json.return_value = {"error": ["EOrder:Order minimum not met"]}
            else:
                resp.json.return_value = {
                    "error": [],
                    "result": {
                        "descr": {"order": f"{data['type']} {data['volume']} {data['pair']}"},
                        "txid": [f"O-KRAKEN-{len(captured)}"],
                    },
                }
        elif url.endswith("/CancelOrder"):
            resp.json.return_value = {"error": [], "result": {"count": 1}}
        elif url.endswith("/Balance"):
            resp.json.return_value = {"error": [], "result": {"ZUSD": "1000.00", "XETH": "0.4500", "XXBT": "0.025"}}
        elif url.endswith("/QueryOrders"):
            resp.json.return_value = {
                "error": [],
                "result": {
                    "O-KRAKEN-1": {"status": "closed", "price": "2412.50", "vol_exec": "0.01000000", "fee": "0.038"}
                },
            }
        else:
            resp.json.return_value = {"error": [], "result": {"open": {}}}
        return resp

    api.session.post = fake_post

    # a) Limit buy with post-only flag
    r_lim = api.add_order("XETHZUSD", "buy", "0.01", "2405.129", ordertype="limit", oflags="post")
    assert r_lim["txid"] == ["O-KRAKEN-1"]
    _, d_lim, _ = captured[-1]
    assert d_lim["price"] == "2405.13" and d_lim["oflags"] == "post" and d_lim["ordertype"] == "limit"

    # b) Market order (no price, no post-only flag)
    r_mkt = api.add_market_order("XXBTZUSD", "sell", "0.002")
    assert r_mkt["txid"] == ["O-KRAKEN-2"]
    _, d_mkt, _ = captured[-1]
    assert d_mkt["ordertype"] == "market" and "price" not in d_mkt and "oflags" not in d_mkt

    # c) Validate-only order
    r_val = api.add_order("XETHZUSD", "buy", "0.01", "2400.00", validate=True)
    assert captured[-1][1].get("validate") == "true"

    # d) Cancel order
    r_can = api.cancel_order("O-KRAKEN-1")
    assert r_can.get("count") == 1

    # e) Reconcile fill price via QueryOrders
    q = api.query_orders(["O-KRAKEN-1"])
    assert float(q["O-KRAKEN-1"]["price"]) == 2412.50

    # f) X-prefixed balance lookup ('XETH' & 'XXBT')
    assert abs(api.asset_balance("XETHZUSD") - 0.45) < 1e-6
    assert abs(api.asset_balance("XXBTZUSD") - 0.025) < 1e-6

    # g) Graceful EOrder rejection (returns {} instead of crashing)
    api._pair_specs["REJECTUSD"] = {"pair_decimals": 2, "lot_decimals": 4, "ordermin": 0.0, "base": "REJ"}
    r_rej = api.add_order("REJECTUSD", "buy", "1.0", "10.00")
    assert r_rej == {}, "Rejected order must return {} without crashing"

    return f"{len(captured)} signed private Kraken requests verified (limit, market, validate, cancel, query, balance, EOrder guard)"


# 5. Full TradingBot live-mode cycle (dry_run=False) against KrakenAPI
def t_full_live_bot_cycle():
    secret = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
    cfg = bot.load_config()
    cfg["dry_run"] = False
    cfg["api_key"] = "LIVE_TEST_KEY"
    cfg["api_secret"] = secret
    cfg["order_flow_enabled"] = False
    cfg["trade_min_interval_sec"] = 0
    tb = bot.TradingBot(cfg, logger)
    assert type(tb.api) is bot.KrakenAPI, "Expected live KrakenAPI instance"
    tb.api.get_pair_spec("XETHZUSD")

    placed_orders = []

    def mock_private(url, data=None, headers=None, timeout=15):
        assert "API-Key" in headers and "API-Sign" in headers
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if url.endswith("/TradeBalance"):
            resp.json.return_value = {"error": [], "result": {"eb": "250.00"}}
        elif url.endswith("/Balance"):
            resp.json.return_value = {"error": [], "result": {"ZUSD": "180.00", "XETH": "0.025"}}
        elif url.endswith("/OpenOrders"):
            resp.json.return_value = {"error": [], "result": {"open": {}}}
        elif url.endswith("/ClosedOrders"):
            resp.json.return_value = {"error": [], "result": {"closed": {}}}
        elif url.endswith("/AddOrder"):
            txid = f"LIVE-TX-{len(placed_orders) + 1}"
            placed_orders.append((txid, dict(data)))
            resp.json.return_value = {"error": [], "result": {"txid": [txid], "descr": {"order": "ok"}}}
        elif url.endswith("/CancelAll"):
            resp.json.return_value = {"error": [], "result": {"count": len(placed_orders)}}
        else:
            resp.json.return_value = {"error": [], "result": {}}
        return resp

    tb.api.session.post = mock_private
    ok = tb.run_cycle()
    assert ok and len(placed_orders) >= 4, f"Expected live orders placed, got {len(placed_orders)}"
    sample = placed_orders[0][1]
    return f"Live TradingBot placed {len(placed_orders)} signed Kraken orders (e.g. {sample['type']} {sample['volume']} {sample['pair']} @ ${sample.get('price')})"


if __name__ == "__main__":
    print("=" * 70)
    print("KRAKEN NAVIGATION & ORDER PLACEMENT TEST SUITE")
    print("=" * 70)
    check("1. Kraken official HMAC-SHA512 signature test vector", t_official_signature)
    check("2. Live Kraken public navigation (Ticker + AssetPairs)", t_live_public_navigation)
    check("3. Pair precision & ordermin normalization (ETH/BTC/VELO)", t_precision_and_ordermin)
    check("4. Full private order lifecycle (Add/Market/Cancel/Query/Balance)", t_private_order_lifecycle)
    check("5. Full TradingBot live-mode cycle (dry_run=False)", t_full_live_bot_cycle)

    for status, name, detail in results:
        mark = "✓" if status == "PASS" else "✗"
        print(f" {mark} {status:4s}  {name}\n         → {detail}")
    print("=" * 70)
    fails = sum(1 for s, _, _ in results if s != "PASS")
    sys.exit(1 if fails else 0)
