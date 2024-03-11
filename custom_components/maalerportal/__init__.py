"""The Målerportal integration."""
from __future__ import annotations

import logging

from mpsmarthome import ApiClient, Configuration, HomeAssistantApi

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# List the platforms that you want to support.
# For your initial PR, limit it to 1 platform.
PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Målerportal from a config entry."""

    hass.data.setdefault(DOMAIN, {})
    api_key: str = entry.data["api_key"]
    api_config = Configuration(host="http://gateway-smarthome:8080")
    api_client = ApiClient(api_config)
    api_client.default_headers["X-API-KEY"] = api_key
    hassapi = HomeAssistantApi(api_client)
    hass.data[DOMAIN][entry.entry_id] = hassapi
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
