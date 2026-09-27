"""Tests that the API token never follows a redirect."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librenms.api import (
    LibreNMSClient,
    LibreNMSRedirectError,
    _redirect_target,
)
from custom_components.librenms.const import (
    CONF_API_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
)

from .conftest import MockLibreNMS, async_poll, setup_integration
from .const import BASE_URL, TOKEN


class _Servers:
    """A LibreNMS stand-in that redirects, and the host it redirects to."""

    def __init__(self) -> None:
        self.seen_by_other_host: list[dict[str, str]] = []
        self.librenms: TestServer
        self.other: TestServer

    async def start(self) -> None:
        async def record(request: web.Request) -> web.Response:
            self.seen_by_other_host.append(dict(request.headers))
            return web.json_response({"status": "ok", "system": [{}]})

        other_app = web.Application()
        other_app.router.add_get("/{tail:.*}", record)
        self.other = TestServer(other_app, host="127.0.0.1")
        await self.other.start_server()

        async def redirect(request: web.Request) -> web.Response:
            raise web.HTTPFound(str(self.other.make_url("/login")))

        librenms_app = web.Application()
        librenms_app.router.add_get("/{tail:.*}", redirect)
        self.librenms = TestServer(librenms_app, host="127.0.0.1")
        await self.librenms.start_server()

    async def close(self) -> None:
        await self.librenms.close()
        await self.other.close()


@pytest.fixture
async def servers(socket_enabled: None) -> AsyncGenerator[_Servers]:
    """Run both servers on 127.0.0.1 on different ports (different origins).

    Real servers rather than the request mocker, which never follows a
    redirect and so could not show whether the token leaks.
    """
    running = _Servers()
    await running.start()
    yield running
    await running.close()


async def test_aiohttp_alone_would_forward_the_token(servers: _Servers) -> None:
    """The premise: aiohttp keeps custom headers on a cross-origin redirect.

    It strips Authorization and cookies, but not X-Auth-Token. If this ever
    stops being true the protection below is still correct, just redundant.
    """
    async with (
        ClientSession() as session,
        session.get(
            servers.librenms.make_url("/api/v0/system"),
            headers={"X-Auth-Token": TOKEN},
        ),
    ):
        pass

    assert [h.get("X-Auth-Token") for h in servers.seen_by_other_host] == [TOKEN]


async def test_client_never_sends_the_token_to_a_redirect(servers: _Servers) -> None:
    """The client stops at the redirect and names where it pointed."""
    async with ClientSession() as session:
        client = LibreNMSClient(session, str(servers.librenms.make_url("/")), TOKEN)
        with pytest.raises(LibreNMSRedirectError) as raised:
            await client.async_get_system()

    assert servers.seen_by_other_host == []
    assert raised.value.location == str(servers.other.make_url("/login"))


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        # The instance moved: suggest the base URL to enter.
        ("https://librenms.example.com/api/v0/system", "https://librenms.example.com"),
        (
            "https://nms.example.com/librenms/api/v0/system",
            "https://nms.example.com/librenms",
        ),
        # A relative redirect resolves against the request.
        ("/librenms/api/v0/system", "http://10.0.0.5/librenms"),
        # Anything else is reported as is.
        (
            "https://sso.example.com/login?next=/",
            "https://sso.example.com/login?next=/",
        ),
        (None, "an unnamed location"),
    ],
)
def test_redirect_target(location: str | None, expected: str) -> None:
    """The error names an address the user can act on."""
    assert _redirect_target("http://10.0.0.5/api/v0/system", location, "system") == (
        expected
    )


def _redirect_system(mock_librenms: MockLibreNMS, location: str) -> None:
    mock_librenms.fail("system", status=301, text="", headers={"Location": location})


async def test_user_flow_explains_a_redirect(
    hass: HomeAssistant, mock_librenms: MockLibreNMS
) -> None:
    """The typical case: http:// on an instance that redirects to https://."""
    _redirect_system(mock_librenms, "https://librenms.example.com/api/v0/system")

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_URL: "http://librenms.example.com",
            CONF_API_TOKEN: TOKEN,
            CONF_VERIFY_SSL: True,
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "redirected"}
    assert result["description_placeholders"] == {
        "redirect_url": "https://librenms.example.com"
    }


async def test_reauth_flow_explains_a_redirect(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Reauth keeps its own placeholder alongside the redirect target."""
    mock_config_entry.add_to_hass(hass)
    _redirect_system(mock_librenms, "https://sso.example.com/login")

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

    assert result["errors"] == {"base": "redirected"}
    placeholders = result["description_placeholders"]
    assert placeholders["url"] == BASE_URL
    assert placeholders["redirect_url"] == "https://sso.example.com/login"


async def test_redirect_while_running_is_a_connection_failure(
    hass: HomeAssistant,
    mock_librenms: MockLibreNMS,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Entities go unavailable and the log says where it tried to go."""
    await setup_integration(hass, mock_config_entry)
    headers: dict[str, Any] = {"Location": "https://sso.example.com/login"}
    mock_librenms.fail("devices", status=302, text="", headers=headers)

    await async_poll(hass, freezer)

    assert hass.states.get("binary_sensor.core_sw01_status").state == (
        STATE_UNAVAILABLE
    )
    assert "redirected to https://sso.example.com/login" in caplog.text
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert not [f for f in flows if f["context"]["source"] == SOURCE_REAUTH]
