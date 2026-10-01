"""Progress reporting, from a live two-line display to a silent implementation.

The display is written by hand rather than with `rich.progress`, for one reason
that matters: rich's `BarColumn` draws the filled part and the remainder with the
*same* glyph, distinguishing them by colour only. On a terminal that does not
render those two colours differently, the bar looks full from the first byte.
Here the filled part is `█` and the remainder is `░`, in both the overall line and
the current-file line.
"""

import time
from dataclasses import dataclass, field
from typing import Protocol

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from .formatting import (
    format_bytes,
    format_bytes_per_second,
    format_count,
    format_duration,
    render_bar,
    shorten,
)

REFRESH_PER_SECOND = 8
MINIMUM_SECONDS_FOR_SPEED = 0.5

#: ``(minimum console width, bar width, file-name width, show speed and ETA)``,
#: widest first. The two lines are composed to fit the console instead of being
#: wrapped by rich, which would break the two-line layout.
LAYOUTS: tuple[tuple[int, int, int, bool], ...] = (
    (110, 24, 34, True),
    (100, 16, 26, True),
    (74, 12, 18, False),
    (62, 6, 12, False),
    (0, 0, 14, False),
)


@dataclass(frozen=True)
class ProgressLayout:
    """How much room the display gives to the bar, the name and the estimates."""

    bar_width: int
    label_width: int
    show_speed: bool


def choose_layout(console_width: int) -> ProgressLayout:
    """Return the layout that fits a console of this width."""
    for minimum_width, bar_width, label_width, show_speed in LAYOUTS:
        if console_width >= minimum_width:
            return ProgressLayout(bar_width, label_width, show_speed)
    raise AssertionError("the last layout must accept any width")


class ProgressReporter(Protocol):
    """Everything the transfer needs to keep the user informed."""

    def start_phase(
        self,
        total_files: int,
        total_bytes: int,
        *,
        label: str = "Overall",
        unit: str = "bytes",
    ) -> None:
        """Announce the beginning of a phase covering `total_files` files.

        `unit` is what the bar measures: the volume being transferred, or simply
        the number of files when nothing is transferred (verifying what is already
        at the destination, for instance).
        """

    def start_file(self, label: str, size: int) -> None:
        """Announce the file about to be processed."""

    def advance(self, byte_count: int) -> None:
        """Declare `byte_count` more bytes processed."""

    def complete_file(self, label: str, status: str) -> None:
        """Announce the outcome of the current file."""

    def note(self, message: str) -> None:
        """Print a line of text without breaking the progress display."""

    def stop(self) -> None:
        """Tear the display down."""


class SilentProgressReporter:
    """A reporter that prints nothing, used by tests and ``--quiet``."""

    def start_phase(
        self,
        total_files: int,
        total_bytes: int,
        *,
        label: str = "Overall",
        unit: str = "bytes",
    ) -> None:
        pass

    def start_file(self, label: str, size: int) -> None:
        pass

    def advance(self, byte_count: int) -> None:
        pass

    def complete_file(self, label: str, status: str) -> None:
        pass

    def note(self, message: str) -> None:
        pass

    def stop(self) -> None:
        pass


@dataclass
class PhaseProgress:
    """The numbers of a phase, and the numbers of the file being processed."""

    label: str = "Overall"
    unit: str = "bytes"
    total_files: int = 0
    total_bytes: int = 0
    done_bytes: int = 0
    done_files: int = 0
    current_label: str = ""
    current_size: int = 0
    current_bytes: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed_seconds(self) -> float:
        return max(time.monotonic() - self.started_at, 0.0)

    @property
    def speed(self) -> float | None:
        """Bytes per second, or None while the measure would be meaningless."""
        elapsed = self.elapsed_seconds
        if elapsed < MINIMUM_SECONDS_FOR_SPEED or self.done_bytes <= 0:
            return None
        return self.done_bytes / elapsed

    @property
    def remaining_seconds(self) -> float | None:
        speed = self.speed
        if speed is None or speed <= 0:
            return None
        return max(self.total_bytes - self.done_bytes, 0) / speed

    @property
    def ratio(self) -> float:
        if self.unit == "files":
            return min(self.done_files / self.total_files, 1.0) if self.total_files else 0.0
        if self.total_bytes <= 0:
            return 1.0 if self.done_files >= self.total_files else 0.0
        return min(self.done_bytes / self.total_bytes, 1.0)

    @property
    def current_ratio(self) -> float:
        if self.current_size <= 0:
            return 0.0
        return min(self.current_bytes / self.current_size, 1.0)


def compose_summary(progress: PhaseProgress, layout: ProgressLayout) -> str:
    """Build the overall line: label, bar, percentage, volume, speed, remaining."""
    if progress.unit == "files":
        parts = [f"{progress.label}"]
        if layout.bar_width:
            parts.append(render_bar(progress.done_files, progress.total_files, layout.bar_width))
            parts.append(f"{progress.ratio * 100:3.0f}%")
        parts.append(f"{format_count(progress.done_files, 'file')} of {progress.total_files}")
        return "  ".join(parts)
    parts = [f"{progress.label} {progress.done_files}/{progress.total_files} files"]
    if layout.bar_width:
        parts.append(render_bar(progress.done_bytes, progress.total_bytes, layout.bar_width))
        parts.append(f"{progress.ratio * 100:3.0f}%")
    parts.append(f"{format_bytes(progress.done_bytes)} / {format_bytes(progress.total_bytes)}")
    speed = progress.speed
    if layout.show_speed and speed is not None:
        parts.append(format_bytes_per_second(speed))
        remaining = progress.remaining_seconds
        if remaining is not None:
            parts.append(f"{format_duration(remaining)} left")
    return "  ".join(parts)


def compose_current_file(progress: PhaseProgress, layout: ProgressLayout) -> str:
    """Build the second line: the file being processed and its own progress.

    In a phase counted in files, no byte advances: showing a byte bar stuck at
    0 % suggests work that is not happening.
    """
    if not progress.current_label:
        return ""
    name = shorten(progress.current_label, layout.label_width)
    parts = [f"  {name:<{layout.label_width}}"]
    if layout.bar_width and progress.unit != "files":
        parts.append(render_bar(progress.current_bytes, progress.current_size, layout.bar_width))
        parts.append(f"{progress.current_ratio * 100:3.0f}%")
    parts.append(f"{format_bytes(progress.current_bytes)} / {format_bytes(progress.current_size)}")
    return "  ".join(parts)


def compose_display(progress: PhaseProgress, layout: ProgressLayout, *, width: int = 0) -> Group:
    """Build the two-line display, in an explicit style.

    Each line is padded to the console width: the live display rewrites the same
    two lines in place, and a shorter line leaves the tail of the previous one
    behind (a stray "%" after the byte count, for instance).
    """
    summary = Text(compose_summary(progress, layout), style="bold")
    current = compose_current_file(progress, layout)
    if width:
        summary = Text(summary.plain.ljust(width), style="bold")
        current = current.ljust(width) if current else current
    if not current:
        return Group(summary)
    return Group(summary, Text(current, style="dim"))


class RichProgressReporter:
    """A two-line live display: the whole phase, then the current file."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.progress = PhaseProgress()
        self.layout = choose_layout(console.width)
        self._live: Live | None = None

    def start_phase(
        self,
        total_files: int,
        total_bytes: int,
        *,
        label: str = "Overall",
        unit: str = "bytes",
    ) -> None:
        self.progress = PhaseProgress(
            label=label,
            unit=unit,
            total_files=total_files,
            total_bytes=total_bytes,
            started_at=time.monotonic(),
        )
        self._live = Live(
            compose_display(self.progress, self.layout, width=self.console.width),
            console=self.console,
            refresh_per_second=REFRESH_PER_SECOND,
            transient=False,
        )
        self._live.start()

    def start_file(self, label: str, size: int) -> None:
        self.progress.current_label = label
        self.progress.current_size = size
        self.progress.current_bytes = 0
        self._refresh(immediately=True)

    def advance(self, byte_count: int) -> None:
        self.progress.current_bytes += byte_count
        self.progress.done_bytes += byte_count
        self._refresh(immediately=False)

    def complete_file(self, label: str, status: str) -> None:
        del label, status
        self.progress.done_files += 1
        # A file that failed mid-copy has already declared the bytes it read: the
        # overall bar must not overshoot its total.
        self.progress.done_bytes = min(self.progress.done_bytes, self.progress.total_bytes)
        self.progress.current_label = ""
        self.progress.current_size = 0
        self.progress.current_bytes = 0
        self._refresh(immediately=True)

    def note(self, message: str) -> None:
        if self._live is not None:
            self._live.console.print(message)
        else:
            self.console.print(message)

    def stop(self) -> None:
        if self._live is None:
            return
        self._live.update(compose_display(self.progress, self.layout), refresh=True)
        self._live.stop()
        self._live = None

    def _refresh(self, *, immediately: bool) -> None:
        if self._live is None:
            return
        self._live.update(compose_display(self.progress, self.layout), refresh=immediately)


def make_reporter(console: Console, *, quiet: bool) -> ProgressReporter:
    """Return the reporter matching the requested verbosity."""
    if quiet:
        return SilentProgressReporter()
    return RichProgressReporter(console)
