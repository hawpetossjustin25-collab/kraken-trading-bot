#!/usr/bin/env python3
"""
System audit — tests whether the bot has everything the full build needs.
Each test maps to a capability the terminal/system requires. Failures =
things to add to the bot.
"""
import json
import logging
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bot as B
import order_flow as OF
import improvement_engine as IE

log = logging.getLogger("audit")
log.addHandler(logging.NullHandler())

PASS, FAIL, MISSING = "PASS", "FAIL", "MISSING"
results = []


def check(name, fn):
    try:
        r = fn()
        results.append((name, r or PASS, ""))
    except Exception as e:
        results.append((name, FAIL, str(e)[:120]))


def missing(name, note):
    results.append((name, MISSING, note))


cfg = json.load(open(os.path.join(BASE, "config.json")))

# --- 1. Dry-run trading works end-to-end ---
def t_dryrun():
    api = B.DryRunAPI(log, starting_equity=100.0, pair="XETHZUSD")
    r = api.add_order("XETHZUSD", "buy", "0.01", "2000.00")
    assert "txid" in r, "order failed"
    eq = api.trade_balance()
    assert float(eq["eb"]) > 0
    return PASS
check("dry-run order placement + equity", t_dryrun)

# --- 2. Order flow state file exists and is fresh ---
def t_flow_state():
    assert os.path.exists(OF.STATE_PATH), "order_flow_state.json missing"
    s = json.load(open(OF.STATE_PATH))
    age = time.time() - __import__("datetime").datetime.fromisoformat(s["timestamp"]).timestamp()
    assert age < 600, f"state stale ({age:.0f}s)"
    for k in ("mid", "obi_10lvl", "composite_score", "signal", "bid_walls", "ask_walls"):
        assert k in s, f"state missing {k}"
    return PASS
check("order-flow state file (live, fresh)", t_flow_state)

# --- 3. Book metrics real ---
def t_book():
    b = OF.OrderBook()
    b.apply_snapshot([(2400.0, 5.0), (2399.0, 2.0)], [(2401.0, 3.0), (2402.0, 1.0)])
    assert b.mid() == 2400.5
    assert b.imbalance(10) > 0  # more bid depth
    assert b.microprice() is not None
    # walls() needs ≥5 levels per side — give it a real book
    for i in range(8):
        b.apply_update([(2390.0 - i, 1.0)], [])
    b.apply_update([(2380.0, 50.0)], [])  # a wall
    assert b.walls("bid", multiple=4.0), "wall not detected"
    return PASS
check("order book math (OBI/microprice/walls)", t_book)

# --- 4. Improvement engine persists + suggests ---
def t_improve():
    rl = B.ReinforcementLearner(cfg, log)
    eng = IE.ImprovementEngine(cfg, log, rl)
    for _ in range(10):
        eng.record_outcome("dip", -2.0, -0.5, "downtrend", -0.5)
    eng.analysis_interval_cycles = 1
    eng.analyze_cycle({"composite_score": -0.3}, 2500.0)
    assert os.path.exists(IE.SUGGESTIONS_PATH)
    assert os.path.exists(IE.STATE_PATH)
    return PASS
check("improvement engine writes state+suggestions", t_improve)

# --- 5. Bot state persistence ---
def t_botstate():
    assert os.path.exists(os.path.join(BASE, "bot_state.json")), "bot_state.json missing — bot never ran a full cycle?"
    return PASS
check("bot_state.json persisted", t_botstate)

# --- 6. Supervisor resilience files ---
def t_supervisor():
    assert os.path.exists(os.path.join(BASE, "run_bot.py"))
    return PASS
check("supervisor script exists", t_supervisor)

# --- 7. Terminal needs: equity/positions feed ---
def t_positions_feed():
    api = B.DryRunAPI(log, starting_equity=100.0, pair="XETHZUSD")
    api.add_order("XETHZUSD", "buy", "0.01", "2000.00")
    assert api.open_orders()["open"], "no open orders tracked"
    snap = api.positions_snapshot()
    for k in ("equity", "usd", "eth", "fees_paid", "open_orders", "mid"):
        assert k in snap, f"snapshot missing {k}"
    return PASS
check("positions/equity state feed for terminal", t_positions_feed)

# --- 8. Trade log structured for terminal parsing ---
def t_tradelog():
    assert os.path.exists(os.path.join(BASE, "trades.log")), "trades.log missing"
    return PASS
check("trades.log exists", t_tradelog)

# --- 9. Fill reconciliation (live fidelity) ---
def t_recon():
    # Bot assumes fill price = order price; real Kraken fills report actual
    # price/vol via ClosedOrders — no reconciliation code exists
    src = open(os.path.join(BASE, "bot.py"), encoding="utf-8").read()
    assert "closed_orders" in src and "query_orders" in src and "reconcile" in src, "reconciliation methods missing"
    api = B.DryRunAPI(log, starting_equity=100.0, pair="XETHZUSD")
    assert hasattr(api, "closed_orders") and hasattr(api, "query_orders")
    return PASS
check("fill reconciliation (ClosedOrders)", t_recon)

# --- 10. Fee model in dry-run accounting ---
def t_fees():
    src = open(os.path.join(BASE, "bot.py"), encoding="utf-8").read()
    assert "fee" not in src.lower() or "kraken_fee" not in src.lower(), ""
    api = B.DryRunAPI(log, starting_equity=100.0, pair="XETHZUSD")
    api.add_order("XETHZUSD", "buy", "0.01", "100.00", ordertype="market")
    bal = float(api.balance()["ZUSD"])
    assert api.fees_paid > 0, "no fee charged"
    assert bal < 100.0 - 0.01 * api._mid() * api.taker_fee * 0.9
    return PASS
check("fee/slippage model in dry-run", t_fees)

# --- 11. Multi-pair support ---
def t_multipair():
    c = dict(cfg); c["pairs"] = ["XETHZUSD", "XXBTZUSD"]
    tb = B.TradingBot(c, log)
    assert set(tb.pairs) == {"XETHZUSD", "XXBTZUSD"}
    assert len(tb.ctxs) == 2 and len(tb.adaptives) == 2
    tb.api.session.close() if hasattr(tb.api, "session") else None
    for ctx in tb.ctxs.values():
        if ctx["flow"]:
            ctx["flow"].stop()
    return PASS
check("multi-pair support", t_multipair)

# --- 12. WebSocket execution channel (real-time fills) ---
def t_ws_exec():
    src = open(os.path.join(BASE, "bot.py"), encoding="utf-8").read()
    assert "get_ws_token" in src and "GetWebSocketsToken" in src
    assert "class ExecFeed" in src and "ownTrades" in src
    assert hasattr(B, "ExecFeed"), "ExecFeed not importable"
    feed = B.ExecFeed.__new__(B.ExecFeed)  # structure only — no network
    assert hasattr(B.ExecFeed, "start") and hasattr(B.ExecFeed, "stop")
    assert hasattr(B.ExecFeed, "_on_message")
    return PASS
check("WS execution channel (ownTrades) for live fills", t_ws_exec)

# ---------------------------------------------------------------------------
print("\n" + "=" * 66)
print("SYSTEM AUDIT — bot vs. full build requirements")
print("=" * 66)
for name, status, note in results:
    icon = {"PASS": "✓", "FAIL": "✗", "MISSING": "○"}[status]
    print(f" {icon} {status:7} {name}")
    if note:
        print(f"          → {note}")
n = {"PASS": 0, "FAIL": 0, "MISSING": 0}
for _, s, _ in results:
    n[s] += 1
print("=" * 66)
print(f" {n['PASS']} pass | {n['FAIL']} fail | {n['MISSING']} missing")
