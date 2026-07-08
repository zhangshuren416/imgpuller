"""Parallel download manager.

Orchestrates downloading multiple blobs (config + layers) concurrently
with semaphore-based concurrency control, progress tracking, and retry logic.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
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

        # Map each blob digest to its declared size from the manifest so the
        # progress bars can show concrete totals (bytes downloaded / total).
        size_map: dict[str, int] = {
            resolved.config_digest: resolved.manifest.config.size,
        }
        for layer in resolved.manifest.layers:
            size_map[layer.digest] = layer.size

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
                            "Corrupt blob %s, re-downloading", digest[:19],
                        )
                        blob_path.unlink()
                        self.state.delete_blob_state(digest)
                except Exception as e:
                    logger.warning(
                        "Verify failed for %s: %s", digest[:19], e,
                    )

            pending.append(digest)

        if not pending:
            logger.info("All %d blobs already present", len(all_digests))
            if progress:
                progress.log("[green]✓ Already up to date[/]")
            return True

        logger.info(
            "Downloading %d blobs (%d cached)",
            len(pending), len(completed),
        )

        # Setup progress tracking
        own_progress = progress is None
        if own_progress:
            progress = Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(bar_width=20),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                transient=False,
            )

        try:
            with progress:
                semaphore = asyncio.Semaphore(self.concurrency)

                async def download_one(digest: str, index: int) -> tuple[str, bool]:
                    """Download a single blob with retries."""
                    async with semaphore:
                        blob_path = self._blob_path(digest)
                        expected_size = size_map.get(digest) or None

                        # Compute the resume offset using the same condition
                        # as the worker (state + partial file present) so the
                        # progress bar start matches the real download offset.
                        temp_path = blob_path.with_suffix(
                            blob_path.suffix + ".partial"
                        )
                        state_data = self.state.read_blob_state(digest)
                        if state_data and temp_path.exists():
                            offset = temp_path.stat().st_size
                        else:
                            offset = 0

                        layer_task = progress.add_task(
                            f"{digest[:19]}: Downloading",
                            total=expected_size,
                            completed=offset if expected_size else 0,
                        )

                        worker = BlobDownloadWorker(
                            client=self.client,
                            image_name=self.image_name,
                            digest=digest,
                            output_path=blob_path,
                            state=self.state,
                            expected_size=expected_size,
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
                                        description=f"{digest[:19]}: [green]Pull complete[/]",
                                    )
                                    return digest, True
                            except DigestMismatchError:
                                # Don't retry - data is wrong server-side
                                progress.update(
                                    layer_task,
                                    description=f"{digest[:19]}: [red]BAD DIGEST[/]",
                                )
                                raise
                            except DownloadInterruptedError as e:
                                if attempt < MAX_RETRIES - 1:
                                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                                    progress.update(
                                        layer_task,
                                        description=f"{digest[:19]}: [yellow]retry in {delay:.0f}s[/]",
                                    )
                                    await asyncio.sleep(delay)
                                else:
                                    progress.update(
                                        layer_task,
                                        description=f"{digest[:19]}: [red]failed[/]",
                                    )
                                    raise DownloadError(
                                        f"Blob {digest[:19]} failed after {MAX_RETRIES} attempts: {e}"
                                    ) from e
                            except Exception as e:
                                progress.update(
                                    layer_task,
                                    description=f"{digest[:19]}: [red]{type(e).__name__}[/]",
                                )
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

                progress.log("[green]✓ All layers pulled[/]")
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
