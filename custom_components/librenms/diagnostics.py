"""Diagnostics support for the LibreNMS integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_API_TOKEN, CONF_URL
from .coordinator import LibreNMSConfigEntry

TO_REDACT = {
    CONF_API_TOKEN,
    CONF_URL,
    "hostname",
    "sysName",
    "display",
    "ip",
    "overwrite_ip",
    "serial",
    "location",
    "lat",
    "lng",
    "purpose",
    "notes",
    "community",
    "authname",
    "authpass",
    "cryptopass",
    "snmp_disable",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LibreNMSConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "system": coordinator.system,
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
            async_redact_data(device.raw, TO_REDACT) for device in data.devices.values()
        ],
        "alerts": [
            async_redact_data(alert.as_dict(), TO_REDACT) for alert in data.alerts
        ],
    }
