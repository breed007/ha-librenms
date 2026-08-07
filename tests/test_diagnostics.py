"""Tests for LibreNMS diagnostics."""

from __future__ import annotations

import json

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librenms.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import MockLibreNMS, setup_integration
from .const import TOKEN

REDACTED = "**REDACTED**"


async def test_diagnostics_redacts_identifying_data(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The token, URL and per-device identifiers never leave the instance."""
    await setup_integration(hass, mock_config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)
    serialised = json.dumps(diagnostics)

    assert TOKEN not in serialised
    assert "librenms.example.com" not in serialised
    assert "core-sw01" not in serialised

    assert diagnostics["entry"]["data"]["api_token"] == REDACTED
    assert diagnostics["entry"]["data"]["url"] == REDACTED
    assert diagnostics["devices"][0]["hostname"] == REDACTED
    assert diagnostics["alerts"][0]["hostname"] == REDACTED


async def test_diagnostics_keeps_useful_context(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Enough detail survives redaction to actually debug an issue."""
    await setup_integration(hass, mock_config_entry)

    diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["system"]["local_ver"] == "25.7.0"
    assert diagnostics["counts"] == {
        "devices_total": 2,
        "devices_up": 1,
        "devices_down": 1,
        "devices_excluded": 2,
        "alerts_active": 2,
        "alerts_critical": 1,
        "alerts_warning": 1,
    }
    assert diagnostics["entry"]["options"]["scan_interval"] == 60
    assert len(diagnostics["devices"]) == 4
    assert diagnostics["devices"][0]["os"] == "edgeswitch"
    assert diagnostics["alerts"][0]["severity"] == "critical"
