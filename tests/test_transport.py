"""Tests for the device transports.

The adb transport is tested against a fake ``adb`` that talks to a local
directory, so the real shell commands, their output and the parsers are all
exercised — without a device.
"""

import stat
import sys
from pathlib import Path

import pytest

from dcim.analysis import StorageSpace
from dcim.errors import TransportError
from dcim.transport import (
    EMPTY_SHA256,
    AdbTransport,
    MtpTransport,
    chunked,
    digest_script,
    parse_df,
    parse_digest_lines,
    parse_size_lines,
    parse_stat_lines,
    quote_for_shell,
)

FAKE_ADB = Path(__file__).parent / "fake_adb.py"
BLOCK_SIZE = 8


@pytest.fixture
def device_root(tmp_path: Path, monkeypatch) -> Path:
    """A local directory standing in for the device storage."""
    root = tmp_path / "sdcard"
    root.mkdir()
    monkeypatch.setenv("FAKE_ADB_ROOT", str(root))
    return root


@pytest.fixture
def adb(device_root: Path, tmp_path: Path) -> AdbTransport:
    executable = tmp_path / "adb"
    executable.write_text(f"#!{sys.executable}\n" + FAKE_ADB.read_text())
    executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
    return AdbTransport("FAKESERIAL", executable=str(executable), batch_size=2)


def populate(root: Path, name: str, content: bytes) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_the_fake_device_is_where_the_commands_expect_it(adb: AdbTransport, device_root: Path):
    assert adb.device_path("DCIM/a.jpg") == "/sdcard/DCIM/a.jpg"
    assert adb.relative_path("/sdcard/DCIM/a.jpg") == "DCIM/a.jpg"


def test_listing_a_directory_gives_size_and_time(adb: AdbTransport, device_root: Path):
    populate(device_root, "DCIM/Camera/a.jpg", b"content" * 10)
    populate(device_root, "DCIM/Camera/b.mp4", b"video" * 100)
    (device_root / "DCIM/Camera/sub").mkdir()
    entries = {entry.name: entry for entry in adb.list_directory("DCIM/Camera")}
    assert set(entries) == {"a.jpg", "b.mp4", "sub"}
    assert entries["a.jpg"].size == 70
    assert not entries["a.jpg"].is_directory
    assert entries["a.jpg"].path == "DCIM/Camera/a.jpg"
    assert entries["sub"].is_directory
    assert entries["a.jpg"].modified_at > 0


def test_listing_a_missing_directory_fails(adb: AdbTransport):
    """An empty listing would be a silent lie about a directory that is not there."""
    with pytest.raises(TransportError, match="the device refused"):
        adb.list_directory("DCIM/Nope")


def test_an_empty_directory_lists_nothing(adb: AdbTransport, device_root: Path):
    (device_root / "DCIM/Empty").mkdir(parents=True)
    assert adb.list_directory("DCIM/Empty") == []


def test_directory_exists(adb: AdbTransport, device_root: Path):
    (device_root / "DCIM/Camera").mkdir(parents=True)
    assert adb.directory_exists("DCIM/Camera")
    assert not adb.directory_exists("DCIM/Nope")


def test_sizes_are_read_in_batches(adb: AdbTransport, device_root: Path):
    for index in range(5):
        populate(device_root, f"DCIM/Camera/f{index}.jpg", b"x" * (index + 1))
    paths = [f"DCIM/Camera/f{index}.jpg" for index in range(5)]
    sizes = adb.sizes([*paths, "DCIM/Camera/gone.jpg"])
    assert sizes == {path: index + 1 for index, path in enumerate(paths)}


def test_partial_digests_match_the_local_ones(adb: AdbTransport, device_root: Path):
    """The device-side digest must be the very same value as the local one."""
    from dcim.verify import digest_file

    content = bytes(range(256)) * 3  # longer than two blocks
    path = populate(device_root, "DCIM/Camera/a.jpg", content)
    digest = adb.digests([("DCIM/Camera/a.jpg", len(content))], "partial", BLOCK_SIZE)
    assert digest["DCIM/Camera/a.jpg"] == digest_file(path, "partial", BLOCK_SIZE)


def test_partial_digests_of_a_short_file_match_too(adb: AdbTransport, device_root: Path):
    """A file shorter than two blocks is covered entirely: same value locally."""
    from dcim.verify import digest_file

    for size in (0, 3, BLOCK_SIZE, BLOCK_SIZE * 2):
        content = bytes(range(256))[:size]
        populate(device_root, f"DCIM/Camera/s{size}.jpg", content)
        digest = adb.digests([(f"DCIM/Camera/s{size}.jpg", size)], "partial", BLOCK_SIZE)
        assert digest[f"DCIM/Camera/s{size}.jpg"] == digest_file(
            device_root / f"DCIM/Camera/s{size}.jpg", "partial", BLOCK_SIZE
        )


def test_full_digests_match_the_local_ones(adb: AdbTransport, device_root: Path):
    from dcim.verify import digest_file

    content = b"content" * 100
    path = populate(device_root, "DCIM/Camera/a.jpg", content)
    digest = adb.digests([("DCIM/Camera/a.jpg", len(content))], "full", BLOCK_SIZE)
    assert digest["DCIM/Camera/a.jpg"] == digest_file(path, "full", BLOCK_SIZE)
    assert digest["DCIM/Camera/a.jpg"].coverage == "full"


def test_digests_are_skipped_when_verification_is_off(adb: AdbTransport, device_root: Path):
    populate(device_root, "DCIM/Camera/a.jpg", b"content")
    assert adb.digests([("DCIM/Camera/a.jpg", 7)], "none", BLOCK_SIZE) == {}


def test_pull_copies_the_bytes_and_reports_progress(
    adb: AdbTransport, device_root: Path, tmp_path: Path
):
    content = b"content" * 500
    populate(device_root, "DCIM/Camera/a.jpg", content)
    destination = tmp_path / "a.jpg.part"
    progress: list[int] = []
    digest = adb.copy_to(
        "DCIM/Camera/a.jpg",
        len(content),
        destination,
        progress.append,
        mode="partial",
        block_size=BLOCK_SIZE,
    )
    assert digest is None  # the transport does not hash what adb writes
    assert destination.read_bytes() == content
    assert sum(progress) == len(content)


def test_pull_refuses_a_truncated_transfer(adb: AdbTransport, device_root: Path, tmp_path: Path):
    populate(device_root, "DCIM/Camera/a.jpg", b"content")
    with pytest.raises(TransportError, match="instead of"):
        adb.copy_to(
            "DCIM/Camera/a.jpg",
            1000,
            tmp_path / "a.part",
            lambda _count: None,
            mode="partial",
            block_size=BLOCK_SIZE,
        )


def test_pull_reports_a_missing_file(adb: AdbTransport, tmp_path: Path):
    with pytest.raises(TransportError, match="failed"):
        adb.copy_to(
            "DCIM/Camera/gone.jpg",
            10,
            tmp_path / "a.part",
            lambda _count: None,
            mode="partial",
            block_size=BLOCK_SIZE,
        )


def test_delete_and_remaining_are_batched(adb: AdbTransport, device_root: Path):
    for index in range(5):
        populate(device_root, f"DCIM/Camera/f{index}.jpg", b"x")
    paths = [f"DCIM/Camera/f{index}.jpg" for index in range(5)]
    seen: list[int] = []
    adb.delete(paths, seen.append)
    assert sum(seen) == 5
    assert adb.remaining(paths) == set()
    assert not list((device_root / "DCIM/Camera").iterdir())


def test_remaining_reports_the_survivors(adb: AdbTransport, device_root: Path):
    populate(device_root, "DCIM/Camera/f0.jpg", b"x")
    populate(device_root, "DCIM/Camera/f1.jpg", b"x")
    adb.delete(["DCIM/Camera/f0.jpg"], lambda _count: None)
    assert adb.remaining(["DCIM/Camera/f0.jpg", "DCIM/Camera/f1.jpg"]) == {"DCIM/Camera/f1.jpg"}


def test_volumes_and_storage_space(adb: AdbTransport, device_root: Path):
    volumes = adb.volumes()
    assert volumes[0] == ("Internal storage", "/sdcard")
    space = adb.storage_space("")
    assert space is not None
    assert space.total > 0 and space.free is not None


def test_with_root_keeps_the_serial(adb: AdbTransport):
    other = adb.with_root("/storage/1234")
    assert other.serial == adb.serial
    assert other.root == "/storage/1234"
    assert other.device_path("DCIM/a.jpg") == "/storage/1234/DCIM/a.jpg"


def test_local_transport_maps_to_a_directory(tmp_path: Path):
    populate(tmp_path, "DCIM/Camera/a.jpg", b"content")
    transport = MtpTransport(tmp_path, "DCIM")
    assert transport.describe().endswith("/DCIM")
    assert transport.directory_exists("Camera")
    assert transport.local_root() == tmp_path / "DCIM"
    assert [entry.name for entry in transport.list_directory("Camera")] == ["a.jpg"]


def test_chunked_splits_and_keeps_the_order():
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunked([], 2)) == []


def test_quote_for_shell_handles_quotes_and_spaces():
    assert quote_for_shell("a b.jpg") == "'a b.jpg'"
    assert quote_for_shell("it's.jpg") == "'it'\\''s.jpg'"


def test_parse_stat_lines_ignores_broken_output():
    output = "70|1759301351|regular file|a.jpg\nbroken\n5|notatime|regular file|b.jpg\n"
    entries = list(parse_stat_lines(output, directory="DCIM/Camera/"))
    assert [entry.name for entry in entries] == ["a.jpg"]


def test_parse_size_lines():
    assert parse_size_lines("70|a.jpg\n0|b.jpg\njunk\n") == {"a.jpg": 70, "b.jpg": 0}


def test_parse_df_picks_the_line_matching_the_path():
    """`df` without a path lists every file system, system partitions included."""
    everything = (
        "Filesystem        1K-blocks      Used Available Use% Mounted on\n"
        "/dev/block/dm-10    1064092   1060828         0 100% /\n"
        "tmpfs               3809944      2416   3807528   1% /dev\n"
    )
    # No path given: the first line wins, which is why a path is always given.
    # `used` is total minus available, as `statvfs` reports on the MTP side, so
    # that both transports answer the same question the same way.
    assert parse_df(everything) == StorageSpace(total=1064092 * 1024, used=1064092 * 1024, free=0)
    assert parse_df("nothing to parse") is None


def test_parse_df_follows_the_longest_mounting_point():
    output = (
        "Filesystem     1K-blocks      Used Available Use% Mounted on\n"
        "/dev/fuse      115249236 113770856   1347308  99% /storage/emulated\n"
        "/dev/block/dm-63 115249236 113770856   1347308  99% /data/user/0\n"
    )
    space = parse_df(output, path="/storage/emulated")
    assert space is not None
    assert space.total == 115249236 * 1024
    assert space.free == 1347308 * 1024


def test_digest_script_uses_the_local_byte_ranges():
    script = digest_script(
        [("DCIM/a.jpg", 1000), ("DCIM/b.mp4", 912736383)], "partial", 1024, root="/sdcard"
    )
    assert "head -c 1000 '/sdcard/DCIM/a.jpg'" in script
    # 1000 bytes <= two blocks of 1024: the tail is empty, so it is the empty
    # digest, never `tail -c 0` (which returns the whole file on Android).
    assert f"t={EMPTY_SHA256}" in script
    assert "head -c 1024 '/sdcard/DCIM/b.mp4'" in script
    assert "tail -c 1024 '/sdcard/DCIM/b.mp4'" in script


def test_parse_digest_lines_builds_the_same_value_as_the_local_digest():
    digests = parse_digest_lines("a.jpg|1111|2222", {"a.jpg": 20}, mode="partial", block_size=8)
    assert digests["a.jpg"].value == "1111-2222"
    assert digests["a.jpg"].size == 20
    assert digests["a.jpg"].coverage == "partial"  # 20 bytes > two blocks of 8
    short = parse_digest_lines("b.jpg|3333|4444", {"b.jpg": 12}, mode="partial", block_size=8)
    assert short["b.jpg"].coverage == "full"  # 12 bytes <= two blocks of 8
    full = parse_digest_lines("c.jpg|5555", {"c.jpg": 20}, mode="full", block_size=8)
    assert full["c.jpg"].value == "5555" and full["c.jpg"].coverage == "full"


def test_the_digest_script_never_asks_for_zero_bytes():
    """`tail -c 0` returns the whole file on Android (toybox), nothing on GNU.

    A file shorter than one block has nothing to hash in its tail: the empty
    digest must be inlined, or the device digest hashes the file twice and every
    small photo fails its verification.
    """
    block = 1024 * 1024
    script = digest_script(
        [
            ("DCIM/small.jpg", 400_000),  # shorter than a block: empty tail
            ("DCIM/empty.jpg", 0),  # nothing at all on both sides
            ("DCIM/mid.jpg", 1_500_000),  # between one and two blocks: tail = the rest
            ("DCIM/big.mp4", 3_000_000),  # more than two blocks: a full tail block
        ],
        "partial",
        block,
        root="/sdcard",
    )
    assert "tail -c 0" not in script
    assert "head -c 0" not in script
    # Three null byte counts: the small file's tail, and both blocks of the
    # empty file.
    assert script.count(EMPTY_SHA256) == 3
    assert "head -c 400000" in script
    assert "tail -c 451424" in script  # 1 500 000 - 1 048 576
    assert "tail -c 1048576" in script  # a whole block for the big one


def test_a_short_file_digest_matches_the_local_one_on_the_device():
    """The same value on both sides, including the zero-tail case (toybox)."""
    import hashlib

    from dcim.verify import compose_partial_digest

    content = b"x" * 400_000  # shorter than one block: the tail is empty
    block_size = 1024 * 1024
    script = digest_script([("f.jpg", len(content))], "partial", block_size, root="/sdcard")
    assert "tail -c 0" not in script
    assert EMPTY_SHA256 in script
    expected = compose_partial_digest(
        len(content), hashlib.sha256(content).digest()[:0] or content, b"", block_size
    )
    # The device returns sha256(content) for the head and sha256("") for the tail.
    head = hashlib.sha256(content).hexdigest()
    device_value = f"{head}-{EMPTY_SHA256}"
    assert expected.value == device_value
    assert expected.coverage == "full"
