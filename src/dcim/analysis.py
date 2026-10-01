"""Pure computations behind ``dcim status``: histogram, presence, suggestion.

Nothing here touches a device, a mount or the terminal: the functions take a
list of :class:`~dcim.selection.MediaFile` and return dataclasses, which is what
makes the histogram and the suggested date range unit-testable.
"""

import os
import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .dates import default_date_range
from .formatting import format_bytes, format_count
from .selection import MediaFile

SIZE_PATTERN = re.compile(r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>[kmgt]?)(?:i?b)?", re.IGNORECASE)
SIZE_UNITS = {"": 1, "k": 1000, "m": 1000**2, "g": 1000**3, "t": 1000**4}


@dataclass(frozen=True)
class MonthStat:
    """One month of the histogram."""

    month: str
    file_count: int
    total_size: int
    present_count: int
    present_size: int

    @property
    def missing_count(self) -> int:
        return self.file_count - self.present_count

    @property
    def missing_size(self) -> int:
        return self.total_size - self.present_size

    @property
    def is_complete(self) -> bool:
        return self.file_count == self.present_count


@dataclass(frozen=True)
class Histogram:
    """The month-by-month view of a source directory."""

    months: list[MonthStat]

    @property
    def file_count(self) -> int:
        return sum(month.file_count for month in self.months)

    @property
    def total_size(self) -> int:
        return sum(month.total_size for month in self.months)

    @property
    def present_count(self) -> int:
        return sum(month.present_count for month in self.months)

    @property
    def present_size(self) -> int:
        return sum(month.present_size for month in self.months)

    @property
    def missing_count(self) -> int:
        return self.file_count - self.present_count

    @property
    def missing_size(self) -> int:
        return self.total_size - self.present_size


@dataclass(frozen=True)
class StorageSpace:
    """How full the device is, and what a run would change.

    `used` and `free` are optional because MTP devices do not always report
    them: when they are missing, the capacity alone still answers "how big is
    this compared to my phone".
    """

    total: int
    used: int | None = None
    free: int | None = None

    @property
    def used_ratio(self) -> float:
        """Share of the capacity in use, or 0 when unknown."""
        return (self.used / self.total) if self.used is not None and self.total else 0.0

    def free_after(self, freed: int) -> int | None:
        """Return the free space once `freed` bytes have been deleted."""
        if self.free is None:
            return None
        return min(self.free + freed, self.total)

    def other_used(self, source_size: int) -> int | None:
        """Return the used space that is not the source directory."""
        if self.used is None:
            return None
        return max(self.used - source_size, 0)


def with_capacity_override(space: StorageSpace | None, capacity: int | None) -> StorageSpace | None:
    """Apply a capacity stated by the user to what the device reported.

    An explicit `--phone-capacity` wins over the device, since a device that
    reports nonsense would otherwise be believed; the free space is then kept
    only when it is consistent with that capacity.
    """
    if capacity is None:
        return space
    free = space.free if space is not None else None
    if free is not None and free > capacity:
        free = None
    return StorageSpace(
        total=capacity, used=capacity - free if free is not None else None, free=free
    )


@dataclass(frozen=True)
class Suggestion:
    """A ready-to-run date range, and what makes it interesting."""

    start_month: str
    end_month: str
    file_count: int
    total_size: int
    missing_count: int
    missing_size: int
    kept_months: int
    is_partial: bool = False


@dataclass
class DestinationIndex:
    """What the destination already holds, keyed by file name.

    Only the names the caller asked for are kept (`wanted_names`), and a name
    maps to *every* size seen for it: the same name can legitimately exist in
    several albums, and one of them may be a truncated copy. Presence is
    therefore "a file with this name **and** this size exists somewhere below
    the destination root".
    """

    sizes: dict[str, set[int]] = field(default_factory=dict)
    files_seen: int = 0
    directories_seen: int = 0
    unreadable_directories: int = 0
    stats_performed: int = 0
    max_depth: int | None = 0

    @property
    def matched_names(self) -> int:
        """How many distinct file names of interest were found."""
        return len(self.sizes)

    def contains(self, name: str, size: int) -> bool:
        """Tell whether a file of this name and size is already there."""
        return size in self.sizes.get(name, frozenset())

    def record(self, name: str, size: int) -> None:
        """Remember one destination file."""
        self.sizes.setdefault(name, set()).add(size)


def build_destination_index(
    destination: Path,
    *,
    wanted_names: Collection[str] | None = None,
    max_depth: int | None = 0,
    on_progress: Callable[[int], None] | None = None,
) -> DestinationIndex:
    """Index the destination by file name, cheaply.

    Two things keep this fast on a network share:

    * `max_depth` bounds the walk. The default (0) reads the destination root
      only, which is the flat layout; storing media in sub-directories means
      asking for a deeper walk explicitly.
    * when `wanted_names` is given, a file is `stat`ed only if its name is
      wanted. Walking a CIFS tree of 45 000 files costs about 12 000 `stat`
      calls less than a full index, which is the difference between "instant"
      and "looks hung".
    """
    index = DestinationIndex(max_depth=max_depth)
    if not destination.is_dir():
        return index
    if wanted_names is not None and not wanted_names:
        return index
    wanted = set(wanted_names) if wanted_names is not None else None
    pending: list[tuple[Path, int]] = [(destination, 0)]
    while pending:
        directory, depth = pending.pop()
        try:
            scandir_context = os.scandir(directory)
        except OSError:
            index.unreadable_directories += 1
            continue
        with scandir_context as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue  # manifest directory, trash directories, thumbnails
                if entry.is_dir(follow_symlinks=False):
                    index.directories_seen += 1
                    if max_depth is None or depth < max_depth:
                        pending.append((Path(entry.path), depth + 1))
                    continue
                index.files_seen += 1
                if wanted is not None and entry.name not in wanted:
                    continue
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                index.stats_performed += 1
                index.record(entry.name, size)
        if on_progress is not None:
            on_progress(index.files_seen)
    return index


def build_histogram(
    media_files: list[MediaFile], destination_index: DestinationIndex | None = None
) -> Histogram:
    """Aggregate candidates by capture month, with their presence at destination."""
    index = destination_index or DestinationIndex()
    counts: dict[str, int] = {}
    sizes: dict[str, int] = {}
    present_counts: dict[str, int] = {}
    present_sizes: dict[str, int] = {}
    for media_file in media_files:
        month = media_file.month_label
        counts[month] = counts.get(month, 0) + 1
        sizes[month] = sizes.get(month, 0) + media_file.size
        if index.contains(media_file.filename, media_file.size):
            present_counts[month] = present_counts.get(month, 0) + 1
            present_sizes[month] = present_sizes.get(month, 0) + media_file.size
    return Histogram(
        months=[
            MonthStat(
                month=month,
                file_count=counts[month],
                total_size=sizes[month],
                present_count=present_counts.get(month, 0),
                present_size=present_sizes.get(month, 0),
            )
            for month in sorted(counts)
        ]
    )


def suggest_range(
    histogram: Histogram,
    *,
    keep_months: int = 0,
    target_size: int | None = None,
    today: date | None = None,
) -> Suggestion | None:
    """Suggest the date range to hand to ``dcim sync``.

    The rule: start at the oldest month seen on the phone, and stop before the
    months the user wants to keep. `keep_months` drops that many months from the
    recent end, and the current (incomplete) month is never suggested — a sync
    run over a month still being written would have to be redone a few days
    later. `target_size` cuts the range at the oldest months until the size to
    free is reached, marking the range as partial when the last month overshoots.
    """
    if today is None:
        today = date.today()
    current_month = f"{today:%Y-%m}"
    candidates = [month for month in histogram.months if month.month < current_month]
    if keep_months > 0:
        candidates = candidates[:-keep_months] if keep_months < len(candidates) else []
    if not candidates:
        return None
    selected = _select_months(candidates, target_size)
    # Months left on the phone: the ones after the range, plus the current one.
    kept_months = len(candidates) - len(selected) + keep_months + 1
    is_partial = len(selected) < len(candidates) and target_size is not None
    return Suggestion(
        start_month=selected[0].month,
        end_month=selected[-1].month,
        file_count=sum(month.file_count for month in selected),
        total_size=sum(month.total_size for month in selected),
        missing_count=sum(month.missing_count for month in selected),
        missing_size=sum(month.missing_size for month in selected),
        kept_months=kept_months,
        is_partial=is_partial,
    )


def _select_months(candidates: list[MonthStat], target_size: int | None) -> list[MonthStat]:
    if target_size is None:
        return candidates
    selected: list[MonthStat] = []
    running_size = 0
    for month in candidates:
        selected.append(month)
        running_size += month.total_size
        if running_size >= target_size:
            break
    return selected


def kept_months_after(
    suggestion: Suggestion | None, histogram: Histogram, *, current_month: str
) -> list[str]:
    """Labels of the months that stay on the phone once the range has moved.

    The current month is listed even when it holds no file yet: it is where the
    range stops, and the reader looks for that boundary in the histogram.
    """
    labels = [
        month.month
        for month in histogram.months
        if suggestion is None or month.month > suggestion.end_month
    ]
    if (
        suggestion is not None
        and current_month > suggestion.end_month
        and current_month not in labels
    ):
        labels.append(current_month)
    return labels


def format_month_list(labels: list[str], *, compact_after: int = 4) -> str:
    """Format month labels, shortening long runs to ``first … last (N months)``."""
    if len(labels) <= compact_after:
        return ", ".join(labels)
    return f"{labels[0]} … {labels[-1]} ({len(labels)} months)"


def default_range_arguments(months: int = 12, today: date | None = None) -> tuple[str, str]:
    """Return the default ``--from``/``--to`` arguments of ``dcim sync``."""
    start, end = default_date_range(today=today, months=months)
    last_month = (end.year, end.month - 1) if end.month > 1 else (end.year - 1, 12)
    return f"{start:%Y-%m}", f"{last_month[0]:04d}-{last_month[1]:02d}"


def parse_size(text: str) -> int:
    """Parse a human size such as ``30G``, ``1.5TB`` or ``512 MiB`` into bytes."""
    match = SIZE_PATTERN.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"expected a size such as 30G or 1.5TB, got {text!r}")
    value = float(match["value"].replace(",", "."))
    return int(value * SIZE_UNITS[match["unit"].lower()])


def describe_suggestion(
    suggestion: Suggestion,
    *,
    kept_months: Sequence[str] = (),
    current_month: str | None = None,
) -> list[str]:
    """Return the human-readable lines describing a suggestion.

    "What stays" is spelled out with month labels rather than a count: a count
    reads as "the last month of the range is kept", which is the opposite of
    what a range ending before the current month means. Only months that hold
    files are named; a month that does not exist yet is not a month that stays.
    """
    lines = [
        f"[bold]{suggestion.start_month}[/bold] .. [bold]{suggestion.end_month}[/bold]  "
        f"{format_count(suggestion.file_count, 'file')}, "
        f"[bold]{format_bytes(suggestion.total_size)}[/bold] would move",
    ]
    if suggestion.missing_count:
        lines.append(
            f"  of which {format_count(suggestion.missing_count, 'file')} "
            f"({format_bytes(suggestion.missing_size)}) still to transfer"
        )
    else:
        lines.append("  every file of this range is already at the destination")
    if kept_months:
        labels = ", ".join(
            f"{label} (current month)" if label == current_month else label for label in kept_months
        )
        lines.append(f"  staying on the phone: {labels}")
    else:
        lines.append(
            "  staying on the phone: nothing — the range covers every month that has files"
        )
    if suggestion.is_partial:
        lines.append("  the last month of the range is only partly needed for the target size")
    return lines
