"""Resolving the capture date of a media file, and parsing date ranges.

The capture date is taken from the file name when it contains a recognizable
timestamp — camera applications keep it there and it survives a file system
that rewrites modification times (MTP devices and most sync tools do). The
modification time is used as a fallback only.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

DateSource = Literal["filename", "mtime"]

# ``PXL_20250801_163922349.MP.jpg``, ``VID_20260102_101112.mp4``,
# ``Screenshot_20250801-163922.png``, ``trashed-1754066365-...`` (no match).
_TIMESTAMP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?<!\d)(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})"
        r"[-_T ]?(?P<hour>\d{2})(?P<minute>\d{2})(?P<second>\d{2})"
    ),
    re.compile(
        r"(?<!\d)(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
        r"[-_T ](?P<hour>\d{2})[.:-](?P<minute>\d{2})[.:-](?P<second>\d{2})"
    ),
)

_DATE_ONLY_PATTERN = re.compile(r"(?<!\d)(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})(?!\d)")


@dataclass(frozen=True)
class CaptureDate:
    """The date a media file was captured, and where it comes from."""

    value: datetime
    source: DateSource

    @property
    def month_label(self) -> str:
        """Return the ``YYYY-MM`` label used by the preview summary."""
        return self.value.strftime("%Y-%m")


def parse_filename_datetime(filename: str) -> datetime | None:
    """Extract a timestamp from a file name, or return None."""
    for pattern in _TIMESTAMP_PATTERNS:
        match = pattern.search(filename)
        if match is None:
            continue
        try:
            return _datetime_from_match(match)
        except ValueError:
            continue  # e.g. 2025-13-45: not a real date, keep searching.
    match = _DATE_ONLY_PATTERN.search(filename)
    if match is None:
        return None
    try:
        return _datetime_from_match(match)
    except ValueError:
        return None


def _datetime_from_match(match: re.Match[str]) -> datetime:
    """Build a datetime from the named groups of a timestamp pattern."""
    groups = match.groupdict()
    return datetime(
        year=int(groups["year"]),
        month=int(groups["month"]),
        day=int(groups["day"]),
        hour=int(groups.get("hour") or 0),
        minute=int(groups.get("minute") or 0),
        second=int(groups.get("second") or 0),
    )


def resolve_capture_date(filename: str, modified_at: float) -> CaptureDate:
    """Return the capture date of a file, preferring its name over its mtime."""
    from_filename = parse_filename_datetime(filename)
    if from_filename is not None:
        return CaptureDate(value=from_filename, source="filename")
    return CaptureDate(value=datetime.fromtimestamp(modified_at), source="mtime")


def default_date_range(today: date | None = None, months: int = 12) -> tuple[date, date]:
    """Return the last `months` complete months as a half-open range.

    With `today` = 2026-09-27 and `months` = 12 this returns
    (2025-09-01, 2026-09-01), i.e. the 2025-09 .. 2026-08 window.
    """
    if today is None:
        today = date.today()
    year, month = today.year, today.month
    # The window ends with the current, incomplete month and goes back `months`
    # complete months: today 2026-09-27 with 12 months gives 2025-09..2026-08.
    first_of_current_month = date(year, month, 1)
    start_months = (year * 12 + (month - 1)) - months
    start_year, start_month = divmod(start_months, 12)
    return date(start_year, start_month + 1, 1), first_of_current_month


def parse_range_bound(text: str, *, is_end: bool) -> datetime:
    """Parse ``YYYY-MM`` or ``YYYY-MM-DD`` into a range bound.

    ``is_end`` makes the bound exclusive: ``--to 2026-08`` must include the
    whole month of August, so it is read as 2026-09-01T00:00.
    """
    stripped = text.strip()
    month_match = re.fullmatch(r"(?P<year>\d{4})-(?P<month>\d{2})", stripped)
    if month_match is not None:
        year, month = int(month_match["year"]), int(month_match["month"])
        if not 1 <= month <= 12:
            raise ValueError(f"invalid month in {text!r}")
        if is_end:
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return datetime(year, month, 1)
    day_match = re.fullmatch(r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})", stripped)
    if day_match is not None:
        parsed = date(int(day_match["year"]), int(day_match["month"]), int(day_match["day"]))
        if is_end:
            return datetime.combine(parsed.fromordinal(parsed.toordinal() + 1), datetime.min.time())
        return datetime.combine(parsed, datetime.min.time())
    raise ValueError(f"expected YYYY-MM or YYYY-MM-DD, got {text!r}")
