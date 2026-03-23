"""Config flow for Målerportal integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiError, MaalerportalApiClient
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_CONSUMER_ID = "consumer_id"
CONF_METER_ID = "meter_id"
CONF_ADDRESS_SELECTION = "address_selection"

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def _extract_address_choices(payload: Any) -> dict[str, dict[str, str]]:
    """Return a mapping of selection key -> ids and label."""
    items: list[dict[str, Any]] = []
    if isinstance(payload, list):
        items = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            items = [item for item in data if isinstance(item, dict)]
        elif isinstance(payload.get("addresses"), list):
            items = [item for item in payload.get("addresses") if isinstance(item, dict)]

    choices: dict[str, dict[str, str]] = {}
    for item in items:
        consumer_id = (
            item.get("consumerId")
            or item.get("consumer_id")
            or item.get("customerId")
            or item.get("customer_id")
            or item.get("id")
            or item.get("globalAddressId")
        )
        address = item.get("address") or item.get("label") or item.get("name") or "Address"

        installations = item.get("installations")
        if isinstance(installations, list):
            for inst in installations:
                if not isinstance(inst, dict):
                    continue
                if not consumer_id:
                    consumer_id = inst.get("installationId") or inst.get("meterSerial")
                meter_candidates: list[dict[str, Any]] = []
                for key in ("meters", "meterList", "meter_list", "devices", "deviceList", "readings"):
                    value = inst.get(key)
                    if isinstance(value, list):
                        meter_candidates = [m for m in value if isinstance(m, dict)]
                        if meter_candidates:
                            break
                if meter_candidates:
                    for meter in meter_candidates:
                        meter_id = (
                            meter.get("meterId")
                            or meter.get("meter_id")
                            or meter.get("meterUuid")
                            or meter.get("meter_uuid")
                            or meter.get("id")
                        )
                        if not consumer_id or not meter_id:
                            continue
                        key = f"{consumer_id}|{meter_id}"
                        meter_label = meter.get("name") or meter.get("label") or meter.get("identifier") or str(meter_id)
                        choices[key] = {
                            "consumer_id": str(consumer_id),
                            "meter_id": str(meter_id),
                            "label": f"{address} - {meter_label}",
                        }
                    continue
                meter_id = (
                    inst.get("meterId")
                    or inst.get("meter_id")
                    or inst.get("meterUuid")
                    or inst.get("meter_uuid")
                    or inst.get("id")
                    or inst.get("installationId")
                    or inst.get("meterSerial")
                )
                if consumer_id and meter_id:
                    label = inst.get("installationType") or inst.get("nickname") or str(meter_id)
                    key = f"{consumer_id}|{meter_id}"
                    choices[key] = {
                        "consumer_id": str(consumer_id),
                        "meter_id": str(meter_id),
                        "label": f"{address} - {label}",
                    }
            if choices:
                continue

        meter_candidates: list[dict[str, Any]] = []
        for key in ("meters", "meterList", "meter_list", "devices", "deviceList"):
            value = item.get(key)
            if isinstance(value, list):
                meter_candidates = [m for m in value if isinstance(m, dict)]
                if meter_candidates:
                    break

        if meter_candidates:
            for meter in meter_candidates:
                meter_id = (
                    meter.get("meterId")
                    or meter.get("meter_id")
                    or meter.get("meterUuid")
                    or meter.get("meter_uuid")
                    or meter.get("id")
                )
                if not consumer_id or not meter_id:
                    continue
                key = f"{consumer_id}|{meter_id}"
                meter_label = meter.get("name") or meter.get("label") or meter.get("identifier") or str(meter_id)
                choices[key] = {
                    "consumer_id": str(consumer_id),
                    "meter_id": str(meter_id),
                    "label": f"{address} - {meter_label}",
                }
            continue

        meter_id = (
            item.get("meterId")
            or item.get("meter_id")
            or item.get("meterUuid")
            or item.get("meter_uuid")
        )
        if not consumer_id or not meter_id:
            continue
        key = f"{consumer_id}|{meter_id}"
        choices[key] = {
            "consumer_id": str(consumer_id),
            "meter_id": str(meter_id),
            "label": f"{address} ({meter_id})",
        }
        return choices


def _summarize_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        keys = list(payload.keys())
        return f"dict keys={keys}"
    if isinstance(payload, list):
        return f"list len={len(payload)}"
    return f"type={type(payload).__name__}"


def _summarize_first_item(payload: Any) -> str:
    if not isinstance(payload, list) or not payload:
        return "no items"
    first = payload[0]
    if not isinstance(first, dict):
        return f"first type={type(first).__name__}"
    keys = list(first.keys())
    list_keys = {k: len(v) for k, v in first.items() if isinstance(v, list)}
    return f"first keys={keys} list_keys={list_keys}"


def _summarize_first_installation(payload: Any) -> str:
    if not isinstance(payload, list) or not payload:
        return "no items"
    first = payload[0]
    if not isinstance(first, dict):
        return "no dict item"
    installations = first.get("installations")
    if not isinstance(installations, list) or not installations:
        return "no installations"
    inst = installations[0]
    if not isinstance(inst, dict):
        return f"installation type={type(inst).__name__}"
    keys = list(inst.keys())
    list_keys = {k: len(v) for k, v in inst.items() if isinstance(v, list)}
    return f"installation keys={keys} list_keys={list_keys}"


def _summarize_installation_details(payload: Any) -> str:
    if not isinstance(payload, list) or not payload:
        return "no items"
    first = payload[0]
    if not isinstance(first, dict):
        return "no dict item"
    installations = first.get("installations")
    if not isinstance(installations, list) or not installations:
        return "no installations"
    inst = installations[0]
    if not isinstance(inst, dict):
        return "installation not dict"
    return (
        f"installationId={inst.get('installationId')} "
        f"meterSerial={inst.get('meterSerial')} "
        f"utilityId={inst.get('utilityId')}"
    )


def _fallback_single_choice(payload: Any) -> dict[str, dict[str, str]]:
    if not isinstance(payload, list) or len(payload) != 1:
        return {}
    item = payload[0]
    if not isinstance(item, dict):
        return {}
    consumer_id = item.get("globalAddressId")
    address = item.get("address") or "Address"
    installations = item.get("installations")
    if not consumer_id or not isinstance(installations, list) or not installations:
        return {}
    inst = installations[0]
    if not isinstance(inst, dict):
        return {}
    meter_id = inst.get("installationId")
    label = inst.get("installationType") or meter_id
    if not meter_id:
        return {}
    key = f"{consumer_id}|{meter_id}"
    return {
        key: {
            "consumer_id": str(consumer_id),
            "meter_id": str(meter_id),
            "label": f"{address} - {label}",
        }
    }


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect."""
    client = MaalerportalApiClient(async_get_clientsession(hass))
    try:
        login = await client.login(data[CONF_EMAIL], data[CONF_PASSWORD])
    except ApiError as err:
        if isinstance(err.data, dict):
            message = err.data.get("message")
            path = err.data.get("path")
        else:
            message = None
            path = None
        _LOGGER.error(
            "Login failed with status %s; message=%s; path=%s; response=%s",
            err.status,
            message,
            path,
            _summarize_payload(err.data),
        )
        if err.status in (401, 403):
            raise InvalidAuth from err
        raise CannotConnect from err

    access_token = (
        login.get("accessToken")
        or login.get("access_token")
        or login.get("token")
    )
    refresh_token = login.get("refreshToken") or login.get("refresh_token")
    if not access_token:
        _LOGGER.error("Login response missing access token. %s", _summarize_payload(login))
        raise CannotConnect

    try:
        addresses = await client.get_addresses(access_token)
    except ApiError as err:
        _LOGGER.error("Addresses request failed with status %s", err.status)
        if err.status in (401, 403):
            raise InvalidAuth from err
        raise CannotConnect from err
    _LOGGER.info("Addresses response: %s", _summarize_payload(addresses))

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "addresses": addresses,
    }


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
                self.context.update(
                    {
                        "auth": info,
                        CONF_EMAIL: user_input[CONF_EMAIL],
                        "password": user_input[CONF_PASSWORD],
                    }
                )
                return await self.async_step_address_selection()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_address_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        auth = self.context.get("auth", {})
        addresses = auth.get("addresses")
        choices = _extract_address_choices(addresses)
        if not choices:
            choices = _fallback_single_choice(addresses)
        if choices and len(choices) == 1 and user_input is None:
            choice = next(iter(choices.values()))
            save_data: dict[str, Any] = {
                CONF_EMAIL: self.context.get(CONF_EMAIL),
                CONF_PASSWORD: self.context.get("password"),
                CONF_CONSUMER_ID: choice["consumer_id"],
                CONF_METER_ID: choice["meter_id"],
                "access_token": auth.get("access_token"),
                "refresh_token": auth.get("refresh_token"),
            }
            return self.async_create_entry(title="Målerportal", data=save_data)
        if not choices:
            _LOGGER.error(
                "No address choices extracted. %s; %s",
                _summarize_payload(auth.get("addresses")),
                _summarize_first_item(auth.get("addresses")),
            )
            _LOGGER.error(
                "Address install summary: %s",
                _summarize_first_installation(auth.get("addresses")),
            )
            _LOGGER.error(
                "Address install details: %s",
                _summarize_installation_details(auth.get("addresses")),
            )
            errors["base"] = "cannot_connect"
            return self.async_show_form(
                step_id="address_selection",
                data_schema=vol.Schema(
                    {vol.Optional(CONF_ADDRESS_SELECTION): cv.multi_select({})}
                ),
                errors=errors,
            )

        if user_input is not None:
            selected = user_input.get(CONF_ADDRESS_SELECTION)
            if not selected:
                errors["base"] = "unknown"
            else:
                choice = choices.get(selected)
                if not choice:
                    errors["base"] = "unknown"
                else:
                    save_data: dict[str, Any] = {
                        CONF_EMAIL: self.context.get(CONF_EMAIL),
                        CONF_CONSUMER_ID: choice["consumer_id"],
                        CONF_METER_ID: choice["meter_id"],
                        "access_token": auth.get("access_token"),
                        "refresh_token": auth.get("refresh_token"),
                    }
                    return self.async_create_entry(title="Målerportal", data=save_data)

        labels = {key: value["label"] for key, value in choices.items()}
        return self.async_show_form(
            step_id="address_selection",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS_SELECTION): vol.In(labels)}
            ),
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
