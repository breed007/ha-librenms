"""Base entity classes for the LibreNMS integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LibreNMSDataUpdateCoordinator, LibreNMSDevice


class LibreNMSEntity(CoordinatorEntity[LibreNMSDataUpdateCoordinator]):
    """An entity attached to the LibreNMS instance itself (the hub device)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LibreNMSDataUpdateCoordinator, key: str) -> None:
        """Initialise an instance-level entity."""
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        # The hub itself is registered in async_setup_entry; this only links
        # the entity to it.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry_id)})


class LibreNMSDeviceEntity(CoordinatorEntity[LibreNMSDataUpdateCoordinator]):
    """An entity attached to one monitored LibreNMS device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LibreNMSDataUpdateCoordinator,
        device_id: int,
        key: str,
    ) -> None:
        """Initialise a per-device entity."""
        super().__init__(coordinator)
        self._device_id = device_id
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{device_id}_{key}"

        device = coordinator.data.devices[device_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{device_id}")},
            via_device_id=coordinator.hub_device_id,
            name=device.name,
            # `hardware` is the model ("U7-Pro-XG", "DS923+") -- LibreNMS
            # calls it Platform. The vendor comes from the device icon, which
            # is what LibreNMS shows in its own Vendor column and correctly
            # attributes drivers that share a manufacturer. `os` is only a
            # fallback for devices with no icon.
            manufacturer=device.vendor or device.os,
            model=device.hardware,
            sw_version=device.version,
            serial_number=device.serial,
            configuration_url=coordinator.client.device_url(device_id),
        )

    @property
    def device(self) -> LibreNMSDevice | None:
        """Return the current payload for this device, if it still exists."""
        return self.coordinator.data.devices.get(self._device_id)

    @property
    def available(self) -> bool:
        """Return False once LibreNMS stops reporting this device."""
        return super().available and self.device is not None


def async_setup_device_entities(
    coordinator: LibreNMSDataUpdateCoordinator,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[int], Iterable[CoordinatorEntity]],
) -> None:
    """Add per-device entities now, and again when new devices show up.

    LibreNMS installs gain devices over time. Without this, a newly added
    device would stay invisible in Home Assistant until the entry was
    reloaded.
    """
    known: set[int] = set()

    def _add_new_devices() -> None:
        new_ids = set(coordinator.data.devices) - known
        if not new_ids:
            return
        known.update(new_ids)
        entities: list[CoordinatorEntity] = []
        for device_id in sorted(new_ids):
            entities.extend(build(device_id))
        async_add_entities(entities)

    _add_new_devices()
    coordinator.config_entry.async_on_unload(
        coordinator.async_add_listener(_add_new_devices)
    )
