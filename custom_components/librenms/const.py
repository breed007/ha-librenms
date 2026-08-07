"""Constants for the LibreNMS integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "librenms"

# Config entry keys
CONF_URL: Final = "url"
CONF_API_TOKEN: Final = "api_token"
CONF_VERIFY_SSL: Final = "verify_ssl"

# Options keys
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_INCLUDE_DISABLED: Final = "include_disabled"

DEFAULT_SCAN_INTERVAL: Final = 60
MIN_SCAN_INTERVAL: Final = 30
MAX_SCAN_INTERVAL: Final = 3600
DEFAULT_INCLUDE_DISABLED: Final = False
DEFAULT_VERIFY_SSL: Final = True

# API
API_PATH: Final = "/api/v0"
REQUEST_TIMEOUT: Final = 15

# Bus event fired for every newly seen / recovered alert.
EVENT_ALERT: Final = "librenms_alert"

# Event entity event types.
EVENT_TYPE_CRITICAL: Final = "critical"
EVENT_TYPE_WARNING: Final = "warning"
EVENT_TYPE_OK: Final = "ok"
EVENT_TYPE_RECOVERED: Final = "recovered"

ALERT_EVENT_TYPES: Final = [
    EVENT_TYPE_CRITICAL,
    EVENT_TYPE_WARNING,
    EVENT_TYPE_OK,
    EVENT_TYPE_RECOVERED,
]

SEVERITY_CRITICAL: Final = "critical"
SEVERITY_WARNING: Final = "warning"
SEVERITY_OK: Final = "ok"

# Cap on the number of alerts serialised into a state attribute. The recorder
# stores every attribute change, so an unbounded list on a busy instance will
# bloat the database.
MAX_ALERT_ATTRIBUTES: Final = 50

# Emit a one-shot warning suggesting a longer scan interval above this many
# devices.
LARGE_INSTALL_DEVICE_COUNT: Final = 500

# Only rewrite a device's boot-time sensor when the computed value drifts by
# more than this many seconds. Poll jitter otherwise causes a state write on
# every single update.
UPTIME_DRIFT_TOLERANCE: Final = 60
