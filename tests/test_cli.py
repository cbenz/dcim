"""Integration tests: both commands against a fake phone and a fake destination.

The MTP mount is replaced by a directory, so the whole pipeline — histogram,
suggestion, preview, copy, verification, deletion, manifest — runs for real,
without a phone and without a NAS.
"""

import json
import re
from datetime import date
from pathlib import Path

import pytest

from dcim import cli
from dcim.config import Config


class FakePhone:
    """Stands in for ``MtpMount``: the 'phone' is a directory of this machine."""

    def __init__(self, root: Path) -> None:
        self.mountpoint = root
        self.mounted = False

    def mount(self) -> None:
        self.mounted = True

    def unmount(self) -> None:
        self.mounted = False


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
def phone(tmp_path: Path) -> Path:
    """A fake phone holding five dated media files and two distractors."""
    mountpoint = tmp_path / "phone-mount"
    camera = mountpoint / "Internal storage" / "DCIM" / "Camera"
    camera.mkdir(parents=True)
    (camera / "PXL_20250115_101112.jpg").write_bytes(b"january" * 100)
    (camera / "PXL_20250915_101112.jpg").write_bytes(b"september" * 100)
    (camera / "PXL_20260720_120000.mp4").write_bytes(b"july" * 200)
    (camera / "PXL_20260901_090000.jpg").write_bytes(b"current" * 50)
    (camera / "notes.txt").write_text("not media")
    (camera / ".stversions").mkdir()
    (camera / ".stversions" / "PXL_20250115_101112.jpg").write_bytes(b"stale")
    return mountpoint


@pytest.fixture
def fake_mount(monkeypatch, phone: Path) -> FakePhone:
    """Patch the device layer and the configuration file of the CLI."""
    mount = FakePhone(phone)
    monkeypatch.setattr("dcim.session.MtpMount", lambda **_kwargs: mount)
    monkeypatch.setattr("dcim.session.adb_serial", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("DCIM_CONFIG", str(phone.parent / "config.toml"))
    return mount


def run_cli(destination: Path, *arguments: str) -> int:
    """Run the CLI without a terminal: every prompt falls back on its default.

    The source directory and the glob pattern therefore come from the built-in
    defaults (DCIM/Camera and *), exactly like a first run with no configuration
    file, and no test has to pass them explicitly.
    """
    return cli.main(
        [
            *arguments,
            "--dest",
            str(destination),
            "--no-release-gvfs",
            "--quiet",
        ]
    )


def run_sync(destination: Path, *extra: str) -> int:
    return run_cli(
        destination, "sync", "--from", "2025-01", "--to", "2026-08", "--no-mount-nas", *extra
    )


def flatten(output: str) -> str:
    """Collapse the line wrapping of the tables so that assertions can match."""
    return " ".join(output.split())


def write_nested_destination(tmp_path: Path, name: str) -> Path:
    """A destination whose copy of one phone file lives in a sub-directory."""
    destination = tmp_path / "nas" / "Camera"
    nested = destination / "2025-01"
    nested.mkdir(parents=True)
    (nested / name).write_bytes((phone_camera(tmp_path / "phone-mount") / name).read_bytes())
    return destination


def phone_camera(phone: Path) -> Path:
    return phone / "Internal storage" / "DCIM" / "Camera"


def phone_media(phone: Path) -> list[str]:
    """Names of the media files still on the fake phone."""
    return sorted(path.name for path in phone_camera(phone).glob("PXL_*"))


def test_status_prints_a_histogram_and_a_suggestion(phone, fake_mount, tmp_path, capsys):
    assert run_cli(tmp_path / "nas", "status") == 0
    output = capsys.readouterr().out
    assert "2025-01" in output
    assert "2026-07" in output
    assert "2026-09" in output  # the current month is shown, but not suggested
    assert "Suggestion" in output
    assert "--from 2025-01 --to 2026-07" in output  # 2026-08 holds no file
    assert "dcim sync" in output
    # Read-only: the destination directory is not even created.
    assert not (tmp_path / "nas").exists()


def test_status_does_not_touch_the_phone(phone, fake_mount, tmp_path):
    assert run_cli(tmp_path / "nas", "status") == 0
    assert len(phone_media(phone)) == 4


def test_status_reports_what_is_already_at_the_destination(phone, fake_mount, tmp_path, capsys):
    destination = tmp_path / "nas"
    assert run_sync(destination) == 0
    assert run_cli(destination, "status") == 0
    output = flatten(capsys.readouterr().out)
    # The three files of the suggested range were just copied: nothing is left.
    assert "every file of this range is already at the destination" in output
    assert "At destination" in output


def test_status_without_the_destination_says_nothing_about_it(phone, fake_mount, tmp_path, capsys):
    assert run_cli(tmp_path / "nas", "status", "--no-check-destination") == 0
    output = capsys.readouterr().out
    assert "not at the destination yet" not in output
    assert "At destination" not in output


def test_status_keep_months_moves_the_end_of_the_suggestion(phone, fake_mount, tmp_path, capsys):
    assert run_cli(tmp_path / "nas", "status", "--keep-months", "1") == 0
    output = capsys.readouterr().out
    assert "--from 2025-01 --to 2025-09" in output


def test_status_target_size_stops_the_range_early(phone, fake_mount, tmp_path, capsys):
    assert run_cli(tmp_path / "nas", "status", "--target-size", "500") == 0
    output = capsys.readouterr().out
    assert "--from 2025-01 --to 2025-01" in output


def test_status_json_is_machine_readable(phone, fake_mount, tmp_path, capsys):
    assert run_cli(tmp_path / "nas", "status", "--json") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["file_count"] == 4
    assert [month["month"] for month in report["months"]] == [
        "2025-01",
        "2025-09",
        "2026-07",
        "2026-09",
    ]
    assert report["suggestion"]["start_month"] == "2025-01"
    assert report["suggestion"]["end_month"] == "2026-07"


def test_sync_asks_nothing_when_the_configuration_file_answers(phone, fake_mount, tmp_path, capsys):
    """Everything the tool needs is already decided: flags, config file, defaults."""
    assert run_cli(tmp_path / "nas", "sync", "--no-mount-nas", "--dry-run") == 0
    output = flatten(capsys.readouterr().out)
    assert "Glob pattern" not in output
    assert "Source directory on the phone" not in output
    assert "First month or day to include" not in output
    # ... and the effective range is printed instead.
    assert "date range" in output


def test_sync_can_be_asked_to_prompt(phone, fake_mount, tmp_path, capsys):
    assert run_cli(tmp_path / "nas", "sync", "--no-mount-nas", "--dry-run", "--prompt") == 0
    output = flatten(capsys.readouterr().out)
    assert "Glob pattern" in output
    assert "Source directory on the phone" in output
    assert "First month or day to include" in output


def test_the_date_range_defaults_to_the_last_complete_months(phone, fake_mount, tmp_path, capsys):
    """The default window starts a year before the current month, current excluded."""
    assert run_cli(tmp_path / "nas", "sync", "--no-mount-nas", "--dry-run") == 0
    output = flatten(capsys.readouterr().out)
    today = date.today()
    first_month = date(today.year - 1, today.month, 1)
    assert f"date range {first_month:%Y-%m-01}" in output


def test_status_never_mounts_the_destination(phone, fake_mount, tmp_path, monkeypatch):
    """Reading the phone is the only thing `status` may depend on."""
    monkeypatch.setattr(
        "dcim.session.ensure_mounted",
        lambda *_args, **_kwargs: pytest.fail("status must not mount the destination"),
    )
    assert run_cli(tmp_path / "nas", "status") == 0


def test_status_survives_a_destination_that_is_not_there(phone, fake_mount, tmp_path, capsys):
    assert run_cli(tmp_path / "absent", "status") == 0
    output = flatten(capsys.readouterr().out)
    assert "does not exist" in output
    assert "Suggestion" in output  # the suggestion is still printed


def test_status_survives_an_unmounted_share(phone, fake_mount, tmp_path, capsys):
    """An empty mount point must not be read as "nothing is copied"."""
    share = tmp_path / "share"
    destination = share / "Photos" / "Camera"
    destination.mkdir(parents=True)
    assert (
        run_cli(
            destination,
            "status",
            "--nas-mountpoint",
            str(share),
            "--check-destination",
        )
        == 0
    )
    output = flatten(capsys.readouterr().out)
    assert "is not mounted" in output
    assert "At destination" not in output


def test_status_can_be_told_not_to_check_the_destination(phone, fake_mount, tmp_path, capsys):
    assert run_cli(tmp_path / "nas", "status", "--no-check-destination") == 0
    output = flatten(capsys.readouterr().out)
    assert "--no-check-destination" in output


def test_status_asks_nothing_and_shows_the_effective_filter(phone, fake_mount, tmp_path, capsys):
    """A read-only command has no reason to interrogate the user."""
    assert run_cli(tmp_path / "nas", "status") == 0
    output = flatten(capsys.readouterr().out)
    assert "Glob pattern" not in output
    assert "Source directory on the phone" not in output
    assert "Filter: * in DCIM/Camera" in output
    assert "photos and videos only" in output


def test_status_can_still_ask_with_prompt(phone, fake_mount, tmp_path, capsys):
    assert run_cli(tmp_path / "nas", "status", "--prompt") == 0
    output = flatten(capsys.readouterr().out)
    assert "Glob pattern" in output
    assert "Source directory on the phone" in output


def test_status_only_looks_at_the_destination_root_by_default(phone, fake_mount, tmp_path, capsys):
    destination = write_nested_destination(tmp_path, "PXL_20250115_101112.jpg")
    assert run_cli(destination, "status") == 0
    output = flatten(capsys.readouterr().out)
    assert "4 files" in output  # nothing matched: all four are reported as missing
    assert "Nothing matched at the destination root" in output


def test_status_can_look_deeper_into_the_destination(phone, fake_mount, tmp_path, capsys):
    destination = write_nested_destination(tmp_path, "PXL_20250115_101112.jpg")
    assert run_cli(destination, "status", "--destination-depth", "2") == 0
    output = flatten(capsys.readouterr().out)
    assert "3 files" in output  # the nested copy is recognized
    assert "Nothing matched at the destination root" not in output


def test_status_destination_depth_all_is_accepted(phone, fake_mount, tmp_path):
    destination = write_nested_destination(tmp_path, "PXL_20250115_101112.jpg")
    assert run_cli(destination, "status", "--destination-depth", "all") == 0


def test_status_is_available_through_its_only_alias(phone, fake_mount, tmp_path, capsys):
    assert run_cli(tmp_path / "nas", "st") == 0
    capsys.readouterr()
    # Only `st`: the aliases no one asked for are gone.
    for gone in ("stats", "analysis", "analyze"):
        with pytest.raises(SystemExit) as exit_status:
            cli.main([gone])
        assert exit_status.value.code == 2


def test_a_bare_invocation_prints_the_help(capsys):
    assert cli.main([]) == 2
    assert "usage: dcim" in capsys.readouterr().out


def test_sync_dry_run_copies_nothing(phone, fake_mount, tmp_path, capsys):
    destination = tmp_path / "nas"
    assert run_sync(destination, "--dry-run") == 0
    assert not destination.exists()
    assert len(phone_media(phone)) == 4
    assert "Dry run" in capsys.readouterr().out


def test_sync_alone_keeps_the_originals(phone, fake_mount, tmp_path):
    destination = tmp_path / "nas"
    assert run_sync(destination, "--yes") == 0
    copied = sorted(path.name for path in destination.iterdir() if path.is_file())
    assert copied == [
        "PXL_20250115_101112.jpg",
        "PXL_20250915_101112.jpg",
        "PXL_20260720_120000.mp4",
    ]
    assert len(phone_media(phone)) == 4
    assert (destination / "PXL_20250115_101112.jpg").exists()


def test_sync_delete_removes_the_verified_originals(phone, fake_mount, tmp_path):
    destination = tmp_path / "nas"
    assert run_sync(destination, "--yes", "--delete") == 0
    assert phone_media(phone) == ["PXL_20260901_090000.jpg"]


def test_sync_delete_needs_a_terminal_or_yes(phone, fake_mount, tmp_path, capsys):
    """Without --yes and without a terminal, the deletion must not happen."""
    destination = tmp_path / "nas"
    assert run_sync(destination, "--delete") == 0
    assert len(phone_media(phone)) == 4
    assert "not interactive" in capsys.readouterr().out


def test_a_second_run_recognizes_the_files_and_deletes_them(phone, fake_mount, tmp_path):
    destination = tmp_path / "nas"
    assert run_sync(destination, "--yes") == 0
    assert run_sync(destination, "--yes", "--delete") == 0
    assert phone_media(phone) == ["PXL_20260901_090000.jpg"]
    assert (destination / "PXL_20250115_101112.jpg").read_bytes() == b"january" * 100


def test_a_manifest_is_written_next_to_the_copies(phone, fake_mount, tmp_path):
    destination = tmp_path / "nas"
    assert run_sync(destination, "--yes", "--delete") == 0
    manifests = list((destination / ".dcim").glob("manifest-*.jsonl"))
    assert len(manifests) == 1
    assert re.fullmatch(r"manifest-\d{8}T\d{6}Z\.jsonl", manifests[0].name)
    content = manifests[0].read_text()
    assert '"event": "run"' in content
    assert '"event": "deletion"' in content


def test_resume_replays_the_deletion_phase(phone, fake_mount, tmp_path, capsys):
    destination = tmp_path / "nas"
    assert run_sync(destination, "--yes") == 0
    manifest = next((destination / ".dcim").glob("manifest-*.jsonl"))
    assert run_cli(destination, "sync", "--yes", "--resume", str(manifest)) == 0
    assert phone_media(phone) == ["PXL_20260901_090000.jpg"]
    assert "pending deletion" in capsys.readouterr().out


def test_an_empty_selection_is_not_an_error(phone, fake_mount, tmp_path, capsys):
    exit_status = run_cli(
        tmp_path / "nas", "sync", "--from", "2020-01", "--to", "2020-02", "--dry-run"
    )
    assert exit_status == 0
    assert "No file matches" in capsys.readouterr().out


def test_an_unknown_source_directory_fails(phone, fake_mount, tmp_path):
    assert run_cli(tmp_path / "nas", "sync", "--source", "DCIM/Nope") == 1


def test_list_devices(monkeypatch, capsys):
    monkeypatch.setattr(cli, "list_mtp_devices", lambda: ["Google-Pixel7-ABC"])
    assert cli.main(["--list-devices"]) == 0
    assert "Google-Pixel7-ABC" in capsys.readouterr().out


def test_default_config_points_to_the_nas():
    config = Config()
    assert config.destination_root == Path("/mnt/nas-photos/Photos")
    assert config.verify_mode == "partial"
    assert config.keep_months == 0


def test_a_prompt_reaching_end_of_input_uses_its_default(monkeypatch, capsys):
    """Ctrl+D must not loop nor print a traceback."""
    from dcim.ui import ask_confirmation, ask_value, build_console

    monkeypatch.setattr("dcim.ui.is_interactive", lambda _console: True)

    def raise_eof(*_args, **_kwargs):
        raise EOFError

    monkeypatch.setattr("dcim.ui.Confirm.ask", raise_eof)
    monkeypatch.setattr("dcim.ui.Prompt.ask", raise_eof)
    console = build_console()
    assert ask_confirmation(console, "Continue?", default=True) is True
    assert ask_confirmation(console, "Delete?", default=False) is False
    assert ask_value(console, "Directory", "DCIM/Camera") == "DCIM/Camera"
    assert "end of input" in flatten(capsys.readouterr().out)


def test_the_deletion_confirmation_defaults_to_yes(monkeypatch, capsys):
    """Reaching that question already required --delete and verified copies."""
    from dcim.ui import ask_deletion_confirmation, build_console

    monkeypatch.setattr("dcim.ui.is_interactive", lambda _console: True)
    seen: list[bool] = []

    def record(_question, *, default=False, **_kwargs):
        seen.append(default)
        return default

    monkeypatch.setattr("dcim.ui.Confirm.ask", record)
    console = build_console()
    assert ask_deletion_confirmation(console, file_count=3, total_size=1000, assume_yes=False)
    assert seen == [True]
    assert "About to delete 3 originals" in flatten(capsys.readouterr().out)
