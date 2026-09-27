# LibreNMS for Home Assistant

[![Tests](https://github.com/breed007/ha-librenms/actions/workflows/tests.yml/badge.svg)](https://github.com/breed007/ha-librenms/actions/workflows/tests.yml)
[![Validate](https://github.com/breed007/ha-librenms/actions/workflows/validate.yml/badge.svg)](https://github.com/breed007/ha-librenms/actions/workflows/validate.yml)
[![hacs](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz)

See your monitored network inside Home Assistant (device up/down, active
alerts, health readings) and automate on it.

This is a read-only integration against the [LibreNMS REST
API](https://docs.librenms.org/API/). It polls your instance and turns every
monitored device into a Home Assistant device, with instance-wide summary
sensors for dashboards and an event entity for alert-driven automations. It
does not modify anything in LibreNMS.

A community project. Not affiliated with, endorsed by, or supported by the
LibreNMS project.

---

## Screenshots

Every device LibreNMS monitors becomes a Home Assistant device, grouped under
a hub for the instance.

![The LibreNMS integration in Home Assistant, listing monitored devices with their models](docs/integration.png)

The hub holds the instance summary: device counts, active alerts broken down
by severity, the problem binary sensor, and the alert event entity.

![The LibreNMS hub device page showing summary sensors](docs/hub-device.png)

Each monitored device gets its own page. Model and vendor come from LibreNMS,
and every device links back to its page there.

![A single monitored device showing its status, alert count and last boot time](docs/device.png)

Setup is a URL and an API token. No YAML.

![Adding the LibreNMS integration from the Home Assistant integrations dashboard](docs/add-integration.png)

---

## Requirements

- Home Assistant **2026.8.0** or newer
- A reachable LibreNMS instance with the API enabled
- A LibreNMS API token

### Supported LibreNMS versions

Verified against LibreNMS **26.8** on a 22-device install. The four endpoints
this integration uses (`/api/v0/system`, `/api/v0/devices`, `/api/v0/alerts`
and `/api/v0/resources/sensors`) have been stable across the lifetime of the
v0 API, so older releases will very likely work.

A hard minimum version has not been pinned. If you hit a problem on an older
release, please open an issue with your LibreNMS version so it can be
documented. That is the most useful thing an early user can report.

---

## Installation

### HACS (custom repository)

1. In Home Assistant, go to **HACS → Integrations**.
2. Open the ⋮ menu → **Custom repositories**.
3. Add `https://github.com/breed007/ha-librenms` with category **Integration**.
4. Find **LibreNMS** in the list, install it, and restart Home Assistant.

### Manual

Copy `custom_components/librenms/` into your Home Assistant `config/custom_components/`
directory and restart.

---

## Setting up the LibreNMS API token

**Create a dedicated user for Home Assistant with the Global Read role.** A
LibreNMS API token has the permissions of the user it belongs to. Global Read
can see every device, alert and health sensor and cannot change anything,
which is what this integration needs.

Two roles to avoid:

- **Normal User** sees only the devices explicitly assigned to it, which on
  most installs is none. The integration would then show no devices at all.
- **Admin** works, but this integration never writes to LibreNMS, and an admin
  token in a leaked Home Assistant backup is admin access to your monitoring
  system.

LibreNMS returns alerts for every device regardless of role, so alerts on
devices the token cannot see are ignored and logged once. That keeps the
alert counts consistent with the devices Home Assistant shows.

1. In LibreNMS, go to **Settings → Manage → Users → Add User**.
2. Create a user (for example `homeassistant`) with the **Global Read** role.
3. Go to **Settings → API → API Access**.
4. Click **Create API access token**, select the `homeassistant` user, give it
   a description, and confirm.
5. Copy the token value.

Then in Home Assistant: **Settings → Devices & Services → Add Integration →
LibreNMS**, and enter:

| Field | Notes |
|---|---|
| **URL** | `https://librenms.example.com`. Subpaths work (`https://host/librenms`). Leave off `/api/v0`; it is added for you, and pasting it is harmless. Without a scheme, https is used. The integration never switches to http by itself, so for an http-only instance type `http://` explicitly. |
| **API token** | The token from step 5. |
| **Verify SSL certificate** | Turn off only for self-signed certificates. |

If the token is ever revoked or rotated, Home Assistant raises a repair prompt
and asks for a new one. The URL does not need re-entering.

### What the token gives access to

- **The token can read every SNMP credential LibreNMS stores.** LibreNMS's
  device API returns each device's SNMP community string and SNMPv3
  authentication and privacy passwords, in plain text, to any token that can
  list devices. That includes a Global Read token; there is no role that can
  see devices without also seeing their credentials. This integration uses
  none of those fields and does not keep them after each poll, but anyone who
  gets hold of the token can read them all.
- **Home Assistant backups contain the token.** It is stored with the
  integration's configuration, so every backup includes it. Treat those
  backups as if they held your SNMP credentials. If one is exposed, delete the
  token in LibreNMS under **Settings → API → API Access** and create a new
  one.
- **Use https.** Every poll sends the token to LibreNMS, and the response
  carries those credentials back. Over plain http, both cross your network
  unencrypted. If your instance only serves http, putting it behind a reverse
  proxy with TLS is worth doing before you connect Home Assistant to it.

---

## Options

**Settings → Devices & Services → LibreNMS → Configure**

| Option | Default | Notes |
|---|---|---|
| **Update interval** | 60 s | 30–3600 s. Lower detects outages faster at the cost of load on your LibreNMS instance. |
| **Count disabled and ignored devices** | off | When off, devices flagged `disabled` or `ignore` in LibreNMS are excluded from the device totals *and* from the up/down counts, so `up + down == total` always holds. The excluded count is available on the disabled-by-default `Devices excluded` sensor. |

Saving options reloads the integration.

### Changing the URL or certificate setting

**Settings → Devices & Services → LibreNMS → ⋮ → Reconfigure** changes the
instance URL or the *Verify SSL certificate* setting in place. The new address
is checked with the existing token before it is saved, and the integration
keeps all its devices, entities and history. There is no need to delete and
re-add it, which would lose that history.

---

## Entities

### Instance (the "LibreNMS" hub device)

| Entity | Type | Notes |
|---|---|---|
| `sensor.librenms_devices` | sensor | Total devices counted |
| `sensor.librenms_devices_up` | sensor | |
| `sensor.librenms_devices_down` | sensor | |
| `sensor.librenms_devices_excluded` | sensor | Disabled/ignored devices. Diagnostic, disabled by default |
| `sensor.librenms_active_alerts` | sensor | Attribute `alerts` holds the alert list (capped at 50 entries for recorder health; `truncated` says whether it was cut) |
| `sensor.librenms_critical_alerts` | sensor | |
| `sensor.librenms_warning_alerts` | sensor | |
| `binary_sensor.librenms_problem` | binary_sensor (`problem`) | On if any counted device is down, any critical alert is active, **or** the poller has stalled. Attributes: `devices_down`, `alerts_critical`, `alerts_warning`, `poller_stale`, `down_devices` (device names as shown in Home Assistant) and `down_hostnames` (the LibreNMS hostnames, often IP addresses) |
| `binary_sensor.librenms_poller_stale` | binary_sensor (`problem`) | On when LibreNMS has stopped polling. See below |
| `event.librenms_alerts` | event | Event types: `critical`, `warning`, `ok`, `recovered` |

### Per monitored device

Each LibreNMS device becomes a Home Assistant device linked to the hub, with a
`configuration_url` that deep-links to its LibreNMS page.

| Entity | Type | Notes |
|---|---|---|
| `binary_sensor.<device>_status` | binary_sensor (`connectivity`) | Attributes: `status_reason`, `hostname`, `location`, `disabled`, `ignored` |
| `sensor.<device>_active_alerts` | sensor | Active alerts for this device |
| `sensor.<device>_last_boot` | sensor (`timestamp`) | Boot time derived from LibreNMS uptime. Reported as a boot *timestamp* rather than a counter, and it only changes when the device reboots. Accurate to about one update interval |
| `sensor.<device>_hardware` | sensor | Diagnostic, disabled by default |
| `sensor.<device>_operating_system` | sensor | Diagnostic, disabled by default |
| `sensor.<device>_last_polled` | sensor | Diagnostic, disabled by default. The raw LibreNMS string, which has no time zone, so it is not exposed as a timestamp |
| `sensor.<device>_<sensor>` | sensor | One per LibreNMS health sensor. See below |

Devices added in LibreNMS appear on the next poll without reloading the
integration, and so do health sensors added to a device Home Assistant
already knows (a new disk, or a sensor LibreNMS rediscovered under a new id).
Devices removed from LibreNMS go unavailable, and can then be deleted from
the Home Assistant device page.

### Health sensors

LibreNMS already collects SNMP health readings such as temperature, fan
speed, voltage, current and power. The integration exposes them as native
Home Assistant sensors with the matching device classes, so you can automate
on them directly. It costs one extra API call per poll regardless of fleet
size, because LibreNMS returns every sensor in a single response.

**Only temperature is enabled by default.** Everything else is registered but
switched off, so enabling a class is a per-entity toggle rather than a
setup decision. On a 22-device install this produced 62 enabled entities out
of 90 registered, which is worth knowing before you enable more on a large
fleet.

Two things are handled that the raw API does not make obvious:

- **Readings are not rescaled.** `sensor_current` arrives already scaled, even
  though the payload also carries `sensor_divisor` and `sensor_multiplier`.
  Applying those again puts every voltage out by a factor of 1000.
- **Impossible readings become unavailable rather than values.** Hardware that
  cannot read a sensor tends to return a 32-bit sentinel instead of nothing:
  real instances report temperatures of 4294704 °C while the sensor's own
  limits stay perfectly sane. Those entities go unavailable and recover on
  their own if the reading comes back, and the integration logs a one-time
  warning saying how many were affected.

If the sensors request fails (a timeout, an error from LibreNMS, a role that
cannot read sensors), only the health sensors go unavailable. Device status,
alerts and the problem sensor keep working, the failure is logged once, and
the readings come back by themselves on the next successful poll. An instance
with no health sensors at all is not treated as a failure.

`state` sensors are deliberately omitted. They are enumerations whose meaning
lives in LibreNMS's translation tables, which this endpoint does not carry, so
a bare `2` could mean healthy or failed.

### Detecting a stalled poller

If LibreNMS's poller stops, every device keeps its last known state. Nothing
goes down, no alerts fire, and the dashboard stays green while nothing is
actually being checked. That is the failure mode `poller_stale` exists to
catch, and it is why it also drives `binary_sensor.librenms_problem`.

It works by watching whether the newest `last_polled` across the fleet is
still *advancing*, rather than comparing it against the clock. LibreNMS
returns that field as a naive local-time string with no time zone, so an age
comparison would need to guess the instance's zone and would be silently
wrong if the guess were off. Progress is the thing that matters, and it can
be checked without a clock at all.

The sensor turns on when the newest poll time has not moved for 15 minutes,
which is three full cycles at LibreNMS's default 300 s poll interval.
Attributes report `last_advanced` and `stalled_for_seconds`.

---

## Alert events

Two things fire when an alert changes state:

- The **`event.librenms_alerts` entity**, whose `event_type` is the alert
  severity (`critical` / `warning` / `ok`) or `recovered`.
- A **`librenms_alert` bus event**, carrying the full payload. This is usually
  the easier trigger for automations because it does not require a template to
  read the entity attributes.

Bus event payload:

```yaml
entry_id: 01J...        # config entry, useful with multiple instances
event_type: critical    # critical | warning | ok | recovered
id: 103                 # LibreNMS alert id
device_id: 1
hostname: core-sw01.lan.example
rule_id: 3
rule: High CPU
severity: critical
timestamp: "2025-07-28 10:00:00"
note: null
acknowledged: false     # true once acknowledged in LibreNMS
```

De-duplication rules:

- Alerts already open when Home Assistant starts do **not** replay.
- An alert that stays open at the same severity fires once, not once per poll.
- An alert that escalates (warning → critical) fires again at the new severity.
- Acknowledging an alert in LibreNMS, or LibreNMS marking it worse, better or
  changed, does not fire anything. The alert is still open.
- When an alert clears, a `recovered` event carries the rule and hostname from
  the last poll that saw it.
- Events follow LibreNMS's list of open alerts, not the device list. A device
  that drops out of one response and comes back fires nothing, because its
  alerts never cleared. An alert on a device the token cannot see fires
  nothing until that device becomes visible.
- If the device list suddenly comes back empty, the update counts as failed
  and entities go unavailable rather than reporting an all-clear. An empty
  list that repeats for three updates in a row is accepted as real.

### Acknowledged alerts

Acknowledged alerts stay in every count: the active, critical and warning
sensors, the per-device alert count, and `binary_sensor.librenms_problem`.
Acknowledging an alert silences LibreNMS's own notifications, but the fault
is still there, and a problem sensor that turned off when someone clicked
"acknowledge" would report a healthy network that isn't. Each alert in the
`alerts` attribute and in the bus event carries `acknowledged: true` or
`false`, so an automation can skip acknowledged alerts if you want that.

---

## Example automations

### Push notification when a device goes down

```yaml
automation:
  - alias: "Network device down"
    triggers:
      - trigger: state
        entity_id:
          - binary_sensor.core_sw01_status
          - binary_sensor.garage_ap_status
        from: "on"
        to: "off"
        for: "00:02:00"
    actions:
      - action: notify.mobile_app_pixel
        data:
          title: "Network device down"
          message: >-
            {{ trigger.to_state.name }} is unreachable
            ({{ trigger.to_state.attributes.status_reason or 'no reason given' }})
          data:
            priority: high
```

The two-minute `for:` guards against a single missed poll during a LibreNMS
restart.

### Flash the lights on a critical alert

```yaml
automation:
  - alias: "Critical LibreNMS alert"
    triggers:
      - trigger: event
        event_type: librenms_alert
        event_data:
          event_type: critical
    actions:
      - action: light.turn_on
        target:
          entity_id: light.office
        data:
          color_name: red
          flash: long
      - action: persistent_notification.create
        data:
          title: "Critical: {{ trigger.event.data.hostname }}"
          message: "{{ trigger.event.data.rule }}"
          notification_id: "librenms_{{ trigger.event.data.id }}"
```

### Clear the notification when the alert recovers

```yaml
automation:
  - alias: "LibreNMS alert recovered"
    triggers:
      - trigger: event
        event_type: librenms_alert
        event_data:
          event_type: recovered
    actions:
      - action: persistent_notification.dismiss
        data:
          notification_id: "librenms_{{ trigger.event.data.id }}"
```

### Overnight escalation only

```yaml
automation:
  - alias: "Overnight network problem"
    triggers:
      - trigger: state
        entity_id: binary_sensor.librenms_problem
        to: "on"
        for: "00:05:00"
    conditions:
      - condition: time
        after: "22:00:00"
        before: "07:00:00"
    actions:
      - action: tts.speak
        target:
          entity_id: tts.piper
        data:
          media_player_entity_id: media_player.bedroom
          message: >-
            {{ states('sensor.librenms_devices_down') }} network devices are
            down and {{ states('sensor.librenms_critical_alerts') }} critical
            alerts are active.
```

---

## Troubleshooting

**"Failed to connect"**: check that Home Assistant can reach the URL (a
LibreNMS instance behind a split-horizon DNS name is a common cause), and turn
off *Verify SSL certificate* if the instance uses a self-signed certificate.
If you entered only a host name or IP address, https was tried, and the
message says so and shows the http address to enter if your instance only
serves http.

**"LibreNMS rejected the API token"**: LibreNMS answered 401. Confirm the
token still exists under **Settings → API → API Access**, and that the user it
belongs to is not disabled. A running integration asks for a new token when
this happens.

**"LibreNMS API token lacks permission"** (a repair, or an error during
setup): LibreNMS accepted the token but answered 403 for devices or alerts, so
the token's user has a role that cannot read them. Give that user the
**Global Read** role. The integration does not ask for a new token in this
case, because the token is fine; it recovers by itself on the next update
once the role is fixed.

**"LibreNMS answered with a redirect"**: the URL you entered redirects
somewhere else, most often from http to https or to a login page. The
integration never follows redirects, because the API token would go along to
whatever address the redirect names. If the address shown is your LibreNMS
instance, enter that as the URL (or use **Reconfigure** on an existing entry).

**Entities go unavailable during a LibreNMS restart**: expected. The
coordinator retries with backoff and entities come back on the next successful
poll, with no user action needed.

**Large installs**: the `/api/v0/devices` endpoint has no column filtering, so
each poll returns full device rows. Above 500 devices the integration logs a
one-time suggestion to raise the update interval. If you run an install that
size, 300 s is a reasonable starting point.

To collect diagnostics: **Settings → Devices & Services → LibreNMS → ⋮ →
Download diagnostics**. The file is built from a fixed list of fields that are
safe to share, such as device ids, OS, model, software versions, up/down
state, alert severities, health sensor readings, and whether the poller has
stalled. Hostnames, device names, IP addresses, serial numbers, locations,
SNMP settings and credentials, alert rule names, sensor descriptions, the
instance URL and the API token are never included.

---

## Not in this version

- No write operations. Acknowledging alerts, enabling or disabling devices
  and device management are all out of scope for now.
- No per-port entities. A 50-device install with 24-port switches would create
  over a thousand entities; port tracking is planned as an opt-in option.
- No graphs. Grafana already does this well against the same data.
- No webhook push. Alerts are discovered by polling, so worst-case detection
  latency is one update interval.

---

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .
```

The test suite mocks the LibreNMS API with sanitized fixtures under
`tests/fixtures/`; no live instance is needed.

### Branding

The integration ships its own icon in `custom_components/librenms/brand/`.
Since [Home Assistant 2026.3](https://developers.home-assistant.io/blog/2026/02/24/brands-proxy-api)
a custom integration provides brand images itself and they take priority over
the brands CDN. No pull request against `home-assistant/brands` is involved,
and that repository no longer accepts icons for custom integrations. On Home
Assistant older than 2026.3 the directory is simply ignored and the default
placeholder is shown.

**The artwork is LibreNMS's, not this project's.** All eight PNGs are rendered
from the SVGs LibreNMS publishes in
[`librenms/librenms`](https://github.com/librenms/librenms/tree/master/html/images),
so the integration carries their real mark:

| Output | Rendered from |
|---|---|
| `icon.png`, `icon@2x.png` | `librenms_logo_only_light.svg` |
| `dark_icon.png`, `dark_icon@2x.png` | `librenms_logo_only_dark.svg` |
| `logo.png`, `logo@2x.png` | `librenms_logo_light.svg` |
| `dark_logo.png`, `dark_logo@2x.png` | `librenms_logo_dark.svg` |

Note the light/dark naming inverts between the two projects: LibreNMS names a
file for the background it sits on, Home Assistant for the artwork itself. So
HA's `logo.png` comes from LibreNMS's `_light` file.

Rebuild after an upstream artwork change. Sources are downloaded at build
time rather than vendored:

```bash
python3 -m venv /tmp/brandtools
/tmp/brandtools/bin/pip install resvg-py pillow pyoxipng
/tmp/brandtools/bin/python scripts/build_brand_assets.py
```

---

## License

MIT. See [LICENSE](LICENSE).
