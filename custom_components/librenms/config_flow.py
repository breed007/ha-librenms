"""Config flow for the LibreNMS integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    LibreNMSAuthError,
    LibreNMSClient,
    LibreNMSConnectionError,
    LibreNMSError,
    LibreNMSPermissionError,
    normalize_url,
)
from .const import (
    CONF_API_TOKEN,
    CONF_INCLUDE_DISABLED,
    CONF_SCAN_INTERVAL,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_INCLUDE_DISABLED,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import LibreNMSConfigEntry

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Required(CONF_API_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): BooleanSelector(),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): NumberSelector(
            NumberSelectorConfig(
                min=MIN_SCAN_INTERVAL,
                max=MAX_SCAN_INTERVAL,
                step=10,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        ),
        vol.Required(
            CONF_INCLUDE_DISABLED, default=DEFAULT_INCLUDE_DISABLED
        ): BooleanSelector(),
    }
)


async def async_validate_connection(
    hass: HomeAssistant, url: str, token: str, verify_ssl: bool
) -> dict[str, Any]:
    """Return instance info if the URL and token work.

    `/system` has no permission check in LibreNMS, so it only proves the token
    exists. Listing devices as well catches a role that cannot read them now,
    rather than as a failing entry after setup.

    Raises:
        LibreNMSAuthError: If the token was rejected.
        LibreNMSPermissionError: If the token's user may not list devices.
        LibreNMSConnectionError: If the instance could not be reached.
        LibreNMSError: For any other API-level failure.
    """
    client = LibreNMSClient(
        async_get_clientsession(hass, verify_ssl=verify_ssl), url, token
    )
    system = await client.async_get_system()
    await client.async_get_devices()
    return system


class LibreNMSConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the LibreNMS config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the instance URL and API token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                base_url = normalize_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                await self.async_set_unique_id(base_url)
                self._abort_if_unique_id_configured()

                try:
                    await async_validate_connection(
                        self.hass,
                        base_url,
                        user_input[CONF_API_TOKEN],
                        user_input[CONF_VERIFY_SSL],
                    )
                except LibreNMSAuthError:
                    errors["base"] = "invalid_auth"
                except LibreNMSPermissionError:
                    errors["base"] = "insufficient_permissions"
                except LibreNMSConnectionError:
                    errors["base"] = "cannot_connect"
                except LibreNMSError:
                    errors["base"] = "unknown"
                except Exception:
                    _LOGGER.exception("Unexpected error validating LibreNMS instance")
                    errors["base"] = "unknown"
                else:
                    return self.async_create_entry(
                        title=base_url,
                        data={**user_input, CONF_URL: base_url},
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA, user_input
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the token was rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect a replacement API token."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            try:
                await async_validate_connection(
                    self.hass,
                    entry.data[CONF_URL],
                    user_input[CONF_API_TOKEN],
                    entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
                )
            except LibreNMSAuthError:
                errors["base"] = "invalid_auth"
            except LibreNMSPermissionError:
                errors["base"] = "insufficient_permissions"
            except LibreNMSConnectionError:
                errors["base"] = "cannot_connect"
            except LibreNMSError:
                errors["base"] = "unknown"
            except Exception:
                _LOGGER.exception("Unexpected error validating LibreNMS instance")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_TOKEN: user_input[CONF_API_TOKEN]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={"url": entry.data[CONF_URL]},
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: LibreNMSConfigEntry,
    ) -> LibreNMSOptionsFlow:
        """Return the options flow handler."""
        return LibreNMSOptionsFlow()


class LibreNMSOptionsFlow(OptionsFlowWithReload):
    """Handle LibreNMS options. Saving reloads the entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the scan interval and disabled-device handling."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                    CONF_INCLUDE_DISABLED: user_input[CONF_INCLUDE_DISABLED],
                }
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA,
                {
                    CONF_SCAN_INTERVAL: self.config_entry.options.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                    CONF_INCLUDE_DISABLED: self.config_entry.options.get(
                        CONF_INCLUDE_DISABLED, DEFAULT_INCLUDE_DISABLED
                    ),
                },
            ),
        )
