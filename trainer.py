# trainer.py — Historical replay trainer (SEPARATE from bot.py)
#
# Runs the exact same strategy/learning logic as data_engine.py over cached
# historical Kraken OHLC candles. Expands each OHLC bar into 4 intra-candle
# ticks (O -> L -> H -> C or O -> H -> L -> C) so grid ladders, dip buys,
# scalps, floor drawdowns, and TP/SL orders see real intra-bar extremes.
#
# Supports single-window replay OR --compare-months (runs clean $5,000/asset
# simulations for Last Month [days 60..30] and This Month [days 30..0] with
# prior-month warmup, then ranks consistent winners across both months).

import argparse
import concurrent.futures
import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

from data_engine import (AssetBook, DataEngine, EQUITY_PER_ASSET,
                         fetch_json, list_usd_pairs, LEDGER_FILE,
                         REPORT_FILE, REPORT_MD, STATE_FILE, BASE)

CACHE_DIR = os.path.join(BASE, "ohlc_cache")
_rate_lock = threading.Lock()
_last_req = [0.0]


def _throttled_fetch(url, min_gap=0.22):
    for attempt in range(5):
        with _rate_lock:
            wait = min_gap - (time.time() - _last_req[0])
            if wait > 0:
                time.sleep(wait)
            _last_req[0] = time.time()
        try:
            d = fetch_json(url)
            err = d.get("error", [])
            if any("Too many requests" in str(e) for e in err):
                time.sleep(1.5 * (attempt + 1))
                continue
            return d
        except Exception:
            if attempt == 4:
                raise
            time.sleep(1.0 * (attempt + 1))
    return {}


def get_candles_ohlc(pair, interval=240):
    """Kraken OHLC with disk cache. Returns [(ts, open, high, low, close)]."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"{pair}_{interval}m.json")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 7200:
        try:
            d = json.load(open(cache))
        except Exception:
            d = {}
    else:
        d = {}
    if not d:
        res = _throttled_fetch(
            f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}")
        d = res.get("result", {})
        if d:
            json.dump(d, open(cache, "w"))
    rows = next((v for k, v in d.items() if k != "last"), [])
    return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            for r in rows]


def expand_ohlc_ticks(candles):
    """Expand each (ts, o, h, l, c) into 4 intra-candle ticks preserving range."""
    ticks = []
    for ts, o, h, l, c in candles:
        path = (o, l, h, c) if c >= o else (o, h, l, c)
        for idx, p in enumerate(path):
            ticks.append((ts + idx * 60, p))
    return ticks


class Trainer(DataEngine):
    """Reuse engine machinery; drive books from OHLC candles."""

    def __init__(self, pair_filter=None, min_24h_usd=5000.0):
        self.pairs = list_usd_pairs()
        if pair_filter:
            flt = set(pair_filter)
            self.pairs = {n: p for n, p in self.pairs.items()
                          if p["asset"] in flt or n in flt}
        elif min_24h_usd > 0:
            try:
                q = ",".join(self.pairs.keys())
                tk = fetch_json(
                    f"https://api.kraken.com/0/public/Ticker?pair={q}"
                ).get("result", {})
                self.pairs = {
                    n: p for n, p in self.pairs.items()
                    if n in tk and float(tk[n]["v"][1]) * float(tk[n]["c"][0]) >= min_24h_usd
                    and int(tk[n]["t"][1]) >= 15
                }
            except Exception as e:
                print(f"[Trainer] Volume filter skipped: {e}")
        print(f"[Trainer] Universe: {len(self.pairs)} active USD pairs")
        self.books = {n: AssetBook(n, p["asset"])
                      for n, p in self.pairs.items()}
        self.cycle = 0
        self.sim_time = None
        self.window_tag = "replay"
        self.trades_buf = []

    def _log_trade(self, bk, strategy, side, price, vol, pnl, extra=None):
        ts_val = self.sim_time or time.time()
        dt = datetime.fromtimestamp(ts_val, timezone.utc)
        rec = {
            "ts": dt.isoformat(),
            "source": self.window_tag,
            "pair": bk.name, "asset": bk.asset, "strategy": strategy,
            "side": side, "price": round(price, 8), "vol": vol,
            "pnl": round(pnl, 4), "regime": bk.regime,
            "floor_dd": round(bk.floor_dd(price), 4),
            "hour": dt.hour, "dow": dt.weekday(), "day": dt.day,
        }
        if extra:
            rec.update(extra)
        self.trades_buf.append(rec)

    def _prefetch_all(self, interval):
        names = list(self.pairs.keys())
        out = {}
        done = 0

        def _job(n):
            return n, get_candles_ohlc(n, interval)

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(_job, n) for n in names]
            for f in concurrent.futures.as_completed(futs):
                done += 1
                try:
                    n, rows = f.result()
                    if rows:
                        out[n] = rows
                except Exception:
                    pass
                if done % 50 == 0 or done == len(names):
                    print(f"[Trainer] Fetched {done}/{len(names)} pairs")
        return out

    def _run_window(self, candle_map, start_ts, end_ts, warmup_ts, tag,
                    prior_books=None):
        """Run a clean $5,000/asset pass over [start_ts, end_ts) with warmup."""
        self.window_tag = tag
        self.trades_buf = []
        self.books = {n: AssetBook(n, p["asset"])
                      for n, p in self.pairs.items()}
        if prior_books:
            for n, pb in prior_books.items():
                if n in self.books:
                    self.books[n].floor_memory = dict(pb.floor_memory)
                    for k, v in pb.strat_stats.items():
                        self.books[n].strat_stats[k] = list(v)
        last_prices = {}

        for n, rows in candle_map.items():
            bk = self.books[n]
            warm_rows = [r for r in rows if warmup_ts <= r[0] < start_ts]
            win_rows = [r for r in rows if start_ts <= r[0] < end_ts]
            if len(win_rows) < 20:
                continue
            # Seed moving-average / floor history with warmup closes (no trades)
            for _, o, h, l, c in warm_rows:
                for p in ((o, l, h, c) if c >= o else (o, h, l, c)):
                    bk.history.append(p)
                    if len(bk.history) > 720:
                        bk.history.pop(0)
            # Replay window ticks with full trading
            for ts, price in expand_ohlc_ticks(win_rows):
                self.cycle += 1
                self.sim_time = ts
                self._tick(bk, price)
            last_prices[n] = win_rows[-1][4]

        # Summarize window
        summary = self._summarize_window(last_prices, tag)
        return summary, list(self.trades_buf)

    def _summarize_window(self, last_prices, tag):
        sells = [t for t in self.trades_buf
                 if t["side"] == "sell" and t.get("exit_reason")]
        by_asset = defaultdict(lambda: [0.0, 0, 0])
        by_strat = defaultdict(lambda: [0.0, 0, 0])
        by_regime = defaultdict(lambda: [0.0, 0, 0])
        by_hour = defaultdict(lambda: [0.0, 0, 0])
        by_day = defaultdict(lambda: [0.0, 0, 0])
        floor_hits = defaultdict(lambda: [0, 0])

        for t in sells:
            for d, k in ((by_asset, t["asset"]), (by_strat, t["strategy"]),
                         (by_regime, t["regime"]), (by_hour, t["hour"]),
                         (by_day, t["day"])):
                d[k][0] += t["pnl"]
                d[k][1] += 1
                d[k][2] += int(t["pnl"] > 0)
            if t["strategy"] == "floor":
                floor_hits[t["asset"]][0] += 1
                floor_hits[t["asset"]][1] += int(t["pnl"] > 0)

        asset_rows = []
        for n, bk in self.books.items():
            lp = last_prices.get(n, 0.0)
            if not lp or bk.trades == 0:
                continue
            eq = bk.equity(lp)
            net_eq_pnl = eq - EQUITY_PER_ASSET
            realized_pnl, n_tr, n_win = by_asset[bk.asset]
            asset_rows.append({
                "pair": n,
                "asset": bk.asset,
                "realized_pnl": round(realized_pnl, 2),
                "net_equity_pnl": round(net_eq_pnl, 2),
                "equity": round(eq, 2),
                "fees": round(bk.fees, 2),
                "trades": n_tr,
                "win_rate": round(n_win / n_tr, 3) if n_tr else 0.0,
            })

        asset_rows.sort(key=lambda r: -r["net_equity_pnl"])
        return {
            "window": tag,
            "generated": datetime.now(timezone.utc).isoformat(),
            "assets_traded": len(asset_rows),
            "closed_positions": len(sells),
            "asset_rankings": asset_rows,
            "pnl_by_strategy": {
                k: {"pnl": round(v[0], 2), "trades": v[1],
                    "wr": round(v[2] / v[1], 3) if v[1] else 0.0}
                for k, v in by_strat.items()
            },
            "pnl_by_regime": {k: round(v[0], 2) for k, v in by_regime.items()},
            "pnl_by_hour_utc": {str(k): round(v[0], 2)
                                for k, v in sorted(by_hour.items())},
            "pnl_by_day_of_month": {str(k): round(v[0], 2)
                                    for k, v in sorted(by_day.items())},
            "floor_accuracy": {
                a: {"floors": v[0], "held": v[1],
                    "rate": round(v[1] / v[0], 3) if v[0] else 0.0}
                for a, v in floor_hits.items()
            },
        }

    def compare_months(self, interval=240):
        candle_map = self._prefetch_all(interval)
        now_ts = int(time.time())
        d30 = 30 * 86400
        last_m_start = now_ts - 2 * d30
        last_m_warm = now_ts - 3 * d30
        this_m_start = now_ts - d30

        print("[Trainer] Replaying LAST MONTH (days 60->30 ago)...")
        rep_last, trades_last = self._run_window(
            candle_map, last_m_start, this_m_start, last_m_warm, "last_month")
        last_books = dict(self.books)
        json.dump(rep_last, open(os.path.join(BASE, "report_last_month.json"), "w"), indent=2)

        print("[Trainer] Replaying THIS MONTH (days 30->0 ago, with learned memory)...")
        rep_this, trades_this = self._run_window(
            candle_map, this_m_start, now_ts + 86400, last_m_start, "this_month",
            prior_books=last_books)
        json.dump(rep_this, open(os.path.join(BASE, "report_this_month.json"), "w"), indent=2)

        # Write combined ledger
        with open(LEDGER_FILE, "w", encoding="utf-8") as f:
            for t in trades_last + trades_this:
                f.write(json.dumps(t) + "\n")

        # Find consistent winners across BOTH months
        map_last = {r["asset"]: r for r in rep_last["asset_rankings"]}
        map_this = {r["asset"]: r for r in rep_this["asset_rankings"]}

        consistent = []
        all_combined = []
        for asset in set(map_last) | set(map_this):
            r1 = map_last.get(asset)
            r2 = map_this.get(asset)
            p1 = r1["net_equity_pnl"] if r1 else 0.0
            p2 = r2["net_equity_pnl"] if r2 else 0.0
            rl1 = r1["realized_pnl"] if r1 else 0.0
            rl2 = r2["realized_pnl"] if r2 else 0.0
            tr = (r1["trades"] if r1 else 0) + (r2["trades"] if r2 else 0)
            pair_name = (r2 or r1)["pair"]
            wr1 = r1["win_rate"] if r1 else 0.0
            wr2 = r2["win_rate"] if r2 else 0.0
            row = {
                "pair": pair_name,
                "asset": asset,
                "total_net_pnl": round(p1 + p2, 2),
                "total_realized_pnl": round(rl1 + rl2, 2),
                "last_month_net": round(p1, 2),
                "this_month_net": round(p2, 2),
                "last_month_realized": round(rl1, 2),
                "this_month_realized": round(rl2, 2),
                "trades": tr,
                "win_rate": round((wr1 + wr2) / (2 if (r1 and r2) else 1), 3),
                "pnl": round(p1 + p2, 2),
                "equity": round(EQUITY_PER_ASSET + p1 + p2, 2),
            }
            all_combined.append(row)
            if r1 and r2 and p1 > 0 and p2 > 0 and rl1 >= 0 and rl2 >= 0 and tr >= 4:
                consistent.append(row)

        consistent.sort(key=lambda r: -r["total_net_pnl"])
        all_combined.sort(key=lambda r: -r["total_net_pnl"])

        combined_report = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "cycle": self.cycle,
            "assets_tracked": len(self.pairs),
            "closed_positions": rep_last["closed_positions"] + rep_this["closed_positions"],
            "consistent_winners": consistent,
            "last_month_top": rep_last["asset_rankings"][:25],
            "this_month_top": rep_this["asset_rankings"][:25],
            "asset_rankings": all_combined,
            "pnl_by_strategy": {
                "last_month": rep_last["pnl_by_strategy"],
                "this_month": rep_this["pnl_by_strategy"],
            },
            "pnl_by_regime": {
                "last_month": rep_last["pnl_by_regime"],
                "this_month": rep_this["pnl_by_regime"],
            },
            "pnl_by_hour_utc": rep_this["pnl_by_hour_utc"],
            "pnl_by_day_of_month": rep_this["pnl_by_day_of_month"],
            "floor_accuracy": rep_this["floor_accuracy"],
        }
        json.dump(combined_report, open(REPORT_FILE, "w"), indent=2)
        self._save_state()
        self._write_comparison_md(combined_report, rep_last, rep_this)
        print(f"[Trainer] Done! Consistent 2-month winners: {len(consistent)}")
        return combined_report

    def _write_comparison_md(self, comb, r_last, r_this):
        lines = [
            "# Data Engine — Last Month vs This Month Report",
            f"Generated: {comb['generated']} | Pairs analyzed: {comb['assets_tracked']} | "
            f"Closed trades: {comb['closed_positions']}",
            "",
            "## Consistent Winners (Profitable in BOTH Last Month & This Month)",
            "",
        ]
        for a in comb["consistent_winners"][:25]:
            lines.append(
                f"- **{a['asset']} ({a['pair']})**: Total **${a['total_net_pnl']:+.2f}** "
                f"(Last M: ${a['last_month_net']:+.2f}, This M: ${a['this_month_net']:+.2f} | "
                f"Realized: ${a['total_realized_pnl']:+.2f} | {a['trades']} trades, {a['win_rate']*100:.0f}% WR)"
            )
        lines += ["", "## Last Month Top 15", ""]
        for a in r_last["asset_rankings"][:15]:
            lines.append(
                f"- **{a['asset']}**: Net ${a['net_equity_pnl']:+.2f} "
                f"(Realized ${a['realized_pnl']:+.2f}, {a['trades']} trades, {a['win_rate']*100:.0f}% WR)"
            )
        lines += ["", "## This Month Top 15", ""]
        for a in r_this["asset_rankings"][:15]:
            lines.append(
                f"- **{a['asset']}**: Net ${a['net_equity_pnl']:+.2f} "
                f"(Realized ${a['realized_pnl']:+.2f}, {a['trades']} trades, {a['win_rate']*100:.0f}% WR)"
            )
        open(REPORT_MD, "w", encoding="utf-8").write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=240,
                    help="candle minutes (240 = 4h candles covering 120 days)")
    ap.add_argument("--pairs", nargs="*", default=None)
    ap.add_argument("--min-vol", type=float, default=5000.0,
                    help="min 24h USD volume filter (0 = all 628 pairs)")
    a = ap.parse_args()
    Trainer(a.pairs, min_24h_usd=a.min_vol).compare_months(interval=a.interval)


if __name__ == "__main__":
    main()
