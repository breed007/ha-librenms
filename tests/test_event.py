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
    "name": "Port utilization over 80%",
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


async def test_de_escalation_fires_the_new_severity(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A critical alert that drops to warning fires `warning`, not recovery.

    Pins the behavior the README documents (QA live lab, N-6).
    """
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)

    mock_librenms.set_alerts([{**ALERT_101, "severity": "warning"}, ALERT_102])
    await async_poll(hass, freezer)

    assert [(e.data["id"], e.data["event_type"]) for e in events] == [(101, "warning")]


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


def _fired(events: list) -> list[tuple[int, str]]:
    return [(event.data["id"], event.data["event_type"]) for event in events]


@pytest.mark.parametrize("blip", ["empty_list", "no_devices_key"])
async def test_one_empty_device_response_fires_nothing(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    blip: str,
) -> None:
    """QA's scenario: one empty /devices poll, then a normal one.

    Nothing changed in LibreNMS, so no alert may recover or re-open, and the
    problem sensor must never report an all-clear. Before the fix this fired
    `recovered` for both alerts, turned the problem sensor off, then fired
    both again as new.
    """
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)
    good = mock_librenms.devices
    problem_states = [hass.states.get("binary_sensor.librenms_problem").state]

    if blip == "empty_list":
        mock_librenms.set_devices([])
    else:
        mock_librenms.devices = {"status": "ok", "count": 0}
    await async_poll(hass, freezer)
    problem_states.append(hass.states.get("binary_sensor.librenms_problem").state)

    mock_librenms.devices = good
    await async_poll(hass, freezer)
    await async_poll(hass, freezer)
    problem_states.append(hass.states.get("binary_sensor.librenms_problem").state)

    assert _fired(events) == []
    assert "off" not in problem_states
    assert problem_states[-1] == "on"


async def test_device_leaving_and_returning_fires_nothing(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A device missing from one /devices response is not a cleared fault.

    Its alert stays open in LibreNMS the whole time, so there is nothing to
    recover and nothing new when the device comes back.
    """
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)
    good = mock_librenms.devices

    mock_librenms.set_devices(
        [d for d in good["devices"] if str(d["device_id"]) != "2"]
    )
    await async_poll(hass, freezer)
    # While hidden, the alert is still counted: its device is still in
    # Home Assistant.
    assert hass.states.get("sensor.librenms_critical_alerts").state == "1"

    mock_librenms.devices = good
    await async_poll(hass, freezer)
    await async_poll(hass, freezer)

    assert _fired(events) == []
    assert hass.states.get("sensor.librenms_critical_alerts").state == "1"


async def test_alert_clearing_while_its_device_is_hidden_recovers_once(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A real recovery still fires, with the detail from /alerts."""
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)
    good = mock_librenms.devices

    mock_librenms.set_devices(
        [d for d in good["devices"] if str(d["device_id"]) != "2"]
    )
    await async_poll(hass, freezer)
    mock_librenms.set_alerts([ALERT_102])
    await async_poll(hass, freezer)
    mock_librenms.devices = good
    await async_poll(hass, freezer)

    assert _fired(events) == [(101, "recovered")]
    assert events[0].data["rule"] == "Device down due to no ICMP response"
    assert events[0].data["hostname"] == "ap-garage.lan.example"


async def test_alert_opening_while_its_device_is_hidden_fires_once(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An alert on a device Home Assistant has is announced when it opens.

    Device 1 is registered but missing from this device list. Its new alert
    still counts, because the device is in Home Assistant, and the device
    coming back is not a reason to announce it a second time.
    """
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)
    good = mock_librenms.devices

    mock_librenms.set_devices(
        [d for d in good["devices"] if str(d["device_id"]) != "1"]
    )
    mock_librenms.set_alerts([ALERT_101, ALERT_102, ALERT_103])
    await async_poll(hass, freezer)
    assert _fired(events) == [(103, "critical")]

    mock_librenms.devices = good
    await async_poll(hass, freezer)
    await async_poll(hass, freezer)

    assert _fired(events) == [(103, "critical")]


async def test_alerts_on_a_device_never_visible_never_fire(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A token that cannot see a device gets no events about it at all.

    This is a Normal User without that device: /alerts still returns its
    alerts, which open and clear without a word to Home Assistant.
    """
    mock_librenms.set_devices(
        [d for d in mock_librenms.devices["devices"] if str(d["device_id"]) != "2"]
    )
    mock_librenms.set_alerts([ALERT_102])
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)

    mock_librenms.set_alerts([ALERT_101, ALERT_102])
    await async_poll(hass, freezer)
    mock_librenms.set_alerts([ALERT_102])
    await async_poll(hass, freezer)

    assert _fired(events) == []


async def test_alert_open_at_startup_on_a_hidden_device_does_not_replay(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Starting during a blip must not replay alerts once devices return."""
    good = dict(mock_librenms.devices)
    mock_librenms.set_devices(
        [d for d in good["devices"] if str(d["device_id"]) != "2"]
    )
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)

    mock_librenms.devices = good
    await async_poll(hass, freezer)

    assert _fired(events) == []


async def test_alert_never_shown_that_clears_fires_nothing(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """QA's N5: no `recovered` for an alert the user was never shown.

    Alert 101 is open at startup, but its device is not in the list, so the
    user never saw it. It then clears while the device is still hidden.
    """
    mock_librenms.set_devices(
        [d for d in mock_librenms.devices["devices"] if str(d["device_id"]) != "2"]
    )
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)

    mock_librenms.set_alerts([ALERT_102])
    await async_poll(hass, freezer)
    await async_poll(hass, freezer)

    assert _fired(events) == []


async def test_event_entity_does_not_replay_on_a_failed_update(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """QA's N6: a failed update hands listeners the old data; it is not news.

    Home Assistant calls listeners with the previous poll's data when an
    update fails. The event entity used to fire that poll's alerts again,
    so it showed a second, later event for alert 103.
    """
    await setup_integration(hass, mock_config_entry)
    mock_librenms.set_alerts([ALERT_101, ALERT_102, ALERT_103])
    await async_poll(hass, freezer)
    fired_at = hass.states.get("event.librenms_alerts").state

    mock_librenms.status = 500
    await async_poll(hass, freezer)
    assert hass.states.get("event.librenms_alerts").state == "unavailable"
    mock_librenms.status = 200
    await async_poll(hass, freezer)
    await async_poll(hass, freezer)

    assert hass.states.get("event.librenms_alerts").state == fired_at
