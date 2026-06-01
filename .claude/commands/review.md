---
description: Run the 3-reviewer + hard-signals review loop on the current uncommitted diff. Aggregates verdicts, manages a retry counter at .claude/state/review-attempts.json, and on the 2nd consecutive FAIL writes an escalation report and surfaces it to the user.
---

# /review — Self-Review Loop Coordinator

You are running the **review coordinator** for the Polymarket BTC-15m project's self-review loop. This is a manual entry point — invoke it after making code changes and before reporting "done".

It is independent of the self-improvement loop (the `reviewer/researcher/implementer/tester/statistician/decider` agents). Those judge backtests; these three judge *your uncommitted code*.

The flow you must execute, in order:

---

## Step 1 — Detect the diff

Run:
```bash
git status --porcelain && git diff HEAD --stat
```

If there are **no code changes** (only docs/markdown, only `.gitignore`, only files under `.claude/state/`, or no diff at all):
- Output a single line: `✓ /review: nothing to review (no code changes).`
- Stop. Do NOT invoke reviewers or hard signals.

Otherwise, continue.

---

## Step 2 — Fan out to all 3 reviewers + hard signals IN PARALLEL

In a **single message with multiple tool calls**, do the following four things concurrently:

1. `Agent` with `subagent_type: "reviewer-project-goal"` — prompt: *"Review the current uncommitted diff against your role. The user's most recent request is summarized as: <one-sentence summary of what the user asked for>. Read CLAUDE.md, README.md, and the diff (git diff HEAD). Output your verdict in the required format."*

2. `Agent` with `subagent_type: "reviewer-task-goal"` — prompt: *"Review the current uncommitted diff against your role. The user's most recent request is: <one-sentence summary of what the user asked for>. Judge whether the diff does that — no more, no less. Read the diff (git diff HEAD) and recent commits. Output your verdict in the required format."*

3. `Agent` with `subagent_type: "reviewer-senior-engineer"` — prompt: *"Review the current uncommitted diff against your role. Read CLAUDE.md and the diff (git diff HEAD). Apply the senior-staff-engineer bar. Output your verdict in the required format."*

4. `Bash` — scoped hard signals for Python (ruff + pytest on changed files only):
   ```bash
   cd /Users/jimmy/Documents/App-project/Polymarket-BTC-15-Minute-Trading-Bot || exit 1
   CHANGED_PY=$(git diff --name-only HEAD | grep -E '\.py$' | grep -vE '^(venv|\.venv)/')
   if [ -z "$CHANGED_PY" ]; then
     echo "RUFF SKIPPED (no .py changes)"; echo "PYTEST SKIPPED (no .py changes)"
   else
     # --- ruff on changed files (real exit code, not the pipe's) ---
     ruff check $CHANGED_PY > /tmp/review_ruff.out 2>&1; RUFF_EXIT=$?
     tail -40 /tmp/review_ruff.out; echo "---RUFF EXIT $RUFF_EXIT---"
     # --- pytest: changed test files + any test_*.py beside changed source ---
     TARGETS=""
     for f in $CHANGED_PY; do
       case "$(basename "$f")" in
         test_*.py|*_test.py) [ -f "$f" ] && TARGETS="$TARGETS $f" ;;
         *) d=$(dirname "$f"); for t in "$d"/test_*.py "$d"/*_test.py; do [ -f "$t" ] && TARGETS="$TARGETS $t"; done ;;
       esac
     done
     TARGETS=$(echo $TARGETS | tr ' ' '\n' | sort -u | tr '\n' ' ' | sed 's/^ *//; s/ *$//')
     if [ -n "$TARGETS" ]; then
       PYTHONDONTWRITEBYTECODE=1 pytest $TARGETS -x -q -p no:cacheprovider --ignore=venv > /tmp/review_pytest.out 2>&1; PYTEST_EXIT=$?
       tail -40 /tmp/review_pytest.out; echo "---PYTEST EXIT $PYTEST_EXIT---"
     else
       echo "PYTEST SKIPPED (no touched test files)"
     fi
   fi
   ```

**All 4 calls go in a single message** so they execute in parallel — this keeps review latency low.

Exit-code interpretation for the hard signals:
- **ruff**: `0` = pass, anything else = fail.
- **pytest**: `0` = pass, `5` = no tests collected (treat as **skipped**, not fail), anything else = fail.

---

## Step 3 — Aggregate the verdict

- **Overall PASS** = all 3 reviewers returned `VERDICT: PASS` AND ruff passed (or skipped) AND pytest passed (or skipped).
- **Overall FAIL** = any reviewer FAIL OR any hard-signal failure.

The hard signals are **non-overridable**: a ruff/pytest failure forces overall FAIL even if all 3 reviewers PASS. This guards against same-model sycophancy.

---

## Step 4 — Update the attempt counter

Read `.claude/state/review-attempts.json`. Structure:
```json
{"task_id": "<sha256 of user's most recent request, first 8 chars>", "attempts": N, "updated_at": "<ISO8601>"}
```

If the file doesn't exist, treat `attempts` as 0 and `task_id` as unmatched.

If `task_id` matches the current task -> use its `attempts` count.
If it doesn't match -> new task, reset to `attempts: 0`.

On PASS: delete the file (counter cleared).
On FAIL: increment `attempts` and write back. If `attempts >= 2`, jump to Step 6 (escalation).

---

## Step 5 — Write the result file

Write `.claude/state/last-review.json`:
```json
{
  "verdict": "PASS" | "FAIL",
  "attempt": N,
  "timestamp": "<ISO8601 UTC>",
  "diff_sha": "<git hash of the working tree diff>",
  "task_id": "<same as counter>",
  "reviewers": {
    "project_goal": "PASS" | "FAIL",
    "task_goal": "PASS" | "FAIL",
    "senior_engineer": "PASS" | "FAIL"
  },
  "hard_signals": {
    "ruff": "pass" | "fail" | "skipped",
    "pytest": "pass" | "fail" | "skipped"
  }
}
```

---

## Step 6 — Output

### If PASS:
```
✓ /review: PASS (attempt N)

1. Project Goal: PASS — <one-line reasoning>
2. Task Goal: PASS — <one-line reasoning>
3. Senior Engineer: PASS — <one-line reasoning>
Hard signals: ruff ✓ · pytest ✓
```

### If FAIL and attempts < 2:
```
✗ /review: FAIL (attempt N of 2)

### 1. Project Goal: PASS | FAIL
<full reasoning from reviewer>

### 2. Task Goal: PASS | FAIL
<full reasoning from reviewer>

### 3. Senior Engineer: PASS | FAIL
<full reasoning from reviewer>

### Hard signals
- ruff: pass | fail | skipped (<head of failure output if any>)
- pytest: pass | fail | skipped (<head of failure output if any>)

### Required changes
- [ ] <every required-change line from every reviewer, deduplicated>

I will now address these and re-run review.
```

Then **act on the required changes** — fix each one, then automatically re-invoke `/review` to retry. Do NOT report "done" to the user until either PASS or escalation.

### If FAIL and attempts >= 2:
Jump to Step 7 (escalation).

---

## Step 7 — Escalation (only on 2nd consecutive FAIL)

1. Compute timestamp slug: `date -u +%Y-%m-%dT%H-%M-%SZ`.
2. Compute task slug from the user's most recent request (kebab-case, max 40 chars).
3. Write `.claude/state/review-escalations/<timestamp>-<slug>.md`:

```markdown
# Escalation: <slug>

**Timestamp**: <ISO8601 UTC>
**Task ID**: <task_id>
**Branch**: <current branch>

## Original user request
<verbatim or one-paragraph summary>

## Diff under review
\`\`\`diff
<output of git diff HEAD; truncate to 200 lines if longer>
\`\`\`

## Attempt 1 verdict
<full structured report from attempt 1, all 3 reviewers + hard signals>

## Attempt 2 verdict
<full structured report from attempt 2>

## What changed between attempts
<diff summary of what was tried between attempt 1 and 2>

## Human, please clarify
<one short paragraph: the specific ambiguity, missing context, or design decision the reviewers cannot resolve without your input>
```

4. Append one line to `.claude/state/review-lessons.md`:
```
- <YYYY-MM-DD> · <slug> · 2x review fail · see .claude/state/review-escalations/<filename>
```

5. Print to chat:
```
⚠ /review: ESCALATED after 2 failed attempts.

I tried twice and could not satisfy the reviewers. Wrote a full report to:
  .claude/state/review-escalations/<timestamp>-<slug>.md

Summary of the blocker:
<3-sentence summary of what couldn't be resolved>

I am stopping here. Please review and clarify.
```

6. **Stop**. Do not retry a third time. Do not silently continue.

---

## Notes

- The three reviewer subagents live at `.claude/agents/review/reviewer-*.md`. Their system prompts are editable (via a route that isn't blocked by the loop guard — see CLAUDE.md / the guard hook); tune one without touching the others.
- The reviewers have **no Write/Edit access**, so they cannot "fix" the diff — only judge it. You (the coder) act on their feedback.
- This review loop is separate from, and does not invoke, the self-improvement loop agents.
