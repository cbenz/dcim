"""Copying files from the device to the destination, and verifying them.

The order is always the same, and deletion is never part of it:

1. the transport copies the source onto a ``.part`` file next to the destination;
2. the source digest is obtained — for free when the transport reads the bytes
   (`mtp` hashes while copying), or from a batched device-side hash (`adb`, one
   shell call for many files, no byte over the wire);
3. ``fsync`` and atomically rename the ``.part`` file into place;
4. the destination is read back and its digest compared with the source one;
5. record the outcome — a later phase may delete the original, and only if this
   step reported an intact copy.
"""

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import TransferError, TransportError
from .manifest import ManifestWriter
from .progress import ProgressReporter
from .selection import MediaFile
from .transport import DeviceTransport
from .verify import DEFAULT_PARTIAL_BLOCK_SIZE, Digest, VerifyMode, digest_file

TransferStatus = Literal["copied", "already_present", "conflict", "failed"]
ConflictPolicy = Literal["skip", "overwrite", "rename"]

DIGEST_BATCH_SIZE = 32


@dataclass(frozen=True)
class TransferPlan:
    """What will happen to one candidate: nothing, or a copy."""

    media_file: MediaFile
    destination_path: Path
    already_present: bool


@dataclass(frozen=True)
class TransferOutcome:
    """What happened to one candidate file."""

    media_file: MediaFile
    destination_path: Path
    status: TransferStatus
    verified: bool
    detail: str = ""
    digest: str = ""

    @property
    def filename(self) -> str:
        return self.media_file.filename


class SourceDigests:
    """Source digests, fetched in batches.

    A digest is what proves a copy, so it is needed for every file — but a
    transport that does not hash while reading (adb) would pay one round trip per
    file. Fetching a batch ahead of the transfer costs one shell call per
    `batch_size` files instead.
    """

    def __init__(
        self,
        transport: DeviceTransport,
        media_files: Sequence[MediaFile],
        *,
        mode: VerifyMode,
        block_size: int,
        batch_size: int = DIGEST_BATCH_SIZE,
    ) -> None:
        self.transport = transport
        self.mode: VerifyMode = mode
        self.block_size = block_size
        self.batch_size = batch_size
        self._sizes = {str(media_file.device_path): media_file.size for media_file in media_files}
        self._order = [str(media_file.device_path) for media_file in media_files]
        self._index = {path: index for index, path in enumerate(self._order)}
        self.cache: dict[str, Digest] = {}
        self.attempted: set[str] = set()

    def get(self, media_file: MediaFile) -> Digest | None:
        """Return the digest of a file, fetching a batch when it is missing."""
        if self.mode == "none":
            return None
        key = str(media_file.device_path)
        if key not in self.cache and key not in self.attempted:
            self._fetch(key)
        return self.cache.get(key)

    def _fetch(self, key: str) -> None:
        start = self._index.get(key, 0)
        batch = self._order[start : start + self.batch_size]
        files = [(path, self._sizes[path]) for path in batch]
        self.attempted.update(batch)
        self.cache.update(self.transport.digests(files, self.mode, self.block_size))


def plan_transfers(
    media_files: Sequence[MediaFile], destination_root: Path, *, preserve_tree: bool
) -> list[TransferPlan]:
    """Split the selection into what is already there and what must be copied.

    "Already there" is a file of the same name **and** the same size: the answer
    is read from the destination only, and decides everything downstream — what
    gets copied (nothing), and what the progress bar is a bar *of*.
    """
    plans: list[TransferPlan] = []
    for media_file in media_files:
        destination_path = plan_destination_path(
            destination_root, media_file, preserve_tree=preserve_tree
        )
        present = False
        if destination_path.exists():
            try:
                present = destination_path.stat().st_size == media_file.size
            except OSError:
                present = False
        plans.append(
            TransferPlan(
                media_file=media_file,
                destination_path=destination_path,
                already_present=present,
            )
        )
    return plans


def plan_destination_path(
    destination_root: Path, media_file: MediaFile, *, preserve_tree: bool
) -> Path:
    """Return where a candidate must land, given the layout policy."""
    if preserve_tree and media_file.relative_path.parent != Path():
        return destination_root / media_file.relative_path
    return destination_root / media_file.filename


def check_destination_is_separate(destination_root: Path, source_root: Path) -> None:
    """Refuse a destination located inside the device mount.

    Deleting the originals is only safe if the copies live on another device: a
    destination under the source would make the deletion phase remove the only
    remaining copy.
    """
    resolved_destination = destination_root.resolve()
    resolved_source = source_root.resolve()
    if resolved_destination.is_relative_to(resolved_source) or resolved_source.is_relative_to(
        resolved_destination
    ):
        raise TransferError(
            f"the destination {destination_root} overlaps the device at {source_root}; "
            "choose a destination on another device"
        )


def transfer_all(
    media_files: Sequence[MediaFile],
    *,
    transport: DeviceTransport,
    destination_root: Path,
    verify_mode: VerifyMode,
    conflict_policy: ConflictPolicy,
    preserve_tree: bool,
    preserve_times: bool,
    progress: ProgressReporter,
    manifest: ManifestWriter | None = None,
    partial_block_size: int = DEFAULT_PARTIAL_BLOCK_SIZE,
    fsync_destination: bool = True,
    on_outcome: Callable[[TransferOutcome], None] | None = None,
) -> list[TransferOutcome]:
    """Verify what is already at the destination, then copy what is missing.

    Two phases, for one reason: a single phase could not have an honest progress
    bar. Files already present are not copied at all — but they are still
    *verified*, because that verification is what allows their original to be
    deleted later. Mixing both kinds in one bar left it at 0 % while it went
    through a hundred files that needed no byte.
    """
    destination_root.mkdir(parents=True, exist_ok=True)
    plans = plan_transfers(media_files, destination_root, preserve_tree=preserve_tree)
    present = [plan for plan in plans if plan.already_present]
    missing = [plan for plan in plans if not plan.already_present]
    outcomes: list[TransferOutcome] = []
    outcomes.extend(
        _verify_present(
            present,
            transport=transport,
            verify_mode=verify_mode,
            progress=progress,
            manifest=manifest,
            partial_block_size=partial_block_size,
            on_outcome=on_outcome,
        )
    )
    outcomes.extend(
        _copy_missing(
            missing,
            transport=transport,
            verify_mode=verify_mode,
            conflict_policy=conflict_policy,
            preserve_times=preserve_times,
            progress=progress,
            manifest=manifest,
            partial_block_size=partial_block_size,
            fsync_destination=fsync_destination,
            on_outcome=on_outcome,
        )
    )
    return outcomes


def _verify_present(
    plans: Sequence[TransferPlan],
    *,
    transport: DeviceTransport,
    verify_mode: VerifyMode,
    progress: ProgressReporter,
    manifest: ManifestWriter | None,
    partial_block_size: int,
    on_outcome: Callable[[TransferOutcome], None] | None,
) -> list[TransferOutcome]:
    """Check that the copies already at the destination are intact."""
    if not plans:
        return []
    progress.start_phase(len(plans), len(plans), label="Verifying", unit="files")
    digests = SourceDigests(
        transport,
        [plan.media_file for plan in plans],
        mode=verify_mode,
        block_size=partial_block_size,
    )
    outcomes: list[TransferOutcome] = []
    for plan in plans:
        media_file = plan.media_file
        progress.start_file(media_file.filename, media_file.size)
        try:
            outcome = _verify_already_present(
                media_file, plan.destination_path, digests, verify_mode, partial_block_size
            )
        except (OSError, TransferError, TransportError) as error:
            outcome = TransferOutcome(
                media_file=media_file,
                destination_path=plan.destination_path,
                status="failed",
                verified=False,
                detail=f"the copy could not be verified: {error}",
            )
        progress.complete_file(media_file.filename, outcome.status)
        if manifest is not None:
            record_outcome(manifest, outcome)
        if on_outcome is not None:
            on_outcome(outcome)
        outcomes.append(outcome)
    progress.stop()
    return outcomes


def _copy_missing(
    plans: Sequence[TransferPlan],
    *,
    transport: DeviceTransport,
    verify_mode: VerifyMode,
    conflict_policy: ConflictPolicy,
    preserve_times: bool,
    progress: ProgressReporter,
    manifest: ManifestWriter | None,
    partial_block_size: int,
    fsync_destination: bool,
    on_outcome: Callable[[TransferOutcome], None] | None,
) -> list[TransferOutcome]:
    """Copy the files the destination does not have yet."""
    if not plans:
        return []
    progress.start_phase(
        total_files=len(plans),
        total_bytes=sum(plan.media_file.size for plan in plans),
        label="Copying",
    )
    digests = SourceDigests(
        transport,
        [plan.media_file for plan in plans],
        mode=verify_mode,
        block_size=partial_block_size,
    )
    outcomes: list[TransferOutcome] = []
    for plan in plans:
        media_file = plan.media_file
        destination_path = plan.destination_path
        progress.start_file(media_file.filename, media_file.size)
        try:
            outcome = transfer_one(
                media_file,
                destination_path,
                transport=transport,
                digests=digests,
                verify_mode=verify_mode,
                conflict_policy=conflict_policy,
                preserve_times=preserve_times,
                partial_block_size=partial_block_size,
                fsync_destination=fsync_destination,
                progress=progress,
            )
        except (OSError, TransferError, TransportError) as error:
            outcome = TransferOutcome(
                media_file=media_file,
                destination_path=destination_path,
                status="failed",
                verified=False,
                detail=str(error),
            )
        progress.complete_file(media_file.filename, outcome.status)
        if manifest is not None:
            record_outcome(manifest, outcome)
        if on_outcome is not None:
            on_outcome(outcome)
        outcomes.append(outcome)
    progress.stop()
    return outcomes


def transfer_one(
    media_file: MediaFile,
    destination_path: Path,
    *,
    transport: DeviceTransport,
    digests: SourceDigests,
    verify_mode: VerifyMode,
    conflict_policy: ConflictPolicy,
    preserve_times: bool,
    progress: ProgressReporter,
    partial_block_size: int = DEFAULT_PARTIAL_BLOCK_SIZE,
    fsync_destination: bool = True,
) -> TransferOutcome:
    """Copy one file and verify it, or recognize it as already present."""
    if destination_path.exists():
        existing_size = destination_path.stat().st_size
        if existing_size == media_file.size:
            return _verify_already_present(
                media_file, destination_path, digests, verify_mode, partial_block_size
            )
        if conflict_policy == "skip":
            return TransferOutcome(
                media_file=media_file,
                destination_path=destination_path,
                status="conflict",
                verified=False,
                detail=(
                    f"{destination_path.name} already exists with a different size "
                    f"({existing_size} bytes instead of {media_file.size})"
                ),
            )
        if conflict_policy == "rename":
            destination_path = _next_available_path(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source_digest = _copy_with_digest(
        media_file,
        destination_path,
        transport=transport,
        digests=digests,
        verify_mode=verify_mode,
        partial_block_size=partial_block_size,
        fsync_destination=fsync_destination,
        progress=progress,
    )
    if preserve_times:
        os.utime(destination_path, (media_file.modified_at, media_file.modified_at))
    if verify_mode == "none" or source_digest is None:
        return TransferOutcome(
            media_file=media_file,
            destination_path=destination_path,
            status="copied",
            verified=True,
            detail="size only",
            digest="",
        )
    destination_digest = digest_file(destination_path, verify_mode, partial_block_size)
    if not source_digest.matches(destination_digest):
        destination_path.unlink(missing_ok=True)
        raise TransferError(
            f"the copy of {media_file.filename} does not match the original "
            f"(digest {destination_digest.value} instead of {source_digest.value}); "
            "the incomplete copy was removed"
        )
    return TransferOutcome(
        media_file=media_file,
        destination_path=destination_path,
        status="copied",
        verified=True,
        detail=f"{verify_mode} digest, {source_digest.coverage} coverage",
        digest=source_digest.value,
    )


def _verify_already_present(
    media_file: MediaFile,
    destination_path: Path,
    digests: SourceDigests,
    verify_mode: VerifyMode,
    partial_block_size: int,
) -> TransferOutcome:
    if verify_mode == "none":
        return TransferOutcome(
            media_file=media_file,
            destination_path=destination_path,
            status="already_present",
            verified=True,
            detail="size only",
        )
    source_digest = digests.get(media_file)
    if source_digest is None:
        return TransferOutcome(
            media_file=media_file,
            destination_path=destination_path,
            status="already_present",
            verified=False,
            detail="the digest of the original could not be read",
        )
    destination_digest = digest_file(destination_path, verify_mode, partial_block_size)
    if not source_digest.matches(destination_digest):
        return TransferOutcome(
            media_file=media_file,
            destination_path=destination_path,
            status="conflict",
            verified=False,
            detail="a file with the same size but a different content already exists",
        )
    return TransferOutcome(
        media_file=media_file,
        destination_path=destination_path,
        status="already_present",
        verified=True,
        detail=f"{verify_mode} digest, {source_digest.coverage} coverage",
        digest=source_digest.value,
    )


def _copy_with_digest(
    media_file: MediaFile,
    destination_path: Path,
    *,
    transport: DeviceTransport,
    digests: SourceDigests,
    verify_mode: VerifyMode,
    partial_block_size: int,
    fsync_destination: bool,
    progress: ProgressReporter,
) -> Digest | None:
    temporary_path = destination_path.with_name(f".{destination_path.name}.part-{os.getpid()}")
    try:
        source_digest = transport.copy_to(
            str(media_file.device_path),
            media_file.size,
            temporary_path,
            progress.advance,
            mode=verify_mode,
            block_size=partial_block_size,
        )
        if fsync_destination:
            _fsync(temporary_path)
        temporary_path.replace(destination_path)
    except (OSError, TransportError):
        temporary_path.unlink(missing_ok=True)
        destination_path.unlink(missing_ok=True)
        raise
    if source_digest is None:
        # The transport did not hash the bytes it wrote: ask the device (one
        # shell call per batch of files, no byte over the wire).
        return digests.get(media_file)
    return source_digest


def _fsync(path: Path) -> None:
    """Flush a closed file to the destination device."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _next_available_path(destination_path: Path) -> Path:
    for counter in range(1, 1000):
        candidate = destination_path.with_name(
            f"{destination_path.stem} ({counter}){destination_path.suffix}"
        )
        if not candidate.exists():
            return candidate
    raise TransferError(f"cannot find an available name next to {destination_path}")


def record_outcome(manifest: ManifestWriter, outcome: TransferOutcome) -> None:
    """Append a transfer outcome to the manifest."""
    manifest.record(
        "transfer",
        filename=outcome.filename,
        source=str(outcome.media_file.device_path),
        relative_path=outcome.media_file.relative_posix,
        destination=str(outcome.destination_path),
        size=outcome.media_file.size,
        modified_at=outcome.media_file.modified_at,
        status=outcome.status,
        verified=outcome.verified,
        digest=outcome.digest,
        detail=outcome.detail,
    )
