"""Tests for the partial and full digests."""

from pathlib import Path

from dcim.verify import (
    compose_partial_digest,
    digest_file,
    digest_file_blocks,
    sizes_match,
)

BLOCK_SIZE = 16


def make_file(path: Path, size: int) -> Path:
    path.write_bytes(bytes(index % 251 for index in range(size)))
    return path


def test_full_digest_round_trip(tmp_path: Path):
    path = make_file(tmp_path / "a.bin", 100)
    digest = digest_file(path, "full")
    assert digest.size == 100
    assert digest.coverage == "full"
    assert len(digest.value) == 64


def test_partial_digest_of_a_small_file_is_full_coverage(tmp_path: Path):
    path = make_file(tmp_path / "small.bin", BLOCK_SIZE)
    digest = digest_file(path, "partial", BLOCK_SIZE)
    assert digest.coverage == "full"
    assert (
        digest.value == compose_partial_digest(BLOCK_SIZE, path.read_bytes(), b"", BLOCK_SIZE).value
    )


def test_partial_digest_of_a_large_file_is_partial_coverage(tmp_path: Path):
    path = make_file(tmp_path / "large.bin", BLOCK_SIZE * 4)
    digest = digest_file(path, "partial", BLOCK_SIZE)
    assert digest.coverage == "partial"


def test_block_digest_matches_the_streamed_digest(tmp_path: Path):
    """Reading two blocks must give exactly the digest of a streamed read."""
    for size in (0, 1, BLOCK_SIZE - 1, BLOCK_SIZE, BLOCK_SIZE + 1, BLOCK_SIZE * 3):
        path = make_file(tmp_path / f"size-{size}.bin", size)
        with path.open("rb") as handle:
            assert digest_file(path, "partial", BLOCK_SIZE) == digest_file_blocks(
                handle, BLOCK_SIZE
            )


def test_partial_digest_detects_a_truncated_copy(tmp_path: Path):
    original = make_file(tmp_path / "original.bin", BLOCK_SIZE * 4)
    truncated = make_file(tmp_path / "truncated.bin", BLOCK_SIZE * 4 - 1)
    assert not digest_file(original, "partial", BLOCK_SIZE).matches(
        digest_file(truncated, "partial", BLOCK_SIZE)
    )


def test_partial_digest_detects_a_tail_corruption(tmp_path: Path):
    original = make_file(tmp_path / "original.bin", BLOCK_SIZE * 4)
    corrupt = make_file(tmp_path / "corrupt.bin", BLOCK_SIZE * 4)
    content = bytearray(corrupt.read_bytes())
    content[-1] ^= 0xFF
    corrupt.write_bytes(bytes(content))
    assert not digest_file(original, "partial", BLOCK_SIZE).matches(
        digest_file(corrupt, "partial", BLOCK_SIZE)
    )


def test_partial_digest_ignores_a_middle_corruption(tmp_path: Path):
    """Documented limit: only the first and the last block are compared."""
    original = make_file(tmp_path / "original.bin", BLOCK_SIZE * 6)
    corrupt = make_file(tmp_path / "corrupt.bin", BLOCK_SIZE * 6)
    content = bytearray(corrupt.read_bytes())
    content[BLOCK_SIZE * 2] ^= 0xFF
    corrupt.write_bytes(bytes(content))
    assert digest_file(original, "partial", BLOCK_SIZE).matches(
        digest_file(corrupt, "partial", BLOCK_SIZE)
    )
    assert not digest_file(original, "full", BLOCK_SIZE).matches(
        digest_file(corrupt, "full", BLOCK_SIZE)
    )


def test_none_mode_only_reports_the_size(tmp_path: Path):
    path = make_file(tmp_path / "a.bin", 12)
    digest = digest_file(path, "none")
    assert digest.value == ""
    assert digest.coverage == "none"
    assert digest.size == 12


def test_sizes_match(tmp_path: Path):
    path = make_file(tmp_path / "a.bin", 12)
    assert sizes_match(path, 12)
    assert not sizes_match(path, 13)
    assert not sizes_match(tmp_path / "missing.bin", 0)


def test_a_partial_digest_reads_two_blocks_and_not_the_whole_file(tmp_path: Path, monkeypatch):
    """Reading a gigabyte to keep two blocks of it made verification crawl."""
    import dcim.verify as verify

    path = make_file(tmp_path / "large.bin", BLOCK_SIZE * 100)
    calls: list[str] = []
    original = verify.digest_file_blocks

    def spy(handle, block_size=verify.DEFAULT_PARTIAL_BLOCK_SIZE):
        calls.append("blocks")
        return original(handle, block_size)

    monkeypatch.setattr(verify, "digest_file_blocks", spy)
    digest_file(path, "partial", BLOCK_SIZE)
    assert calls == ["blocks"]

    calls.clear()
    digest_file(path, "full", BLOCK_SIZE)
    assert calls == []  # the full digest does read everything, that is its job
