"""Authentication providers for OCI registries.

Handles:
- NoAuthProvider: Public registries, no credentials
- TokenAuthProvider: Bearer token exchange (WWW-Authenticate challenge/response)
- BasicAuthProvider: HTTP Basic authentication
- DockerConfigAuthProvider: Reads credentials from ~/.docker/config.json
"""

from __future__ import annotations

import base64
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import aiohttp

from imgpuller.config import (
    RegistryCredentials,
    get_credentials_for_registry,
    load_docker_config,
)

logger = logging.getLogger(__name__)


@dataclass
class WWWAuthenticateChallenge:
    """Parsed WWW-Authenticate header."""
    realm: str
    service: str
    scope: str = ""
    scheme: str = "Bearer"


def parse_www_authenticate(header: str) -> WWWAuthenticateChallenge | None:
    """Parse a WWW-Authenticate header into its components.

    Example header:
        Bearer realm="https://auth.docker.io/token",
              service="registry.docker.io",
              scope="repository:library/ubuntu:pull"

    Args:
        header: Raw WWW-Authenticate header value.

    Returns:
        Parsed challenge or None if parsing fails.
    """
    if not header:
        return None

    # Match: Bearer realm="...", service="...", scope="..."
    pattern = r'(?:Bearer|Basic)\s+'
    params = {}

    # Extract key="value" pairs
    for match in re.finditer(r'(\w+)=("[^"]*"|[^,]+)', header):
        key = match.group(1).lower()
        value = match.group(2).strip('"')
        params[key] = value

    if "realm" not in params:
        return None

    return WWWAuthenticateChallenge(
        realm=params["realm"],
        service=params.get("service", ""),
        scope=params.get("scope", ""),
        scheme="Bearer" if "Bearer" in header else "Basic",
    )


class AuthProvider(ABC):
    """Base class for authentication providers."""

    @abstractmethod
    async def apply_auth(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        headers: dict,
        image_name: str | None = None,
    ) -> bool:
        """Apply authentication to a request.

        Called before every request. Return True if auth was applied,
        False if not needed.

        Args:
            session: The aiohttp session.
            method: HTTP method.
            url: Full request URL.
            headers: Request headers dict (modified in-place).
            image_name: Image name for scope computation.

        Returns:
            True if auth headers were added.
        """
        ...

    @abstractmethod
    async def handle_401(
        self,
        session: aiohttp.ClientSession,
        response: aiohttp.ClientResponse,
        image_name: str | None = None,
    ) -> bool:
        """Handle a 401 response. Return True if auth was obtained and
        the request should be retried.

        Args:
            session: The aiohttp session.
            response: The 401 response.
            image_name: Image name for scope computation.

        Returns:
            True if auth was obtained, request should be retried.
        """
        ...


class NoAuthProvider(AuthProvider):
    """No authentication - for public registries."""

    async def apply_auth(self, *args, **kwargs) -> bool:
        return False

    async def handle_401(self, *args, **kwargs) -> bool:
        return False


class BasicAuthProvider(AuthProvider):
    """HTTP Basic authentication."""

    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self._auth_header: str | None = None

    @property
    def auth_header(self) -> str:
        if self._auth_header is None:
            credentials = f"{self.username}:{self.password}"
            encoded = base64.b64encode(credentials.encode()).decode()
            self._auth_header = f"Basic {encoded}"
        return self._auth_header

    async def apply_auth(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        headers: dict,
        image_name: str | None = None,
    ) -> bool:
        headers["Authorization"] = self.auth_header
        return True

    async def handle_401(self, *args, **kwargs) -> bool:
        # Basic auth doesn't handle 401 dynamically
        return False


class TokenAuthProvider(AuthProvider):
    """Bearer token authentication via WWW-Authenticate challenge.

    Handles the OCI/Docker token flow:
    1. Registry returns 401 with WWW-Authenticate header
    2. Parse realm, service, scope from header
    3. GET {realm}?service={service}&scope={scope}
    4. Use returned token as Bearer token
    """

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        identity_token: str | None = None,
    ):
        self.username = username
        self.password = password
        self.identity_token = identity_token
        self._tokens: dict[str, str] = {}  # scope -> token cache

    async def apply_auth(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        headers: dict,
        image_name: str | None = None,
    ) -> bool:
        # We need scope to get a token, which we don't know until 401
        # So we don't preemptively add auth here
        return False

    async def handle_401(
        self,
        session: aiohttp.ClientSession,
        response: aiohttp.ClientResponse,
        image_name: str | None = None,
    ) -> bool:
        www_auth = response.headers.get("WWW-Authenticate", "")
        challenge = parse_www_authenticate(www_auth)

        if challenge is None:
            logger.debug("No WWW-Authenticate header in 401 response")
            return False

        # Check cache
        cache_key = challenge.scope
        if cache_key in self._tokens:
            return True  # We already have a token, caller should retry

        # Fetch token
        token = await self._fetch_token(session, challenge)
        if token:
            self._tokens[cache_key] = token
            return True

        return False

    def get_token(self, url: str, image_name: str | None = None) -> str | None:
        """Get a cached token for a scope matching the URL."""
        # Try to find a matching cached token
        for scope, token in self._tokens.items():
            return token
        return None

    async def _fetch_token(
        self,
        session: aiohttp.ClientSession,
        challenge: WWWAuthenticateChallenge,
    ) -> str | None:
        """Fetch a Bearer token from the auth realm.

        Args:
            session: aiohttp session.
            challenge: Parsed WWW-Authenticate challenge.

        Returns:
            Bearer token string or None.
        """
        params = {}
        if challenge.service:
            params["service"] = challenge.service
        if challenge.scope:
            params["scope"] = challenge.scope

        # Some registries (e.g., ghcr.io) require scope in query params
        url = challenge.realm

        headers = {}
        if self.username and self.password:
            credentials = f"{self.username}:{self.password}"
            encoded = base64.b64encode(credentials.encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"

        try:
            async with session.get(
                url, params=params, headers=headers
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Token endpoint returned %d: %s",
                        resp.status,
                        await resp.text(),
                    )
                    return None

                data = await resp.json()
                token = data.get("token") or data.get("access_token")
                if token:
                    logger.debug(
                        "Obtained token for scope=%s expires_in=%s",
                        challenge.scope,
                        data.get("expires_in", "unknown"),
                    )
                    return token

                logger.warning("Token response missing 'token' field: %s", list(data.keys()))
                return None

        except aiohttp.ClientError as e:
            logger.warning("Failed to fetch token from %s: %s", url, e)
            return None


class CompositeAuthProvider(AuthProvider):
    """Try multiple auth providers in sequence."""

    def __init__(self, providers: list[AuthProvider]):
        self.providers = providers

    async def apply_auth(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        headers: dict,
        image_name: str | None = None,
    ) -> bool:
        for provider in self.providers:
            if await provider.apply_auth(session, method, url, headers, image_name):
                return True
        return False

    async def handle_401(
        self,
        session: aiohttp.ClientSession,
        response: aiohttp.ClientResponse,
        image_name: str | None = None,
    ) -> bool:
        for provider in self.providers:
            if await provider.handle_401(session, response, image_name):
                return True
        return False

    def __repr__(self) -> str:
        return f"CompositeAuthProvider({self.providers!r})"


def create_auth_provider(
    registry: str,
    credentials: RegistryCredentials | None = None,
    username: str | None = None,
    password: str | None = None,
) -> AuthProvider:
    """Create the appropriate auth provider for a registry.

    Priority:
    1. Explicit username/password from CLI
    2. Credentials from docker config.json
    3. Token auth (anonymous, will handle 401 dynamically)

    Args:
        registry: Registry hostname.
        credentials: Credentials from docker config.
        username: Explicit username from CLI.
        password: Explicit password from CLI.

    Returns:
        AuthProvider instance.
    """
    providers: list[AuthProvider] = []

    # CLI-provided credentials take priority
    if username and password:
        providers.append(BasicAuthProvider(username, password))
    elif username:
        # Username-only might be for token auth (e.g. GitHub PAT)
        providers.append(BasicAuthProvider(username, username))

    # Docker config credentials
    if credentials:
        if credentials.identity_token:
            providers.append(
                TokenAuthProvider(identity_token=credentials.identity_token)
            )
        if credentials.auth_token:
            if credentials.username and credentials.password:
                providers.append(
                    BasicAuthProvider(credentials.username, credentials.password)
                )

    # Always add token auth for dynamic 401 handling (anonymous pull)
    if username and password:
        providers.append(TokenAuthProvider(username, password))
    elif username:
        providers.append(TokenAuthProvider(username, username))
    elif credentials and credentials.username and credentials.password:
        providers.append(
            TokenAuthProvider(credentials.username, credentials.password)
        )
    else:
        providers.append(TokenAuthProvider())

    if len(providers) == 1:
        return providers[0]

    return CompositeAuthProvider(providers)
