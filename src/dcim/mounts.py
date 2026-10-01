"""Reading the kernel mount table.

``/proc/self/mounts`` is the single source of truth used to decide whether a
path is already mounted. It is preferred over parsing ``mount(8)`` output
because it is stable, cheap and not localized.
"""

from pathlib import Path

MOUNT_TABLE_PATH = Path("/proc/self/mounts")

_ESCAPES = {
    "\\040": " ",
    "\\011": "\t",
    "\\012": "\n",
    "\\134": "\\",
}


def _unescape(field: str) -> str:
    for escaped, character in _ESCAPES.items():
        field = field.replace(escaped, character)
    return field


def mounted_filesystems() -> dict[Path, str]:
    """Return a mapping of mount point to file system type."""
    table: dict[Path, str] = {}
    with MOUNT_TABLE_PATH.open(encoding="utf-8") as lines:
        for line in lines:
            fields = line.split()
            if len(fields) < 3:
                continue
            table[Path(_unescape(fields[1]))] = fields[2]
    return table


def filesystem_type(path: Path) -> str | None:
    """Return the file system type mounted on `path`, or None if not mounted."""
    return mounted_filesystems().get(path)


def is_mounted(path: Path) -> bool:
    """Tell whether `path` is a mount point."""
    return filesystem_type(path) is not None
