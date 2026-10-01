"""Tests for scanning, glob matching and date filtering."""

from datetime import datetime
from pathlib import Path

import pytest

from dcim.errors import SourceSelectionError, TransportError
from dcim.selection import (
    build_summary,
    iter_media_files,
    match_patterns,
    parse_patterns,
    select_media_files,
)
from dcim.transport import LocalTransport


@pytest.fixture
def phone(tmp_path: Path) -> Path:
    """A fake phone source directory with a few files and subdirectories."""
    source = tmp_path / "DCIM" / "Camera"
    source.mkdir(parents=True)
    (source / "PXL_20250915_101112.jpg").write_bytes(b"x" * 100)
    (source / "PXL_20260720_120000.mp4").write_bytes(b"y" * 200)
    (source / "PXL_20250101_000000.jpg").write_bytes(b"z" * 50)
    (source / "notes.txt").write_text("not media")
    (source / ".stversions").mkdir()
    (source / ".stversions" / "PXL_20250915_101112.jpg").write_bytes(b"old")
    (source / "thumbnails").mkdir()
    (source / "thumbnails" / "PXL_20250915_101112.jpg").write_bytes(b"thumb")
    return source


def scan(source: Path, patterns: str = "*", **overrides):
    """Scan a directory of the fake device, rooted two levels above it."""
    root = source.parents[1]
    directory = source.relative_to(root).as_posix()
    options = {
        "patterns": parse_patterns(patterns),
        "media_only": True,
        "exclude_patterns": (".stversions", "thumbnails"),
        "recursive": True,
    }
    options.update(overrides)
    return list(iter_media_files(LocalTransport(root), directory, **options))


def test_scan_keeps_only_media_files(phone):
    filenames = {media_file.filename for media_file in scan(phone)}
    assert filenames == {
        "PXL_20250915_101112.jpg",
        "PXL_20260720_120000.mp4",
        "PXL_20250101_000000.jpg",
    }


def test_scan_skips_excluded_directories(phone):
    filenames = {media_file.filename for media_file in scan(phone)}
    assert "old" not in filenames
    assert "thumb" not in filenames


def test_scan_can_keep_every_file(phone):
    filenames = {media_file.filename for media_file in scan(phone, media_only=False)}
    assert "notes.txt" in filenames


def test_scan_without_recursion_ignores_subdirectories(phone):
    nested = phone / "sub"
    nested.mkdir()
    (nested / "PXL_20250901_000000.jpg").write_bytes(b"nested")
    filenames = {media_file.filename for media_file in scan(phone, recursive=False)}
    assert "PXL_20250901_000000.jpg" not in filenames


def test_glob_pattern_on_the_file_name(phone):
    filenames = {media_file.filename for media_file in scan(phone, patterns="*.mp4")}
    assert filenames == {"PXL_20260720_120000.mp4"}


def test_glob_pattern_with_a_path_is_relative_to_the_source(phone):
    nested = phone / "sub"
    nested.mkdir()
    (nested / "PXL_20250901_000000.jpg").write_bytes(b"nested")
    filenames = {media_file.filename for media_file in scan(phone, patterns="sub/*.jpg")}
    assert filenames == {"PXL_20250901_000000.jpg"}


def test_relative_path_is_relative_to_the_source(phone):
    media_file = scan(phone, patterns="*.mp4")[0]
    assert media_file.relative_posix == "PXL_20260720_120000.mp4"


def test_parse_patterns_splits_on_commas_and_spaces():
    assert parse_patterns("*.jpg, *.mp4") == ("*.jpg", "*.mp4")
    assert parse_patterns("*.jpg *.mp4") == ("*.jpg", "*.mp4")


def test_parse_patterns_rejects_an_empty_pattern():
    with pytest.raises(SourceSelectionError):
        parse_patterns("  , ")


def test_match_patterns_ignores_a_leading_slash():
    assert match_patterns(("/DCIM/Camera/*.jpg",), "a.jpg", "DCIM/Camera/a.jpg")


def test_select_media_files_filters_a_half_open_range(phone):
    media_files = scan(phone)
    selected = select_media_files(media_files, start=datetime(2025, 9, 1), end=datetime(2026, 9, 1))
    assert [media_file.filename for media_file in selected] == [
        "PXL_20250915_101112.jpg",
        "PXL_20260720_120000.mp4",
    ]


def test_select_media_files_sorts_by_capture_date(phone):
    media_files = scan(phone)
    selected = select_media_files(media_files)
    assert [media_file.filename for media_file in selected] == [
        "PXL_20250101_000000.jpg",
        "PXL_20250915_101112.jpg",
        "PXL_20260720_120000.mp4",
    ]


def test_build_summary_aggregates_by_month(phone):
    summary = build_summary(scan(phone))
    assert summary.count == 3
    assert summary.total_size == 350
    assert [month for month, _count, _size in summary.per_month] == [
        "2025-01",
        "2025-09",
        "2026-07",
    ]
    assert summary.oldest is not None and summary.oldest.filename == "PXL_20250101_000000.jpg"
    assert summary.newest is not None and summary.newest.filename == "PXL_20260720_120000.mp4"


def test_scan_of_a_missing_directory_fails(tmp_path: Path):
    """An empty listing for a missing directory would be a silent lie."""
    transport = LocalTransport(tmp_path)
    with pytest.raises(TransportError, match="cannot list"):
        list(
            iter_media_files(
                transport, "nope", patterns=("*",), media_only=True, exclude_patterns=()
            )
        )
