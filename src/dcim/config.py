"""Persisted defaults, so that a repeated run only needs the Enter key.

The file is ``~/.config/dcim/config.toml`` and holds the answers
given interactively (source directory, glob pattern, destination). It is
written by ``--save-defaults`` and never modified behind the user's back.
"""

import argparse
import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import AndroidDcimSyncError
from .selection import DEFAULT_EXCLUDE_PATTERNS, parse_patterns

if TYPE_CHECKING:
    from .transfer import ConflictPolicy
    from .verify import VerifyMode

CONFIG_ENVIRONMENT_VARIABLE = "DCIM_CONFIG"
DEFAULT_DESTINATION_ROOT = Path("/mnt/nas-photos/Photos")
DEFAULT_NAS_MOUNTPOINT = Path("/mnt/nas-photos")
DEFAULT_SOURCE_DIRECTORY = "DCIM/Camera"
DEFAULT_FILE_PATTERN = "*"
DEFAULT_MONTHS = 12


@dataclass
class Config:
    """The persisted settings of the program."""

    destination_root: Path = DEFAULT_DESTINATION_ROOT
    nas_mountpoint: Path = DEFAULT_NAS_MOUNTPOINT
    mount_nas: bool = True
    source_directory: str = DEFAULT_SOURCE_DIRECTORY
    file_pattern: str = DEFAULT_FILE_PATTERN
    media_only: bool = True
    recursive: bool = True
    exclude_patterns: tuple[str, ...] = field(default_factory=lambda: DEFAULT_EXCLUDE_PATTERNS)
    device_name: str | None = None
    months: int = DEFAULT_MONTHS
    verify_mode: "VerifyMode" = "partial"
    conflict_policy: "ConflictPolicy" = "skip"
    preserve_tree: bool = False
    preserve_times: bool = True
    release_gvfs: bool = True
    keep_months: int = 0
    check_destination: bool = True
    phone_capacity: str | None = None
    transport: str = "auto"


def config_path() -> Path:
    """Return the path of the configuration file."""
    override = os.environ.get(CONFIG_ENVIRONMENT_VARIABLE)
    if override:
        return Path(override).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "dcim" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    """Read the configuration file, falling back to the built-in defaults."""
    resolved_path = path or config_path()
    if not resolved_path.is_file():
        return Config()
    with resolved_path.open("rb") as handle:
        try:
            raw = tomllib.load(handle)
        except tomllib.TOMLDecodeError as error:
            raise AndroidDcimSyncError(f"{resolved_path} is not valid TOML: {error}") from error
    return config_from_mapping(raw)


def config_from_mapping(raw: dict) -> Config:
    """Build a Config from a decoded TOML table, ignoring unknown keys."""
    known = {config_field.name for config_field in fields(Config)}
    unknown = set(raw) - known
    if unknown:
        raise AndroidDcimSyncError(f"unknown configuration keys: {', '.join(sorted(unknown))}")
    values = dict(raw)
    for key in ("destination_root", "nas_mountpoint"):
        if key in values:
            values[key] = Path(values[key]).expanduser()
    if "exclude_patterns" in values:
        values["exclude_patterns"] = tuple(values["exclude_patterns"])
    return Config(**values)


def save_config(config: Config, path: Path | None = None) -> Path:
    """Write the configuration file and return its path."""
    resolved_path = path or config_path()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(render_toml(config), encoding="utf-8")
    return resolved_path


def render_toml(config: Config) -> str:
    """Serialize a Config as TOML, one flat key per setting."""
    lines = ["# Written by dcim --save-defaults", ""]
    for key, value in asdict(config).items():
        if value is None:
            continue
        lines.append(f"{key} = {_render_value(value)}")
    return "\n".join(lines) + "\n"


def _render_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Path):
        return f'"{value}"'
    if isinstance(value, (list, tuple)):
        rendered = ", ".join(f'"{element}"' for element in value)
        return f"[{rendered}]"
    return f'"{value}"'


def apply_overrides(config: Config, options: argparse.Namespace) -> None:
    """Let the command line win over the configuration file.

    Only non-destructive settings are accepted here: flags that delete data
    (``--delete``) or that change how existing files are treated
    (``--on-conflict``) are handled by the command itself, and are never read
    from the configuration file.
    """
    # Every flag is read with `getattr` because the two commands do not accept
    # the same ones, and `apply_overrides` is shared.
    if getattr(options, "dest", None) is not None:
        config.destination_root = options.dest.expanduser()
    if getattr(options, "nas_mountpoint", None) is not None:
        config.nas_mountpoint = options.nas_mountpoint.expanduser()
    if getattr(options, "mount_nas", None) is not None:
        config.mount_nas = options.mount_nas
    if getattr(options, "source", None) is not None:
        config.source_directory = options.source
    if getattr(options, "pattern", None) is not None:
        config.file_pattern = options.pattern
    if getattr(options, "media_only", None) is not None:
        config.media_only = options.media_only
    if getattr(options, "recursive", None) is not None:
        config.recursive = options.recursive
    if getattr(options, "exclude", None):
        config.exclude_patterns = parse_patterns(",".join(options.exclude))
    if getattr(options, "device", None) is not None:
        config.device_name = options.device
    if getattr(options, "months", None) is not None:
        config.months = options.months
    if getattr(options, "verify", None) is not None:
        config.verify_mode = options.verify
    if getattr(options, "on_conflict", None) is not None:
        config.conflict_policy = options.on_conflict
    if getattr(options, "preserve_tree", False):
        config.preserve_tree = True
    if getattr(options, "no_preserve_times", False):
        config.preserve_times = False
    if getattr(options, "no_release_gvfs", False):
        config.release_gvfs = False
    if getattr(options, "check_destination", None) is not None:
        config.check_destination = options.check_destination
    if getattr(options, "transport", None) is not None:
        config.transport = options.transport
