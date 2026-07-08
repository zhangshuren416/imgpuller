"""OCI layout generation.

Creates the standard OCI image layout directory structure:

<output>/
├── oci-layout          {"imageLayoutVersion": "1.0.0"}
├── index.json           Multi-image index pointing to manifest
└── blobs/sha256/
    ├── <manifest-digest>     Manifest JSON blob
    ├── <config-digest>       Image config JSON blob
    └── <layer-digest>        Layer files (tar.gz)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from imgpuller.exceptions import OCILayoutError
from imgpuller.manifest.resolver import ResolvedImage, compute_digest
from imgpuller.verification.hasher import verify_file

logger = logging.getLogger(__name__)

OCI_LAYOUT_VERSION = "1.0.0"
INDEX_SCHEMA_VERSION = 2


class OCILayoutWriter:
    """Writes OCI layout directory from downloaded blobs."""

    def __init__(self, output_dir: Path):
        """Initialize the writer.

        Args:
            output_dir: Root directory for OCI layout.
        """
        self.output_dir = Path(output_dir)
        self.blobs_dir = self.output_dir / "blobs" / "sha256"

    def write(
        self,
        resolved: ResolvedImage,
        image_ref: str = "",
    ) -> Path:
        """Write complete OCI layout.

        Args:
            resolved: ResolvedImage with manifest and downloaded blobs.
            image_ref: Original image reference for annotations.

        Returns:
            Path to the output directory.

        Raises:
            OCILayoutError: If writing fails.
        """
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.blobs_dir.mkdir(parents=True, exist_ok=True)

            # 1. Write oci-layout file
            self._write_oci_layout()

            # 2. Write manifest blob
            manifest_digest = self._write_manifest_blob(
                resolved.manifest.raw_bytes
            )

            # 3. Write index.json
            self._write_index(
                manifest_digest=manifest_digest,
                manifest_size=len(resolved.manifest.raw_bytes),
                manifest_media_type=resolved.manifest.media_type,
                platform=resolved.platform,
                image_ref=image_ref,
                annotations=resolved.annotations,
            )

            logger.info("OCI layout written to %s", self.output_dir)
            return self.output_dir

        except (OSError, json.JSONDecodeError) as e:
            raise OCILayoutError(f"Failed to write OCI layout: {e}") from e

    def _write_oci_layout(self) -> None:
        """Write the oci-layout file."""
        layout = {"imageLayoutVersion": OCI_LAYOUT_VERSION}
        path = self.output_dir / "oci-layout"
        with open(path, "w") as f:
            json.dump(layout, f)
            f.write("\n")
        logger.debug("Wrote oci-layout")

    def _write_manifest_blob(self, manifest_bytes: bytes) -> str:
        """Write the manifest as a blob and return its digest.

        The manifest is stored as a blob so the OCI layout is self-contained.
        """
        digest = compute_digest(manifest_bytes, "sha256")
        hex_digest = digest.split(":", 1)[1]
        blob_path = self.blobs_dir / hex_digest

        if not blob_path.exists():
            # Manifest data is small; write inline
            with open(blob_path, "wb") as f:
                f.write(manifest_bytes)

        logger.debug("Wrote manifest blob: %s", digest[:19])
        return digest

    def _write_index(
        self,
        manifest_digest: str,
        manifest_size: int,
        manifest_media_type: str,
        platform,
        image_ref: str = "",
        annotations: dict | None = None,
    ) -> None:
        """Write the index.json file."""
        index_entry = {
            "mediaType": manifest_media_type,
            "digest": manifest_digest,
            "size": manifest_size,
            "platform": {
                "architecture": platform.architecture,
                "os": platform.os,
            },
        }

        if platform.variant:
            index_entry["platform"]["variant"] = platform.variant

        # Combine annotations
        merged_annotations = dict(annotations or {})
        if image_ref:
            merged_annotations.setdefault(
                "org.opencontainers.image.ref.name", image_ref
            )

        if merged_annotations:
            index_entry["annotations"] = merged_annotations

        index = {
            "schemaVersion": INDEX_SCHEMA_VERSION,
            "manifests": [index_entry],
        }

        path = self.output_dir / "index.json"
        with open(path, "w") as f:
            json.dump(index, f, indent=2)
            f.write("\n")

        logger.debug("Wrote index.json with %d manifest(s)", 1)

    def verify(self) -> dict:
        """Verify the OCI layout integrity.

        Checks:
        - oci-layout file exists and has correct version
        - index.json exists and is valid
        - All referenced blobs exist and match their digests

        Returns:
            Dict with verification results: {status, checked, valid, errors}
        """
        result = {
            "status": "ok",
            "checked": 0,
            "valid": 0,
            "errors": [],
        }

        # Check oci-layout
        oci_layout_path = self.output_dir / "oci-layout"
        if not oci_layout_path.exists():
            result["errors"].append("Missing oci-layout file")
            result["status"] = "error"
            return result

        try:
            with open(oci_layout_path, "r") as f:
                layout = json.load(f)
            if layout.get("imageLayoutVersion") != OCI_LAYOUT_VERSION:
                result["errors"].append(
                    f"Unsupported layout version: {layout.get('imageLayoutVersion')}"
                )
                result["status"] = "error"
        except Exception as e:
            result["errors"].append(f"Invalid oci-layout: {e}")
            result["status"] = "error"
            return result

        # Check index.json
        index_path = self.output_dir / "index.json"
        if not index_path.exists():
            result["errors"].append("Missing index.json")
            result["status"] = "error"
            return result

        try:
            with open(index_path, "r") as f:
                index = json.load(f)

            for manifest_entry in index.get("manifests", []):
                manifest_digest = manifest_entry.get("digest", "")
                result["checked"] += 1
                hex_d = manifest_digest.split(":", 1)[-1] if ":" in manifest_digest else manifest_digest
                manifest_path = self.blobs_dir / hex_d
                if not manifest_path.exists():
                    result["errors"].append(f"Missing manifest blob: {manifest_digest[:19]}")
                    result["status"] = "error"
                elif manifest_digest:
                    try:
                        if verify_file(manifest_path, manifest_digest):
                            result["valid"] += 1
                        else:
                            result["errors"].append(f"Corrupt manifest blob: {manifest_digest[:19]}")
                            result["status"] = "error"
                    except Exception as e:
                        result["errors"].append(f"Failed to verify manifest: {e}")

            # Check all blobs in directory
            for blob_path in self.blobs_dir.iterdir():
                if blob_path.is_file() and blob_path.name != hex_d:
                    result["checked"] += 1
                    expected_digest = f"sha256:{blob_path.name}"
                    try:
                        if verify_file(blob_path, expected_digest):
                            result["valid"] += 1
                        else:
                            result["errors"].append(f"Corrupt blob: {blob_path.name[:19]}")
                            result["status"] = "error"
                    except Exception as e:
                        result["errors"].append(f"Cannot verify {blob_path.name[:19]}: {e}")

        except json.JSONDecodeError as e:
            result["errors"].append(f"Invalid index.json: {e}")
            result["status"] = "error"

        return result
