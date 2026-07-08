"""Manifest parsing and platform-aware resolution.

Handles:
- OCI Image Index (multi-arch manifest list)
- Docker Manifest List V2
- OCI Image Manifest V1
- Docker Manifest V2

Resolves manifest lists to platform-specific manifests.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

from imgpuller.config import Platform, detect_current_platform
from imgpuller.exceptions import (
    PlatformNotFoundError,
)
from imgpuller.registry.client import RegistryClient

logger = logging.getLogger(__name__)

# Media type constants
MEDIA_TYPE_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MEDIA_TYPE_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MEDIA_TYPE_DOCKER_MANIFEST_LIST = (
    "application/vnd.docker.distribution.manifest.list.v2+json"
)
MEDIA_TYPE_DOCKER_MANIFEST = (
    "application/vnd.docker.distribution.manifest.v2+json"
)
MEDIA_TYPE_DOCKER_MANIFEST_V1 = (
    "application/vnd.docker.distribution.manifest.v1+prettyjws"
)

# Media types that indicate a multi-arch manifest list
MULTIARCH_MEDIA_TYPES = {MEDIA_TYPE_OCI_INDEX, MEDIA_TYPE_DOCKER_MANIFEST_LIST}

# Media types for single-image manifests
SINGLE_MANIFEST_MEDIA_TYPES = {
    MEDIA_TYPE_OCI_MANIFEST,
    MEDIA_TYPE_DOCKER_MANIFEST,
    MEDIA_TYPE_DOCKER_MANIFEST_V1,
}


@dataclass
class Descriptor:
    """A blob or manifest descriptor as used in OCI/Docker manifests."""

    media_type: str
    digest: str  # e.g. "sha256:abc123..."
    size: int
    urls: list[str] = field(default_factory=list)
    annotations: dict = field(default_factory=dict)
    platform: Platform | None = None

    @property
    def algorithm(self) -> str:
        """Extract the hash algorithm from the digest."""
        if ":" in self.digest:
            return self.digest.split(":", 1)[0]
        return "sha256"

    @property
    def hex_digest(self) -> str:
        """Extract the hex hash from the digest."""
        if ":" in self.digest:
            return self.digest.split(":", 1)[1]
        return self.digest


@dataclass
class ImageManifest:
    """A single-image manifest (OCI or Docker V2)."""

    schema_version: int
    media_type: str
    config: Descriptor
    layers: list[Descriptor]
    raw_bytes: bytes


@dataclass
class ManifestList:
    """A multi-arch manifest list (OCI Index or Docker Manifest List)."""

    schema_version: int
    media_type: str
    manifests: list[Descriptor]


@dataclass
class ResolvedImage:
    """A fully resolved image ready for download."""

    manifest: ImageManifest
    config_digest: str
    layer_digests: list[str]
    platform: Platform
    annotations: dict = field(default_factory=dict)

    @property
    def all_blob_digests(self) -> list[str]:
        """Return all blob digests (config + layers)."""
        return [self.config_digest] + self.layer_digests


class ManifestResolver:
    """Resolves image references to concrete manifests."""

    def __init__(self, client: RegistryClient):
        self.client = client

    async def resolve(
        self,
        name: str,
        reference: str,
        platform: Platform | None = None,
    ) -> ResolvedImage:
        """Resolve an image reference to a platform-specific manifest.

        Args:
            name: Image name (e.g. "library/ubuntu")
            reference: Tag or digest (e.g. "22.04")
            platform: Target platform. Uses current system if None.

        Returns:
            ResolvedImage with manifest, config digest, and layer digests.

        Raises:
            ManifestNotFoundError: If manifest is not found.
            PlatformNotFoundError: If no matching platform in manifest list.
            UnsupportedMediaTypeError: If manifest type is unrecognized.
        """
        if platform is None:
            platform = detect_current_platform()

        # Fetch manifest (may be manifest list or single manifest)
        response = await self.client.get_manifest(name, reference)
        manifest_bytes = response.content
        content_type = response.content_type

        logger.info(
            "Fetched manifest for %s:%s, type=%s, size=%d",
            name, reference, content_type, len(manifest_bytes),
        )

        # Parse the response
        manifest_data = json.loads(manifest_bytes)
        media_type = manifest_data.get("mediaType", content_type)

        # Check if it's a manifest list (multi-arch)
        if media_type in MULTIARCH_MEDIA_TYPES or self._is_manifest_list(
            manifest_data
        ):
            logger.info(
                "Manifest is a multi-arch list, finding platform %s", platform
            )
            manifest_list = self._parse_manifest_list(manifest_data, media_type)

            # Find matching platform
            descriptor = self._find_platform_match(manifest_list, platform)

            if descriptor is None:
                available = [
                    str(d.platform) for d in manifest_list.manifests if d.platform
                ]
                raise PlatformNotFoundError(
                    f"No manifest found for platform {platform} in "
                    f"{name}:{reference}.\n"
                    f"Available platforms: {', '.join(available) if available else 'unknown'}"
                )

            logger.info(
                "Found platform match: %s -> %s",
                platform, descriptor.digest[:19],
            )

            # Fetch the platform-specific manifest
            response = await self.client.get_manifest(name, descriptor.digest)
            manifest_bytes = response.content
            manifest_data = json.loads(manifest_bytes)
            media_type = manifest_data.get("mediaType", response.content_type)

        # Parse single manifest
        manifest = self._parse_manifest(manifest_data, media_type, manifest_bytes)

        return ResolvedImage(
            manifest=manifest,
            config_digest=manifest.config.digest,
            layer_digests=[layer.digest for layer in manifest.layers],
            platform=platform,
            annotations=manifest_data.get("annotations", {}),
        )

    def _is_manifest_list(self, data: dict) -> bool:
        """Heuristic check if data is a manifest list."""
        return "manifests" in data and isinstance(data["manifests"], list)

    def _parse_manifest_list(
        self, data: dict, media_type: str
    ) -> ManifestList:
        """Parse a manifest list / OCI index."""
        manifests = []
        for entry in data.get("manifests", []):
            platform = None
            platform_data = entry.get("platform", {})
            if platform_data:
                platform = Platform(
                    os=platform_data.get("os", "linux"),
                    architecture=platform_data.get("architecture", "amd64"),
                    variant=platform_data.get("variant"),
                )

            manifests.append(
                Descriptor(
                    media_type=entry.get("mediaType", ""),
                    digest=entry["digest"],
                    size=entry.get("size", 0),
                    urls=entry.get("urls", []),
                    annotations=entry.get("annotations", {}),
                    platform=platform,
                )
            )

        return ManifestList(
            schema_version=data.get("schemaVersion", 2),
            media_type=media_type,
            manifests=manifests,
        )

    def _parse_manifest(
        self, data: dict, media_type: str, raw_bytes: bytes
    ) -> ImageManifest:
        """Parse a single-image manifest."""
        config_data = data.get("config", {})
        config = Descriptor(
            media_type=config_data.get("mediaType", ""),
            digest=config_data["digest"],
            size=config_data.get("size", 0),
        )

        layers = []
        for layer_data in data.get("layers", []):
            layers.append(
                Descriptor(
                    media_type=layer_data.get("mediaType", ""),
                    digest=layer_data["digest"],
                    size=layer_data.get("size", 0),
                    urls=layer_data.get("urls", []),
                    annotations=layer_data.get("annotations", {}),
                )
            )

        return ImageManifest(
            schema_version=data.get("schemaVersion", 2),
            media_type=media_type,
            config=config,
            layers=layers,
            raw_bytes=raw_bytes,
        )

    def _find_platform_match(
        self, manifest_list: ManifestList, target: Platform
    ) -> Descriptor | None:
        """Find the best matching manifest descriptor for a target platform.

        Match priority:
        1. Exact match (os + arch + variant)
        2. os + arch match (no variant constraint)
        3. os match only
        """
        exact_candidates = []
        os_arch_candidates = []
        os_candidates = []

        for descriptor in manifest_list.manifests:
            if descriptor.platform is None:
                continue

            p = descriptor.platform

            # Exact match: os + arch + variant all match
            if (
                p.os == target.os
                and p.architecture == target.architecture
                and (p.variant == target.variant or not target.variant)
            ):
                exact_candidates.append(descriptor)
            # os + arch match
            elif (
                p.os == target.os and p.architecture == target.architecture
            ):
                os_arch_candidates.append(descriptor)
            # os match only
            elif p.os == target.os:
                os_candidates.append(descriptor)

        # Return best match
        for candidates in [exact_candidates, os_arch_candidates, os_candidates]:
            if candidates:
                return candidates[0]

        # If none of the descriptors has platform info (rare edge case),
        # return the first one as best-effort fallback
        has_any_platform = any(d.platform is not None for d in manifest_list.manifests)
        if not has_any_platform and manifest_list.manifests:
            return manifest_list.manifests[0]

        return None


def compute_digest(data: bytes, algorithm: str = "sha256") -> str:
    """Compute a digest string for data."""
    h = hashlib.new(algorithm)
    h.update(data)
    return f"{algorithm}:{h.hexdigest()}"
