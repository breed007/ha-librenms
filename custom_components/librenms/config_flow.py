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
    LibreNMSRedirectError,
    LibreNMSUnreachableError,
    has_scheme,
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

STEP_RECONFIGURE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        # Optional: blank keeps the stored token. A new one is needed when
        # moving to a different LibreNMS instance, which the old token will
        # not work on and which reauth cannot reach.
        vol.Optional(CONF_API_TOKEN): TextSelector(
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

    async def _async_validate(
        self, url: str, token: str, verify_ssl: bool, *, https_assumed: bool = False
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Validate a connection and return the form errors and placeholders.

        Both are empty when the URL and token work. `https_assumed` means the
        user typed no scheme; if nothing answered, the error then says https
        was tried and how to choose http deliberately, rather than a generic
        "failed to connect" for an instance that may only serve http.
        """
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        try:
            await async_validate_connection(self.hass, url, token, verify_ssl)
        except LibreNMSAuthError:
            errors["base"] = "invalid_auth"
        except LibreNMSPermissionError:
            errors["base"] = "insufficient_permissions"
        except LibreNMSRedirectError as err:
            errors["base"] = "redirected"
            placeholders["redirect_url"] = err.location
        except LibreNMSUnreachableError:
            if https_assumed:
                errors["base"] = "cannot_connect_https_assumed"
                placeholders["url"] = url
                placeholders["http_url"] = "http://" + url.removeprefix("https://")
            else:
                errors["base"] = "cannot_connect"
        except LibreNMSConnectionError:
            errors["base"] = "cannot_connect"
        except LibreNMSError:
            errors["base"] = "unknown"
        except Exception:
            _LOGGER.exception("Unexpected error validating LibreNMS instance")
            errors["base"] = "unknown"
        return errors, placeholders

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the instance URL and API token."""
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}

        if user_input is not None:
            try:
                base_url = normalize_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                await self.async_set_unique_id(base_url)
                self._abort_if_unique_id_configured()

                errors, placeholders = await self._async_validate(
                    base_url,
                    user_input[CONF_API_TOKEN],
                    user_input[CONF_VERIFY_SSL],
                    https_assumed=not has_scheme(user_input[CONF_URL]),
                )
                if not errors:
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
            description_placeholders=placeholders,
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
        placeholders = {"url": entry.data[CONF_URL]}

        if user_input is not None:
            errors, extra = await self._async_validate(
                entry.data[CONF_URL],
                user_input[CONF_API_TOKEN],
                entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
            )
            placeholders.update(extra)
            if errors.get("base") == "redirected":
                # This form has no URL field; the fix is Reconfigure.
                errors["base"] = "redirected_reauth"
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_TOKEN: user_input[CONF_API_TOKEN]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders=placeholders,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the URL, token or certificate verification of an entry.

        Keeps the same config entry, so entity ids and history survive; they
        are keyed on the entry id, which delete and re-add would change.
        """
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            try:
                base_url = normalize_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                if base_url != entry.unique_id:
                    # Refuse an instance another entry already monitors.
                    await self.async_set_unique_id(base_url)
                    self._abort_if_unique_id_configured()

                token = (user_input.get(CONF_API_TOKEN) or "").strip() or entry.data[
                    CONF_API_TOKEN
                ]
                errors, placeholders = await self._async_validate(
                    base_url,
                    token,
                    user_input[CONF_VERIFY_SSL],
                    https_assumed=not has_scheme(user_input[CONF_URL]),
                )
                if not errors:
                    # Follow the URL in the title unless the user renamed it.
                    title = (
                        base_url
                        if entry.title == entry.data.get(CONF_URL)
                        else entry.title
                    )
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=base_url,
                        title=title,
                        data_updates={
                            CONF_URL: base_url,
                            CONF_API_TOKEN: token,
                            CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
                        },
                    )

        suggested = user_input or {
            CONF_URL: entry.data.get(CONF_URL),
            CONF_VERIFY_SSL: entry.data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_RECONFIGURE_SCHEMA, suggested
            ),
            errors=errors,
            description_placeholders=placeholders,
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
