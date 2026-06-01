#!/usr/bin/env python3
"""Read-only branch-health reporter for the Polymarket BTC-15m project.

Single source of truth for the /branches command, the branch-manager subagent, and the
SessionStart auto-surface. It NEVER mutates: no fetch, push, prune-without-dry-run, -D,
--force. Suggested deletions are copy-paste `git branch -d` (the SAFE form, which itself
refuses unmerged work) that the USER runs — this script does not delete anything.

Modes:
    branch_report.py --json            structured report (consumed by the subagent)
    branch_report.py --human           full categorized text report
    branch_report.py --session-hook    <=3-line summary for SessionStart, ONLY if flagged

Buckets per local branch:
    PROTECTED        main / current branch / configured PROTECTED list — never deleted
    SAFE_TO_DELETE   fully merged into main, not protected
    PROBLEM          unmerged + (behind main / local-only / unpushed / gone upstream / stale)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__)).rsplit("/.claude", 1)[0]

# --- tunables ---------------------------------------------------------------
MAIN_REF = "main"
PROTECTED = {"main", "master"}          # never suggested for deletion
STALE_DAYS = 90                          # GitHub's stale definition
UPSTREAM_REMOTE = "upstream"             # the forked-from remote, if any
SEP = "@@FS@@"                           # field separator for for-each-ref (printable:
#                                          NOT \x1f — Python str.strip() eats \x1f as
#                                          whitespace and would drop a trailing empty field)


def git(*args: str) -> str:
    try:
        r = subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=15
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _count_pair(rev_range: str) -> tuple[int, int]:
    """`git rev-list --left-right --count A...B` -> (left, right)."""
    out = git("rev-list", "--left-right", "--count", rev_range)
    try:
        left, right = out.split()
        return int(left), int(right)
    except Exception:
        return 0, 0


def classify_branch(b: dict, current: str, main_ref: str, protected: set, merged: set,
                    stale_days: int) -> tuple[str, list[str]]:
    """Pure classifier. Returns (bucket, reasons)."""
    name = b["name"]
    reasons: list[str] = []

    # annotations that apply regardless of bucket
    if b["unpushed"] > 0:
        reasons.append(f"{b['unpushed']} unpushed commit(s) — push or work is local-only")
    if b["upstream"] is None:
        reasons.append("no upstream — local-only, not backed up to a remote")
    if b["gone"]:
        reasons.append("upstream is gone (deleted on remote)")
    if b["behind"] > 0:
        reasons.append(f"{b['behind']} commit(s) behind {main_ref} — rebase candidate")
    if b["age_days"] is not None and b["age_days"] > stale_days:
        reasons.append(f"stale: no commit in {b['age_days']}d (>{stale_days}d)")

    if name in protected or name == main_ref or name == current:
        return "PROTECTED", reasons
    if name in merged:
        return "SAFE_TO_DELETE", reasons or [f"fully merged into {main_ref}"]
    return "PROBLEM", reasons or [f"not merged into {main_ref}; diverged by {b['ahead']} ahead"]


def gather() -> dict:
    current = git("rev-parse", "--abbrev-ref", "HEAD")
    merged = {ln.strip(" *") for ln in git("branch", "--merged", MAIN_REF).splitlines() if ln.strip()}
    now = time.time()

    fmt = SEP.join(["%(refname:short)", "%(committerdate:unix)", "%(authorname)",
                    "%(upstream:short)", "%(upstream:track)"])
    branches: list[dict] = []
    for line in git("for-each-ref", "--sort=-committerdate", "refs/heads/",
                    f"--format={fmt}").splitlines():
        if not line.strip():
            continue
        parts = line.split(SEP)
        parts += [""] * (5 - len(parts))  # pad: a trailing empty field (e.g. no track) is fine
        name, cdate, author, upstream, track = parts[0], parts[1], parts[2], parts[3], parts[4]
        try:
            age_days = int((now - int(cdate)) // 86400) if cdate else None
        except Exception:
            age_days = None
        behind, ahead = _count_pair(f"{MAIN_REF}...{name}") if name != MAIN_REF else (0, 0)
        unpushed = 0
        if upstream:
            try:
                unpushed = int(git("rev-list", "--count", f"{upstream}..{name}") or 0)
            except Exception:
                unpushed = 0
        b = {
            "name": name, "ahead": ahead, "behind": behind,
            "merged": name in merged, "upstream": upstream or None,
            "unpushed": unpushed, "gone": "gone" in track,
            "last_commit_unix": int(cdate) if cdate.isdigit() else None,
            "age_days": age_days, "author": author,
        }
        bucket, reasons = classify_branch(b, current, MAIN_REF, PROTECTED, merged, STALE_DAYS)
        b["bucket"], b["reasons"] = bucket, reasons
        branches.append(b)

    # --- repo-level signals --------------------------------------------------
    remotes = set(git("remote").split())
    upstream_drift = None
    if UPSTREAM_REMOTE in remotes:
        behind, ahead = _count_pair(f"{MAIN_REF}...{UPSTREAM_REMOTE}/{MAIN_REF}")
        if behind or ahead:
            upstream_drift = {"behind": behind, "ahead": ahead,
                              "ref": f"{UPSTREAM_REMOTE}/{MAIN_REF}"}
    gone_remotes = []
    if "origin" in remotes:
        for ln in git("remote", "prune", "origin", "--dry-run").splitlines():
            ln = ln.strip()
            if ln.startswith("*") and "would prune" in ln:
                gone_remotes.append(ln.split()[-1])
    exp_tags = [t for t in git("tag", "--list", "exp-*").splitlines() if t.strip()]
    dirty = bool(git("status", "--porcelain"))
    current_protected = current in PROTECTED or current == MAIN_REF
    suggest_new_branch = dirty and current_protected

    return {
        "current": current, "main_ref": MAIN_REF, "branches": branches,
        "signals": {
            "upstream_drift": upstream_drift,
            "gone_remotes": gone_remotes,
            "exp_tags": len(exp_tags),
            "dirty": dirty,
            "suggest_new_branch": suggest_new_branch,
        },
    }


def render_human(rep: dict) -> str:
    L: list[str] = []
    by = {"PROBLEM": [], "SAFE_TO_DELETE": [], "PROTECTED": []}
    for b in rep["branches"]:
        by[b["bucket"]].append(b)
    L.append(f"Branch health — current: {rep['current']}  (baseline: {rep['main_ref']})")
    L.append("")

    if by["SAFE_TO_DELETE"]:
        L.append("SAFE TO DELETE (merged into %s):" % rep["main_ref"])
        for b in by["SAFE_TO_DELETE"]:
            L.append(f"  • {b['name']}  ({b['age_days']}d old)  — git branch -d {b['name']}")
        L.append("")
    else:
        L.append("SAFE TO DELETE: none.")
        L.append("")

    if by["PROBLEM"]:
        L.append("PROBLEM (review before acting):")
        for b in by["PROBLEM"]:
            L.append(f"  • {b['name']}  [+{b['ahead']}/-{b['behind']} vs {rep['main_ref']}]")
            for r in b["reasons"]:
                L.append(f"      - {r}")
        L.append("")

    L.append("PROTECTED (never auto-deleted): " +
             ", ".join(b["name"] for b in by["PROTECTED"]))
    notes = [f"{b['name']}: {'; '.join(b['reasons'])}" for b in by["PROTECTED"] if b["reasons"]]
    for n in notes:
        L.append(f"  - {n}")
    L.append("")

    s = rep["signals"]
    L.append("Repo signals:")
    if s["upstream_drift"]:
        d = s["upstream_drift"]
        L.append(f"  • {rep['main_ref']} is {d['behind']} behind {d['ref']} (drift from source).")
    if s["gone_remotes"]:
        L.append(f"  • stale remote refs (run `git remote prune origin`): {', '.join(s['gone_remotes'])}")
    if s["exp_tags"]:
        L.append(f"  • {s['exp_tags']} exp-* tags accumulated (loop checkpoints; clutter candidate).")
    if s["suggest_new_branch"]:
        L.append(f"  • uncommitted work on protected branch '{rep['current']}' — consider a feature branch.")
    if not any([s["upstream_drift"], s["gone_remotes"], s["exp_tags"], s["suggest_new_branch"]]):
        L.append("  • none.")
    return "\n".join(L)


def render_session(rep: dict) -> str:
    """<=3-line summary, only when something is worth flagging. Empty string = stay silent."""
    by_problem = [b for b in rep["branches"] if b["bucket"] == "PROBLEM"]
    safe = [b for b in rep["branches"] if b["bucket"] == "SAFE_TO_DELETE"]
    # work-at-risk on any branch (incl. the protected/current one): unpushed or no upstream
    at_risk = [b for b in rep["branches"] if b["unpushed"] > 0 or b["upstream"] is None]
    s = rep["signals"]
    flagged = (safe or by_problem or at_risk or s["upstream_drift"]
               or s["gone_remotes"] or s["suggest_new_branch"])
    if not flagged:
        return ""
    bits = [f"{len(rep['branches'])} branches"]
    bits.append(f"{len(safe)} safe-to-delete" if safe else "0 safe-to-delete")
    if by_problem:
        bits.append("⚠ " + ", ".join(b["name"] for b in by_problem[:3]))
    line1 = "⎇ branch-health: " + " · ".join(bits)
    extra = []
    for b in at_risk:
        if b["unpushed"] > 0:
            extra.append(f"{b['name']}: {b['unpushed']} unpushed")
        elif b["upstream"] is None:
            extra.append(f"{b['name']}: local-only (no upstream)")
    if s["upstream_drift"]:
        extra.append(f"{rep['main_ref']} {s['upstream_drift']['behind']} behind {s['upstream_drift']['ref']}")
    if s["gone_remotes"]:
        extra.append(f"{len(s['gone_remotes'])} stale remote ref(s)")
    if s["suggest_new_branch"]:
        extra.append(f"uncommitted work on protected '{rep['current']}'")
    out = [line1]
    if extra:
        out.append("  " + " · ".join(extra))
    out.append("  (run /branches for the full report)")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--human"
    try:
        rep = gather()
    except Exception as e:  # fail-open: never break a session or a command
        if mode == "--session-hook":
            return 0
        print(f"branch_report: could not gather git state: {e!r}", file=sys.stderr)
        return 0
    if mode == "--json":
        print(json.dumps(rep, indent=2))
    elif mode == "--session-hook":
        out = render_session(rep)
        if out:
            print(out)  # plain stdout -> added to Claude's context at SessionStart
    else:
        print(render_human(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
