"""Building the list of files to transfer: scanning, filtering, summarizing.

The three filters are applied in this order:

1. the glob pattern, matched against the file name (or the path relative to the
   source directory when the pattern contains a ``/``);
2. the media extension filter, unless disabled;
3. the capture date range, half-open: ``start <= captured_at < end``.
"""

import fnmatch
import os
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .dates import CaptureDate, resolve_capture_date
from .errors import SourceSelectionError

MEDIA_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".heic",
        ".heif",
        ".webp",
        ".gif",
        ".bmp",
        ".dng",
        ".raw",
        ".arw",
        ".cr2",
        ".nef",
        ".mp4",
        ".mov",
        ".3gp",
        ".3gpp",
        ".mkv",
        ".avi",
        ".webm",
        ".m4v",
        ".mpg",
        ".mpeg",
        ".ts",
    }
)

DEFAULT_EXCLUDE_PATTERNS: tuple[str, ...] = (
    ".stfolder",
    ".stversions",
    ".thumbnails",
    "thumbnails",
    "*.trashed-*",
)


@dataclass(frozen=True)
class MediaFile:
    """A candidate file living on the device."""

    device_path: Path
    """Path relative to the transport root, e.g. ``DCIM/Camera/PXL_…jpg``."""

    relative_path: Path
    """Path relative to the scanned directory, for the destination layout."""

    size: int
    modified_at: float
    capture_date: CaptureDate

    @property
    def filename(self) -> str:
        return self.device_path.name

    @property
    def month_label(self) -> str:
        return self.capture_date.month_label

    @property
    def relative_posix(self) -> str:
        return self.relative_path.as_posix()


@dataclass
class SelectionSummary:
    """Aggregated view of the candidates, used by the preview and the report."""

    files: list[MediaFile] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.files)

    @property
    def total_size(self) -> int:
        return sum(media_file.size for media_file in self.files)

    @property
    def per_month(self) -> list[tuple[str, int, int]]:
        """Return ``(month, file count, byte count)`` sorted by month."""
        counts: Counter[str] = Counter()
        sizes: Counter[str] = Counter()
        for media_file in self.files:
            counts[media_file.month_label] += 1
            sizes[media_file.month_label] += media_file.size
        return [(month, counts[month], sizes[month]) for month in sorted(counts)]

    @property
    def date_sources(self) -> list[tuple[str, int]]:
        """Return the number of files dated from the name vs from the mtime."""
        sources = Counter(media_file.capture_date.source for media_file in self.files)
        return sorted(sources.items())

    @property
    def oldest(self) -> MediaFile | None:
        return min(self.files, key=lambda media_file: media_file.capture_date.value, default=None)

    @property
    def newest(self) -> MediaFile | None:
        return max(self.files, key=lambda media_file: media_file.capture_date.value, default=None)


def parse_patterns(text: str) -> tuple[str, ...]:
    """Split a comma or whitespace separated list of glob patterns."""
    patterns = tuple(part for part in _split_pattern_list(text) if part)
    if not patterns:
        raise SourceSelectionError("the glob pattern is empty")
    return patterns


def _split_pattern_list(text: str) -> list[str]:
    parts: list[str] = []
    for comma_separated in text.split(","):
        parts.extend(comma_separated.split())
    return [part.strip() for part in parts]


def match_patterns(patterns: Sequence[str], filename: str, relative_posix: str) -> bool:
    """Tell whether a file matches at least one glob pattern."""
    for pattern in patterns:
        if "/" in pattern:
            if fnmatch.fnmatch(relative_posix, pattern.lstrip("/")):
                return True
        elif fnmatch.fnmatch(filename, pattern):
            return True
    return False


def is_excluded(patterns: Sequence[str], name: str, relative_posix: str) -> bool:
    """Tell whether a file or directory must be skipped."""
    return bool(patterns) and match_patterns(patterns, name, relative_posix)


def iter_media_files(
    transport,
    directory: str,
    *,
    patterns: Sequence[str],
    media_only: bool,
    exclude_patterns: Sequence[str] = DEFAULT_EXCLUDE_PATTERNS,
    recursive: bool = True,
) -> Iterator[MediaFile]:
    """Walk the device and yield the files matching the glob patterns.

    The walk goes through the transport, so it works the same whether the device
    is mounted or reached through adb. Symlinked directories are not followed:
    the device must stay the only source of truth.
    """
    pending = [directory.strip("/")]
    while pending:
        current = pending.pop()
        for entry in transport.list_directory(current):
            relative_posix = _relative_posix(directory, entry.path)
            if is_excluded(exclude_patterns, entry.name, relative_posix):
                continue
            if entry.is_directory:
                if recursive:
                    pending.append(entry.path)
                continue
            if media_only and Path(entry.name).suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            if not match_patterns(patterns, entry.name, relative_posix):
                continue
            yield MediaFile(
                device_path=Path(entry.path),
                relative_path=Path(relative_posix),
                size=entry.size,
                modified_at=entry.modified_at,
                capture_date=resolve_capture_date(entry.name, entry.modified_at),
            )


def _relative_posix(directory: str, path: str) -> str:
    """Return the path of an entry relative to the scanned directory."""
    prefix = directory.strip("/")
    if prefix and path.startswith(f"{prefix}/"):
        return path[len(prefix) + 1 :]
    return path.lstrip("/")


def select_media_files(media_files: list[MediaFile], *, start=None, end=None) -> list[MediaFile]:
    """Filter candidates by their capture date, half-open ``[start, end)``."""
    selected = [
        media_file
        for media_file in media_files
        if (start is None or media_file.capture_date.value >= start)
        and (end is None or media_file.capture_date.value < end)
    ]
    return sorted(selected, key=lambda media_file: media_file.capture_date.value)


def build_summary(media_files: list[MediaFile]) -> SelectionSummary:
    """Wrap candidates in a summary object."""
    return SelectionSummary(files=list(media_files))


def check_free_space(
    destination: Path, required_bytes: int, extra_margin: int = 64 * 1024**2
) -> None:
    """Raise if `destination` cannot hold `required_bytes` plus a safety margin."""
    usage = os.statvfs(destination)
    free_bytes = usage.f_bavail * usage.f_frsize
    if free_bytes < required_bytes + extra_margin:
        from .formatting import format_bytes

        raise SourceSelectionError(
            f"{destination} has only {format_bytes(free_bytes)} free, "
            f"{format_bytes(required_bytes + extra_margin)} required"
        )
