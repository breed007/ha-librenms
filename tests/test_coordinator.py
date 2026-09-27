"""Tests for coordinator parsing and resilience details."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientConnectionError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.librenms.api import (
    LibreNMSAuthError,
    LibreNMSClient,
    LibreNMSConnectionError,
    LibreNMSError,
    LibreNMSNotFoundError,
    LibreNMSPermissionError,
)
from custom_components.librenms.const import LARGE_INSTALL_DEVICE_COUNT
from custom_components.librenms.coordinator import (
    LibreNMSAlert,
    LibreNMSDevice,
    _as_bool,
    _as_int,
    _as_str,
)

from .conftest import MockLibreNMS, async_poll, setup_integration
from .const import BASE_URL, TOKEN


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, 5),
        ("5", 5),
        (None, None),
        ("", None),
        ("not a number", None),
        ([], None),
    ],
)
def test_as_int(value: Any, expected: int | None) -> None:
    """LibreNMS returns numbers as ints or strings depending on version."""
    assert _as_int(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("1", True),
        ("0", False),
        ("true", True),
        ("YES", True),
        ("false", False),
        (None, False),
        ([], False),
    ],
)
def test_as_bool(value: Any, expected: bool) -> None:
    """Flags arrive as bools, ints or strings."""
    assert _as_bool(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("text", "text"), ("  text  ", "text"), ("", None), ("   ", None), (None, None)],
)
def test_as_str(value: Any, expected: str | None) -> None:
    """Empty strings are normalised to None so they do not show as blanks."""
    assert _as_str(value) == expected


def test_device_without_id_is_dropped() -> None:
    """A malformed device payload is skipped rather than crashing the poll."""
    assert LibreNMSDevice.from_api({"hostname": "orphan"}) is None


def test_device_falls_back_to_a_generated_name() -> None:
    """A device with no hostname or sysName still gets a usable name."""
    device = LibreNMSDevice.from_api({"device_id": 9})
    assert device is not None
    assert device.hostname == "device-9"
    assert device.name == "device-9"


def test_alert_without_id_is_dropped() -> None:
    """A malformed alert payload is skipped."""
    assert LibreNMSAlert.from_api({"hostname": "orphan"}) is None


@pytest.mark.parametrize(
    ("severity", "expected"),
    [
        ("critical", "critical"),
        ("CRITICAL", "critical"),
        ("warning", "warning"),
        (None, "ok"),
        ("something-new", "ok"),
    ],
)
def test_alert_severity_is_normalised(severity: Any, expected: str) -> None:
    """An unrecognised severity degrades to ok rather than breaking counts."""
    alert = LibreNMSAlert.from_api({"id": 1, "severity": severity})
    assert alert is not None
    assert alert.severity == expected


async def test_large_install_warns_once(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A big instance gets one hint to raise the scan interval, not a flood."""
    mock_librenms.set_devices(
        [
            {"device_id": index, "hostname": f"device-{index}", "status": 1}
            for index in range(LARGE_INSTALL_DEVICE_COUNT + 1)
        ]
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.librenms"):
        await setup_integration(hass, mock_config_entry)
        await mock_config_entry.runtime_data.async_refresh()

    warnings = [
        record
        for record in caplog.records
        if "Consider raising the scan interval" in record.message
    ]
    assert len(warnings) == 1


async def test_api_rejects_a_non_dict_payload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A proxy returning a JSON list is reported as a bad response."""
    aioclient_mock.get(f"{BASE_URL}/api/v0/system", json=["not", "an", "envelope"])
    client = LibreNMSClient(async_get_clientsession(hass), BASE_URL, TOKEN)

    with pytest.raises(LibreNMSError, match="Unexpected response shape"):
        await client.async_get_system()


async def test_api_rejects_invalid_json(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An HTML error page from a reverse proxy is not silently swallowed."""
    aioclient_mock.get(f"{BASE_URL}/api/v0/system", text="<html>502</html>")
    client = LibreNMSClient(async_get_clientsession(hass), BASE_URL, TOKEN)

    with pytest.raises(LibreNMSError, match="invalid JSON"):
        await client.async_get_system()


async def test_api_wraps_connection_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Transport failures surface as LibreNMSConnectionError."""
    aioclient_mock.get(
        f"{BASE_URL}/api/v0/system", exc=ClientConnectionError("refused")
    )
    client = LibreNMSClient(async_get_clientsession(hass), BASE_URL, TOKEN)

    with pytest.raises(LibreNMSConnectionError):
        await client.async_get_system()


async def test_api_wraps_timeouts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A slow instance is reported as a connection problem, not a crash."""
    aioclient_mock.get(f"{BASE_URL}/api/v0/system", exc=TimeoutError())
    client = LibreNMSClient(async_get_clientsession(hass), BASE_URL, TOKEN)

    with pytest.raises(LibreNMSConnectionError, match="Timeout"):
        await client.async_get_system()


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, LibreNMSAuthError),
        (403, LibreNMSPermissionError),
        (404, LibreNMSNotFoundError),
    ],
)
async def test_api_maps_status_codes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    error: type[LibreNMSError],
) -> None:
    """401 is a bad token; 403 is a valid token whose role is too narrow."""
    aioclient_mock.get(
        f"{BASE_URL}/api/v0/devices",
        status=status,
        json={"status": "error", "message": "nope"},
    )
    client = LibreNMSClient(async_get_clientsession(hass), BASE_URL, TOKEN)

    with pytest.raises(error) as raised:
        await client.async_get_devices()
    # A permission problem must never be mistaken for a bad token.
    assert isinstance(raised.value, LibreNMSAuthError) is (status == 401)


@pytest.mark.parametrize("key", ["devices", "alerts", "sensors"])
async def test_api_rejects_a_non_list_collection(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, key: str
) -> None:
    """A collection that is not a list is an error, not a crash in parsing."""
    path = "resources/sensors" if key == "sensors" else key
    aioclient_mock.get(
        f"{BASE_URL}/api/v0/{path}", json={"status": "ok", key: {"oops": 1}}
    )
    client = LibreNMSClient(async_get_clientsession(hass), BASE_URL, TOKEN)

    with pytest.raises(LibreNMSError, match="Unexpected response shape"):
        await getattr(client, f"async_get_{key}")()


async def test_reauth_is_raised_from_a_running_entry(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A token revoked while running triggers reauth on the next poll."""
    await setup_integration(hass, mock_config_entry)

    mock_librenms.status = 401
    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler("librenms")
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"


async def test_alerts_on_devices_the_token_cannot_see_are_dropped(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Counts stay consistent with the devices the token can actually see.

    LibreNMS filters /devices through hasDeviceAccess, so a Normal User only
    gets the devices assigned to it, while /alerts applies no per-device
    check and returns everything. Here only core-sw01 is assigned; the
    critical alert on the garage AP must not surface.
    """
    mock_librenms.set_devices(
        [d for d in mock_librenms.devices["devices"] if str(d["device_id"]) == "1"]
    )

    with caplog.at_level(logging.WARNING, logger="custom_components.librenms"):
        await setup_integration(hass, mock_config_entry)
        await async_poll(hass, freezer)

    active = hass.states.get("sensor.librenms_active_alerts")
    assert active.state == "1"
    assert [alert["id"] for alert in active.attributes["alerts"]] == [102]
    assert hass.states.get("sensor.librenms_critical_alerts").state == "0"
    assert hass.states.get("sensor.librenms_warning_alerts").state == "1"
    assert (
        hass.states.get("binary_sensor.librenms_problem").attributes["alerts_critical"]
        == 0
    )
    assert hass.states.get("binary_sensor.garage_ap_status") is None

    hints = [r for r in caplog.records if "Global Read" in r.message]
    assert len(hints) == 1
