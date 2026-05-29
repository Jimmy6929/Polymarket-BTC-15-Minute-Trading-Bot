---
description: Run one iteration of the self-improvement loop. Regenerates the full-history backtest, then sequentially invokes reviewer → researcher → implementer → tester → evaluator → decider. IN-SAMPLE ONLY (no holdout) — accepted changes are candidates pending live validation. Stops early if any agent emits a halt signal. Intended to be invoked under /loop for autonomous operation.
allowed-tools: Bash, Read, Agent
argument-hint: "(no arguments)"
---

You are the **orchestrator** for one iteration of the autonomous self-improvement loop on the Polymarket BTC 15-min trading bot. You delegate every substantive decision to a specialised subagent. You do not formulate hypotheses, you do not write code, you do not commit. You shepherd state.

**This loop is IN-SAMPLE ONLY.** There is no holdout split; the Reviewer analyses all 6 months and the evaluation runs on the same full history. An "accept" therefore means **candidate pending live validation**, never "validated" or "profitable." The only true out-of-sample test is forward paper-trading on Polymarket (out of this loop's scope). The one backtester run you perform yourself (Step 2.5) is a deterministic baseline regeneration; you still never run ad-hoc backtests beyond that.

# Step 0 — Honour the stop signal

```bash
if [ -s .claude/state/loop_status ]; then
  echo "improve-iteration: loop_status is non-empty: $(cat .claude/state/loop_status)"
  echo "Refusing to run. Clear .claude/state/loop_status to resume."
  exit 0
fi
```

If `loop_status` has content, halt. Print `halted: <reason>` and exit. Do NOT spawn any agents.

# Step 1 — Compute the iteration number

```bash
NNN=$(printf "%03d" $(($(wc -l < .claude/state/experiments.jsonl) + 1)))
echo "improve-iteration: starting EXP-$NNN"
```

Remember `NNN` for downstream prints. The subagents compute it themselves from the same source, but you use it for status lines and tags.

# Step 2 — Git checkpoint

```bash
git tag "exp-${NNN}-pre" 2>/dev/null || git tag -f "exp-${NNN}-pre"
```

Use `-f` if the tag already exists (e.g. you're rerunning a stuck iteration manually).

# Step 2.5 — Regenerate the Reviewer's full-history backtest

Refresh `backtest_trades.csv` so the Reviewer always post-mortems the **current** strategy (the working tree is clean here — the previous Decider either committed an accept or reset to baseline), over all 6 months:

```bash
venv/bin/python build-steps/real_backtester.py --split both --out build-steps/data/backtest_trades.csv
```

This is the ONE backtester invocation the orchestrator performs. It is deterministic and unconditional every iteration (~47s; negligible vs the agents). If it exits non-zero, append a `crashed` row (stage_failed `regen`) and exit — do NOT run the Reviewer on a stale file.

# Step 3 — Invoke the six agents in sequence

Use the Agent tool. Each agent has a fixed brief; you pass the same minimal prompt to each.

**Critical rules:**

- After each agent returns, check that its expected output file exists at the path specified in the agent's definition. If missing, append a `crashed` row to `experiments.jsonl` and exit (do NOT continue down the chain).
- If an agent emits a sentinel (`NO_VIABLE_HYPOTHESIS`, `NO_CHANGE`, `crashed_on_train`), follow the abort logic for that sentinel (described below) — do not blindly continue.
- Do NOT pass agents' full output through the conversation context. Each agent reads inputs from disk and writes outputs to disk; you only pass the iteration number.

## 3a. Reviewer

```
Agent({
  subagent_type: "reviewer",
  description: "Iteration NNN: find a losing pattern and form a hypothesis",
  prompt: "Run as the reviewer for iteration {NNN}. Read .claude/state/experiments.jsonl in full; never re-attack a rejected hypothesis. Write .claude/state/iteration_scratch/{NNN}_hypothesis.md per your instructions. Print only your one-line summary."
})
```

After it returns:
- If `.claude/state/iteration_scratch/{NNN}_hypothesis.md` is missing → log crash row, exit.
- If file content starts with `# NO_VIABLE_HYPOTHESIS` → skip directly to **Step 4 (Decider)** with verdict `no_change`, reason `no_viable_hypothesis`.

## 3b. Researcher

```
Agent({
  subagent_type: "researcher",
  description: "Iteration NNN: research the hypothesis",
  prompt: "Run as the researcher for iteration {NNN}. Read {NNN}_hypothesis.md and search for evidence. Write {NNN}_research.md. Print only your one-line summary."
})
```

If `{NNN}_research.md` is missing after return → still continue (Implementer can work without research). Note the absence in the row.

## 3c. Implementer

```
Agent({
  subagent_type: "implementer",
  description: "Iteration NNN: make ONE targeted code change",
  prompt: "Run as the implementer for iteration {NNN}. Read the hypothesis and research, then make one change per your allowlist. Write {NNN}_diff.patch (or NO_CHANGE)."
})
```

After it returns:
- If `{NNN}_diff.patch` is missing or contains `NO_CHANGE` → skip to **Step 4** with verdict `no_change`.

## 3d. Tester

```
Agent({
  subagent_type: "tester",
  description: "Iteration NNN: run full-history backtest and compute in-sample stats",
  prompt: "Run as the tester for iteration {NNN}. Execute the backtester on --split both (full 6 months), write {NNN}_insample_summary.json."
})
```

After it returns:
- If `{NNN}_insample_summary.json` is missing → log crash row, exit.
- If summary `status == "crashed"` → skip to **Step 4** with verdict `crashed`. Do NOT revert (the Decider will leave git alone so we can diagnose).

## 3e. Evaluator (in-sample)

The `statistician` subagent has been repurposed as the **in-sample evaluator** — there is no holdout. It reads the Tester's full-history summary plus `.claude/state/baseline.json`, applies the in-sample lift + deflated-Sharpe gate, and emits a verdict.

```
Agent({
  subagent_type: "statistician",
  description: "Iteration NNN: in-sample evaluation (no holdout) + DSR deflation",
  prompt: "Run as the in-sample evaluator for iteration {NNN}. Read {NNN}_insample_summary.json, .claude/state/baseline.json, and the experiments log. Apply your in-sample verdict rules (NO holdout, NO --split holdout). Write {NNN}_eval_summary.json."
})
```

After it returns:
- If `{NNN}_eval_summary.json` is missing → log crash row, exit.

## 3f. Decider

```
Agent({
  subagent_type: "decider",
  description: "Iteration NNN: commit / revert and append to experiment log",
  prompt: "Run as the decider for iteration {NNN}. Read all the iteration artifacts. Execute the appropriate git operation. Append a JSON row to .claude/state/experiments.jsonl. Decide whether any stop condition is met and update loop_status accordingly."
})
```

# Step 4 — Print the iteration summary

After the Decider returns, print one line:

```
EXP-${NNN} <verdict> commit=<sha-or-none> stop=<true|false>
```

You can derive the verdict, sha, and stop flag from the last line of `experiments.jsonl`:

```bash
tail -1 .claude/state/experiments.jsonl | python3 -c "
import json, sys
row = json.loads(sys.stdin.read())
import os
status = os.path.exists('.claude/state/loop_status') and open('.claude/state/loop_status').read().strip()
print(f\"EXP-{row['iter']:03d} {row['verdict']} commit={row.get('commit_sha') or 'none'} stop={bool(status)}\")
"
```

# Failure path — crashed iteration

If you exit early (any agent's expected output missing), you must still leave the system consistent:

1. Append a minimal row to `experiments.jsonl`:
   ```json
   {"iter": NNN, "ts_utc": "<now>", "verdict": "crashed_orchestrator", "stage_failed": "<regen|reviewer|researcher|implementer|tester|evaluator|decider>", "git_action": "none"}
   ```
2. Do NOT update `loop_status`. The loop will keep trying — a transient failure should not auto-halt.
3. Print: `EXP-${NNN} crashed_orchestrator stage=<...>`.

# Constraints

- The **only** backtester run you perform is the deterministic Step 2.5 baseline regeneration (`--split both` → `backtest_trades.csv`). You never run any other backtest. The Tester runs the post-change full backtest; there is no holdout run anymore.
- You **never** edit code. Only the Implementer does.
- You **never** modify `experiments.jsonl` or `loop_status` directly. Only the Decider modifies them. (`holdout_lock.json` is obsolete — the loop no longer uses a holdout.)
- You **never** run `git commit`, `git reset`, or `git tag` other than the single `exp-{NNN}-pre` tag in Step 2. Decider handles all other git operations.

# Done

After Step 4, you are done with this iteration. Return control to the user (or the parent `/loop` invocation, which will call you again).
