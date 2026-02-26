"""
v13pro/combo_promoter.py  —  Auto-promote / demote strategy combos

Reads shadow outcomes, computes rolling expectancy per strategy/TF combo
for LONG trades, and promotes combos with positive edge to LIVE_COMBOS
or demotes combos that have decayed below threshold.

Runs on a timer (default: hourly). Modifies cfg.LIVE_COMBOS in-memory
and persists state to combo_promoter_state.json.

Philosophy: ALL strategies stay in shadow trader forever. Only combos
with proven data-backed edge get promoted to live trading. Market
conditions change — today's shadow loser may be tomorrow's live winner.
"""

import asyncio
import glob
import json
import os
import time
from collections import defaultdict
from typing import Dict, Set, Tuple

from v13pro import config as cfg
from v13pro import logger as log

import logging
_log = logging.getLogger(__name__)

_STATE_FILE = os.path.join(cfg.BASE_DIR, "combo_promoter_state.json")
_SHADOW_DIR = os.path.join(cfg.LOG_DIR, "shadow")


def _load_shadow_longs() -> Dict[Tuple[str, str], list]:
    """Load all shadow outcomes, filter to completed LONG trades, group by (strategy, tf)."""
    files = sorted(glob.glob(os.path.join(_SHADOW_DIR, "shadow_*.jsonl")))
    combos: Dict[Tuple[str, str], list] = defaultdict(list)
    for f in files:
        try:
            for line in open(f, encoding="utf-8"):
                try:
                    rec = json.loads(line.strip())
                except (json.JSONDecodeError, ValueError):
                    continue
                # Only completed outcomes
                if rec.get("pnl_r") is None:
                    continue
                # Only longs (since LONG_ONLY_MODE)
                if rec.get("side", "").lower() != "long":
                    continue
                strat = rec.get("strategy", "?")
                tf = rec.get("tf", "?")
                combos[(strat, tf)].append(rec)
        except Exception:
            continue
    return combos


def compute_combo_stats(records: list) -> dict:
    """Compute stats for a list of shadow outcomes."""
    if not records:
        return {"n": 0, "wr": 0.0, "exp_r": 0.0, "total_r": 0.0}
    wins = sum(1 for r in records if r["pnl_r"] > 0)
    total_r = sum(r["pnl_r"] for r in records)
    return {
        "n": len(records),
        "wr": wins / len(records) * 100,
        "exp_r": total_r / len(records),
        "total_r": total_r,
        "wins": wins,
        "losses": len(records) - wins,
    }


def _load_state() -> dict:
    """Load persisted promoter state."""
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"promotions": [], "demotions": [], "last_review": 0}


def _save_state(state: dict):
    """Persist promoter state."""
    try:
        with open(_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        _log.warning(f"Failed to save promoter state: {e}")


def review_combos() -> dict:
    """
    Review all shadow combo performance and return promotion/demotion actions.
    
    Returns dict with:
      - promote: set of (strat, tf) combos to ADD to LIVE_COMBOS
      - demote: set of (strat, tf) combos to REMOVE from LIVE_COMBOS
      - stats: dict of (strat, tf) → stats for logging
    """
    combos = _load_shadow_longs()
    
    promote: Set[Tuple[str, str]] = set()
    demote: Set[Tuple[str, str]] = set()
    stats: Dict[Tuple[str, str], dict] = {}
    
    for key, records in combos.items():
        s = compute_combo_stats(records)
        stats[key] = s
        strat, tf = key
        is_live = (strat, tf) in cfg.LIVE_COMBOS
        
        if not is_live:
            # Check for promotion: shadow-only → live
            if (s["n"] >= cfg.SHADOW_PROMOTE_MIN_TRADES and 
                s["exp_r"] >= cfg.SHADOW_PROMOTE_MIN_EXPR):
                promote.add(key)
        else:
            # Check for demotion: live → shadow-only
            if (s["n"] >= cfg.SHADOW_DEMOTE_MIN_TRADES and 
                s["exp_r"] <= cfg.SHADOW_DEMOTE_MAX_EXPR):
                demote.add(key)
    
    return {"promote": promote, "demote": demote, "stats": stats}


def apply_review(review: dict) -> dict:
    """
    Apply promotion/demotion actions to LIVE_COMBOS.
    Returns summary of changes made.
    """
    changes = {"promoted": [], "demoted": []}
    
    for key in review["promote"]:
        strat, tf = key
        cfg.LIVE_COMBOS.add(key)
        s = review["stats"].get(key, {})
        log.info(f"  PROMOTED to live: {strat}/{tf} — "
                 f"N={s.get('n',0)} WR={s.get('wr',0):.1f}% "
                 f"ExpR={s.get('exp_r',0):+.3f}")
        changes["promoted"].append(f"{strat}/{tf}")
    
    for key in review["demote"]:
        strat, tf = key
        cfg.LIVE_COMBOS.discard(key)
        s = review["stats"].get(key, {})
        log.info(f"  DEMOTED to shadow: {strat}/{tf} — "
                 f"N={s.get('n',0)} WR={s.get('wr',0):.1f}% "
                 f"ExpR={s.get('exp_r',0):+.3f}")
        changes["demoted"].append(f"{strat}/{tf}")
    
    # Persist
    state = _load_state()
    state["last_review"] = time.time()
    state["live_combos"] = [list(k) for k in sorted(cfg.LIVE_COMBOS)]
    state["promotions"] = state.get("promotions", []) + [
        {"ts": time.time(), "action": "promote", "combo": c} for c in changes["promoted"]
    ]
    state["demotions"] = state.get("demotions", []) + [
        {"ts": time.time(), "action": "demote", "combo": c} for c in changes["demoted"]
    ]
    _save_state(state)
    
    return changes


async def promotion_loop():
    """Background task: review shadow combos periodically."""
    log.info("Combo promoter started — reviewing every "
             f"{cfg.SHADOW_REVIEW_INTERVAL}s")
    
    while True:
        try:
            await asyncio.sleep(cfg.SHADOW_REVIEW_INTERVAL)
            
            review = review_combos()
            n_promote = len(review["promote"])
            n_demote = len(review["demote"])
            
            if n_promote or n_demote:
                log.info(f"Combo review: {n_promote} promotions, {n_demote} demotions")
                changes = apply_review(review)
                if changes["promoted"]:
                    log.info(f"  Newly live: {', '.join(changes['promoted'])}")
                if changes["demoted"]:
                    log.info(f"  Now shadow-only: {', '.join(changes['demoted'])}")
            else:
                # Quiet log
                live_count = len(cfg.LIVE_COMBOS)
                shadow_count = len(review["stats"]) - live_count
                _log.debug(f"Combo review: no changes "
                          f"({live_count} live, {shadow_count} shadow-only)")
                
        except asyncio.CancelledError:
            break
        except Exception as e:
            _log.error(f"Combo promoter error: {e}", exc_info=True)
            await asyncio.sleep(60)


def print_status():
    """Print current combo status for CLI use."""
    combos = _load_shadow_longs()
    
    print("\n" + "=" * 70)
    print("  COMBO PROMOTER STATUS")
    print("=" * 70)
    
    live = []
    shadow = []
    
    for key, records in sorted(combos.items()):
        s = compute_combo_stats(records)
        strat, tf = key
        is_live = key in cfg.LIVE_COMBOS
        entry = (strat, tf, s, is_live)
        if is_live:
            live.append(entry)
        else:
            shadow.append(entry)
    
    print(f"\n  LIVE COMBOS ({len(live)}):")
    print(f"  {'Combo':<22} {'N':>4} {'WR%':>6} {'ExpR':>7} {'TotalR':>8} {'Status':<10}")
    print("  " + "-" * 60)
    for strat, tf, s, _ in sorted(live, key=lambda x: x[2]["exp_r"], reverse=True):
        status = "OK"
        if s["n"] >= cfg.SHADOW_DEMOTE_MIN_TRADES and s["exp_r"] <= cfg.SHADOW_DEMOTE_MAX_EXPR:
            status = "DEMOTING"
        print(f"  {strat}/{tf:<6} {s['n']:>4} {s['wr']:>5.1f}% {s['exp_r']:>+6.3f} {s['total_r']:>+7.1f} {status:<10}")
    
    print(f"\n  SHADOW-ONLY COMBOS ({len(shadow)}):")
    print(f"  {'Combo':<22} {'N':>4} {'WR%':>6} {'ExpR':>7} {'TotalR':>8} {'Status':<10}")
    print("  " + "-" * 60)
    for strat, tf, s, _ in sorted(shadow, key=lambda x: x[2]["exp_r"], reverse=True):
        status = "studying"
        if s["n"] >= cfg.SHADOW_PROMOTE_MIN_TRADES and s["exp_r"] >= cfg.SHADOW_PROMOTE_MIN_EXPR:
            status = "PROMOTING"
        elif s["n"] < cfg.SHADOW_PROMOTE_MIN_TRADES:
            status = f"need {cfg.SHADOW_PROMOTE_MIN_TRADES - s['n']}more"
        print(f"  {strat}/{tf:<6} {s['n']:>4} {s['wr']:>5.1f}% {s['exp_r']:>+6.3f} {s['total_r']:>+7.1f} {status:<10}")
    
    print()


if __name__ == "__main__":
    print_status()
