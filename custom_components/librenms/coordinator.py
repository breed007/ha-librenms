"""Data update coordinator for the LibreNMS integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    LibreNMSAuthError,
    LibreNMSClient,
    LibreNMSError,
)
from .const import (
    CONF_API_TOKEN,
    CONF_INCLUDE_DISABLED,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_INCLUDE_DISABLED,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    EVENT_ALERT,
    EVENT_TYPE_RECOVERED,
    LARGE_INSTALL_DEVICE_COUNT,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARNING,
)

_LOGGER = logging.getLogger(__name__)

type LibreNMSConfigEntry = ConfigEntry[LibreNMSDataUpdateCoordinator]


def _as_int(value: Any) -> int | None:
    """Coerce an API value to int, tolerating the string forms LibreNMS uses."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    """Coerce an API value to bool, tolerating 0/1, "0"/"1" and "true"/"false"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def _as_str(value: Any) -> str | None:
    """Return a non-empty string, or None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _vendor_from_icon(value: Any) -> str | None:
    """Derive a vendor name from LibreNMS's device icon filename.

    The icon is the closest thing the API exposes to a manufacturer, and it is
    what LibreNMS renders in its own Vendor column. Crucially it groups drivers
    that share a vendor: `unifi`, `unifi-usp` and the legacy `edgeswitch` all
    resolve to `ubiquiti.svg`, so switches running the old EdgeSwitch MIB are
    still attributed to Ubiquiti.
    """
    icon = _as_str(value)
    if icon is None:
        return None
    stem = icon.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return _as_str(stem.replace("-", " ").replace("_", " ").title())


@dataclass(slots=True)
class LibreNMSDevice:
    """A single device as reported by LibreNMS."""

    device_id: int
    hostname: str
    name: str
    up: bool
    status_reason: str | None
    uptime: int | None
    hardware: str | None
    os: str | None
    vendor: str | None
    version: str | None
    serial: str | None
    location: str | None
    last_polled: str | None
    disabled: bool
    ignored: bool
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def excluded(self) -> bool:
        """Return True if LibreNMS is not actively alerting on this device."""
        return self.disabled or self.ignored

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> LibreNMSDevice | None:
        """Build a device from an API payload, or None if it has no ID."""
        device_id = _as_int(payload.get("device_id"))
        if device_id is None:
            return None

        hostname = _as_str(payload.get("hostname")) or f"device-{device_id}"
        # `display` is computed from a global LibreNMS template that defaults
        # to the hostname, so it is never empty and usually just repeats it.
        # Treat it as a real override only when it actually differs, otherwise
        # a fleet added by IP ends up with every device named after its
        # address instead of its sysName.
        display = _as_str(payload.get("display"))
        name = (
            (display if display != hostname else None)
            or _as_str(payload.get("sysName"))
            or hostname
        )

        return cls(
            device_id=device_id,
            hostname=hostname,
            name=name,
            up=_as_bool(payload.get("status")),
            status_reason=_as_str(payload.get("status_reason")),
            uptime=_as_int(payload.get("uptime")),
            hardware=_as_str(payload.get("hardware")),
            os=_as_str(payload.get("os")),
            vendor=_vendor_from_icon(payload.get("icon")),
            version=_as_str(payload.get("version")),
            serial=_as_str(payload.get("serial")),
            location=_as_str(payload.get("location")),
            last_polled=_as_str(payload.get("last_polled")),
            disabled=_as_bool(payload.get("disabled")),
            ignored=_as_bool(payload.get("ignore")),
            raw=payload,
        )


@dataclass(slots=True)
class LibreNMSAlert:
    """A single active alert as reported by LibreNMS."""

    alert_id: int
    device_id: int | None
    hostname: str | None
    rule_id: int | None
    rule_name: str | None
    severity: str
    timestamp: str | None
    note: str | None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> LibreNMSAlert | None:
        """Build an alert from an API payload, or None if it has no ID."""
        alert_id = _as_int(payload.get("id"))
        if alert_id is None:
            return None

        severity = (_as_str(payload.get("severity")) or SEVERITY_OK).lower()
        if severity not in (SEVERITY_CRITICAL, SEVERITY_WARNING, SEVERITY_OK):
            severity = SEVERITY_OK

        return cls(
            alert_id=alert_id,
            device_id=_as_int(payload.get("device_id")),
            hostname=_as_str(payload.get("hostname")),
            # `name` is the alert rule name, joined in by the API.
            rule_id=_as_int(payload.get("rule_id")),
            rule_name=_as_str(payload.get("name")),
            severity=severity,
            timestamp=_as_str(payload.get("timestamp")),
            note=_as_str(payload.get("note")),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a serialisable form for state attributes and bus events."""
        return {
            "id": self.alert_id,
            "device_id": self.device_id,
            "hostname": self.hostname,
            "rule_id": self.rule_id,
            "rule": self.rule_name,
            "severity": self.severity,
            "timestamp": self.timestamp,
            "note": self.note,
        }


@dataclass(slots=True)
class LibreNMSData:
    """Everything one poll produced."""

    devices: dict[int, LibreNMSDevice]
    alerts: list[LibreNMSAlert]
    alerts_by_device: dict[int, list[LibreNMSAlert]]
    devices_total: int
    devices_up: int
    devices_down: int
    devices_excluded: int
    alerts_critical: int
    alerts_warning: int
    new_alerts: list[LibreNMSAlert]
    recovered_alerts: list[LibreNMSAlert]

    @property
    def has_problem(self) -> bool:
        """Return True if anything warrants attention right now."""
        return self.devices_down > 0 or self.alerts_critical > 0


class LibreNMSDataUpdateCoordinator(DataUpdateCoordinator[LibreNMSData]):
    """Poll devices and alerts on a single cadence."""

    config_entry: LibreNMSConfigEntry

    def __init__(self, hass: HomeAssistant, entry: LibreNMSConfigEntry) -> None:
        """Initialise the coordinator from a config entry."""
        self.client = LibreNMSClient(
            async_get_clientsession(
                hass,
                verify_ssl=entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            ),
            entry.data[CONF_URL],
            entry.data[CONF_API_TOKEN],
        )
        self.system: dict[str, Any] = {}

        # Alert id -> severity at the time we last saw it. Severity is part of
        # the key so an escalation (warning -> critical) re-fires, while an
        # unchanged alert stays quiet across polls.
        self._seen_alerts: dict[int, str] = {}
        self._primed = False
        self._warned_large_install = False

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )

    @property
    def include_disabled(self) -> bool:
        """Return whether disabled/ignored devices count toward up/down."""
        return self.config_entry.options.get(
            CONF_INCLUDE_DISABLED, DEFAULT_INCLUDE_DISABLED
        )

    async def _async_setup(self) -> None:
        """Fetch instance metadata once, for the hub device entry."""
        try:
            self.system = await self.client.async_get_system()
        except LibreNMSAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LibreNMSError as err:
            raise UpdateFailed(str(err)) from err

    async def _async_update_data(self) -> LibreNMSData:
        """Fetch devices and alerts, and derive counts and alert deltas."""
        try:
            raw_devices, raw_alerts = await self.client.async_get_overview()
        except LibreNMSAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except LibreNMSError as err:
            raise UpdateFailed(str(err)) from err

        devices: dict[int, LibreNMSDevice] = {}
        for payload in raw_devices:
            device = LibreNMSDevice.from_api(payload)
            if device is not None:
                devices[device.device_id] = device

        if not self._warned_large_install and len(devices) > LARGE_INSTALL_DEVICE_COUNT:
            self._warned_large_install = True
            _LOGGER.warning(
                "LibreNMS reports %s devices. Consider raising the scan interval "
                "in the integration options to reduce load on the instance",
                len(devices),
            )

        alerts: list[LibreNMSAlert] = []
        alerts_by_device: dict[int, list[LibreNMSAlert]] = {}
        for payload in raw_alerts:
            alert = LibreNMSAlert.from_api(payload)
            if alert is None:
                continue
            alerts.append(alert)
            if alert.device_id is not None:
                alerts_by_device.setdefault(alert.device_id, []).append(alert)

        counted = [
            device
            for device in devices.values()
            if self.include_disabled or not device.excluded
        ]
        devices_up = sum(1 for device in counted if device.up)

        data = LibreNMSData(
            devices=devices,
            alerts=alerts,
            alerts_by_device=alerts_by_device,
            devices_total=len(counted),
            devices_up=devices_up,
            devices_down=len(counted) - devices_up,
            devices_excluded=len(devices) - len(counted),
            alerts_critical=sum(
                1 for alert in alerts if alert.severity == SEVERITY_CRITICAL
            ),
            alerts_warning=sum(
                1 for alert in alerts if alert.severity == SEVERITY_WARNING
            ),
            new_alerts=[],
            recovered_alerts=[],
        )

        self._resolve_alert_deltas(data)
        return data

    def _resolve_alert_deltas(self, data: LibreNMSData) -> None:
        """Populate new/recovered alerts and fire bus events for each."""
        current = {alert.alert_id: alert for alert in data.alerts}

        if not self._primed:
            # Seed on the first successful poll so a Home Assistant restart
            # does not replay every alert that was already open.
            self._primed = True
            self._seen_alerts = {
                alert_id: alert.severity for alert_id, alert in current.items()
            }
            return

        data.new_alerts = [
            alert
            for alert_id, alert in current.items()
            if self._seen_alerts.get(alert_id) != alert.severity
        ]
        # A recovered alert has left the active set, so the API no longer
        # returns its detail — carry the record over from the previous poll.
        previous = (
            {alert.alert_id: alert for alert in self.data.alerts}
            if self.data is not None
            else {}
        )
        data.recovered_alerts = [
            previous[alert_id]
            for alert_id in self._seen_alerts
            if alert_id not in current and alert_id in previous
        ]

        self._seen_alerts = {
            alert_id: alert.severity for alert_id, alert in current.items()
        }

        for alert in data.new_alerts:
            self._fire_alert_event(alert, alert.severity)
        for alert in data.recovered_alerts:
            self._fire_alert_event(alert, EVENT_TYPE_RECOVERED)

    def _fire_alert_event(self, alert: LibreNMSAlert, event_type: str) -> None:
        """Fire the `librenms_alert` bus event for automation triggers."""
        self.hass.bus.async_fire(
            EVENT_ALERT,
            {
                "entry_id": self.config_entry.entry_id,
                "event_type": event_type,
                **alert.as_dict(),
            },
        )
