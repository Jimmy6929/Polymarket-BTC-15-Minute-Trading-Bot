"""Terminal UI for the Polymarket BTC-15m historical backtest.

Installs the console command (see pyproject.toml [project.scripts]):

    backtest-run     run the real-data backtest and show a results dashboard

It runs build-steps/real_backtester.py as a SUBPROCESS (the backtester is a frozen
measurement instrument — untouched), shows a spinner during the run, then parses the
summary + reads backtest_trades.csv to render a Rich dashboard. The headline is the
per-trade EV with its bootstrap CI; the value-add panel is a spread-bucket EV
breakdown (half_spread = |fill − mid| per trade) showing whether the edge decays as
spread widens — the central question for this strategy.
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

REPO = Path(__file__).resolve().parent
BACKTESTER = REPO / "build-steps" / "real_backtester.py"
TRADES_CSV = REPO / "build-steps" / "data" / "backtest_trades.csv"
MANIFEST = REPO / "build-steps" / "data" / "MANIFEST.json"
FETCHER = REPO / "build-steps" / "fetch_data.py"
PYTHON = sys.executable

console = Console()


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _num(pattern: str, text: str, cast=float):
    m = re.search(pattern, text)
    return cast(m.group(1)) if m else None


def parse_summary(text: str) -> dict:
    """Extract the backtester's summary fields (best-effort; missing → None)."""
    g = {}
    g["processed"] = _num(r"Total markets processed\s*:\s*(\d+)", text, int)
    g["skip_no_price"] = _num(r"no price@min13\)\s*:\s*(\d+)", text, int)
    g["skip_no_spot"] = _num(r"no BTC spot\)\s*:\s*(\d+)", text, int)
    g["skip_short_hist"] = _num(r"price_history<20\)\s*:\s*(\d+)", text, int)
    g["skip_no_signal"] = _num(r"no fused signal\)\s*:\s*(\d+)", text, int)
    g["skip_neutral"] = _num(r"neutral 0\.4-0\.6\)\s*:\s*(\d+)", text, int)
    g["trades"] = _num(r"Trades executed\s*:\s*(\d+)", text, int)
    g["win_rate"] = _num(r"Win rate\s*:\s*([\d.]+)%", text)
    m = re.search(r"Wilson 95% CI:\s*([\d.]+)%[^\d]+([\d.]+)%", text)
    g["wr_lo"], g["wr_hi"] = (float(m.group(1)), float(m.group(2))) if m else (None, None)
    g["total_pnl"] = _num(r"Total P&L\s*:\s*\$([+-][\d.]+)", text)
    g["roi"] = _num(r"ROI\s*:\s*([+-][\d.]+)%", text)
    g["avg_pnl"] = _num(r"Avg P&L per trade\s*:\s*\$([+-][\d.]+)", text)
    m = re.search(r"bootstrap 95% CI:\s*\$([+-][\d.]+)[^$]+\$([+-][\d.]+)", text)
    g["ev_lo"], g["ev_hi"] = (float(m.group(1)), float(m.group(2))) if m else (None, None)
    return g


def load_trades() -> list[dict]:
    try:
        with TRADES_CSV.open() as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def dataset_range() -> str | None:
    try:
        d = json.loads(MANIFEST.read_text())["sources"]["polymarket_btc_15m.json"]
        r = d.get("date_range_utc")
        return f"{r[0][:10]} → {r[1][:10]} ({d.get('markets')} markets)" if r else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _ev_text(v: float | None) -> Text:
    if v is None:
        return Text("—", style="dim")
    return Text(f"${v:+.4f}", style="bold green" if v >= 0 else "bold red")


def _headline_panel(g: dict) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style="bold")
    t.add_column()
    ev, lo, hi = g.get("avg_pnl"), g.get("ev_lo"), g.get("ev_hi")
    t.add_row("Avg P&L / trade (EV)", _ev_text(ev))
    if lo is not None and hi is not None:
        straddles = lo < 0 < hi
        ci = Text(f"[{lo:+.4f}, {hi:+.4f}]", style="yellow" if straddles else ("green" if lo > 0 else "red"))
        verdict = "  straddles 0 → indistinguishable from no edge" if straddles else (
            "  CI > 0 → positive" if lo > 0 else "  CI < 0 → negative edge")
        t.add_row("bootstrap 95% CI", ci + Text(verdict, style="dim"))
    t.add_row("Total P&L", _ev_text(g.get("total_pnl")))
    roi = g.get("roi")
    t.add_row("ROI", Text(f"{roi:+.2f}%" if roi is not None else "—", style="green" if (roi or 0) >= 0 else "red"))
    wr, wlo, whi = g.get("win_rate"), g.get("wr_lo"), g.get("wr_hi")
    wr_txt = f"{wr:.1f}%" if wr is not None else "—"
    if wlo is not None:
        wr_txt += f"  (95% CI {wlo:.1f}–{whi:.1f}%)"
    t.add_row("Win rate", Text(wr_txt))
    t.add_row("Trades", Text(str(g.get("trades") or "—")))
    return Panel(t, title="[bold]Backtest result[/bold]", border_style="blue")


def _skips_panel(g: dict) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style="bold")
    t.add_column(justify="right")
    t.add_row("Markets processed", str(g.get("processed") or "—"))
    t.add_row("→ traded", Text(str(g.get("trades") or "—"), style="green"))
    t.add_row("skip: no price@13", str(g.get("skip_no_price") or 0))
    t.add_row("skip: no BTC spot", Text(str(g.get("skip_no_spot") or 0), style="yellow"))
    t.add_row("skip: <20 history", str(g.get("skip_short_hist") or 0))
    t.add_row("skip: no signal", str(g.get("skip_no_signal") or 0))
    t.add_row("skip: neutral 0.4–0.6", str(g.get("skip_neutral") or 0))
    return Panel(t, title="[bold]Funnel[/bold]", border_style="bright_black")


def _spread_panel(trades: list[dict]) -> Panel:
    """EV bucketed by per-trade half-spread (|fill − mid|) — does the edge decay with cost?"""
    edges = [(0.0, 0.005), (0.005, 0.01), (0.01, 0.02), (0.02, 0.05), (0.05, 1.01)]
    table = Table(expand=True, show_edge=False, pad_edge=False)
    table.add_column("Half-spread |fill−mid|", width=22)
    table.add_column("Trades", justify="right")
    table.add_column("Avg EV", justify="right")
    table.add_column("Win%", justify="right")
    # Pre-parse each trade once into (half_spread, pnl, outcome), guarding all casts together.
    parsed = []
    for t in trades:
        try:
            hs = abs(float(t["fill_price"]) - float(t["entry_price"]))
            pnl = float(t["pnl"])
        except (KeyError, ValueError):
            continue
        parsed.append((hs, pnl, t.get("outcome")))
    for lo, hi in edges:
        bucket = [(pnl, o) for hs, pnl, o in parsed if lo <= hs < hi]
        label = f"[{lo:.3f}, {hi:.3f})" if hi < 1 else f"≥ {lo:.3f}"
        if not bucket:
            table.add_row(label, "0", Text("—", style="dim"), "—")
            continue
        ev = sum(p for p, _ in bucket) / len(bucket)
        wins = sum(1 for _, o in bucket if o == "WIN")
        table.add_row(label, str(len(bucket)), _ev_text(ev), f"{wins/len(bucket)*100:.0f}%")
    return Panel(table, title="[bold]EV by spread bucket[/bold]  (the thesis: does edge decay as spread widens?)",
                 border_style="magenta")


def render(g: dict, trades: list[dict], split: str) -> Group:
    rng = dataset_range()
    header = Panel(
        Text(f"Polymarket BTC 15-min — Backtest  [split={split}]"
             + (f"\n{rng}" if rng else ""), style="bold white", justify="center"),
        border_style="bright_black",
    )
    from rich.columns import Columns
    top = Columns([_headline_panel(g), _skips_panel(g)], equal=True, expand=True)
    return Group(header, top, _spread_panel(trades))


# --------------------------------------------------------------------------- #
# Typer app
# --------------------------------------------------------------------------- #
app = typer.Typer(add_completion=False, help="Polymarket BTC-15m historical backtest UI.")


@app.command()
def run(
    split: str = typer.Option("both", "--split", help="Which split to backtest: both / train / holdout."),
    refresh: bool = typer.Option(False, "--refresh", help="Top up the dataset (fetch --full) before backtesting."),
):
    """Run the real-data backtest and render a results dashboard."""
    if refresh:
        with console.status("[bold]Refreshing dataset (incremental fetch)…[/]", spinner="dots"):
            fr = subprocess.run([PYTHON, str(FETCHER), "--full"], cwd=str(REPO))
        if fr.returncode != 0:
            console.print("[yellow]Dataset refresh failed — backtesting on existing data.[/yellow]")

    rng = dataset_range()
    label = f" over {rng}" if rng else ""
    with console.status(f"[bold]Running backtest{label}…[/]  (~1–2 min)", spinner="dots"):
        proc = subprocess.run(
            [PYTHON, str(BACKTESTER), "--split", split],
            capture_output=True, text=True, cwd=str(REPO),
        )

    if proc.returncode != 0:
        console.print(f"[bold red]Backtest failed (exit {proc.returncode}).[/bold red]")
        console.print(proc.stderr[-1500:] or proc.stdout[-1500:])
        raise typer.Exit(1)

    g = parse_summary(proc.stdout)
    trades = load_trades()
    console.print(render(g, trades, split))
    if g.get("trades") and g.get("skip_no_spot"):
        console.print(
            f"[dim]Note: {g['skip_no_spot']} markets skipped for missing BTC spot (Coinbase gap) — "
            f"the wide-spread early regime is under-represented until spot is backfilled.[/dim]"
        )


def run_entry() -> None:
    """Console-script entry for `backtest-run`."""
    typer.run(run)


if __name__ == "__main__":
    app()
