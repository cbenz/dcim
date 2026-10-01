"""Tests for the pure computations behind ``dcim status``."""

from datetime import date, datetime
from pathlib import Path

import pytest

from dcim.analysis import (
    DestinationIndex,
    StorageSpace,
    build_destination_index,
    build_histogram,
    default_range_arguments,
    describe_suggestion,
    format_month_list,
    kept_months_after,
    parse_size,
    suggest_range,
    with_capacity_override,
)
from dcim.dates import CaptureDate, default_date_range, parse_range_bound
from dcim.selection import MediaFile


def index_of(entries: dict[str, int | set[int]]) -> DestinationIndex:
    """Build a destination index from a name-to-size(s) mapping."""
    index = DestinationIndex()
    for name, sizes in entries.items():
        for size in {sizes} if isinstance(sizes, int) else sizes:
            index.record(name, size)
    return index


def make_media_file(name: str, captured: datetime, size: int = 100) -> MediaFile:
    return MediaFile(
        device_path=Path(name),
        relative_path=Path(name),
        size=size,
        modified_at=captured.timestamp(),
        capture_date=CaptureDate(value=captured, source="filename"),
    )


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch):
    """Freeze `date.today()` so the suggestions do not depend on the wall clock.

    The fixture files span 2025-01 .. 2026-09, and the ragged month in the
    middle of the range is what the assertions pin.
    """

    class FrozenDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 9, 27)

    monkeypatch.setattr("dcim.analysis.date", FrozenDate)
    return FrozenDate


@pytest.fixture
def media_files() -> list[MediaFile]:
    return [
        make_media_file("a.jpg", datetime(2025, 1, 15), 1000),
        make_media_file("b.jpg", datetime(2025, 1, 20), 2000),
        make_media_file("c.jpg", datetime(2025, 2, 10), 4000),
        make_media_file("d.jpg", datetime(2026, 8, 1), 8000),
        make_media_file("e.jpg", datetime(2026, 9, 15), 16000),
    ]


def test_histogram_groups_by_month(media_files):
    histogram = build_histogram(media_files)
    assert [month.month for month in histogram.months] == [
        "2025-01",
        "2025-02",
        "2026-08",
        "2026-09",
    ]
    assert histogram.months[0].file_count == 2
    assert histogram.months[0].total_size == 3000
    assert histogram.file_count == 5
    assert histogram.total_size == 31000


def test_histogram_counts_files_already_at_the_destination(media_files):
    # d.jpg exists at the destination with a different size: not the same file.
    index = index_of({"a.jpg": 1000, "c.jpg": 4000, "d.jpg": 1})
    histogram = build_histogram(media_files, index)
    january, february, august = histogram.months[0], histogram.months[1], histogram.months[2]
    assert (january.present_count, january.missing_count) == (1, 1)
    assert not january.is_complete
    assert february.is_complete
    assert august.present_count == 0
    assert august.missing_size == 8000
    assert histogram.missing_count == 3


def test_suggestion_covers_every_complete_month(media_files):
    suggestion = suggest_range(build_histogram(media_files), today=date(2026, 9, 27))
    assert suggestion is not None
    assert suggestion.start_month == "2025-01"
    assert suggestion.end_month == "2026-08"
    assert suggestion.file_count == 4
    assert suggestion.total_size == 15000
    assert suggestion.kept_months == 1  # the current month only


def test_suggestion_never_includes_the_current_month(media_files):
    suggestion = suggest_range(build_histogram(media_files), today=date(2026, 8, 3))
    assert suggestion is not None
    assert suggestion.end_month == "2025-02"


def test_suggestion_can_keep_the_recent_months(media_files):
    suggestion = suggest_range(build_histogram(media_files), keep_months=1, today=date(2026, 9, 27))
    assert suggestion is not None
    assert suggestion.end_month == "2025-02"
    assert suggestion.file_count == 3
    assert suggestion.kept_months == 2


def test_suggestion_returns_nothing_when_every_month_is_kept(media_files):
    suggestion = suggest_range(build_histogram(media_files), keep_months=3, today=date(2026, 9, 27))
    assert suggestion is None


def test_suggestion_fills_a_target_size_from_the_oldest_month(media_files):
    suggestion = suggest_range(
        build_histogram(media_files), target_size=5000, today=date(2026, 9, 27)
    )
    assert suggestion is not None
    assert suggestion.end_month == "2025-02"
    assert suggestion.total_size == 7000
    assert suggestion.is_partial


def test_suggestion_without_a_target_is_never_partial(media_files):
    suggestion = suggest_range(build_histogram(media_files), today=date(2026, 9, 27))
    assert suggestion is not None
    assert not suggestion.is_partial


def test_suggestion_reports_what_is_still_to_transfer(media_files):
    index = index_of({"a.jpg": 1000, "b.jpg": 2000})
    suggestion = suggest_range(build_histogram(media_files, index), today=date(2026, 9, 27))
    assert suggestion is not None
    assert suggestion.missing_count == 2
    assert suggestion.missing_size == 12000


def test_suggestion_of_an_empty_source_is_none_and_not_a_crash():
    assert suggest_range(build_histogram([])) is None


def test_describe_suggestion_names_the_months_that_stay(media_files):
    """A count would read as "the last month of the range is kept"."""
    histogram = build_histogram(media_files)
    suggestion = suggest_range(histogram, today=date(2026, 9, 27))
    assert suggestion is not None
    kept = kept_months_after(suggestion, histogram, current_month="2026-09")
    assert kept == ["2026-09"]
    lines = describe_suggestion(suggestion, kept_months=kept, current_month="2026-09")
    assert any("staying on the phone: 2026-09 (current month)" in line for line in lines)
    assert any("would move" in line for line in lines)


def test_describe_suggestion_lists_the_months_kept_by_keep_months(media_files):
    histogram = build_histogram(media_files)
    suggestion = suggest_range(histogram, keep_months=2, today=date(2026, 9, 27))
    assert suggestion is not None
    assert (suggestion.start_month, suggestion.end_month) == ("2025-01", "2025-01")
    kept = kept_months_after(suggestion, histogram, current_month="2026-09")
    assert kept == ["2025-02", "2026-08", "2026-09"]
    lines = describe_suggestion(suggestion, kept_months=kept, current_month="2026-09")
    assert any(
        "staying on the phone: 2025-02, 2026-08, 2026-09 (current month)" in line for line in lines
    )


def test_kept_months_after_covers_everything_without_a_suggestion(media_files):
    histogram = build_histogram(media_files)
    assert kept_months_after(None, histogram, current_month="2026-09") == [
        "2025-01",
        "2025-02",
        "2026-08",
        "2026-09",
    ]


def test_format_month_list_compacts_long_runs():
    assert format_month_list(["2026-01", "2026-02"]) == "2026-01, 2026-02"
    assert format_month_list(["2026-01", "2026-02", "2026-03"]) == "2026-01, 2026-02, 2026-03"
    assert format_month_list(["2026-01", "2026-02", "2026-03", "2026-04", "2026-05"]) == (
        "2026-01 … 2026-05 (5 months)"
    )


def build_destination(tmp_path: Path) -> Path:
    """A destination with one file at the root, one nested, one hidden dir."""
    (tmp_path / "a.jpg").write_bytes(b"x" * 10)
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "b.jpg").write_bytes(b"y" * 20)
    hidden = tmp_path / ".dcim"
    hidden.mkdir()
    (hidden / "manifest-1.jsonl").write_text("{}")
    return tmp_path


def test_destination_index_maps_names_to_sizes(tmp_path: Path):
    index = build_destination_index(build_destination(tmp_path), max_depth=None)
    assert index.sizes == {"a.jpg": {10}, "b.jpg": {20}}
    assert index.contains("a.jpg", 10)
    assert not index.contains("a.jpg", 11)
    assert not index.contains("missing.jpg", 10)


def test_destination_index_keeps_every_size_seen_for_a_name(tmp_path: Path):
    (tmp_path / "album").mkdir()
    (tmp_path / "album" / "a.jpg").write_bytes(b"x" * 10)
    (tmp_path / "a.jpg").write_bytes(b"x" * 99)
    index = build_destination_index(tmp_path, max_depth=None)
    assert index.sizes == {"a.jpg": {10, 99}}


def test_destination_index_reads_the_root_only_by_default(tmp_path: Path):
    index = build_destination_index(build_destination(tmp_path))
    assert index.sizes == {"a.jpg": {10}}
    assert index.directories_seen == 1  # 'sub' is seen but not entered; dot dirs skipped


def test_destination_index_can_walk_one_level(tmp_path: Path):
    index = build_destination_index(build_destination(tmp_path), max_depth=1)
    assert index.sizes == {"a.jpg": {10}, "b.jpg": {20}}


def test_destination_index_only_stats_the_wanted_names(tmp_path: Path):
    destination = build_destination(tmp_path)
    for number in range(20):
        (destination / f"other-{number}.jpg").write_bytes(b"z" * 5)
    index = build_destination_index(destination, wanted_names={"a.jpg"}, max_depth=None)
    assert index.files_seen == 22
    assert index.stats_performed == 1  # 21 other files were never stat'ed
    assert index.sizes == {"a.jpg": {10}}


def test_destination_index_without_wanted_names_is_empty_but_still_walks(tmp_path: Path):
    index = build_destination_index(build_destination(tmp_path), wanted_names=set())
    assert index.sizes == {}
    assert index.files_seen == 0


def test_destination_index_of_a_missing_directory_is_empty(tmp_path: Path):
    index = build_destination_index(tmp_path / "nope", max_depth=None)
    assert index.sizes == {}
    assert index.files_seen == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30G", 30_000_000_000),
        ("30 GB", 30_000_000_000),
        ("1.5TB", 1_500_000_000_000),
        ("512MiB", 512_000_000),
        ("1024", 1024),
    ],
)
def test_parse_size(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "30X", "1,2,3G"])
def test_parse_size_rejects_invalid_input(text):
    with pytest.raises(ValueError, match="expected a size"):
        parse_size(text)


def test_default_range_arguments_matches_the_sync_defaults():
    assert default_range_arguments(months=12, today=date(2026, 9, 27)) == ("2025-09", "2026-08")


def test_default_range_arguments_describe_the_same_window_as_sync():
    """The interactive default must be the window `sync` uses without arguments."""
    today = date(2026, 9, 27)
    start, end = default_date_range(today=today, months=12)
    from_text, to_text = default_range_arguments(months=12, today=today)
    assert parse_range_bound(from_text, is_end=False).date() == start
    assert parse_range_bound(to_text, is_end=True).date() == end


def test_a_stated_capacity_wins_over_the_device():
    """A device that reports nonsense must not be believed over the user."""
    space = with_capacity_override(
        StorageSpace(total=3_900_000_000_000, used=2_720_000_000_000, free=1_180_000_000_000),
        capacity=64_000_000_000,
    )
    assert space is not None
    assert space.total == 64_000_000_000
    assert space.free is None and space.used is None  # inconsistent: dropped


def test_a_stated_capacity_keeps_a_consistent_free_space():
    space = with_capacity_override(StorageSpace(total=100, used=90, free=10), capacity=200)
    assert space is not None
    assert space.total == 200
    assert space.free == 10
    assert space.used == 190


def test_without_a_stated_capacity_the_device_is_believed():
    reported = StorageSpace(total=100, used=90, free=10)
    assert with_capacity_override(reported, None) == reported
    assert with_capacity_override(None, None) is None
    assert with_capacity_override(None, 500) == StorageSpace(total=500)


def test_free_after_never_exceeds_the_capacity():
    space = StorageSpace(total=100, used=90, free=10)
    assert space.free_after(30) == 40
    assert space.free_after(10_000) == 100
    assert StorageSpace(total=100).free_after(30) is None


def test_other_used_is_what_the_source_directory_does_not_take():
    space = StorageSpace(total=100, used=90, free=10)
    assert space.other_used(30) == 60
    assert space.other_used(500) == 0
    assert StorageSpace(total=100).other_used(30) is None


def test_used_ratio_of_an_unknown_usage_is_zero():
    assert StorageSpace(total=100).used_ratio == 0.0
    assert StorageSpace(total=0, used=0).used_ratio == 0.0
