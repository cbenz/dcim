"""Tests for the rendering of the status report.

Assertions are made on the text a Console writes, since what is being tested is
exactly what the user reads: the demarcation of the suggested range, the
capacity bar, and the wording of the numbers.
"""

from io import StringIO

from rich.console import Console

from dcim.analysis import Histogram, MonthStat, StorageSpace
from dcim.ui import (
    capacity_bar,
    render_histogram,
    render_space,
    segment_lengths,
)

GIGABYTE = 1_000_000_000


def render(callable_, width: int = 100) -> str:
    """Run a render function against a console that captures its output."""
    buffer = StringIO()
    console = Console(file=buffer, width=width, no_color=True)
    callable_(console)
    return buffer.getvalue()


def make_histogram(months: list[tuple[str, int, int]]) -> Histogram:
    return Histogram(
        months=[
            MonthStat(
                month=month,
                file_count=file_count,
                total_size=size,
                present_count=file_count,
                present_size=size,
            )
            for month, file_count, size in months
        ]
    )


def test_segment_lengths_always_sum_to_the_width():
    for amounts in ([1, 1, 1, 1], [100, 1, 200, 3], [0, 0, 5, 0], [1, 0, 0, 0]):
        lengths = segment_lengths(amounts, 34)
        assert sum(lengths) == 34
        assert all(length >= 0 for length in lengths)


def test_segment_lengths_of_an_empty_total_are_zero():
    assert segment_lengths([0, 0, 0, 0], 34) == [0, 0, 0, 0]


def test_capacity_bar_fills_its_width():
    space = StorageSpace(total=100 * GIGABYTE, used=80 * GIGABYTE, free=20 * GIGABYTE)
    bar = capacity_bar(space, source_size=50 * GIGABYTE, freed=30 * GIGABYTE, width=34)
    assert len(bar.plain) == 34
    assert set(bar.plain) == {"█", "░"}  # used in full blocks, the rest in light shades
    assert len(capacity_bar(space, source_size=0, freed=0, width=20).plain) == 20
    unknown = capacity_bar(
        StorageSpace(total=64 * GIGABYTE), source_size=30 * GIGABYTE, freed=20 * GIGABYTE, width=20
    )
    assert "█" in unknown.plain and "░" in unknown.plain


def test_histogram_marks_the_suggested_months():
    histogram = make_histogram(
        [
            ("2025-01", 10, GIGABYTE),
            ("2025-02", 20, 2 * GIGABYTE),
            ("2025-03", 30, 3 * GIGABYTE),
            ("2025-04", 40, 4 * GIGABYTE),
            ("2025-05", 50, 5 * GIGABYTE),
        ]
    )
    output = render(
        lambda console: render_histogram(
            console,
            histogram,
            with_destination=True,
            suggested_range=("2025-02", "2025-03"),
        )
    )
    marked = [line for line in output.splitlines() if line.rstrip().endswith("▸")]
    assert [line.split()[0] for line in marked] == ["2025-02", "2025-03"]
    assert "▸ = months of the suggested range 2025-02 .. 2025-03" in output
    assert "2025-01" in output and "2025-04" in output


def test_histogram_draws_demarcation_lines_around_the_range():
    histogram = make_histogram(
        [("2025-01", 10, GIGABYTE), ("2025-02", 20, 2 * GIGABYTE), ("2025-03", 30, 3 * GIGABYTE)]
    )
    with_range = render(
        lambda console: render_histogram(
            console, histogram, with_destination=False, suggested_range=("2025-02", "2025-02")
        )
    )
    without_range = render(
        lambda console: render_histogram(
            console, histogram, with_destination=False, suggested_range=None
        )
    )
    rules = [
        line for line in with_range.splitlines() if line.strip() and set(line.strip()) == {"─"}
    ]
    plain_rules = [
        line for line in without_range.splitlines() if line.strip() and set(line.strip()) == {"─"}
    ]
    # Two extra rules: one before the suggested month, one after it.
    assert len(rules) == len(plain_rules) + 2


def test_histogram_lists_no_month_that_does_not_exist_yet():
    """A month with no file has nothing to say: no phantom row."""
    histogram = make_histogram([("2025-08", 10, GIGABYTE), ("2025-09", 20, 2 * GIGABYTE)])
    output = render(
        lambda console: render_histogram(
            console, histogram, with_destination=True, suggested_range=("2025-08", "2025-09")
        )
    )
    assert "2025-10" not in output
    assert "2025-11" not in output


def test_the_legend_mentions_the_other_months_only_when_there_are_some():
    histogram = make_histogram(
        [("2025-08", 10, GIGABYTE), ("2025-09", 20, 2 * GIGABYTE), ("2025-10", 5, GIGABYTE)]
    )
    inside = render(
        lambda console: render_histogram(
            console,
            histogram,
            with_destination=True,
            suggested_range=("2025-08", "2025-09"),
            has_months_outside_the_range=True,
        )
    )
    assert "the other months stay on the phone" in inside
    everything = render(
        lambda console: render_histogram(
            console,
            histogram,
            with_destination=True,
            suggested_range=("2025-08", "2025-10"),
            has_months_outside_the_range=False,
        )
    )
    assert "the other months stay on the phone" not in everything
    assert "months of the suggested range 2025-08 .. 2025-10" in everything


def test_histogram_shows_copied_as_a_share_not_as_a_check_mark():
    """A check mark in a column about copying reads as "all of it is to be copied"."""
    histogram = Histogram(
        months=[
            MonthStat(
                month="2025-01",
                file_count=10,
                total_size=GIGABYTE,
                present_count=10,
                present_size=GIGABYTE,
            ),
            MonthStat(
                month="2025-02",
                file_count=10,
                total_size=GIGABYTE,
                present_count=4,
                present_size=400_000_000,
            ),
            MonthStat(
                month="2025-03",
                file_count=10,
                total_size=GIGABYTE,
                present_count=0,
                present_size=0,
            ),
        ]
    )
    output = render(lambda console: render_histogram(console, histogram, with_destination=True))
    assert "Copied" in output
    assert "100%" in output  # January
    assert " 40%" in output  # February
    assert "  0%" in output  # March
    assert "✓" not in output


def test_space_report_states_the_totals_and_the_after_state():
    space = StorageSpace(total=64 * GIGABYTE, used=60 * GIGABYTE, free=4 * GIGABYTE)
    output = render(
        lambda console: render_space(
            console,
            space=space,
            source_label="Camera",
            source_size=50 * GIGABYTE,
            source_files=1000,
            freed=40 * GIGABYTE,
            freed_files=800,
            suggested_range=("2025-01", "2026-08"),
            to_transfer=(200, 2 * GIGABYTE),
        )
    )
    assert "total 64.00 GB" in output
    assert "used 60.00 GB (94%)" in output
    assert "free 4.00 GB" in output
    assert "Camera (1000 files)  50.00 GB — 83% of what is used" in output
    assert "200 files (2.00 GB) left to transfer" in output
    assert "40.00 GB — 800 files, months 2025-01 .. 2026-08" in output
    assert "free 44.00 GB" in output and "was 4.00 GB" in output
    assert "█ to free 40.00 GB" in output
    assert "60.00 GB / 64.00 GB used" in output


def test_space_report_says_when_everything_is_copied():
    space = StorageSpace(total=64 * GIGABYTE, used=60 * GIGABYTE, free=4 * GIGABYTE)
    output = render(
        lambda console: render_space(
            console,
            space=space,
            source_label="Camera",
            source_size=50 * GIGABYTE,
            source_files=1000,
            freed=40 * GIGABYTE,
            freed_files=800,
            suggested_range=("2025-01", "2026-08"),
            to_transfer=(0, 0),
        )
    )
    assert "everything is already copied" in output


def test_space_report_without_free_space_from_the_device():
    """`--phone-capacity` alone: the capacity is known, the free space is not."""
    space = StorageSpace(total=128 * GIGABYTE, used=None, free=None)
    output = render(
        lambda console: render_space(
            console,
            space=space,
            source_label="Camera",
            source_size=30 * GIGABYTE,
            source_files=1000,
            freed=20 * GIGABYTE,
            freed_files=800,
            suggested_range=("2025-01", "2026-08"),
            to_transfer=None,
        )
    )
    assert "capacity 128.00 GB" in output
    assert "23% of the capacity" in output
    assert "+20.00 GB freed" in output
    assert "░ rest of the capacity" in output


def test_space_report_without_any_capacity():
    output = render(
        lambda console: render_space(
            console,
            space=None,
            source_label="Camera",
            source_size=30 * GIGABYTE,
            source_files=1000,
            freed=0,
            freed_files=0,
            suggested_range=None,
        )
    )
    assert "Camera (1000 files)  30.00 GB" in output
    assert "--phone-capacity" in output
