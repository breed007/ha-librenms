"""Diagnostics support for the LibreNMS integration.

Built from an explicit allowlist of parsed fields rather than by redacting
raw payloads. LibreNMS device rows carry SNMP communities, SNMPv3 passwords,
contact details and hostnames, and a denylist only protects against the
fields someone thought of. Here a field reaches the output only if it is
named below, so a new field in a future LibreNMS release cannot leak.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from homeassistant.core import HomeAssistant

from .const import (
    CONF_INCLUDE_DISABLED,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_VERIFY_SSL,
)
from .coordinator import LibreNMSAlert, LibreNMSConfigEntry, LibreNMSDevice

# Instance facts from /system: software versions, nothing about the network.
SYSTEM_FIELDS = (
    "local_ver",
    "local_sha",
    "local_date",
    "local_branch",
    "db_schema",
    "php_ver",
    "python_ver",
    "database_ver",
    "rrdtool_ver",
    "netsnmp_ver",
)


def _device(device: LibreNMSDevice, alert_count: int) -> dict[str, Any]:
    """Return what is safe to share about one device.

    Deliberately absent: hostname, name, serial number and location.
    """
    return {
        "device_id": device.device_id,
        "up": device.up,
        "status_reason": device.status_reason,
        "disabled": device.disabled,
        "ignored": device.ignored,
        "os": device.os,
        "vendor": device.vendor,
        "hardware": device.hardware,
        "version": device.version,
        "uptime": device.uptime,
        "last_polled": device.last_polled,
        "alert_count": alert_count,
    }


def _alert(alert: LibreNMSAlert) -> dict[str, Any]:
    """Return what is safe to share about one alert.

    Deliberately absent: hostname, and the rule name and note, which are
    free text that can name hosts or people.
    """
    return {
        "id": alert.alert_id,
        "device_id": alert.device_id,
        "rule_id": alert.rule_id,
        "severity": alert.severity,
        "state": alert.state,
        "acknowledged": alert.acknowledged,
        "timestamp": alert.timestamp,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LibreNMSConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data

    return {
        "entry": {
            # The scheme says whether the token travels encrypted; the host
            # and path identify the instance, so they stay out.
            "url_scheme": urlparse(entry.data.get(CONF_URL, "")).scheme or None,
            "verify_ssl": entry.data.get(CONF_VERIFY_SSL),
            "options": {
                CONF_SCAN_INTERVAL: entry.options.get(CONF_SCAN_INTERVAL),
                CONF_INCLUDE_DISABLED: entry.options.get(CONF_INCLUDE_DISABLED),
            },
        },
        "system": {
            key: coordinator.system[key]
            for key in SYSTEM_FIELDS
            if key in coordinator.system
        },
        "counts": {
            "devices_total": data.devices_total,
            "devices_up": data.devices_up,
            "devices_down": data.devices_down,
            "devices_excluded": data.devices_excluded,
            "alerts_active": len(data.alerts),
            "alerts_critical": data.alerts_critical,
            "alerts_warning": data.alerts_warning,
        },
        "devices": [
            _device(device, len(data.alerts_by_device.get(device.device_id, [])))
            for device in data.devices.values()
        ],
        "alerts": [_alert(alert) for alert in data.alerts],
    }
