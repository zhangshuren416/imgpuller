"""Image reference parsing and Docker config.json reading."""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from imgpuller.exceptions import InvalidImageReferenceError


# Default Docker Hub registry
DEFAULT_REGISTRY = "registry-1.docker.io"
DEFAULT_NAMESPACE = "library"
DEFAULT_TAG = "latest"

# Official Docker Hub image names (no namespace needed)
OFFICIAL_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


@dataclass
class ImageReference:
    """Parsed image reference."""

    registry: str
    name: str  # e.g. "library/ubuntu" or "org/app"
    reference: str  # tag or digest
    is_digest: bool = False

    @property
    def tag(self) -> str | None:
        """Return tag if reference is a tag, None if digest."""
        return None if self.is_digest else self.reference

    @property
    def digest(self) -> str | None:
        """Return digest if reference is a digest, None if tag."""
        return self.reference if self.is_digest else None

    def __str__(self) -> str:
        if self.is_digest:
            return f"{self.registry}/{self.name}@{self.reference}"
        return f"{self.registry}/{self.name}:{self.reference}"


@dataclass
class RegistryCredentials:
    """Credentials for a registry."""

    username: str | None = None
    password: str | None = None
    auth_token: str | None = None  # base64 encoded "user:pass"
    identity_token: str | None = None


@dataclass
class Platform:
    """Target platform for multi-arch images."""

    os: str = "linux"
    architecture: str = "amd64"
    variant: str | None = None

    def __str__(self) -> str:
        if self.variant:
            return f"{self.os}/{self.architecture}/{self.variant}"
        return f"{self.os}/{self.architecture}"


def detect_current_platform() -> Platform:
    """Detect the current system's platform."""
    import platform as plat

    arch = plat.machine()
    # Normalize architecture names
    arch_map = {
        "x86_64": "amd64",
        "aarch64": "arm64",
        "armv7l": "arm",
        "armv8l": "arm64",
    }
    return Platform(
        os="linux" if plat.system() == "Linux" else plat.system().lower(),
        architecture=arch_map.get(arch, arch),
    )


def parse_image_reference(image: str) -> ImageReference:
    """Parse an image reference into its components.

    Handles formats:
        ubuntu:22.04
        nginx
        library/ubuntu:22.04
        docker.io/library/ubuntu:22.04
        registry.example.com:5000/myapp:v1
        ghcr.io/org/pkg:2
        localhost:5000/app
        ubuntu@sha256:abc123...
        myimage@sha256:abc123...

    Args:
        image: The image reference string.

    Returns:
        Parsed ImageReference.

    Raises:
        InvalidImageReferenceError: If the reference cannot be parsed.
    """
    if not image or not image.strip():
        raise InvalidImageReferenceError("Empty image reference")

    image = image.strip()

    # Separate digest if present
    digest = None
    digest_match = re.match(r"^(.+)@(sha256:[a-f0-9]{64})$", image)
    if digest_match:
        image = digest_match.group(1)
        digest = digest_match.group(2)

    # Separate tag if present (only if no digest)
    tag = None
    if not digest:
        tag_match = re.match(r"^(.+):([^/]+)$", image)
        if tag_match and "/" not in tag_match.group(2):
            image = tag_match.group(1)
            tag = tag_match.group(2)

    # Now image is "registry[/namespace/]name"
    parts = image.split("/")

    registry: str
    name: str

    if len(parts) == 0:
        raise InvalidImageReferenceError(f"Invalid image reference: {image!r}")

    # Check if the first part is a registry (contains '.' or ':' or is 'localhost')
    if len(parts) == 1:
        # "ubuntu" -> Docker Hub, library namespace
        registry = DEFAULT_REGISTRY
        name = f"{DEFAULT_NAMESPACE}/{parts[0]}"
        if not OFFICIAL_IMAGE_RE.match(parts[0]):
            raise InvalidImageReferenceError(
                f"Invalid image name: {parts[0]!r}"
            )
    elif parts[0] == "docker.io":
        # docker.io/namespace/image -- map to registry-1.docker.io
        registry = DEFAULT_REGISTRY
        name = "/".join(parts[1:])
        if len(parts) == 2:
            name = f"{DEFAULT_NAMESPACE}/{parts[1]}"
    elif "." in parts[0] or ":" in parts[0] or parts[0] == "localhost":
        # registry.example.com/namespace/image
        registry = parts[0]
        name = "/".join(parts[1:])
    elif len(parts) == 2:
        # namespace/image -> Docker Hub with explicit namespace
        registry = DEFAULT_REGISTRY
        name = f"{parts[0]}/{parts[1]}"
    else:
        raise InvalidImageReferenceError(
            f"Cannot parse image reference: {image!r}. "
            f"Use format [registry/]name[:tag] or [registry/]name@digest"
        )

    # Validate name (Docker Hub requires namespace/name; custom registries don't)
    if not name:
        raise InvalidImageReferenceError(
            f"Invalid image name: {name!r}"
        )
    if registry == DEFAULT_REGISTRY and "/" not in name:
        raise InvalidImageReferenceError(
            f"Docker Hub images require namespace: {name!r}. "
            f"Use 'library/{name}' or '{name}:tag' format."
        )

    # Determine reference
    if digest:
        return ImageReference(
            registry=registry, name=name, reference=digest, is_digest=True
        )
    return ImageReference(
        registry=registry, name=name, reference=tag or DEFAULT_TAG
    )


def get_default_output_file(image_ref: ImageReference) -> str:
    """Get default output .tar filename from an image reference.

    e.g. library/ubuntu:22.04 -> ubuntu-22.04.tar
         library/ubuntu@sha256:abc... -> ubuntu-<short>.tar
    """
    name_part = image_ref.name.split("/")[-1]
    if image_ref.is_digest:
        short_digest = image_ref.reference.replace("sha256:", "")[:12]
        stem = f"{name_part}-{short_digest}"
    else:
        stem = f"{name_part}-{image_ref.reference}"
    return f"{stem}.tar"


# Backwards-compatible alias.
get_default_output_dir = get_default_output_file


def load_docker_config(config_path: Path | None = None) -> dict:
    """Load Docker/Podman config.json.

    Checks in order:
    1. config_path if provided
    2. DOCKER_CONFIG environment variable
    3. ~/.docker/config.json
    4. $XDG_RUNTIME_DIR/containers/auth.json (podman)

    Returns:
        Parsed config dict. Empty dict if no config found.
    """
    search_paths = []

    if config_path:
        search_paths.append(config_path)

    # DOCKER_CONFIG env var
    docker_config_env = os.environ.get("DOCKER_CONFIG")
    if docker_config_env:
        search_paths.append(Path(docker_config_env) / "config.json")

    # Default docker config
    search_paths.append(Path.home() / ".docker" / "config.json")

    # Podman auth
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        search_paths.append(Path(xdg_runtime) / "containers" / "auth.json")

    for path in search_paths:
        try:
            if path.exists() and path.is_file():
                with open(path, "r") as f:
                    config = json.load(f)
                    if config:
                        return config
        except (OSError, json.JSONDecodeError):
            continue

    return {}


def get_credentials_for_registry(
    registry: str, config: dict | None = None
) -> RegistryCredentials | None:
    """Look up credentials for a registry in docker config.

    Args:
        registry: Registry hostname (e.g. "registry-1.docker.io")
        config: Parsed docker config.json dict. Loaded if None.

    Returns:
        RegistryCredentials if found, None otherwise.
    """
    if config is None:
        config = load_docker_config()

    auths = config.get("auths", {})
    if not auths:
        return None

    # Try exact match, then with https:// prefix, then http:// prefix
    candidates = [
        registry,
        f"https://{registry}",
        f"http://{registry}",
        f"https://{registry}/v1/",  # Docker Hub specific
        "https://index.docker.io/v1/",  # Docker Hub legacy
    ]

    # Docker Hub special case
    if registry == DEFAULT_REGISTRY or registry == "docker.io":
        candidates.extend([
            "https://index.docker.io/v1/",
            "https://registry-1.docker.io/v1/",
            "https://registry.hub.docker.com",
        ])

    for key in candidates:
        if key in auths:
            entry = auths[key]
            if not entry:
                continue

            creds = RegistryCredentials()

            # "auth" field is base64(username:password)
            auth_b64 = entry.get("auth")
            if auth_b64:
                creds.auth_token = auth_b64
                try:
                    decoded = base64.b64decode(auth_b64).decode("utf-8")
                    if ":" in decoded:
                        creds.username, creds.password = decoded.split(":", 1)
                    else:
                        # Token-only auth (e.g. GitHub PAT)
                        creds.username = decoded
                        creds.password = decoded
                except Exception:
                    pass

            # Direct username/password fields
            if entry.get("username"):
                creds.username = entry["username"]
            if entry.get("password"):
                creds.password = entry["password"]

            # Identity token
            creds.identity_token = entry.get("identitytoken")

            if creds.username or creds.password or creds.identity_token:
                return creds

    return None


def resolve_registry_url(registry: str, insecure: bool = False) -> str:
    """Resolve a registry hostname to a full URL.

    Args:
        registry: Registry hostname (e.g. "registry-1.docker.io")
        insecure: If True, use http:// instead of https://

    Returns:
        Full registry URL (e.g. "https://registry-1.docker.io")
    """
    scheme = "http" if insecure else "https"

    # Docker Hub uses registry-1.docker.io for the API
    if registry in ("docker.io", "registry-1.docker.io"):
        return f"{scheme}://registry-1.docker.io"

    # If registry already has a scheme, use it as-is
    if registry.startswith(("http://", "https://")):
        return registry

    return f"{scheme}://{registry}"
