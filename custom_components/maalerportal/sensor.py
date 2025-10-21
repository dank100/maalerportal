"""Platform for Målerportal sensor integration."""

from datetime import datetime, timedelta, timezone
import logging
import re
from typing import Optional, cast

from smarthome_meterportal import (
    FullRequest,
    HomeAssistantApi,
    MeterReadingData,
    MeterReadingResponse,
    MeterReadingResponseData,
    MetersResponse,
    PartialRequest,
    exceptions
)

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    DOMAIN as RECORDER_DOMAIN,
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

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor platform."""

    meters: list[MetersResponse] = config.data["meters"]
    hassapi: HomeAssistantApi = hass.data[DOMAIN][config.entry_id]
    sensors = []

    for m in meters:
        if isinstance(m, dict):
            m = MetersResponse(**m)
        sensors.append(MaalerportalStatisticSensor(m, hassapi))

    async_add_entities(sensors)


class MaalerportalStatisticSensor(SensorEntity):
    """Handles water meter statistics."""

    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, meter: MetersResponse, api: HomeAssistantApi) -> None:
        """Set up the meter."""

        self._meter = meter
        self._attr_name = f"{self._meter.address}"
        self._attr_unique_id = f"{meter.identifier}-statistics"
        self._api = api
        if (
            meter.identifier is None
            or meter.address is None
            or meter.address_meter_id is None
        ):
            return
        self.entity_id = f"sensor.{to_snake_case(meter.identifier + meter.address + meter.address_meter_id)}"

    @Throttle(timedelta(minutes=15))
    async def async_update(self) -> None:
        """Continually update history."""
        lastest_statistic = await self._get_last_stat(self.hass)
        if lastest_statistic is not None:
            current_time_utc = datetime.now(timezone.utc)  # noqa: UP017
            statistic_start_time_utc = datetime.fromtimestamp(
                lastest_statistic["start"],
                tz=timezone.utc,  # noqa: UP017
            )

            if current_time_utc - statistic_start_time_utc < timedelta(hours=1):
                _LOGGER.debug(
                    "Skipping fetching new readings, latest at %s",
                    statistic_start_time_utc,
                )
                return
        _LOGGER.debug("Attempting to fetch data")
        await self._get_data(lastest_statistic)

    async def _get_last_stat(self, hass: HomeAssistant) -> Optional[StatisticsRow]:
        last_stats = await get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, self.entity_id, True, {"sum"}
        )

        if self.entity_id in last_stats and len(last_stats[self.entity_id]) > 0:
            result: StatisticsRow = last_stats[self.entity_id][0]
            return result

        return None

    async def _get_data(self, lastest_statistic: Optional[StatisticsRow]) -> None:
        """Get data from API."""
        statistics: list[StatisticData] = []
        response: MeterReadingResponse = None
        if lastest_statistic is None:
            request = FullRequest(address_meter_id=self._meter.address_meter_id)
            try:
                response = await self._api.api_homeassistant_full_post([request])
            except exceptions.ApiException as api_exception:
                _LOGGER.error(api_exception)
                return
        else:
            request = PartialRequest(
                address_meter_id=self._meter.address_meter_id,
                latestMeasurementTime=(lastest_statistic["start"] + 1),
            )
            try:
                response = await self._api.api_homeassistant_partial_post([request])
            except exceptions.ApiException as api_exception:
                _LOGGER.error(api_exception)
                return
        meter_readings = cast(
            list[MeterReadingResponseData], response.address_meter_readings
        )

        # Calculate the range of missing hours
        start_time = datetime.utcfromtimestamp(lastest_statistic["start"] + 1)
        now = datetime.utcnow()

        # Create a set of existing hours from the meter readings
        existing_hours = set()
        for reading in meter_readings:
            existing_hours.add(reading.time.hour)

        # Iterate through the missing hours and insert 0 readings
        missing_hours = []
        current_time = start_time
        while current_time <= now:
            if current_time.hour not in existing_hours:
                missing_hours.append({
                    "timestamp": current_time,
                    "value": 0
                })
            current_time += timedelta(hours=1)

        all_readings = meter_readings + missing_hours

        # Initialize a variable to keep track of the newest reading
        newest_reading: Optional[MeterReadingData] = None

        for am in all_readings:
            readings = cast(list[MeterReadingData], am.readings)
            readings.sort(
                key=lambda x: x.timestamp
                if x.timestamp is not None
                else datetime(1970, 1, 1)
            )
            for reading in readings:
                if reading.timestamp is None or reading.value is None or reading.timestamp == lastest_statistic["start"]:
                    continue
                statistics.append(
                    StatisticData(start=hour_ceil(reading.timestamp), sum=float(reading.value))
                )
                if (newest_reading is None) or (reading.timestamp > newest_reading.timestamp):
                    newest_reading = reading

        #newest value

        metadata = StatisticMetaData(
            name=self._attr_name,
            source=RECORDER_DOMAIN,
            statistic_id=self.entity_id,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            has_mean=False,
            has_sum=True,
        )

        if len(statistics) > 0:
            _LOGGER.debug("Adding %s readings for %s", len(statistics), self.entity_id)
            async_import_statistics(self.hass, metadata, statistics)

            if newest_reading is not None:
                self._attr_native_value = float(newest_reading.value)
                self.async_write_ha_state()
        else:
            _LOGGER.debug("No new readings found")


def to_snake_case(s: str) -> str:
    """Convert a string to snake_case."""

    s = re.sub("[^a-zA-Z0-9]", " ", s)
    s = re.sub("(.)([A-Z][a-z]+)", r"\1 \2", s)
    s = re.sub("([a-z0-9])([A-Z])", r"\1 \2", s)
    return s.lower().replace(" ", "_")

def hour_ceil(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0, minute=0, hour=t.hour+1)
