"""Thin async client for the LibreNMS REST API (v0)."""

from __future__ import annotations

from contextlib import suppress
import logging
from typing import Any
from urllib.parse import urlparse, urlunparse

from aiohttp import (
    ClientError,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
    hdrs,
)
from yarl import URL

from .const import API_PATH, OPEN_ALERT_STATES, REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)


class LibreNMSError(Exception):
    """Base error for all LibreNMS API failures."""


class LibreNMSConnectionError(LibreNMSError):
    """Raised when the instance could not be reached."""


class LibreNMSAuthError(LibreNMSError):
    """Raised when LibreNMS does not accept the API token at all (HTTP 401)."""


class LibreNMSPermissionError(LibreNMSError):
    """Raised when a valid token's user may not read an endpoint (HTTP 403).

    Kept apart from LibreNMSAuthError on purpose. Asking for a new token
    cannot fix a role problem: the same token passes validation again, the
    entry reloads, hits the same 403, and the user is stuck in a loop.
    """


class LibreNMSNotFoundError(LibreNMSConnectionError):
    """Raised on HTTP 404: the URL does not lead to this API endpoint."""


class LibreNMSRedirectError(LibreNMSConnectionError):
    """Raised when the instance answers with a redirect instead of the API.

    Redirects are never followed. aiohttp drops Authorization and cookies on
    a cross-origin redirect but not a custom header like X-Auth-Token, so
    following one would hand the token, and with it every SNMP credential
    LibreNMS holds, to whatever host the redirect names.
    """

    def __init__(self, location: str) -> None:
        """Record where the instance tried to send the request."""
        super().__init__(
            f"LibreNMS redirected to {location} instead of answering. If that is "
            "the right address, reconfigure the integration to use it"
        )
        self.location = location


def normalize_url(url: str) -> str:
    """Return a canonical base URL for a LibreNMS instance.

    Accepts anything a user is likely to paste: with or without a scheme, with
    a trailing slash, with a subpath (reverse-proxied installs), or with the
    ``/api/v0`` suffix already appended. The result never has a trailing slash
    and never includes the API path.

    Raises:
        ValueError: If the URL has no host.
    """
    candidate = url.strip()
    if not candidate:
        raise ValueError("empty url")

    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"invalid url: {url}")

    path = parsed.path.rstrip("/")
    if path.endswith(API_PATH):
        path = path[: -len(API_PATH)]

    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", "", ""))


class LibreNMSClient:
    """Minimal read-only client for the endpoints this integration needs."""

    def __init__(self, session: ClientSession, url: str, token: str) -> None:
        """Initialise the client against a normalized base URL."""
        self._session = session
        self._base_url = normalize_url(url)
        self._token = token

    @property
    def base_url(self) -> str:
        """Return the normalized instance base URL (no API path)."""
        return self._base_url

    def device_url(self, device_id: int | str) -> str:
        """Return the LibreNMS web UI URL for a device."""
        return f"{self._base_url}/device/device={device_id}/"

    async def _request(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Perform a GET against the API and return the decoded envelope."""
        url = f"{self._base_url}{API_PATH}/{endpoint.lstrip('/')}"
        try:
            async with self._session.get(
                url,
                headers={"X-Auth-Token": self._token},
                params=params,
                timeout=ClientTimeout(total=REQUEST_TIMEOUT),
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    raise LibreNMSAuthError(
                        "LibreNMS rejected the API token (HTTP 401)"
                    )
                if response.status == 403:
                    raise LibreNMSPermissionError(
                        f"The API token's LibreNMS user is not allowed to read "
                        f"{endpoint} (HTTP 403)"
                    )
                if response.status == 404:
                    raise LibreNMSNotFoundError(
                        f"LibreNMS returned HTTP 404 for {endpoint}"
                    )
                if 300 <= response.status < 400:
                    raise LibreNMSRedirectError(
                        _redirect_target(
                            url, response.headers.get(hdrs.LOCATION), endpoint
                        )
                    )
                response.raise_for_status()
                # Reverse proxies and error pages routinely return the wrong
                # content type, so don't let aiohttp enforce it.
                payload = await response.json(content_type=None)
        except TimeoutError as err:
            raise LibreNMSConnectionError(f"Timeout connecting to {url}") from err
        except ClientResponseError as err:
            raise LibreNMSConnectionError(
                f"LibreNMS returned HTTP {err.status} for {endpoint}"
            ) from err
        except ClientError as err:
            raise LibreNMSConnectionError(f"Error connecting to {url}: {err}") from err
        except ValueError as err:
            raise LibreNMSError(
                f"LibreNMS returned invalid JSON for {endpoint}"
            ) from err

        if not isinstance(payload, dict):
            raise LibreNMSError(f"Unexpected response shape for {endpoint}")

        if payload.get("status") != "ok":
            message = payload.get("message") or "unknown error"
            raise LibreNMSError(f"LibreNMS API error for {endpoint}: {message}")

        return payload

    async def async_get_system(self) -> dict[str, Any]:
        """Return instance/version info. Used to validate credentials."""
        payload = await self._request("system")
        system = payload.get("system") or []
        return system[0] if system else {}

    async def async_get_devices(self) -> list[dict[str, Any]]:
        """Return every device the token's user is allowed to see.

        Note: unlike the ports endpoint, ``/devices`` has no ``columns``
        parameter; the API always returns the full device row.
        """
        payload = await self._request("devices")
        return _as_list(payload, "devices")

    async def async_get_alerts(self) -> list[dict[str, Any]]:
        """Return every open alert, whatever state it has moved to.

        LibreNMS defaults to ``state=1`` (active) only, which drops alerts
        that are acknowledged or have worsened, improved or changed. The
        endpoint accepts a comma-separated list, so ask for all open states.

        The API joins ``alert_rules``, so each alert already carries the rule
        ``name`` and ``severity``; no separate ``/rules`` lookup is needed.
        """
        payload = await self._request(
            "alerts",
            params={"state": ",".join(str(state) for state in OPEN_ALERT_STATES)},
        )
        return _as_list(payload, "alerts")

    async def async_get_sensors(self) -> list[dict[str, Any]]:
        """Return every health sensor across the whole instance.

        One call covers all devices, so adding health data costs a single
        extra request per poll rather than one per device. LibreNMS answers
        HTTP 404 ("Sensors do not exist") when there are none, which is an
        empty result rather than a failure.
        """
        try:
            payload = await self._request("resources/sensors")
        except LibreNMSNotFoundError:
            return []
        return _as_list(payload, "sensors")


def _redirect_target(request_url: str, location: str | None, endpoint: str) -> str:
    """Return the address a redirect points at, as a URL to configure.

    A redirect for `/api/v0/system` to `https://host/api/v0/system` is the
    instance moving (http to https, a new name); report the base URL the
    user should enter. Anything else, such as a login portal, is reported
    as is.
    """
    if not location:
        return "an unnamed location"
    target = URL(request_url).join(URL(location))
    suffix = f"{API_PATH}/{endpoint.lstrip('/')}"
    path = target.path.rstrip("/")
    if target.scheme in ("http", "https") and path.endswith(suffix):
        base = target.with_path(path[: -len(suffix)] or "/").with_query(None)
        with suppress(ValueError):
            return normalize_url(str(base))
    return str(target)


def _as_list(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Return the list under `key`, rejecting any other shape."""
    items = payload.get(key)
    if items is None:
        return []
    if not isinstance(items, list):
        raise LibreNMSError(f"Unexpected response shape for {key}")
    return [item for item in items if isinstance(item, dict)]
