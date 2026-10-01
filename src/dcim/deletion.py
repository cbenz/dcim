"""Deleting the originals, once the copies on the destination are proven intact.

Safety rules enforced here, in this order:

1. only files whose transfer was recorded as *verified* are eligible;
2. the destination is checked again, just before the deletion: it must still
   exist, still have the recorded size, and — when a digest was recorded — still
   match it;
3. the original must still have the size recorded during the scan (a file that
   changed since the scan is left alone);
4. the originals are deleted **in batches**, which is what makes this phase fast
   on adb (one shell command for hundreds of files) and still correct on MTP;
5. the device is asked afterwards which paths survived, so "deleted" is never
   assumed from a command that returned zero.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .dates import resolve_capture_date
from .formatting import format_count
from .manifest import ManifestWriter
from .progress import ProgressReporter
from .selection import MediaFile
from .transfer import TransferOutcome
from .transport import DeviceTransport
from .verify import DEFAULT_PARTIAL_BLOCK_SIZE, VerifyMode, digest_file


@dataclass
class DeletionReport:
    """The result of the deletion phase."""

    deleted: list[TransferOutcome] = field(default_factory=list)
    skipped: list[tuple[TransferOutcome, str]] = field(default_factory=list)
    failed: list[tuple[TransferOutcome, str]] = field(default_factory=list)

    @property
    def freed_bytes(self) -> int:
        return sum(outcome.media_file.size for outcome in self.deleted)


def eligible_outcomes(outcomes: Sequence[TransferOutcome]) -> list[TransferOutcome]:
    """Return the transfers that a deletion phase may act on."""
    return [outcome for outcome in outcomes if outcome.verified and outcome.status != "failed"]


def revalidate(
    outcome: TransferOutcome,
    *,
    source_size: int | None,
    verify_mode: VerifyMode,
    partial_block_size: int = DEFAULT_PARTIAL_BLOCK_SIZE,
) -> str | None:
    """Return why the deletion must be refused, or None when it is allowed."""
    destination_path = outcome.destination_path
    try:
        destination_size = destination_path.stat().st_size
    except OSError as error:
        return f"the copy {destination_path} is not readable anymore: {error}"
    if destination_size != outcome.media_file.size:
        return (
            f"the copy {destination_path} is {destination_size} bytes "
            f"instead of {outcome.media_file.size}"
        )
    if verify_mode != "none" and outcome.digest:
        destination_digest = digest_file(destination_path, verify_mode, partial_block_size)
        if destination_digest.value != outcome.digest:
            return (
                f"the copy {destination_path} no longer matches the digest recorded "
                f"during the transfer ({destination_digest.value} instead of {outcome.digest})"
            )
    if source_size is None:
        return "the original is not readable anymore"
    if source_size != outcome.media_file.size:
        return f"the original changed since the scan (now {source_size} bytes): left untouched"
    return None


def delete_originals(
    outcomes: Sequence[TransferOutcome],
    *,
    transport: DeviceTransport,
    verify_mode: VerifyMode,
    progress: ProgressReporter,
    manifest: ManifestWriter | None = None,
    partial_block_size: int = DEFAULT_PARTIAL_BLOCK_SIZE,
    on_outcome: Callable[[TransferOutcome, str], None] | None = None,
) -> DeletionReport:
    """Delete the originals of the verified transfers.

    Two visible phases, because both take time and neither is instantaneous:

    * **checking the copies again** (one file at a time, counted in files): the
      destination is read back just before the deletion, which is the whole point
      of this phase, and it is the slow part on a network share;
    * **freeing** (counted in bytes): the deletions themselves, in batches that
      are small enough for the bar to move, each batch followed by asking the
      device which paths survived — so "deleted" is never assumed from a command
      that returned zero.
    """
    report = DeletionReport()
    targets = eligible_outcomes(outcomes)
    if not targets:
        return report

    # The state of the originals is read in one batch before touching anything.
    source_sizes = transport.sizes([str(outcome.media_file.device_path) for outcome in targets])
    progress.start_phase(len(targets), len(targets), label="Checking the copies", unit="files")
    accepted: list[TransferOutcome] = []
    for outcome in targets:
        progress.start_file(outcome.filename, outcome.media_file.size)
        refusal = revalidate(
            outcome,
            source_size=source_sizes.get(str(outcome.media_file.device_path)),
            verify_mode=verify_mode,
            partial_block_size=partial_block_size,
        )
        if refusal is not None:
            report.skipped.append((outcome, refusal))
            progress.complete_file(outcome.filename, "kept")
            _record(manifest, outcome, "skipped", refusal)
            if on_outcome is not None:
                on_outcome(outcome, "skipped")
            continue
        accepted.append(outcome)
        progress.complete_file(outcome.filename, "checked")
    progress.stop()

    if accepted:
        _delete_accepted(
            accepted,
            transport=transport,
            progress=progress,
            manifest=manifest,
            report=report,
            on_outcome=on_outcome,
        )
    return report


def delete_batch_size(file_count: int) -> int:
    """Return how many deletions to attempt between two progress updates.

    A batch is small enough for the bar to move (ten to twenty updates on a real
    run) and never smaller than 16, so that a handful of files is still a single
    command on the device.
    """
    return max(16, -(-file_count // 20))


def _delete_accepted(
    accepted: Sequence[TransferOutcome],
    *,
    transport: DeviceTransport,
    progress: ProgressReporter,
    manifest: ManifestWriter | None,
    report: DeletionReport,
    on_outcome: Callable[[TransferOutcome, str], None] | None,
) -> None:
    """Delete the accepted files, batch by batch, and report what survives."""
    batch_size = delete_batch_size(len(accepted))
    progress.start_phase(
        total_files=len(accepted),
        total_bytes=sum(outcome.media_file.size for outcome in accepted),
        label="Freeing",
    )
    for start in range(0, len(accepted), batch_size):
        batch = list(accepted[start : start + batch_size])
        progress.note(f"[dim]deleting {format_count(len(batch), 'file')} on the device…[/dim]")
        transport.delete([str(outcome.media_file.device_path) for outcome in batch], _ignore)
        survivors = transport.remaining([str(outcome.media_file.device_path) for outcome in batch])
        for outcome in batch:
            path = str(outcome.media_file.device_path)
            if path in survivors:
                detail = f"the file is still on the device after the deletion: {path}"
                report.failed.append((outcome, detail))
                progress.complete_file(outcome.filename, "failed")
                _record(manifest, outcome, "failed", detail)
                if on_outcome is not None:
                    on_outcome(outcome, "failed")
                continue
            report.deleted.append(outcome)
            progress.complete_file(outcome.filename, "deleted")
            progress.advance(outcome.media_file.size)
            _record(manifest, outcome, "deleted", "")
            if on_outcome is not None:
                on_outcome(outcome, "deleted")
    progress.stop()


def _ignore(_count: int) -> None:
    """Swallow the per-file progress of the transport, which cannot know."""


def outcomes_from_manifest(entries: Sequence[dict], directory: str) -> list[TransferOutcome]:
    """Rebuild transfer outcomes from manifest records, for ``--resume``.

    Manifest records store the path *relative to the source directory*, because
    the mount point — and now the transport root too — changes between runs.
    """
    outcomes: list[TransferOutcome] = []
    for entry in entries:
        try:
            relative_path = Path(str(entry["relative_path"]).lstrip("/"))
            destination_path = Path(entry["destination"])
            size = int(entry["size"])
            modified_at = float(entry.get("modified_at") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        device_path = Path(directory.strip("/")) / relative_path
        outcomes.append(
            TransferOutcome(
                media_file=MediaFile(
                    device_path=device_path,
                    relative_path=relative_path,
                    size=size,
                    modified_at=modified_at,
                    capture_date=resolve_capture_date(device_path.name, modified_at),
                ),
                destination_path=destination_path,
                status="already_present",
                verified=True,
                detail="resumed from manifest",
                digest=str(entry.get("digest") or ""),
            )
        )
    return outcomes


def _record(
    manifest: ManifestWriter | None, outcome: TransferOutcome, status: str, detail: str
) -> None:
    if manifest is None:
        return
    manifest.record(
        "deletion",
        filename=outcome.filename,
        source=str(outcome.media_file.device_path),
        relative_path=outcome.media_file.relative_posix,
        destination=str(outcome.destination_path),
        size=outcome.media_file.size,
        status=status,
        detail=detail,
    )
