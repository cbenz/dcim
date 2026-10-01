"""Tests for the JSON Lines manifest."""

from pathlib import Path

from dcim.manifest import (
    ManifestWriter,
    default_manifest_path,
    read_manifest,
    verified_entries,
)


def test_records_are_appended_and_readable(tmp_path: Path):
    manifest_path = tmp_path / "manifest.jsonl"
    with ManifestWriter(manifest_path) as manifest:
        manifest.record("run", destination_root="/mnt/nas-photos", verify_mode="partial")
        manifest.record("transfer", filename="a.jpg", destination="/nas/a.jpg", verified=True)
    records = list(read_manifest(manifest_path))
    assert [record["event"] for record in records] == ["run", "transfer"]
    assert records[0]["destination_root"] == "/mnt/nas-photos"
    assert records[1]["verified"] is True
    assert records[0]["timestamp"]


def test_verified_entries_excludes_unverified_and_deleted_ones(tmp_path: Path):
    manifest_path = tmp_path / "manifest.jsonl"
    with ManifestWriter(manifest_path) as manifest:
        manifest.record(
            "transfer", filename="a.jpg", destination="/nas/a.jpg", verified=True, size=1
        )
        manifest.record(
            "transfer", filename="b.jpg", destination="/nas/b.jpg", verified=False, size=2
        )
        manifest.record(
            "transfer", filename="c.jpg", destination="/nas/c.jpg", verified=True, size=3
        )
        manifest.record("deletion", filename="c.jpg", destination="/nas/c.jpg", status="deleted")
    entries = verified_entries(manifest_path)
    assert [entry["filename"] for entry in entries] == ["a.jpg"]


def test_unreadable_lines_are_skipped(tmp_path: Path):
    manifest_path = tmp_path / "manifest.jsonl"
    manifest_path.write_text('{"event": "run"}\nnot json\n{"event": "transfer"}\n')
    assert [record["event"] for record in read_manifest(manifest_path)] == ["run", "transfer"]


def test_default_manifest_path_is_hidden_in_the_destination(tmp_path: Path):
    path = default_manifest_path(tmp_path / "nas", "run-1")
    assert path == tmp_path / "nas" / ".dcim" / "manifest-run-1.jsonl"
