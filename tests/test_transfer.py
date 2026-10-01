"""Tests for the copy phase: verification, conflicts and failure handling."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest

from dcim.dates import CaptureDate
from dcim.errors import TransferError
from dcim.progress import SilentProgressReporter
from dcim.selection import MediaFile
from dcim.transfer import (
    SourceDigests,
    TransferOutcome,
    check_destination_is_separate,
    plan_destination_path,
    transfer_all,
    transfer_one,
)
from dcim.transport import LocalTransport
from dcim.verify import Digest, VerifyMode

PARTIAL_BLOCK_SIZE = 8


@dataclass
class FakeDevice:
    """A device that is a local directory, seen through a local transport."""

    root: Path
    transport: LocalTransport = field(init=False)

    def __post_init__(self) -> None:
        self.transport = LocalTransport(self.root)

    def add(self, name: str, content: bytes, relative: str | None = None) -> MediaFile:
        """Create a file on the device and return the candidate pointing at it."""
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_bytes(content)
        return MediaFile(
            device_path=Path(name),
            relative_path=Path(relative or name),
            size=len(content),
            modified_at=datetime(2025, 9, 15, 10, 11, 12).timestamp(),
            capture_date=CaptureDate(value=datetime(2025, 9, 15, 10, 11, 12), source="filename"),
        )

    def digests(self, media_files, mode: VerifyMode = "partial") -> SourceDigests:
        return SourceDigests(self.transport, media_files, mode=mode, block_size=PARTIAL_BLOCK_SIZE)

    def read(self, media_file: MediaFile) -> bytes:
        return (self.root / media_file.device_path).read_bytes()


def write_destination(path: Path, content: bytes) -> Path:
    """Create a file that already sits on the destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture
def device(tmp_path: Path) -> FakeDevice:
    return FakeDevice(tmp_path / "phone")


def transfer(
    device: FakeDevice, media_file: MediaFile, destination: Path, **overrides
) -> TransferOutcome:
    options = {
        "transport": device.transport,
        "digests": device.digests([media_file]),
        "verify_mode": "partial",
        "conflict_policy": "skip",
        "preserve_times": True,
        "progress": SilentProgressReporter(),
        "partial_block_size": PARTIAL_BLOCK_SIZE,
    }
    options.update(overrides)
    return transfer_one(media_file, destination, **options)


def test_copy_verifies_and_keeps_the_content(device: FakeDevice, tmp_path: Path):
    media_file = device.add("a.jpg", b"content" * 10)
    destination = tmp_path / "nas" / "a.jpg"
    outcome = transfer(device, media_file, destination)
    assert outcome.status == "copied"
    assert outcome.verified
    assert destination.read_bytes() == device.read(media_file)


def test_copy_preserves_the_modification_time(device: FakeDevice, tmp_path: Path):
    media_file = device.add("a.jpg", b"content")
    destination = tmp_path / "nas" / "a.jpg"
    transfer(device, media_file, destination)
    assert destination.stat().st_mtime == pytest.approx(media_file.modified_at, abs=1)


def test_copy_without_preserving_times(device: FakeDevice, tmp_path: Path):
    media_file = device.add("a.jpg", b"content")
    destination = tmp_path / "nas" / "a.jpg"
    transfer(device, media_file, destination, preserve_times=False)
    assert destination.stat().st_mtime != pytest.approx(media_file.modified_at, abs=1)


def test_no_temporary_file_is_left_behind(device: FakeDevice, tmp_path: Path):
    media_file = device.add("a.jpg", b"content" * 10)
    destination = tmp_path / "nas" / "a.jpg"
    transfer(device, media_file, destination)
    assert list(destination.parent.iterdir()) == [destination]


def test_a_corrupted_copy_is_removed_and_reported(device: FakeDevice, tmp_path: Path, monkeypatch):
    media_file = device.add("a.jpg", b"content" * 10)
    destination = tmp_path / "nas" / "a.jpg"

    def corrupt(path: Path, mode: str, block_size: int = 0):

        return Digest(size=media_file.size, value="0" * 64, coverage="partial")

    monkeypatch.setattr("dcim.transfer.digest_file", corrupt)
    with pytest.raises(TransferError, match="does not match the original"):
        transfer(device, media_file, destination)
    assert not destination.exists()
    assert not list(destination.parent.glob(".*part*"))


def test_transfer_all_reports_the_failure_and_continues(device: FakeDevice, tmp_path: Path):
    broken = device.add("broken.jpg", b"a" * 20)
    healthy = device.add("healthy.jpg", b"b" * 20)
    (device.root / broken.device_path).chmod(0o000)
    try:
        outcomes = transfer_all(
            [broken, healthy],
            transport=device.transport,
            destination_root=tmp_path / "nas",
            verify_mode="partial",
            conflict_policy="skip",
            preserve_tree=False,
            preserve_times=True,
            progress=SilentProgressReporter(),
            partial_block_size=PARTIAL_BLOCK_SIZE,
        )
    finally:
        (device.root / broken.device_path).chmod(0o600)
    assert {outcome.filename: outcome.status for outcome in outcomes} == {
        "broken.jpg": "failed",
        "healthy.jpg": "copied",
    }


def test_an_identical_file_is_recognized_as_already_present(device: FakeDevice, tmp_path: Path):
    media_file = device.add("a.jpg", b"content" * 10)
    destination = tmp_path / "nas" / "a.jpg"
    transfer(device, media_file, destination)
    second = transfer(device, media_file, destination)
    assert second.status == "already_present"
    assert second.verified


def test_a_different_file_with_the_same_size_is_a_conflict(device: FakeDevice, tmp_path: Path):
    media_file = device.add("a.jpg", b"content" * 10)
    destination = write_destination(tmp_path / "nas" / "a.jpg", b"x" * media_file.size)
    outcome = transfer(device, media_file, destination)
    assert outcome.status == "conflict"
    assert not outcome.verified
    assert destination.read_bytes() == b"x" * media_file.size


def test_a_different_size_is_a_conflict(device: FakeDevice, tmp_path: Path):
    media_file = device.add("a.jpg", b"content" * 10)
    destination = write_destination(tmp_path / "nas" / "a.jpg", b"short")
    outcome = transfer(device, media_file, destination)
    assert outcome.status == "conflict"
    assert "different size" in outcome.detail


def test_the_rename_policy_moves_the_existing_file_aside(device: FakeDevice, tmp_path: Path):
    media_file = device.add("a.jpg", b"content" * 10)
    destination = write_destination(tmp_path / "nas" / "a.jpg", b"short")
    outcome = transfer(device, media_file, destination, conflict_policy="rename")
    assert outcome.status == "copied"
    assert outcome.destination_path == tmp_path / "nas" / "a (1).jpg"


def test_the_overwrite_policy_replaces_the_existing_file(device: FakeDevice, tmp_path: Path):
    media_file = device.add("a.jpg", b"content" * 10)
    destination = write_destination(tmp_path / "nas" / "a.jpg", b"short")
    outcome = transfer(device, media_file, destination, conflict_policy="overwrite")
    assert outcome.status == "copied"
    assert destination.read_bytes() == device.read(media_file)


def test_size_only_verification_skips_the_read_back(device: FakeDevice, tmp_path: Path):
    media_file = device.add("a.jpg", b"content")
    destination = tmp_path / "nas" / "a.jpg"
    outcome = transfer(device, media_file, destination, verify_mode="none")
    assert outcome.status == "copied"
    assert outcome.detail == "size only"


def test_full_verification_compares_whole_files(device: FakeDevice, tmp_path: Path):
    media_file = device.add("a.jpg", b"content" * 10)
    destination = tmp_path / "nas" / "a.jpg"
    outcome = transfer(
        device,
        media_file,
        destination,
        verify_mode="full",
        digests=device.digests([media_file], mode="full"),
    )
    assert outcome.status == "copied"
    assert outcome.verified
    assert "full coverage" in outcome.detail


def test_plan_destination_path_is_flat_by_default(device: FakeDevice, tmp_path: Path):
    media_file = device.add("a.jpg", b"x", relative="sub/dir/a.jpg")
    assert (
        plan_destination_path(tmp_path / "nas", media_file, preserve_tree=False)
        == tmp_path / "nas" / "a.jpg"
    )


def test_plan_destination_path_can_preserve_the_tree(device: FakeDevice, tmp_path: Path):
    media_file = device.add("a.jpg", b"x", relative="sub/dir/a.jpg")
    assert (
        plan_destination_path(tmp_path / "nas", media_file, preserve_tree=True)
        == tmp_path / "nas" / "sub" / "dir" / "a.jpg"
    )


def test_a_destination_inside_the_device_is_refused(tmp_path: Path):
    with pytest.raises(TransferError, match="overlaps the device"):
        check_destination_is_separate(tmp_path / "phone" / "backup", tmp_path / "phone")


def test_a_device_inside_the_destination_is_refused(tmp_path: Path):
    with pytest.raises(TransferError, match="overlaps the device"):
        check_destination_is_separate(tmp_path, tmp_path / "phone")


def test_separate_paths_are_accepted(tmp_path: Path):
    check_destination_is_separate(tmp_path / "nas", tmp_path / "phone")


class RecordingProgress(SilentProgressReporter):
    """A reporter that remembers the phases it was told about."""

    def __init__(self) -> None:
        self.phases: list[tuple[str, int, int, str]] = []

    def start_phase(
        self,
        total_files: int,
        total_bytes: int,
        *,
        label: str = "Overall",
        unit: str = "bytes",
    ) -> None:
        self.phases.append((label, total_files, total_bytes, unit))


class CountingTransport(LocalTransport):
    """A transport that counts how many files it was asked to copy."""

    def __init__(self, root):
        super().__init__(root)
        self.copied: list[str] = []

    def copy_to(self, path, size, destination, on_progress, *, mode, block_size):
        self.copied.append(path)
        return super().copy_to(
            path, size, destination, on_progress, mode=mode, block_size=block_size
        )


def run_all(device, media_files, destination, progress=None, **overrides):
    options = {
        "transport": device.transport,
        "destination_root": destination,
        "verify_mode": "partial",
        "conflict_policy": "skip",
        "preserve_tree": False,
        "preserve_times": True,
        "progress": progress or SilentProgressReporter(),
        "partial_block_size": PARTIAL_BLOCK_SIZE,
    }
    options.update(overrides)
    return transfer_all(media_files, **options)


def test_a_present_file_is_not_copied_again(device: FakeDevice, tmp_path: Path):
    """Incremental: the second run transfers nothing, and verifies instead."""
    media_files = [device.add(f"a{index}.jpg", b"content" * 10) for index in range(3)]
    destination = tmp_path / "nas"
    first = run_all(device, media_files, destination)
    assert [outcome.status for outcome in first] == ["copied"] * 3

    device.transport = CountingTransport(device.root)
    second = run_all(device, media_files, destination)
    assert [outcome.status for outcome in second] == ["already_present"] * 3
    assert all(outcome.verified for outcome in second)
    assert device.transport.copied == []  # not one byte was transferred


def test_the_copy_phase_only_covers_what_is_missing(device: FakeDevice, tmp_path: Path):
    """The bar must be a bar of what is really transferred."""
    media_files = [device.add(f"a{index}.jpg", b"content" * 10) for index in range(4)]
    destination = tmp_path / "nas"
    run_all(device, media_files, destination)  # everything lands
    progress = RecordingProgress()
    outcomes = run_all(device, media_files, destination, progress)
    assert [phase[0] for phase in progress.phases] == ["Verifying"]
    assert progress.phases[0][1] == 4  # four files verified
    assert progress.phases[0][3] == "files"  # a bar of files, not of bytes
    assert all(outcome.verified for outcome in outcomes)


def test_the_copy_phase_counts_only_the_missing_files(device: FakeDevice, tmp_path: Path):
    present = device.add("present.jpg", b"content" * 10)
    destination = tmp_path / "nas"
    run_all(device, [present], destination)
    missing = [device.add("m0.jpg", b"x" * (1_000_000)), device.add("m1.jpg", b"y" * (2_000_000))]
    progress = RecordingProgress()
    outcomes = run_all(device, [present, *missing], destination, progress)
    phases = {phase[0]: phase for phase in progress.phases}
    assert phases["Verifying"][1] == 1
    assert phases["Copying"][1] == 2
    assert phases["Copying"][2] == 3_000_000  # only the two files being copied
    assert [outcome.status for outcome in outcomes] == ["already_present", "copied", "copied"]


def test_a_modified_copy_is_a_conflict_not_a_copy(device: FakeDevice, tmp_path: Path):
    """Same name, same size, different content: verified, found wrong, kept."""
    media_file = device.add("a.jpg", b"content" * 10)
    destination = write_destination(tmp_path / "nas" / "a.jpg", b"x" * media_file.size)
    outcomes = run_all(device, [media_file], tmp_path / "nas")
    assert outcomes[0].status == "conflict"
    assert not outcomes[0].verified
    assert destination.read_bytes() == b"x" * media_file.size
    assert (device.root / media_file.device_path).exists()
