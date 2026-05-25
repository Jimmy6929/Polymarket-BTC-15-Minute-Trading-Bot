# Patch set: add a "select" (trade-skipping) hypothesis path to the loop

**Why:** the loop currently validates a hypothesis by re-measuring the *targeted
cluster's* per-trade EV after the change, requiring `cluster_ev > 0`. A hypothesis
that *skips* the cluster's trades makes the cluster empty (n=0) → unfalsifiable →
always `reject_on_train` (this killed EXP-001). But on this strategy the losing
clusters are **intrinsically negative pre-fee** (EXP-002), so the only real remedy
*is* to skip them. This adds a `select` evaluation path so skip-hypotheses can be
validated honestly.

**Core principle that makes it sound:** a skip is a deterministic filter, and each
15-min market in `real_backtester.py` resolves independently of the others. So the
post-skip PnL series = the baseline per-trade CSV with the cohort rows removed —
*identical* to re-running the strategy with the cohort skipped. Therefore a `select`
hypothesis is evaluated by **filtering** baseline backtest CSVs, needing only **one**
holdout run (on baseline code, cohort present) to get both the cohort's OOS EV and
the post-skip overall EV.

These files are editable by you (the harness only hard-blocks Claude from editing
`.claude/agents/**`). Apply the five edits below.

---

## 1. `.claude/agents/reviewer.md`

### 1a. In the output template (the ```# Iteration {NNN} Hypothesis``` block), add a `Hypothesis type` line right after the `## Cluster filter ...` section:

```
## Hypothesis type
<modify | select>
# modify = change KEEPS the cluster's trades (resize / re-time / re-score); validated by in-cluster post-change EV.
# select = SKIP the cluster's trades entirely; validated by overall-EV lift + cohort being -EV out-of-sample + trade retention.
```

### 1b. For a `select` hypothesis, the Success metric must be defined on overall EV + retention, not in-cluster EV. Replace the `## Success metric` guidance line with:

```
## Success metric
<exact, measurable.
 - For `modify`: defined on the cluster's post-change trades (which still exist), e.g. "cluster mean PnL rises to >= $0".
 - For `select`: defined on OVERALL EV + retention, e.g. "skipping this cohort lifts overall train EV by >= +$0.003 AND retains >= 80% of total trades AND the cohort is EV-negative out-of-sample (holdout cohort CI upper < 0)".>
```

### 1c. Discipline rule 3 is unchanged in spirit (cohort must be statistically -EV with >=100 trades) — it already qualifies a `select` cohort. No "no-skip" prohibition exists in this file, so nothing to remove. (Any "prefer trade-modifying" steer in the orchestrator prompt should be dropped once this path exists.)

---

## 2. `.claude/agents/implementer.md`

Add to the discipline rules:

```
8. Read `## Hypothesis type` from the hypothesis file.
   - `modify`: change the cluster's trades in place (keep them in the backtest).
   - `select`: implement a clean SKIP for the cohort — return None / `continue` past
     entry for trades matching the cohort predicate, gated by an explicit, readable
     condition with the `# EXP-{NNN}` comment. The skip must be expressible by the
     same predicate as the hypothesis `Cluster filter` so the Statistician's
     analytic filter matches the code exactly. Do not also change sizing/scoring in
     the same patch (one concept).
```

(EXP-001 already demonstrated a clean skip in `signal_fusion.py`; that mechanic is correct — it was only the *evaluation* that couldn't score it.)

---

## 3. `.claude/agents/tester.md`

The train run for a `select` hypothesis will show cluster `n=0` (expected). The Tester
must additionally report baseline + retention numbers. Add to **What you compute**:

```
- If hypothesis type == "select", also read the BASELINE per-trade CSV
  `build-steps/data/backtest_trades.csv` (cohort still present) and compute:
    * baseline_overall_ev   = mean PnL over all baseline train trades
    * baseline_n            = baseline train trade count
    * cohort_baseline       = stats over baseline trades matching the cohort filter:
                              {n, mean_pnl, ci_95 (bootstrap, seed 42)}
    * retention             = (post-change n) / baseline_n
```

Add these to the train summary JSON (only for select; null/omit for modify):

```json
  "hypothesis_type": "select",
  "baseline_overall_ev": <float>,
  "baseline_n": <int>,
  "cohort_baseline": {"n": <int>, "mean_pnl": <float>, "ci_95": [<lo>,<hi>]},
  "retention": <float>
```

---

## 4. `.claude/agents/statistician.md`  (the substantive change)

Read `hypothesis_type` from the hypothesis/train summary. Branch the decision flow.
Keep the existing flow for `modify`. For `select`, use the following.

Define a constant near the top: `MAX_DROP = 0.20` (max fraction of total trades a skip
may remove; tune to taste).

### Step 1 (select) — train gate
Proceed to holdout ONLY if ALL hold (else `reject_on_train`):
- `train.overall_post_ev > train.baseline_overall_ev`  (skipping lifted overall EV)
- `train.cohort_baseline.ci_95[1] < 0`                  (cohort is genuinely -EV in-sample)
- `train.retention >= 1 - MAX_DROP`                     (didn't gut the strategy)

### Step 3 (select) — ONE holdout run, on BASELINE code, cohort present
The skip is deterministic, so run the holdout on the *unchanged* code and apply the
cohort filter analytically. Temporarily revert the implementer's change for the run:

```bash
# files the implementer touched, from the diff:
CHANGED=$(git diff --name-only)
git stash push -m "select-holdout-baseline" -- $CHANGED
venv/bin/python build-steps/real_backtester.py --split holdout --out build-steps/data/runs/{NNN}_holdout.csv
git stash pop
```
(If `stash pop` ever conflicts, prefer a throwaway `git worktree` at `exp-{NNN}-pre`
instead — nothing else writes these files mid-iteration, so a conflict shouldn't occur.)

Then update the holdout lock (as today) and compute from the holdout CSV:
- `holdout_overall_before` = mean PnL over ALL holdout trades
- `holdout_overall_after`  = mean PnL over holdout trades NOT in the cohort filter
- `holdout_cohort`         = {n, ev, ci_95} over holdout trades IN the cohort filter
- `holdout_retention`      = (count not in cohort) / (total)
- `dsr`                    = deflated_sharpe(post-skip PnL series = PnLs NOT in cohort, N)

### Step 5 (select) — verdict
| Condition | Verdict |
|---|---|
| `holdout_overall_after > holdout_overall_before` AND `holdout_cohort.ci_95[1] < 0` AND `dsr >= 0.95` AND `holdout_retention >= 1 - MAX_DROP` | **accept** (stop_gate_cleared if post-skip overall CI lower > 0) |
| `holdout_overall_after > holdout_overall_before` AND `holdout_cohort.ci_95[1] < 0` but `dsr < 0.95` | **indistinguishable** |
| `holdout_cohort.ci_95[1] >= 0` (cohort not reliably -EV OOS) | **reject_on_holdout** |
| else | **indistinguishable** |

Add the select fields to `{NNN}_holdout_summary.json` (`holdout_overall_before/after`,
`holdout_cohort`, `holdout_retention`, plus the existing `dsr`, `verdict`,
`stop_gate_cleared`).

---

## 5. `.claude/agents/decider.md`  — refresh the Reviewer's input after an accept

After an `accept`/`accept_profitable` commit, the strategy has changed, so
`backtest_trades.csv` (the Reviewer's input) is now stale relative to the new code.
Regenerate it so the next iteration analyses the current strategy:

```bash
# only after an accept commit:
venv/bin/python build-steps/real_backtester.py --split train --out build-steps/data/backtest_trades.csv
```

(No change to the reject/no_change paths.)

---

## Caveats for you to sanity-check (you're the quant)

1. **Independence assumption.** The analytic filter is exact only if skipping one
   market never changes another's entry/resolution. True in `real_backtester.py` today
   (each 15-min market is scored and resolved standalone). If you ever add cross-market
   state (bankroll-dependent sizing, cooldowns, position limits), the filter is no
   longer exact and `select` must run the changed code on holdout instead.
2. **Multiple testing.** DSR `N` must keep counting *every* configuration tried —
   `modify` and `select` alike — or you under-deflate. The existing `N = prior_rows + 1`
   already does this; don't reset it.
3. **MAX_DROP.** 20% is a guess. A strategy that only works by skipping 40% of trades
   is a different strategy; decide what retention floor you'll accept.
4. **Cohort overfitting.** Requiring `holdout_cohort.ci_95[1] < 0` (the cohort is -EV
   *out-of-sample*, not just in-sample) is the main guard against the Reviewer
   data-mining a spuriously-losing in-sample cohort. Keep it strict.
5. **`accept_local` analogue.** For `select`, "improved overall but CI straddles 0"
   should probably revert by default (same anti-overfit stance as the modify path).
