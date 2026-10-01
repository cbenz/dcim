"""The ``dcim sync`` command: copy, verify, and optionally delete the originals.

``sync`` alone is a backup: it copies and verifies, and never touches the
originals. ``sync --delete`` is an offload: it deletes the originals that were
verified, after an explicit confirmation. Keeping the two behind one flag means
the destructive behaviour is always an explicit, per-run decision — it is
deliberately not a configuration file setting.
"""

import argparse
import os
from datetime import datetime
from pathlib import Path

from rich.console import Console

from .analysis import default_range_arguments
from .config import Config, apply_overrides, load_config, save_config
from .dates import parse_range_bound
from .deletion import (
    DeletionReport,
    delete_originals,
    eligible_outcomes,
    outcomes_from_manifest,
)
from .errors import SourceSelectionError
from .formatting import format_bytes, format_count, format_utc_timestamp
from .manifest import ManifestWriter, default_manifest_path, read_manifest, verified_entries
from .progress import ProgressReporter, make_reporter
from .selection import (
    MediaFile,
    SelectionSummary,
    build_summary,
    check_free_space,
    iter_media_files,
    select_media_files,
)
from .session import PhoneSession
from .transfer import (
    TransferOutcome,
    check_destination_is_separate,
    plan_destination_path,
    transfer_all,
)
from .ui import (
    ask_confirmation,
    ask_date_range,
    ask_deletion_confirmation,
    render_deletion_report,
    render_preview,
    render_transfer_report,
    section,
    working,
)


def run_sync(options: argparse.Namespace, console: Console) -> int:
    """Copy a date range from the phone to the destination, then optionally delete."""
    config = load_config(options.config)
    apply_overrides(config, options)
    if options.save_defaults:
        console.print(f"[green]Defaults saved to {save_config(config, options.config)}[/green]")
    destination_root = config.destination_root.expanduser()
    manifest_path = manifest_path_for(options, destination_root)
    if options.resume is not None:
        return resume_run(options, config, console, manifest_path)

    start, end = resolve_date_range(options, config, console)
    with PhoneSession(
        config,
        console,
        transport_name=config.transport,
        mount_destination=config.mount_nas,
        keep_mounted=options.keep_mounted,
    ) as session:
        source = session.select_source(
            source_option=options.source,
            pattern_option=options.pattern,
            prompt=options.prompt,
        )
        transport = session.assert_open()
        local_root = transport.local_root()
        if local_root is not None:
            check_destination_is_separate(destination_root, local_root)
        console.print(f"[dim]Source: {source.root}/{source.directory}[/dim]")
        console.print(f"[dim]Destination: {destination_root}[/dim]")
        console.print(
            f"[dim]Pattern: {', '.join(source.patterns)} — date range "
            f"{start:%Y-%m-%d} .. {end:%Y-%m-%d} (end excluded)[/dim]"
        )

        section(console, "Preview")
        summary = scan_source(
            transport,
            source,
            config=config,
            start=start,
            end=end,
            console=console,
        )
        render_preview(console, summary)
        if summary.count == 0:
            return 0
        if options.limit is not None:
            summary = build_summary(summary.files[: options.limit])
            console.print(f"[yellow]Limited to the first {summary.count} files.[/yellow]")
        if options.dry_run:
            print_plan(console, summary, destination_root, config, delete=options.delete)
            console.print("[yellow]Dry run: nothing was copied and nothing was deleted.[/yellow]")
            return 0
        if not options.yes and not ask_confirmation(
            console,
            f"Copy {format_count(summary.count, 'file')} ({format_bytes(summary.total_size)})?",
            default=True,
        ):
            console.print("[yellow]Aborted by the user.[/yellow]")
            return 0
        # A dry run must not create anything, so the destination directory is
        # only created once the user has accepted the copy.
        destination_root.mkdir(parents=True, exist_ok=True)
        check_free_space(destination_root, summary.total_size)

        section(console, "Copy")
        with ManifestWriter(manifest_path) as manifest:
            record_run(manifest, config, source, transport.name, start, end)
            outcomes = copy_phase(
                summary,
                transport=transport,
                destination_root=destination_root,
                config=config,
                manifest=manifest,
                console=console,
                quiet=options.quiet,
            )
        render_transfer_report(console, outcomes)

        section(console, "Deletion")
        deletion_report = None
        if not options.delete:
            console.print(
                "[yellow]The originals were kept on the phone.[/yellow] "
                "[dim]Add --delete to remove the verified copies from the phone.[/dim]"
            )
        else:
            if any(outcome.status in ("failed", "conflict") for outcome in outcomes):
                console.print(
                    "[yellow]Some files failed: they stay on the phone, and only the "
                    "verified copies can be deleted.[/yellow]"
                )
            deletion_report = delete_phase(
                outcomes,
                transport=transport,
                config=config,
                assume_yes=options.yes,
                manifest_path=manifest_path,
                console=console,
                quiet=options.quiet,
            )
        console.print(f"\n[dim]Manifest: {manifest_path}[/dim]")
        return exit_code(outcomes, deletion_report)


def resolve_date_range(
    options: argparse.Namespace, config: Config, console: Console
) -> tuple[datetime, datetime]:
    """Return the capture date range, half-open, asking the user when needed."""
    # The prompted and the silent defaults must be the same window, which is why
    # the labels are computed once, as --from/--to arguments.
    default_from, default_to = default_range_arguments(months=config.months)
    from_text = options.from_date or default_from
    to_text = options.to_date or default_to
    if options.prompt and (options.from_date is None or options.to_date is None):
        from_text, to_text = ask_date_range(console, default_from, default_to)
    try:
        start = parse_range_bound(from_text, is_end=False)
        end = parse_range_bound(to_text, is_end=True)
    except ValueError as error:
        raise SourceSelectionError(f"invalid date range: {error}") from error
    if end <= start:
        raise SourceSelectionError(
            f"the end bound ({to_text}) must be after the start bound ({from_text})"
        )
    return start, end


def scan_source(
    transport,
    source,
    *,
    config: Config,
    start: datetime,
    end: datetime,
    console: Console,
) -> SelectionSummary:
    """Scan the device and return the selection matching every criterion."""
    with working(console, f"Reading {source.directory} on the phone…"):
        media_files: list[MediaFile] = list(
            iter_media_files(
                transport,
                source.device_directory,
                patterns=source.patterns,
                media_only=config.media_only,
                exclude_patterns=config.exclude_patterns,
                recursive=config.recursive,
            )
        )
    console.print(f"[dim]{format_count(len(media_files), 'file')} read from the phone.[/dim]")
    return build_summary(select_media_files(media_files, start=start, end=end))


def print_plan(
    console: Console,
    summary: SelectionSummary,
    destination_root: Path,
    config: Config,
    *,
    delete: bool,
) -> None:
    """Print where the files would land, without copying anything.

    One example, not five: the point is to show the shape of the destination —
    flattened or with the source tree — and the first file shows it. Five names
    are five wrapped lines that say the same thing.
    """
    console.print(f"[dim]Destination root: {destination_root}[/dim]")
    console.print(
        f"[dim]Layout: {'tree preserved' if config.preserve_tree else 'flat'}, "
        f"verification: {config.verify_mode}, conflicts: {config.conflict_policy}, "
        f"originals: {'deleted after verification' if delete else 'kept'}[/dim]"
    )
    first = summary.files[0]
    console.print(
        f"  {first.relative_posix} → "
        f"{plan_destination_path(destination_root, first, preserve_tree=config.preserve_tree)}"
    )
    if summary.count > 1:
        console.print(f"  [dim]… and {format_count(summary.count - 1, 'other file')}[/dim]")


def copy_phase(
    summary: SelectionSummary,
    *,
    transport,
    destination_root: Path,
    config: Config,
    manifest: ManifestWriter,
    console: Console,
    quiet: bool = False,
) -> list[TransferOutcome]:
    """Copy and verify the whole selection."""
    progress: ProgressReporter = make_reporter(console, quiet=quiet)
    return transfer_all(
        summary.files,
        transport=transport,
        destination_root=destination_root,
        verify_mode=config.verify_mode,
        conflict_policy=config.conflict_policy,
        preserve_tree=config.preserve_tree,
        preserve_times=config.preserve_times,
        progress=progress,
        manifest=manifest,
    )


def delete_phase(
    outcomes: list[TransferOutcome],
    *,
    transport,
    config: Config,
    assume_yes: bool,
    manifest_path: Path,
    console: Console,
    quiet: bool = False,
) -> DeletionReport:
    """Ask for confirmation, then delete the originals of the verified copies."""
    targets = eligible_outcomes(outcomes)
    if not targets:
        console.print("[yellow]Nothing to delete: no verified copy.[/yellow]")
        return DeletionReport()
    total_size = sum(outcome.media_file.size for outcome in targets)
    if not ask_deletion_confirmation(
        console, file_count=len(targets), total_size=total_size, assume_yes=assume_yes
    ):
        console.print("[yellow]The originals were kept on the phone.[/yellow]")
        return DeletionReport()
    with ManifestWriter(manifest_path) as manifest:
        progress: ProgressReporter = make_reporter(console, quiet=quiet)
        report = delete_originals(
            targets,
            transport=transport,
            verify_mode=config.verify_mode,
            progress=progress,
            manifest=manifest,
        )
    render_deletion_report(console, report)
    return report


def resume_run(
    options: argparse.Namespace, config: Config, console: Console, manifest_path: Path
) -> int:
    """Replay the deletion phase of an interrupted run from its manifest."""
    if not manifest_path.is_file():
        raise SourceSelectionError(f"no manifest to resume: {manifest_path}")
    run_record = read_run_record(manifest_path)
    if run_record is None:
        raise SourceSelectionError(f"{manifest_path} does not describe a run")
    config.verify_mode = run_record.get("verify_mode", config.verify_mode)
    config.destination_root = Path(run_record["destination_root"])
    source_subdirectory = str(run_record.get("source_subdirectory") or config.source_directory)
    with PhoneSession(
        config,
        console,
        transport_name=config.transport,
        mount_destination=config.mount_nas,
        keep_mounted=options.keep_mounted,
    ) as session:
        transport = session.assert_open()
        label, root = session.choose_volume()
        del label
        session.transport = transport.with_root(root)
        transport = session.transport
        directory = os.path.normpath(source_subdirectory.lstrip("/"))
        outcomes = outcomes_from_manifest(verified_entries(manifest_path), directory)
        console.print(
            f"[bold]{len(outcomes)}[/bold] verified files pending deletion, "
            f"{format_bytes(sum(outcome.media_file.size for outcome in outcomes))}"
        )
        if options.dry_run:
            console.print("[yellow]Dry run: nothing was deleted.[/yellow]")
            return 0
        deletion_report = delete_phase(
            outcomes,
            transport=transport,
            config=config,
            assume_yes=options.yes,
            manifest_path=manifest_path,
            console=console,
            quiet=options.quiet,
        )
        return exit_code(outcomes, deletion_report)


def record_run(
    manifest: ManifestWriter,
    config: Config,
    source,
    transport_name: str,
    start: datetime,
    end: datetime,
) -> None:
    """Record everything needed to understand — and resume — the run."""
    manifest.record(
        "run",
        destination_root=str(config.destination_root),
        nas_mountpoint=str(config.nas_mountpoint),
        volume_label=source.label,
        transport=transport_name,
        source_subdirectory=source.directory,
        pattern=",".join(source.patterns),
        media_only=config.media_only,
        recursive=config.recursive,
        date_from=start.isoformat(),
        date_to=end.isoformat(),
        verify_mode=config.verify_mode,
        started_at=format_utc_timestamp(),
    )


def read_run_record(manifest_path: Path) -> dict | None:
    """Return the ``run`` record of a manifest, or None."""
    for record in read_manifest(manifest_path):
        if record.get("event") == "run":
            return record
    return None


def exit_code(outcomes: list[TransferOutcome], deletion: DeletionReport | None = None) -> int:
    """Return 0 when everything succeeded, 2 when something was left behind."""
    failed = [outcome for outcome in outcomes if outcome.status in ("failed", "conflict")]
    if deletion is not None:
        failed.extend(outcome for outcome, _reason in deletion.failed)
    return 2 if failed else 0


def manifest_path_for(options: argparse.Namespace, destination_root: Path) -> Path:
    """Return the manifest to write, or the one to resume."""
    if options.resume is not None:
        return options.resume
    # 2026-09-27T13:29:57+00:00 -> 20260927T132957Z, a file-name-safe label
    run_label = format_utc_timestamp().replace("+00:00", "Z").replace(":", "").replace("-", "")
    return default_manifest_path(destination_root, run_label)
