"""Platform for Målerportal sensor integration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Optional

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    DOMAIN as RECORDER_DOMAIN,
    StatisticMeanType,
    StatisticsRow,
    async_import_statistics,
    get_last_statistics,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import Throttle
from homeassistant.util import dt as dt_util

from .api import ApiError, MaalerportalApiClient
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor platform."""
    store: dict[str, Any] = hass.data[DOMAIN][config.entry_id]
    client: MaalerportalApiClient = store["client"]
    metric_sensor = MaalerportalMetricSensor(
        client=client,
        hass=hass,
        entry=config,
        store=store,
        consumer_id=config.data["consumer_id"],
        meter_id=config.data["meter_id"],
        access_token=config.data["access_token"],
        refresh_token=config.data.get("refresh_token"),
        email=config.data.get("email"),
        password=config.data.get("password"),
    )
    total_sensor = MaalerportalTotalSensor(
        client=client,
        hass=hass,
        entry=config,
        store=store,
        consumer_id=config.data["consumer_id"],
        meter_id=config.data["meter_id"],
        access_token=config.data["access_token"],
        refresh_token=config.data.get("refresh_token"),
        email=config.data.get("email"),
        password=config.data.get("password"),
    )
    async_add_entities([metric_sensor, total_sensor])


class MaalerportalMetricSensor(SensorEntity):
    """Handles meter metrics."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        client: MaalerportalApiClient,
        hass: HomeAssistant,
        entry: ConfigEntry,
        store: dict[str, Any],
        consumer_id: str,
        meter_id: str,
        access_token: str,
        refresh_token: Optional[str],
        email: Optional[str],
        password: Optional[str],
    ) -> None:
        self._client = client
        self._hass = hass
        self._entry = entry
        self._store = store
        self._consumer_id = consumer_id
        self._meter_id = meter_id
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._email = email
        self._password = password
        self._attr_name = "Målerportal Meter"
        self._attr_unique_id = f"{consumer_id}_{meter_id}_metric"

    @Throttle(timedelta(minutes=15))
    async def async_update(self) -> None:
        try:
            data = await self._client.get_metrics(
                self._consumer_id, self._meter_id, self._access_token
            )
        except ApiError as err:
            if err.status in (401, 403) and self._refresh_token:
                if await self._refresh_access_token():
                    data = await self._client.get_metrics(
                        self._consumer_id, self._meter_id, self._access_token
                    )
                else:
                    _LOGGER.error("Failed to refresh token: %s", err.data)
                    return
            else:
                _LOGGER.error("Failed to fetch metrics: %s", err.data)
                return

        value, attrs, unit = _extract_metric(data)
        self._attr_native_value = value
        if unit:
            self._attr_native_unit_of_measurement = unit
        self._attr_extra_state_attributes = attrs
        self._store["metrics"] = data
        self._store["unit"] = unit
        # Always compute a running sum so the total sensor has a value,
        # even when no new statistics are imported.
        computed_sum = _calculate_running_sum(data)
        if computed_sum is not None:
            self._store["last_sum"] = computed_sum
        latest_sum = await self._import_hourly_statistics(data, unit)
        if latest_sum is not None:
            self._store["last_sum"] = latest_sum

    async def _refresh_access_token(self) -> bool:
        try:
            refresh = await self._client.refresh(self._access_token, self._refresh_token)
        except ApiError as err:
            _LOGGER.error("Refresh token failed: %s", err.data)
            return await self._relogin()
        access_token = (
            refresh.get("accessToken")
            or refresh.get("access_token")
            or refresh.get("token")
        )
        if not access_token:
            return await self._relogin()
        self._access_token = access_token
        new_refresh = refresh.get("refreshToken") or refresh.get("refresh_token")
        if new_refresh:
            self._refresh_token = new_refresh
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
            },
        )
        return True

    async def _import_hourly_statistics(
        self, payload: Any, unit: Optional[str]
    ) -> Optional[float]:
        if not isinstance(payload, dict):
            return None
        hourly = payload.get("hourly")
        if not isinstance(hourly, list) or not hourly:
            return None
        statistic_id = self.entity_id
        last = await self._get_last_statistic(statistic_id)
        last_start = last["start"] if last else None
        last_sum = last.get("sum") if last else None
        if last_sum is None:
            last_sum = self._store.get("last_sum") or 0.0
        stats: list[StatisticData] = []
        running_sum = float(last_sum)
        items: list[dict[str, Any]] = [
            i for i in hourly if isinstance(i, dict)
        ]
        items.sort(key=_hourly_sort_key)
        for item in items:
            try:
                year = int(item.get("year"))
                month = int(item.get("month"))
                day = int(item.get("day"))
                hour = int(item.get("hour"))
            except (TypeError, ValueError):
                continue
            try:
                consumption = float(item.get("consumption"))
            except (TypeError, ValueError):
                continue
            if consumption < 0:
                _LOGGER.warning("Skipping negative consumption value: %s", consumption)
                continue
            local_dt = datetime(year, month, day, hour, 0, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE)
            start = dt_util.as_utc(local_dt)
            if last_start and start.timestamp() <= last_start:
                continue
            running_sum += consumption
            stats.append(StatisticData(start=start, mean=consumption, sum=running_sum))

        if not stats:
            return None

        metadata = StatisticMetaData(
            name=self._attr_name,
            source=RECORDER_DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement=unit or UnitOfVolume.LITERS,
            has_mean=True,
            has_sum=True,
            mean_type=StatisticMeanType.ARITHMETIC,
        )
        async_import_statistics(self._hass, metadata, stats)
        return stats[-1]["sum"]

    async def _get_last_statistic(self, statistic_id: str) -> Optional[StatisticsRow]:
        last_stats = await get_instance(self._hass).async_add_executor_job(
            get_last_statistics, self._hass, 1, statistic_id, True, {"sum"}
        )
        if statistic_id in last_stats and last_stats[statistic_id]:
            return last_stats[statistic_id][0]
        return None


class MaalerportalTotalSensor(SensorEntity):
    """Total consumption sensor for Energy dashboard."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_device_class = SensorDeviceClass.WATER

    def __init__(
        self,
        client: MaalerportalApiClient,
        hass: HomeAssistant,
        entry: ConfigEntry,
        store: dict[str, Any],
        consumer_id: str,
        meter_id: str,
        access_token: str,
        refresh_token: Optional[str],
        email: Optional[str],
        password: Optional[str],
    ) -> None:
        self._client = client
        self._hass = hass
        self._entry = entry
        self._store = store
        self._attr_name = "Målerportal Total Consumption"
        self._attr_unique_id = f"{consumer_id}_{meter_id}_total"
        self._attr_native_unit_of_measurement = UnitOfVolume.LITERS
        self._consumer_id = consumer_id
        self._meter_id = meter_id
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._email = email
        self._password = password

    @Throttle(timedelta(minutes=15))
    async def async_update(self) -> None:
        unit = self._store.get("unit")
        last_sum = self._store.get("last_sum")
        metrics = self._store.get("metrics")
        if last_sum is None:
            if isinstance(metrics, dict):
                last_sum = _calculate_running_sum(metrics)
        if last_sum is None:
            metrics = await self._refresh_metrics()
            if isinstance(metrics, dict):
                self._store["metrics"] = metrics
                unit = unit or _map_unit(metrics.get("hourlyUnit"))
                last_sum = _calculate_running_sum(metrics)
        if last_sum is not None:
            self._attr_native_value = last_sum
        if unit:
            self._attr_native_unit_of_measurement = unit

    async def _refresh_metrics(self) -> Optional[dict[str, Any]]:
        try:
            return await self._client.get_metrics(
                self._consumer_id, self._meter_id, self._access_token
            )
        except ApiError as err:
            if err.status in (401, 403) and self._refresh_token:
                if await self._refresh_access_token():
                    return await self._client.get_metrics(
                        self._consumer_id, self._meter_id, self._access_token
                    )
            _LOGGER.error("Failed to fetch metrics for total sensor: %s", err.data)
            return None

    async def _refresh_access_token(self) -> bool:
        if not self._refresh_token:
            return await self._relogin()
        try:
            refresh = await self._client.refresh(self._access_token, self._refresh_token)
        except ApiError as err:
            _LOGGER.error("Refresh token failed: %s", err.data)
            return await self._relogin()
        access_token = (
            refresh.get("accessToken")
            or refresh.get("access_token")
            or refresh.get("token")
        )
        if not access_token:
            return await self._relogin()
        self._access_token = access_token
        new_refresh = refresh.get("refreshToken") or refresh.get("refresh_token")
        if new_refresh:
            self._refresh_token = new_refresh
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
            },
        )
        return True

    async def _relogin(self) -> bool:
        if not self._email or not self._password:
            return False
        try:
            login = await self._client.login(self._email, self._password)
        except ApiError as err:
            _LOGGER.error("Re-login failed: %s", err.data)
            return False
        access_token = (
            login.get("accessToken")
            or login.get("access_token")
            or login.get("token")
        )
        refresh_token = login.get("refreshToken") or login.get("refresh_token")
        if not access_token:
            return False
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "email": self._email,
                "password": self._password,
            },
        )
        return True

    async def _relogin(self) -> bool:
        if not self._email or not self._password:
            return False
        try:
            login = await self._client.login(self._email, self._password)
        except ApiError as err:
            _LOGGER.error("Re-login failed: %s", err.data)
            return False
        access_token = (
            login.get("accessToken")
            or login.get("access_token")
            or login.get("token")
        )
        refresh_token = login.get("refreshToken") or login.get("refresh_token")
        if not access_token:
            return False
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._hass.config_entries.async_update_entry(
            self._entry,
            data={
                **self._entry.data,
                "access_token": self._access_token,
                "refresh_token": self._refresh_token,
                "email": self._email,
                "password": self._password,
            },
        )
        return True


def _extract_metric(payload: Any) -> tuple[Optional[float], dict[str, Any], Optional[str]]:
    """Extract a numeric value and unit from metrics payload."""
    attrs: dict[str, Any] = {}
    unit = None
    if isinstance(payload, dict):
        attrs.update(
            {
                "hourlyUnit": payload.get("hourlyUnit"),
                "dailyUnit": payload.get("dailyUnit"),
                "monthlyUnit": payload.get("monthlyUnit"),
                "yearlyUnit": payload.get("yearlyUnit"),
            }
        )

        for key, unit_key in (
            ("hourly", "hourlyUnit"),
            ("daily", "dailyUnit"),
            ("monthly", "monthlyUnit"),
            ("yearly", "yearlyUnit"),
        ):
            series = payload.get(key)
            if isinstance(series, list) and series:
                value = _latest_consumption(series)
                if value is not None:
                    unit = _map_unit(payload.get(unit_key))
                    return value, attrs, unit

    if isinstance(payload, list):
        value = _latest_consumption(payload)
        return value, attrs, unit

    return None, attrs, unit


def _latest_consumption(series: list[dict[str, Any]]) -> Optional[float]:
    """Pick the most recent entry and return consumption."""
    items = [i for i in series if isinstance(i, dict)]
    if not items:
        return None
    latest = sorted(items, key=_hourly_sort_key)[-1]
    for key in ("consumption", "value", "reading", "sum", "volume", "total"):
        if key in latest:
            try:
                return float(latest[key])
            except (TypeError, ValueError):
                return None
    return None


def _map_unit(unit_value: Any) -> Optional[str]:
    if not isinstance(unit_value, str):
        return None
    normalized = unit_value.lower()
    if normalized in ("liter", "liters", "l"):
        return UnitOfVolume.LITERS
    if normalized in ("cubicmeter", "cubicmeters", "m3", "m³"):
        return UnitOfVolume.CUBIC_METERS
    return unit_value


def _hourly_sort_key(item: dict[str, Any]) -> tuple:
    return (
        int(item.get("year", 0) or 0),
        int(item.get("month", 0) or 0),
        int(item.get("day", 0) or 0),
        int(item.get("hour", -1) if item.get("hour") is not None else -1),
    )


def _calculate_running_sum(payload: dict[str, Any]) -> Optional[float]:
    hourly = payload.get("hourly")
    if not isinstance(hourly, list) or not hourly:
        return None
    items = [i for i in hourly if isinstance(i, dict)]
    if not items:
        return None
    total = 0.0
    for item in sorted(items, key=_hourly_sort_key):
        try:
            consumption = float(item.get("consumption"))
        except (TypeError, ValueError):
            continue
        if consumption < 0:
            continue
        total += consumption
    return total
