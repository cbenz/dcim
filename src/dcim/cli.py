"""The ``dcim`` command line: two sub-commands, one parser, no logic.

``dcim status`` reads (histogram of the phone, suggestion of what to sync next)
and ``dcim sync`` writes (copy, verify, and optionally delete). Splitting the
parser from the commands keeps this module small enough to read in one go, and
keeps each command's safety rules in its own module.
"""

import argparse
from pathlib import Path

from . import __version__
from .config import DEFAULT_MONTHS
from .errors import AndroidDcimSyncError
from .mtp import list_mtp_devices
from .session import TRANSPORT_CHOICES
from .status import run_status
from .sync import run_sync
from .transfer import ConflictPolicy
from .ui import build_console
from .verify import VerifyMode

VERIFY_MODES: tuple[VerifyMode, ...] = ("partial", "full", "none")
CONFLICT_POLICIES: tuple[ConflictPolicy, ...] = ("skip", "overwrite", "rename")


def main(argv: list[str] | None = None) -> int:
    """Parse the arguments, run the command and map errors to exit codes."""
    parser = build_argument_parser()
    options = parser.parse_args(argv)
    console = build_console(stderr=bool(getattr(options, "json", False)))
    if options.list_devices:
        return run_list_devices(options, console)
    command_handler = getattr(options, "command_handler", None)
    if command_handler is None:
        parser.print_help()
        return 2
    # Nothing is asked by default: the configuration file, the flags and the
    # built-in defaults already answer every question, and asking again for what
    # they say is noise. What remains is the two confirmations (the copy, the
    # deletion), which are decisions, not settings. `--prompt` restores the
    # guided questions for discovering another directory interactively.
    # `set_defaults` cannot be used for this: the shared parser's action default
    # wins over it when the sub-parser fills the namespace.
    if getattr(options, "prompt", None) is None:
        options.prompt = False
    try:
        return command_handler(options, console)
    except AndroidDcimSyncError as error:
        console.print(f"[bold red]Error:[/bold red] {error}")
        return 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by the user.[/yellow]")
        return 130
    except EOFError:
        # Ctrl+D at a prompt: an interruption, not a crash.
        console.print("\n[yellow]End of input: interrupted.[/yellow]")
        return 130


def run_list_devices(options: argparse.Namespace, console) -> int:
    """Print the MTP devices visible to android-file-transfer."""
    del options
    devices = list_mtp_devices()
    if not devices:
        console.print("[yellow]No MTP device found.[/yellow]")
        return 1
    for device in devices:
        console.print(device)
    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    """Describe the command line interface."""
    parser = argparse.ArgumentParser(
        prog="dcim",
        description=(
            "Offload photos and videos from an MTP device (phone, camera) to a "
            "destination such as a NAS share, verifying every copy before the "
            "originals are deleted."
        ),
        epilog=(
            "Examples:\n"
            "  dcim status                      what is on the phone, and what to sync next\n"
            "  dcim sync --from 2025-09 --to 2026-08            copy and verify a range\n"
            "  dcim sync --from 2025-09 --to 2026-08 --delete   ... and free the phone\n"
            "\n"
            "Settings fall back on ~/.config/dcim/config.toml (see --save-defaults).\n"
            "--delete is never read from that file: removing the originals is an\n"
            "explicit decision, taken again on every run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="list the MTP devices android-file-transfer can see, then exit",
    )
    subparsers = parser.add_subparsers(title="commands", dest="command", metavar="COMMAND")

    shared = shared_parser()
    status = subparsers.add_parser(
        "status",
        aliases=["st"],
        parents=[shared],
        help="show what is on the phone and suggest a date range to sync",
        description=(
            "Read only. Mounts the phone, reads the capture date of every matching "
            "file, prints a histogram by month, and suggests the range to hand to "
            "'dcim sync'. Nothing is copied, nothing is deleted, and the destination "
            "directory is never created."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    status.set_defaults(command_handler=run_status)
    status.add_argument(
        "--keep-months",
        type=int,
        help="months to keep on the phone, starting from the most recent (default: 0)",
    )
    status.add_argument(
        "--target-size",
        metavar="SIZE",
        help="suggest the oldest months until this size is reached, e.g. 30G",
    )
    status.add_argument(
        "--check-destination",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "read the destination to tell what is already copied; it is never "
            "mounted, and an unavailable destination is reported, not fatal"
        ),
    )
    status.add_argument(
        "--phone-capacity",
        metavar="SIZE",
        help=(
            "total size of the phone storage, for devices whose MTP mount does "
            "not report it, e.g. 128G"
        ),
    )
    status.add_argument(
        "--destination-depth",
        type=depth_argument,
        default=0,
        metavar="N|all",
        help=(
            "how deep to look into the destination for already copied files "
            "(default: 0, the destination root only; 'all' to read everything)"
        ),
    )
    status.add_argument("--json", action="store_true", help="print the report as JSON")

    sync = subparsers.add_parser(
        "sync",
        parents=[shared],
        help="copy a date range to the destination, verify it, and keep the originals",
        description=(
            "Copies every file of the date range, verifies each copy against the "
            "destination, and keeps the originals. With --delete, the originals "
            "whose copy was verified are then deleted from the phone, after an "
            "explicit confirmation.\n\n"
            "Nothing is asked: the source directory, the glob pattern and the date "
            "range come from the flags, the configuration file, or their defaults "
            "(the last complete months), and are printed before the copy."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sync.set_defaults(command_handler=run_sync)
    sync.add_argument(
        "--mount-nas",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="mount the destination when it is not mounted",
    )
    sync.add_argument("--from", dest="from_date", help="start of the capture date range")
    sync.add_argument(
        "--to", dest="to_date", help="end of the capture date range (the whole month is included)"
    )
    sync.add_argument(
        "--months",
        type=int,
        help=f"complete months covered when no range is given (default: {DEFAULT_MONTHS})",
    )
    sync.add_argument(
        "--verify", choices=VERIFY_MODES, help="verification level (default: partial)"
    )
    sync.add_argument(
        "--on-conflict",
        choices=CONFLICT_POLICIES,
        help="what to do when a different file already exists (default: skip)",
    )
    sync.add_argument(
        "--preserve-tree", action="store_true", help="keep the source sub-directories"
    )
    sync.add_argument(
        "--no-preserve-times", action="store_true", help="do not copy the modification times"
    )
    sync.add_argument("--limit", type=int, help="only process the N oldest matching files")
    sync.add_argument("--dry-run", action="store_true", help="show the plan and stop")
    sync.add_argument(
        "--delete",
        action="store_true",
        help="delete the originals from the phone once their copy is verified",
    )
    sync.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sync.add_argument(
        "--resume", type=Path, help="replay the deletion phase of an interrupted run manifest"
    )
    return parser


def shared_parser() -> argparse.ArgumentParser:
    """Return the options both commands accept."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--config", type=Path, help="configuration file to read (and to write with --save-defaults)"
    )
    parser.add_argument("--save-defaults", action="store_true", help="persist these answers")
    parser.add_argument("--source", help="source directory on the phone, e.g. DCIM/Camera")
    parser.add_argument("--pattern", help="glob pattern, e.g. '*.jpg,*.mp4'")
    parser.add_argument("--exclude", action="append", help="glob pattern to exclude")
    parser.add_argument("--device", help="MTP device name or serial to use")
    parser.add_argument(
        "--transport",
        choices=TRANSPORT_CHOICES,
        help=(
            "how to reach the device: adb when USB debugging is on (about 15x "
            "faster), mtp through the mount otherwise, auto to pick (default: auto)"
        ),
    )
    parser.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="recurse into sub-directories",
    )
    parser.add_argument(
        "--media-only",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="keep only known photo and video extensions",
    )
    parser.add_argument(
        "--dest", type=Path, help="destination directory (default: from the configuration file)"
    )
    parser.add_argument("--nas-mountpoint", type=Path, help="CIFS mount point of the destination")
    parser.add_argument(
        "--keep-mounted", action="store_true", help="leave the mounts in place at the end"
    )
    parser.add_argument(
        "--no-release-gvfs", action="store_true", help="do not stop gvfsd-mtp before mounting"
    )
    parser.add_argument("--quiet", action="store_true", help="hide the progress display")
    parser.add_argument(
        "--prompt",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "ask for the source directory, the glob pattern and the date range, "
            "instead of taking them from the flags and the configuration file"
        ),
    )
    return parser


def depth_argument(text: str) -> int | None:
    """Parse ``--destination-depth``: a number of levels, or ``all``."""
    if text.strip().lower() in ("all", "unlimited", "-1"):
        return None
    try:
        depth = int(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"expected a number or 'all', got {text!r}") from error
    if depth < 0:
        raise argparse.ArgumentTypeError("the depth cannot be negative")
    return depth
