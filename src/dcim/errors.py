"""Error hierarchy of dcim.

Every expected failure mode raises a subclass of :class:`AndroidDcimSyncError`,
so that the CLI can turn it into a readable message instead of a traceback.
"""


class AndroidDcimSyncError(RuntimeError):
    """Base class of every error raised on purpose by this project."""


class DeviceNotFoundError(AndroidDcimSyncError):
    """No Android device exposes an MTP interface over USB."""


class MtpMountError(AndroidDcimSyncError):
    """The MTP mount could not be established or released."""


class NasMountError(AndroidDcimSyncError):
    """The CIFS destination could not be mounted or unmounted."""


class SourceSelectionError(AndroidDcimSyncError):
    """The source directory, the glob pattern or the date range is unusable."""


class TransferError(AndroidDcimSyncError):
    """A file could not be copied or verified."""


class DeletionError(AndroidDcimSyncError):
    """An original file could not be deleted from the phone."""


class TransportError(AndroidDcimSyncError):
    """A device transport could not list, read or delete a file."""
