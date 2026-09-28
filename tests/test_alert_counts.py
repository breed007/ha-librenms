"""Tests that alert counts follow the same rule as alert events.

An alert counts while its device is in Home Assistant: in LibreNMS's latest
device list, or registered for the entry from an earlier update. Alerts on
devices the API token has never listed count for nothing (QA live lab, V-A).
"""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.librenms.const import EVENT_ALERT

from .conftest import (
    MockLibreNMS,
    async_poll,
    get_device,
    remove_device,
    setup_integration,
)
from .test_event import ALERT_101, ALERT_102

ACTIVE = "sensor.librenms_active_alerts"
CRITICAL = "sensor.librenms_critical_alerts"
WARNING = "sensor.librenms_warning_alerts"
PROBLEM = "binary_sensor.librenms_problem"


def _hide(mock_librenms: MockLibreNMS, device_id: str) -> None:
    mock_librenms.set_devices(
        [
            d
            for d in mock_librenms.devices["devices"]
            if str(d["device_id"]) != device_id
        ]
    )


def _counts(hass: HomeAssistant) -> tuple[str, str, str]:
    return tuple(hass.states.get(e).state for e in (ACTIVE, CRITICAL, WARNING))


def _listed(hass: HomeAssistant) -> dict[int, bool]:
    return {
        alert["id"]: alert["device_listed"]
        for alert in hass.states.get(ACTIVE).attributes["alerts"]
    }


async def test_hidden_warning_is_counted_and_listed(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A warning on a missing device used to appear in no entity at all."""
    await setup_integration(hass, mock_config_entry)
    _hide(mock_librenms, "1")
    await async_poll(hass, freezer)

    assert _counts(hass) == ("2", "1", "1")
    assert _listed(hass) == {101: True, 102: False}


async def test_hidden_critical_is_counted_when_it_pages(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The page and the critical count agree.

    A critical alert opens on device 2 while it is missing from the list.
    The `critical` event fires, and "Critical alerts" reads 1, not 0.
    """
    mock_librenms.set_alerts([ALERT_102])
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)
    _hide(mock_librenms, "2")
    await async_poll(hass, freezer)

    mock_librenms.set_alerts([ALERT_102, ALERT_101])
    await async_poll(hass, freezer)

    assert [(e.data["id"], e.data["event_type"]) for e in events] == [(101, "critical")]
    assert events[0].data["device_listed"] is False
    assert hass.states.get(CRITICAL).state == "1"
    problem = hass.states.get(PROBLEM)
    assert problem.state == "on"
    assert problem.attributes["alerts_critical"] == 1
    assert problem.attributes["alerts_critical_hidden"] == 1
    assert [a["id"] for a in problem.attributes["hidden_alerts"]] == [101]


async def test_counts_are_the_same_after_a_reload(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The device registry outlives a reload, so the counts do too."""
    await setup_integration(hass, mock_config_entry)
    _hide(mock_librenms, "2")
    await async_poll(hass, freezer)
    before = _counts(hass), _listed(hass)

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert before == (("2", "1", "1"), {101: False, 102: True})
    assert (_counts(hass), _listed(hass)) == before


async def test_deleted_device_alerts_stop_counting(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Deleting a device that left the token's view takes its alerts out."""
    await setup_integration(hass, mock_config_entry)
    _hide(mock_librenms, "2")
    await async_poll(hass, freezer)
    assert _counts(hass) == ("2", "1", "1")

    response = await remove_device(
        hass, hass_ws_client, mock_config_entry, get_device(hass, mock_config_entry, 2)
    )
    assert response["success"], response
    await async_poll(hass, freezer)

    assert _counts(hass) == ("1", "0", "1")
    assert _listed(hass) == {102: True}
    assert hass.states.get(PROBLEM).state == "off"


async def test_never_listed_device_counts_for_nothing(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """/alerts has no permission filter; a narrow token still sees them.

    Device 99 was never in the device list, so it is not in Home Assistant
    and its alert stays out of every count, list and event.
    """
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)
    mock_librenms.set_alerts(
        [ALERT_101, ALERT_102, {**ALERT_101, "id": "301", "device_id": "99"}]
    )
    await async_poll(hass, freezer)

    assert _counts(hass) == ("2", "1", "1")
    assert 301 not in _listed(hass)
    assert events == []
