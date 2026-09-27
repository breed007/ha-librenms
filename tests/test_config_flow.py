"""Tests for the LibreNMS config flow."""

from __future__ import annotations

from unittest.mock import patch

from aiohttp import ClientConnectionError
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

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


NEW_URL = "https://nms.example.com/librenms"


async def test_reconfigure_changes_url_and_ssl_in_place(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """The same entry is updated: new URL, new unique id, token untouched."""
    mock_config_entry.add_to_hass(hass)
    entry_id = mock_config_entry.entry_id

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        # Messy input is normalized, just like the first setup.
        {CONF_URL: "nms.example.com/librenms/api/v0/", CONF_VERIFY_SSL: False},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert hass.config_entries.async_entries(DOMAIN) == [mock_config_entry]
    assert mock_config_entry.entry_id == entry_id
    assert mock_config_entry.unique_id == NEW_URL
    assert mock_config_entry.title == NEW_URL
    assert mock_config_entry.data == {
        CONF_URL: NEW_URL,
        CONF_API_TOKEN: TOKEN,
        CONF_VERIFY_SSL: False,
    }


async def test_reconfigure_can_just_toggle_ssl(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Keeping the URL is not mistaken for adding the instance twice."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: BASE_URL, CONF_VERIFY_SSL: False}
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.unique_id == BASE_URL
    assert mock_config_entry.data[CONF_VERIFY_SSL] is False


async def test_reconfigure_refuses_a_url_another_entry_uses(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Two entries must never end up pointing at the same instance."""
    mock_config_entry.add_to_hass(hass)
    other = MockConfigEntry(
        domain=DOMAIN,
        title=NEW_URL,
        unique_id=NEW_URL,
        data={CONF_URL: NEW_URL, CONF_API_TOKEN: "other", CONF_VERIFY_SSL: True},
    )
    other.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: f"{NEW_URL}/", CONF_VERIFY_SSL: True}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert mock_config_entry.unique_id == BASE_URL
    assert mock_config_entry.data[CONF_URL] == BASE_URL


async def test_reconfigure_keeps_a_renamed_title(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
) -> None:
    """A title the user chose is not overwritten with the new URL."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Home network",
        unique_id=BASE_URL,
        data={CONF_URL: BASE_URL, CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: NEW_URL, CONF_VERIFY_SSL: True}
    )
    await hass.async_block_till_done()

    assert entry.title == "Home network"
    assert entry.data[CONF_URL] == NEW_URL


@pytest.mark.parametrize(
    ("user_input", "failure", "expected_errors"),
    [
        (
            {CONF_URL: "ftp://nms.example.com", CONF_VERIFY_SSL: True},
            {},
            {CONF_URL: "invalid_url"},
        ),
        (
            {CONF_URL: NEW_URL, CONF_VERIFY_SSL: True},
            {"status": 401},
            {"base": "invalid_auth"},
        ),
        (
            {CONF_URL: NEW_URL, CONF_VERIFY_SSL: True},
            {"exception": ClientConnectionError("boom")},
            {"base": "cannot_connect"},
        ),
    ],
)
async def test_reconfigure_validates_before_saving(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    user_input: dict,
    failure: dict,
    expected_errors: dict,
) -> None:
    """A URL that does not work with the existing token is not saved."""
    mock_config_entry.add_to_hass(hass)
    for key, value in failure.items():
        setattr(mock_librenms, key, value)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == expected_errors
    assert mock_config_entry.data[CONF_URL] == BASE_URL
    assert mock_config_entry.unique_id == BASE_URL


async def test_bare_host_that_fails_says_https_was_assumed(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An http-only instance entered as a bare IP gets a specific, safe answer.

    The flow never retries over http by itself: that would send the token,
    and receive every SNMP credential, in cleartext without the user
    choosing it.
    """
    mock_librenms.exception = ClientConnectionError("connection refused")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: "10.0.0.5:8000", CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect_https_assumed"}
    assert result["description_placeholders"] == {
        "url": "https://10.0.0.5:8000",
        "http_url": "http://10.0.0.5:8000",
    }
    schemes = {call[1].scheme for call in aioclient_mock.mock_calls}
    assert schemes == {"https"}


async def test_bare_host_then_explicit_http_works(
    hass: HomeAssistant, mock_librenms: MockLibreNMS
) -> None:
    """Typing http:// is the deliberate choice, and it is honored as typed."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_URL: "http://10.0.0.5:8000",
            CONF_API_TOKEN: TOKEN,
            CONF_VERIFY_SSL: True,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_URL] == "http://10.0.0.5:8000"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        # An explicit scheme gets the plain message: nothing was assumed.
        ({"url": "https://10.0.0.5", "exc": True}, "cannot_connect"),
        # https answered, just with an error, so the hint would mislead.
        ({"url": "10.0.0.5", "status": 500}, "cannot_connect"),
    ],
)
async def test_https_hint_only_when_it_applies(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    failure: dict,
    expected: str,
) -> None:
    """The https hint appears only for a bare host that got no answer."""
    if failure.get("exc"):
        mock_librenms.exception = ClientConnectionError("refused")
    if "status" in failure:
        mock_librenms.status = failure["status"]

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: failure["url"], CONF_API_TOKEN: TOKEN, CONF_VERIFY_SSL: True},
    )

    assert result["errors"] == {"base": expected}


async def test_reconfigure_bare_host_gets_the_https_hint(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Reconfigure treats a bare host the same way as first setup."""
    mock_config_entry.add_to_hass(hass)
    mock_librenms.exception = ClientConnectionError("refused")

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "10.0.0.5", CONF_VERIFY_SSL: True}
    )

    assert result["errors"] == {"base": "cannot_connect_https_assumed"}
    assert result["description_placeholders"]["http_url"] == "http://10.0.0.5"


async def test_reconfigure_can_move_to_a_new_instance_with_a_new_token(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A different instance needs a different token; the entry is kept."""
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_URL: NEW_URL,
            CONF_API_TOKEN: " new-instance-token ",
            CONF_VERIFY_SSL: True,
        },
    )
    await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data == {
        CONF_URL: NEW_URL,
        CONF_API_TOKEN: "new-instance-token",
        CONF_VERIFY_SSL: True,
    }
    # The new instance was checked with the new token, not the old one.
    sent = {call[3]["X-Auth-Token"] for call in aioclient_mock.mock_calls}
    assert sent == {"new-instance-token"}


@pytest.mark.parametrize("blank", [None, "", "   "])
async def test_reconfigure_blank_token_keeps_the_current_one(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    blank: str | None,
) -> None:
    """Leaving the token field empty is not the same as clearing the token."""
    mock_config_entry.add_to_hass(hass)
    user_input = {CONF_URL: NEW_URL, CONF_VERIFY_SSL: True}
    if blank is not None:
        user_input[CONF_API_TOKEN] = blank

    result = await mock_config_entry.start_reconfigure_flow(hass)
    await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()

    assert mock_config_entry.data[CONF_API_TOKEN] == TOKEN


async def test_reconfigure_rejected_new_token_is_not_saved(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A token the new instance refuses stays out of the entry."""
    mock_config_entry.add_to_hass(hass)
    mock_librenms.status = 401

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_URL: NEW_URL, CONF_API_TOKEN: "wrong", CONF_VERIFY_SSL: True},
    )

    assert result["errors"] == {"base": "invalid_auth"}
    assert mock_config_entry.data[CONF_API_TOKEN] == TOKEN
    assert mock_config_entry.data[CONF_URL] == BASE_URL
