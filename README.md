# LibreNMS for Home Assistant

[![Tests](https://github.com/breed007/ha-librenms/actions/workflows/tests.yml/badge.svg)](https://github.com/breed007/ha-librenms/actions/workflows/tests.yml)
[![Validate](https://github.com/breed007/ha-librenms/actions/workflows/validate.yml/badge.svg)](https://github.com/breed007/ha-librenms/actions/workflows/validate.yml)
[![hacs](https://img.shields.io/badge/HACS-custom-41BDF5.svg)](https://hacs.xyz)

See your entire monitored network — device up/down, active alerts, health
summary — inside Home Assistant, and automate on it.

This is a read-only integration against the [LibreNMS REST
API](https://docs.librenms.org/API/). It polls your instance and turns every
monitored device into a Home Assistant device, with instance-wide summary
sensors for dashboards and an event entity for alert-driven automations. It
does not modify anything in LibreNMS.

A community project. Not affiliated with, endorsed by, or supported by the
LibreNMS project.

---

## Screenshots

> **TODO before release:** add real screenshots from a live instance.
> Suggested set, saved under `docs/`:
>
> - `docs/config-flow.png` — the URL + token setup dialog
> - `docs/hub-device.png` — the LibreNMS hub device page with summary sensors
> - `docs/device.png` — a single network device with its status and alert count
> - `docs/dashboard.png` — an example network dashboard card

---

## Requirements

- Home Assistant **2025.8.0** or newer
- A reachable LibreNMS instance with the API enabled
- A LibreNMS API token

### Supported LibreNMS versions

Developed and tested against LibreNMS **25.x**. The three endpoints this
integration uses — `/api/v0/system`, `/api/v0/devices` and `/api/v0/alerts` —
have been stable across the lifetime of the v0 API, so older releases will
very likely work. A hard minimum version has not been pinned yet; if you hit a
problem on an older release, please open an issue with your version so it can
be documented.

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

**Create a dedicated read-only user for Home Assistant.** A LibreNMS API token
inherits the permissions of the user it belongs to, and this integration never
needs write access. Handing it an admin token means a leaked Home Assistant
backup leaks admin access to your monitoring system.

1. In LibreNMS, go to **Settings → Manage → Users → Add User**.
2. Create a user (for example `homeassistant`) with the **Normal User** role.
3. Go to **Settings → API → API Access**.
4. Click **Create API access token**, select the `homeassistant` user, give it
   a description, and confirm.
5. Copy the token value.

Then in Home Assistant: **Settings → Devices & Services → Add Integration →
LibreNMS**, and enter:

| Field | Notes |
|---|---|
| **URL** | `https://librenms.example.com`. Subpaths work (`https://host/librenms`). Leave off `/api/v0` — it is added for you, and pasting it is harmless. |
| **API token** | The token from step 5. |
| **Verify SSL certificate** | Turn off only for self-signed certificates. |

If the token is ever revoked or rotated, Home Assistant raises a repair prompt
and asks for a new one — the URL does not need re-entering.

---

## Options

**Settings → Devices & Services → LibreNMS → Configure**

| Option | Default | Notes |
|---|---|---|
| **Update interval** | 60 s | 30–3600 s. Lower detects outages faster at the cost of load on your LibreNMS instance. |
| **Count disabled and ignored devices** | off | When off, devices flagged `disabled` or `ignore` in LibreNMS are excluded from the device totals *and* from the up/down counts, so `up + down == total` always holds. The excluded count is available on the disabled-by-default `Devices excluded` sensor. |

Saving options reloads the integration.

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
| `binary_sensor.librenms_problem` | binary_sensor (`problem`) | On if any counted device is down **or** any critical alert is active. Attributes include `down_hostnames` |
| `event.librenms_alerts` | event | Event types: `critical`, `warning`, `ok`, `recovered` |

### Per monitored device

Each LibreNMS device becomes a Home Assistant device linked to the hub, with a
`configuration_url` that deep-links to its LibreNMS page.

| Entity | Type | Notes |
|---|---|---|
| `binary_sensor.<device>_status` | binary_sensor (`connectivity`) | Attributes: `status_reason`, `hostname`, `location`, `disabled`, `ignored` |
| `sensor.<device>_active_alerts` | sensor | Active alerts for this device |
| `sensor.<device>_last_boot` | sensor (`timestamp`) | Boot time derived from LibreNMS uptime. Reported as a boot *timestamp* rather than a counter so the state does not change on every poll |
| `sensor.<device>_hardware` | sensor | Diagnostic, disabled by default |
| `sensor.<device>_operating_system` | sensor | Diagnostic, disabled by default |
| `sensor.<device>_last_polled` | sensor | Diagnostic, disabled by default. Raw LibreNMS string — it has no time zone, so it is not exposed as a timestamp |

Devices added in LibreNMS appear on the next poll without reloading the
integration. Devices removed from LibreNMS go unavailable, and can then be
deleted from the Home Assistant device page.

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
```

De-duplication rules:

- Alerts already open when Home Assistant starts do **not** replay.
- An alert that stays open at the same severity fires once, not once per poll.
- An alert that escalates (warning → critical) fires again at the new severity.
- When an alert clears, a `recovered` event carries the rule and hostname from
  the last poll that saw it.

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

**"Failed to connect"** — check that Home Assistant can reach the URL (a
LibreNMS instance behind a split-horizon DNS name is a common cause), and turn
off *Verify SSL certificate* if the instance uses a self-signed certificate.

**"Invalid API token"** — the token was rejected with a 401 or 403. Confirm
the token still exists under **Settings → API → API Access**, and that the user
it belongs to is not disabled.

**Entities go unavailable during a LibreNMS restart** — expected. The
coordinator retries with backoff and entities come back on the next successful
poll, with no user action needed.

**Large installs** — the `/api/v0/devices` endpoint has no column filtering, so
each poll returns full device rows. Above 500 devices the integration logs a
one-time suggestion to raise the update interval. If you run an install that
size, 300 s is a reasonable starting point.

To collect diagnostics: **Settings → Devices & Services → LibreNMS → ⋮ →
Download diagnostics**. Hostnames, locations, SNMP credentials, the instance
URL and the API token are redacted.

---

## Not in this version

- No write operations — acknowledging alerts, enabling/disabling devices and
  device management are all out of scope for now.
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

Integration artwork is not stored in the component — Home Assistant reads it
from the [`home-assistant/brands`](https://github.com/home-assistant/brands)
repository. The PNG set is staged in [`brands/`](brands/README.md) and rebuilt
from LibreNMS's official SVGs with `scripts/build_brand_assets.py`.

---

## License

MIT — see [LICENSE](LICENSE).
