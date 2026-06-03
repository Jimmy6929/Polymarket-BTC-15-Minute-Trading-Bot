# Archive — the never-wired live-execution stack

This directory holds an early live-trading-and-monitoring scaffold that was built **before** any
edge had been established — the wrong order, and one of the lessons of this project. It is kept,
honestly labelled, rather than deleted: it documents the mistake instead of hiding it.

**None of this code is wired, validated, or safe to run.** Specifically:

- `execution/` — order-execution engine, a Polymarket client whose `get_btc_market()` returns a
  hardcoded mock, and a risk engine. Order IDs were not idempotent; there was no fill-event
  listener (a live fill never created a position); state was in-memory only. **Cannot touch
  live capital.**
- `feedback/` — a "self-learning" engine that optimised signal weights from a trade history
  buffer that was **never populated** anywhere in the bot. It learned from an empty list.
- `monitoring/`, `grafana/`, `redis_control.py` — a Grafana/Prometheus/Redis stack that was
  never connected to anything that runs.
- `data_sources/`, `core/ingestion/`, `core/nautilus_core/` — unified data adapters and a
  Nautilus integration layer that the actual research code (the backtesters in `build-steps/`)
  never imports.
- `bot.py`, `15m_bot_runner.py` — the live entry points. The live path was never validated and
  the underlying strategy was later proven EV-negative.

**What actually drove the research** lives at the repo root: the `build-steps/` backtesters and
data pipelines, `core/strategy_brain/` (the only `core/` subtree the Polymarket backtester
imports), and the write-ups in `README.md` / `RESEARCH_JOURNEY.md` / `REPORT.md`.

The honest one-line summary: production infrastructure built for a strategy that had no edge.
Edge research is cheap to falsify and comes first; execution infrastructure is expensive and
comes last. This project learned that order the expensive way, and this folder is the receipt.
