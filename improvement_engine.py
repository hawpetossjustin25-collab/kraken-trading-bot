#!/usr/bin/env python3
"""
Self-Improvement Engine — meta-learning layer for the trading bot.

Runs inside the bot every cycle. Watches every trade outcome, every
order-flow signal, and every blocked/skipped opportunity, then:

  1. SCORES each strategy + signal source by real PnL contribution
  2. AUTO-TUNES parameters within safety bounds (delegates to RL Q-table)
  3. LEARNS which order-flow conditions precede wins/losses
  4. DETECTS systematic failures (blocked entries that would have won,
     strategies losing repeatedly, fills that always slip)
  5. WRITES upgrade suggestions to improvement_suggestions.json —
     things only a human/Devin can change (code upgrades, new data
     sources, new strategies, risk-limit changes)
  6. PERSISTS everything to improvement_state.json across restarts

This is the "get smarter every trade" layer: it never stops learning.
"""

import json
import logging
import os
import statistics
import time
from collections import deque
from datetime import datetime
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUGGESTIONS_PATH = os.path.join(BASE_DIR, "improvement_suggestions.json")
STATE_PATH = os.path.join(BASE_DIR, "improvement_state.json")
REPORT_PATH = os.path.join(BASE_DIR, "SELF_IMPROVEMENT.md")

# Safety bounds — the engine can never tune outside these, ever.
PARAM_LIMITS = {
    "grid_spacing_pct":      (0.5, 5.0),
    "scalp_tp_pct":          (0.2, 3.0),
    "dip_threshold_pct":     (1.0, 8.0),
    "dip_buy_pct":           (5.0, 40.0),
    "scalp_size_pct":        (3.0, 25.0),
    "grid_alloc_pct":        (20.0, 80.0),
    "flow_block_threshold":  (-0.8, -0.1),
}


class ImprovementEngine:
    """Meta-learner. One instance per bot; call analyze_cycle() each loop."""

    def __init__(self, config: dict, logger: logging.Logger, rl):
        self.cfg = config
        self.log = logger
        self.rl = rl
        self.enabled = config.get("improvement_enabled", True)
        self.min_trades = config.get("improvement_min_trades", 10)
        self._cycle = 0

        # ---- learned state (persisted) ----
        self.flow_outcomes: dict[str, list[float]] = {}   # flow-signal bucket -> pnl list
        self.blocked_opportunities: deque = deque(maxlen=200)
        self.param_history: list[dict] = []
        self.last_suggestions: list[dict] = []
        self.improvement_score: float = 0.0
        self._load_state()

        # What to check each analysis pass
        self.analysis_interval_cycles = 25

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_state(self):
        if os.path.exists(STATE_PATH):
            try:
                with open(STATE_PATH) as f:
                    s = json.load(f)
                self.flow_outcomes = s.get("flow_outcomes", {})
                self.param_history = s.get("param_history", [])
                self.improvement_score = s.get("improvement_score", 0.0)
                self.log.info("[Improve] Loaded state: %d flow-condition buckets tracked, score=%.3f",
                              len(self.flow_outcomes), self.improvement_score)
            except (json.JSONDecodeError, IOError):
                pass

    def _save_state(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump({
                    "flow_outcomes": self.flow_outcomes,
                    "param_history": self.param_history[-200:],
                    "improvement_score": self.improvement_score,
                    "last_saved": datetime.now().isoformat(),
                }, f, indent=2)
        except IOError as e:
            self.log.warning("[Improve] State save failed: %s", e)

    # ------------------------------------------------------------------
    # Learning hooks — called by bot.py
    # ------------------------------------------------------------------

    def record_outcome(self, strategy: str, pnl_pct: float, flow_score: float,
                       regime: str, sentiment: float):
        """Called when a trade closes. Links order-flow state → outcome."""
        bucket = self._flow_bucket(flow_score, regime, sentiment)
        self.flow_outcomes.setdefault(bucket, []).append(pnl_pct)
        self.improvement_score = self._compute_improvement_score()

    def record_blocked_entry(self, strategy: str, flow_score: float,
                             price: float):
        """Called when order flow vetoed an entry — we track whether the
        veto was correct (price later fell) or wrong (price rose)."""
        self.blocked_opportunities.append({
            "ts": time.time(), "strategy": strategy,
            "flow_score": flow_score, "price": price,
        })

    def _flow_bucket(self, score: float, regime: str, sentiment: float) -> str:
        band = ("strong_bull" if score > 0.3 else
                "bull" if score > 0.1 else
                "neutral" if score > -0.1 else
                "bear" if score > -0.3 else "strong_bear")
        sent = "pos" if sentiment > 0.2 else ("neg" if sentiment < -0.2 else "flat")
        return f"{regime}|{band}|{sent}"

    # ------------------------------------------------------------------
    # Analysis pass
    # ------------------------------------------------------------------

    def analyze_cycle(self, flow_state: Optional[dict], current_price: float):
        """Run every cycle; heavy analysis every N cycles."""
        if not self.enabled:
            return
        self._cycle += 1
        self._review_blocked(current_price)
        if self._cycle % self.analysis_interval_cycles != 0:
            return
        self.log.info("[Improve] Running meta-analysis (cycle %d)…", self._cycle)
        suggestions = []
        suggestions += self._check_strategy_health()
        suggestions += self._check_flow_calibration()
        suggestions += self._check_param_drift()
        suggestions += self._check_blocked_accuracy(current_price)
        self.last_suggestions = suggestions
        self._write_suggestions(suggestions)
        self._write_report()
        self._save_state()

    # ------------------------------------------------------------------
    # Analyzers
    # ------------------------------------------------------------------

    def _check_strategy_health(self) -> list[dict]:
        """Flag strategies with a sustained losing record."""
        out = []
        for strat in ("grid", "dip", "scalp"):
            trades = self.rl.strategy_trades.get(strat, 0)
            pnl = self.rl.strategy_total_pnl.get(strat, 0.0)
            wins = self.rl.strategy_wins.get(strat, 0)
            if trades >= self.min_trades:
                wr = wins / trades
                if pnl < 0 and wr < 0.45:
                    out.append({
                        "severity": "high",
                        "type": "strategy_underperforming",
                        "target": strat,
                        "detail": (f"'{strat}' losing: {trades} trades, "
                                   f"win-rate {wr:.0%}, total PnL {pnl:+.2f}%. "
                                   f"Suggest disabling or retuning parameters."),
                        "auto_fixable": True,
                    })
                elif trades >= 30 and wr > 0.65 and pnl > 0:
                    out.append({
                        "severity": "info",
                        "type": "strategy_outperforming",
                        "target": strat,
                        "detail": (f"'{strat}' performing well ({wr:.0%} WR, "
                                   f"{pnl:+.2f}%). Suggest raising its capital "
                                   f"weight via RL."),
                        "auto_fixable": True,
                    })
        return out

    def _check_flow_calibration(self) -> list[dict]:
        """Check whether order-flow buckets actually predict outcomes."""
        out = []
        for bucket, pnls in self.flow_outcomes.items():
            if len(pnls) < 8:
                continue
            mean = statistics.mean(pnls)
            band = bucket.split("|")[1]
            if band in ("strong_bull", "bull") and mean < -0.3:
                out.append({
                    "severity": "medium",
                    "type": "flow_miscalibration",
                    "target": bucket,
                    "detail": (f"Trades taken in '{band}' book conditions "
                               f"averaged {mean:+.2f}% — bullish flow signal is "
                               f"losing money. Suggest flipping weight or "
                               f"raising flow_block threshold."),
                    "auto_fixable": True,
                })
            elif band in ("strong_bear", "bear") and mean > 0.3:
                out.append({
                    "severity": "medium",
                    "type": "flow_miscalibration",
                    "target": bucket,
                    "detail": (f"Trades in '{band}' conditions averaged "
                               f"{mean:+.2f}% — the bearish veto may be too "
                               f"aggressive; we're winning where we expected "
                               f"to lose."),
                    "auto_fixable": True,
                })
        return out

    def _check_param_drift(self) -> list[dict]:
        """Compare RL-recommended params to current config — flag drift."""
        out = []
        rec = self.rl.get_recommended_params()
        for param, suggested in rec.items():
            lo, hi = PARAM_LIMITS.get(param, (None, None))
            if lo is None:
                continue
            suggested = max(lo, min(hi, suggested))
            current = self.cfg.get(param)
            if current is None:
                continue
            if abs(suggested - current) / max(abs(current), 1e-9) > 0.25:
                out.append({
                    "severity": "low",
                    "type": "param_drift",
                    "target": param,
                    "detail": (f"RL suggests {param}={suggested:.2f} "
                               f"(current {current}) — learned evidence says "
                               f"the current value is off by >25%."),
                    "auto_fixable": True,
                    "suggested_value": suggested,
                })
        return out

    def _check_blocked_accuracy(self, current_price: float) -> list[dict]:
        """Grade past blocked entries: did we dodge losses or miss wins?"""
        if len(self.blocked_opportunities) < 5:
            return []
        good_vetoes = 0
        bad_vetoes = 0
        for opp in self.blocked_opportunities:
            if current_price < opp["price"] * 0.995:
                good_vetoes += 1   # price fell — veto saved us
            elif current_price > opp["price"] * 1.005:
                bad_vetoes += 1    # price rose — veto cost us
        total = good_vetoes + bad_vetoes
        if total < 5:
            return []
        acc = good_vetoes / total
        if acc < 0.5:
            return [{
                "severity": "medium",
                "type": "veto_accuracy_low",
                "target": "order_flow_block",
                "detail": (f"Order-flow entry veto is only {acc:.0%} accurate "
                           f"({bad_vetoes} missed wins vs {good_vetoes} dodged "
                           f"losses). Suggest loosening the -0.40 threshold."),
                "auto_fixable": True,
            }]
        return [{
            "severity": "info",
            "type": "veto_accuracy_ok",
            "target": "order_flow_block",
            "detail": f"Entry veto accuracy {acc:.0%} — order-flow gate is working.",
            "auto_fixable": False,
        }]

    def _review_blocked(self, current_price: float):
        """Lightweight per-cycle: expire stale blocked entries (>1h)."""
        cutoff = time.time() - 3600
        while self.blocked_opportunities and self.blocked_opportunities[0]["ts"] < cutoff:
            self.blocked_opportunities.popleft()

    # ------------------------------------------------------------------
    # Scoring & output
    # ------------------------------------------------------------------

    def _compute_improvement_score(self) -> float:
        """0..1 — fraction of tracked flow-buckets whose mean PnL sign
        matches expectation (bullish bucket → positive, bearish → avoided)."""
        correct = 0
        total = 0
        for bucket, pnls in self.flow_outcomes.items():
            if len(pnls) < 3:
                continue
            band = bucket.split("|")[1]
            mean = statistics.mean(pnls)
            total += 1
            if band in ("strong_bull", "bull") and mean > 0:
                correct += 1
            elif band in ("strong_bear", "bear") and mean < 0:
                correct += 1   # correctly identified losers
            elif band == "neutral":
                correct += 0.5
        return round(correct / total, 3) if total else 0.0

    def _write_suggestions(self, suggestions: list[dict]):
        data = {
            "generated": datetime.now().isoformat(),
            "improvement_score": self.improvement_score,
            "total_suggestions": len(suggestions),
            "suggestions": suggestions,
        }
        try:
            with open(SUGGESTIONS_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            self.log.warning("[Improve] Suggestion write failed: %s", e)

    def _write_report(self):
        """Human/Devin-readable report."""
        lines = [
            "# Self-Improvement Report",
            f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"Improvement score: **{self.improvement_score}**",
            f"Flow conditions tracked: {len(self.flow_outcomes)}",
            "",
            "## Strategy standings",
        ]
        for s in ("grid", "dip", "scalp"):
            t = self.rl.strategy_trades.get(s, 0)
            w = self.rl.strategy_wins.get(s, 0)
            p = self.rl.strategy_total_pnl.get(s, 0.0)
            lines.append(f"- **{s}**: {t} trades, {w/t:.0%} WR, {p:+.2f}% total"
                         if t else f"- **{s}**: no trades yet")
        lines += ["", "## Open suggestions"]
        for s in self.last_suggestions:
            lines.append(f"- [{s['severity'].upper()}] `{s['target']}` — {s['detail']}")
        try:
            with open(REPORT_PATH, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except IOError:
            pass

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "cycle": self._cycle,
            "improvement_score": self.improvement_score,
            "flow_buckets": len(self.flow_outcomes),
            "open_suggestions": len(self.last_suggestions),
        }
