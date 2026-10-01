"""Device transports: how files are listed, read, and deleted on the device.

Two transports implement the same small protocol, and a run uses exactly one of
them from beginning to end:

``mtp``
    The device mounted with `aft-mtp-mount`. Ordinary file operations on the
    mount point — simple, and the only option when USB debugging is off. Measured
    at 7.3 MB/s reading and ~51 ms per deletion on a Pixel 7.
``adb``
    `adb shell` for metadata and deletion, `adb pull` for the bytes. Measured at
    106 MB/s reading and ~4.6 ms per deletion on the same device, because a batch
    of deletions is one shell command instead of one MTP round trip per file.

Mixing them is not an option: `aft-mtp-mount` caches directory listings and does
not notice changes made outside it, so an adb-created file is invisible over the
mount, and a file deleted by adb is still listed (and answered with an I/O error
when touched) until the mount is remounted.

Paths are **relative to the transport root** everywhere in this program
(``DCIM/Camera/PXL_…jpg``), so no other module needs to know whether the device
is mounted or reached through adb.
"""

import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .analysis import StorageSpace
from .errors import TransportError
from .verify import Digest, StreamDigest, VerifyMode, digest_file

ADB_BINARY = "adb"
ADB_TIMEOUT = 120.0
DEFAULT_BATCH_SIZE = 64
DEFAULT_ADB_ROOT = "/sdcard"
#: SHA-256 of the empty string. Used instead of `tail -c 0`: toybox, the shell of
#: Android devices, prints the **whole file** for a null byte count, where GNU
#: tail prints nothing — which made every file shorter than one block fail its
#: verification, the device digest hashing the file twice.
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
EXTERNAL_STORAGE_ROOT = "/storage"
COPY_BLOCK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class RemoteEntry:
    """One entry of a device directory."""

    path: str
    name: str
    is_directory: bool
    size: int
    modified_at: float


class DeviceTransport(Protocol):
    """What the rest of the program needs from a device."""

    name: str

    def describe(self) -> str:
        """Return a one-line description of the transport, for the report."""
        ...

    def volumes(self) -> list[tuple[str, str]]:
        """Return ``(label, root)`` for each storage the device exposes."""
        ...

    def storage_space(self, root: str) -> StorageSpace | None:
        """Return the capacity and free space of the storage holding `root`."""
        ...

    def list_directory(self, directory: str) -> list[RemoteEntry]:
        """Return the entries of a device directory."""
        ...

    def directory_exists(self, directory: str) -> bool:
        """Tell whether a device directory exists."""
        ...

    def with_root(self, root: str) -> "DeviceTransport":
        """Return the same transport, rooted at another storage."""
        ...

    def local_root(self) -> Path | None:
        """Return the local path of the storage, when the device is mounted."""
        ...

    def sizes(self, paths: Sequence[str]) -> dict[str, int]:
        """Return the size of each path that still exists."""
        ...

    def digests(
        self,
        files: Sequence[tuple[str, int]],
        mode: VerifyMode,
        block_size: int,
    ) -> dict[str, Digest]:
        """Return the digest of each file, computed on the device side."""
        ...

    def copy_to(
        self,
        path: str,
        size: int,
        destination: Path,
        on_progress: Callable[[int], None],
        *,
        mode: VerifyMode,
        block_size: int,
    ) -> Digest | None:
        """Copy a device file to a local path, returning its digest when free."""
        ...

    def delete(self, paths: Sequence[str], on_progress: Callable[[int], None]) -> None:
        """Delete the given files, in as few round trips as possible."""
        ...

    def remaining(self, paths: Sequence[str]) -> set[str]:
        """Return the subset of paths that still exist."""
        ...

    def release(self) -> None:
        """Release whatever the transport holds (a mount, a process...)."""
        ...


def chunked(items: Sequence, size: int) -> Iterator[Sequence]:
    """Split a sequence into chunks of at most `size` elements."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def quote_for_shell(value: str) -> str:
    """Quote a path so that the device shell sees it as one word."""
    return "'" + value.replace("'", "'\\''") + "'"


class MtpTransport:
    """A device mounted with `aft-mtp-mount`, used through ordinary file calls."""

    name = "mtp"

    def __init__(self, mountpoint: Path, root: str = "") -> None:
        self.mountpoint = mountpoint
        self.root = root.strip("/")

    def describe(self) -> str:
        where = f"{self.mountpoint}/{self.root}" if self.root else str(self.mountpoint)
        return f"MTP mount at {where}"

    def with_root(self, root: str) -> "MtpTransport":
        return MtpTransport(self.mountpoint, root)

    def local_root(self) -> Path | None:
        return self.resolve("") if self.root else self.mountpoint

    def resolve(self, path: str) -> Path:
        """Return the local path of a storage-relative path."""
        return self.mountpoint / self.root / path.lstrip("/")

    def volumes(self) -> list[tuple[str, str]]:
        return [
            (entry.name, entry.name)
            for entry in sorted(os.scandir(self.mountpoint), key=lambda entry: entry.name)
            if entry.is_dir(follow_symlinks=False)
        ]

    def storage_space(self, root: str) -> StorageSpace | None:
        try:
            usage = os.statvfs(self.resolve(root))
        except OSError:
            return None
        total = usage.f_blocks * usage.f_frsize
        free = usage.f_bavail * usage.f_frsize
        if total <= 0:
            return None
        return StorageSpace(total=total, used=total - free, free=free)

    def list_directory(self, directory: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        try:
            scandir_context = os.scandir(self.resolve(directory))
        except OSError as error:
            raise TransportError(f"cannot list {directory} on the device: {error}") from error
        with scandir_context as scanned:
            for entry in scanned:
                try:
                    stat_result = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise TransportError(f"cannot read {entry.name}: {error}") from error
                entries.append(
                    RemoteEntry(
                        path=f"{directory.strip('/')}/{entry.name}",
                        name=entry.name,
                        is_directory=entry.is_dir(follow_symlinks=False),
                        size=stat_result.st_size,
                        modified_at=stat_result.st_mtime,
                    )
                )
        return entries

    def directory_exists(self, directory: str) -> bool:
        return self.resolve(directory).is_dir()

    def sizes(self, paths: Sequence[str]) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for path in paths:
            try:
                sizes[path] = self.resolve(path).stat().st_size
            except OSError:
                continue
        return sizes

    def digests(
        self, files: Sequence[tuple[str, int]], mode: VerifyMode, block_size: int
    ) -> dict[str, Digest]:
        digests: dict[str, Digest] = {}
        for path, _size in files:
            try:
                digests[path] = digest_file(self.resolve(path), mode, block_size)
            except OSError as error:
                raise TransportError(f"cannot read {path} on the device: {error}") from error
        return digests

    def copy_to(
        self,
        path: str,
        size: int,
        destination: Path,
        on_progress: Callable[[int], None],
        *,
        mode: VerifyMode,
        block_size: int,
    ) -> Digest | None:
        """Copy through the mount, hashing the bytes as they are read."""
        del size
        stream_digest = None if mode == "none" else StreamDigest(mode, block_size)
        try:
            with self.resolve(path).open("rb") as source, destination.open("wb") as target:
                for chunk in _read_blocks(source):
                    target.write(chunk)
                    on_progress(len(chunk))
                    if stream_digest is not None:
                        stream_digest.update(chunk)
        except OSError as error:
            raise TransportError(f"cannot copy {path} from the device: {error}") from error
        return stream_digest.digest() if stream_digest is not None else None

    def delete(self, paths: Sequence[str], on_progress: Callable[[int], None]) -> None:
        for path in paths:
            try:
                self.resolve(path).unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                raise TransportError(f"cannot delete {path} on the device: {error}") from error
            on_progress(1)

    def remaining(self, paths: Sequence[str]) -> set[str]:
        return {path for path in paths if self.resolve(path).exists()}

    def release(self) -> None:
        pass


class AdbTransport:
    """A device reached through ``adb``: metadata and deletion by shell, bytes by pull."""

    name = "adb"

    def __init__(
        self,
        serial: str | None = None,
        *,
        root: str = DEFAULT_ADB_ROOT,
        batch_size: int = DEFAULT_BATCH_SIZE,
        executable: str = ADB_BINARY,
    ) -> None:
        self.serial = serial
        self.root = root.rstrip("/") or "/"
        self.batch_size = batch_size
        self.executable = executable

    def describe(self) -> str:
        return f"adb on {self.serial or 'the connected device'} ({self.root})"

    def with_root(self, root: str) -> "AdbTransport":
        return AdbTransport(
            self.serial,
            root=root,
            batch_size=self.batch_size,
            executable=self.executable,
        )

    def local_root(self) -> Path | None:
        return None

    def directory_exists(self, directory: str) -> bool:
        # `|| true`: a directory that is not there is an answer, not a failure.
        answer = self.shell(
            f"[ -d {quote_for_shell(self.device_path(directory))} ] && echo yes || true"
        )
        return answer.strip() == "yes"

    def device_path(self, path: str) -> str:
        """Return the absolute device path of a transport-relative path."""
        return f"{self.root}/{path.lstrip('/')}"

    def relative_path(self, device_path: str) -> str:
        """Return the transport-relative path of an absolute device path."""
        return device_path.removeprefix(f"{self.root}/")

    def volumes(self) -> list[tuple[str, str]]:
        volumes = [("Internal storage", self.root)]
        listing = self.shell(f"ls -d {EXTERNAL_STORAGE_ROOT}/* 2>/dev/null || true")
        for line in listing.splitlines():
            name = line.strip()
            if name and not name.endswith(("/emulated", "/self")):
                volumes.append((Path(name).name, name))
        return volumes

    def storage_space(self, root: str) -> StorageSpace | None:
        # The path must be absolute: `df` without one lists every file system of
        # the device, and the first line is the read-only system partition.
        for source in (self.device_path(root), "/data"):
            output = self.shell(f"df -k {quote_for_shell(source)} 2>/dev/null || true")
            space = parse_df(output, path=source)
            if space is not None:
                return space
        return None

    def list_directory(self, directory: str) -> list[RemoteEntry]:
        """List a directory with one `stat` call, whatever its size."""
        # `cd` failing must fail the call: an empty listing for a missing
        # directory would silently look like a directory with no file. The
        # path is absolute because the device shell starts in `/`.
        command = (
            f"cd {quote_for_shell(self.device_path(directory))} 2>/dev/null || exit 66; "
            "stat -c '%s|%Y|%F|%n' -- * 2>/dev/null || true"
        )
        return [
            entry
            for entry in parse_stat_lines(self.shell(command), directory=f"{directory.strip('/')}/")
        ]

    def sizes(self, paths: Sequence[str]) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for batch in chunked(list(paths), self.batch_size):
            payload = b"\0".join(self.device_path(path).encode() for path in batch)
            # `|| true`: a path that vanished between the scan and now is skipped.
            output = self.shell("xargs -0 stat -c '%s|%n' 2>/dev/null || true", input_bytes=payload)
            for device_path, size in parse_size_lines(output).items():
                sizes[self.relative_path(device_path)] = size
        return sizes

    def digests(
        self, files: Sequence[tuple[str, int]], mode: VerifyMode, block_size: int
    ) -> dict[str, Digest]:
        """Hash on the device: one shell call per batch, no byte over the wire."""
        if mode == "none":
            return {}
        digests: dict[str, Digest] = {}
        wanted = dict(files)
        for batch in chunked(list(files), self.batch_size):
            output = self.shell(digest_script(batch, mode, block_size, root=self.root))
            digests.update(parse_digest_lines(output, wanted, mode=mode, block_size=block_size))
        return digests

    def copy_to(
        self,
        path: str,
        size: int,
        destination: Path,
        on_progress: Callable[[int], None],
        *,
        mode: VerifyMode,
        block_size: int,
    ) -> Digest | None:
        """Pull a file, reporting progress by watching the local file grow.

        `adb pull` uses the fast sync protocol (106 MB/s measured, against 7.3
        through the mount) but reports its own progress, which would fight with
        ours: the destination size is polled instead. The digest is not free
        here — the caller gets it from :meth:`digests`.
        """
        del mode, block_size
        device_path = self.device_path(path)
        with subprocess.Popen(
            [*self._base_command(), "pull", device_path, str(destination)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as process:
            reported = 0
            output = b""
            while True:
                reported = report_growth(destination, reported, on_progress)
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            if process.stdout is not None:
                output = process.stdout.read()
            return_code = process.returncode
        if return_code != 0:
            raise TransportError(
                f"adb pull {device_path} failed: {output.decode('utf-8', 'replace').strip()}"
            )
        written = destination.stat().st_size
        if written != size:
            raise TransportError(f"{path} is {written} bytes instead of {size} after the pull")
        return None

    def delete(self, paths: Sequence[str], on_progress: Callable[[int], None]) -> None:
        """Delete a batch of files with a single shell command per chunk."""
        for batch in chunked(list(paths), self.batch_size * 4):
            payload = b"\0".join(self.device_path(path).encode() for path in batch)
            self.shell("xargs -0 rm -f", input_bytes=payload)
            for _path in batch:
                on_progress(1)

    def remaining(self, paths: Sequence[str]) -> set[str]:
        survivors: set[str] = set()
        # `|| true`: a path that survived is an answer, and so is one that did not.
        script = 'xargs -0 -n1 sh -c \'[ -e "$0" ] && echo "$0" || true\' || true'
        for batch in chunked(list(paths), self.batch_size):
            payload = b"\0".join(self.device_path(path).encode() for path in batch)
            for line in self.shell(script, input_bytes=payload).splitlines():
                survivor = line.strip()
                if survivor:
                    survivors.add(self.relative_path(survivor))
        return survivors

    def shell(self, command: str, *, input_bytes: bytes | None = None) -> str:
        """Run a shell command on the device and return its output."""
        completed = self._run(["shell", command], input_bytes=input_bytes)
        if completed.returncode != 0:
            message = completed.stderr.decode("utf-8", "replace").strip()
            if not message:
                message = completed.stdout.decode("utf-8", "replace").strip()
            raise TransportError(f"the device refused: {command}\n{message}")
        return completed.stdout.decode("utf-8", "replace")

    def release(self) -> None:
        pass

    def _base_command(self) -> list[str]:
        command = [self.executable]
        if self.serial:
            command.extend(["-s", self.serial])
        return command

    def _run(
        self, arguments: Sequence[str], *, input_bytes: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [*self._base_command(), *arguments],
                input=input_bytes,
                capture_output=True,
                check=False,
                timeout=ADB_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TransportError(f"adb failed: {error}") from error


class LocalTransport(MtpTransport):
    """An `MtpTransport` whose root is any local directory.

    Used by the tests, and by anyone who wants to exercise the whole pipeline
    (scan, copy, verify, delete) without a device.
    """

    name = "local"

    def describe(self) -> str:
        return f"local directory {self.mountpoint}"


def adb_serial(executable: str = ADB_BINARY) -> str | None:
    """Return the serial of the first authorized adb device, if any."""
    if shutil.which(executable) is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "devices"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in completed.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            return fields[0]
    return None


def _read_blocks(source) -> Iterator[bytes]:
    """Read a file in blocks until the end."""
    return iter(lambda: source.read(COPY_BLOCK_SIZE), b"")


def parse_stat_lines(output: str, *, directory: str) -> Iterator[RemoteEntry]:
    """Parse ``stat -c '%s|%Y|%F|%n'`` output."""
    for line in output.splitlines():
        fields = line.split("|", 3)
        if len(fields) != 4:
            continue
        size, modified, kind, name = fields
        if not size.isdigit() or not _is_number(modified):
            continue
        yield RemoteEntry(
            path=f"{directory}{name}",
            name=name,
            is_directory=kind.startswith("directory"),
            size=int(size),
            modified_at=float(modified),
        )


def parse_size_lines(output: str) -> dict[str, int]:
    """Parse ``stat -c '%s|%n'`` output."""
    sizes: dict[str, int] = {}
    for line in output.splitlines():
        fields = line.split("|", 1)
        if len(fields) == 2 and fields[0].isdigit():
            sizes[fields[1]] = int(fields[0])
    return sizes


def parse_df(output: str, path: str | None = None) -> StorageSpace | None:
    """Parse ``df -k`` output: total and available, in kilobytes.

    When several file systems are listed — `df` without an argument lists every
    one of them, including the read-only system partitions — the line whose mount
    point is the longest prefix of `path` wins. A single line is the common case,
    and a device whose mount point does not prefix the queried path (/sdcard is
    /storage/emulated there) still gets that line.
    """
    candidates: list[tuple[int, StorageSpace]] = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 4 or not fields[1].isdigit() or not fields[3].isdigit():
            continue
        total = int(fields[1]) * 1024
        free = int(fields[3]) * 1024
        space = StorageSpace(total=total, used=max(total - free, 0), free=free)
        mounted_on = fields[-1] if len(fields) >= 6 else ""
        prefix = len(mounted_on) if path and path.startswith(mounted_on) else 0
        candidates.append((prefix, space))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def digest_script(
    batch: Sequence[tuple[str, int]], mode: VerifyMode, block_size: int, *, root: str
) -> str:
    """Build the device-side script hashing a batch of files.

    The byte ranges are the ones hashed locally (`partial`: the first block and
    the last one, files shorter than two blocks being covered entirely), so the
    digests compare with the ones computed by :mod:`dcim.verify` — and no byte
    travels over the wire.
    """
    lines: list[str] = []
    for path, size in batch:
        quoted = quote_for_shell(f"{root}/{path.lstrip('/')}")
        if mode == "full":
            lines.append(
                f"v=$(sha256sum {quoted} | cut -d' ' -f1); "
                f"printf '%s|%s\\n' {quote_for_shell(path)} \"$v\""
            )
            continue
        head_bytes = min(size, block_size)
        tail_bytes = size - head_bytes if size <= 2 * block_size else block_size
        lines.append(
            f"h={_block_hash_command(quoted, head_bytes, 'head')}; "
            f"t={_block_hash_command(quoted, tail_bytes, 'tail')}; "
            f'printf \'%s|%s|%s\\n\' {quote_for_shell(path)} "$h" "$t"'
        )
    return " ; ".join(lines)


def _block_hash_command(quoted_path: str, byte_count: int, tool: str) -> str:
    """Hash `byte_count` bytes of a file, without asking for zero of them.

    A null byte count means "nothing to hash", so the empty digest is used
    directly: `tail -c 0` on the device returns the whole file instead.
    """
    if byte_count <= 0:
        return EMPTY_SHA256
    return f"$({tool} -c {byte_count} {quoted_path} | sha256sum | cut -d' ' -f1)"


def parse_digest_lines(
    output: str,
    wanted: dict[str, int],
    *,
    mode: VerifyMode,
    block_size: int,
) -> dict[str, Digest]:
    """Build digests from the device script's output.

    The device returns *hashes of byte ranges*, so the digest value is assembled
    here exactly as :func:`dcim.verify.compose_partial_digest` would: same value,
    same coverage.
    """
    digests: dict[str, Digest] = {}
    for line in output.splitlines():
        fields = line.split("|")
        if len(fields) < 2 or fields[0] not in wanted:
            continue
        path = fields[0]
        size = wanted[path]
        if mode == "full":
            if len(fields) >= 2 and fields[1]:
                digests[path] = Digest(size=size, value=fields[1], coverage="full")
            continue
        if len(fields) >= 3 and fields[1] and fields[2]:
            digests[path] = Digest(
                size=size,
                value=f"{fields[1]}-{fields[2]}",
                coverage="full" if size <= 2 * block_size else "partial",
            )
    return digests


def report_growth(destination: Path, reported: int, on_progress: Callable[[int], None]) -> int:
    """Declare how many bytes the local file gained since the last look."""
    try:
        current = destination.stat().st_size
    except OSError:
        return reported
    if current > reported:
        on_progress(current - reported)
        return current
    return reported


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True
