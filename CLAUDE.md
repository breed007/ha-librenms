# CLAUDE.md

Working notes for this repository: conventions, architecture, the dev loop,
and the LibreNMS behavior that has already cost us bugs. Read the gotchas
section before touching anything that parses an API response.

## What this is

`ha-librenms`: a Home Assistant custom integration (domain `librenms`) that
polls a LibreNMS instance over its REST API (`/api/v0`) and exposes devices,
alerts and health sensors. Read-only; it never writes to LibreNMS. Public repo
at github.com/breed007/ha-librenms, MIT, installed through HACS as a custom
repository. Minimum Home Assistant is 2026.8.0 (`hacs.json`, README
Requirements, and the floor row in the CI matrix must all agree).

## Architecture

```
custom_components/librenms/
  api.py            LibreNMSClient: one GET helper plus one method per endpoint.
                    Maps HTTP status to exceptions (see "Errors" below).
  coordinator.py    One DataUpdateCoordinator. Parses payloads into dataclasses
                    (LibreNMSDevice, LibreNMSAlert, LibreNMSSensor), derives counts,
                    alert deltas and poller health into LibreNMSData.
  __init__.py       Setup: first refresh, then registers the hub device, then
                    forwards platforms.
  entity.py         Base entities and async_add_new_entities(), which adds
                    entities for keys (device id, (device, sensor) pair) that
                    appear on later polls.
  binary_sensor.py  problem, poller_stale, per-device status.
  sensor.py         Instance counts, per-device sensors, last boot, health sensors.
  event.py          The alerts event entity.
  config_flow.py    user, reauth, reconfigure steps and the options flow.
  diagnostics.py    Built from an allowlist of parsed fields. Never a raw payload.
```

Polls per update: `/devices`, `/alerts?state=1,2,3,4,5` and
`/resources/sensors`, run concurrently. Devices and alerts are core; if either
fails the whole update fails. Sensors are optional; a failure there only marks
health sensors unavailable (`LibreNMSData.sensors_available`).

The hub device is registered explicitly in `async_setup_entry`, and monitored
devices link to it with `via_device_id`. Do not go back to
`DeviceInfo(via_device=...)`: deprecated in 2026.9, removed in 2027.8.

Entity unique ids are `{entry_id}_{key}` and `{entry_id}_{device_id}_{key}`,
so they survive a URL change through the reconfigure flow but not a delete and
re-add.

### Trusting the device list

`/devices` can come back empty or partial for reasons that have nothing to
do with the network (a permission cache being rebuilt, a database hiccup, a
role change). Three rules keep that from producing false events or a false
all-clear, and they are deliberately independent of each other:

1. **Alert events follow `/alerts`, never `/devices`.** The coordinator
   tracks alerts it has accounted for (`_known_alerts`), which the user has
   seen (`_shown_alerts`) and their last record. `new` fires only for a
   visible alert; `recovered` only when LibreNMS stops listing a shown alert
   as open. A device leaving the list and returning fires nothing.
2. **The problem sensor follows open alerts the user has seen.** A shown
   critical alert that is still open keeps `has_problem` true even when its
   device is missing from the list (`alerts_critical_hidden`). Counts still
   follow the visible devices, as the round-1 Global Read fix intended.
   Alerts on devices the token never saw count for nothing. "Seen" must
   survive a reload or restart, so at startup an open alert whose device is
   already in the device registry for this entry counts as seen. Deleting
   that device in Home Assistant (`async_remove_config_entry_device`) is the
   user's way out: its alerts stop counting as seen immediately, and after a
   reload the registry no longer lists it.
3. **An unexpected empty list fails the update.** Devices are expected when
   the last published update had some, or, before the first success, when
   the device registry holds devices for the entry (so a reload or restart
   during a blip is covered). Such an empty list raises UpdateFailed. The
   run of them is counted in `hass.data[DOMAIN]["empty_device_polls"]`,
   because a new coordinator is built for every setup attempt; any other
   result resets it. The third in a row is accepted, after which nothing is
   expected until devices return. Because of rule 2, accepting an empty list
   never clears a fault LibreNMS still has open.

A missing `last_polled` (no devices to read it from) is "nothing to judge"
for the stale-poller check, never "the poller moved": the stall timer keeps
running and the previous stale judgment carries over until poll times return.

### Errors

| HTTP | Exception | Effect |
|---|---|---|
| 401 | `LibreNMSAuthError` | Reauth (`ConfigEntryAuthFailed`) |
| 403 | `LibreNMSPermissionError` | Repair issue naming the Global Read role. Never reauth: a new token cannot fix a role, and reauth would loop |
| 404 | `LibreNMSNotFoundError` | Connection error. On `/resources/sensors`, only the body message "Sensors do not exist" means "no sensors"; any other 404 is a sensors failure |
| 3xx | `LibreNMSRedirectError` | Redirects are never followed (see gotchas) |
| no response | `LibreNMSUnreachableError` | Connection error; the flow explains a failed https guess |

## LibreNMS gotchas (do not relearn these)

Field names in this API routinely do not mean what they say. Two v0.x bugs
shipped because test fixtures encoded the same wrong assumption as the code.
**Fixtures must mirror real LibreNMS payloads, and the mock must apply the
same filtering LibreNMS does.**

- **Alert `state`** (`LibreNMS/Enum/AlertState.php`): RECOVERED=0, ACTIVE=1,
  ACKNOWLEDGED=2, WORSE=3, BETTER=4, CHANGED=5. An open alert moves between
  1 and 5. `/alerts` defaults to `state=1` and splits the parameter on commas,
  so ask for all five or acknowledged alerts look recovered.
- **`/alerts` has no per-device permission filter; `/devices` and
  `/resources/sensors` do** (`hasDeviceAccess`). Only admin and Global Read see
  every device. A Normal User sees only devices assigned to it directly or
  through a device group (static groups only, unless
  `permission.device_group.allow_dynamic` is set), usually none.
  Alerts on devices not in the device list are dropped.
- **`/devices` returns SNMP secrets** for every device the token can see, to
  any role that can list devices through the API:
  `community`, `authname`, `authpass`, `cryptopass`, plus `sysContact`,
  `sysDescr`, `snmpEngineID`, `dependency_parent_hostname` and more. Never keep
  the raw payload and never put a raw payload in diagnostics.
- **`/devices` has no `columns` parameter**; it always returns full rows.
- **`/resources/sensors` answers 404 "Sensors do not exist"** when there are
  none. That is an empty list, not an error. LibreNMS also answers 404 ("This
  API route doesn't exist.") for a missing route, so check the message.
- **`/system` has no permission middleware.** It proves a token exists, not
  that it can read anything; the config flow also lists devices.
- **`uptime` is written once per LibreNMS poll** (every 300 s by default) and
  frozen in between. LibreNMS detects a reboot as "new uptime < old uptime".
  `now - uptime` is an upper bound on the boot time; the last boot sensor keeps
  the earliest bound. Tests must model the 300 s cadence, not advance uptime
  in real time.
- **`uptime` of 0** means LibreNMS could not read it.
- **`last_polled`** is a naive local-time string with no zone. The stale-poller
  check compares it with the previous poll's value instead of the clock.
- **`sensor_current` is already scaled.** Do not apply `sensor_divisor` or
  `sensor_multiplier` again. Unreadable sensors report a ~2^32 sentinel, caught
  by `SENSOR_PLAUSIBLE_RANGE`.
- **`display`** defaults to the hostname through a global template, so it is
  only a real name when it differs from `hostname`; otherwise use `sysName`.
- **The vendor** comes from the `icon` filename, not `os`.
- **API tokens changed twice in 2026** (verified in LibreNMS source at the
  tags named; the README's token setup depends on this):
  - Before 26.4.0: `/api-access` (menu "API Settings") lets an account with
    `api.access` (admins) create a legacy token for any user from a picker.
  - 26.4.0 to 26.8.x: `/api-access` (menu "API Tokens") is self-service only
    (`ApiAccessController`). v0 still authenticates legacy tokens only
    (`auth:token` guard, `ApiToken::isValid`). `lnms api:token-create`
    arrives in 26.8.0 but makes a Sanctum token, which v0 rejects there.
  - 26.9.0 on: v0 uses `auth:sanctum` and reads `X-Auth-Token` for
    `api/v0*`; legacy tokens were migrated to Sanctum by hash, so old tokens
    keep working. `lnms api:token-create <username> [--name=<label>]` works;
    tokens do not expire (`sanctum.expiration` is null).
  - `api.access` gates only the token page and its menu entry, never the REST
    API. Global Read does not get it (`Gate::before` grants only
    `*.view`/`viewAll`/`viewAny`), and the seeded roles carry no permissions.
  - Disabling a user stops its tokens from 26.8.0 (checked in 26.8.0 and
    26.9.x) but not at 26.4.0, so don't document that as a revocation step.
- **Redirects:** aiohttp strips `Authorization` and cookies on a cross-origin
  redirect but not `X-Auth-Token`. Requests use `allow_redirects=False`.

## Dev loop

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check .
```

- Test against more than one Home Assistant version before pushing. Each
  `pytest-homeassistant-custom-component` (phcc) release pins exactly one HA
  release; a second venv with a different phcc pin is the easy way to check
  another version.
- On HA 2026.9 and later, tests must look devices up with
  `async_get_device_by_identifier` (use `get_device()` in `tests/conftest.py`);
  `async_get_device` is rejected in tests.
- Watch for `Detected that custom integration` warnings. They only print for
  failing tests unless you run with `-o log_cli=true --log-cli-level=WARNING`.
- `MockLibreNMS` in `tests/conftest.py` serves the fixtures. Change what the
  instance reports between polls by assigning to it, use `fail(endpoint, ...)`
  and `recover(endpoint)` for per-endpoint failures, and `async_poll()` to
  advance time and run the coordinator.
- Delete a device in tests with `remove_device()` from `tests/conftest.py`.
  It sends `config/device_registry/remove` where registered (2026.9+) and
  the older `remove_config_entry` otherwise; the older one logs a
  deprecation warning on 2026.9 and goes away in 2027.9. Drop the fallback
  when the floor passes 2026.8.
- Sockets are blocked in tests except through the `socket_enabled` fixture,
  which `tests/test_redirects.py` uses for real servers on 127.0.0.1.

## CI and version pinning

- `.github/workflows/tests.yml`: ruff (pinned 0.16.2) plus pytest across a
  matrix of HA versions, selected by pinning phcc. Current rows: 2026.9.4
  (0.13.367), 2026.8.3 (0.13.357) and 2026.8.0 (0.13.354). Runs on push,
  pull request, weekly on Monday, and manually.
- The last matrix row is the declared minimum in `hacs.json`. Raise both
  together, never one alone. The minimum was raised from 2025.8.0 to 2026.8.0
  in v0.3.0 because 2025.8 had never been tested and the suite needs 2026.8+
  registry APIs.
- phcc needs Python 3.14 from 0.13.317 onward; CI uses 3.14.
- `ruff target-version` stays `py313` on purpose even though HA 2026.8 needs
  Python 3.14.2: py314 lets the formatter rewrite code into 3.14-only syntax
  for no benefit.
- `.github/workflows/validate.yml`: hassfest and the HACS action, also weekly.
  hassfest rejects URLs inside `strings.json`; pass them as description
  placeholders.

## Conventions

- US English everywhere we own the text. Never change the spelling of a
  LibreNMS field or other third-party symbol.
- `strings.json` and `translations/en.json` stay identical.
- Commit subjects in the imperative; bodies explain why. One commit per fix or
  tight group.
- Every behavior fix gets a regression test that fails on the old code. Prove
  it by running the test against the old code before committing.
- README prose is published under the maintainer's name. Keep it plain and
  specific, and avoid AI writing tells.
- Releases: bump `manifest.json`, tag `v<version>`, publish notes. Only after
  QA signs off on the branch.
- Never commit `.claude/settings.local.json`.

## Branding

`custom_components/librenms/brand/` holds the icon and logo, rendered from
LibreNMS's own SVGs by `scripts/build_brand_assets.py`. Since HA 2026.3 a
custom integration ships its brand images itself; `home-assistant/brands` no
longer accepts them.
