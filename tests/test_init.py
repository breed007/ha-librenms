"""Tests for LibreNMS setup, teardown and coordinator behaviour."""

from __future__ import annotations

from datetime import timedelta

from aiohttp import ClientConnectionError
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from custom_components.librenms import async_remove_config_entry_device
from custom_components.librenms.const import (
    CONF_INCLUDE_DISABLED,
    CONF_SCAN_INTERVAL,
    DOMAIN,
)

from .conftest import MockLibreNMS, async_poll, setup_integration
from .const import BASE_URL, ENTRY_DATA


async def test_setup_and_unload(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The entry loads, creates entities, and unloads cleanly."""
    assert await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED

    assert hass.states.get("sensor.librenms_devices") is not None

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_hub_and_devices_registered(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Each LibreNMS device becomes an HA device linked to the hub."""
    await setup_integration(hass, mock_config_entry)
    registry = dr.async_get(hass)

    hub = registry.async_get_device(identifiers={(DOMAIN, mock_config_entry.entry_id)})
    assert hub is not None
    assert hub.configuration_url == BASE_URL
    assert hub.sw_version == "25.7.0"

    device = registry.async_get_device(
        identifiers={(DOMAIN, f"{mock_config_entry.entry_id}_1")}
    )
    assert device is not None
    # `display` merely repeats the hostname here, so sysName wins.
    assert device.name == "core-sw01"
    # The vendor comes from the icon, not the os driver: this device runs the
    # legacy `edgeswitch` driver but is Ubiquiti hardware.
    assert device.manufacturer == "Ubiquiti"
    assert device.model == "Ubiquiti EdgeSwitch 24"
    assert device.sw_version == "1.9.3"
    assert device.via_device_id == hub.id
    assert device.configuration_url == f"{BASE_URL}/device/device=1/"

    # `display` wins over `sysName` for the device name, as it does in the
    # LibreNMS UI.
    garage = registry.async_get_device(
        identifiers={(DOMAIN, f"{mock_config_entry.entry_id}_2")}
    )
    assert garage is not None
    assert garage.name == "Garage AP"


async def test_device_without_hardware_still_has_a_manufacturer(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """`hardware` is frequently empty, but the icon still names a vendor.

    On a real homelab 8 of 22 devices reported no hardware string at all.
    """
    mock_librenms.set_devices(
        [
            {
                "device_id": 1,
                "hostname": "192.168.10.40",
                "sysName": "navigator",
                "display": "192.168.10.40",
                "status": 1,
                "os": "linux",
                "hardware": None,
                "icon": "linux.svg",
            }
        ]
    )
    await setup_integration(hass, mock_config_entry)

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, f"{mock_config_entry.entry_id}_1")}
    )
    assert device is not None
    assert device.manufacturer == "Linux"
    assert device.model is None
    assert device.name == "navigator"


async def test_device_name_prefers_sysname_over_a_mirrored_display(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A fleet added by IP must not end up named after its addresses.

    LibreNMS computes `display` from a global template that defaults to the
    hostname, so it is always populated and usually identical to it. A real
    override still wins.
    """
    mock_librenms.set_devices(
        [
            # display mirrors hostname -> sysName should win
            {
                "device_id": 1,
                "hostname": "192.168.10.1",
                "sysName": "utopia",
                "display": "192.168.10.1",
                "status": 1,
                "icon": "linux.svg",
            },
            # a genuine display override -> it wins over sysName
            {
                "device_id": 2,
                "hostname": "192.168.10.10",
                "sysName": "star",
                "display": "Core Switch",
                "status": 1,
                "icon": "ubiquiti.svg",
            },
            # nothing but a hostname -> fall all the way back
            {
                "device_id": 3,
                "hostname": "192.168.10.99",
                "status": 1,
                "icon": "ubiquiti.svg",
            },
        ]
    )
    await setup_integration(hass, mock_config_entry)

    registry = dr.async_get(hass)
    names = {
        i: registry.async_get_device(
            identifiers={(DOMAIN, f"{mock_config_entry.entry_id}_{i}")}
        ).name
        for i in (1, 2, 3)
    }
    assert names == {1: "utopia", 2: "Core Switch", 3: "192.168.10.99"}


@pytest.mark.parametrize(
    ("icon", "expected"),
    [
        # The legacy EdgeSwitch driver is still Ubiquiti hardware, and the
        # icon is what makes that visible.
        ("ubiquiti.svg", "Ubiquiti"),
        ("synology.svg", "Synology"),
        ("apple.svg", "Apple"),
        ("proxmox.svg", "Proxmox"),
        ("images/os/brother.png", "Brother"),
        ("aruba-instant.svg", "Aruba Instant"),
        ("", None),
        (None, None),
    ],
)
def test_vendor_from_icon(icon: str | None, expected: str | None) -> None:
    """The icon filename is the API's closest thing to a manufacturer."""
    from custom_components.librenms.coordinator import _vendor_from_icon

    assert _vendor_from_icon(icon) == expected


async def test_auth_failure_triggers_reauth(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A rejected token starts a reauth flow instead of retrying forever."""
    mock_librenms.status = 401
    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH


async def test_setup_retries_on_connection_error(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """An unreachable instance leaves the entry in retry, not error."""
    mock_librenms.exception = ClientConnectionError("no route to host")
    await setup_integration(hass, mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_entities_recover_after_outage(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Entities go unavailable during an outage and come back afterwards."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("sensor.librenms_devices").state == "2"

    mock_librenms.exception = ClientConnectionError("instance restarted")
    await async_poll(hass, freezer)

    assert hass.states.get("sensor.librenms_devices").state == STATE_UNAVAILABLE
    assert hass.states.get("binary_sensor.core_sw01_status").state == STATE_UNAVAILABLE

    mock_librenms.exception = None
    await async_poll(hass, freezer)

    assert hass.states.get("sensor.librenms_devices").state == "2"
    assert hass.states.get("binary_sensor.core_sw01_status").state == "on"


@pytest.mark.parametrize(
    ("include_disabled", "total", "up", "down", "excluded"),
    [
        (False, "2", "1", "1", 2),
        (True, "4", "2", "2", 0),
    ],
)
async def test_disabled_device_counting(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    include_disabled: bool,
    total: str,
    up: str,
    down: str,
    excluded: int,
) -> None:
    """Disabled and ignored devices are excluded from counts by default."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=BASE_URL,
        unique_id=BASE_URL,
        data=ENTRY_DATA,
        options={CONF_SCAN_INTERVAL: 60, CONF_INCLUDE_DISABLED: include_disabled},
    )
    await setup_integration(hass, entry)

    assert hass.states.get("sensor.librenms_devices").state == total
    assert hass.states.get("sensor.librenms_devices_up").state == up
    assert hass.states.get("sensor.librenms_devices_down").state == down
    assert entry.runtime_data.data.devices_excluded == excluded


async def test_new_device_is_added_without_reload(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A device added in LibreNMS shows up on the next poll."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("binary_sensor.new_switch_status") is None

    devices = list(mock_librenms.devices["devices"])
    devices.append(
        {
            "device_id": 5,
            "hostname": "new-switch.lan.example",
            "sysName": "new-switch",
            "status": 1,
            "uptime": 120,
            "hardware": "Netgear GS308",
            "os": "netgear",
            "version": "1.0.0",
            "disabled": 0,
            "ignore": 0,
        }
    )
    mock_librenms.set_devices(devices)

    await async_poll(hass, freezer)

    assert hass.states.get("binary_sensor.new_switch_status").state == "on"


async def test_removed_device_becomes_unavailable(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Entities for a deleted LibreNMS device stop reporting stale state."""
    await setup_integration(hass, mock_config_entry)
    assert hass.states.get("binary_sensor.core_sw01_status").state == "on"

    mock_librenms.set_devices(
        [d for d in mock_librenms.devices["devices"] if str(d["device_id"]) != "1"]
    )

    await async_poll(hass, freezer)

    assert hass.states.get("binary_sensor.core_sw01_status").state == STATE_UNAVAILABLE


async def test_scan_interval_option_applies(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Saving options reloads the entry so the new interval takes effect."""
    await setup_integration(hass, mock_config_entry)
    assert mock_config_entry.runtime_data.update_interval == timedelta(seconds=60)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 300, CONF_INCLUDE_DISABLED: True},
    )
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    assert coordinator.update_interval == timedelta(seconds=300)
    assert coordinator.include_disabled is True
    assert hass.states.get("sensor.librenms_devices").state == "4"


async def test_stale_device_can_be_removed(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A device deleted in LibreNMS can be removed from HA, others cannot."""
    await setup_integration(hass, mock_config_entry)
    registry = dr.async_get(hass)

    hub = registry.async_get_device(identifiers={(DOMAIN, mock_config_entry.entry_id)})
    still_there = registry.async_get_device(
        identifiers={(DOMAIN, f"{mock_config_entry.entry_id}_2")}
    )
    stale = registry.async_get_device(
        identifiers={(DOMAIN, f"{mock_config_entry.entry_id}_1")}
    )

    mock_librenms.set_devices(
        [d for d in mock_librenms.devices["devices"] if str(d["device_id"]) != "1"]
    )
    await async_poll(hass, freezer)

    assert await async_remove_config_entry_device(hass, mock_config_entry, stale)
    assert not await async_remove_config_entry_device(
        hass, mock_config_entry, still_there
    )
    # The hub must survive for as long as the entry does.
    assert not await async_remove_config_entry_device(hass, mock_config_entry, hub)
