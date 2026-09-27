"""Tests for the LibreNMS config flow."""

from __future__ import annotations

from unittest.mock import patch

from aiohttp import ClientConnectionError
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librenms.const import (
    CONF_API_TOKEN,
    CONF_INCLUDE_DISABLED,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
)

from .conftest import MockLibreNMS, setup_integration
from .const import BASE_URL, TOKEN


@pytest.fixture(autouse=True)
def bypass_setup() -> None:
    """Skip the real entry setup while exercising the flow."""
    with patch(
        "custom_components.librenms.async_setup_entry", return_value=True
    ) as mock_setup:
        yield mock_setup


async def test_user_flow(hass: HomeAssistant, mock_librenms: MockLibreNMS) -> None:
    """A valid URL and token create an entry keyed on the normalized URL."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            # Deliberately messy input: no scheme, trailing slash, API path.
            CONF_URL: "librenms.example.com/api/v0/",
            CONF_API_TOKEN: TOKEN,
            CONF_VERIFY_SSL: True,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == BASE_URL
    assert result["data"] == {
        CONF_URL: BASE_URL,
        CONF_API_TOKEN: TOKEN,
        CONF_VERIFY_SSL: True,
    }
    assert result["result"].unique_id == BASE_URL


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ({"status": 401}, "invalid_auth"),
        ({"status": 403}, "insufficient_permissions"),
        ({"status": 500}, "cannot_connect"),
        ({"exception": ClientConnectionError("boom")}, "cannot_connect"),
    ],
)
async def test_user_flow_errors_recover(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    failure: dict,
    expected_error: str,
) -> None:
    """Errors are shown on the form, and the flow completes after a retry."""
    for key, value in failure.items():
        setattr(mock_librenms, key, value)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: BASE_URL, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}

    mock_librenms.status = 200
    mock_librenms.exception = None

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: BASE_URL, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_api_error(
    hass: HomeAssistant, mock_librenms: MockLibreNMS
) -> None:
    """A 200 response carrying an API-level error surfaces as unknown."""
    mock_librenms.system = {"status": "error", "message": "something broke"}

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: BASE_URL, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_user_flow_unexpected_error(
    hass: HomeAssistant, mock_librenms: MockLibreNMS
) -> None:
    """An unforeseen exception is caught and logged, not raised at the user."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.librenms.config_flow.async_validate_connection",
        side_effect=RuntimeError("boom"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: BASE_URL, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ({"exception": ClientConnectionError("boom")}, "cannot_connect"),
        ({"system": {"status": "error", "message": "nope"}}, "unknown"),
    ],
)
async def test_reauth_flow_errors(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    failure: dict,
    expected_error: str,
) -> None:
    """Reauth surfaces the same error classes as the initial setup."""
    mock_config_entry.add_to_hass(hass)
    for key, value in failure.items():
        setattr(mock_librenms, key, value)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": mock_config_entry.entry_id,
            "unique_id": mock_config_entry.unique_id,
        },
        data=mock_config_entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "another-token"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected_error}


async def test_reauth_flow_unexpected_error(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """An unforeseen exception during reauth keeps the user on the form."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": mock_config_entry.entry_id,
            "unique_id": mock_config_entry.unique_id,
        },
        data=mock_config_entry.data,
    )
    with patch(
        "custom_components.librenms.config_flow.async_validate_connection",
        side_effect=RuntimeError("boom"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_TOKEN: "another-token"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}


async def test_user_flow_invalid_url(
    hass: HomeAssistant, mock_librenms: MockLibreNMS
) -> None:
    """A URL that cannot be normalized is rejected on the URL field."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_URL: "ftp://librenms.example.com",
            CONF_API_TOKEN: TOKEN,
            CONF_VERIFY_SSL: True,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_URL: "invalid_url"}


async def test_duplicate_entry_aborts(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The same instance cannot be added twice, however it is spelled."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_URL: "https://librenms.example.com/",
            CONF_API_TOKEN: "a-different-token",
            CONF_VERIFY_SSL: True,
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_flow(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A rejected token can be replaced without re-entering the URL."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": mock_config_entry.entry_id,
            "unique_id": mock_config_entry.unique_id,
        },
        data=mock_config_entry.data,
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    # A still-bad token keeps the user on the form.
    mock_librenms.status = 401
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "still-wrong"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    mock_librenms.status = 200
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "a-fresh-token"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_API_TOKEN] == "a-fresh-token"
    assert mock_config_entry.data[CONF_URL] == BASE_URL


async def test_options_flow(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Options are stored with the scan interval coerced to an int."""
    await setup_integration(hass, mock_config_entry)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_SCAN_INTERVAL: 300.0, CONF_INCLUDE_DISABLED: True},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {
        CONF_SCAN_INTERVAL: 300,
        CONF_INCLUDE_DISABLED: True,
    }


async def test_user_flow_rejects_a_role_that_cannot_list_devices(
    hass: HomeAssistant, mock_librenms: MockLibreNMS
) -> None:
    """/system has no permission check, so devices are listed to prove access."""
    mock_librenms.fail("devices", status=403, message="Insufficient permissions")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: BASE_URL, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "insufficient_permissions"}


async def test_reauth_rejects_a_role_that_cannot_list_devices(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A valid token with the wrong role is refused instead of being stored."""
    mock_config_entry.add_to_hass(hass)
    mock_librenms.fail("devices", status=403)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_REAUTH,
            "entry_id": mock_config_entry.entry_id,
            "unique_id": mock_config_entry.unique_id,
        },
        data=mock_config_entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "another-token"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "insufficient_permissions"}
    assert mock_config_entry.data[CONF_API_TOKEN] == TOKEN
