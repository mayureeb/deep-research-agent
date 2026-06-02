"""Live event emitter. Off by default; enable with `set_verbose(True)` or
the `-v` flag to main.py. Output goes to stderr so it doesn't interleave
with the main report on stdout.
"""
from __future__ import annotations

from rich.console import Console

# Stderr-bound console keeps the event log off stdout for clean `--out`.
_console = Console(stderr=True)
_verbose = False


def set_verbose(v: bool) -> None:
    """Toggle verbose mode."""
    global _verbose
    _verbose = v


def is_verbose() -> bool:
    """Read the current verbose flag."""
    return _verbose


def orch(msg: str) -> None:
    """Emit an orchestrator event (cyan)."""
    _emit("orchestrator", msg, "cyan")


def planner(msg: str) -> None:
    """Emit a planner event (yellow)."""
    _emit("planner", msg, "yellow")


def researcher(rid: str, sqid: str, msg: str) -> None:
    """Emit a researcher event tagged with the last 6 chars of the researcher
    id and the sub-question id."""
    short = rid[-6:] if len(rid) > 6 else rid
    _emit(f"res #{short}", f"[{sqid}] {msg}", "blue")


def reconciler(msg: str) -> None:
    """Emit a reconciler event (yellow)."""
    _emit("reconciler", msg, "yellow")


def writer(msg: str) -> None:
    """Emit a writer event (green)."""
    _emit("writer", msg, "green")


def critic(msg: str) -> None:
    """Emit a critic event (red)."""
    _emit("critic", msg, "red")


def baseline(msg: str) -> None:
    """Emit a baseline-pipeline event (magenta)."""
    _emit("baseline", msg, "magenta")


def _emit(role: str, msg: str, color: str) -> None:
    """No-op when not verbose; otherwise prints a colored `role | msg` line
    to stderr. `highlight=False` disables Rich's auto-highlighting."""
    if not _verbose:
        return
    _console.print(f"[{color}]{role:<13}[/{color}] {msg}", highlight=False)
