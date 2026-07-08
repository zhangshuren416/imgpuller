"""Parallel download manager.

Orchestrates downloading multiple blobs (config + layers) concurrently
with semaphore-based concurrency control, progress tracking, and retry logic.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from imgpuller.download.state import DownloadState
from imgpuller.download.worker import BlobDownloadWorker
from imgpuller.exceptions import (
    DigestMismatchError,
    DownloadError,
    DownloadInterruptedError,
)
from imgpuller.manifest.resolver import ResolvedImage
from imgpuller.registry.client import RegistryClient
from imgpuller.verification.hasher import verify_file

logger = logging.getLogger(__name__)

# Maximum retries per blob
MAX_RETRIES = 3

# Base delay for retry backoff (seconds)
RETRY_BASE_DELAY = 2.0


class DownloadManager:
    """Orchestrates parallel blob downloads with progress tracking."""

    def __init__(
        self,
        client: RegistryClient,
        image_name: str,
        output_dir: Path,
        concurrency: int = 4,
        verify: bool = True,
    ):
        """Initialize the download manager.

        Args:
            client: Registry HTTP client.
            image_name: Image name for blob URLs.
            output_dir: OCI layout output directory.
            concurrency: Max parallel downloads.
            verify: Whether to verify SHA256 digests.
        """
        self.client = client
        self.image_name = image_name
        self.output_dir = Path(output_dir)
        self.blobs_dir = self.output_dir / "blobs" / "sha256"
        self.state = DownloadState(output_dir)
        self.concurrency = max(1, min(concurrency, 16))
        self.verify = verify

    async def download_all(
        self,
        resolved: ResolvedImage,
        progress: Progress | None = None,
    ) -> bool:
        """Download all blobs for a resolved image.

        Args:
            resolved: ResolvedImage with all blob references.
            progress: Rich Progress instance for display.

        Returns:
            True if all downloads succeeded.

        Raises:
            DownloadError: If any blob fails after retries.
        """
        all_digests = resolved.all_blob_digests

        # Create output directories
        self.blobs_dir.mkdir(parents=True, exist_ok=True)

        # Filter out already-completed blobs
        completed = self.state.get_completed_digests()
        pending = []

        for digest in all_digests:
            blob_path = self._blob_path(digest)

            # Check if already downloaded and valid
            if blob_path.exists():
                if not self.verify:
                    completed.add(digest)
                    continue
                try:
                    if verify_file(blob_path, digest):
                        completed.add(digest)
                        logger.debug("Blob already valid: %s", digest[:19])
                        continue
                    else:
                        # Corrupt, re-download
                        logger.warning(
                            "Corrupt blob detected, re-downloading: %s",
                            digest[:19],
                        )
                        blob_path.unlink()
                        self.state.delete_blob_state(digest)
                except Exception as e:
                    logger.warning(
                        "Failed to verify existing blob %s: %s",
                        digest[:19], e,
                    )

            pending.append(digest)

        if not pending:
            logger.info("All %d blobs already downloaded and verified", len(all_digests))
            if progress:
                progress.log("[green]All blobs already downloaded ✓")
            return True

        logger.info(
            "Downloading %d blobs (%d already complete)",
            len(pending), len(completed),
        )

        # Setup progress tracking
        own_progress = progress is None
        if own_progress:
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                transient=False,
            )

        try:
            with progress:
                overall_task = progress.add_task(
                    f"[bold]Downloading {len(pending)} blobs",
                    total=len(pending),
                )

                semaphore = asyncio.Semaphore(self.concurrency)

                async def download_one(digest: str, index: int) -> tuple[str, bool]:
                    """Download a single blob with retries."""
                    async with semaphore:
                        blob_path = self._blob_path(digest)
                        layer_task = progress.add_task(
                            f"  {digest[:19]}...",
                            total=None,  # indeterminate until we know size
                        )

                        worker = BlobDownloadWorker(
                            client=self.client,
                            image_name=self.image_name,
                            digest=digest,
                            output_path=blob_path,
                            state=self.state,
                            verify=self.verify,
                            progress_callback=lambda n: progress.update(
                                layer_task, advance=n
                            ),
                        )

                        for attempt in range(MAX_RETRIES):
                            try:
                                success = await worker.download()
                                if success:
                                    progress.update(
                                        layer_task,
                                        description=f"  [green]✓[/] {digest[:19]}",
                                        completed=True,
                                    )
                                    progress.advance(overall_task)
                                    return digest, True
                            except DigestMismatchError:
                                # Don't retry - data is wrong server-side
                                progress.update(
                                    layer_task,
                                    description=f"  [red]✗[/] {digest[:19]} (BAD DIGEST)",
                                )
                                progress.advance(overall_task)
                                raise
                            except DownloadInterruptedError as e:
                                if attempt < MAX_RETRIES - 1:
                                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                                    progress.update(
                                        layer_task,
                                        description=f"  [yellow]↻[/] {digest[:19]} retry in {delay:.0f}s",
                                    )
                                    await asyncio.sleep(delay)
                                else:
                                    progress.update(
                                        layer_task,
                                        description=f"  [red]✗[/] {digest[:19]} (FAILED)",
                                    )
                                    progress.advance(overall_task)
                                    raise DownloadError(
                                        f"Blob {digest[:19]} failed after {MAX_RETRIES} attempts: {e}"
                                    ) from e
                            except Exception as e:
                                progress.update(
                                    layer_task,
                                    description=f"  [red]✗[/] {digest[:19]} ({type(e).__name__})",
                                )
                                progress.advance(overall_task)
                                raise

                        return digest, False

                # Run all downloads with concurrency limit
                results = await asyncio.gather(
                    *[download_one(d, i) for i, d in enumerate(pending)],
                    return_exceptions=True,
                )

                # Check results
                failures = []
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        failures.append((pending[i], result))

                if failures:
                    error_msg_parts = ["Download failed for some blobs:"]
                    for digest, exc in failures:
                        error_msg_parts.append(
                            f"  {digest[:19]}: {exc}"
                        )
                    raise DownloadError("\n".join(error_msg_parts))

                # Save overall state
                self.state.save_overall_state(
                    image_ref=f"{self.image_name}",
                    completed_digests=list(all_digests),
                )

                progress.log("[green]✓ All blobs downloaded and verified")
                return True

        finally:
            pass  # Progress context manager handles cleanup

    def _blob_path(self, digest: str) -> Path:
        """Get the final blob path for a digest.

        Args:
            digest: Blob digest (e.g. "sha256:abc...").

        Returns:
            Path in blobs/sha256/<hex>.
        """
        if ":" in digest:
            hex_part = digest.split(":", 1)[1]
        else:
            hex_part = digest
        return self.blobs_dir / hex_part

    def cleanup_state(self) -> None:
        """Clean up all resume state after successful download."""
        self.state.clear_all()
