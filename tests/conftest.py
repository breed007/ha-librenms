"""Fixtures for the LibreNMS tests."""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Generator
from datetime import timedelta
import json
import pathlib
import re
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.librenms.const import DOMAIN

from .const import BASE_URL, ENTRY_DATA, ENTRY_OPTIONS

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"

SYSTEM_PATTERN = re.compile(r"/api/v0/system(\?|$)")
DEVICES_PATTERN = re.compile(r"/api/v0/devices(\?|$)")
ALERTS_PATTERN = re.compile(r"/api/v0/alerts(\?|$)")
SENSORS_PATTERN = re.compile(r"/api/v0/resources/sensors(\?|$)")


def load_fixture_json(name: str) -> dict[str, Any]:
    """Load a JSON fixture from tests/fixtures."""
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


class MockLibreNMS:
    """A controllable stand-in for a LibreNMS instance.

    Every attribute is read at request time, so a test can change what the
    instance reports between coordinator polls just by assigning to it.
    """

    def __init__(self, mocker: AiohttpClientMocker) -> None:
        """Register handlers for every endpoint the integration uses."""
        self.system = load_fixture_json("system.json")
        self.devices = load_fixture_json("devices.json")
        self.alerts = load_fixture_json("alerts.json")
        self.sensors = load_fixture_json("sensors.json")
        self.status = 200
        self.exception: Exception | None = None
        # Per-endpoint failures, keyed by "system", "devices", "alerts" or
        # "sensors". They take precedence over the instance-wide status.
        self.failures: dict[str, dict[str, Any]] = {}

        mocker.get(SYSTEM_PATTERN, side_effect=self._responder("system"))
        mocker.get(DEVICES_PATTERN, side_effect=self._responder("devices"))
        mocker.get(ALERTS_PATTERN, side_effect=self._alerts_responder)
        mocker.get(SENSORS_PATTERN, side_effect=self._responder("sensors"))

    def _responder(
        self, attribute: str
    ) -> Callable[..., Coroutine[Any, Any, AiohttpClientMockResponse]]:
        """Build a side effect backed by a mutable attribute."""

        async def _side_effect(
            method: str, url: Any, data: Any
        ) -> AiohttpClientMockResponse:
            if attribute in self.failures:
                return AiohttpClientMockResponse(
                    method=method, url=url, **self.failures[attribute]
                )
            return AiohttpClientMockResponse(
                method=method,
                url=url,
                status=self.status,
                json=getattr(self, attribute),
                exc=self.exception,
            )

        return _side_effect

    def fail(
        self,
        endpoint: str,
        *,
        status: int = 200,
        message: str = "error",
        text: str | None = None,
        exc: Exception | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Make one endpoint fail the way LibreNMS or a proxy would.

        An error status gets LibreNMS's own api_error() body; `text` replaces
        the body outright (an HTML error page, say) and `exc` raises instead
        of answering. `headers` adds response headers, such as a Location.
        """
        failure: dict[str, Any] = {"status": status, "exc": exc, "headers": headers}
        if text is not None:
            failure["text"] = text
        else:
            failure["json"] = {"status": "error", "message": message}
        self.failures[endpoint] = failure

    def recover(self, endpoint: str) -> None:
        """Let a failed endpoint answer normally again."""
        self.failures.pop(endpoint, None)

    async def _alerts_responder(
        self, method: str, url: Any, data: Any
    ) -> AiohttpClientMockResponse:
        """Filter alerts by `state` exactly as LibreNMS's list_alerts does.

        LibreNMS returns only state 1 unless `state` is given, and splits the
        parameter on commas. Returning every alert regardless would let a
        test pass against code that asks for the wrong states.
        """
        if "alerts" in self.failures:
            return AiohttpClientMockResponse(
                method=method, url=url, **self.failures["alerts"]
            )
        requested = url.query.get("state")
        wanted = requested.split(",") if requested is not None else ["1"]
        alerts = [
            alert
            for alert in self.alerts.get("alerts", [])
            if str(alert.get("state", "1")) in wanted
        ]
        return AiohttpClientMockResponse(
            method=method,
            url=url,
            status=self.status,
            json={**self.alerts, "count": len(alerts), "alerts": alerts},
            exc=self.exception,
        )

    def set_alerts(self, alerts: list[dict[str, Any]]) -> None:
        """Replace the active alert list returned by the instance."""
        self.alerts = {
            "status": "ok",
            "message": "",
            "count": len(alerts),
            "alerts": alerts,
        }

    def set_devices(self, devices: list[dict[str, Any]]) -> None:
        """Replace the device list returned by the instance."""
        self.devices = {"status": "ok", "count": len(devices), "devices": devices}

    def set_sensors(self, sensors: list[dict[str, Any]]) -> None:
        """Replace the health sensor list returned by the instance."""
        self.sensors = {"status": "ok", "count": len(sensors), "sensors": sensors}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Enable loading custom_components in every test."""
    yield


@pytest.fixture
def mock_librenms(aioclient_mock: AiohttpClientMocker) -> MockLibreNMS:
    """Mock the LibreNMS HTTP API."""
    return MockLibreNMS(aioclient_mock)


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a config entry for the mocked instance."""
    return MockConfigEntry(
        domain=DOMAIN,
        title=BASE_URL,
        unique_id=BASE_URL,
        data=ENTRY_DATA,
        options=ENTRY_OPTIONS,
    )


async def setup_integration(hass: HomeAssistant, entry: MockConfigEntry) -> bool:
    """Add the entry to hass and set it up."""
    entry.add_to_hass(hass)
    result = await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return result


async def async_poll(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: int = 61
) -> None:
    """Advance past the scan interval and let the coordinator run.

    The coordinator schedules its interval refresh as a background task, so
    `async_block_till_done` has to be told to wait for those too.
    """
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def get_device(
    hass: HomeAssistant, entry: MockConfigEntry, device_id: int | None = None
) -> dr.DeviceEntry | None:
    """Return the hub device, or the device for one LibreNMS device id.

    Goes through `async_get_device_by_identifier`, which is what Home
    Assistant 2026.9 requires in tests; `async_get_device` is rejected there.
    """
    identifier = (
        entry.entry_id if device_id is None else f"{entry.entry_id}_{device_id}"
    )
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, identifier), entry.entry_id
    )


# The Delete button on a device page sends `config/device_registry/remove`
# from Home Assistant 2026.9. Earlier releases only have
# `remove_config_entry`, which 2026.9 keeps as a deprecated alias until
# 2027.9. Both end in the integration's async_remove_config_entry_device.
REMOVE_DEVICE = "config/device_registry/remove"
REMOVE_DEVICE_LEGACY = "config/device_registry/remove_config_entry"


async def remove_device(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    entry: MockConfigEntry,
    device: dr.DeviceEntry,
) -> dict[str, Any]:
    """Delete a device the way the device page's Delete button does.

    Sends whichever command this Home Assistant registers, so the test
    exercises the real request on every version in the matrix without
    tripping the deprecation warning on newer ones. Returns the response.
    """
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    if REMOVE_DEVICE in hass.data[websocket_api.DOMAIN]:
        message = {"type": REMOVE_DEVICE, "device_id": device.id}
    else:
        message = {
            "type": REMOVE_DEVICE_LEGACY,
            "config_entry_id": entry.entry_id,
            "device_id": device.id,
        }
    await client.send_json_auto_id(message)
    return await client.receive_json()
