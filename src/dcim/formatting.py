"""Human-readable formatting helpers shared by the CLI and the preview tables."""

from datetime import UTC, datetime

_SIZE_UNITS = ("B", "kB", "MB", "GB", "TB", "PB")

BAR_FILLED = "█"
BAR_TRACK = "░"


def render_bar(value: int, maximum: int, width: int) -> str:
    """Render a proportional bar with two glyphs, readable without colours.

    The filled part is drawn with `█` and the remainder with `░`, so the bar says
    the same thing on a monochrome terminal as on a 256-colour one.
    """
    if width <= 0:
        return ""
    if maximum <= 0:
        return BAR_TRACK * width
    filled = min(round(max(value, 0) / maximum * width), width)
    return BAR_FILLED * filled + BAR_TRACK * (width - filled)


def shorten(text: str, width: int) -> str:
    """Truncate `text` to `width` characters, with an ellipsis when cut."""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def format_bytes(count: int) -> str:
    """Format a byte count with a binary-safe decimal unit (1000-based)."""
    value = float(count)
    for unit in _SIZE_UNITS:
        if abs(value) < 1000 or unit == _SIZE_UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1000
    raise AssertionError("unreachable")


def format_bytes_per_second(count: float) -> str:
    """Format a throughput such as ``12.34 MB/s``."""
    return f"{format_bytes(int(count))}/s"


def format_count(count: int, singular: str, plural: str | None = None) -> str:
    """Format ``1 file`` / ``3 files``."""
    if plural is None:
        plural = f"{singular}s"
    return f"{count} {singular if count == 1 else plural}"


def format_duration(seconds: float) -> str:
    """Format a duration in seconds as ``1h02m03s``, ``02m03s`` or ``03s``."""
    if seconds < 0 or seconds != seconds:  # negative or NaN: unknown
        return "unknown"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes:02d}m{secs:02d}s"
    return f"{secs}s"


def format_datetime(moment: datetime) -> str:
    """Format a datetime for display, in local time."""
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_utc_timestamp(moment: datetime | None = None) -> str:
    """Return the current UTC time as an ISO-8601 string, for log records."""
    if moment is None:
        moment = datetime.now(UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds")
