"""Data update coordinator for the LibreNMS integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    LibreNMSAuthError,
    LibreNMSClient,
    LibreNMSError,
    LibreNMSPermissionError,
    LibreNMSRedirectError,
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
    EMPTY_DEVICE_LIST_CONFIRMATIONS,
    EMPTY_DEVICE_POLLS,
    EVENT_ALERT,
    EVENT_TYPE_RECOVERED,
    IGNORED_SENSOR_CLASSES,
    ISSUE_INSUFFICIENT_PERMISSIONS,
    ISSUE_REDIRECTED,
    LARGE_INSTALL_DEVICE_COUNT,
    OPEN_ALERT_STATES,
    POLLER_STALE_AFTER,
    SENSOR_ABSURD_MAGNITUDE,
    SENSOR_CLASS_LABELS,
    SENSOR_PLAUSIBLE_RANGE,
    SENSOR_WRAP_WINDOW,
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    SEVERITY_WARNING,
    VENDOR_NAMES,
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
    # Vendors whose names title-casing would mangle ("Apc", "Mikrotik") are
    # spelled out in VENDOR_NAMES; everything else is title-cased.
    if (name := VENDOR_NAMES.get(stem.lower())) is not None:
        return name
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
    last_ping: str | None = None

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
            last_ping=_as_str(payload.get("last_ping")),
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
        """Return a serializable form for state attributes and bus events."""
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


def newest_first(alerts: Iterable[LibreNMSAlert]) -> list[LibreNMSAlert]:
    """Return alerts newest first, in an order that never depends on luck.

    LibreNMS timestamps are "YYYY-MM-DD HH:MM:SS" strings, so they sort as
    text. Alerts with the same timestamp go by alert id, highest first. That
    only makes the order stable from one poll to the next: LibreNMS keeps one
    alert row per device and rule and reuses it every time that alert opens
    again, so the id says nothing about which alert is newer. Alerts with no
    timestamp go last. Used wherever a capped alert list is published, so the
    cap always drops the oldest alerts.
    """
    return sorted(
        alerts,
        key=lambda alert: (
            alert.timestamp is not None,
            alert.timestamp or "",
            alert.alert_id,
        ),
        reverse=True,
    )


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
    # LibreNMS's grouping for the sensor ("Disks", "Ports"), if any.
    group: str | None = None
    # The name shown in Home Assistant. It starts as the description and is
    # made unique per device by name_sensors.
    name: str = ""

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
        # applied again -- doing so puts every voltage out by 1000x. They
        # are used only to recover the value the device sent, to spot a
        # 32-bit sentinel; see _is_wrapped.
        reading = _as_float(payload.get("sensor_current"))
        value, implausible = cls._sanity_check(sensor_class, reading)
        if value is not None and cls._is_wrapped(payload, value):
            value, implausible = None, True

        description = _as_str(payload.get("sensor_descr")) or f"sensor {sensor_id}"
        return cls(
            sensor_id=sensor_id,
            device_id=device_id,
            sensor_class=sensor_class,
            description=description,
            value=value,
            implausible=implausible,
            limit_high=_as_float(payload.get("sensor_limit")),
            limit_low=_as_float(payload.get("sensor_limit_low")),
            group=_as_str(payload.get("group")),
            name=description,
        )

    @staticmethod
    def _sanity_check(
        sensor_class: str, reading: float | None
    ) -> tuple[float | None, bool]:
        """Return the reading, or None when it is obviously not a reading."""
        if reading is None:
            return None, False
        low, high = SENSOR_PLAUSIBLE_RANGE.get(
            sensor_class, (-SENSOR_ABSURD_MAGNITUDE, SENSOR_ABSURD_MAGNITUDE)
        )
        if not low <= reading <= high:
            return None, True
        return reading, False

    @staticmethod
    def _is_wrapped(payload: dict[str, Any], reading: float) -> bool:
        """Return True if the device sent a 32-bit sentinel for this reading.

        LibreNMS's poller divides the device's value by `sensor_divisor`,
        multiplies by `sensor_multiplier`, then applies `user_func` if set.
        Undoing the first two recovers what the device sent. A `user_func`
        (a unit conversion) cannot be undone this way, so those are left to
        the range check.
        """
        if _as_str(payload.get("user_func")):
            return False
        divisor = _as_float(payload.get("sensor_divisor")) or 1.0
        multiplier = _as_float(payload.get("sensor_multiplier")) or 1.0
        sent = abs(reading * divisor / multiplier)
        return 2**32 - SENSOR_WRAP_WINDOW <= sent <= 2**32


def name_sensors(sensors: list[LibreNMSSensor]) -> None:
    """Give each of one device's health sensors a name no other one has.

    LibreNMS often reuses a description on one device: a Synology's disk
    temperature and bad-sector count are both "Disk 1 DT01ACA300", and a
    MikroTik port's PoE current, power and voltage are all "ether1 POE".
    Home Assistant would name them alike and number their entity ids.

    A description used once is left as it is. Where it repeats, the sensor
    class is added when the classes differ, which settles most cases (940 of
    1,465 in LibreNMS's recorded test data). Sensors still alike, same
    description and same class, get their LibreNMS group when every one has
    a different group, and otherwise a number in LibreNMS sensor id order.
    """
    by_description: dict[str, list[LibreNMSSensor]] = {}
    for sensor in sensors:
        sensor.name = sensor.description
        by_description.setdefault(sensor.description, []).append(sensor)

    for alike in by_description.values():
        if len(alike) < 2:
            continue
        by_class: dict[str, list[LibreNMSSensor]] = {}
        for sensor in alike:
            by_class.setdefault(sensor.sensor_class, []).append(sensor)
        if len(by_class) > 1:
            for sensor in alike:
                label = SENSOR_CLASS_LABELS.get(
                    sensor.sensor_class, sensor.sensor_class.replace("_", " ")
                )
                sensor.name = f"{sensor.description} {label}"
        for peers in by_class.values():
            if len(peers) < 2:
                continue
            peers.sort(key=lambda sensor: sensor.sensor_id)
            groups = [sensor.group for sensor in peers]
            by_group = all(groups) and len(set(groups)) == len(groups)
            for number, sensor in enumerate(peers, start=1):
                sensor.name = f"{sensor.name} {sensor.group if by_group else number}"


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
    # Open critical alerts on devices Home Assistant has for this entry but
    # that were missing from this poll's device list. They are not in the
    # counts, which follow the visible devices, but they keep the problem
    # sensor on: a device dropping out of a response is not the fault
    # clearing.
    alerts_critical_hidden: int = 0
    # Those alerts themselves, newest first, and the Home Assistant name of
    # each one's device, so the problem sensor can say what is holding it on.
    hidden_alerts: list[LibreNMSAlert] = field(default_factory=list)
    hidden_device_names: dict[int, str | None] = field(default_factory=dict)

    @property
    def has_problem(self) -> bool:
        """Return True if anything warrants attention right now.

        A stuck poller counts. Without it every other signal here reads as
        healthy precisely when nothing is being measured.
        """
        return (
            self.devices_down > 0
            or self.alerts_critical > 0
            or self.alerts_critical_hidden > 0
            or self.poller_stale
        )


class LibreNMSDataUpdateCoordinator(DataUpdateCoordinator[LibreNMSData]):
    """Poll devices and alerts on a single cadence."""

    config_entry: LibreNMSConfigEntry

    def __init__(self, hass: HomeAssistant, entry: LibreNMSConfigEntry) -> None:
        """Initialize the coordinator from a config entry."""
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

        # Alert events are worked out from LibreNMS's own open-alert set, never
        # from which devices happened to come back in the same poll: a device
        # missing from one /devices response says nothing about its faults.
        # Which alerts count at all is decided afresh on every update; see
        # _devices_in_home_assistant.
        #
        # _known_alerts: alert id -> severity HA has already accounted for,
        # either by firing an event or because it was open at startup.
        # Severity is part of it so an escalation (warning -> critical)
        # re-fires; the LibreNMS state is not, because acknowledging an alert
        # or it getting worse or better is the same open fault.
        # _alert_records: the last record /alerts returned for each known
        # alert, so `recovered` can still name the rule and host.
        self._known_alerts: dict[int, str] = {}
        self._alert_records: dict[int, LibreNMSAlert] = {}
        # Called with a LibreNMS device id when the user deletes that device
        # in Home Assistant; see async_device_removed.
        self._device_removed_listeners: list[Callable[[int], None]] = []
        self._primed = False
        self._warned_large_install = False
        self._warned_implausible = False
        self._warned_hidden_alerts = False
        self._sensors_failing = False
        # Why the last sensors request failed, or None if it succeeded.
        self._sensors_error: LibreNMSError | None = None

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
        core = (raw_devices, raw_alerts)
        if any(isinstance(result, BaseException) for result in core):
            # Only empty lists with nothing else in between count as a run.
            self._empty_device_polls = 0
        # A rejected token outranks everything else in the same poll, so a
        # 401 on one request starts reauth even if the other got a 403.
        for result in core:
            if isinstance(result, LibreNMSAuthError):
                self._raise_update_error(result)
        for result in (*core, raw_sensors):
            if isinstance(result, BaseException):
                self._raise_update_error(result)
        self._check_empty_device_list(raw_devices)
        ir.async_delete_issue(self.hass, DOMAIN, self._permission_issue_id)
        ir.async_delete_issue(self.hass, DOMAIN, self._redirect_issue_id)
        self._note_sensor_availability()

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

        open_alerts: list[LibreNMSAlert] = []
        alerts: list[LibreNMSAlert] = []
        alerts_by_device: dict[int, list[LibreNMSAlert]] = {}
        hidden_alerts = 0
        for payload in raw_alerts:
            alert = LibreNMSAlert.from_api(payload)
            if alert is None:
                continue
            open_alerts.append(alert)
            # /devices only lists what the token's user may see, but /alerts
            # applies no per-device permission check at all. Keep the counts
            # consistent with the visible devices, or the totals would count
            # alerts on devices no entity can show. Events and the problem
            # sensor are worked out from open_alerts instead; see
            # _resolve_alert_deltas.
            if alert.device_id not in devices:
                hidden_alerts += 1
                continue
            alerts.append(alert)
            alerts_by_device.setdefault(alert.device_id, []).append(alert)

        if hidden_alerts and not self._warned_hidden_alerts:
            self._warned_hidden_alerts = True
            _LOGGER.warning(
                "Leaving %s LibreNMS alert(s) on devices this API token cannot "
                "see out of the alert counts. If devices are missing, give the "
                "token's LibreNMS user the Global Read role",
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
        for device_sensors in sensors_by_device.values():
            name_sensors(device_sensors)

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
        self._resolve_alert_deltas(data, open_alerts)
        return data

    @property
    def _empty_device_polls(self) -> int:
        """Return how many empty device lists in a row were not trusted.

        Kept in hass.data rather than on the coordinator: Home Assistant
        builds a new coordinator for every setup attempt, and a count that
        restarted each time would keep a genuinely emptied instance from ever
        finishing setup.
        """
        counts = self.hass.data.get(DOMAIN, {}).get(EMPTY_DEVICE_POLLS, {})
        return counts.get(self.config_entry.entry_id, 0)

    @_empty_device_polls.setter
    def _empty_device_polls(self, value: int) -> None:
        counts = self.hass.data.setdefault(DOMAIN, {}).setdefault(
            EMPTY_DEVICE_POLLS, {}
        )
        if value:
            counts[self.config_entry.entry_id] = value
        else:
            counts.pop(self.config_entry.entry_id, None)

    def _registered_devices(self) -> dict[int, str | None]:
        """Return the devices registered for this entry, with their names.

        These are the devices the user has been shown at some point and has
        not deleted, whether or not the latest device list includes them,
        keyed by LibreNMS id. The name is the one Home Assistant shows,
        including a rename by the user. The registry indexes devices by
        config entry, so this costs well under a millisecond per update even
        with a few thousand devices.
        """
        prefix = f"{self.config_entry.entry_id}_"
        devices: dict[int, str | None] = {}
        for device in dr.async_entries_for_config_entry(
            dr.async_get(self.hass), self.config_entry.entry_id
        ):
            for domain, identifier in device.identifiers:
                if domain == DOMAIN and identifier.startswith(prefix):
                    device_id = _as_int(identifier.removeprefix(prefix))
                    if device_id is not None:
                        devices[device_id] = device.name_by_user or device.name
        return devices

    @callback
    def async_add_device_removed_listener(
        self, listener: Callable[[int], None]
    ) -> CALLBACK_TYPE:
        """Call `listener` with the device id whenever a device is deleted."""
        self._device_removed_listeners.append(listener)

        @callback
        def _remove() -> None:
            self._device_removed_listeners.remove(listener)

        return _remove

    @callback
    def async_device_removed(self, device_id: int) -> None:
        """Tell the entity trackers the user deleted this device.

        Home Assistant removes its entities with it. The trackers must then
        forget they built them, or the device would stay missing if
        LibreNMS lists it again, until the entry was reloaded.
        """
        for listener in list(self._device_removed_listeners):
            listener(device_id)

    def _devices_in_home_assistant(self, data: LibreNMSData) -> dict[int, str | None]:
        """Return the devices whose alerts count, for this update, by name.

        That is every device in this update's list, plus every device
        registered for this entry from an earlier one, even if LibreNMS
        left it out this time. The rule is the same on every update,
        including the first after a reload or restart, because the device
        registry outlives the coordinator. So the problem sensor and alert
        events give the same answer whether or not Home Assistant restarted
        in between.

        Alerts on devices the token has never listed do not count: /alerts
        applies no per-device permission check, so a narrow role still sees
        alerts for devices it cannot see. Deleting a device in Home
        Assistant takes it out of the registry, which is how the user stops
        a device that left the token's view for good from counting.
        """
        return {
            **{device_id: device.name for device_id, device in data.devices.items()},
            **self._registered_devices(),
        }

    def _expects_devices(self) -> bool:
        """Return True if an empty device list would contradict what HA knows.

        That is the case once this run has published devices, and before the
        first successful update when the registry already holds devices for
        this entry, which is what makes a reload or restart during a blip
        safe. After an empty list has been accepted, nothing is expected
        until devices come back.
        """
        if self.data is not None:
            return bool(self.data.devices)
        return bool(self._registered_devices())

    def _check_empty_device_list(self, raw_devices: list[dict[str, Any]]) -> None:
        """Fail the update when /devices comes back empty unexpectedly.

        An empty list where devices are expected is far more likely to be a
        fault (a permission cache being rebuilt, a database hiccup) than an
        instance emptied since the last poll. Publishing it would report
        every device gone at once, so the update fails and entities go
        unavailable instead. It is a suspicion, not a verdict: after
        EMPTY_DEVICE_LIST_CONFIRMATIONS empty lists in a row, with no other
        result in between, the list is accepted, so an instance that really
        was emptied, or a role that really lost its devices, settles.
        """
        if raw_devices or not self._expects_devices():
            self._empty_device_polls = 0
            return
        count = self._empty_device_polls + 1
        if count >= EMPTY_DEVICE_LIST_CONFIRMATIONS:
            self._empty_device_polls = 0
            _LOGGER.warning(
                "LibreNMS has returned no devices for %s updates in a row; "
                "accepting that the API token can no longer see any devices",
                count,
            )
            return
        self._empty_device_polls = count
        raise UpdateFailed(
            "LibreNMS returned no devices, where devices were expected; "
            "treating this as a failed update"
        )

    @property
    def _permission_issue_id(self) -> str:
        """Return the repair issue id for this entry's permission problem."""
        return f"{ISSUE_INSUFFICIENT_PERMISSIONS}_{self.config_entry.entry_id}"

    @property
    def _redirect_issue_id(self) -> str:
        """Return the repair issue id for this entry's redirect problem."""
        return f"{ISSUE_REDIRECTED}_{self.config_entry.entry_id}"

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
            if self.config_entry.state is not ConfigEntryState.LOADED:
                # During setup the error shows on the integration card. A
                # repair raised now could not be cleaned up if the user then
                # disabled the entry: Home Assistant does not unload an entry
                # that never loaded.
                raise UpdateFailed(str(err)) from err
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
        if isinstance(err, LibreNMSRedirectError):
            # Entities going unavailable with only a log line leaves the user
            # guessing. The repair says where LibreNMS now sends requests;
            # err.location already has any credentials stripped. Raised only
            # once loaded, for the same reason as the permission repair.
            if self.config_entry.state is ConfigEntryState.LOADED:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    self._redirect_issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key=ISSUE_REDIRECTED,
                    translation_placeholders={
                        "url": self.client.base_url,
                        "redirect_url": err.location,
                    },
                )
            raise UpdateFailed(str(err)) from err
        if isinstance(err, LibreNMSError):
            raise UpdateFailed(str(err)) from err
        raise err

    async def _async_fetch_sensors(self) -> list[dict[str, Any]] | None:
        """Return raw health sensors, or None if they could not be fetched.

        The reason is kept for _note_sensor_availability, which decides
        what to log once the rest of the poll is known.
        """
        try:
            sensors = await self.client.async_get_sensors()
        except LibreNMSError as err:
            self._sensors_error = err
            return None
        self._sensors_error = None
        return sensors

    def _note_sensor_availability(self) -> None:
        """Log once when sensor data stops arriving and once when it returns.

        Called only after devices and alerts succeeded in the same poll, so
        "device status and alerts are unaffected" is true whenever it is
        logged. A poll that failed outright is already reported as a failed
        update, and the sensors outage is logged on the next poll that gets
        this far.
        """
        if self._sensors_error is not None:
            if not self._sensors_failing:
                self._sensors_failing = True
                _LOGGER.warning(
                    "LibreNMS health sensors are unavailable until sensor data "
                    "can be fetched again; device status and alerts are "
                    "unaffected: %s",
                    self._sensors_error,
                )
        elif self._sensors_failing:
            self._sensors_failing = False
            _LOGGER.info("LibreNMS health sensor data is available again")

    @staticmethod
    def _poll_marker_from(devices: dict[int, LibreNMSDevice]) -> str | None:
        """Return the newest time LibreNMS's poller is known to have run.

        That is the newest `last_polled`. LibreNMS writes it only when it
        polls a device that is up, so when every device it polls is down,
        poll times freeze while the poller keeps running. Only then is
        `last_ping` used as well: from 26.7.0 LibreNMS writes it on every
        poll of a device, up or down, plus at discovery. Before 26.7.0 its
        separate ping service wrote it too, so trusting it while devices
        are up would let a moving `last_ping` hide a dead poller. Disabled
        devices are not polled and do not count toward "every device".
        Returns None when there is nothing to judge.
        """
        times = [device.last_polled for device in devices.values()]
        polled = [device for device in devices.values() if not device.disabled]
        if polled and not any(device.up for device in polled):
            times += [device.last_ping for device in polled]
        return max((time for time in times if time), default=None)

    def _resolve_poller_health(self, data: LibreNMSData) -> None:
        """Flag a poller that has stopped making progress.

        Deliberately not an age comparison. LibreNMS returns `last_polled` as a
        naive local-time string with no zone, so comparing it against Home
        Assistant's clock needs the instance's time zone and is silently wrong
        if that guess is off. What actually matters is not how old the value is
        but whether it is still moving, and that is answered by comparing the
        string to the one from the previous poll -- no clock, no zone, no skew.
        """
        marker = self._poll_marker_from(data.devices)
        now = dt_util.utcnow()

        if marker is None:
            # Nothing reports a poll time, so there is nothing to judge. That
            # is not the poller moving either: keep the last marker, so a
            # stretch without devices does not restart the stall timer. The
            # last judgment carries over, so a stalled poller that is the only
            # problem does not flap off and on while the list is empty, and a
            # healthy one is not declared stalled for want of evidence.
            data.poller_last_advanced = self._poll_marker
            if self._poll_marker_moved is not None:
                data.poller_stalled_for = (
                    now - self._poll_marker_moved
                ).total_seconds()
            data.poller_stale = self.data.poller_stale if self.data else False
            return

        if marker != self._poll_marker or self._poll_marker_moved is None:
            self._poll_marker = marker
            self._poll_marker_moved = now

        data.poller_last_advanced = marker
        data.poller_stalled_for = (now - self._poll_marker_moved).total_seconds()
        data.poller_stale = data.poller_stalled_for > POLLER_STALE_AFTER

    def _resolve_alert_deltas(
        self, data: LibreNMSData, open_alerts: list[LibreNMSAlert]
    ) -> None:
        """Populate new/recovered alerts and fire bus events for each.

        `open_alerts` is everything /alerts reported as open, before the
        visible-device filter. Only LibreNMS clearing an alert produces
        `recovered`; a device dropping out of /devices does not, so a
        partial response cannot page an all-clear and then re-page the same
        fault as new once the device is back.

        An alert takes part only while its device is in Home Assistant (see
        _devices_in_home_assistant): it is announced, it can recover, and a
        critical one holds the problem sensor on.
        """
        current = {alert.alert_id: alert for alert in open_alerts}
        visible = {alert.alert_id for alert in data.alerts}
        in_home_assistant = self._devices_in_home_assistant(data)

        if not self._primed:
            # Seed on the first successful poll so a Home Assistant restart
            # does not replay every alert that was already open, including
            # ones on devices that are not visible at that moment.
            self._primed = True
            self._known_alerts = {
                alert_id: alert.severity for alert_id, alert in current.items()
            }
            self._alert_records = dict(current)
        else:
            cleared = [a for a in self._known_alerts if a not in current]
            data.recovered_alerts = [
                self._alert_records[alert_id]
                for alert_id in cleared
                if self._alert_records[alert_id].device_id in in_home_assistant
            ]
            for alert_id in cleared:
                del self._known_alerts[alert_id]
                self._alert_records.pop(alert_id, None)

            data.new_alerts = [
                alert
                for alert in current.values()
                if alert.device_id in in_home_assistant
                and self._known_alerts.get(alert.alert_id) != alert.severity
            ]
            for alert in data.new_alerts:
                self._known_alerts[alert.alert_id] = alert.severity
            for alert_id, alert in current.items():
                if alert_id in self._known_alerts:
                    self._alert_records[alert_id] = alert

        data.hidden_alerts = newest_first(
            alert
            for alert_id, alert in current.items()
            if alert_id not in visible
            and alert.device_id in in_home_assistant
            and alert.severity == SEVERITY_CRITICAL
        )
        data.hidden_device_names = {
            alert.device_id: in_home_assistant[alert.device_id]
            for alert in data.hidden_alerts
            if alert.device_id is not None
        }
        data.alerts_critical_hidden = len(data.hidden_alerts)

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
