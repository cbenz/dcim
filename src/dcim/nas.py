"""The destination transport: a CIFS share declared in ``/etc/fstab``.

The mount point is mounted on demand and released at the end of the run, but
only when this program mounted it: a share that was already mounted (by the
system, or by the user) is left alone.

Mounting a CIFS share declared in ``/etc/fstab`` requires root, since the
credentials file is root-only. The program therefore tries, in order, a plain
``mount`` (which works when the fstab entry carries the ``user`` option), then
``sudo -n``, and finally explains how to fix the system when neither works.
"""

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import NasMountError
from .mounts import is_mounted

MOUNT_TIMEOUT = 60.0


@dataclass(frozen=True)
class NasMount:
    """The result of ensuring that a CIFS destination is available."""

    mountpoint: Path
    mounted_by_us: bool


def systemd_unit_name(mountpoint: Path, kind: str = "mount") -> str:
    """Return the systemd unit name generated from the fstab entry."""
    escaped = str(mountpoint).replace("-", "\\x2d").replace("/", "-").lstrip("-")
    return f"{escaped}.{kind}"


def ensure_mounted(mountpoint: Path) -> NasMount:
    """Mount `mountpoint` if needed and report whether we did it ourselves."""
    if is_mounted(mountpoint):
        return NasMount(mountpoint=mountpoint, mounted_by_us=False)
    if not mountpoint.is_dir():
        raise NasMountError(f"the destination mount point {mountpoint} does not exist")
    failures: list[str] = []
    for command in _mount_commands(mountpoint):
        completed = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=MOUNT_TIMEOUT
        )
        if is_mounted(mountpoint):
            return NasMount(mountpoint=mountpoint, mounted_by_us=True)
        failures.append(
            f"$ {' '.join(command)}\n{completed.stderr.strip() or completed.stdout.strip()}"
        )
    raise NasMountError(
        f"could not mount {mountpoint}:\n"
        + "\n".join(failures)
        + "\n\nFix it once for all with either of:\n"
        f"  sudo systemctl enable --now '{systemd_unit_name(mountpoint, 'automount')}'\n"
        f"  sudo mount {mountpoint}\n"
        "  (adding 'user' to the fstab options also allows mounting without sudo)"
    )


def release(mount: NasMount) -> None:
    """Unmount the destination if this program mounted it."""
    if not mount.mounted_by_us or not is_mounted(mount.mountpoint):
        return
    for command in (
        ["umount", str(mount.mountpoint)],
        ["sudo", "-n", "umount", str(mount.mountpoint)],
        ["sudo", "-n", "systemctl", "stop", systemd_unit_name(mount.mountpoint)],
    ):
        if shutil.which(command[0]) is None:
            continue
        subprocess.run(command, capture_output=True, text=True, check=False, timeout=MOUNT_TIMEOUT)
        if not is_mounted(mount.mountpoint):
            return


def _mount_commands(mountpoint: Path) -> list[list[str]]:
    commands = [["mount", str(mountpoint)]]
    if shutil.which("sudo") is not None:
        commands.append(["sudo", "-n", "mount", str(mountpoint)])
        commands.append(["sudo", "-n", "systemctl", "start", systemd_unit_name(mountpoint)])
    return commands
