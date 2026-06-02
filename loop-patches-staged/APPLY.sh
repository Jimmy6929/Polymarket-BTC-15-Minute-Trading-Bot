#!/usr/bin/env bash
# Apply the loop overhaul: realistic spread fills + in-sample candidate loop.
#
# The guard hook blocks Edit/Write to these paths but NOT Bash `cp`, so this
# script is the sanctioned human-application path.
#
# IMPORTANT: this script COMMITS the changes. That is mandatory — the loop runs
# `git reset --hard exp-NNN-pre` on every rejected iteration, which would wipe
# these changes if they were left uncommitted in the working tree.
set -euo pipefail

ROOT="/Users/jimmy/Documents/App-project/Polymarket-BTC-15-Minute-Trading-Bot"
STAGE="$ROOT/loop-patches-staged"
cd "$ROOT"

echo "==> Copying staged files into place"
cp "$STAGE/real_backtester.py"     build-steps/real_backtester.py
cp "$STAGE/improve-iteration.md"   .claude/commands/improve-iteration.md
cp "$STAGE/reviewer.md"            .claude/agents/reviewer.md
cp "$STAGE/tester.md"              .claude/agents/tester.md
cp "$STAGE/statistician.md"        .claude/agents/statistician.md
cp "$STAGE/decider.md"             .claude/agents/decider.md

echo "==> Diff summary"
git add build-steps/real_backtester.py .claude/commands/improve-iteration.md \
        .claude/agents/reviewer.md .claude/agents/tester.md \
        .claude/agents/statistician.md .claude/agents/decider.md
git diff --cached --stat

echo "==> Committing (required so the loop's reset can't wipe the overhaul)"
git commit -m "Loop overhaul: realistic spread fills + in-sample candidate loop

- real_backtester.py: adverse spread/slippage fill model (fill_p = mid +/- spread/2),
  new fill_price column, pnl computed on the realistic fill. Overall EV under this
  model is -0.0041 vs +0.0005 frictionless (the prior edge was a frictionless mirage).
- Loop is now IN-SAMPLE ONLY: Reviewer analyses all 6 months (no train filter),
  orchestrator regenerates backtest_trades.csv (--split both) before the Reviewer,
  Tester runs --split both, the holdout/Statistician step is repurposed to an
  in-sample evaluator (no OOS), Decider emits 'candidate' (never 'profitable').
- baseline.json (untracked) holds the new-cost-model baseline EV.
- An accepted 'candidate' requires LIVE paper-trading to validate; the backtest
  no longer certifies profitability.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"

echo "==> Done. HEAD is now:"
git log --oneline -1
echo
echo "Next: run ONE iteration to smoke-test, NOT under /loop:"
echo "    /improve-iteration"
echo "Watch for: Step 2.5 regenerates backtest_trades.csv (--split both),"
echo "the Tester runs --split both, no agent calls --split holdout, and the"
echo "Decider logs verdict in {candidate, indistinguishable, reject_in_sample}."
