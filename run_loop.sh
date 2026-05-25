#!/usr/bin/env bash
#
# run_loop.sh — run the agent self-improvement loop with a clean terminal
#               and full on-disk logging.
#
# WHY THIS WRAPPER EXISTS
# -----------------------
# The Claude Code agent loop is a TUI that constantly rewrites the terminal
# (cursor moves, partial line redraws, color changes, spinners). That
# workload triggers two failure modes in Cursor/VS Code's integrated
# terminal that produce the "text turned into garbled glyphs" symptom:
#
#   1. GPU glyph-atlas corruption. The WebGL renderer caches rendered
#      characters in a texture atlas. Under heavy partial-redraw load it
#      can desync, so subsequent characters map to wrong texture cells.
#      Once that happens, every character is wrong until the terminal is
#      reloaded.
#
#   2. DEC line-drawing charset getting stuck. ANSI defines an escape
#      sequence (ESC ( 0) that switches the terminal into line-drawing
#      mode where ASCII renders as box characters. If a TUI's partial
#      write splits or drops the matching reset (ESC ( B), the terminal
#      stays in that mode and every character is remapped to a glyph
#      from the wrong charset.
#
# This wrapper does three things to make both failures non-fatal:
#
#   - Resets the terminal charset and SGR attributes on entry AND exit,
#     so a corrupted state from the previous run can't leak in, and a
#     corrupted state from this run can't leak out into your next shell.
#
#   - Uses `script(1)` to run the loop under a real PTY while capturing
#     every byte to a log file. The TUI gets the PTY it needs to render
#     correctly, and you get a complete on-disk record. If the screen
#     garbles mid-run, the log file is unaffected and fully readable.
#
#   - Adds a timestamped log per run, so you can grep history across runs.
#
# USAGE
# -----
#   ./run_loop.sh                      # runs the default loop command
#   ./run_loop.sh <any command...>     # runs whatever you pass instead
#
# READING THE LOG
# ---------------
#   less -R logs/loop/loop_<ts>.log    # -R interprets ANSI colors
#   # Strip ANSI for grep:
#   sed -E 's/\x1b\[[0-9;]*[a-zA-Z]//g' logs/loop/loop_<ts>.log | grep ...

set -uo pipefail

# ── Configure default loop command here ──────────────────────────────────
# Edit this if the way you invoke the loop changes. The wrapper itself
# stays generic.
DEFAULT_CMD=(claude /improve-iteration)

# ── Setup ────────────────────────────────────────────────────────────────
cd "$(dirname "$0")"
mkdir -p logs/loop
LOG_FILE="logs/loop/loop_$(date +%Y%m%d_%H%M%S).log"

# Reset terminal state:
#   \033(B  -> G0 charset back to ASCII (undoes DEC line-drawing if stuck)
#   \033)B  -> G1 charset back to ASCII
#   \033[0m -> reset all SGR attributes (color, bold, inverse, etc.)
#   \033[?25h -> show cursor (in case a crashed TUI left it hidden)
reset_term() {
  printf '\033(B\033)B\033[0m\033[?25h'
}
trap reset_term EXIT INT TERM
reset_term

# ── Command to run ───────────────────────────────────────────────────────
if [ "$#" -eq 0 ]; then
  set -- "${DEFAULT_CMD[@]}"
fi

echo "=== loop start: $(date) ==="
echo "=== command:    $* ==="
echo "=== log file:   $LOG_FILE ==="
echo

# ── Run under PTY with full logging ──────────────────────────────────────
# macOS BSD script:    script [-q] [file [command ...]]
# Linux util-linux:    script [-q] -c '<command>' <file>
# Detect which we have and call accordingly.
if script --version 2>/dev/null | grep -qi util-linux; then
  script -qec "$(printf '%q ' "$@")" "$LOG_FILE"
  STATUS=$?
else
  # BSD script (macOS default)
  script -q "$LOG_FILE" "$@"
  STATUS=$?
fi

echo
echo "=== loop end: $(date) exit=$STATUS ==="
echo "=== log saved: $LOG_FILE ==="
exit "$STATUS"
