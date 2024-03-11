"""Config flow for Målerportal integration."""
from __future__ import annotations

import logging
from typing import Any

import mpsmarthome
from mpsmarthome import Configuration, MetersResponse
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, APIURL

_CONFIG = Configuration(host=APIURL)
_LOGGER = logging.getLogger(__name__)
# adjust the data schema to the data that you need
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> str:
    """Validate the user input allows us to connect.

    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    request = mpsmarthome.AuthRequest(
        email_address=data[CONF_EMAIL], password=data[CONF_PASSWORD]
    )
    try:
        async with mpsmarthome.ApiClient(_CONFIG) as api_client:
            hassapi = mpsmarthome.HomeAssistantApi(api_client)
            result = await hassapi.api_homeassistant_authenticate_post(request)
            if result.api_key is None:
                raise InvalidAuth("API key is missing")
            return result.api_key
    except mpsmarthome.exceptions.NotFoundException as err:
        raise InvalidAuth from err


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Målerportal."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                self.context.update({"apikey": info})
                return await self.async_step_entity_selection()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_entity_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle selection of meters step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            meters: list[MetersResponse] = self.context["meterdata"]
            save_data: dict[str, Any] = {}
            save_data["meters"] = []
            for k in user_input["entity_selection"]:
                for meter in meters:
                    if meter.address_meter_id == k:
                        save_data["meters"].append(
                            {
                                "address_meter_id": meter.address_meter_id,
                                "address": meter.address,
                                "identifier": meter.identifier,
                                "manufacturer": meter.manufacturer,
                                "model": meter.model,
                                "access_from": meter.access_from.isoformat()
                                if meter.access_from
                                else None,
                                "access_to": meter.access_to.isoformat()
                                if meter.access_to
                                else None,
                                "meter_counter_type": meter.meter_counter_type,
                            }
                        )
                        break
            save_data["api_key"] = self.context["apikey"]
            return self.async_create_entry(title="Målere", data=save_data)

        entities_with_labels: dict[str, str] = {}
        async with mpsmarthome.ApiClient(_CONFIG) as api_client:
            api_client.default_headers["X-API-KEY"] = self.context["apikey"]
            hassapi = mpsmarthome.HomeAssistantApi(api_client)
            result = await hassapi.api_homeassistant_meters_get()
            for m in result:
                entities_with_labels[str(m.address_meter_id)] = (
                    "(" + str(m.identifier) + ") " + str(m.address)
                )
            self.context["meterdata"] = result
        return self.async_show_form(
            step_id="entity_selection",
            data_schema=vol.Schema(
                {
                    vol.Optional("entity_selection"): cv.multi_select(
                        entities_with_labels
                    )
                },
                extra=vol.ALLOW_EXTRA,
            ),
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
