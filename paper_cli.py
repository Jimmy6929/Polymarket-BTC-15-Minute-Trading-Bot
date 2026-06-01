"""Terminal UI for the Polymarket BTC-15m paper trader.

Installs two console commands (see pyproject.toml [project.scripts]):

    paper-trading-run     launch the data-only paper trader with a live dashboard
    paper-trading-view    print a one-shot table of recorded paper trades

`paper-trading-run` spawns `bot.py` as a subprocess (paper / data-only mode — no
Polymarket account, no orders), redirects its verbose Nautilus logs to a file, and
renders a live Rich dashboard by reading that log + `paper_trades.json`. The bot
code is untouched; this is purely a launcher + monitor. Ctrl-C stops the bot.
"""
from __future__ import annotations

import json
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.columns import Columns
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

REPO = Path(__file__).resolve().parent
BOT = REPO / "bot.py"
PAPER_TRADES = REPO / "paper_trades.json"
DEFAULT_LOG = REPO / "paper.log"
PYTHON = sys.executable  # the venv interpreter this CLI was installed under

console = Console()


# --------------------------------------------------------------------------- #
# Data gathering
# --------------------------------------------------------------------------- #
def _running_bot_pids() -> list[str]:
    """PIDs of any already-running bot.py (so we don't double-launch / collide on port 8000)."""
    try:
        out = subprocess.run(["pgrep", "-f", r"[b]ot\.py"], capture_output=True, text=True).stdout
        return out.split()
    except Exception:
        return []


def parse_log(log_path: Path) -> dict:
    """Derive live status from the tail of the bot's log (best-effort, never raises)."""
    info = {
        "mode": "starting…",
        "no_account": False,
        "node_built": False,
        "connected": False,
        "streaming": False,
        "market": None,
        "next_switch": None,
        "last_decision": None,         # (text, style)
        "last_line": None,
        "crashed": False,
    }
    try:
        lines = log_path.read_text(errors="ignore").splitlines()
    except Exception:
        return info
    for ln in lines[-600:]:
        if "DATA-ONLY paper mode" in ln:
            info["mode"] = "DATA-ONLY paper"
            info["node_built"] = True
        if "injecting placeholder" in ln:
            info["no_account"] = True
        if "Connected to wss" in ln:
            info["connected"] = True
        if "Market STABLE" in ln:
            info["streaming"] = True
        m = re.search(r"CURRENT MARKET: (btc-updown-15m-\d+)", ln)
        if m:
            info["market"] = m.group(1)
        m2 = re.search(r"Next switch at: ([0-9:]+)", ln)
        if m2:
            info["next_switch"] = m2.group(1)
        if "TREND: UP" in ln:
            info["last_decision"] = ("UP → buy YES", "green")
        elif "TREND: DOWN" in ln:
            info["last_decision"] = ("DOWN → buy NO", "green")
        elif "NEUTRAL" in ln and "SKIP" in ln.upper():
            info["last_decision"] = ("NEUTRAL → skipped (coin-flip zone)", "yellow")
        if "Traceback (most recent call last)" in ln:
            info["crashed"] = True
    if lines:
        info["last_line"] = lines[-1][-140:]
    return info


def load_trades() -> list[dict]:
    try:
        return json.loads(PAPER_TRADES.read_text())
    except Exception:
        return []


def trade_stats(trades: list[dict]) -> dict:
    wins = sum(1 for t in trades if t.get("outcome") == "WIN")
    losses = sum(1 for t in trades if t.get("outcome") == "LOSS")
    pending = sum(1 for t in trades if t.get("outcome") == "PENDING")
    resolved = wins + losses
    pnl_vals = [t.get("pnl") for t in trades if t.get("pnl") is not None]
    hs_vals = [t.get("half_spread") for t in trades if t.get("half_spread") is not None]
    return {
        "total": len(trades),
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "win_rate": (wins / resolved * 100) if resolved else None,
        "total_pnl": sum(pnl_vals) if pnl_vals else None,
        "avg_pnl": (sum(pnl_vals) / len(pnl_vals)) if pnl_vals else None,
        "avg_half_spread": (sum(hs_vals) / len(hs_vals)) if hs_vals else None,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _status_panel(info: dict, start_ts: datetime, proc_alive: bool) -> Panel:
    up = datetime.now(timezone.utc) - start_ts
    h, rem = divmod(int(up.total_seconds()), 3600)
    mnt, sec = divmod(rem, 60)
    uptime = f"{h}h {mnt}m {sec}s" if h else f"{mnt}m {sec}s"

    def dot(ok: bool, label: str, style_ok: str = "bold green") -> Text:
        return Text("● ", style=style_ok if ok else "bold red") + Text(label, style="white")

    if not proc_alive:
        state = Text("● STOPPED", style="bold red")
    elif info["crashed"]:
        state = Text("● ERROR (see log)", style="bold red")
    elif info["streaming"]:
        state = Text("● LIVE — streaming quotes", style="bold green")
    elif info["connected"]:
        state = Text("● connected, awaiting quotes", style="bold yellow")
    elif info["node_built"]:
        state = Text("● node built, connecting…", style="bold yellow")
    else:
        state = Text("● starting…", style="bold yellow")

    body = Group(
        state,
        Text(""),
        dot(info["node_built"], f"Mode: {info['mode']}"),
        dot(info["no_account"], "No account (placeholder creds, no exec client)"),
        dot(info["connected"], "Public market websocket"),
        dot(info["streaming"], "Order-book quotes"),
        Text(""),
        Text(f"Market:  {info['market'] or '—'}", style="cyan"),
        Text(f"Next switch: {info['next_switch'] or '—'}   Uptime: {uptime}", style="dim"),
    )
    return Panel(body, title="[bold]Status[/bold]", border_style="green" if proc_alive else "red")


def _stats_panel(stats: dict) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style="bold")
    t.add_column()

    def pnl_text(v):
        if v is None:
            return Text("—", style="dim")
        return Text(f"${v:+.4f}", style="green" if v >= 0 else "red")

    t.add_row("Total trades", str(stats["total"]))
    t.add_row("Pending", Text(str(stats["pending"]), style="yellow"))
    t.add_row("Wins / Losses", f"[green]{stats['wins']}[/green] / [red]{stats['losses']}[/red]")
    t.add_row("Win rate", f"{stats['win_rate']:.1f}%" if stats["win_rate"] is not None else Text("—", style="dim"))
    t.add_row("Total P&L", pnl_text(stats["total_pnl"]))
    t.add_row("Avg P&L / trade", pnl_text(stats["avg_pnl"]))
    hs = stats["avg_half_spread"]
    t.add_row("Avg half-spread", Text(f"{hs:.4f}" if hs is not None else "—", style="magenta" if hs else "dim"))
    return Panel(t, title="[bold]Paper P&L[/bold]", border_style="blue")


def _trades_table(trades: list[dict], limit: int = 12) -> Panel:
    table = Table(expand=True, show_edge=False, pad_edge=False)
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Time (UTC)", width=16)
    table.add_column("Dir", width=6)
    table.add_column("Price", justify="right", width=7)
    table.add_column("Fill", justify="right", width=7)
    table.add_column("½spr", justify="right", width=7)
    table.add_column("Outcome", width=8)
    table.add_column("P&L", justify="right", width=10)

    recent = trades[-limit:]
    base = len(trades) - len(recent)
    for i, tr in enumerate(recent, start=base + 1):
        try:
            ts = datetime.fromisoformat(tr["timestamp"]).strftime("%m-%d %H:%M")
        except Exception:
            ts = "—"
        d = tr.get("direction", "")
        dl = d.lower()  # bot.py persists direction uppercased ("LONG"/"SHORT")
        dstyle = "green" if dl == "long" else "red" if dl == "short" else "white"
        outcome = tr.get("outcome", "PENDING")
        ostyle = {"WIN": "bold green", "LOSS": "bold red", "PENDING": "yellow"}.get(outcome, "white")
        pnl = tr.get("pnl")
        pnl_txt = Text("—", style="dim") if pnl is None else Text(f"${pnl:+.4f}", style="green" if pnl >= 0 else "red")
        fill = tr.get("fill_price")
        hs = tr.get("half_spread")
        table.add_row(
            str(i), ts,
            Text(d[:5] or "—", style=dstyle),
            f"{tr.get('price', 0):.3f}",
            f"{fill:.3f}" if fill is not None else "—",
            f"{hs:.4f}" if hs is not None else "—",
            Text(outcome, style=ostyle),
            pnl_txt,
        )
    if not recent:
        table.add_row("—", "no trades yet — first decision fires near minute 13", "", "", "", "", "", "")
    return Panel(table, title=f"[bold]Recent paper trades[/bold] (showing {len(recent)} of {len(trades)})", border_style="cyan")


def render(info: dict, stats: dict, trades: list[dict], start_ts: datetime, proc_alive: bool) -> Group:
    header = Panel(
        Text("Polymarket BTC 15-min — Paper Trader", style="bold white", justify="center"),
        border_style="bright_black",
    )
    top = Columns([_status_panel(info, start_ts, proc_alive), _stats_panel(stats)], equal=True, expand=True)
    footer = Text(
        f"Ctrl-C to stop · logs → {DEFAULT_LOG.name} · {'running' if proc_alive else 'STOPPED'}",
        style="dim", justify="center",
    )
    return Group(header, top, _trades_table(trades), footer)


# --------------------------------------------------------------------------- #
# Typer app
# --------------------------------------------------------------------------- #
app = typer.Typer(add_completion=False, help="Polymarket BTC-15m paper trader UI.")


@app.command()
def run(
    restart: bool = typer.Option(False, "--restart", "-r", help="Kill any already-running bot.py first."),
    log_file: Path = typer.Option(DEFAULT_LOG, "--log", help="Where to write the bot's verbose log."),
    refresh: float = typer.Option(1.5, "--refresh", help="Dashboard refresh interval (seconds)."),
):
    """Launch the data-only paper trader with a live dashboard. Places NO orders."""
    # Treat SIGTERM (kill / process manager) like Ctrl-C so the bot subprocess is
    # always stopped cleanly, not orphaned. SIGINT already raises KeyboardInterrupt.
    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_sigterm)

    existing = _running_bot_pids()
    if existing:
        if restart:
            # Graceful first (SIGINT → bot.py's node.dispose()), SIGKILL only if it lingers.
            for pid in existing:
                try:
                    subprocess.run(["kill", "-INT", pid])
                except Exception:
                    pass
            time.sleep(5)
            for pid in _running_bot_pids():
                try:
                    subprocess.run(["kill", "-9", pid])
                except Exception:
                    pass
            console.print(f"[yellow]Stopped existing bot ({', '.join(existing)}).[/yellow]")
        else:
            console.print(
                f"[bold red]A bot.py is already running (pid {', '.join(existing)}).[/bold red]\n"
                "Stop it first, or re-run with [bold]--restart[/bold] to replace it."
            )
            raise typer.Exit(1)

    console.print("[bold green]Starting paper trader (data-only, no account, no orders)…[/bold green]")
    log_fh = open(log_file, "w")
    proc = subprocess.Popen([PYTHON, str(BOT)], stdout=log_fh, stderr=subprocess.STDOUT, cwd=str(REPO))
    start_ts = datetime.now(timezone.utc)

    interrupted = False
    try:
        with Live(console=console, screen=False, refresh_per_second=4, auto_refresh=False) as live:
            while proc.poll() is None:
                info = parse_log(log_file)
                trades = load_trades()
                live.update(render(info, trade_stats(trades), trades, start_ts, True), refresh=True)
                time.sleep(refresh)
            # process exited on its own
            info = parse_log(log_file)
            trades = load_trades()
            live.update(render(info, trade_stats(trades), trades, start_ts, False), refresh=True)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        log_fh.close()

    trades = load_trades()
    s = trade_stats(trades)
    console.print()
    if interrupted:
        console.print("[yellow]Stopped by user.[/yellow]")
    elif proc.returncode not in (0, -signal.SIGINT):
        console.print(f"[bold red]Bot exited unexpectedly (code {proc.returncode}). See {log_file}.[/bold red]")
    console.print(
        f"Session: [bold]{s['total']}[/bold] trades "
        f"([green]{s['wins']}W[/green]/[red]{s['losses']}L[/red]/[yellow]{s['pending']} pending[/yellow]). "
        f"Logs: {log_file}"
    )


@app.command()
def view(limit: int = typer.Option(50, help="Max trades to show.")):
    """Print a one-shot table of recorded paper trades and exit."""
    trades = load_trades()
    if not trades:
        console.print("[yellow]No paper trades recorded yet.[/yellow]")
        raise typer.Exit()
    console.print(_trades_table(trades, limit=limit))
    s = trade_stats(trades)
    console.print(_stats_panel(s))


def run_entry() -> None:
    """Console-script entry for `paper-trading-run` — invokes the run command directly."""
    typer.run(run)


def view_entry() -> None:
    """Console-script entry for `paper-trading-view`."""
    typer.run(view)


if __name__ == "__main__":
    app()
