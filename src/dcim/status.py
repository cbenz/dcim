"""The ``dcim status`` command: what is on the phone, and what to sync next.

Read-only by construction: it mounts, scans, compares with the destination and
prints a histogram and a suggested range. It never writes to the destination,
never deletes anything on the phone, and never creates the destination
directory — the only state it can change is mounting and unmounting.

It is also the one command that asks nothing by default: what it reports comes
from the configuration file, and the effective filter is printed instead of being
asked for. ``--prompt`` restores the guided questions when exploring another
directory interactively.
"""

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from rich.console import Console

from .analysis import (
    DestinationIndex,
    Histogram,
    StorageSpace,
    Suggestion,
    build_destination_index,
    build_histogram,
    kept_months_after,
    parse_size,
    suggest_range,
    with_capacity_override,
)
from .config import Config, apply_overrides, load_config, save_config
from .formatting import format_bytes, format_count
from .mounts import is_mounted
from .selection import MediaFile, build_summary, iter_media_files
from .session import PhoneSession, PhoneSource
from .ui import render_histogram, render_space, render_suggestion, section, working


def run_status(options: argparse.Namespace, console: Console) -> int:
    """Report what the phone holds, and suggest the next sync range."""
    config = load_config(options.config)
    apply_overrides(config, options)
    if options.keep_months is not None:
        config.keep_months = options.keep_months
    if options.save_defaults:
        console.print(f"[green]Defaults saved to {save_config(config, options.config)}[/green]")
    destination_root = config.destination_root.expanduser()
    target_size = parse_size(options.target_size) if options.target_size else None
    capacity_text = options.phone_capacity or config.phone_capacity
    capacity = parse_size(capacity_text) if capacity_text else None
    # `status` never mounts anything on the destination side: it reads what is
    # already available, and says so when it is not. Reading the device is the
    # only thing this command depends on.
    readiness = destination_readiness(config, destination_root)
    compare_destination = config.check_destination and readiness.available

    with PhoneSession(
        config,
        console,
        transport_name=config.transport,
        mount_destination=False,
        keep_mounted=options.keep_mounted,
    ) as session:
        source = session.select_source(
            source_option=options.source,
            pattern_option=options.pattern,
            prompt=options.prompt,
        )
        transport = session.assert_open()
        console.print(f"[dim]Filter: {describe_filter(source, config)}[/dim]")
        with working(console, f"Reading {source.directory} on the phone…"):
            media_files = scan_everything(
                transport, source.device_directory, config=config, patterns=source.patterns
            )
        metadata = build_summary(media_files)
        console.print(
            f"[dim]{format_count(metadata.count, 'file')}, "
            f"{format_bytes(metadata.total_size)}; dates read "
            + ", ".join(f"{label}: {count}" for label, count in metadata.date_sources)
            + "[/dim]"
        )
        if compare_destination:
            destination_index = read_destination(
                console,
                destination_root,
                wanted_names={media_file.filename for media_file in media_files},
                max_depth=options.destination_depth,
            )
            print_destination_hint(console, destination_index)
        else:
            # Say it where the reader expects the destination, not at the end.
            section(console, "Destination")
            console.print(f"[dim]{readiness.reason}[/dim]")
            destination_index = None
        histogram = build_histogram(media_files, destination_index)
        suggestion = suggest_range(
            histogram, keep_months=config.keep_months, target_size=target_size
        )
        suggested_range = (
            (suggestion.start_month, suggestion.end_month) if suggestion is not None else None
        )
        current_month = f"{date.today():%Y-%m}"
        kept_months = kept_months_after(suggestion, histogram, current_month=current_month)
        months_with_files = [month.month for month in histogram.months]
        kept_months = [month for month in kept_months if month in months_with_files]
        space: StorageSpace | None = with_capacity_override(transport.storage_space(""), capacity)
        if options.json:
            # Raw JSON on stdout: no colour, no wrapping, nothing else.
            print(
                json.dumps(
                    build_report(
                        histogram=histogram,
                        suggestion=suggestion,
                        source_directory=f"{source.root}/{source.directory}",
                        destination_root=str(destination_root),
                        compared=compare_destination,
                    ),
                    ensure_ascii=False,
                )
            )
            return 0
        render_space(
            console,
            space=space,
            source_label=source.directory,
            source_size=metadata.total_size,
            source_files=metadata.count,
            freed=suggestion.total_size if suggestion is not None else 0,
            freed_files=suggestion.file_count if suggestion is not None else 0,
            suggested_range=suggested_range,
            to_transfer=(
                (histogram.missing_count, histogram.missing_size) if compare_destination else None
            ),
        )
        section(console, "Histogram")
        render_histogram(
            console,
            histogram,
            with_destination=compare_destination,
            suggested_range=suggested_range,
            has_months_outside_the_range=bool(kept_months),
        )
        render_suggestion(
            console,
            suggestion,
            kept_months=kept_months,
            current_month=current_month,
        )
    return 0


@dataclass(frozen=True)
class DestinationReadiness:
    """Whether the destination can be read right now, and why not."""

    available: bool
    reason: str


def destination_readiness(config: Config, destination_root: Path) -> DestinationReadiness:
    """Tell whether the destination can be read, without mounting it.

    Three situations: the directory is missing; the directory is a mount point of
    a share that is not mounted (an empty directory, which would wrongly report
    every file as missing); or it is readable. Only the last one is compared.
    """
    if not config.check_destination:
        return DestinationReadiness(
            available=False,
            reason="--no-check-destination: the destination was not inspected, so nothing "
            "is said about what is already copied.",
        )
    if not destination_root.is_dir():
        return DestinationReadiness(
            available=False,
            reason=f"{destination_root} does not exist: nothing is said about already "
            "copied files.",
        )
    if destination_root.is_relative_to(config.nas_mountpoint) and not is_mounted(
        config.nas_mountpoint
    ):
        return DestinationReadiness(
            available=False,
            reason=f"{config.nas_mountpoint} is not mounted (status never mounts it): "
            "nothing is said about already copied files.",
        )
    return DestinationReadiness(available=True, reason="")


def describe_filter(source: PhoneSource, config: Config) -> str:
    """Describe what the file set of this run actually covers, in one line."""
    parts = [
        f"{', '.join(source.patterns)} in {source.directory}",
        "photos and videos only" if config.media_only else "every file",
    ]
    if not config.recursive:
        parts.append("this directory only")
    if config.exclude_patterns:
        parts.append("excluding " + ", ".join(config.exclude_patterns))
    return " — ".join(parts)


def read_destination(
    console: Console,
    destination_root: Path,
    *,
    wanted_names: set[str],
    max_depth: int | None,
) -> DestinationIndex:
    """Read the destination, and say how much of it was walked.

    The walk is bounded by `--destination-depth` because the destination is
    often a whole photo library: reading every `stat` of a 45 000-file CIFS tree
    to answer questions about 1 000 files is what made this command look hung.
    Only the names seen on the phone are ever `stat`ed.
    """
    section(console, "Destination")
    levels = "the root only" if max_depth == 0 else f"{max_depth} level(s) deep"
    if max_depth is None:
        levels = "every level"
    with working(console, f"Reading {destination_root} ({levels})…"):
        index = build_destination_index(
            destination_root, wanted_names=wanted_names, max_depth=max_depth
        )
    detail = f"{format_count(index.files_seen, 'file')} seen"
    if index.directories_seen:
        count = format_count(index.directories_seen, "sub-directory", "sub-directories")
        detail += f", {count}" + ("" if max_depth == 0 else " entered")
    if index.stats_performed:
        detail += f", {format_count(index.stats_performed, 'name')} matched"
    if index.unreadable_directories:
        detail += f", {index.unreadable_directories} unreadable"
    console.print(f"[dim]{detail}[/dim]")
    return index


def print_destination_hint(console: Console, index: DestinationIndex) -> None:
    """Explain an empty or partial comparison, so it does not look like a bug."""
    if index.matched_names == 0 and index.max_depth == 0:
        console.print(
            "[yellow]Nothing matched at the destination root. If the copies live in "
            "sub-directories, add --destination-depth 2 (or 'all'), or point --dest at "
            "the folder that holds them.[/yellow]"
        )


def scan_everything(
    transport, directory: str, *, config: Config, patterns: tuple[str, ...]
) -> list[MediaFile]:
    """Read every candidate file of the source directory, without date filtering."""
    return list(
        iter_media_files(
            transport,
            directory,
            patterns=patterns,
            media_only=config.media_only,
            exclude_patterns=config.exclude_patterns,
            recursive=config.recursive,
        )
    )


def build_report(
    *,
    histogram: Histogram,
    suggestion: Suggestion | None,
    source_directory: str,
    destination_root: str,
    compared: bool,
) -> dict:
    """Build the machine-readable report printed by ``--json``."""
    report: dict = {
        "source": source_directory,
        "destination": destination_root,
        "destination_compared": compared,
        "file_count": histogram.file_count,
        "total_size": histogram.total_size,
        "missing_count": histogram.missing_count,
        "missing_size": histogram.missing_size,
        "months": [asdict(month) for month in histogram.months],
        "suggestion": None,
    }
    if suggestion is not None:
        report["suggestion"] = asdict(suggestion)
    return report
