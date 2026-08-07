"""Shared constants for the LibreNMS tests."""

from __future__ import annotations

from typing import Any

from custom_components.librenms.const import (
    CONF_API_TOKEN,
    CONF_INCLUDE_DISABLED,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_VERIFY_SSL,
)

BASE_URL = "https://librenms.example.com"
API_URL = f"{BASE_URL}/api/v0"
TOKEN = "0123456789abcdef0123456789abcdef"

SYSTEM_URL = f"{API_URL}/system"
DEVICES_URL = f"{API_URL}/devices"
ALERTS_URL = f"{API_URL}/alerts?state=1"

ENTRY_DATA: dict[str, Any] = {
    CONF_URL: BASE_URL,
    CONF_API_TOKEN: TOKEN,
    CONF_VERIFY_SSL: True,
}

ENTRY_OPTIONS: dict[str, Any] = {
    CONF_SCAN_INTERVAL: 60,
    CONF_INCLUDE_DISABLED: False,
}
