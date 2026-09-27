"""Data update coordinator for the LibreNMS integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    LibreNMSAuthError,
    LibreNMSClient,
    LibreNMSError,
    LibreNMSPermissionError,
)
from .const import (
    ALERT_STATE_ACKNOWLEDGED,
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
    IGNORED_SENSOR_CLASSES,
    ISSUE_INSUFFICIENT_PERMISSIONS,
    LARGE_INSTALL_DEVICE_COUNT,
    OPEN_ALERT_STATES,
    POLLER_STALE_AFTER,
    SENSOR_ABSURD_MAGNITUDE,
    SENSOR_PLAUSIBLE_RANGE,
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


def _as_float(value: Any) -> float | None:
    """Coerce an API value to float, tolerating string forms."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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

    @property
    def excluded(self) -> bool:
        """Return True if LibreNMS is not actively alerting on this device."""
        return self.disabled or self.ignored

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> LibreNMSDevice | None:
        """Build a device from an API payload, or None if it has no ID.

        Only the fields below are kept. The payload itself is not: LibreNMS
        returns every device's SNMP community and SNMPv3 passwords to any
        token that can list devices, and there is no reason to hold them in
        memory for the life of Home Assistant.
        """
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
        )


@dataclass(slots=True)
class LibreNMSAlert:
    """A single open alert as reported by LibreNMS."""

    alert_id: int
    device_id: int | None
    hostname: str | None
    rule_id: int | None
    rule_name: str | None
    severity: str
    timestamp: str | None
    note: str | None
    state: int | None = None

    @property
    def acknowledged(self) -> bool:
        """Return True once someone has acknowledged the alert in LibreNMS."""
        return self.state == ALERT_STATE_ACKNOWLEDGED

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> LibreNMSAlert | None:
        """Build an alert, or None if it has no ID or is no longer open."""
        alert_id = _as_int(payload.get("id"))
        if alert_id is None:
            return None

        # The request already asks for open states only; this guards against
        # an instance that ignores the filter and hands back cleared rows.
        state = _as_int(payload.get("state"))
        if state is not None and state not in OPEN_ALERT_STATES:
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
            state=state,
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
            "acknowledged": self.acknowledged,
        }


@dataclass(slots=True)
class LibreNMSSensor:
    """One health sensor reading, as LibreNMS reports it."""

    sensor_id: int
    device_id: int
    sensor_class: str
    description: str
    value: float | None
    implausible: bool
    limit_high: float | None
    limit_low: float | None

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> LibreNMSSensor | None:
        """Build a sensor, or None if it is unusable or deliberately skipped."""
        sensor_id = _as_int(payload.get("sensor_id"))
        device_id = _as_int(payload.get("device_id"))
        sensor_class = _as_str(payload.get("sensor_class"))
        if sensor_id is None or device_id is None or sensor_class is None:
            return None

        # LibreNMS keeps rows for sensors that have gone away.
        if _as_bool(payload.get("sensor_deleted")):
            return None
        if sensor_class in IGNORED_SENSOR_CLASSES:
            return None

        # `sensor_current` is already scaled. The divisor and multiplier
        # alongside it describe how the poller derived it and must not be
        # applied again -- doing so puts every voltage out by 1000x.
        raw = _as_float(payload.get("sensor_current"))
        value, implausible = cls._sanity_check(sensor_class, raw)

        return cls(
            sensor_id=sensor_id,
            device_id=device_id,
            sensor_class=sensor_class,
            description=_as_str(payload.get("sensor_descr")) or f"sensor {sensor_id}",
            value=value,
            implausible=implausible,
            limit_high=_as_float(payload.get("sensor_limit")),
            limit_low=_as_float(payload.get("sensor_limit_low")),
        )

    @staticmethod
    def _sanity_check(
        sensor_class: str, raw: float | None
    ) -> tuple[float | None, bool]:
        """Return the reading, or None when it is obviously not a reading."""
        if raw is None:
            return None, False
        low, high = SENSOR_PLAUSIBLE_RANGE.get(
            sensor_class, (-SENSOR_ABSURD_MAGNITUDE, SENSOR_ABSURD_MAGNITUDE)
        )
        if not low <= raw <= high:
            return None, True
        return raw, False


@dataclass(slots=True)
class LibreNMSData:
    """Everything one poll produced."""

    devices: dict[int, LibreNMSDevice]
    alerts: list[LibreNMSAlert]
    alerts_by_device: dict[int, list[LibreNMSAlert]]
    sensors_by_device: dict[int, list[LibreNMSSensor]]
    # False when the optional sensors endpoint failed on this poll. Health
    # sensors then go unavailable rather than presenting old readings as
    # current, while device status and alerts carry on unaffected.
    sensors_available: bool
    devices_total: int
    devices_up: int
    devices_down: int
    devices_excluded: int
    alerts_critical: int
    alerts_warning: int
    new_alerts: list[LibreNMSAlert]
    recovered_alerts: list[LibreNMSAlert]
    poller_stale: bool
    poller_last_advanced: str | None
    poller_stalled_for: float

    @property
    def has_problem(self) -> bool:
        """Return True if anything warrants attention right now.

        A stuck poller counts. Without it every other signal here reads as
        healthy precisely when nothing is being measured.
        """
        return self.devices_down > 0 or self.alerts_critical > 0 or self.poller_stale


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
        # Device registry id of the hub, set once async_setup_entry has
        # registered it. Monitored devices link to the hub through it.
        self.hub_device_id: str | None = None

        # Alert id -> severity at the time we last saw it. Severity is part of
        # the key so an escalation (warning -> critical) re-fires, while an
        # unchanged alert stays quiet across polls. The LibreNMS state is
        # deliberately not part of it: acknowledging an alert, or it getting
        # worse or better, is the same open fault and must not re-fire.
        self._seen_alerts: dict[int, str] = {}
        self._primed = False
        self._warned_large_install = False
        self._warned_implausible = False
        self._warned_hidden_alerts = False
        self._sensors_failing = False

        # Newest `last_polled` seen across the fleet, and when it last moved.
        # Detecting that this stops advancing is what catches a stuck poller;
        # see _resolve_poller_health for why it is not an age comparison.
        self._poll_marker: str | None = None
        self._poll_marker_moved: datetime | None = None

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
        """Fetch devices, alerts and sensors, and derive counts and deltas.

        Devices and alerts are the core of the integration: if either fails,
        the whole poll fails. Health sensors are optional, so a failure there
        only takes the health sensors down.
        """
        raw_devices, raw_alerts, raw_sensors = await asyncio.gather(
            self.client.async_get_devices(),
            self.client.async_get_alerts(),
            self._async_fetch_sensors(),
            # Collect every outcome so no request is left running unobserved
            # when another one fails.
            return_exceptions=True,
        )
        for result in (raw_devices, raw_alerts, raw_sensors):
            if isinstance(result, BaseException):
                self._raise_update_error(result)
        ir.async_delete_issue(self.hass, DOMAIN, self._permission_issue_id)

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
        hidden_alerts = 0
        for payload in raw_alerts:
            alert = LibreNMSAlert.from_api(payload)
            if alert is None:
                continue
            # /devices only lists what the token's user may see, but /alerts
            # applies no per-device permission check at all. Keep the two
            # consistent, or the totals would count alerts on devices no
            # entity can show.
            if alert.device_id not in devices:
                hidden_alerts += 1
                continue
            alerts.append(alert)
            alerts_by_device.setdefault(alert.device_id, []).append(alert)

        if hidden_alerts and not self._warned_hidden_alerts:
            self._warned_hidden_alerts = True
            _LOGGER.warning(
                "Ignoring %s LibreNMS alert(s) on devices this API token cannot "
                "see. If devices are missing, give the token's LibreNMS user "
                "the Global Read role",
                hidden_alerts,
            )

        sensors_by_device: dict[int, list[LibreNMSSensor]] = {}
        implausible = 0
        for payload in raw_sensors or []:
            sensor = LibreNMSSensor.from_api(payload)
            if sensor is None:
                continue
            implausible += sensor.implausible
            sensors_by_device.setdefault(sensor.device_id, []).append(sensor)

        if implausible and not self._warned_implausible:
            self._warned_implausible = True
            _LOGGER.warning(
                "%s LibreNMS sensor(s) reported readings outside any plausible "
                "range and are shown as unavailable rather than as real values",
                implausible,
            )

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
            sensors_by_device=sensors_by_device,
            sensors_available=raw_sensors is not None,
            devices_total=len(counted),
            devices_up=devices_up,
            devices_down=len(counted) - devices_up,
            devices_excluded=len(devices) - len(counted),
            # Acknowledged alerts still count. Acknowledging silences
            # LibreNMS's own notifications, but the fault is still there, and
            # a problem sensor that clears when someone clicks "ack" would
            # report a healthy network that is not. Each alert carries an
            # `acknowledged` flag for anyone who wants to filter them out.
            alerts_critical=sum(
                1 for alert in alerts if alert.severity == SEVERITY_CRITICAL
            ),
            alerts_warning=sum(
                1 for alert in alerts if alert.severity == SEVERITY_WARNING
            ),
            new_alerts=[],
            recovered_alerts=[],
            poller_stale=False,
            poller_last_advanced=None,
            poller_stalled_for=0.0,
        )

        self._resolve_poller_health(data)
        self._resolve_alert_deltas(data)
        return data

    @property
    def _permission_issue_id(self) -> str:
        """Return the repair issue id for this entry's permission problem."""
        return f"{ISSUE_INSUFFICIENT_PERMISSIONS}_{self.config_entry.entry_id}"

    def _raise_update_error(self, err: BaseException) -> None:
        """Translate a failed core request into the right coordinator error.

        Only a 401 means the token itself is bad and worth asking for a new
        one. A 403 means the token works but its user's role is too narrow,
        which a new token cannot fix, so it raises a repair issue that names
        the role instead of starting reauthentication.
        """
        if isinstance(err, LibreNMSAuthError):
            raise ConfigEntryAuthFailed(str(err)) from err
        if isinstance(err, LibreNMSPermissionError):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._permission_issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_INSUFFICIENT_PERMISSIONS,
                translation_placeholders={"url": self.client.base_url},
            )
            raise UpdateFailed(str(err)) from err
        if isinstance(err, LibreNMSError):
            raise UpdateFailed(str(err)) from err
        raise err

    async def _async_fetch_sensors(self) -> list[dict[str, Any]] | None:
        """Return raw health sensors, or None if they could not be fetched.

        Logs once when sensor data stops arriving and once when it returns,
        rather than on every poll in between.
        """
        try:
            sensors = await self.client.async_get_sensors()
        except LibreNMSError as err:
            if not self._sensors_failing:
                self._sensors_failing = True
                _LOGGER.warning(
                    "LibreNMS health sensors are unavailable until sensor data "
                    "can be fetched again; device status and alerts are "
                    "unaffected: %s",
                    err,
                )
            return None

        if self._sensors_failing:
            self._sensors_failing = False
            _LOGGER.info("LibreNMS health sensor data is available again")
        return sensors

    def _resolve_poller_health(self, data: LibreNMSData) -> None:
        """Flag a poller that has stopped making progress.

        Deliberately not an age comparison. LibreNMS returns `last_polled` as a
        naive local-time string with no zone, so comparing it against Home
        Assistant's clock needs the instance's time zone and is silently wrong
        if that guess is off. What actually matters is not how old the value is
        but whether it is still moving, and that is answered by comparing the
        string to the one from the previous poll -- no clock, no zone, no skew.
        """
        marker = max(
            (
                device.last_polled
                for device in data.devices.values()
                if device.last_polled
            ),
            default=None,
        )
        now = dt_util.utcnow()

        if marker != self._poll_marker:
            self._poll_marker = marker
            self._poll_marker_moved = now

        data.poller_last_advanced = marker
        if marker is None or self._poll_marker_moved is None:
            # Nothing reports a poll time, so there is nothing to judge.
            return

        data.poller_stalled_for = (now - self._poll_marker_moved).total_seconds()
        data.poller_stale = data.poller_stalled_for > POLLER_STALE_AFTER

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
