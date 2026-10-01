"""Tests for the capture date resolution and the date range parsing."""

from datetime import date, datetime

import pytest

from dcim.dates import (
    default_date_range,
    parse_filename_datetime,
    parse_range_bound,
    resolve_capture_date,
)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("PXL_20250801_163922349.MP.jpg", datetime(2025, 8, 1, 16, 39, 22)),
        ("PXL_20250801_163922349.TS.mp4", datetime(2025, 8, 1, 16, 39, 22)),
        ("VID_20260102_101112.mp4", datetime(2026, 1, 2, 10, 11, 12)),
        ("Screenshot_20250801-163922.png", datetime(2025, 8, 1, 16, 39, 22)),
        ("IMG-2025-09-30-12-00-00.jpg", datetime(2025, 9, 30, 12, 0, 0)),
        ("2025-09-30 12.00.00.jpg", datetime(2025, 9, 30, 12, 0, 0)),
        ("holiday-2025-09-30.jpg", datetime(2025, 9, 30, 0, 0, 0)),
        ("trashed-1754066365-photo.jpg", None),
        ("photo.jpg", None),
        ("IMG_20251345_101112.jpg", None),
    ],
)
def test_parse_filename_datetime(filename, expected):
    assert parse_filename_datetime(filename) == expected


def test_resolve_capture_date_prefers_the_filename():
    capture_date = resolve_capture_date("PXL_20250801_163922349.jpg", modified_at=0)
    assert capture_date.source == "filename"
    assert capture_date.value == datetime(2025, 8, 1, 16, 39, 22)


def test_resolve_capture_date_falls_back_on_mtime():
    capture_date = resolve_capture_date(
        "photo.jpg", modified_at=datetime(2026, 3, 4, 5, 6, 7).timestamp()
    )
    assert capture_date.source == "mtime"
    assert capture_date.value == datetime(2026, 3, 4, 5, 6, 7)


def test_default_date_range_covers_the_last_complete_months():
    start, end = default_date_range(today=date(2026, 9, 27), months=12)
    assert start == date(2025, 9, 1)
    assert end == date(2026, 9, 1)


def test_default_date_range_across_a_year_boundary():
    # The current, incomplete month is excluded from the default window.
    start, end = default_date_range(today=date(2026, 1, 15), months=3)
    assert start == date(2025, 10, 1)
    assert end == date(2026, 1, 1)


def test_parse_range_bound_month_is_inclusive_when_end():
    assert parse_range_bound("2025-09", is_end=False) == datetime(2025, 9, 1)
    assert parse_range_bound("2026-08", is_end=True) == datetime(2026, 9, 1)


def test_parse_range_bound_day_is_exclusive_when_end():
    assert parse_range_bound("2026-08-31", is_end=False) == datetime(2026, 8, 31)
    assert parse_range_bound("2026-08-31", is_end=True) == datetime(2026, 9, 1)


@pytest.mark.parametrize("text", ["", "2025", "2025-13", "août 2025", "2025-09-31"])
def test_parse_range_bound_rejects_invalid_input(text):
    with pytest.raises(ValueError, match=r"invalid|expected|must be in range"):
        parse_range_bound(text, is_end=False)
