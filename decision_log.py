"""Loader + summary for the research decision log (`decisions.jsonl`).

`bot.py` writes an event-sourced, append-only JSONL during paper-trading-run:

    {"event": "decision",   "decision_id": "...", ...point-in-time snapshot...}
    {"event": "resolution", "decision_id": "...", ...realized outcome + counterfactual P&L...}

Each minute-13 window — executed AND skipped — produces one `decision` event with
its full feature vector + per-processor signals; a later `resolution` event records
what each choice WOULD have earned. This module joins the two streams into one flat
DataFrame so you can mine the trades the guards blocked, not just the ones taken.

Usage:
    from decision_log import load_decisions
    df = load_decisions()                # joined, deduped (latest decision per id)

    python decision_log.py               # print a one-shot summary table
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

DEFAULT_PATH = Path(__file__).resolve().parent / "decisions.jsonl"


def _read_events(path: Path) -> tuple[dict, dict]:
    """Return (decisions_by_id, resolutions_by_id). Latest event per id wins, so an
    'opened' that upgraded a prior 'skipped' (same window) is the one kept."""
    decisions: dict[str, dict] = {}
    resolutions: dict[str, dict] = {}
    if not path.exists():
        return decisions, resolutions
    with open(path, "r", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a torn final line from a crash mid-append
            did = ev.get("decision_id")
            if not did:
                continue
            if ev.get("event") == "decision":
                decisions[did] = ev
            elif ev.get("event") == "resolution":
                resolutions[did] = ev
    return decisions, resolutions


def _flatten(decision: dict, resolution: Optional[dict]) -> dict:
    """Flatten one joined decision (+ optional resolution) into a single row."""
    market = decision.get("market") or {}
    poly = decision.get("poly") or {}
    feats = decision.get("features") or {}
    fused = decision.get("fused") or {}
    row = {
        "decision_id": decision.get("decision_id"),
        "ts": decision.get("ts"),
        "slug": market.get("slug"),
        "sub_interval": market.get("sub_interval"),
        "seconds_into_window": market.get("seconds_into_window"),
        "action": decision.get("action"),
        "reason": decision.get("reason"),
        "direction": decision.get("direction"),
        "mid": poly.get("mid"),
        "bid": poly.get("bid"),
        "ask": poly.get("ask"),
        "half_spread": poly.get("half_spread"),
        "would_fill_long": poly.get("would_fill_long"),
        "would_fill_short": poly.get("would_fill_short"),
        "spot_btc": decision.get("spot_btc"),
        "fused_direction": fused.get("direction"),
        "fused_score": fused.get("score"),
        "fused_confidence": fused.get("confidence"),
        "n_signals": len(decision.get("signals") or []),
    }
    # Spread the point-in-time feature scalars out as their own columns.
    for k in ("deviation", "momentum", "volatility", "sentiment_score"):
        row[k] = feats.get(k)
    # Per-processor signal scores, one column each (NaN-friendly via .get).
    for s in (decision.get("signals") or []):
        src = s.get("source")
        if src:
            row[f"sig_{src}_score"] = s.get("score")
            row[f"sig_{src}_dir"] = s.get("direction")
    # Resolution / counterfactual columns.
    if resolution:
        row["resolved"] = True
        row["yes_won"] = resolution.get("yes_won")
        row["pnl_if_long"] = resolution.get("pnl_if_long")
        row["pnl_if_short"] = resolution.get("pnl_if_short")
        row["pnl_if_taken"] = resolution.get("pnl_if_taken")
    else:
        row["resolved"] = False
        row["yes_won"] = None
        row["pnl_if_long"] = None
        row["pnl_if_short"] = None
        row["pnl_if_taken"] = None
    return row


def load_decisions(path: Path | str = DEFAULT_PATH):
    """Load the decision log into a pandas DataFrame, one row per decision window,
    joined with its resolution (counterfactual P&L). Returns an empty DataFrame if
    the log does not exist yet."""
    import pandas as pd

    path = Path(path)
    decisions, resolutions = _read_events(path)
    rows = [_flatten(d, resolutions.get(did)) for did, d in decisions.items()]
    df = pd.DataFrame(rows)
    if not df.empty and "ts" in df:
        df = df.sort_values("ts").reset_index(drop=True)
    return df


def _summary(path: Path = DEFAULT_PATH) -> None:
    df = load_decisions(path)
    if df.empty:
        print(f"No decisions logged yet at {path}")
        return
    n = len(df)
    resolved = int(df["resolved"].sum())
    print(f"Decisions: {n}  ({resolved} resolved)  ·  source: {path}")
    print("\nBy action / reason:")
    print(df.groupby(["action", "reason"]).size().to_string())

    rdf = df[df["resolved"]].copy()
    if not rdf.empty:
        # Counterfactual: what WOULD each window have earned if you'd always taken
        # the trend-implied side (long if mid>0.5 else short)? This is the headline
        # research number — including the windows the guards blocked.
        import numpy as np
        side_pnl = np.where(rdf["mid"].astype(float) > 0.5, rdf["pnl_if_long"], rdf["pnl_if_short"])
        side_pnl = [p for p in side_pnl if p is not None]
        if side_pnl:
            tot = float(sum(side_pnl))
            print(f"\nCounterfactual (always trade trend side), {len(side_pnl)} resolved windows:")
            print(f"  total P&L: ${tot:+.4f}   avg/window: ${tot/len(side_pnl):+.4f}")
        # What you actually executed vs what the guards BLOCKED would have earned.
        executed = rdf[(rdf["action"] == "opened") & rdf["pnl_if_taken"].notna()]
        if not executed.empty:
            ex = float(executed["pnl_if_taken"].sum())
            print(f"  executed P&L: ${ex:+.4f} over {len(executed)} taken")
        blocked = rdf[(rdf["action"] == "skipped") & rdf["pnl_if_taken"].notna()]
        if not blocked.empty:
            bk = float(blocked["pnl_if_taken"].sum())
            print(f"  blocked-would-be P&L: ${bk:+.4f} over {len(blocked)} skipped (directional) "
                  f"— what the guards saved/cost you")


if __name__ == "__main__":
    _summary()
