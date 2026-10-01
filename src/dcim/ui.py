"""Everything the user sees and answers: previews, tables and prompts.

The preview is the contract of the run: the user must be able to tell, before
anything is copied, how many files were matched, where they come from, how much
space they take and in which months they were captured.
"""

import itertools
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from .analysis import Histogram, MonthStat, StorageSpace, Suggestion, describe_suggestion
from .deletion import DeletionReport
from .formatting import format_bytes, format_count, format_datetime, render_bar
from .selection import SelectionSummary
from .transfer import TransferOutcome

MONTHLY_TABLE_LIMIT = 24


def build_console(*, quiet: bool = False, stderr: bool = False) -> Console:
    """Return the console used everywhere in the program.

    In machine-readable mode (``--json``) the console writes to stderr, so that
    stdout carries nothing but the JSON document.
    """
    return Console(quiet=quiet, stderr=stderr, highlight=False, soft_wrap=False)


def banner(console: Console, title: str) -> None:
    console.print(Panel.fit(title, border_style="cyan"))


def section(console: Console, title: str) -> None:
    console.print(f"\n[bold cyan]── {title}[/bold cyan]")


@contextmanager
def working(console: Console, message: str) -> Iterator[None]:
    """Announce a slow step, and keep a spinner alive while it runs.

    The message is printed in every mode — a redirected log must still say what
    is going on — while the spinner only appears on a terminal.
    """
    console.print(f"[dim]{message}[/dim]")
    status = console.status("", spinner="dots")
    if console.is_terminal:
        status.start()
    try:
        yield
    finally:
        if console.is_terminal:
            status.stop()


def ask_value(console: Console, question: str, default: str) -> str:
    """Ask for a free-form value with a default.

    Without a terminal (a script, a cron job, a test), or when the input reaches
    its end (Ctrl+D), the default is returned: every prompt has an answer that
    does not require the user, and none of them can spin on an absent input.
    """
    if not is_interactive(console):
        console.print(f"[dim]{question}: {default} (default, not interactive)[/dim]")
        return default
    try:
        return Prompt.ask(f"[bold]{question}[/bold]", default=default, console=console).strip()
    except EOFError:
        console.print(f"[dim]end of input: {default} is used[/dim]")
        return default


def ask_confirmation(console: Console, question: str, *, default: bool = False) -> bool:
    """Ask a yes/no question, using `default` for an empty or absent answer."""
    if not is_interactive(console):
        console.print(
            f"[dim]{question} {'yes' if default else 'no'} (default, not interactive)[/dim]"
        )
        return default
    try:
        return Confirm.ask(f"[bold]{question}[/bold]", default=default, console=console)
    except EOFError:
        console.print(f"[dim]end of input: {'yes' if default else 'no'} is used[/dim]")
        return default


def is_interactive(console: Console) -> bool:
    """Tell whether prompts can actually be answered."""
    return console.is_terminal and sys.stdin.isatty()


def ask_date_range(console: Console, default_start: str, default_end: str) -> tuple[str, str]:
    """Ask for the first and last month (or day) to include."""
    console.print(
        "[dim]A bound is YYYY-MM (the whole month is included) or YYYY-MM-DD (that "
        "day is included, the files of the following one are not).[/dim]"
    )
    start = ask_value(console, "First month or day to include", default_start)
    end = ask_value(console, "Last month or day to include", default_end)
    return start, end


def render_preview(console: Console, summary: SelectionSummary) -> None:
    """Print what is about to move: how much, from when, and per month.

    No list of file names: twelve near-identical names teach nothing that the
    monthly table and the totals do not already say. What matters before copying
    is the volume, the period, and where the dates came from — the number of files
    dated from their modification time is the one signal worth reading twice.
    """
    if summary.count == 0:
        console.print("[yellow]No file matches these criteria.[/yellow]")
        return
    oldest, newest = summary.oldest, summary.newest
    console.print(
        f"[bold]{format_count(summary.count, 'file')}[/bold], "
        f"[bold]{format_bytes(summary.total_size)}[/bold] to copy — the same volume "
        "will be freed on the phone once the copies are verified"
    )
    if oldest is not None and newest is not None:
        console.print(
            f"[dim]capture dates from {format_datetime(oldest.capture_date.value)} "
            f"to {format_datetime(newest.capture_date.value)}[/dim]"
        )
    sources = ", ".join(f"{source}: {count}" for source, count in summary.date_sources)
    console.print(f"[dim]dates read from {sources}[/dim]")

    per_month = summary.per_month
    table = Table(title="Files per month", title_justify="left")
    table.add_column("Month")
    table.add_column("Files", justify="right")
    table.add_column("Size", justify="right")
    for month, count, size in per_month[:MONTHLY_TABLE_LIMIT]:
        table.add_row(month, str(count), format_bytes(size))
    if len(per_month) > MONTHLY_TABLE_LIMIT:
        table.add_row("…", "…", "…")
    console.print(table)


def render_transfer_report(console: Console, outcomes: list[TransferOutcome]) -> None:
    """Print what happened during the copy phase."""
    counts: dict[str, int] = {}
    sizes: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
        sizes[outcome.status] = sizes.get(outcome.status, 0) + outcome.media_file.size
    table = Table(title="Copy phase", title_justify="left")
    table.add_column("Status")
    table.add_column("Files", justify="right")
    table.add_column("Size", justify="right")
    for status in ("copied", "already_present", "conflict", "failed"):
        if status not in counts:
            continue
        table.add_row(status, str(counts[status]), format_bytes(sizes[status]))
    console.print(table)
    verified = sum(1 for outcome in outcomes if outcome.verified and outcome.status != "failed")
    console.print(f"[bold]{verified}[/bold] files verified on the destination")
    for outcome in outcomes:
        if outcome.status in ("failed", "conflict"):
            console.print(f"[red]✗[/red] {outcome.filename}: {outcome.detail}")


def render_deletion_report(console: Console, report: DeletionReport) -> None:
    """Print what happened during the deletion phase."""
    table = Table(title="Deletion phase", title_justify="left")
    table.add_column("Outcome")
    table.add_column("Files", justify="right")
    table.add_row("deleted from the phone", str(len(report.deleted)))
    table.add_row("kept", str(len(report.skipped)))
    table.add_row("failed", str(len(report.failed)))
    console.print(table)
    console.print(f"[bold green]{format_bytes(report.freed_bytes)} freed on the phone[/bold green]")
    for outcome, reason in report.skipped:
        console.print(f"[yellow]•[/yellow] kept {outcome.filename}: {reason}")
    for outcome, reason in report.failed:
        console.print(f"[red]✗[/red] {outcome.filename}: {reason}")


def ask_deletion_confirmation(
    console: Console, *, file_count: int, total_size: int, assume_yes: bool
) -> bool:
    """Ask for the final, irreversible confirmation."""
    if assume_yes:
        return True
    console.print(
        f"\n[bold red]About to delete {file_count} originals "
        f"({format_bytes(total_size)}) from the phone.[/bold red]"
    )
    console.print("[dim]The verified copies on the destination are kept.[/dim]")
    if not is_interactive(console):
        console.print(
            "[yellow]Not interactive: the originals were kept. Pass --yes to confirm.[/yellow]"
        )
        return False
    # Yes by default: reaching this question already required --delete, a verified
    # copy of every file counted above, and pressing Enter is what one expects.
    # Answer `n` to keep everything.
    return ask_confirmation(console, "Delete these originals now?", default=True)


def _is_in_range(month: str, suggested_range: tuple[str, str]) -> bool:
    """Tell whether a month label belongs to a suggested range, bounds included."""
    return suggested_range[0] <= month <= suggested_range[1]


def _directory_names(directory: Path) -> list[str]:
    try:
        return sorted(
            entry.name for entry in os.scandir(directory) if entry.is_dir(follow_symlinks=False)
        )
    except OSError:
        return []


BAR_WIDTH = 14


def render_histogram(
    console: Console,
    histogram: Histogram,
    *,
    with_destination: bool,
    suggested_range: tuple[str, str] | None = None,
    has_months_outside_the_range: bool = False,
) -> None:
    """Print the month-by-month histogram, optionally with destination presence.

    The months of the suggested range are delimited by horizontal rules and
    marked with `▸`, so the range is readable at a glance instead of being
    recomputed in the reader's head. Only months that hold files are listed: a
    month that does not exist yet has nothing to say.
    """
    if not histogram.months:
        console.print("[yellow]No file matches these criteria.[/yellow]")
        return
    table = Table(title="Files per month", title_justify="left", box=box.HORIZONTALS)
    table.add_column("Month")
    table.add_column("Files", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Distribution", no_wrap=True)
    table.add_column("Cumulative", justify="right")
    if with_destination:
        table.add_column("Copied", justify="right", no_wrap=True)
    table.add_column("", no_wrap=True)
    largest = max(month.total_size for month in histogram.months)
    running_size = 0
    in_range_before = False
    for month in histogram.months:
        running_size += month.total_size
        in_range = suggested_range is not None and _is_in_range(month.month, suggested_range)
        if in_range != in_range_before and month is not histogram.months[0]:
            table.add_section()
        in_range_before = in_range
        row = [
            f"[bold]{month.month}[/bold]" if in_range else month.month,
            str(month.file_count),
            format_bytes(month.total_size),
            f"[cyan]{render_bar(month.total_size, largest, BAR_WIDTH)}[/cyan]",
            format_bytes(running_size),
        ]
        if with_destination:
            row.append(render_copied(month))
        row.append("[bold red]▸[/bold red]" if in_range else "")
        table.add_row(*row, style="bold" if in_range else None)
    console.print(table)
    if suggested_range is not None:
        legend = (
            f"[dim][bold red]▸[/bold red] = months of the suggested range "
            f"{suggested_range[0]} .. {suggested_range[1]}"
        )
        if has_months_outside_the_range:
            legend += "; the other months stay on the phone"
        console.print(legend + "[/dim]")


def render_copied(month: MonthStat, *, width: int = 6) -> str:
    """Render how much of a month is already at the destination, as a bar + %.

    A share, not a count and not a check mark: "to copy: ✓" reads as "everything
    is to be copied", which is the opposite of what a complete month means.
    """
    ratio = month.present_count / month.file_count if month.file_count else 0
    bar = render_bar(month.present_count, month.file_count, width)
    percentage = f"{ratio * 100:3.0f}%"
    if month.is_complete:
        return f"[green]{bar} {percentage}[/green]"
    if month.present_count:
        return f"[yellow]{bar} {percentage}[/yellow]"
    return f"[dim]{bar} {percentage}[/dim]"


def segment_lengths(amounts: list[int], width: int) -> list[int]:
    """Split `width` characters proportionally, so the parts always sum to it."""
    total = sum(amounts)
    if total <= 0:
        return [0] * len(amounts)
    boundaries = [round(sum(amounts[:index]) / total * width) for index in range(len(amounts) + 1)]
    return [max(after - before, 0) for before, after in itertools.pairwise(boundaries)]


def capacity_segments(
    space: StorageSpace, *, source_size: int, freed: int
) -> list[tuple[int, str, str]]:
    """Return the bar segments as ``(bytes, glyph, style)``.

    The used part of the bar is drawn with `█`, the remaining space with `░` so
    that the two are told apart even without colours. When the device does not
    report a free space, the remaining capacity is drawn as unknown rather than
    claimed to be free.
    """
    to_free = min(freed, space.total)
    kept = min(max(source_size - freed, 0), max(space.total - to_free, 0))
    remaining = max(space.total - to_free - kept, 0)
    if space.used is None:
        return [(to_free, "█", "cyan"), (kept, "█", "blue"), (remaining, "░", "grey62")]
    other = min(space.other_used(source_size) or 0, remaining)
    free = max(remaining - other, 0)
    return [
        (to_free, "█", "cyan"),
        (kept, "█", "blue"),
        (other, "█", "grey62"),
        (free, "░", "green"),
    ]


def capacity_bar(space: StorageSpace, *, source_size: int, freed: int, width: int = 34) -> Text:
    """Draw the phone's capacity: to free, kept in the source, other, remaining."""
    segments = capacity_segments(space, source_size=source_size, freed=freed)
    lengths = segment_lengths([amount for amount, _glyph, _style in segments], width)
    text = Text()
    for (amount, glyph, style), length in zip(segments, lengths, strict=True):
        text.append(glyph * length if amount else "", style=style)
    return text


def render_space(
    console: Console,
    *,
    space: StorageSpace | None,
    source_label: str,
    source_size: int,
    source_files: int,
    freed: int,
    freed_files: int,
    suggested_range: tuple[str, str] | None,
    to_transfer: tuple[int, int] | None = None,
) -> None:
    """Print how full the phone is, what the source takes, and what would remain.

    This is the section that answers "how much do I get back?": the totals in
    words, then the same thing as a bar, where the part that would be freed is a
    segment of its own.
    """
    section(console, "Space")
    if space is not None and space.used is not None and space.free is not None:
        console.print(
            f"Phone storage    total [bold]{format_bytes(space.total)}[/bold] · used "
            f"[bold]{format_bytes(space.used)}[/bold] ({space.used_ratio * 100:.0f}%) · "
            f"free [bold]{format_bytes(space.free)}[/bold]"
        )
    elif space is not None:
        console.print(f"Phone storage    capacity [bold]{format_bytes(space.total)}[/bold]")
    if space is not None and space.used:
        share = source_size / space.used * 100
        console.print(
            f"{source_label} ({format_count(source_files, 'file')})  "
            f"[bold]{format_bytes(source_size)}[/bold] — {share:.0f}% of what is used"
        )
    else:
        share = source_size / space.total * 100 if space is not None else 0
        suffix = f" — {share:.0f}% of the capacity" if space is not None else ""
        console.print(
            f"{source_label} ({format_count(source_files, 'file')})  "
            f"[bold]{format_bytes(source_size)}[/bold]{suffix}"
        )
    if to_transfer is not None:
        transfer_files, transfer_size = to_transfer
        total_files = source_files
        copied = max(total_files - transfer_files, 0)
        share = f"{copied / total_files * 100:.0f}% copied" if total_files else ""
        console.print(
            "At destination   "
            + (
                "[green]everything is already copied[/green]"
                if not transfer_files
                else f"[bold]{format_count(transfer_files, 'file')}[/bold] "
                f"([bold]{format_bytes(transfer_size)}[/bold]) left to transfer"
            )
            + (f" — {share}" if share else "")
        )
    if space is None:
        console.print(
            "[dim]the device reports neither its capacity nor its free space; pass "
            "--phone-capacity (e.g. 128G) to see the shares[/dim]"
        )
        return
    free_after = space.free_after(freed) if freed else None
    if suggested_range is not None and freed:
        console.print(
            f"Would move       [bold]{format_bytes(freed)}[/bold] — "
            f"{format_count(freed_files, 'file')}, months "
            f"{suggested_range[0]} .. {suggested_range[1]}"
        )
        if free_after is not None and space.free is not None:
            console.print(
                f"After the move   [bold green]free {format_bytes(free_after)}[/bold green] "
                f"[dim](was {format_bytes(space.free)})[/dim]"
            )
        else:
            console.print(f"After the move   [bold green]+{format_bytes(freed)}[/bold green] freed")
    console.print()
    bar = Text("Phone  ")
    bar.append_text(capacity_bar(space, source_size=source_size, freed=freed))
    bar.append("  ")
    bar.append(
        f"{format_bytes(space.used)} / {format_bytes(space.total)} used"
        if space.used is not None
        else f"capacity {format_bytes(space.total)}"
    )
    console.print(bar)
    legend = Text("       ")
    if freed:
        legend.append(f"█ to free {format_bytes(freed)}  ", style="cyan")
        if source_size > freed:
            legend.append(
                f"█ stays on the phone {format_bytes(source_size - freed)}  ", style="blue"
            )
        else:
            legend.append("█ nothing stays  ", style="blue")
    else:
        legend.append(f"█ {source_label} {format_bytes(source_size)}  ", style="blue")
    if space.used is None:
        legend.append("░ rest of the capacity", style="grey62")
    else:
        legend.append(
            f"█ other {format_bytes(space.other_used(source_size) or 0)}  ", style="grey62"
        )
        legend.append(f"░ free {format_bytes(space.free or 0)}", style="green")
    console.print(legend)


def render_suggestion(
    console: Console,
    suggestion: Suggestion | None,
    *,
    kept_months: Sequence[str] = (),
    current_month: str | None = None,
) -> None:
    """Print the suggested range and the command to run it."""
    section(console, "Suggestion")
    if suggestion is None:
        console.print(
            "[yellow]Nothing to suggest: no complete month is still on the phone.[/yellow]"
        )
        return
    for line in describe_suggestion(
        suggestion, kept_months=kept_months, current_month=current_month
    ):
        console.print(line)
    arguments = f"--from {suggestion.start_month} --to {suggestion.end_month}"
    console.print("\n  [bold]dcim sync[/bold] " + arguments)
    console.print(
        "  [bold]dcim sync[/bold] "
        + arguments
        + " [bold red]--delete[/bold red]"
        + f"   [dim]# frees {format_bytes(suggestion.total_size)} on the phone[/dim]"
    )
