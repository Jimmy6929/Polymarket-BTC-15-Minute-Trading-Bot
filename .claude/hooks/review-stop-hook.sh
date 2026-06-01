#!/usr/bin/env bash
# Stop hook for the Polymarket BTC-15m project's self-review loop.
# Reads stdin (JSON Stop event from Claude Code), decides whether to allow
# Claude to finish the turn or to block-and-inject a directive to run /review.
#
# Allow stop when ANY of:
#   1. No *.py code changed in the working tree.
#   2. The user's last message contained the [skip-review] sentinel.
#   3. .claude/state/last-review.json shows verdict PASS for the CURRENT diff.
#   4. .claude/state/review-attempts.json shows attempts >= 2 (already escalated).
#   5. Circuit breaker: this exact diff has already been blocked 3 times
#      (guards against an infinite loop if /review ever fails to write state).
# Otherwise -> block stop and inject the "run /review" directive.
#
# Contract: exit 0 + empty stdout = allow stop. exit 0 + JSON
# {"decision":"block","reason":"..."} = block stop and feed reason to Claude.

set -uo pipefail

REPO_ROOT="/Users/jimmy/Documents/App-project/Polymarket-BTC-15-Minute-Trading-Bot"
STATE_DIR="$REPO_ROOT/.claude/state"
LAST_REVIEW="$STATE_DIR/last-review.json"
ATTEMPTS_FILE="$STATE_DIR/review-attempts.json"
GATE_FILE="$STATE_DIR/review-gate.json"

mkdir -p "$STATE_DIR"

# Consume stdin so the caller doesn't see a SIGPIPE.
HOOK_INPUT="$(cat 2>/dev/null || echo '{}')"

allow_stop() { exit 0; }

block_stop() {
  local reason="$1"
  printf '{"decision":"block","reason":%s}\n' \
    "$(printf '%s' "$reason" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
  exit 0
}

read_json_field() {  # $1=file  $2=key  $3=default
  python3 -c 'import json,sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get(sys.argv[2], sys.argv[3]))
except Exception:
    print(sys.argv[3])' "$1" "$2" "$3" 2>/dev/null
}

cd "$REPO_ROOT" 2>/dev/null || allow_stop

# ---- Rule 1: any *.py changed in the working tree (excluding venv/.claude)? ----
CHANGED_CODE="$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null \
  | cut -c4- \
  | grep -E '\.py$' \
  | grep -vE '^(venv/|\.venv/|\.claude/)' \
  | head -5)"

if [ -z "$CHANGED_CODE" ]; then
  allow_stop
fi

# ---- Rule 2: [skip-review] sentinel in the last user message ----
TRANSCRIPT_PATH="$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys
try:
    print(json.loads(sys.stdin.read() or "{}").get("transcript_path",""))
except Exception:
    print("")' 2>/dev/null)"

if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
  LAST_USER="$(tail -200 "$TRANSCRIPT_PATH" 2>/dev/null | grep -E '"role":"user"' | tail -1)"
  if echo "$LAST_USER" | grep -qE '\[skip-review\]'; then
    allow_stop
  fi
fi

# ---- Current diff SHA (so a prior PASS can be detected as stale) ----
CURRENT_DIFF_SHA="$(git -C "$REPO_ROOT" diff HEAD 2>/dev/null | shasum -a 256 | awk '{print $1}')"

# ---- Rule 3: prior PASS for THIS exact diff -> allow ----
if [ -f "$LAST_REVIEW" ]; then
  LAST_VERDICT="$(read_json_field "$LAST_REVIEW" verdict "")"
  LAST_SHA="$(read_json_field "$LAST_REVIEW" diff_sha "")"
  if [ "$LAST_VERDICT" = "PASS" ] && [ "$LAST_SHA" = "$CURRENT_DIFF_SHA" ]; then
    allow_stop
  fi
fi

# ---- Rule 4: already escalated (attempts >= 2) -> allow, don't loop ----
if [ -f "$ATTEMPTS_FILE" ]; then
  ATTEMPTS="$(read_json_field "$ATTEMPTS_FILE" attempts 0)"
  if [ "${ATTEMPTS:-0}" -ge 2 ]; then
    allow_stop
  fi
fi

# ---- Rule 5: circuit breaker — same diff blocked 3x already -> allow ----
PRIOR_GATE_SHA="$(read_json_field "$GATE_FILE" diff_sha "")"
PRIOR_BLOCKS="$(read_json_field "$GATE_FILE" blocks 0)"
if [ "$PRIOR_GATE_SHA" = "$CURRENT_DIFF_SHA" ]; then
  NEW_BLOCKS=$(( ${PRIOR_BLOCKS:-0} + 1 ))
else
  NEW_BLOCKS=1
fi
if [ "$NEW_BLOCKS" -gt 3 ]; then
  allow_stop
fi
printf '{"diff_sha":"%s","blocks":%d}\n' "$CURRENT_DIFF_SHA" "$NEW_BLOCKS" > "$GATE_FILE"

# ---- Otherwise: block & direct ----
REASON="REVIEW REQUIRED before finishing.

You have uncommitted Python changes that have not passed the self-review loop. You must:

1. Run the /review slash command now. It will:
   - Invoke 3 specialized reviewers (Project Goal, Task Goal, Senior Engineer) in parallel
   - Run hard signals (ruff + scoped pytest on the changed files)
   - Aggregate the verdict and write .claude/state/last-review.json

2. If /review returns PASS, you may finish the turn.

3. If /review returns FAIL, address every item under 'Required changes' and re-run /review. After 2 consecutive FAILs the loop escalates to the user automatically.

4. To bypass this loop for trivial work (typo fixes, doc tweaks), the USER can include the sentinel [skip-review] in their request.

Do not attempt to finish the turn without doing this first."

block_stop "$REASON"
