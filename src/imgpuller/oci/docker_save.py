"""Docker save (docker-archive) format writer.

Produces a single .tar file compatible with `docker load -i <file>.tar`,
matching the layout emitted by `docker save`.

    <output>.tar
    ├── manifest.json        [{"Config","RepoTags","Layers"}]
    ├── repositories         {"repo": {"tag": "<top-chain-id>"}}
    ├── <config-hex>.json    image config blob
    └── <chain-id>/
        ├── layer.tar        uncompressed layer tar
        ├── VERSION          "1.0"
        └── json             legacy layer metadata

Layer chain IDs are derived from the uncompressed diff IDs found in the
image config's ``rootfs.diff_ids``. Downloaded layer blobs (gzip-compressed
tar) are decompressed before being stored as ``layer.tar`` and their
uncompressed SHA256 is verified against the declared diff ID.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import tarfile
from pathlib import Path
from typing import BinaryIO

from imgpuller.config import ImageReference, DEFAULT_REGISTRY, DEFAULT_NAMESPACE
from imgpuller.exceptions import OCILayoutError
from imgpuller.manifest.resolver import ResolvedImage
from imgpuller.verification.hasher import compute_bytes_digest

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024  # 1 MB


def _hex(digest: str) -> str:
    """Return the hex portion of a 'sha256:...' digest."""
    return digest.split(":", 1)[1] if ":" in digest else digest


def compute_chain_ids(diff_ids_hex: list[str]) -> list[str]:
    """Compute OCI/Docker chain IDs from a list of diff ID hex values.

    chain_id[0] = diff_id[0]
    chain_id[n] = sha256(chain_id[n-1] + " " + diff_id[n])

    Args:
        diff_ids_hex: List of uncompressed layer SHA256 hex values.

    Returns:
        List of chain ID hex values (one per layer).
    """
    chain_ids: list[str] = []
    parent: str | None = None
    for diff_hex in diff_ids_hex:
        if parent is None:
            chain = diff_hex
        else:
            chain = hashlib.sha256(
                f"{parent} {diff_hex}".encode()
            ).hexdigest()
        chain_ids.append(chain)
        parent = chain
    return chain_ids


def _repo_full_name(image_ref: ImageReference) -> str:
    """Repository name used in the repositories map (no tag, no scheme)."""
    if image_ref.registry == DEFAULT_REGISTRY:
        return image_ref.name  # e.g. "library/alpine"
    return f"{image_ref.registry}/{image_ref.name}"


def _repo_tag(image_ref: ImageReference) -> str | None:
    """Repo:tag string for manifest.json RepoTags, or None for digest refs."""
    if image_ref.is_digest:
        return None
    if image_ref.registry == DEFAULT_REGISTRY:
        # Docker Hub: drop the library/ namespace prefix for display.
        name = image_ref.name
        if name.startswith(f"{DEFAULT_NAMESPACE}/"):
            name = name[len(DEFAULT_NAMESPACE) + 1:]
        return f"{name}:{image_ref.reference}"
    return f"{image_ref.registry}/{image_ref.name}:{image_ref.reference}"


def _decompress_layer(blob_path: Path, media_type: str, out: BinaryIO) -> str:
    """Stream-decompress a layer blob into ``out`` and return its SHA256 hex.

    Args:
        blob_path: Path to the downloaded (possibly compressed) layer blob.
        media_type: Layer media type from the manifest.
        out: Open binary file object to receive the uncompressed layer tar.

    Returns:
        SHA256 hex of the uncompressed layer content (the diff ID).

    Raises:
        OCILayoutError: If the media type is unsupported.
    """
    h = hashlib.sha256()

    if media_type.endswith("+gzip") or media_type.endswith(".gzip") or "+gzip" in media_type:
        opener = gzip.open
    elif media_type.endswith("+tar") or media_type == "application/vnd.docker.image.rootfs.diff.tar":
        # Already an uncompressed tar.
        opener = open
    elif "zstd" in media_type:
        raise OCILayoutError(
            f"Unsupported layer media type (zstd not supported): {media_type}"
        )
    else:
        # Best-effort: try gzip, fall back to raw.
        try:
            with gzip.open(blob_path, "rb") as f:
                if f.read(2):  # peek to validate gzip header
                    opener = gzip.open
                else:
                    opener = open
        except (OSError, gzip.BadGzipFile):
            opener = open

    with opener(blob_path, "rb") as src:
        while True:
            chunk = src.read(CHUNK_SIZE)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)

    return h.hexdigest()


class DockerSaveWriter:
    """Writes a Docker save (.tar) archive from downloaded blobs."""

    def __init__(self, output_tar: Path):
        """Initialize the writer.

        Args:
            output_tar: Destination .tar file path.
        """
        self.output_tar = Path(output_tar)

    def write(
        self,
        resolved: ResolvedImage,
        blobs_dir: Path,
        image_ref: ImageReference,
    ) -> Path:
        """Write the complete docker-archive tar.

        Args:
            resolved: Resolved image with manifest and downloaded blobs.
            blobs_dir: Directory containing downloaded blobs
                       (blobs/sha256/<hex>).
            image_ref: Original image reference for tagging.

        Returns:
            Path to the written .tar file.

        Raises:
            OCILayoutError: If blobs are missing, corrupt, or diff IDs
                            don't match.
        """
        blobs_dir = Path(blobs_dir)
        config_digest = resolved.config_digest
        config_hex = _hex(config_digest)
        config_blob = blobs_dir / config_hex

        if not config_blob.exists():
            raise OCILayoutError(
                f"Config blob not found: {config_blob}"
            )

        config_bytes = config_blob.read_bytes()
        try:
            config_data = json.loads(config_bytes)
        except json.JSONDecodeError as e:
            raise OCILayoutError(f"Invalid config JSON: {e}") from e

        # Verify config blob digest matches its filename.
        actual_config_digest = compute_bytes_digest(config_bytes)
        if actual_config_digest != config_digest:
            raise OCILayoutError(
                f"Config blob digest mismatch: expected {config_digest}, "
                f"got {actual_config_digest}"
            )

        diff_ids = config_data.get("rootfs", {}).get("diff_ids", [])
        if len(diff_ids) != len(resolved.layer_digests):
            raise OCILayoutError(
                f"diff_ids count ({len(diff_ids)}) != layer count "
                f"({len(resolved.layer_digests)})"
            )

        diff_ids_hex = [_hex(d) for d in diff_ids]
        chain_ids = compute_chain_ids(diff_ids_hex)

        # Build manifest.json + repositories metadata.
        repo_tag = _repo_tag(image_ref)
        layer_members = [f"{cid}/layer.tar" for cid in chain_ids]

        manifest_entry = {
            "Config": f"{config_hex}.json",
            "RepoTags": [repo_tag] if repo_tag else None,
            "Layers": layer_members,
        }
        manifest_json = json.dumps([manifest_entry], separators=(",", ":")).encode()

        top_chain_id = chain_ids[-1] if chain_ids else ""
        repo_full = _repo_full_name(image_ref)
        if repo_tag and ":" in repo_tag:
            tag = repo_tag.rsplit(":", 1)[1]
            repositories = {repo_full: {tag: top_chain_id}}
        else:
            repositories = {}
        repositories_json = json.dumps(repositories, separators=(",", ":")).encode()

        # Write tar atomically via a .partial file.
        self.output_tar.parent.mkdir(parents=True, exist_ok=True)
        partial = self.output_tar.with_suffix(self.output_tar.suffix + ".partial")

        try:
            with tarfile.open(partial, "w") as tar:
                self._add_bytes(tar, "manifest.json", manifest_json)
                self._add_bytes(tar, "repositories", repositories_json)
                self._add_bytes(tar, f"{config_hex}.json", config_bytes)

                for i, layer_digest in enumerate(resolved.layer_digests):
                    layer_hex = _hex(layer_digest)
                    blob_path = blobs_dir / layer_hex
                    if not blob_path.exists():
                        raise OCILayoutError(
                            f"Layer blob not found: {blob_path}"
                        )

                    media_type = resolved.manifest.layers[i].media_type
                    chain_id = chain_ids[i]
                    expected_diff_hex = diff_ids_hex[i]

                    # Decompress into memory-efficient buffer and verify.
                    buf = io.BytesIO()
                    actual_diff_hex = _decompress_layer(
                        blob_path, media_type, buf
                    )
                    if actual_diff_hex != expected_diff_hex:
                        raise OCILayoutError(
                            f"Layer {layer_hex[:19]} diff ID mismatch: "
                            f"expected sha256:{expected_diff_hex}, "
                            f"got sha256:{actual_diff_hex}"
                        )

                    layer_bytes = buf.getvalue()
                    self._add_bytes(
                        tar, f"{chain_id}/layer.tar", layer_bytes
                    )
                    self._add_bytes(tar, f"{chain_id}/VERSION", b"1.0")
                    self._add_bytes(
                        tar,
                        f"{chain_id}/json",
                        json.dumps({"id": chain_id}).encode(),
                    )
                    logger.debug(
                        "Wrote layer %d/%d (%s, %d bytes)",
                        i + 1, len(resolved.layer_digests),
                        chain_id[:12], len(layer_bytes),
                    )

            partial.replace(self.output_tar)
        except (OSError, tarfile.TarError) as e:
            partial.unlink(missing_ok=True)
            raise OCILayoutError(f"Failed to write docker save tar: {e}") from e

        logger.info(
            "Docker save archive written to %s (%d layers)",
            self.output_tar, len(resolved.layer_digests),
        )
        return self.output_tar

    @staticmethod
    def _add_bytes(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
        """Add an in-memory bytes blob as a member to the tar."""
        info = tarfile.TarInfo(name=arcname)
        info.size = len(data)
        info.mtime = 0
        info.mode = 0o644
        info.type = tarfile.REGTYPE
        tar.addfile(info, io.BytesIO(data))

    def verify(self) -> dict:
        """Verify a docker save tar archive's integrity.

        Checks:
        - manifest.json exists and is valid
        - config file exists and its SHA256 matches its filename
        - each layer.tar SHA256 matches the config's diff_ids
        - chain IDs derived from diff IDs match the layer directory names

        Returns:
            Dict with {status, checked, valid, errors}.
        """
        result = {"status": "ok", "checked": 0, "valid": 0, "errors": []}

        if not self.output_tar.exists():
            result["errors"].append(f"File not found: {self.output_tar}")
            result["status"] = "error"
            return result

        try:
            with tarfile.open(self.output_tar, "r") as tar:
                members = {m.name: m for m in tar.getmembers()}

                if "manifest.json" not in members:
                    result["errors"].append("Missing manifest.json")
                    result["status"] = "error"
                    return result

                manifest = json.loads(
                    tar.extractfile("manifest.json").read()
                )
                if not manifest or not isinstance(manifest, list):
                    result["errors"].append("Invalid manifest.json")
                    result["status"] = "error"
                    return result

                entry = manifest[0]
                config_name = entry.get("Config", "")
                layer_paths = entry.get("Layers", [])

                # Verify config blob.
                if config_name not in members:
                    result["errors"].append(f"Missing config: {config_name}")
                    result["status"] = "error"
                    return result

                config_bytes = tar.extractfile(config_name).read()
                config_hex = Path(config_name).stem
                result["checked"] += 1
                if f"sha256:{hashlib.sha256(config_bytes).hexdigest()}" == f"sha256:{config_hex}":
                    result["valid"] += 1
                else:
                    result["errors"].append(f"Config digest mismatch: {config_name}")

                config_data = json.loads(config_bytes)
                diff_ids = config_data.get("rootfs", {}).get("diff_ids", [])

                if len(diff_ids) != len(layer_paths):
                    result["errors"].append(
                        f"diff_ids count ({len(diff_ids)}) != "
                        f"layers ({len(layer_paths)})"
                    )
                    result["status"] = "error"
                    return result

                expected_chain_ids = compute_chain_ids(
                    [_hex(d) for d in diff_ids]
                )

                for i, layer_path in enumerate(layer_paths):
                    result["checked"] += 1
                    expected_diff_hex = _hex(diff_ids[i])
                    expected_chain = expected_chain_ids[i]

                    # Directory name should be the chain id.
                    dir_name = layer_path.rsplit("/", 1)[0] if "/" in layer_path else ""
                    if dir_name != expected_chain:
                        result["errors"].append(
                            f"Layer {i} chain id mismatch: "
                            f"dir {dir_name[:12]} != expected {expected_chain[:12]}"
                        )

                    if layer_path not in members:
                        result["errors"].append(f"Missing layer: {layer_path}")
                        continue

                    h = hashlib.sha256()
                    f = tar.extractfile(layer_path)
                    if f is None:
                        result["errors"].append(f"Cannot read layer: {layer_path}")
                        continue
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        h.update(chunk)
                    actual = h.hexdigest()
                    if actual == expected_diff_hex:
                        result["valid"] += 1
                    else:
                        result["errors"].append(
                            f"Layer {i} diff id mismatch: "
                            f"expected sha256:{expected_diff_hex[:19]}, "
                            f"got sha256:{actual[:19]}"
                        )

        except (tarfile.TarError, json.JSONDecodeError, OSError) as e:
            result["errors"].append(f"Failed to read archive: {e}")
            result["status"] = "error"

        if result["errors"]:
            result["status"] = "error"

        return result
