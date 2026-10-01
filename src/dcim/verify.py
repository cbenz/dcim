"""Checksums used to verify that a copy is intact before deleting the original.

Three verification levels are supported:

``partial`` (default)
    SHA-256 of the first block and of the last block of the file. The source
    side is hashed *while copying*, so it costs nothing extra; the destination
    side is read back. Catches truncated, partially written and mismatched
    files without reading 25 GB back over the network.
``full``
    SHA-256 of the whole file on both sides. Slowest, most thorough.
``none``
    Size comparison only.

A partial digest is rendered as ``<head hex>-<tail hex>``: comparing the two
strings is enough, and the two 64-character halves cannot be ambiguous. Files
shorter than two blocks are covered entirely by the head and the tail, which is
recorded as the ``full`` coverage so that a truncated copy can never match.
"""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

VerifyMode = Literal["partial", "full", "none"]
Coverage = Literal["full", "partial", "none"]

DEFAULT_PARTIAL_BLOCK_SIZE = 1024 * 1024
READ_BLOCK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class Digest:
    """The digest of a file, with the size it was computed over."""

    size: int
    value: str
    coverage: Coverage

    def matches(self, other: "Digest") -> bool:
        """Tell whether two digests were computed the same way and are equal."""
        return (
            self.size == other.size
            and self.coverage == other.coverage
            and self.value == other.value
        )


def compose_partial_digest(
    size: int, head: bytes, tail: bytes, block_size: int = DEFAULT_PARTIAL_BLOCK_SIZE
) -> Digest:
    """Build the partial digest of a file from its first and last block."""
    return Digest(
        size=size,
        value=f"{hashlib.sha256(head).hexdigest()}-{hashlib.sha256(tail).hexdigest()}",
        coverage="full" if size <= 2 * block_size else "partial",
    )


class StreamDigest:
    """Incremental digest of a stream, for the ``partial`` and ``full`` modes.

    Feeding the stream to :meth:`update` is the only way to compute a digest, so
    the bytes hashed are always the ones actually read or written.
    """

    def __init__(self, mode: VerifyMode, block_size: int = DEFAULT_PARTIAL_BLOCK_SIZE) -> None:
        self.mode = mode
        self.block_size = block_size
        self.size = 0
        self._head_buffer = bytearray()
        self._tail_buffer = bytearray()
        self._full_hasher = hashlib.sha256() if mode == "full" else None

    def update(self, chunk: bytes) -> None:
        """Feed the next bytes of the stream."""
        self.size += len(chunk)
        if self.mode == "none":
            return
        if self._full_hasher is not None:
            self._full_hasher.update(chunk)
            return
        if len(self._head_buffer) < self.block_size:
            head_part = chunk[: self.block_size - len(self._head_buffer)]
            self._head_buffer.extend(head_part)
            chunk = chunk[len(head_part) :]
        if chunk:
            self._tail_buffer.extend(chunk)
            if len(self._tail_buffer) > self.block_size:
                del self._tail_buffer[: len(self._tail_buffer) - self.block_size]

    def digest(self) -> Digest:
        """Return the digest of everything fed so far."""
        if self.mode == "none":
            return Digest(size=self.size, value="", coverage="none")
        if self._full_hasher is not None:
            return Digest(size=self.size, value=self._full_hasher.hexdigest(), coverage="full")
        return compose_partial_digest(
            size=self.size,
            head=bytes(self._head_buffer),
            tail=bytes(self._tail_buffer),
            block_size=self.block_size,
        )


def digest_stream(handle, mode: VerifyMode, block_size: int = DEFAULT_PARTIAL_BLOCK_SIZE) -> Digest:
    """Read `handle` to the end and return its digest."""
    stream_digest = StreamDigest(mode, block_size)
    for chunk in iter(lambda: handle.read(READ_BLOCK_SIZE), b""):
        stream_digest.update(chunk)
    return stream_digest.digest()


def digest_file(
    path: Path, mode: VerifyMode, block_size: int = DEFAULT_PARTIAL_BLOCK_SIZE
) -> Digest:
    """Compute the digest of a local file.

    In `partial` mode only two blocks are read — the first and the last — whatever
    the size of the file. Reading the whole file to keep two blocks of it made the
    verification of an already-copied range read gigabytes over the network for
    nothing (measured: 48 s instead of 12 s for 32 files, one of them 90 MB).
    """
    with path.open("rb") as handle:
        if mode == "partial":
            return digest_file_blocks(handle, block_size)
        return digest_stream(handle, mode, block_size)


def digest_file_blocks(handle, block_size: int = DEFAULT_PARTIAL_BLOCK_SIZE) -> Digest:
    """Compute the partial digest of an open file, reading only two blocks.

    Used to hash a remote original without reading it entirely: an already
    present 1 GB video costs 2 MB of MTP traffic instead of 1 GB.
    """
    head = handle.read(block_size)
    if len(head) < block_size:
        return compose_partial_digest(len(head), head, b"", block_size)
    handle.seek(0, os.SEEK_END)
    total_size = handle.tell()
    handle.seek(max(total_size - block_size, block_size))
    tail = handle.read(block_size)
    return compose_partial_digest(total_size, head, tail, block_size)


def sizes_match(path: Path, expected_size: int) -> bool:
    """Compare the size of a local file with an expected value."""
    try:
        return path.stat().st_size == expected_size
    except OSError:
        return False
