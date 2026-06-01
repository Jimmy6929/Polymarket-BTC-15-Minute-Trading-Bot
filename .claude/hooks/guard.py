#!/usr/bin/env python3
"""Pre-tool-use guard for the Polymarket bot self-improvement loop.

Invoked by Claude Code as a PreToolUse hook on Edit / Write tool calls.
Reads PreToolUse JSON from stdin. Exit 0 = allow, exit 2 = block (with reason
printed to stderr — Claude Code surfaces this to the subagent).

Three classes of block:

  1. Path blocklist: the subagent tried to edit a file that's off-limits
     (its own agent definition, the orchestrator, the backtester / stats
     modules, raw data, the live-trading entry point, the persona file).

  2. Content blocklist: the proposed file content contains a pattern that
     would enable live trading (simulation=False, DRY_RUN=False) or touch
     the order-submission code path.

  3. Holdout single-use: only enforced separately by the Statistician
     reading holdout_lock.json. This guard does not see the calling
     subagent identity, so we trust the agent system prompts on that one.
"""
import json
import os
import re
import sys

PROJECT_ROOT = "/Users/jimmy/Documents/App-project/Polymarket-BTC-15-Minute-Trading-Bot"

# Path patterns (project-relative, regex). Any match blocks the edit.
#
# Note: `.claude/state/**` and `build-steps/data/runs/**` are deliberately
# NOT blocked — agents need to write hypothesis files, summaries, the
# experiment log, the loop status, and the per-iteration backtest CSVs there.
BLOCKLIST_PATTERNS = [
    r"^\.claude/agents/.*",                   # subagent definitions (never self-modify)
    r"^\.claude/commands/.*",                 # the orchestrator
    r"^\.claude/hooks/.*",                    # this guard script itself
    r"^\.claude/settings(\.local)?\.json$",   # the hook config
    r"^claude/.*\.md$",                       # the persona file (lowercase claude/)
    r"^15m_bot_runner\.py$",                  # live-trading entry point
    r"^execution/.*",                         # order placement, polymarket client, risk engine
    r"^build-steps/data/[^/]+\.(json|csv)$",  # raw top-level data files
    r"^build-steps/data/splits/.*",           # the immutable train/holdout split files
#     r"^build-steps/fetch_data\.py$",          # data fetcher (frozen)
    r"^build-steps/real_backtester\.py$",     # the measurement instrument
    r"^build-steps/stats/.*",                 # DSR module (frozen)
    r"^build-steps/PLAN\.md$",                # the project log (only humans edit)
]

# Content patterns. Any match in the proposed file content blocks the edit.
FORBIDDEN_CONTENT = [
    r"simulation\s*=\s*False",
    r"DRY_RUN\s*=\s*False",
    r"dry_run\s*=\s*False",
    r"def\s+_place_real_order\s*\(",          # touching the order-placement function definition
    r"self\.submit_order\s*\(",               # calling submit_order in new code
]


def normalize_path(p: str) -> str:
    """Return path relative to project root, with forward slashes.

    Crucially, only strip an explicit './' prefix — never strip arbitrary
    leading dots, because '.claude/' and 'claude/' are TWO DIFFERENT
    directories with different policies.
    """
    if not p:
        return ""
    if os.path.isabs(p):
        try:
            p = os.path.relpath(p, PROJECT_ROOT)
        except ValueError:
            return p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.replace("\\", "/")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        # Hook crashed parsing input — fail open (don't block the user).
        print(f"guard.py: stdin parse failed ({e}); allowing", file=sys.stderr)
        return 0

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    if not file_path:
        return 0  # nothing path-related to check

    rel = normalize_path(file_path)

    # Path blocklist
    for pat in BLOCKLIST_PATTERNS:
        if re.match(pat, rel):
            print(
                f"guard.py: BLOCKED — edit to '{rel}' matches blocklist pattern '{pat}'.\n"
                f"  This file is frozen for the self-improvement loop. If you "
                f"believe this is wrong, a human should review.",
                file=sys.stderr,
            )
            return 2

    # Content blocklist — check both `new_string` (Edit) and `content` (Write)
    candidates = [
        tool_input.get("new_string") or "",
        tool_input.get("content") or "",
    ]
    proposed = "\n".join(candidates)
    for pat in FORBIDDEN_CONTENT:
        if re.search(pat, proposed):
            print(
                f"guard.py: BLOCKED — proposed content contains forbidden pattern '{pat}'.\n"
                f"  Live-trading code paths and the order-submission function are off-limits "
                f"to the self-improvement loop. Simulation must stay True.",
                file=sys.stderr,
            )
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
