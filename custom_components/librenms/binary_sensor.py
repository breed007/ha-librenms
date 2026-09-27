"""Binary sensor platform for the LibreNMS integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LibreNMSConfigEntry, LibreNMSDataUpdateCoordinator
from .entity import LibreNMSDeviceEntity, LibreNMSEntity, async_setup_device_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LibreNMSConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up LibreNMS binary sensors."""
    coordinator = entry.runtime_data

    async_add_entities(
        [
            LibreNMSProblemBinarySensor(coordinator),
            LibreNMSPollerStaleBinarySensor(coordinator),
        ]
    )

    async_setup_device_entities(
        coordinator,
        async_add_entities,
        lambda device_id: [LibreNMSDeviceStatusBinarySensor(coordinator, device_id)],
    )


class LibreNMSProblemBinarySensor(LibreNMSEntity, BinarySensorEntity):
    """On when a device is down, a critical alert is open, or polling stalled."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "problem"

    def __init__(self, coordinator: LibreNMSDataUpdateCoordinator) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, "problem")

    @property
    def is_on(self) -> bool:
        """Return True if something needs attention."""
        return self.coordinator.data.has_problem

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return a breakdown of what is currently wrong.

        Every cause that can turn the sensor on is named here, so the state
        is never on without an attribute that says why.
        """
        data = self.coordinator.data
        down = [
            device
            for device in data.devices.values()
            if not device.up
            and (self.coordinator.include_disabled or not device.excluded)
        ]
        return {
            "devices_down": data.devices_down,
            "alerts_critical": data.alerts_critical,
            "alerts_warning": data.alerts_warning,
            # Open critical alerts on devices Home Assistant has but the
            # latest device list left out. Not in alerts_critical, which
            # follows the visible devices, but still a problem.
            "alerts_critical_hidden": data.alerts_critical_hidden,
            "poller_stale": data.poller_stale,
            # Device names as shown in Home Assistant. `down_hostnames` keeps
            # its original contents (often IP addresses) so existing
            # templates do not break.
            "down_devices": sorted(device.name for device in down),
            "down_hostnames": sorted(device.hostname for device in down),
        }


class LibreNMSPollerStaleBinarySensor(LibreNMSEntity, BinarySensorEntity):
    """On when LibreNMS has stopped polling anything.

    This is the sensor that protects every other sensor. If the poller dies,
    device states freeze at their last known values and the whole integration
    reports a healthy network that nobody is actually checking.
    """

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "poller_stale"

    def __init__(self, coordinator: LibreNMSDataUpdateCoordinator) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, "poller_stale")

    @property
    def is_on(self) -> bool:
        """Return True once the newest poll time has stopped advancing."""
        return self.coordinator.data.poller_stale

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return when polling last progressed, and for how long it hasn't."""
        data = self.coordinator.data
        return {
            "last_advanced": data.poller_last_advanced,
            "stalled_for_seconds": round(data.poller_stalled_for),
        }


class LibreNMSDeviceStatusBinarySensor(LibreNMSDeviceEntity, BinarySensorEntity):
    """Up/down state of a single monitored device."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "status"

    def __init__(
        self, coordinator: LibreNMSDataUpdateCoordinator, device_id: int
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, device_id, "status")

    @property
    def is_on(self) -> bool | None:
        """Return True when LibreNMS considers the device up."""
        if (device := self.device) is None:
            return None
        return device.up

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return why LibreNMS marked the device down, plus its flags."""
        if (device := self.device) is None:
            return None
        return {
            "status_reason": device.status_reason,
            "hostname": device.hostname,
            "location": device.location,
            "disabled": device.disabled,
            "ignored": device.ignored,
        }
