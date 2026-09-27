"""Tests for how far an empty /devices response is trusted.

The model (see CLAUDE.md, "Trusting the device list"):

- The integration expects devices once it has published some, or, before its
  first successful update, when the device registry already holds devices
  for the entry.
- While it expects devices, an empty list fails the update. Three empty
  lists in a row, with nothing else in between, are accepted as real.
- Alert events and the problem sensor follow LibreNMS's open alerts, not
  which devices came back, so neither a blip nor an accepted empty list
  produces an event or an all-clear for an alert that is still open.
"""

from __future__ import annotations

from collections.abc import Callable
import logging

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.librenms.const import (
    CONF_INCLUDE_DISABLED,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    DOMAIN,
    EMPTY_DEVICE_POLLS,
    EVENT_ALERT,
)

from .conftest import MockLibreNMS, async_poll, get_device, setup_integration

PROBLEM = "binary_sensor.librenms_problem"
DEVICES = "sensor.librenms_devices"


def _record(hass: HomeAssistant, entity_id: str) -> list[str]:
    """Collect every state an entity takes from now on."""
    states: list[str] = []

    def _listener(event: Event) -> None:
        if event.data["entity_id"] == entity_id and event.data["new_state"]:
            states.append(event.data["new_state"].state)

    hass.bus.async_listen("state_changed", _listener)
    return states


def _fired(events: list[Event]) -> list[tuple[int, str]]:
    return [(event.data["id"], event.data["event_type"]) for event in events]


async def _polls(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    mock_librenms: MockLibreNMS,
    pattern: str,
    good: dict,
    each: Callable[[], None] | None = None,
) -> None:
    """Run one poll per letter: E = empty list, G = normal list, X = HTTP 500."""
    for step in pattern:
        mock_librenms.recover("devices")
        if step == "E":
            mock_librenms.set_devices([])
        elif step == "G":
            mock_librenms.devices = good
        else:
            mock_librenms.devices = good
            mock_librenms.fail("devices", status=500)
        await async_poll(hass, freezer)
        if each:
            each()
    mock_librenms.recover("devices")


async def test_empty_list_during_a_reload_is_not_an_all_clear(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """QA's N2: an empty list at startup contradicts the devices HA already has.

    Reconfigure and saving options both reload the entry. Before the fix the
    reload accepted the empty list: the problem sensor went off, the device
    count showed 0, and a warning blamed the token's role.
    """
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)
    problem = _record(hass, PROBLEM)
    devices = _record(hass, DEVICES)
    good = mock_librenms.devices

    mock_librenms.set_devices([])
    with caplog.at_level(logging.WARNING, logger="custom_components.librenms"):
        await hass.config_entries.async_reload(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY

        mock_librenms.devices = good
        await async_poll(hass, freezer)
        await async_poll(hass, freezer)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert "off" not in problem
    assert "0" not in devices
    assert hass.states.get(PROBLEM).state == "on"
    assert _fired(events) == []
    assert "Global Read" not in caplog.text


async def test_emptied_instance_still_loads_after_a_restart(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The count survives setup retries, so a real empty list is accepted.

    Home Assistant builds a new coordinator for every setup attempt; if the
    count lived there, a genuinely emptied instance would never finish setup.
    """
    await setup_integration(hass, mock_config_entry)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    mock_librenms.set_devices([])
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY

    for _ in range(4):
        await async_poll(hass, freezer, seconds=300)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(DEVICES).state == "0"


@pytest.mark.parametrize("pattern", ["EXEXE", "EEXEE", "EEGEE"])
async def test_only_consecutive_empty_lists_are_accepted(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    pattern: str,
) -> None:
    """QA's N3: a failed poll between empty lists breaks the run too."""
    await setup_integration(hass, mock_config_entry)
    devices = _record(hass, DEVICES)

    await _polls(hass, freezer, mock_librenms, pattern, mock_librenms.devices)

    assert "0" not in devices


async def test_accepted_empty_list_keeps_open_alerts_a_problem(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Accepting an empty list does not clear a fault LibreNMS still has open.

    Alert 101 (critical) was shown before the token lost sight of its
    device. Until LibreNMS clears it, the problem sensor stays on and says
    why. It clears, with one `recovered`, when LibreNMS clears the alert.
    """
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)
    problem = _record(hass, PROBLEM)

    await _polls(hass, freezer, mock_librenms, "EEE", mock_librenms.devices)

    assert hass.states.get(DEVICES).state == "0"
    state = hass.states.get(PROBLEM)
    assert state.state == "on"
    assert state.attributes["alerts_critical"] == 0
    assert state.attributes["alerts_critical_hidden"] == 1
    assert "off" not in problem
    assert _fired(events) == []

    mock_librenms.set_alerts([])
    await async_poll(hass, freezer)

    assert hass.states.get(PROBLEM).state == "off"
    assert sorted(_fired(events)) == [(101, "recovered"), (102, "recovered")]


async def test_partial_list_keeps_open_alerts_a_problem(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A device missing from one response does not clear its open alert."""
    await setup_integration(hass, mock_config_entry)
    problem = _record(hass, PROBLEM)
    good = mock_librenms.devices

    mock_librenms.set_devices(
        [d for d in good["devices"] if str(d["device_id"]) != "2"]
    )
    await async_poll(hass, freezer)
    assert hass.states.get(PROBLEM).attributes["alerts_critical_hidden"] == 1

    mock_librenms.devices = good
    await async_poll(hass, freezer)

    assert "off" not in problem
    assert hass.states.get(PROBLEM).attributes["alerts_critical_hidden"] == 0


async def test_alert_never_shown_does_not_hold_the_problem_on(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Alerts on devices the token never saw still stay out of the problem.

    This is the Normal User case from round 1: counts, and now the problem
    sensor, only reflect alerts the user could see.
    """
    mock_librenms.set_devices(
        [
            {**d, "status": 1}
            for d in mock_librenms.devices["devices"]
            if str(d["device_id"]) != "2"
        ]
    )
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(PROBLEM)
    assert state.state == "off"
    assert state.attributes["alerts_critical_hidden"] == 0


async def test_accepted_empty_list_does_not_reset_the_stalled_poller(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """QA's N7: no poll times to judge is not the poller making progress.

    `last_polled` is frozen, so the poller is stale. A stretch of accepted
    empty lists used to count as the marker moving, so when the same frozen
    devices came back the stale sensor went off for another 15 minutes.
    """
    await setup_integration(hass, mock_config_entry)
    good = mock_librenms.devices
    await async_poll(hass, freezer, seconds=901)
    assert hass.states.get("binary_sensor.librenms_poller_stale").state == "on"

    await _polls(hass, freezer, mock_librenms, "EEE", good)
    mock_librenms.devices = good
    await async_poll(hass, freezer)

    stale = hass.states.get("binary_sensor.librenms_poller_stale")
    assert stale.state == "on"
    assert stale.attributes["last_advanced"] == "2025-07-28 09:14:03"
    assert stale.attributes["stalled_for_seconds"] > 900 + 4 * 61


async def test_blip_during_retry_count_is_not_carried_into_a_new_run(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Empty lists before a good poll do not count toward a later run."""
    await setup_integration(hass, mock_config_entry)
    good = mock_librenms.devices
    devices = _record(hass, DEVICES)

    await _polls(hass, freezer, mock_librenms, "EEG", good)
    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    await _polls(hass, freezer, mock_librenms, "E", good)

    assert "0" not in devices
    assert hass.states.get(DEVICES).state == STATE_UNAVAILABLE


# Rule 2 has to hold across a reload or restart, not just within one run of
# the coordinator. The device registry is the integration's lasting record of
# which devices the user has seen, so at startup an open alert on a device
# already in the registry counts as seen (QA round 3, F1).


async def test_seen_alert_survives_a_reload_after_empty_lists(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """QA's repro A: two empty lists, then the user clicks Reload.

    The reload's first update is the third empty list in a row, so it is
    accepted at startup. Critical alert 101 is still open in LibreNMS, so
    the problem sensor must stay on, and must not turn off then on again
    when the devices come back.
    """
    await setup_integration(hass, mock_config_entry)
    events = async_capture_events(hass, EVENT_ALERT)
    good = mock_librenms.devices
    mock_librenms.set_devices([])
    await async_poll(hass, freezer)
    await async_poll(hass, freezer)
    problem = _record(hass, PROBLEM)

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(DEVICES).state == "0"
    state = hass.states.get(PROBLEM)
    assert state.state == "on"
    assert state.attributes["alerts_critical_hidden"] == 1

    mock_librenms.devices = good
    await async_poll(hass, freezer)

    assert "off" not in problem
    assert hass.states.get(PROBLEM).state == "on"
    assert _fired(events) == []


async def test_seen_alert_survives_a_restart_during_a_long_blip(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """QA's repro B: Home Assistant restarts while the list stays empty.

    A restart loses everything in memory, including the empty-list count;
    the entry retries setup until the third empty list is accepted.
    """
    await setup_integration(hass, mock_config_entry)
    good = mock_librenms.devices
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    hass.data.get(DOMAIN, {}).pop(EMPTY_DEVICE_POLLS, None)
    mock_librenms.set_devices([])
    problem = _record(hass, PROBLEM)
    events = async_capture_events(hass, EVENT_ALERT)

    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    for _ in range(4):
        await async_poll(hass, freezer, seconds=300)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    state = hass.states.get(PROBLEM)
    assert state.state == "on"
    assert state.attributes["alerts_critical_hidden"] == 1

    mock_librenms.devices = good
    await async_poll(hass, freezer)

    assert "off" not in problem
    assert _fired(events) == []


async def test_seen_alert_survives_an_options_save_while_hidden(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """QA's repro C: the device leaves the token's view for good.

    Saving options reloads the entry. The alert is still open, so the
    problem sensor keeps saying so.
    """
    await setup_integration(hass, mock_config_entry)
    mock_librenms.set_devices(
        [d for d in mock_librenms.devices["devices"] if str(d["device_id"]) != "2"]
    )
    await async_poll(hass, freezer)
    assert hass.states.get(PROBLEM).state == "on"
    problem = _record(hass, PROBLEM)

    coordinator = mock_config_entry.runtime_data
    # Options only reload the entry when they change.
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 120, CONF_INCLUDE_DISABLED: False},
    )
    await hass.async_block_till_done()
    assert mock_config_entry.runtime_data is not coordinator
    await async_poll(hass, freezer, seconds=121)

    assert "off" not in problem
    state = hass.states.get(PROBLEM)
    assert state.state == "on"
    assert state.attributes["alerts_critical_hidden"] == 1


async def test_deleting_the_stale_device_releases_its_alert(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The way out when a device is gone from the token's view for good.

    Deleting the device in Home Assistant says the user no longer wants to
    hear about it. The problem sensor lets go of its alert straight away,
    and it stays released after a reload.
    """
    await setup_integration(hass, mock_config_entry)
    mock_librenms.set_devices(
        [d for d in mock_librenms.devices["devices"] if str(d["device_id"]) != "2"]
    )
    await async_poll(hass, freezer)
    assert hass.states.get(PROBLEM).attributes["alerts_critical_hidden"] == 1

    # The request the Delete button on the device page sends.
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    device = get_device(hass, mock_config_entry, 2)
    await client.send_json_auto_id(
        {
            "type": "config/device_registry/remove_config_entry",
            "config_entry_id": mock_config_entry.entry_id,
            "device_id": device.id,
        }
    )
    response = await client.receive_json()
    assert response["success"], response
    assert get_device(hass, mock_config_entry, 2) is None
    await async_poll(hass, freezer)

    state = hass.states.get(PROBLEM)
    assert state.state == "off"
    assert state.attributes["alerts_critical_hidden"] == 0

    await hass.config_entries.async_reload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(PROBLEM).state == "off"


async def test_stalled_poller_stays_a_problem_through_an_empty_list(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """QA round 3, F7: no poll times to judge is not "the poller is fine".

    The stalled poller is the only problem here. While the device list is
    empty there is nothing to judge, so the last judgment carries over
    instead of flapping the problem sensor off and back on.
    """
    mock_librenms.set_devices(
        [{**d, "status": 1} for d in mock_librenms.devices["devices"]]
    )
    mock_librenms.set_alerts([])
    await setup_integration(hass, mock_config_entry)
    good = mock_librenms.devices
    await async_poll(hass, freezer, seconds=901)
    assert hass.states.get(PROBLEM).state == "on"
    problem = _record(hass, PROBLEM)
    stale = _record(hass, "binary_sensor.librenms_poller_stale")

    await _polls(hass, freezer, mock_librenms, "EEEE", good)
    assert hass.states.get("binary_sensor.librenms_poller_stale").state == "on"
    mock_librenms.devices = good
    await async_poll(hass, freezer)

    assert "off" not in problem
    assert "off" not in stale
    assert hass.states.get(PROBLEM).state == "on"


async def test_empty_list_does_not_invent_a_stalled_poller(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Carrying the last judgment over works both ways: off stays off.

    An emptied instance has no poll times at all; that must not turn into
    a stale-poller alarm 15 minutes later.
    """
    await setup_integration(hass, mock_config_entry)
    good = mock_librenms.devices
    await _polls(hass, freezer, mock_librenms, "EEE", good)
    for _ in range(20):
        mock_librenms.set_devices([])
        await async_poll(hass, freezer)

    assert hass.states.get("binary_sensor.librenms_poller_stale").state == "off"


def _devices_state(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    """Return the device-count sensor state for one entry."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_devices_total"
    )
    assert entity_id is not None
    return hass.states.get(entity_id).state


async def test_empty_list_count_is_kept_per_entry(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Two instances each need three empty lists of their own (QA round 3, F3).

    Both entries see the same empty lists here. A count shared between them
    would reach three on the second poll and accept one entry early.
    """
    other = MockConfigEntry(
        domain=DOMAIN,
        title="https://nms2.example.com",
        unique_id="https://nms2.example.com",
        data={**mock_config_entry.data, CONF_URL: "https://nms2.example.com"},
        options=dict(mock_config_entry.options),
    )
    await setup_integration(hass, mock_config_entry)
    await setup_integration(hass, other)
    mock_librenms.set_devices([])

    for _ in range(2):
        await async_poll(hass, freezer)
        assert _devices_state(hass, mock_config_entry) == STATE_UNAVAILABLE
        assert _devices_state(hass, other) == STATE_UNAVAILABLE

    await async_poll(hass, freezer)
    assert _devices_state(hass, mock_config_entry) == "0"
    assert _devices_state(hass, other) == "0"


async def test_empty_list_count_is_removed_with_the_entry(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The count survives an unload (reloads continue the run) but not removal."""
    await setup_integration(hass, mock_config_entry)
    mock_librenms.set_devices([])
    await async_poll(hass, freezer)
    counts = hass.data[DOMAIN][EMPTY_DEVICE_POLLS]
    assert counts == {mock_config_entry.entry_id: 1}

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert counts == {mock_config_entry.entry_id: 1}

    await hass.config_entries.async_remove(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.entry_id not in counts
