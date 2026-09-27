"""Tests for LibreNMS alert events and de-duplication."""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.librenms.const import EVENT_ALERT
from custom_components.librenms.coordinator import LibreNMSAlert

from .conftest import MockLibreNMS, async_poll, setup_integration

ALERT_101 = {
    "id": "101",
    "device_id": "2",
    "hostname": "ap-garage.lan.example",
    "rule_id": "7",
    "name": "Device down due to no ICMP response",
    "state": "1",
    "severity": "critical",
    "timestamp": "2025-07-28 09:12:00",
}
ALERT_102 = {
    "id": "102",
    "device_id": "1",
    "hostname": "core-sw01.lan.example",
    "rule_id": "12",
    "name": "Port utilisation over 80%",
    "state": "1",
    "severity": "warning",
    "timestamp": "2025-07-28 08:55:00",
}
ALERT_103 = {
    "id": "103",
    "device_id": "1",
    "hostname": "core-sw01.lan.example",
    "rule_id": "3",
    "name": "High CPU",
    "state": "1",
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


# LibreNMS/Enum/AlertState.php: every state an open alert can move through.
ACKNOWLEDGED, WORSE, BETTER, CHANGED = "2", "3", "4", "5"


async def test_every_open_alert_state_is_requested(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """LibreNMS returns only state 1 by default, so all open states are asked for."""
    await setup_integration(hass, mock_config_entry)

    alert_urls = [
        call[1] for call in aioclient_mock.mock_calls if "/alerts" in str(call[1])
    ]
    assert alert_urls
    for url in alert_urls:
        assert sorted(url.query["state"].split(",")) == ["1", "2", "3", "4", "5"]


@pytest.mark.parametrize("state", [ACKNOWLEDGED, WORSE, BETTER, CHANGED])
async def test_state_change_is_not_a_recovery(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    state: str,
) -> None:
    """An alert moving between open states is the same fault, still open."""
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)

    mock_librenms.set_alerts([{**ALERT_101, "state": state}, ALERT_102])
    await async_poll(hass, freezer)

    assert events == []
    assert hass.states.get("sensor.librenms_active_alerts").state == "2"
    assert hass.states.get("sensor.librenms_critical_alerts").state == "1"
    assert hass.states.get("sensor.garage_ap_active_alerts").state == "1"
    assert hass.states.get("binary_sensor.librenms_problem").state == "on"


async def test_alert_walking_every_open_state_recovers_only_when_it_clears(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Only leaving the open set fires `recovered`, and it fires exactly once."""
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)

    for state in (ACKNOWLEDGED, WORSE, BETTER, CHANGED, ACKNOWLEDGED, "1"):
        mock_librenms.set_alerts([{**ALERT_101, "state": state}, ALERT_102])
        await async_poll(hass, freezer)
        assert events == [], state

    # LibreNMS sets the row to RECOVERED (0) once the fault clears, which
    # drops it out of the open states the integration asks for.
    mock_librenms.set_alerts([{**ALERT_101, "state": "0"}, ALERT_102])
    await async_poll(hass, freezer)
    await async_poll(hass, freezer)

    assert [(e.data["id"], e.data["event_type"]) for e in events] == [
        (101, "recovered")
    ]


async def test_acknowledged_alert_is_flagged_and_still_counted(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Acknowledging silences LibreNMS, not the fault: it stays in every count."""
    mock_librenms.set_alerts([{**ALERT_101, "state": ACKNOWLEDGED}, ALERT_102])
    await setup_integration(hass, mock_config_entry)

    alerts = hass.states.get("sensor.librenms_active_alerts").attributes["alerts"]
    flags = {alert["id"]: alert["acknowledged"] for alert in alerts}
    assert flags == {101: True, 102: False}

    assert hass.states.get("sensor.librenms_critical_alerts").state == "1"
    problem = hass.states.get("binary_sensor.librenms_problem")
    assert problem.state == "on"
    assert problem.attributes["alerts_critical"] == 1


async def test_acknowledging_then_escalating_still_refires(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Dedupe is on id and severity; an ack in between does not reset it."""
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)

    mock_librenms.set_alerts([ALERT_101, {**ALERT_102, "state": ACKNOWLEDGED}])
    await async_poll(hass, freezer)
    assert events == []

    escalated = {**ALERT_102, "state": WORSE, "severity": "critical"}
    mock_librenms.set_alerts([ALERT_101, escalated])
    await async_poll(hass, freezer)
    await async_poll(hass, freezer)

    assert [(e.data["id"], e.data["event_type"]) for e in events] == [(102, "critical")]
    assert events[0].data["acknowledged"] is False


@pytest.mark.parametrize(
    ("state", "is_open", "acknowledged"),
    [
        ("1", True, False),
        (2, True, True),
        ("3", True, False),
        ("5", True, False),
        (None, True, False),
        ("0", False, False),
    ],
)
def test_alert_state_parsing(state: Any, is_open: bool, acknowledged: bool) -> None:
    """A cleared row is dropped even if an instance ignores the state filter."""
    alert = LibreNMSAlert.from_api({"id": 1, "severity": "warning", "state": state})
    assert (alert is not None) is is_open
    if alert is not None:
        assert alert.acknowledged is acknowledged
