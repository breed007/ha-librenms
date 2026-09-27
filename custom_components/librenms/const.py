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

# Repair issue raised when the token's LibreNMS role cannot read devices or
# alerts.
ISSUE_INSUFFICIENT_PERMISSIONS: Final = "insufficient_permissions"

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

# LibreNMS alert states (LibreNMS/Enum/AlertState.php). While a fault exists
# its alert row moves between ACTIVE, ACKNOWLEDGED, WORSE, BETTER and CHANGED;
# only RECOVERED (0) means it has cleared. Requesting ACTIVE alone makes an
# acknowledged or evolving alert look like it recovered.
ALERT_STATE_ACKNOWLEDGED: Final = 2
OPEN_ALERT_STATES: Final = (1, 2, 3, 4, 5)

SEVERITY_CRITICAL: Final = "critical"
SEVERITY_WARNING: Final = "warning"
SEVERITY_OK: Final = "ok"

# Cap on the number of alerts serialized into a state attribute. The recorder
# stores every attribute change, so an unbounded list on a busy instance will
# bloat the database.
MAX_ALERT_ATTRIBUTES: Final = 50

# Emit a one-shot warning suggesting a longer scan interval above this many
# devices.
LARGE_INSTALL_DEVICE_COUNT: Final = 500

# Only move a device's boot time earlier when a new estimate beats the
# current one by more than this many seconds. Smaller gains are poll timing
# noise and would otherwise each cost a recorder write. See
# LibreNMSUptimeSensor for why estimates only ever move earlier.
UPTIME_DRIFT_TOLERANCE: Final = 60

# An empty /devices response right after one that had devices is treated as
# a failed update until it has repeated on this many consecutive polls. One
# empty list is almost always a fault; three in a row means the instance, or
# the token's view of it, really is empty.
EMPTY_DEVICE_LIST_CONFIRMATIONS: Final = 3

# hass.data[DOMAIN] key for the per-entry run of untrusted empty lists.
EMPTY_DEVICE_POLLS: Final = "empty_device_polls"

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
#
# Units are LibreNMS's (lang/en/sensors.php). The bounds were checked
# against every sensor in LibreNMS's recorded test data at 26.9.1.1: what
# they reject there is a sentinel or a vendor definition that skips its own
# scaling (Delta UPS volts x 10, IOS-XE millivolts), never a real reading.
# Values are never rescaled, so a bound has to admit every scale real
# hardware uses:
# - frequency has no bound of its own beyond SENSOR_ABSURD_MAGNITUDE: it is
#   in Hz, 60 GHz radios report about 6.7e10, CPU clocks about 1.5e9, and
#   carrier offsets go negative. Its sentinels are caught by
#   SENSOR_WRAP_WINDOW.
# - power_factor is -1 to 1 on most devices but 0 to 100 on Raritan and
#   Sentry PDUs, so it allows -100 to 100.
# - load goes above 100 percent on a UPS in overload, so it allows up to
#   300; vertiv-dcs's 999.9 sentinel stays out.
SENSOR_PLAUSIBLE_RANGE: Final[dict[str, tuple[float, float]]] = {
    "charge": (0.0, 100.0),
    "current": (-10_000.0, 10_000.0),
    "dbm": (-200.0, 50.0),
    "fanspeed": (0.0, 100_000.0),
    "humidity": (0.0, 100.0),
    "load": (0.0, 300.0),
    "power": (-1_000_000.0, 1_000_000.0),
    "power_factor": (-100.0, 100.0),
    "temperature": (-100.0, 250.0),
    "voltage": (-1_000.0, 1_000.0),
}

# Fallback bound for classes with no specific range, to catch the same
# sentinel values without guessing at real-world limits.
SENSOR_ABSURD_MAGNITUDE: Final = 1e12

# A sensor whose raw SNMP value, before LibreNMS applied its divisor and
# multiplier, lies within this distance of 2^32 is a small negative number
# read as unsigned, or all ones: a sentinel in any class and at any scale.
# A range cannot catch these everywhere (2^32 - 2145 Hz is a believable
# radio frequency), but no real reading in LibreNMS's test data comes near.
SENSOR_WRAP_WINDOW: Final = 2**20

# Manufacturer names for LibreNMS icon file names (html/images/os at 26.9.1.1)
# where title-casing the name gets the vendor wrong: acronyms, camel case,
# joined words, and icons named after an operating system rather than its
# maker. Any icon not listed here is title-cased, which is right for most
# ("cisco" -> "Cisco"). Only display text: never part of an identifier.
VENDOR_NAMES: Final[dict[str, str]] = {
    "4rf": "4RF",
    "a10": "A10 Networks",
    "abb": "ABB",
    "adtran": "ADTRAN",
    "adva": "ADVA",
    "aix": "IBM",
    "akcp": "AKCP",
    "alaxala": "ALAXALA",
    "alcatellucent": "Alcatel-Lucent",
    "alliedtelesis": "Allied Telesis",
    "almalinux": "AlmaLinux",
    "altalabs": "Alta Labs",
    "apc": "APC",
    "arraynetworks": "Array Networks",
    "arris": "ARRIS",
    "asrockrack": "ASRock Rack",
    "asuswrt-merlin": "ASUS",
    "aten": "ATEN",
    "audiocodes": "AudioCodes",
    "avtech": "AVTECH",
    "bdcom": "BDCOM",
    "beagleboard": "BeagleBoard",
    "bluecoat": "Blue Coat",
    "bnt": "BNT",
    "carel": "CAREL",
    "centos": "CentOS",
    "checkpoint": "Check Point",
    "cisco-old": "Cisco",
    "cloudlinux": "CloudLinux",
    "coreos": "CoreOS",
    "cxr-networks": "CXR Networks",
    "cyberpower": "CyberPower",
    "dd-wrt": "DD-WRT",
    "ddn": "DDN",
    "dhcpatriot": "DHCPatriot",
    "dlink": "D-Link",
    "dpstelecom": "DPS Telecom",
    "dragonfly": "DragonFly BSD",
    "dragonwave": "DragonWave",
    "draytek": "DrayTek",
    "edge-core": "Edgecore",
    "edgeos": "Ubiquiti",
    "emc": "EMC",
    "endrun": "EndRun",
    "engenius": "EnGenius",
    "esphome": "ESPHome",
    "etherwan": "EtherWAN",
    "exagrid": "ExaGrid",
    "extrahop": "ExtraHop",
    "extreme": "Extreme Networks",
    "extremeboss": "Extreme Networks",
    "extremevoss": "Extreme Networks",
    "fiberhome": "FiberHome",
    "firebrick": "FireBrick",
    "freebsd": "FreeBSD",
    "fs": "FS",
    "fujifilm": "FUJIFILM",
    "ge": "GE",
    "generex-ups": "GENEREX",
    "hanwhatechwin": "Hanwha Techwin",
    "haproxy": "HAProxy",
    "hp": "HP",
    "hpe": "HPE",
    "huber-suhner": "HUBER+SUHNER",
    "hwg": "HW group",
    "hwg-poseidon": "HW group",
    "ibmos": "IBM",
    "ignitenet": "IgniteNet",
    "ipinfusion": "IP Infusion",
    "junos": "Juniper",
    "konica": "Konica Minolta",
    "lancom": "LANCOM",
    "ligowave": "LigoWave",
    "linuxmint": "Linux Mint",
    "mcafee": "McAfee",
    "mcafeewebgateway": "McAfee",
    "mikrotik": "MikroTik",
    "mobileiron": "MobileIron",
    "motorola-cm": "Motorola",
    "mrv": "MRV",
    "nec": "NEC",
    "netapp": "NetApp",
    "netbotz": "NetBotz",
    "netbsd": "NetBSD",
    "netgear": "NETGEAR",
    "netmodule": "NetModule",
    "nvent-hoffman": "nVent Hoffman",
    "oki": "OKI",
    "oneaccess": "OneAccess",
    "open-e": "Open-E",
    "openbsd": "OpenBSD",
    "openindiana": "OpenIndiana",
    "opensolaris": "OpenSolaris",
    "opensuse": "openSUSE",
    "openwrt": "OpenWrt",
    "opnsense": "OPNsense",
    "packetflux": "PacketFlux",
    "packetlight": "PacketLight",
    "packetpower": "Packet Power",
    "panos": "Palo Alto Networks",
    "pfsense": "pfSense",
    "picos": "Pica8",
    "powerwalker": "PowerWalker",
    "primekey": "PrimeKey",
    "purestorage": "Pure Storage",
    "qnap": "QNAP",
    "rad": "RAD",
    "radwin": "RADWIN",
    "redhat": "Red Hat",
    "redlion": "Red Lion",
    "rocky": "Rocky Linux",
    "rs": "Rohde & Schwarz",
    "samsungprinter": "Samsung",
    "schneider": "Schneider Electric",
    "screenos": "Juniper",
    "seh": "SEH",
    "servertech": "Server Technology",
    "siae": "SIAE",
    "silverpeak": "Silver Peak",
    "socomecpdu": "Socomec",
    "sonicwall": "SonicWall",
    "sophos-xg": "Sophos",
    "stulz": "STULZ",
    "suse": "SUSE",
    "thomson-cm": "Thomson",
    "tplink": "TP-Link",
    "trendnet": "TRENDnet",
    "tripplite": "Tripp Lite",
    "truenas": "TrueNAS",
    "truenas-scale": "TrueNAS",
    "tyconsystems": "Tycon Systems",
    "velocloud": "VeloCloud",
    "vivotek": "VIVOTEK",
    "vmware": "VMware",
    "vyos": "VyOS",
    "watchguard": "WatchGuard",
    "westmountainradio": "West Mountain Radio",
    "worldcastsystems": "WorldCast Systems",
    "wti": "WTI",
    "xcp-ng": "XCP-ng",
    "xkl": "XKL",
    "zte": "ZTE",
}
