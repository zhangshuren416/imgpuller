"""Streaming SHA256 hash verification for blob downloads.

Provides a StreamingHashWriter that transparently computes the SHA256 digest
as data is written to a file, avoiding a separate read pass for verification.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import BinaryIO, Optional

logger = logging.getLogger(__name__)


class StreamingHashWriter:
    """A file-like object wrapper that computes SHA256 while writing.

    Usage:
        hasher = StreamingHashWriter(open('file.bin', 'wb'))
        hasher.write(chunk1)
        hasher.write(chunk2)
        hasher.close()
        assert hasher.hexdigest() == expected_hex
    """

    def __init__(self, fileobj: BinaryIO, algorithm: str = "sha256"):
        """Initialize with an open binary file object.

        Args:
            fileobj: An open file in write-binary mode.
            algorithm: Hash algorithm (sha256, sha512, etc.)
        """
        self._file = fileobj
        self._hash = hashlib.new(algorithm)
        self._bytes_written = 0

    def write(self, data: bytes) -> int:
        """Write data to the file and update the hash.

        Args:
            data: Bytes to write.

        Returns:
            Number of bytes written.
        """
        self._file.write(data)
        self._hash.update(data)
        self._bytes_written += len(data)
        return len(data)

    def hexdigest(self) -> str:
        """Get the current hex digest."""
        return self._hash.hexdigest()

    def digest(self) -> bytes:
        """Get the current binary digest."""
        return self._hash.digest()

    def full_digest(self) -> str:
        """Get the full digest string (e.g. "sha256:abc123...")."""
        return f"{self._hash.name}:{self._hash.hexdigest()}"

    @property
    def bytes_written(self) -> int:
        """Get total bytes written so far."""
        return self._bytes_written

    def close(self) -> None:
        """Close the underlying file."""
        self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def fileno(self):
        return self._file.fileno()

    def flush(self):
        self._file.flush()


def parse_digest(digest: str) -> tuple[str, str]:
    """Parse a digest string into (algorithm, hex_value).

    Args:
        digest: Digest string like "sha256:abc123..."

    Returns:
        Tuple of (algorithm, hex_string).

    Raises:
        ValueError: If digest format is invalid.
    """
    if ":" not in digest:
        raise ValueError(
            f"Invalid digest format: {digest!r}. Expected 'algorithm:hex'"
        )
    algo, hex_val = digest.split(":", 1)
    if not hex_val or not all(c in "0123456789abcdef" for c in hex_val.lower()):
        raise ValueError(f"Invalid hex in digest: {digest!r}")
    return algo, hex_val


def verify_file(path: Path, expected_digest: str, chunk_size: int = 1024 * 1024) -> bool:
    """Verify a file's SHA256 digest against an expected value.

    Args:
        path: Path to the file.
        expected_digest: Expected digest (e.g. "sha256:abc...").
        chunk_size: Read chunk size in bytes.

    Returns:
        True if the file matches the expected digest.

    Raises:
        FileNotFoundError: If path doesn't exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    try:
        algo, expected_hex = parse_digest(expected_digest)
    except ValueError:
        logger.warning("Invalid digest format: %s", expected_digest)
        return False

    hasher = hashlib.new(algo)

    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)

    actual_hex = hasher.hexdigest()
    match = actual_hex == expected_hex

    if not match:
        logger.warning(
            "Digest mismatch for %s: expected %s, got %s",
            path.name,
            expected_digest,
            f"{algo}:{actual_hex}",
        )

    return match


def compute_file_digest(path: Path, algorithm: str = "sha256") -> str:
    """Compute the digest of a file.

    Args:
        path: Path to the file.
        algorithm: Hash algorithm.

    Returns:
        Digest string like "sha256:abc123..."
    """
    hasher = hashlib.new(algorithm)

    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)

    return f"{algorithm}:{hasher.hexdigest()}"


def compute_bytes_digest(data: bytes, algorithm: str = "sha256") -> str:
    """Compute the digest of bytes data."""
    h = hashlib.new(algorithm)
    h.update(data)
    return f"{algorithm}:{h.hexdigest()}"
