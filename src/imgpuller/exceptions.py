"""Exception hierarchy for imgpuller."""


class ImgpullerError(Exception):
    """Base exception for all imgpuller errors."""

    exit_code = 1


# -- Configuration errors (exit code 2) --

class ConfigurationError(ImgpullerError):
    """Configuration or argument error."""

    exit_code = 2


class InvalidImageReferenceError(ConfigurationError):
    """Invalid image reference format."""


class CredentialNotFoundError(ConfigurationError):
    """Credentials not found for the requested registry."""


# -- Registry errors (exit code 3) --

class RegistryError(ImgpullerError):
    """Registry API error."""

    exit_code = 3


class ManifestNotFoundError(RegistryError):
    """Manifest not found (HTTP 404)."""


class BlobNotFoundError(RegistryError):
    """Blob/layer not found (HTTP 404)."""


class RateLimitError(RegistryError):
    """Registry rate limit exceeded (HTTP 429)."""


class RegistryServerError(RegistryError):
    """Server-side registry error (HTTP 5xx)."""


class AuthenticationError(RegistryError):
    """Authentication failed."""


# -- Download errors (exit code 4) --

class DownloadError(ImgpullerError):
    """Download error."""

    exit_code = 4


class DigestMismatchError(DownloadError):
    """SHA256 digest verification failed."""


class DownloadInterruptedError(DownloadError):
    """Download was interrupted (network issue, etc.)."""


# -- Manifest/platform errors (exit code 5) --

class ManifestError(ImgpullerError):
    """Manifest parsing or resolution error."""

    exit_code = 5


class PlatformNotFoundError(ManifestError):
    """No matching platform in manifest list."""


class UnsupportedMediaTypeError(ManifestError):
    """Unsupported manifest media type."""


# -- OCI layout errors (exit code 6) --

class OCILayoutError(ImgpullerError):
    """OCI layout write/read error."""

    exit_code = 6
