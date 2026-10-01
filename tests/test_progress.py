"""Tests for the progress display: the bar must read without colours."""

from io import StringIO

from rich.console import Console

from dcim.formatting import render_bar
from dcim.progress import (
    LAYOUTS,
    PhaseProgress,
    RichProgressReporter,
    choose_layout,
    compose_current_file,
    compose_summary,
)

WIDE = choose_layout(200)

#: A progress state close to the worst case for the line width.
WORST_CASE = {
    "total_files": 99999,
    "total_bytes": 999_990_000_000,
    "done_bytes": 999_985_000_000,
    "done_files": 99998,
    "current_label": "PXL_20250928_123511581.TS.mp4",
    "current_size": 999_990_000_000,
    "current_bytes": 999_985_000_000,
}

MEGABYTE = 1000**2


def make_progress(**overrides) -> PhaseProgress:
    values = {
        "label": "Copying",
        "total_files": 10,
        "total_bytes": 100 * MEGABYTE,
        "done_bytes": 0,
        "done_files": 0,
        "started_at": 0.0,
    }
    values.update(overrides)
    return PhaseProgress(**values)


def test_render_bar_uses_two_glyphs():
    assert render_bar(0, 100, 10) == "░" * 10
    assert render_bar(50, 100, 10) == "█" * 5 + "░" * 5
    assert render_bar(100, 100, 10) == "█" * 10
    assert render_bar(1, 1_000_000, 10) == "░" * 10  # too small to show
    assert render_bar(5, 0, 4) == "░" * 4  # unknown total: an empty track


def test_the_summary_bar_is_empty_at_the_start():
    """The bug the two-glyph bar fixes: rich drew a full line at 0 %."""
    line = compose_summary(make_progress(), WIDE)
    assert "░" * WIDE.bar_width in line
    assert "█" not in line
    assert "0%" in line
    assert "0 B / 100.00 MB" in line


def test_the_summary_bar_grows_with_the_volume_done():
    line = compose_summary(make_progress(done_bytes=50 * MEGABYTE, done_files=5), WIDE)
    assert f"{'█' * 12}{'░' * 12}" in line
    assert "50%" in line
    assert "5/10 files" in line


def test_the_summary_hides_the_speed_until_it_means_something():
    fresh = make_progress(done_bytes=1024, done_files=1)
    fresh.started_at = __import__("time").monotonic()  # no time elapsed yet
    assert "left" not in compose_summary(fresh, WIDE)
    established = make_progress(done_bytes=50 * MEGABYTE, done_files=5)
    established.started_at = __import__("time").monotonic() - 10
    line = compose_summary(established, WIDE)
    assert "MB/s" in line
    assert "left" in line


def test_the_current_file_line_shows_its_own_progress():
    progress = make_progress(
        current_label="PXL_20250928_123511581.TS.mp4",
        current_size=36 * MEGABYTE,
        current_bytes=18 * MEGABYTE,
    )
    line = compose_current_file(progress, WIDE)
    assert "PXL_20250928_123511581.TS.mp4" in line
    assert "50%" in line
    assert "18.00 MB / 36.00 MB" in line
    assert compose_current_file(make_progress(), WIDE) == ""


def test_a_long_file_name_is_shortened():
    progress = make_progress(current_label="P" * 80, current_size=10, current_bytes=5)
    line = compose_current_file(progress, WIDE)
    assert "…" in line
    assert len(line) < 120


def test_the_reporter_advances_and_survives_a_failed_file():
    reporter = RichProgressReporter(Console(file=StringIO(), width=120, no_color=True))
    reporter.start_phase(total_files=2, total_bytes=100, label="Copying")
    reporter.start_file("a.jpg", 60)
    reporter.advance(60)
    reporter.complete_file("a.jpg", "copied")
    reporter.start_file("b.jpg", 40)
    reporter.advance(25)
    reporter.complete_file("b.jpg", "failed")
    assert reporter.progress.done_files == 2
    assert reporter.progress.done_bytes == 85  # never above the total
    reporter.stop()


def test_the_silent_reporter_prints_nothing():
    buffer = StringIO()
    from dcim.progress import make_reporter

    reporter = make_reporter(Console(file=buffer), quiet=True)
    reporter.start_phase(total_files=1, total_bytes=10)
    reporter.start_file("a.jpg", 10)
    reporter.advance(10)
    reporter.complete_file("a.jpg", "copied")
    reporter.note("hello")
    reporter.stop()
    assert buffer.getvalue() == ""


def test_the_lines_always_fit_the_console():
    """Rich must not wrap the display: a wrapped line breaks the two-line layout."""
    progress = make_progress(**WORST_CASE)
    thresholds = [minimum for minimum, _bar, _label, _speed in LAYOUTS]
    for width in [max(threshold, 50) for threshold in thresholds] + [48, 200]:
        layout = choose_layout(width)
        assert len(compose_summary(progress, layout)) <= width, width
        assert len(compose_current_file(progress, layout)) <= width, width


def test_a_narrow_console_loses_the_bar_before_the_numbers():
    narrow = choose_layout(50)
    assert narrow.bar_width == 0
    assert not narrow.show_speed
    line = compose_summary(make_progress(), narrow)
    assert "0 B / 100.00 MB" in line
    assert "█" not in line
