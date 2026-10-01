"""The MTP transport: mounting the phone with android-file-transfer.

``aft-mtp-mount`` (from android-file-transfer) is used rather than gvfs because
it exposes the device as a plain FUSE file system, which lets the rest of the
program use ordinary ``open``/``stat``/``unlink`` calls — and therefore real
read-back verification and real deletions.

Two facts drive this module:

* the MTP USB interface can only be claimed by one process at a time, so any
  ``gvfsd-mtp`` daemon (started by the file manager) must be released first;
* the storage volumes are exposed as top-level directories whose names are
  localized (``Espace de stockage interne partagé`` on a French phone), so they
  are discovered at runtime and never hardcoded.
"""

import contextlib
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import DeviceNotFoundError, MtpMountError
from .mounts import filesystem_type, is_mounted

MTP_MOUNT_FILESYSTEM_TYPE = "fuse.aft-mtp-mount"
MOUNT_BINARY = "aft-mtp-mount"
CLI_BINARY = "aft-mtp-cli"
DEFAULT_MOUNT_TIMEOUT = 30.0


@dataclass(frozen=True)
class StorageVolume:
    """A storage exposed by the phone (internal storage, SD card...)."""

    path: Path
    label: str


def list_mtp_devices(timeout: float = 20.0) -> list[str]:
    """Return the MTP devices seen by android-file-transfer."""
    executable = shutil.which(CLI_BINARY)
    if executable is None:
        raise MtpMountError(f"{CLI_BINARY} is not installed (package android-file-transfer)")
    try:
        completed = subprocess.run(
            [executable, "-l"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise MtpMountError(f"{CLI_BINARY} -l timed out after {timeout:.0f}s") from error
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def release_gvfs_mtp() -> None:
    """Stop the gvfs MTP daemon so that the USB interface becomes claimable."""
    while True:
        completed = subprocess.run(["pkill", "-x", "gvfsd-mtp"], capture_output=True, check=False)
        if completed.returncode != 0:
            return
        time.sleep(0.2)


class MtpMount:
    """An ``aft-mtp-mount`` file system, mounted on demand and released on exit."""

    def __init__(
        self,
        mountpoint: Path | None = None,
        *,
        device_name: str | None = None,
        mount_timeout: float = DEFAULT_MOUNT_TIMEOUT,
        release_gvfs: bool = True,
        reset_device: bool = False,
    ) -> None:
        self.mountpoint = mountpoint or _default_mountpoint()
        self.device_name = device_name
        self.mount_timeout = mount_timeout
        self.release_gvfs = release_gvfs
        self.reset_device = reset_device
        self.log_path = self.mountpoint.with_suffix(".log")
        self._process: subprocess.Popen[bytes] | None = None
        self._created_mountpoint = False
        self._adopted = False

    def is_mounted(self) -> bool:
        """Tell whether something is mounted on the mount point."""
        return is_mounted(self.mountpoint)

    def mount(self) -> None:
        """Mount the phone, or adopt the mount already in place."""
        mounted_type = filesystem_type(self.mountpoint)
        if mounted_type == MTP_MOUNT_FILESYSTEM_TYPE:
            # Another run left the phone mounted: adopt it, and leave it alone
            # when this run ends.
            self._adopted = True
            return
        if mounted_type is not None:
            raise MtpMountError(
                f"{self.mountpoint} is mounted as {mounted_type}, expected "
                f"{MTP_MOUNT_FILESYSTEM_TYPE}; unmount it first"
            )
        executable = shutil.which(MOUNT_BINARY)
        if executable is None:
            raise MtpMountError(f"{MOUNT_BINARY} is not installed (package android-file-transfer)")
        if self.release_gvfs:
            release_gvfs_mtp()
        self._create_mountpoint()
        command = [executable]
        if self.device_name:
            command.extend(["-D", self.device_name])
        if self.reset_device:
            command.append("-R")
        command.append(str(self.mountpoint))
        with self.log_path.open("wb") as log_handle:
            self._process = subprocess.Popen(
                command,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._wait_until_mounted()

    def unmount(self) -> None:
        """Release the file system and stop the helper process."""
        if self._adopted:
            return
        if self.is_mounted():
            last_error = "no fusermount binary found"
            for command in (
                ["fusermount3", "-u", str(self.mountpoint)],
                ["fusermount", "-u", str(self.mountpoint)],
                ["fusermount3", "-uz", str(self.mountpoint)],
                ["fusermount", "-uz", str(self.mountpoint)],
            ):
                if shutil.which(command[0]) is None:
                    continue
                completed = subprocess.run(command, capture_output=True, text=True, check=False)
                if not self.is_mounted():
                    break
                last_error = completed.stderr.strip() or completed.stdout.strip()
            else:
                raise MtpMountError(f"could not unmount {self.mountpoint}: {last_error}")
        self._stop_process()
        if self._created_mountpoint:
            with contextlib.suppress(OSError):
                self.mountpoint.rmdir()

    def volumes(self) -> list[StorageVolume]:
        """Return the storage volumes exposed by the mounted phone."""
        if not self.is_mounted():
            raise MtpMountError(f"{self.mountpoint} is not mounted")
        volumes = []
        try:
            for entry in sorted(os.scandir(self.mountpoint), key=lambda entry: entry.name):
                if entry.is_dir(follow_symlinks=False):
                    volumes.append(StorageVolume(path=Path(entry.path), label=entry.name))
        except OSError as error:
            raise MtpMountError(f"cannot list {self.mountpoint}: {error}") from error
        if not volumes:
            raise DeviceNotFoundError(
                "the phone is mounted but exposes no storage volume; unlock it and "
                "accept the 'file transfer' USB mode, then retry"
            )
        return volumes

    def mount_log_tail(self, lines: int = 12) -> str:
        """Return the last lines of the helper log, for error messages."""
        try:
            content = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(content.splitlines()[-lines:])

    def __enter__(self) -> "MtpMount":
        self.mount()
        return self

    def __exit__(self, *unused: object) -> None:
        self.unmount()

    def _create_mountpoint(self) -> None:
        if self.mountpoint.exists():
            return
        self.mountpoint.mkdir(parents=True, exist_ok=True)
        self._created_mountpoint = True

    def _wait_until_mounted(self) -> None:
        deadline = time.monotonic() + self.mount_timeout
        while time.monotonic() < deadline:
            if self.is_mounted():
                if self._process is not None and self._process.poll() is not None:
                    # The helper exits as soon as the file system is up.
                    self._process = None
                return
            if self._process is not None and self._process.poll() is not None:
                exit_code = self._process.returncode
                self._process = None
                raise self._mount_failure(exit_code)
            time.sleep(0.2)
        self._stop_process()
        raise MtpMountError(
            f"{MOUNT_BINARY} did not mount {self.mountpoint} within {self.mount_timeout:.0f}s"
        )

    def _mount_failure(self, exit_code: int | None) -> MtpMountError:
        detail = self.mount_log_tail()
        devices = self._safe_device_list()
        message = f"{MOUNT_BINARY} exited with code {exit_code}"
        if detail:
            message += f":\n{detail}"
        if devices:
            message += "\nMTP devices currently visible: " + ", ".join(devices)
        else:
            message += (
                "\nNo MTP device is visible: plug the phone, unlock it and choose "
                "the 'file transfer' USB mode"
            )
        return MtpMountError(message)

    def _safe_device_list(self) -> list[str]:
        try:
            return list_mtp_devices()
        except MtpMountError:
            return []

    def _stop_process(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            self._process.send_signal(signal.SIGTERM)
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        self._process = None


def _default_mountpoint() -> Path:
    """Return a private mount point under the per-user runtime directory."""
    runtime_directory = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
    candidate = runtime_directory / "dcim" / "mtp"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(tempfile.mkdtemp(prefix="dcim-mtp-"))
    return candidate
