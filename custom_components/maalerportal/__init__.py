"""The Målerportal integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MaalerportalApiClient
from .const import DOMAIN, SERVICE_FORCE_RELOGIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Målerportal from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    session = async_get_clientsession(hass)
    client = MaalerportalApiClient(session)
    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "metrics": None,
        "last_sum": None,
        "unit": None,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if not hass.services.has_service(DOMAIN, SERVICE_FORCE_RELOGIN):
        hass.services.async_register(DOMAIN, SERVICE_FORCE_RELOGIN, _handle_force_relogin)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def _handle_force_relogin(call) -> None:
    """Force re-login for all Målerportal entries."""
    hass = call.hass
    entry_id_filter = call.data.get("entry_id")
    email_override = call.data.get("email")
    password_override = call.data.get("password")
    store_password = bool(call.data.get("store_password"))

    for entry_id, client in hass.data.get(DOMAIN, {}).items():
        if entry_id_filter and entry_id_filter != entry_id:
            continue
        entry = hass.config_entries.async_get_entry(entry_id)
        if not entry:
            continue
        client_obj = client["client"] if isinstance(client, dict) else client
        email = email_override or entry.data.get("email")
        password = password_override or entry.data.get("password")
        if not email or not password:
            continue
        try:
            login = await client_obj.login(email, password)
        except Exception:  # pylint: disable=broad-except
            continue
        access_token = (
            login.get("accessToken")
            or login.get("access_token")
            or login.get("token")
        )
        refresh_token = login.get("refreshToken") or login.get("refresh_token")
        if not access_token:
            continue
        new_data = {
            **entry.data,
            "access_token": access_token,
            "refresh_token": refresh_token,
        }
        if store_password or "password" in entry.data:
            new_data["email"] = email
            new_data["password"] = password
        hass.config_entries.async_update_entry(entry, data=new_data)
