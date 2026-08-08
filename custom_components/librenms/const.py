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

# Report the poller as stuck once the newest `last_polled` across the whole
# fleet has not advanced for this long. LibreNMS polls devices every 300s by
# default, so this allows three full cycles before crying wolf.
POLLER_STALE_AFTER: Final = 900

# `state` sensors are enumerations whose meaning lives in LibreNMS's state
# translation tables, which the sensors endpoint does not carry. A bare "2"
# could mean healthy or failed, so publishing them would be worse than
# omitting them.
IGNORED_SENSOR_CLASSES: Final = frozenset({"state"})

# Hardware that cannot read a sensor often reports a 32-bit sentinel rather
# than nothing at all -- real instances return temperatures of 4294704.096 C
# while the sensor's own limits stay perfectly sane. Readings outside these
# bounds are treated as "no reading" instead of being published as fact.
SENSOR_PLAUSIBLE_RANGE: Final[dict[str, tuple[float, float]]] = {
    "charge": (0.0, 100.0),
    "current": (-10_000.0, 10_000.0),
    "dbm": (-200.0, 50.0),
    "fanspeed": (0.0, 100_000.0),
    "frequency": (0.0, 1_000_000.0),
    "humidity": (0.0, 100.0),
    "load": (0.0, 100.0),
    "power": (-1_000_000.0, 1_000_000.0),
    "power_factor": (-1.0, 1.0),
    "temperature": (-100.0, 250.0),
    "voltage": (-1_000.0, 1_000.0),
}

# Fallback bound for classes with no specific range, to catch the same
# sentinel values without guessing at real-world limits.
SENSOR_ABSURD_MAGNITUDE: Final = 1e12
