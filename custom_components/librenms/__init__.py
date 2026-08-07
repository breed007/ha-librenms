"""The LibreNMS integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
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

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LibreNMSConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: LibreNMSConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removing a device that LibreNMS no longer monitors."""
    coordinator = entry.runtime_data
    current_ids = {
        (DOMAIN, f"{entry.entry_id}_{device_id}")
        for device_id in coordinator.data.devices
    }
    # The hub itself must stay for as long as the entry exists.
    current_ids.add((DOMAIN, entry.entry_id))
    return not device_entry.identifiers & current_ids
