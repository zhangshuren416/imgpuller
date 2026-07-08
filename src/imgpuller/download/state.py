"""Resume state persistence for blob downloads.

Each blob has a JSON state file in .imgpuller-state/ that records:
- digest: The blob's expected digest
- temp_path: Path to the partial download file
- completed_bytes: Bytes already downloaded
- expected_size: Expected total size (from manifest)

On resume, we read the state and use HTTP Range: bytes={offset}- to continue.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATE_DIR_NAME = ".imgpuller-state"
OVERALL_STATE_FILE = "overall.json"


def _sanitize_filename(digest: str) -> str:
    """Convert a digest to a safe filename."""
    return digest.replace(":", "-").replace("/", "-")


class DownloadState:
    """Manages resume state for blob downloads."""

    def __init__(self, output_dir: Path):
        """Initialize state manager.

        Args:
            output_dir: The OCI layout output directory.
        """
        self.output_dir = Path(output_dir)
        self.state_dir = self.output_dir / STATE_DIR_NAME

    def ensure_state_dir(self) -> None:
        """Create the state directory if it doesn't exist."""
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # -- Per-blob state --

    def save_blob_state(
        self,
        digest: str,
        completed_bytes: int,
        temp_path: Path,
        expected_size: int | None = None,
        retry_count: int = 0,
    ) -> None:
        """Save progress for a single blob.

        Args:
            digest: Blob digest (e.g. "sha256:abc...").
            completed_bytes: Bytes downloaded so far.
            temp_path: Path to the temporary download file.
            expected_size: Expected total size from manifest.
            retry_count: Number of retry attempts.
        """
        self.ensure_state_dir()

        state = {
            "digest": digest,
            "temp_path": str(temp_path),
            "completed_bytes": completed_bytes,
            "expected_size": expected_size,
            "retry_count": retry_count,
            "last_modified": datetime.now(timezone.utc).isoformat(),
        }

        state_file = self.state_dir / f"{_sanitize_filename(digest)}.json"

        try:
            # Write atomically: write to temp, then rename
            tmp_file = state_file.with_suffix(".tmp")
            with open(tmp_file, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_file, state_file)
        except OSError as e:
            logger.warning("Failed to save blob state for %s: %s", digest[:19], e)

    def read_blob_state(
        self, digest: str
    ) -> dict | None:
        """Read saved state for a blob.

        Args:
            digest: Blob digest.

        Returns:
            State dict or None if no state exists.
        """
        state_file = self.state_dir / f"{_sanitize_filename(digest)}.json"

        if not state_file.exists():
            return None

        try:
            with open(state_file, "r") as f:
                state = json.load(f)
            return state
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Failed to read blob state for %s: %s", digest[:19], e)
            return None

    def delete_blob_state(self, digest: str) -> None:
        """Delete state for a completed/cancelled blob.

        Args:
            digest: Blob digest.
        """
        state_file = self.state_dir / f"{_sanitize_filename(digest)}.json"
        try:
            state_file.unlink(missing_ok=True)
        except OSError:
            pass

    # -- Overall state --

    def save_overall_state(
        self,
        image_ref: str,
        completed_digests: list[str],
        platform: str = "",
    ) -> None:
        """Save overall download progress.

        Args:
            image_ref: The original image reference string.
            completed_digests: List of completed blob digests.
            platform: Target platform string.
        """
        self.ensure_state_dir()

        state = {
            "image_ref": image_ref,
            "platform": platform,
            "completed_digests": completed_digests,
            "last_modified": datetime.now(timezone.utc).isoformat(),
            "timestamp": time.time(),
        }

        state_file = self.state_dir / OVERALL_STATE_FILE

        try:
            tmp_file = state_file.with_suffix(".tmp")
            with open(tmp_file, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_file, state_file)
        except OSError as e:
            logger.warning("Failed to save overall state: %s", e)

    def read_overall_state(self) -> dict | None:
        """Read the overall download progress.

        Returns:
            Overall state dict or None.
        """
        state_file = self.state_dir / OVERALL_STATE_FILE

        if not state_file.exists():
            return None

        try:
            with open(state_file, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def get_completed_digests(self) -> set[str]:
        """Get the set of completed blob digests.

        Returns:
            Set of digest strings.
        """
        state = self.read_overall_state()
        if state is None:
            return set()
        return set(state.get("completed_digests", []))

    def clear_all(self) -> None:
        """Remove all state files."""
        if not self.state_dir.exists():
            return

        for f in self.state_dir.iterdir():
            try:
                f.unlink()
            except OSError:
                pass

        try:
            self.state_dir.rmdir()
        except OSError:
            pass

    def has_state(self) -> bool:
        """Check if any resume state exists."""
        if not self.state_dir.exists():
            return False
        return bool(list(self.state_dir.glob("*.json")))
