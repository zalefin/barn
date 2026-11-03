"""Terminal output. Progress and messages go to stderr; data (`list`) to stdout.
Colors and spinners turn themselves off when the stream is not a terminal, or
with NO_COLOR set."""

import sys
import time
from contextlib import contextmanager

from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, ProgressColumn, SpinnerColumn, TextColumn
from rich.text import Text
from rich.theme import Theme

from . import compat

THEME = Theme(
    {
        "ok": "green",
        "warn": "yellow",
        "fail": "bold red",
        "muted": "dim",
        "accent": "cyan",
        "name": "bold",
    }
)

if compat.WINDOWS:
    # Redirected output there is cp1252, which has no ✓ or →; consoles are UTF-8 already.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower() not in ("utf-8", "utf8"):
            stream.reconfigure(encoding="utf-8", errors="replace")

err = Console(stderr=True, theme=THEME, highlight=False)
out = Console(theme=THEME, highlight=False)

MARKS = {"pass": "[ok]✓[/]", "warn": "[warn]![/]", "fail": "[fail]✗[/]"}


def _line(mark, text, detail=""):
    tail = f"  [muted]{escape(detail)}[/]" if detail else ""
    err.print(f"{mark} {escape(text)}{tail}")


def info(text, detail=""):
    _line("[accent]›[/]", text, detail)


def done(text, detail=""):
    _line(MARKS["pass"], text, detail)


def warn(text, detail=""):
    _line(MARKS["warn"], text, detail)


def fail(text, detail=""):
    _line(MARKS["fail"], text, detail)


def check(status, text, detail=""):
    """One doctor result: status is pass, warn or fail."""
    _line(MARKS[status], text, detail)


def header(*parts):
    """`zach-dev · m7i.xlarge · us-east-1`, first part bold."""
    first, *rest = parts
    tail = "".join(f" [muted]·[/] {escape(str(p))}" for p in rest if p)
    err.print(f"[name]{escape(str(first))}[/]{tail}")


def block(text, title=""):
    """Dimmed, indented output from somewhere else (console log, terraform)."""
    if title:
        err.print(f"  [muted]{escape(title)}[/]")
    for line in text.rstrip().splitlines():
        err.print(f"  [muted]│ {escape(line)}[/]")


def elapsed(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s" if m < 60 else f"{m // 60}h{m % 60:02d}m"


class ElapsedColumn(ProgressColumn):
    def render(self, task):
        return Text(elapsed(task.elapsed or 0), style="muted")


class Step:
    def __init__(self, progress, task):
        self._progress = progress
        self._task = task
        self.result = ""  # shown after the ✓ line

    def update(self, detail):
        """Replace the dimmed detail next to the spinner (e.g. terraform's last line)."""
        detail = " ".join(detail.split())
        if len(detail) > 70:
            detail = detail[:69] + "…"
        self._progress.update(self._task, detail=escape(detail))


@contextmanager
def step(text):
    """A spinner with elapsed time while the block runs, then a ✓ or ✗ line.
    Messages printed through `err` inside the block appear above the spinner."""
    progress = Progress(
        SpinnerColumn(style="accent"),
        TextColumn("{task.description}"),
        ElapsedColumn(),
        TextColumn("[muted]{task.fields[detail]}[/]"),
        console=err,
        transient=True,
    )
    start = time.monotonic()
    with progress:
        s = Step(progress, progress.add_task(escape(text), detail=""))
        try:
            yield s
        except BaseException:
            progress.stop()
            fail(text, elapsed(time.monotonic() - start))
            raise
    detail = " · ".join(p for p in (s.result, elapsed(time.monotonic() - start)) if p)
    done(text, detail)
