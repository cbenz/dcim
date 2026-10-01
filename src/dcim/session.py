"""The device session: which transport, which storage, which directory.

Both commands need the same three things before they can do anything: a
destination mounted, a device transport open, and a source directory and glob
pattern decided. Doing it once here keeps the lifecycle in a single place, with a
single ``finally`` — the guarantee that a run never leaves the phone mounted
behind it.

The transport is chosen once, for the whole run: `adb` when USB debugging makes
it available (106 MB/s reading, ~4.6 ms per deletion on the reference device),
`mtp` otherwise (7.3 MB/s, ~51 ms per deletion). They are never mixed: the mount
caches directory listings and would report a stale view of what adb changed.
"""

import os
from dataclasses import dataclass

from rich.console import Console

from .config import Config
from .errors import AndroidDcimSyncError, MtpMountError, SourceSelectionError
from .mtp import MtpMount
from .nas import NasMount, ensure_mounted, release
from .selection import parse_patterns
from .transport import AdbTransport, DeviceTransport, MtpTransport, adb_serial
from .ui import ask_value, is_interactive, section

TRANSPORT_CHOICES = ("auto", "adb", "mtp")


@dataclass(frozen=True)
class PhoneSource:
    """Where to read from: a storage, a directory, and glob patterns."""

    label: str
    root: str
    directory: str
    patterns: tuple[str, ...]

    @property
    def device_directory(self) -> str:
        """Return the directory path as the transport sees it."""
        return self.directory.lstrip("/")


class PhoneSession:
    """Opens a device transport and the destination, and releases both on exit.

    The destination is mounted first so that a destination problem never leaves
    the phone mounted. A mount that already existed before the run is adopted and
    left in place; only mounts created here are released.
    """

    def __init__(
        self,
        config: Config,
        console: Console,
        *,
        transport_name: str = "auto",
        mount_destination: bool = True,
        keep_mounted: bool = False,
    ) -> None:
        self.config = config
        self.console = console
        self.transport_name = transport_name
        self.mount_destination = mount_destination
        self.keep_mounted = keep_mounted
        self.transport: DeviceTransport | None = None
        self.mtp_mount: MtpMount | None = None
        self.destination_mount: NasMount | None = None
        self.volumes: list[tuple[str, str]] = []

    def __enter__(self) -> "PhoneSession":
        self.open()
        return self

    def __exit__(self, *unused: object) -> None:
        self.release()

    def open(self) -> DeviceTransport:
        """Mount the destination, then open a transport to the device."""
        if self.mount_destination:
            section(self.console, "Destination")
            self.destination_mount = ensure_mounted(self.config.nas_mountpoint)
            self.console.print(
                f"{self.config.nas_mountpoint}: "
                + (
                    "mounted by this run"
                    if self.destination_mount.mounted_by_us
                    else "already mounted"
                )
            )
        section(self.console, "Phone")
        self.transport = self._open_transport()
        self.console.print(f"[dim]{self.transport.describe()}[/dim]")
        self.volumes = self.transport.volumes()
        return self.transport

    def _open_transport(self) -> DeviceTransport:
        """Return the transport asked for, or the best one available."""
        if self.transport_name in ("auto", "adb"):
            serial = adb_serial()
            if serial is not None:
                self._warn_about_a_stale_mount()
                return AdbTransport(serial)
            if self.transport_name == "adb":
                raise AndroidDcimSyncError(
                    "no adb device is authorized: enable USB debugging on the phone "
                    "(Developer options), accept the computer's key on the phone, "
                    "and check `adb devices`"
                )
            self.console.print(
                "[dim]adb is not available, falling back to the MTP mount "
                "(--transport adb to require it)[/dim]"
            )
        self.mtp_mount = MtpMount(
            device_name=self.config.device_name, release_gvfs=self.config.release_gvfs
        )
        self.mtp_mount.mount()
        return MtpTransport(self.mtp_mount.mountpoint)

    def _warn_about_a_stale_mount(self) -> None:
        """Warn when a mount from an earlier run would show a stale device.

        The mount caches its directory listings and does not see what adb changes:
        leaving it in place is harmless for this run, but browsing it afterwards
        would lie.
        """
        stale = MtpMount()
        if not stale.is_mounted():
            return
        self.console.print(
            f"[yellow]A mount from an earlier run is still in place at "
            f"{stale.mountpoint}: this run uses adb, and that mount does not see "
            f"what adb changes (release it with `fusermount3 -u {stale.mountpoint}`)."
            "[/yellow]"
        )

    def assert_open(self) -> DeviceTransport:
        """Return the transport, complaining if the session was not opened."""
        if self.transport is None:
            raise AndroidDcimSyncError("the device session is not open")
        return self.transport

    def release(self) -> None:
        """Release the device, then the destination."""
        if self.keep_mounted:
            self.console.print("[dim]Left mounted: the destination (the device is not)[/dim]")
            return
        if self.mtp_mount is not None:
            try:
                self.mtp_mount.unmount()
            except (MtpMountError, OSError) as error:
                self.console.print(f"[yellow]Could not unmount the phone: {error}[/yellow]")
        if self.destination_mount is not None:
            release(self.destination_mount)

    def select_source(
        self,
        *,
        source_option: str | None,
        pattern_option: str | None,
        prompt: bool = True,
    ) -> PhoneSource:
        """Return the storage, directory and patterns to read from.

        `--prompt` is what separates a guided run from a scriptable one: the two
        commands ask nothing by default, and read the configuration file.
        """
        label, root = self.choose_volume(prompt=prompt)
        transport = self.assert_open().with_root(root)
        self.transport = transport
        directory = self.resolve_directory(label, transport, source_option, prompt=prompt)
        patterns = self.resolve_patterns(pattern_option, prompt=prompt)
        return PhoneSource(label=label, root=root, directory=directory, patterns=patterns)

    def choose_volume(self, *, prompt: bool = True) -> tuple[str, str]:
        """Return the storage to read from, as ``(label, root)``."""
        if len(self.volumes) == 1:
            self.console.print(f"[dim]Storage: {self.volumes[0][0]}[/dim]")
            return self.volumes[0]
        if not prompt:
            raise SourceSelectionError(
                "several storages are exposed ("
                + ", ".join(label for label, _root in self.volumes)
                + "); run with --prompt to choose one"
            )
        for index, (label, _root) in enumerate(self.volumes, start=1):
            self.console.print(f"[dim]{index}. {label}[/dim]")
        answer = ask_value(self.console, "Storage to use", "1")
        if answer.isdigit() and 1 <= int(answer) <= len(self.volumes):
            return self.volumes[int(answer) - 1]
        for label, root in self.volumes:
            if label == answer:
                return label, root
        raise SourceSelectionError(f"unknown storage: {answer!r}")

    def resolve_directory(
        self,
        label: str,
        transport: DeviceTransport,
        source_option: str | None,
        *,
        prompt: bool = True,
    ) -> str:
        """Return the device directory to read, relative to the storage root."""
        if source_option is not None or not prompt:
            candidate = os.path.normpath(
                (source_option or self.config.source_directory).strip("/")
            ).lstrip("/")
            if not transport.directory_exists(candidate):
                raise SourceSelectionError(
                    f"{candidate} does not exist on the phone; use --source to name "
                    "another directory"
                )
            return candidate
        listings = [entry.name for entry in transport.list_directory("") if entry.is_directory]
        self.console.print(f"[dim]Storage « {label} »[/dim]")
        if listings:
            self.console.print("[dim]Available directories: " + ", ".join(listings[:40]) + "[/dim]")
        while True:
            answer = ask_value(
                self.console,
                f"Source directory on the phone (inside « {label} »)",
                self.config.source_directory,
            ).strip("/")
            candidate = os.path.normpath(answer).lstrip("/")
            if not candidate:
                raise SourceSelectionError("the source directory cannot be empty")
            if transport.directory_exists(candidate):
                return candidate
            self.console.print(f"[red]{candidate} does not exist on the phone.[/red]")
            if not is_interactive(self.console):
                raise SourceSelectionError(
                    f"{candidate} does not exist on the phone; use --source to name "
                    "an existing directory"
                )

    def resolve_patterns(
        self, pattern_option: str | None, *, prompt: bool = True
    ) -> tuple[str, ...]:
        """Return the glob patterns to use, asking for them only when asked to."""
        if pattern_option is not None:
            return parse_patterns(self.config.file_pattern)
        if prompt:
            self.console.print(
                "[dim]The pattern selects the files of this run: matched against file "
                "names, or against the path relative to the source directory when it "
                "contains '/'. Several patterns can be separated by commas, and Enter "
                "keeps every photo and video.[/dim]"
            )
            answer = ask_value(self.console, "Glob pattern", self.config.file_pattern)
            return parse_patterns(answer)
        return parse_patterns(self.config.file_pattern)
