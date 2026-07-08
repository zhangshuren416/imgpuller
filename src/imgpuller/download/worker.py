"""Single blob download worker with resume and verification support.

Handles:
- Downloading a single blob from the registry
- Resuming interrupted downloads via HTTP Range header
- Streaming SHA256 verification during download
- Failure recovery and state management
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Callable

import aiohttp

from imgpuller.download.state import DownloadState
from imgpuller.exceptions import (
    DigestMismatchError,
    DownloadInterruptedError,
)
from imgpuller.registry.client import RegistryClient

logger = logging.getLogger(__name__)

# Chunk size for downloads: 1 MB
DEFAULT_CHUNK_SIZE = 1024 * 1024

# How often to save progress state (in bytes)
STATE_SAVE_INTERVAL = 10 * 1024 * 1024  # 10 MB


class BlobDownloadWorker:
    """Downloads a single blob with resume and integrity verification."""

    def __init__(
        self,
        client: RegistryClient,
        image_name: str,
        digest: str,
        output_path: Path,
        state: DownloadState,
        expected_size: int | None = None,
        verify: bool = True,
        progress_callback: Callable[[int], None] | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ):
        """Initialize the worker.

        Args:
            client: Registry HTTP client.
            image_name: Image name (e.g. "library/ubuntu").
            digest: Blob digest (e.g. "sha256:abc...").
            output_path: Final destination path in blobs/sha256/.
            state: DownloadState manager.
            expected_size: Expected blob size from manifest.
            verify: Whether to verify SHA256 after download.
            progress_callback: Called with bytes downloaded since last call.
            chunk_size: Download chunk size in bytes.
        """
        self.client = client
        self.image_name = image_name
        self.digest = digest
        self.output_path = Path(output_path)
        self.state = state
        self.expected_size = expected_size
        self.verify = verify
        self.progress_callback = progress_callback or (lambda n: None)
        self.chunk_size = chunk_size

    @property
    def short_digest(self) -> str:
        """Short digest for display."""
        return self.digest[:19] if len(self.digest) > 19 else self.digest

    async def download(self) -> bool:
        """Download the blob with resume support.

        Returns:
            True if download and verification succeeded.

        Raises:
            DigestMismatchError: If SHA256 verification fails.
            DownloadInterruptedError: If download is interrupted.
        """
        # Setup paths
        temp_dir = Path(tempfile.gettempdir()) / "imgpuller"
        temp_dir.mkdir(parents=True, exist_ok=True)

        # Use output directory's parent for temp files (same filesystem)
        # to ensure atomic rename works
        temp_path = self.output_path.with_suffix(
            self.output_path.suffix + ".partial"
        )

        # Determine starting offset
        offset = 0
        state_data = self.state.read_blob_state(self.digest)

        if state_data and temp_path.exists():
            # Use actual file size, not saved state (process could have been
            # killed before saving state)
            actual_size = temp_path.stat().st_size

            if actual_size > 0:
                offset = actual_size
                logger.info(
                    "Resuming %s from byte %d",
                    self.short_digest, offset,
                )
            else:
                # Empty temp file, start from scratch
                temp_path.unlink(missing_ok=True)

        mode = "ab" if offset > 0 else "wb"

        try:
            # Open temp file
            with open(temp_path, mode) as fileobj:
                import hashlib

                hasher = hashlib.sha256()

                # If resuming, pre-hash existing content
                if offset > 0:
                    logger.debug(
                        "Pre-hashing %d existing bytes for %s",
                        offset, self.short_digest,
                    )
                    with open(temp_path, "rb") as existing:
                        while True:
                            chunk = existing.read(self.chunk_size)
                            if not chunk:
                                break
                            hasher.update(chunk)

                # Track bytes for periodic state saves
                bytes_since_last_save = 0
                total_written = offset

                # Stream from registry
                async for chunk in self.client.get_blob(
                    self.image_name, self.digest, offset=offset,
                ):
                    fileobj.write(chunk)
                    hasher.update(chunk)

                    chunk_len = len(chunk)
                    total_written += chunk_len
                    bytes_since_last_save += chunk_len

                    # Progress callback
                    self.progress_callback(chunk_len)

                    # Periodic state save
                    if bytes_since_last_save >= STATE_SAVE_INTERVAL:
                        await self._save_progress(
                            total_written, temp_path, bytes_since_last_save
                        )
                        bytes_since_last_save = 0

                # Final state save
                if bytes_since_last_save > 0:
                    await self._save_progress(
                        total_written, temp_path, bytes_since_last_save
                    )

                expected_digest = self.digest
                actual_digest = f"sha256:{hasher.hexdigest()}"

                # Verify
                if self.verify and actual_digest != expected_digest:
                    logger.error(
                        "Digest mismatch for %s: expected %s, got %s",
                        self.short_digest, expected_digest, actual_digest,
                    )
                    temp_path.unlink(missing_ok=True)
                    self.state.delete_blob_state(self.digest)
                    raise DigestMismatchError(
                        f"Digest verification failed for {self.short_digest}: "
                        f"expected {expected_digest}, got {actual_digest}"
                    )

                # Move to final location (atomic on same filesystem)
                self.output_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path.rename(self.output_path)

                # Clean up state
                self.state.delete_blob_state(self.digest)

                logger.info(
                    "Downloaded %s (%d bytes)",
                    self.short_digest, total_written,
                )

                return True

        except DigestMismatchError:
            raise

        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # Save progress for resume
            if temp_path.exists():
                sz = temp_path.stat().st_size
                await self._save_progress(sz, temp_path, 0, force=True)
            raise DownloadInterruptedError(
                f"Download interrupted for {self.short_digest}: {e}"
            ) from e

        except (asyncio.CancelledError, KeyboardInterrupt):
            # Save progress before cancellation so resume works
            if temp_path.exists():
                sz = temp_path.stat().st_size
                await self._save_progress(sz, temp_path, 0, force=True)
            raise

        except Exception:
            # Don't save state for unexpected errors - might be corrupt
            temp_path.unlink(missing_ok=True)
            self.state.delete_blob_state(self.digest)
            raise

    async def _save_progress(
        self,
        total_written: int,
        temp_path: Path,
        bytes_since_last_save: int,
        force: bool = False,
    ) -> None:
        """Save download progress to state file.

        Args:
            total_written: Total bytes written so far.
            temp_path: Path to temp file.
            bytes_since_last_save: Bytes since last save (for logging).
            force: Force save even if 0 bytes.
        """
        if bytes_since_last_save == 0 and not force:
            return

        # Run in thread to avoid blocking
        await asyncio.to_thread(
            self.state.save_blob_state,
            self.digest,
            total_written,
            temp_path,
            self.expected_size,
        )
