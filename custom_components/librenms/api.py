"""Thin async client for the LibreNMS REST API (v0)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlparse, urlunparse

from aiohttp import ClientError, ClientResponseError, ClientSession, ClientTimeout

from .const import API_PATH, REQUEST_TIMEOUT

_LOGGER = logging.getLogger(__name__)


class LibreNMSError(Exception):
    """Base error for all LibreNMS API failures."""


class LibreNMSConnectionError(LibreNMSError):
    """Raised when the instance could not be reached."""


class LibreNMSAuthError(LibreNMSError):
    """Raised when the API token was rejected."""


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
            ) as response:
                if response.status in (401, 403):
                    raise LibreNMSAuthError(
                        f"LibreNMS rejected the API token (HTTP {response.status})"
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
        """Return every device known to the instance.

        Note: unlike the ports endpoint, ``/devices`` has no ``columns``
        parameter — the API always selects ``d.*``.
        """
        payload = await self._request("devices")
        return payload.get("devices") or []

    async def async_get_alerts(self) -> list[dict[str, Any]]:
        """Return currently active (state=1) alerts.

        The API joins ``alert_rules``, so each alert already carries the rule
        ``name`` and ``severity`` — no separate ``/rules`` lookup is needed.
        """
        payload = await self._request("alerts", params={"state": "1"})
        return payload.get("alerts") or []

    async def async_get_sensors(self) -> list[dict[str, Any]]:
        """Return every health sensor across the whole instance.

        One call covers all devices, so adding health data costs a single
        extra request per poll rather than one per device.
        """
        payload = await self._request("resources/sensors")
        return payload.get("sensors") or []

    async def async_get_overview(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Fetch devices, alerts and sensors concurrently."""
        devices, alerts, sensors = await asyncio.gather(
            self.async_get_devices(),
            self.async_get_alerts(),
            self.async_get_sensors(),
        )
        return devices, alerts, sensors
