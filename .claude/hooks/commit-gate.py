#!/usr/bin/env python3
"""PreToolUse(Bash) gate: require /review PASS before Claude commits Python code.

Replaces the old per-turn Stop hook. This fires ONLY when Claude is about to run a
`git commit` that includes Python files — so planning/discussion turns, other sessions'
uncommitted files, and the autonomous loop's EXP-NNN commits are never gated.

Contract: read PreToolUse JSON from stdin. exit 0 = allow. exit 2 = block (stderr shown
to Claude). Fail OPEN on any internal error — this is a helpful nudge, not the safety
boundary (guard.py is). The real live-trading guard is unaffected.

Allow when ANY of:
  - the command is not a `git commit`
  - the commit includes no *.py (outside venv/.claude)
  - commit message matches the loop's decider pattern EXP-<n>  (loop must not be gated)
  - `--no-verify` is present, or `[skip-review]` is in the commit message
  - last-review.json shows verdict PASS whose diff_sha is a prefix of the current
    `git diff HEAD` sha
Otherwise: block with a directive to run /review first.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

PROJECT_ROOT = "/Users/jimmy/Documents/App-project/Polymarket-BTC-15-Minute-Trading-Bot"
LAST_REVIEW = os.path.join(PROJECT_ROOT, ".claude/state/last-review.json")
EXCLUDE_RE = re.compile(r"^(venv/|\.venv/|\.claude/)")
EXP_RE = re.compile(r"\bEXP-\d+", re.IGNORECASE)
GLOBAL_OPTS_TAKING_ARG = {"-c", "-C", "--git-dir", "--work-tree", "--namespace", "--exec-path"}


def allow() -> int:
    return 0


def git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10
        )
        return out.stdout
    except Exception:
        return ""


def segments(command: str) -> list[str]:
    """Split a shell command on &&, ||, ;, | into rough segments."""
    return re.split(r"&&|\|\||;|\|", command)


def is_git_commit(tokens: list[str]) -> bool:
    """True if tokens are a `git [global-opts] commit ...` invocation."""
    if "git" not in tokens:
        return False
    i = tokens.index("git") + 1
    while i < len(tokens):
        t = tokens[i]
        if t in GLOBAL_OPTS_TAKING_ARG:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return t == "commit"
    return False


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return allow()  # fail open

    command = (payload.get("tool_input") or {}).get("command") or ""
    if "commit" not in command:
        return allow()

    # Find the git-commit segment and tokenize it.
    commit_tokens: list[str] = []
    for seg in segments(command):
        try:
            toks = shlex.split(seg)
        except Exception:
            continue
        if is_git_commit(toks):
            commit_tokens = toks
            break
    if not commit_tokens:
        return allow()

    # --- exemptions from flags / message -------------------------------------
    includes_all = False
    msg_parts: list[str] = []
    i = 0
    while i < len(commit_tokens):
        t = commit_tokens[i]
        if t == "--no-verify":
            return allow()
        if t in ("-a", "--all"):
            includes_all = True
        elif re.fullmatch(r"-[a-zA-Z]+", t) and "a" in t[1:]:
            includes_all = True  # combined short flags like -am
        if t in ("-m", "--message") and i + 1 < len(commit_tokens):
            msg_parts.append(commit_tokens[i + 1])
            i += 2
            continue
        if t.startswith("--message="):
            msg_parts.append(t.split("=", 1)[1])
        i += 1
    message = " ".join(msg_parts)
    if EXP_RE.search(message):
        return allow()  # autonomous loop commit
    if "[skip-review]" in message:
        return allow()

    # --- which files does this commit touch? ---------------------------------
    files = set(git("diff", "--cached", "--name-only").split())
    if includes_all:
        files |= set(git("diff", "--name-only").split())
    py = [f for f in files if f.endswith(".py") and not EXCLUDE_RE.match(f)]
    if not py:
        return allow()  # no python in this commit

    # --- require a matching PASS ---------------------------------------------
    current_sha = ""
    try:
        import hashlib

        current_sha = hashlib.sha256(
            git("diff", "HEAD").encode("utf-8", "replace")
        ).hexdigest()
    except Exception:
        return allow()  # fail open

    try:
        with open(LAST_REVIEW) as fh:
            lr = json.load(fh)
        stored_sha = str(lr.get("diff_sha", ""))
        if lr.get("verdict") == "PASS" and stored_sha and current_sha.startswith(stored_sha):
            return allow()
    except Exception:
        pass  # no/var last-review -> block below

    sys.stderr.write(
        "COMMIT BLOCKED — Python changes in this commit have not passed /review.\n\n"
        "Run the /review slash command. If it returns PASS, re-run this commit.\n"
        "Python files in this commit:\n  - " + "\n  - ".join(sorted(py)) + "\n\n"
        "Bypass for trivial changes: add [skip-review] to the commit message, or use "
        "--no-verify. (Autonomous-loop EXP-<n> commits are exempt automatically.)\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
