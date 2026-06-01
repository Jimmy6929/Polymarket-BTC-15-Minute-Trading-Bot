---
description: Report git branch health — flags branches safe to delete (merged), problematic branches (diverged / local-only / unpushed / gone-upstream / stale), protected branches, plus repo signals (upstream drift, exp-* tag clutter). Read-only and report-only; suggests copy-paste git branch -d commands you run yourself. Never deletes or pushes.
---

# /branches — Branch Health Report

Invoke the read-only branch reporter and relay its findings.

## Step 1 — Run the branch-manager subagent

Use the `Agent` tool with `subagent_type: "branch-manager"` and prompt:

> "Produce the branch-health report for this repo. Run `.claude/branch_report.py --json`,
> classify and present SAFE-TO-DELETE / PROBLEM / PROTECTED, the repo signals, and
> copy-paste `git branch -d` commands. Report-only — do not delete, push, or mutate
> anything."

## Step 2 — Relay the report

Present the subagent's report to the user verbatim (it is already formatted). Do **not**
run any of the suggested deletion/push commands yourself — those are for the user to run.

## Notes

- The engine is `.claude/branch_report.py` (read-only; modes `--json` / `--human` /
  `--session-hook`). A quiet version runs at SessionStart to surface flags into context.
- Tunables live at the top of `branch_report.py`: `PROTECTED`, `STALE_DAYS` (90),
  `MAIN_REF` (main), `UPSTREAM_REMOTE` (upstream).
- This command never deletes a branch. Safe deletes use `git branch -d` (which refuses
  unmerged work); the user runs them.
