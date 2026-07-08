"""Registry HTTP client implementing the Docker Registry HTTP API V2.

Handles:
- GET /v2/ - API version check
- GET /v2/{name}/manifests/{reference} - manifest fetching
- GET /v2/{name}/blobs/{digest} - blob/layer streaming with Range support

Includes automatic authentication flow: request -> 401 -> handle challenge -> retry.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import AsyncIterator, Optional

import aiohttp

from imgpuller.registry.auth import AuthProvider
from imgpuller.exceptions import (
    AuthenticationError,
    BlobNotFoundError,
    ManifestNotFoundError,
    RateLimitError,
    RegistryError,
    RegistryServerError,
)

logger = logging.getLogger(__name__)

# Media types for manifest negotiation (in priority order)
MANIFEST_ACCEPT_HEADERS = [
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.v1+prettyjws",
]


@dataclass
class ManifestResponse:
    """Result of a manifest fetch."""

    content: bytes
    content_type: str
    digest: str  # Docker-Content-Digest response header


class RegistryClient:
    """Async HTTP client for OCI/Docker Registry API V2."""

    def __init__(
        self,
        registry_url: str,
        auth_provider: AuthProvider | None = None,
        insecure: bool = False,
        proxy: str | None = None,
        max_connections: int = 20,
        connect_timeout: float = 30.0,
        read_timeout: float = 300.0,
    ):
        """Initialize the registry client.

        Args:
            registry_url: Registry base URL (e.g. "https://registry-1.docker.io")
            auth_provider: Auth provider. If None, no auth is attempted.
            insecure: Allow HTTP connections (no TLS verification).
            proxy: HTTP proxy URL (e.g. "http://proxy:8080"). Also respects
                   HTTP_PROXY/HTTPS_PROXY environment variables.
            max_connections: Max concurrent connections to the registry.
            connect_timeout: Connection timeout in seconds.
            read_timeout: Read timeout in seconds (long for blob downloads).
        """
        self.registry_url = registry_url.rstrip("/")
        self.auth_provider = auth_provider
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout

        # Apply explicit proxy via environment (aiohttp reads from env with trust_env=True)
        if proxy:
            os.environ.setdefault("HTTPS_PROXY", proxy)
            os.environ.setdefault("HTTP_PROXY", proxy)

        # Setup TLS/connection config
        connector = aiohttp.TCPConnector(
            ssl=not insecure,
            limit=max_connections,
            limit_per_host=max_connections,
            force_close=False,
            enable_cleanup_closed=True,
        )

        timeout = aiohttp.ClientTimeout(
            connect=connect_timeout,
            sock_read=read_timeout,
            sock_connect=connect_timeout,
        )

        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            trust_env=True,  # respect HTTP_PROXY/HTTPS_PROXY/NO_PROXY
        )

    async def close(self) -> None:
        """Close the HTTP session."""
        if self.session and not self.session.closed:
            await self.session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def check_api(self) -> bool:
        """Check if the registry supports the V2 API.

        Returns:
            True if V2 API is available.
        """
        try:
            async with self.session.get(f"{self.registry_url}/v2/") as resp:
                # 200 = supported, 401 = supported but needs auth
                return resp.status in (200, 401)
        except aiohttp.ClientError as e:
            logger.debug("Registry API check failed: %s", e)
            return False

    async def get_manifest(
        self, name: str, reference: str
    ) -> ManifestResponse:
        """Fetch an image manifest.

        GET /v2/{name}/manifests/{reference}

        Supports manifest list negotiation via Accept headers.

        Args:
            name: Image name (e.g. "library/ubuntu")
            reference: Tag or digest (e.g. "22.04" or "sha256:abc...")

        Returns:
            ManifestResponse with content, content_type, and digest.

        Raises:
            ManifestNotFoundError: If manifest is not found.
            RegistryError: For other registry errors.
        """
        url = f"{self.registry_url}/v2/{name}/manifests/{reference}"
        headers = {
            "Accept": ", ".join(MANIFEST_ACCEPT_HEADERS),
        }

        content, content_type, digest = await self._request_with_retry(
            "GET", url, headers=headers, image_name=name
        )

        # Use Docker-Content-Digest if available, else compute
        return ManifestResponse(
            content=content,
            content_type=content_type,
            digest=digest,
        )

    async def get_blob(
        self,
        name: str,
        digest: str,
        offset: int = 0,
        chunk_callback=None,
    ) -> AsyncIterator[bytes]:
        """Stream a blob/layer from the registry.

        GET /v2/{name}/blobs/{digest}
        With optional Range header for resume support.

        Args:
            name: Image name.
            digest: Blob digest (e.g. "sha256:abc...").
            offset: Byte offset to start from (for resume).
            chunk_callback: Optional async callback(bytes) for each chunk.

        Yields:
            Chunks of blob data.

        Raises:
            BlobNotFoundError: If blob is not found (404).
            RegistryError: For other errors.
        """
        url = f"{self.registry_url}/v2/{name}/blobs/{digest}"
        headers = {}

        if offset > 0:
            headers["Range"] = f"bytes={offset}-"

        response = await self._request_stream_with_retry(
            "GET", url, headers=headers, image_name=name
        )

        try:
            async for chunk in response.content.iter_chunked(
                1024 * 1024  # 1MB chunks
            ):
                if chunk_callback:
                    await chunk_callback(chunk)
                yield chunk
        finally:
            response.release()

    async def get_blob_size(self, name: str, digest: str) -> int | None:
        """Get the size of a blob via HEAD request.

        GET /v2/{name}/blobs/{digest} (HEAD)

        Args:
            name: Image name.
            digest: Blob digest.

        Returns:
            Content-Length in bytes, or None if not available.
        """
        url = f"{self.registry_url}/v2/{name}/blobs/{digest}"

        try:
            resp = await self._request_head_with_retry(
                "HEAD", url, image_name=name
            )
            content_length = resp.headers.get("Content-Length")
            resp.release()
            if content_length:
                return int(content_length)
            return None
        except BlobNotFoundError:
            return None

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        image_name: str | None = None,
        max_retries: int = 2,
    ) -> tuple[bytes, str, str]:
        """Make an HTTP request with automatic auth retry.

        Returns:
            Tuple of (response_body_bytes, content_type, docker_content_digest)
        """
        headers = dict(headers or {})

        for attempt in range(max_retries + 1):
            # Apply auth before request
            if self.auth_provider:
                # Check for cached token
                if hasattr(self.auth_provider, 'get_token'):
                    token = self.auth_provider.get_token(url, image_name)
                    if token:
                        headers["Authorization"] = f"Bearer {token}"

                await self.auth_provider.apply_auth(
                    self.session, method, url, headers, image_name
                )

            try:
                async with self.session.request(
                    method, url, headers=headers, allow_redirects=True
                ) as resp:
                    if resp.status == 401 and attempt < max_retries:
                        if self.auth_provider:
                            handled = await self.auth_provider.handle_401(
                                self.session, resp, image_name
                            )
                            if handled:
                                continue
                        # Can't authenticate
                        raise AuthenticationError(
                            f"Authentication failed for {url}\n"
                            f"Status: {resp.status}\n"
                            f"Response: {await resp.text()}"
                        )

                    self._check_response_status(resp, await resp.text())

                    content_type = resp.headers.get("Content-Type", "")
                    digest = resp.headers.get("Docker-Content-Digest", "")
                    body = await resp.read()

                    return body, content_type, digest

            except aiohttp.ClientError as e:
                if attempt < max_retries:
                    logger.debug(
                        "Request failed (attempt %d/%d): %s",
                        attempt + 1, max_retries + 1, e,
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise RegistryError(f"Registry request failed: {e}") from e

        raise RegistryError(f"Request failed after {max_retries + 1} attempts: {url}")

    async def _request_stream_with_retry(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        image_name: str | None = None,
    ) -> aiohttp.ClientResponse:
        """Make a streaming request with auth retry.

        Returns the response object - caller must call .release() after streaming.
        """
        headers = dict(headers or {})

        for attempt in range(2):  # 2 attempts for streaming
            if self.auth_provider:
                if hasattr(self.auth_provider, 'get_token'):
                    token = self.auth_provider.get_token(url, image_name)
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                await self.auth_provider.apply_auth(
                    self.session, method, url, headers, image_name
                )

            try:
                resp = await self.session.request(
                    method, url, headers=headers, allow_redirects=True
                )

                if resp.status == 401 and attempt == 0:
                    if self.auth_provider:
                        handled = await self.auth_provider.handle_401(
                            self.session, resp, image_name
                        )
                        resp.release()
                        if handled:
                            continue
                    raise AuthenticationError(
                        f"Authentication failed for {url}"
                    )

                self._check_response_status(resp, await resp.text())
                return resp

            except aiohttp.ClientError as e:
                if attempt == 0:
                    await asyncio.sleep(1)
                    continue
                raise RegistryError(f"Streaming request failed: {e}") from e

        raise RegistryError(f"Streaming request failed after retries: {url}")

    async def _request_head_with_retry(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        image_name: str | None = None,
    ) -> aiohttp.ClientResponse:
        """Make a HEAD request with auth retry."""
        headers = dict(headers or {})

        for attempt in range(2):
            if self.auth_provider:
                if hasattr(self.auth_provider, 'get_token'):
                    token = self.auth_provider.get_token(url, image_name)
                    if token:
                        headers["Authorization"] = f"Bearer {token}"

            resp = await self.session.request(
                method, url, headers=headers, allow_redirects=True
            )

            if resp.status == 401 and attempt == 0:
                if self.auth_provider:
                    handled = await self.auth_provider.handle_401(
                        self.session, resp, image_name
                    )
                    resp.release()
                    if handled:
                        continue

            return resp

        raise RegistryError("HEAD request failed after retries")

    @staticmethod
    def _check_response_status(
        resp: aiohttp.ClientResponse, body: str = ""
    ) -> None:
        """Check response status and raise appropriate exceptions."""
        if 200 <= resp.status < 300:
            return

        if resp.status == 401:
            raise AuthenticationError(
                f"Authentication failed (HTTP 401): {body[:500]}"
            )
        elif resp.status == 404:
            if "/manifests/" in str(resp.url):
                raise ManifestNotFoundError(
                    f"Manifest not found: {resp.url}\n{body[:500]}"
                )
            else:
                raise BlobNotFoundError(
                    f"Blob not found: {resp.url}\n{body[:500]}"
                )
        elif resp.status == 429:
            raise RateLimitError(
                f"Rate limit exceeded: {resp.url}\n{body[:500]}"
            )
        elif resp.status >= 500:
            raise RegistryServerError(
                f"Registry server error (HTTP {resp.status}): {resp.url}\n{body[:500]}"
            )
        else:
            raise RegistryError(
                f"Unexpected response (HTTP {resp.status}): {resp.url}\n{body[:500]}"
            )
