"""Tests for LibreNMS alert events and de-duplication."""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.librenms.const import EVENT_ALERT

from .conftest import MockLibreNMS, async_poll, setup_integration

ALERT_101 = {
    "id": "101",
    "device_id": "2",
    "hostname": "ap-garage.lan.example",
    "rule_id": "7",
    "name": "Device down due to no ICMP response",
    "severity": "critical",
    "timestamp": "2025-07-28 09:12:00",
}
ALERT_102 = {
    "id": "102",
    "device_id": "1",
    "hostname": "core-sw01.lan.example",
    "rule_id": "12",
    "name": "Port utilisation over 80%",
    "severity": "warning",
    "timestamp": "2025-07-28 08:55:00",
}
ALERT_103 = {
    "id": "103",
    "device_id": "1",
    "hostname": "core-sw01.lan.example",
    "rule_id": "3",
    "name": "High CPU",
    "severity": "critical",
    "timestamp": "2025-07-28 10:00:00",
}


async def test_open_alerts_do_not_replay_on_startup(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Alerts already open when Home Assistant starts must stay quiet."""
    events = async_capture_events(hass, EVENT_ALERT)
    await setup_integration(hass, mock_config_entry)

    assert events == []
    assert hass.states.get("event.librenms_alerts").state == "unknown"


async def test_unchanged_alert_does_not_refire(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The same alert ID at the same severity fires at most once."""
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)

    await async_poll(hass, freezer)
    await async_poll(hass, freezer)

    assert events == []


async def test_new_alert_fires_once(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A newly opened alert fires exactly one event, then goes quiet."""
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)

    mock_librenms.set_alerts([ALERT_101, ALERT_102, ALERT_103])
    await async_poll(hass, freezer)

    assert len(events) == 1
    payload: dict[str, Any] = events[0].data
    assert payload["event_type"] == "critical"
    assert payload["id"] == 103
    assert payload["rule"] == "High CPU"
    assert payload["hostname"] == "core-sw01.lan.example"
    assert payload["entry_id"] == mock_config_entry.entry_id

    state = hass.states.get("event.librenms_alerts")
    assert state.attributes["event_type"] == "critical"
    assert state.attributes["id"] == 103

    # A second poll with the same alert set must not fire again.
    await async_poll(hass, freezer)
    assert len(events) == 1


async def test_escalation_refires(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An alert that escalates to critical fires again at the new severity."""
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)

    mock_librenms.set_alerts([ALERT_101, {**ALERT_102, "severity": "critical"}])
    await async_poll(hass, freezer)

    assert len(events) == 1
    assert events[0].data["id"] == 102
    assert events[0].data["event_type"] == "critical"


async def test_recovery_fires_with_the_original_detail(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A cleared alert reports the rule and host it was originally for."""
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)

    mock_librenms.set_alerts([ALERT_102])
    await async_poll(hass, freezer)

    assert len(events) == 1
    assert events[0].data["event_type"] == "recovered"
    assert events[0].data["id"] == 101
    assert events[0].data["hostname"] == "ap-garage.lan.example"
    assert events[0].data["rule"] == "Device down due to no ICMP response"

    assert hass.states.get("event.librenms_alerts").attributes["event_type"] == (
        "recovered"
    )


async def test_burst_of_alerts_all_fire(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Several changes in one poll each produce their own event."""
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)

    # 101 clears, 103 opens, 102 stays as it was.
    mock_librenms.set_alerts([ALERT_102, ALERT_103])
    await async_poll(hass, freezer)

    assert len(events) == 2
    assert {(event.data["id"], event.data["event_type"]) for event in events} == {
        (103, "critical"),
        (101, "recovered"),
    }
