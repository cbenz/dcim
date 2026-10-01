"""Tests for the deletion phase, which is the only destructive part."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from dcim.dates import CaptureDate
from dcim.deletion import (
    delete_originals,
    eligible_outcomes,
    outcomes_from_manifest,
    revalidate,
)
from dcim.progress import SilentProgressReporter
from dcim.selection import MediaFile
from dcim.transfer import TransferOutcome
from dcim.transport import LocalTransport
from dcim.verify import digest_file

BLOCK_SIZE = 8


@dataclass
class FakeDevice:
    """A device that is a local directory, seen through a local transport."""

    root: Path

    def __post_init__(self) -> None:
        self.transport: LocalTransport = LocalTransport(self.root)

    def add(self, name: str, content: bytes) -> MediaFile:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_bytes(content)
        return MediaFile(
            device_path=Path(name),
            relative_path=Path(name),
            size=len(content),
            modified_at=datetime(2025, 9, 15).timestamp(),
            capture_date=CaptureDate(value=datetime(2025, 9, 15), source="filename"),
        )

    def exists(self, media_file: MediaFile) -> bool:
        return (self.root / media_file.device_path).exists()

    def write(self, media_file: MediaFile, content: bytes) -> None:
        (self.root / media_file.device_path).write_bytes(content)


def make_outcome(
    device: FakeDevice,
    destination_root: Path,
    content: bytes = b"content" * 4,
    name: str = "PXL_20250915_101112.jpg",
) -> TransferOutcome:
    """Build a verified transfer outcome: a file on the device and its copy."""
    media_file = device.add(name, content)
    destination_path = destination_root / media_file.filename
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_bytes(content)
    return TransferOutcome(
        media_file=media_file,
        destination_path=destination_path,
        status="copied",
        verified=True,
        detail="copied",
        digest=digest_file(destination_path, "partial", BLOCK_SIZE).value,
    )


@pytest.fixture
def device(tmp_path: Path) -> FakeDevice:
    return FakeDevice(tmp_path / "phone")


@pytest.fixture
def nas(tmp_path: Path) -> Path:
    return tmp_path / "nas"


def test_a_verified_outcome_is_eligible(device: FakeDevice, nas: Path):
    assert len(eligible_outcomes([make_outcome(device, nas)])) == 1


def test_an_unverified_outcome_is_not_eligible(device: FakeDevice, nas: Path):
    outcome = make_outcome(device, nas)
    unverified = TransferOutcome(
        media_file=outcome.media_file,
        destination_path=outcome.destination_path,
        status="failed",
        verified=False,
    )
    assert eligible_outcomes([unverified]) == []


def test_revalidate_accepts_an_intact_copy(device: FakeDevice, nas: Path):
    outcome = make_outcome(device, nas)
    assert (
        revalidate(
            outcome,
            source_size=outcome.media_file.size,
            verify_mode="partial",
            partial_block_size=BLOCK_SIZE,
        )
        is None
    )


def test_revalidate_refuses_a_missing_copy(device: FakeDevice, nas: Path):
    outcome = make_outcome(device, nas)
    outcome.destination_path.unlink()
    refusal = revalidate(
        outcome,
        source_size=outcome.media_file.size,
        verify_mode="partial",
        partial_block_size=BLOCK_SIZE,
    )
    assert refusal is not None and "not readable" in refusal


def test_revalidate_refuses_a_modified_copy(device: FakeDevice, nas: Path):
    outcome = make_outcome(device, nas)
    outcome.destination_path.write_bytes(b"y" * outcome.media_file.size)
    refusal = revalidate(
        outcome,
        source_size=outcome.media_file.size,
        verify_mode="partial",
        partial_block_size=BLOCK_SIZE,
    )
    assert refusal is not None and "no longer matches" in refusal


def test_revalidate_refuses_a_filtered_copy(device: FakeDevice, nas: Path):
    outcome = make_outcome(device, nas)
    outcome.destination_path.write_bytes(b"short")
    refusal = revalidate(
        outcome,
        source_size=outcome.media_file.size,
        verify_mode="partial",
        partial_block_size=BLOCK_SIZE,
    )
    assert refusal is not None and "instead of" in refusal


def test_revalidate_refuses_a_changed_original(device: FakeDevice, nas: Path):
    outcome = make_outcome(device, nas)
    refusal = revalidate(
        outcome,
        source_size=outcome.media_file.size + 1,
        verify_mode="partial",
        partial_block_size=BLOCK_SIZE,
    )
    assert refusal is not None and "changed since the scan" in refusal


def test_revalidate_refuses_an_unreadable_original(device: FakeDevice, nas: Path):
    outcome = make_outcome(device, nas)
    refusal = revalidate(
        outcome, source_size=None, verify_mode="partial", partial_block_size=BLOCK_SIZE
    )
    assert refusal is not None and "not readable anymore" in refusal


class RecordingProgress(SilentProgressReporter):
    """A reporter that remembers the phases and the files it was told about."""

    def __init__(self) -> None:
        self.phases: list[tuple[str, int, str]] = []
        self.files: list[tuple[str, str]] = []

    def start_phase(
        self,
        total_files: int,
        total_bytes: int,
        *,
        label: str = "Overall",
        unit: str = "bytes",
    ) -> None:
        self.phases.append((label, total_files, unit))

    def start_file(self, label: str, size: int) -> None:
        self.files.append(("start", label))

    def complete_file(self, label: str, status: str) -> None:
        self.files.append((status, label))


def delete(device: FakeDevice, outcomes, **overrides):
    options = {
        "transport": device.transport,
        "verify_mode": "partial",
        "progress": SilentProgressReporter(),
        "partial_block_size": BLOCK_SIZE,
    }
    options.update(overrides)
    return delete_originals(outcomes, **options)


def test_delete_originals_deletes_the_verified_files(device: FakeDevice, nas: Path):
    outcome = make_outcome(device, nas)
    report = delete(device, [outcome])
    assert len(report.deleted) == 1
    assert report.freed_bytes == outcome.media_file.size
    assert not device.exists(outcome.media_file)
    assert outcome.destination_path.exists()


def test_delete_originals_keeps_a_file_whose_copy_is_gone(device: FakeDevice, nas: Path):
    outcome = make_outcome(device, nas)
    outcome.destination_path.unlink()
    report = delete(device, [outcome])
    assert len(report.deleted) == 0
    assert len(report.skipped) == 1
    assert device.exists(outcome.media_file)


def test_delete_originals_records_a_survivor_as_a_failure(device: FakeDevice, nas: Path):
    """A transport that claims success while the file is still there must be caught."""
    outcome = make_outcome(device, nas)

    class DeafTransport(LocalTransport):
        def delete(self, paths, on_progress) -> None:
            del paths, on_progress  # pretends to delete

    device.transport = DeafTransport(device.root)
    report = delete(device, [outcome])
    assert len(report.failed) == 1
    assert "still on the device" in report.failed[0][1]
    assert device.exists(outcome.media_file)


def test_delete_originals_records_every_deletion(device: FakeDevice, nas: Path):
    from dcim.manifest import ManifestWriter, read_manifest

    outcome = make_outcome(device, nas)
    manifest_path = nas / "manifest.jsonl"
    with ManifestWriter(manifest_path) as manifest:
        delete(device, [outcome], manifest=manifest)
    assert [record["status"] for record in read_manifest(manifest_path)] == ["deleted"]


def test_delete_originals_deletes_in_batches(device: FakeDevice, nas: Path):
    """Deleting many files must not be one transport call per file."""
    outcomes = [
        make_outcome(device, nas, b"content" * 4, name=f"PXL_20250915_10111{index}.jpg")
        for index in range(5)
    ]
    calls = []

    class CountingTransport(LocalTransport):
        def delete(self, paths, on_progress) -> None:
            calls.append(len(paths))
            super().delete(paths, on_progress)

    device.transport = CountingTransport(device.root)
    report = delete(device, outcomes)
    assert len(report.deleted) == 5
    assert calls == [5]


def test_outcomes_from_manifest_rebuilds_the_device_paths():
    entries = [
        {
            "relative_path": "PXL_20250915_101112.jpg",
            "destination": "/nas/PXL_20250915_101112.jpg",
            "size": 32,
            "modified_at": 1757929200,
            "digest": "abc",
        },
        {"destination": "", "relative_path": "broken.jpg"},
    ]
    outcomes = outcomes_from_manifest(entries, "DCIM/Camera")
    assert len(outcomes) == 1
    assert outcomes[0].media_file.device_path == Path("DCIM/Camera/PXL_20250915_101112.jpg")
    assert outcomes[0].digest == "abc"


def test_deletion_shows_both_phases(device: FakeDevice, nas: Path):
    """The slow part is the re-reading of the copies: it must be visible."""
    outcomes = [
        make_outcome(device, nas, b"content" * 4, name=f"PXL_2025091{index}.jpg")
        for index in range(3)
    ]
    progress = RecordingProgress()
    report = delete(device, outcomes, progress=progress)
    assert [phase[0] for phase in progress.phases] == ["Checking the copies", "Freeing"]
    assert progress.phases[0][1] == 3  # three files re-checked
    assert progress.phases[0][2] == "files"  # counted in files, no byte moves yet
    assert progress.phases[1][2] == "bytes"
    assert len(report.deleted) == 3
    assert [status for status, _name in progress.files].count("checked") == 3
    assert [status for status, _name in progress.files].count("deleted") == 3


def test_a_kept_file_is_not_checked_twice(device: FakeDevice, nas: Path):
    outcome = make_outcome(device, nas)
    outcome.destination_path.unlink()
    progress = RecordingProgress()
    report = delete(device, [outcome], progress=progress)
    assert len(report.skipped) == 1
    assert [phase[0] for phase in progress.phases] == ["Checking the copies"]


def test_the_deletion_batch_size_gives_the_bar_enough_steps():
    from dcim.deletion import delete_batch_size

    assert delete_batch_size(1) == 16
    assert delete_batch_size(3) == 16  # small sets stay one command
    assert delete_batch_size(16) == 16
    assert delete_batch_size(154) == 16  # ten updates
    assert delete_batch_size(5000) == 250  # twenty updates at most
