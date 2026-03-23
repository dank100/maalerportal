"""API client for Målerportal (new gateway endpoints)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

from .const import API_BASE, LOGIN_PATH, REFRESH_PATH

_LOGGER = logging.getLogger(__name__)


class MaalerportalApiClient:
    """Thin wrapper around the new Målerportal gateway API."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def login(self, email: str, password: str) -> dict[str, Any]:
        url = f"{API_BASE}{LOGIN_PATH}"
        payloads = [
            {"emailAddress": email, "password": password, "platform": "consumer"},
            {"email": email, "password": password, "platform": "consumer"},
            {"email_address": email, "password": password, "platform": "consumer"},
            {"username": email, "password": password, "platform": "consumer"},
            {"emailAddress": email, "password": password, "platform": "homeassistant"},
            {"email": email, "password": password, "platform": "homeassistant"},
            {"email_address": email, "password": password, "platform": "homeassistant"},
            {"username": email, "password": password, "platform": "homeassistant"},
        ]
        last_error: ApiError | None = None
        for payload in payloads:
            async with self._session.post(url, json=payload, ssl=False) as resp:
                data = await resp.json(content_type=None)
                if resp.status < 400:
                    return data
                err = ApiError(resp.status, data)
                last_error = err
                if resp.status in (401, 403):
                    raise err
                if resp.status in (400, 422):
                    continue
                raise err
        if last_error:
            raise last_error
        raise ApiError(400, {"error": "Login failed"})

    async def refresh(self, access_token: str, refresh_token: str) -> dict[str, Any]:
        url = f"{API_BASE}{REFRESH_PATH}"
        base_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "X-Skip-Auth-Refresh": "true",
            "Origin": "https://consumer.meterportal.eu",
            "Referer": "https://consumer.meterportal.eu/",
            "User-Agent": "HomeAssistant-Maalerportal",
        }
        payload = {
            "platform": "consumer",
            "token": access_token,
            "refreshToken": refresh_token,
        }
        async with self._session.post(url, json=payload, headers=base_headers, ssl=False) as resp:
            data = await resp.json(content_type=None)
            if resp.status < 400:
                return data
            raise ApiError(resp.status, data)

    async def get_metrics(self, consumer_id: str, meter_id: str, access_token: str) -> Any:
        url = f"{API_BASE}/v1/consumer/metrics/{consumer_id}/{meter_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "User-Agent": "HomeAssistant-Maalerportal",
        }
        async with self._session.get(url, headers=headers, ssl=False) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise ApiError(resp.status, data)
            return data

    async def get_addresses(self, access_token: str) -> Any:
        url = f"{API_BASE}/v1/consumer/addresses"
        headers = {"Authorization": f"Bearer {access_token}"}
        async with self._session.get(url, headers=headers, ssl=False) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise ApiError(resp.status, data)
            return data


class ApiError(Exception):
    def __init__(self, status: int, data: Any) -> None:
        super().__init__(f"API error {status}")
        self.status = status
        self.data = data
