"""Sensor platform for the LibreNMS integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from .const import (
    MAX_ALERT_ATTRIBUTES,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARNING,
    UPTIME_DRIFT_TOLERANCE,
)
from .coordinator import (
    LibreNMSConfigEntry,
    LibreNMSData,
    LibreNMSDataUpdateCoordinator,
    LibreNMSDevice,
    LibreNMSSensor,
)
from .entity import LibreNMSDeviceEntity, LibreNMSEntity, async_setup_device_entities

SEVERITY_ORDER = {SEVERITY_CRITICAL: 0, SEVERITY_WARNING: 1, SEVERITY_OK: 2}


@dataclass(frozen=True, kw_only=True)
class HealthClass:
    """How one LibreNMS sensor class maps onto a Home Assistant sensor."""

    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    enabled: bool = False


# Only temperature is on by default. It is the class users actually automate
# on, and enabling everything would roughly triple the entity count on a
# sensor-rich fleet. Everything else is registered and can be switched on per
# entity. Classes missing here still appear as plain numbers.
HEALTH_CLASSES: dict[str, HealthClass] = {
    "temperature": HealthClass(
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        enabled=True,
    ),
    "voltage": HealthClass(
        device_class=SensorDeviceClass.VOLTAGE,
        unit=UnitOfElectricPotential.VOLT,
    ),
    "current": HealthClass(
        device_class=SensorDeviceClass.CURRENT,
        unit=UnitOfElectricCurrent.AMPERE,
    ),
    "power": HealthClass(
        device_class=SensorDeviceClass.POWER,
        unit=UnitOfPower.WATT,
    ),
    "humidity": HealthClass(
        device_class=SensorDeviceClass.HUMIDITY,
        unit=PERCENTAGE,
    ),
    "dbm": HealthClass(
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        unit="dBm",
    ),
    "frequency": HealthClass(
        device_class=SensorDeviceClass.FREQUENCY,
        unit=UnitOfFrequency.HERTZ,
    ),
    "fanspeed": HealthClass(unit=REVOLUTIONS_PER_MINUTE),
    "charge": HealthClass(device_class=SensorDeviceClass.BATTERY, unit=PERCENTAGE),
    "load": HealthClass(unit=PERCENTAGE),
    "count": HealthClass(),
}


@dataclass(frozen=True, kw_only=True)
class LibreNMSSensorDescription(SensorEntityDescription):
    """Describes an instance-level LibreNMS sensor."""

    value_fn: Callable[[LibreNMSData], StateType]
    attrs_fn: Callable[[LibreNMSData], dict[str, Any]] | None = None


@dataclass(frozen=True, kw_only=True)
class LibreNMSDeviceSensorDescription(SensorEntityDescription):
    """Describes a per-device LibreNMS sensor."""

    value_fn: Callable[[LibreNMSDevice, LibreNMSData], StateType]


def _alert_attributes(data: LibreNMSData) -> dict[str, Any]:
    """Return the active alert list, most severe and most recent first."""
    ordered = sorted(
        data.alerts,
        key=lambda alert: (
            SEVERITY_ORDER.get(alert.severity, 3),
            alert.timestamp or "",
        ),
    )
    return {
        "alerts": [alert.as_dict() for alert in ordered[:MAX_ALERT_ATTRIBUTES]],
        "truncated": len(ordered) > MAX_ALERT_ATTRIBUTES,
    }


INSTANCE_SENSORS: tuple[LibreNMSSensorDescription, ...] = (
    LibreNMSSensorDescription(
        key="devices_total",
        translation_key="devices_total",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.devices_total,
    ),
    LibreNMSSensorDescription(
        key="devices_up",
        translation_key="devices_up",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.devices_up,
    ),
    LibreNMSSensorDescription(
        key="devices_down",
        translation_key="devices_down",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.devices_down,
    ),
    LibreNMSSensorDescription(
        key="devices_excluded",
        translation_key="devices_excluded",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.devices_excluded,
    ),
    LibreNMSSensorDescription(
        key="active_alerts",
        translation_key="active_alerts",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: len(data.alerts),
        attrs_fn=_alert_attributes,
    ),
    LibreNMSSensorDescription(
        key="alerts_critical",
        translation_key="alerts_critical",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.alerts_critical,
    ),
    LibreNMSSensorDescription(
        key="alerts_warning",
        translation_key="alerts_warning",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.alerts_warning,
    ),
)

DEVICE_SENSORS: tuple[LibreNMSDeviceSensorDescription, ...] = (
    LibreNMSDeviceSensorDescription(
        key="alert_count",
        translation_key="alert_count",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device, data: len(
            data.alerts_by_device.get(device.device_id, [])
        ),
    ),
    LibreNMSDeviceSensorDescription(
        key="last_polled",
        translation_key="last_polled",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        # LibreNMS returns a naive local-time string; surface it verbatim
        # rather than guessing a time zone.
        value_fn=lambda device, data: device.last_polled,
    ),
    LibreNMSDeviceSensorDescription(
        key="hardware",
        translation_key="hardware",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device, data: device.hardware,
    ),
    LibreNMSDeviceSensorDescription(
        key="os",
        translation_key="os",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda device, data: device.os,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LibreNMSConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up LibreNMS sensors."""
    coordinator = entry.runtime_data

    async_add_entities(
        LibreNMSInstanceSensor(coordinator, description)
        for description in INSTANCE_SENSORS
    )

    def _build(device_id: int) -> list[SensorEntity]:
        entities: list[SensorEntity] = [
            LibreNMSDeviceSensor(coordinator, device_id, description)
            for description in DEVICE_SENSORS
        ]
        entities.append(LibreNMSUptimeSensor(coordinator, device_id))
        entities.extend(
            LibreNMSHealthSensor(coordinator, device_id, sensor.sensor_id)
            for sensor in coordinator.data.sensors_by_device.get(device_id, [])
        )
        return entities

    async_setup_device_entities(coordinator, async_add_entities, _build)


class LibreNMSInstanceSensor(LibreNMSEntity, SensorEntity):
    """A summary sensor for the LibreNMS instance."""

    entity_description: LibreNMSSensorDescription

    def __init__(
        self,
        coordinator: LibreNMSDataUpdateCoordinator,
        description: LibreNMSSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes, if this sensor defines any."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


class LibreNMSDeviceSensor(LibreNMSDeviceEntity, SensorEntity):
    """A sensor for a single monitored device."""

    entity_description: LibreNMSDeviceSensorDescription

    def __init__(
        self,
        coordinator: LibreNMSDataUpdateCoordinator,
        device_id: int,
        description: LibreNMSDeviceSensorDescription,
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, device_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType:
        """Return the sensor value."""
        if (device := self.device) is None:
            return None
        return self.entity_description.value_fn(device, self.coordinator.data)


class LibreNMSUptimeSensor(LibreNMSDeviceEntity, SensorEntity):
    """Boot time derived from the device's reported uptime.

    Reporting boot time rather than a running counter keeps the state stable,
    so the recorder does not get a new row on every poll.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "uptime"

    def __init__(
        self, coordinator: LibreNMSDataUpdateCoordinator, device_id: int
    ) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator, device_id, "uptime")
        self._boot_time: datetime | None = None

    @property
    def native_value(self) -> datetime | None:
        """Return the device's boot time."""
        device = self.device
        if device is None or device.uptime is None or not device.up:
            return None

        computed = dt_util.utcnow() - timedelta(seconds=device.uptime)
        if (
            self._boot_time is None
            or abs((computed - self._boot_time).total_seconds())
            > UPTIME_DRIFT_TOLERANCE
        ):
            self._boot_time = computed
        return self._boot_time

    @callback
    def _handle_coordinator_update(self) -> None:
        """Forget the cached boot time when the device reboots or drops."""
        device = self.device
        if device is None or not device.up:
            self._boot_time = None
        super()._handle_coordinator_update()


class LibreNMSHealthSensor(LibreNMSDeviceEntity, SensorEntity):
    """A single SNMP health reading LibreNMS collected for a device.

    Names come from LibreNMS's own sensor description rather than a
    translation key, because they are discovered at runtime -- "Disk 1
    WD101EFBX-68B0AN" is not something that can be enumerated up front.
    """

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: LibreNMSDataUpdateCoordinator,
        device_id: int,
        sensor_id: int,
    ) -> None:
        """Initialise from the sensor's current definition."""
        super().__init__(coordinator, device_id, f"sensor_{sensor_id}")
        self._sensor_id = sensor_id

        sensor = self._find(coordinator.data, device_id, sensor_id)
        mapping = HEALTH_CLASSES.get(
            sensor.sensor_class if sensor else "", HealthClass()
        )
        self._attr_name = sensor.description if sensor else f"sensor {sensor_id}"
        self._attr_device_class = mapping.device_class
        self._attr_native_unit_of_measurement = mapping.unit
        self._attr_entity_registry_enabled_default = mapping.enabled

    @staticmethod
    def _find(
        data: LibreNMSData, device_id: int, sensor_id: int
    ) -> LibreNMSSensor | None:
        """Return this sensor from a poll, if it is still present."""
        for sensor in data.sensors_by_device.get(device_id, []):
            if sensor.sensor_id == sensor_id:
                return sensor
        return None

    @property
    def sensor(self) -> LibreNMSSensor | None:
        """Return the latest reading for this sensor."""
        return self._find(self.coordinator.data, self._device_id, self._sensor_id)

    @property
    def available(self) -> bool:
        """Unavailable if sensor data failed, the sensor is gone, or it is junk."""
        sensor = self.sensor
        return (
            super().available
            and self.coordinator.data.sensors_available
            and sensor is not None
            and sensor.value is not None
        )

    @property
    def native_value(self) -> float | None:
        """Return the reading. LibreNMS has already applied any scaling."""
        sensor = self.sensor
        return sensor.value if sensor else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose the thresholds LibreNMS alerts on, when it defines any."""
        sensor = self.sensor
        if sensor is None:
            return None
        return {
            "sensor_class": sensor.sensor_class,
            "limit_high": sensor.limit_high,
            "limit_low": sensor.limit_low,
        }
