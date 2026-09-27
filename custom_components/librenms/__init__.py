"""The LibreNMS integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, issue_registry as ir

from .const import DOMAIN, EMPTY_DEVICE_POLLS, ISSUE_INSUFFICIENT_PERMISSIONS
from .coordinator import LibreNMSConfigEntry, LibreNMSDataUpdateCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: LibreNMSConfigEntry) -> bool:
    """Set up LibreNMS from a config entry."""
    coordinator = LibreNMSDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    # Register the hub up front so monitored devices can point at it by its
    # registry id. Linking through `via_device=(DOMAIN, entry_id)` is
    # deprecated from Home Assistant 2026.9 and stops working in 2027.8.
    hub = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        entry_type=dr.DeviceEntryType.SERVICE,
        name="LibreNMS",
        manufacturer="LibreNMS",
        model="Network monitoring",
        sw_version=coordinator.system.get("local_ver"),
        configuration_url=coordinator.client.base_url,
    )
    coordinator.hub_device_id = hub.id

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LibreNMSConfigEntry) -> bool:
    """Unload a config entry.

    The permission repair goes with it: a disabled or unloaded entry is not
    polling, so it has nothing to report. A reload raises it again on the
    next update if the role is still wrong.
    """
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        _delete_permission_issue(hass, entry)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: LibreNMSConfigEntry) -> None:
    """Drop anything a removed entry left behind."""
    _delete_permission_issue(hass, entry)
    hass.data.get(DOMAIN, {}).get(EMPTY_DEVICE_POLLS, {}).pop(entry.entry_id, None)


def _delete_permission_issue(hass: HomeAssistant, entry: LibreNMSConfigEntry) -> None:
    """Remove the entry's insufficient-permissions repair, if raised."""
    ir.async_delete_issue(
        hass, DOMAIN, f"{ISSUE_INSUFFICIENT_PERMISSIONS}_{entry.entry_id}"
    )


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: LibreNMSConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removing a device that LibreNMS no longer monitors."""
    # The hub itself must stay for as long as the entry exists.
    if (DOMAIN, entry.entry_id) in device_entry.identifiers:
        return False

    # While setup is retrying there is no coordinator and no device list to
    # check against. Deleting a stale device is the way out of a long
    # empty-list blip, so allow it; the next setup that succeeds recreates
    # any device LibreNMS still lists.
    coordinator: LibreNMSDataUpdateCoordinator | None = getattr(
        entry, "runtime_data", None
    )
    if coordinator is None:
        return True

    current_ids = {
        (DOMAIN, f"{entry.entry_id}_{device_id}")
        for device_id in coordinator.data.devices
    }
    # The user is deleting a device LibreNMS no longer shows this token.
    # Once it is out of the device registry its open alerts stop counting,
    # so they no longer hold the problem sensor on or fire events.
    return not device_entry.identifiers & current_ids
