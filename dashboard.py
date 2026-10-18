#!/usr/bin/env python3
"""
Trading Terminal Dashboard — live read-only monitor for the Kraken bot.

Reads the state files the bot writes every cycle and renders a live
terminal UI. Places NO orders and requires NO API keys — completely
safe to run alongside the bot.

Panels:
  - Header:      equity, mid price, regime, strategies, dry-run/live mode
  - Order book:  top-of-book ladder, bid/ask depth $, walls, OBI gauge
  - Order flow:  CVD, microprice, composite score, signal arrow
  - Positions:   balances, open orders table, fees paid
  - Improvement: latest self-improvement suggestions
  - Log tail:    last lines of trades.log

Usage:
    python dashboard.py
"""

import json
import os
import sys
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

console = Console()


def read_json(name: str) -> dict:
    try:
        with open(os.path.join(BASE_DIR, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def read_log_tail(name: str, n: int = 8) -> list[str]:
    try:
        with open(os.path.join(BASE_DIR, name), encoding="utf-8", errors="replace") as f:
            return [l.rstrip() for l in f.readlines()[-n:]]
    except Exception:
        return []


def fmt_age(ts: float) -> str:
    if not ts:
        return "—"
    age = time.time() - ts
    if age < 90:
        return f"{age:.0f}s ago"
    return f"{age/60:.0f}m ago (STALE)"


def gauge(score: float, width: int = 20) -> Text:
    """-1..+1 → coloured bar."""
    pos = int((max(-1.0, min(1.0, score)) + 1.0) / 2 * width)
    bar = Text()
    for i in range(width):
        if i < pos:
            bar.append("█", style="green" if score > 0 else "red")
        else:
            bar.append("░", style="dim")
    return bar


def header_panel(pos: dict, flow: dict) -> Panel:
    t = Table.grid(expand=True)
    t.add_column(justify="left")
    t.add_column(justify="right")
    mode = "[bold yellow]DRY-RUN[/]" if not pos.get("live", False) else "[bold red]LIVE[/]"
    ts = pos.get("timestamp", 0.0)
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts).timestamp()
        except Exception:
            ts = 0.0
    stale = fmt_age(ts)
    eq = pos.get("equity", 0.0)
    mid = pos.get("mid", 0.0)
    pairs = ", ".join(pos.get("pairs", []))
    t.add_row(
        f"[bold]Kraken Bot[/]  {mode}  pair(s): {pairs}",
        f"Equity: [bold]${eq:,.2f}[/]   Mid: ${mid:,.2f}   updated {stale}",
    )
    strat = pos.get("strategies_active", {})
    if strat:
        t.add_row("Active strategies:", "  ".join(
            f"{p}: {', '.join(s)}" for p, s in strat.items()))
    sig = flow.get("signal", "neutral")
    colour = {"bullish": "green", "bearish": "red"}.get(sig, "yellow")
    t.add_row("Order-flow signal:", f"[bold {colour}]{sig.upper()}[/]")
    return Panel(t, title="STATUS", border_style="cyan")


def book_panel(flow: dict) -> Panel:
    t = Table(show_header=True, header_style="bold")
    t.add_column("Side", justify="right", width=6)
    t.add_column("Depth ($)", justify="right")
    t.add_column("Wall @", justify="right")
    t.add_column("Wall $", justify="right")
    bids = flow.get("bid_depth_usd", 0.0)
    asks = flow.get("ask_depth_usd", 0.0)
    bw = (flow.get("bid_walls") or [{}])[0]
    aw = (flow.get("ask_walls") or [{}])[0]
    t.add_row("BIDS", f"${bids:,.0f}",
              f"{bw.get('price', 0):,.2f}" if bw else "—",
              f"${bw.get('usd', 0):,.0f}" if bw else "—",
              style="green")
    t.add_row("ASKS", f"${asks:,.0f}",
              f"{aw.get('price', 0):,.2f}" if aw else "—",
              f"${aw.get('usd', 0):,.0f}" if aw else "—",
              style="red")
    obi = flow.get("obi_10lvl", 0.0)
    body = Table.grid(expand=True)
    body.add_row(t)
    body.add_row(Text(f"OBI {obi:+.3f}  "), gauge(obi))
    return Panel(body, title="ORDER BOOK (L2)", border_style="blue")


def flow_panel(flow: dict) -> Panel:
    t = Table.grid(expand=True)
    cvd = flow.get("cvd_5min_eth", 0.0)
    micro = flow.get("microprice", 0.0)
    score = flow.get("composite_score", 0.0)
    whales = len(flow.get("whale_trades") or [])
    t.add_row(f"CVD (5m): {cvd:+.4f}", f"Microprice: ${micro:,.2f}")
    t.add_row(f"Whale trades (5m): {whales}",
              Text("Score "), gauge(score), Text(f" {score:+.3f}"))
    return Panel(t, title="ORDER FLOW", border_style="magenta")


def positions_panel(pos: dict) -> Panel:
    t = Table(show_header=True, header_style="bold")
    t.add_column("TXID")
    t.add_column("Side")
    t.add_column("Price", justify="right")
    t.add_column("Volume", justify="right")
    for o in (pos.get("open_orders") or [])[:12]:
        side = o.get("type", "?")
        t.add_row(o.get("txid", "?"),
                  f"[green]{side}[/]" if side == "buy" else f"[red]{side}[/]",
                  f"${o.get('price', 0):,.2f}",
                  f"{o.get('volume', 0):.6f}")
    if not pos.get("open_orders"):
        t.add_row("—", "—", "—", "—")
    body = Table.grid(expand=True)
    body.add_row(t)
    body.add_row(f"USD: ${pos.get('usd', 0):,.2f}   ETH: {pos.get('eth', 0):.6f}   "
                 f"Fees paid: ${pos.get('fees_paid', 0):.4f}")
    return Panel(body, title="POSITIONS / OPEN ORDERS", border_style="green")


def improve_panel() -> Panel:
    sug = read_json("improvement_suggestions.json")
    items = sug.get("suggestions", []) if isinstance(sug, dict) else []
    text = Text()
    if not items:
        text.append("No suggestions yet — engine gathers data over ~25 cycles.", style="dim")
    for s in items[:6]:
        sev = s.get("severity", "info")
        colour = {"high": "red", "medium": "yellow"}.get(sev, "cyan")
        text.append(f"[{sev.upper()}] ", style=colour)
        text.append(f"{s.get('message', s.get('type', '?'))}\n")
    return Panel(text, title="SELF-IMPROVEMENT SUGGESTIONS", border_style="yellow")


def log_panel() -> Panel:
    lines = read_log_tail("trades.log", 8)
    return Panel("\n".join(lines) or "(no log yet)", title="LOG TAIL",
                 border_style="dim")


def make_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top", size=7),
        Layout(name="mid"),
        Layout(name="low", size=9),
        Layout(name="bottom", size=11),
    )
    layout["mid"].split_row(
        Layout(name="book"), Layout(name="flow"),
    )
    layout["low"].split_row(
        Layout(name="positions"), Layout(name="improve"),
    )
    return layout


def main():
    layout = make_layout()
    with Live(layout, console=console, refresh_per_second=2, screen=True):
        while True:
            pos = read_json("positions_state.json")
            flow = read_json("order_flow_state.json")
            layout["top"].update(header_panel(pos, flow))
            layout["book"].update(book_panel(flow))
            layout["flow"].update(flow_panel(flow))
            layout["positions"].update(positions_panel(pos))
            layout["improve"].update(improve_panel())
            layout["bottom"].update(log_panel())
            time.sleep(0.5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
