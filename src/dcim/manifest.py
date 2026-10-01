"""The JSON Lines manifest: the audit trail of a run.

One JSON object per line, flushed on every write, so an interrupted run still
leaves a usable file. It records what was copied, what was verified and what was
deleted — and it is the only thing the deletion phase trusts.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .formatting import format_utc_timestamp

MANIFEST_DIRECTORY_NAME = ".dcim"


def default_manifest_path(destination_root: Path, run_label: str) -> Path:
    """Return ``<destination>/.dcim/manifest-<run label>.jsonl``."""
    return destination_root / MANIFEST_DIRECTORY_NAME / f"manifest-{run_label}.jsonl"


class ManifestWriter:
    """Append-only writer for a run manifest."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")

    def record(self, event: str, **fields: Any) -> None:
        """Append one event to the manifest."""
        payload = {"timestamp": format_utc_timestamp(), "event": event}
        payload.update(fields)
        self._handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> "ManifestWriter":
        return self

    def __exit__(self, *unused: object) -> None:
        self.close()


def read_manifest(path: Path) -> Iterator[dict[str, Any]]:
    """Yield the records of a manifest, skipping unreadable lines."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def verified_entries(path: Path) -> list[dict[str, Any]]:
    """Return the transfers that were copied and verified but not deleted yet.

    This is what ``--resume`` replays: the copy phase is over for those files,
    only their deletion may still be pending.
    """
    deleted: set[str] = set()
    entries: dict[str, dict[str, Any]] = {}
    for record in read_manifest(path):
        destination = str(record.get("destination", ""))
        if not destination:
            continue
        if record.get("event") == "deletion":
            deleted.add(destination)
            continue
        if record.get("event") == "transfer" and record.get("verified") is True:
            entries[destination] = record
    return [record for destination, record in entries.items() if destination not in deleted]
