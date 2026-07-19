"""Runtime coordinators for the SP Group integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from spgroup.mobile import SpGroupMobileError

from .auth import async_refresh_runtime_session_if_needed
from .const import DEFAULT_UPDATE_TIMEOUT_SECONDS, DOMAIN
from .discovery import DiscoveryDataError, SpGroupCoordinatorData
from .discovery_service import async_fetch_discovery_data
from .ledger import LedgerError, SpGroupBucketObservation, closed_month_ready
from .polling_options import polling_options
from .sources import (
    SINGAPORE_TZ,
    SOURCE_MANUAL_SEED_AMI,
    SOURCE_MANUAL_SEED_MONTHLY,
    SpGroupSourceConfig,
    source_configs_for_data,
)

if TYPE_CHECKING:
    from . import SpGroupConfigEntry

LOGGER = logging.getLogger(__name__)

_DISCOVERY_UPDATE_FAILED = "Error refreshing SP Group discovery data"
_LEDGER_UPDATE_FAILED = "Error refreshing SP Group accumulation ledger"
_UTILITY_UPDATE_FAILED = "Error refreshing SP Group utility evidence"
_TIMEOUT_UPDATE_FAILED = "Timed out refreshing SP Group data"


class SpGroupDiscoveryCoordinator(DataUpdateCoordinator[SpGroupCoordinatorData]):
    """Coordinate one-shot SP Group account discovery."""

    def __init__(self, hass: HomeAssistant, entry: SpGroupConfigEntry) -> None:
        """Initialize the discovery coordinator."""
        super().__init__(
            hass,
            logger=LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}_discovery",
            update_interval=None,
        )
        self.entry = entry

    async def _async_update_data(self) -> SpGroupCoordinatorData:
        """Fetch discovery data from SP Group APIs."""
        try:
            async with asyncio.timeout(DEFAULT_UPDATE_TIMEOUT_SECONDS):
                session = await async_refresh_runtime_session_if_needed(
                    self.hass, self.entry
                )
                LOGGER.debug("SP Group auth refresh phase completed")
                runtime = self.entry.runtime_data
                data = await async_fetch_discovery_data(
                    session=session,
                    yggdrasil_client=runtime.yggdrasil_client,
                    jarvis_client=runtime.jarvis_client,
                    account_id=runtime.account_id,
                    require_jarvis_correlation=True,
                )
        except ConfigEntryAuthFailed:
            raise
        except TimeoutError as err:
            raise UpdateFailed(_TIMEOUT_UPDATE_FAILED) from err
        except DiscoveryDataError as err:
            raise UpdateFailed(_DISCOVERY_UPDATE_FAILED) from err
        except SpGroupMobileError as err:
            raise UpdateFailed(_DISCOVERY_UPDATE_FAILED) from err

        LOGGER.debug(
            "SP Group discovery phase completed: account_count=%s",
            len(data.accounts),
        )
        LOGGER.debug(
            "SP Group normalization phase completed: premise_count=%s "
            "account_count=%s future_sensor_count=%s partial_errors=%s",
            len(data.premises),
            len(data.accounts),
            len(data.future_sensor_keys()),
            data.has_partial_errors,
        )
        return data


class SpGroupUtilityCoordinator(DataUpdateCoordinator[None]):
    """Base utility coordinator that updates the accumulation ledger."""

    source: str
    utility_type: str
    grouped_by: str
    api_utility_type: str
    require_two_sightings: bool

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SpGroupConfigEntry,
        *,
        name: str,
        update_interval: timedelta | None,
    ) -> None:
        """Initialize the utility coordinator."""
        super().__init__(
            hass,
            logger=LOGGER,
            config_entry=entry,
            name=name,
            update_interval=update_interval,
        )
        self.entry = entry

    async def _async_update_data(self) -> None:
        """Refresh this utility's ledger-backed source."""
        try:
            async with asyncio.timeout(DEFAULT_UPDATE_TIMEOUT_SECONDS):
                await self._async_poll_sources(backfill=False)
        except ConfigEntryAuthFailed:
            raise
        except TimeoutError as err:
            raise UpdateFailed(_TIMEOUT_UPDATE_FAILED) from err
        except (LedgerError, OSError) as err:
            raise UpdateFailed(_LEDGER_UPDATE_FAILED) from err
        except SpGroupMobileError as err:
            raise UpdateFailed(_UTILITY_UPDATE_FAILED) from err
        return None

    async def async_poll_now(self, *, backfill: bool = False) -> None:
        """Run one immediate utility poll and notify listeners.

        This intentionally bypasses the coordinator request-refresh debouncer for
        controlled backfill paths that need consecutive sightings.
        """
        try:
            async with asyncio.timeout(DEFAULT_UPDATE_TIMEOUT_SECONDS):
                await self._async_poll_sources(backfill=backfill)
        except ConfigEntryAuthFailed:
            raise
        except TimeoutError as err:
            raise UpdateFailed(_TIMEOUT_UPDATE_FAILED) from err
        except (LedgerError, OSError) as err:
            raise UpdateFailed(_LEDGER_UPDATE_FAILED) from err
        except SpGroupMobileError as err:
            raise UpdateFailed(_UTILITY_UPDATE_FAILED) from err
        self.async_set_updated_data(None)

    async def _async_poll_sources(self, *, backfill: bool) -> int:
        """Poll enabled source configs for this utility and return request count."""
        runtime = self.entry.runtime_data
        if runtime.ledger_store is None:
            return 0
        data = _discovery_data(self.entry)
        if data is None:
            return 0
        source_configs = _source_configs_for_utility(
            data,
            self.entry.options,
            source=self.source,
            utility_type=self.utility_type,
        )
        await runtime.ledger_store.async_initialize_sources(source_configs)
        if not source_configs:
            return 0

        session = await async_refresh_runtime_session_if_needed(self.hass, self.entry)
        premise_by_key = {premise.key: premise for premise in data.premises}
        now = datetime.now(SINGAPORE_TZ)
        request_count = 0
        for sensor_key, config in source_configs.items():
            premise = premise_by_key.get(config.premise_key)
            if premise is None or premise.jarvis_premise_id is None:
                continue
            request_start = (
                config.backfill_from if backfill else config.seed_effective_from
            )
            payload = await runtime.jarvis_client.get_ami_charts(
                session,
                premise.jarvis_premise_id,
                _date_for_api(request_start),
                _date_for_api(now),
                self.grouped_by,
                self.api_utility_type,
            )
            request_count += 1
            observations = self._observations(
                payload,
                backfill_from=request_start,
                now=now,
            )
            await runtime.ledger_store.async_apply_observations(
                sensor_key,
                observations,
                now=now,
                require_two_sightings=self.require_two_sightings,
            )
        LOGGER.debug(
            "SP Group %s ledger source phase completed: request_count=%s "
            "source_count=%s",
            self.utility_type,
            request_count,
            len(source_configs),
        )
        return request_count

    def _observations(
        self,
        payload: dict[str, Any],
        *,
        backfill_from: datetime,
        now: datetime,
    ) -> tuple[SpGroupBucketObservation, ...]:
        raise NotImplementedError


class SpGroupElectricityCoordinator(SpGroupUtilityCoordinator):
    """Coordinate electricity AMI ledger polling."""

    source = SOURCE_MANUAL_SEED_AMI
    utility_type = "electric"
    grouped_by = "day"
    api_utility_type = "electric"
    require_two_sightings = True

    def __init__(self, hass: HomeAssistant, entry: SpGroupConfigEntry) -> None:
        """Initialize the electricity coordinator."""
        options = polling_options(entry.options)
        super().__init__(
            hass,
            entry,
            name=f"{DOMAIN}_electricity",
            update_interval=timedelta(
                hours=options.electricity_poll_interval_hours
            ),
        )

    def _observations(
        self,
        payload: dict[str, Any],
        *,
        backfill_from: datetime,
        now: datetime,
    ) -> tuple[SpGroupBucketObservation, ...]:
        return _electricity_ami_observations(
            payload,
            backfill_from=backfill_from,
            now=now,
        )


class SpGroupWaterCoordinator(SpGroupUtilityCoordinator):
    """Coordinate water monthly history-chart ledger polling."""

    source = SOURCE_MANUAL_SEED_MONTHLY
    utility_type = "water"
    require_two_sightings = False

    def __init__(self, hass: HomeAssistant, entry: SpGroupConfigEntry) -> None:
        """Initialize the water coordinator."""
        super().__init__(
            hass,
            entry,
            name=f"{DOMAIN}_water",
            update_interval=None,
        )
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._last_accepted_count = 0

    @callback
    def async_start_schedule(self) -> None:
        """Start the water monthly schedule."""
        self.async_cancel_schedule()
        self._schedule_next(datetime.now(SINGAPORE_TZ))

    @callback
    def async_cancel_schedule(self) -> None:
        """Cancel scheduled water polling."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None

    def _schedule_next(self, now: datetime) -> None:
        next_run = next_water_run(now, self.entry.options)
        self._unsub_timer = async_track_point_in_time(
            self.hass,
            self._async_scheduled_refresh,
            next_run,
        )

    async def _async_poll_sources(self, *, backfill: bool) -> int:
        """Poll water sources via GET /jarvis/v4/charts/{premise_id}.

        Water is not a smart meter so AMI endpoints return invalid kWh data.
        This override delegates to the v4 history chart endpoint instead.
        """
        runtime = self.entry.runtime_data
        if runtime.ledger_store is None:
            return 0
        data = _discovery_data(self.entry)
        if data is None:
            return 0
        source_configs = _source_configs_for_utility(
            data,
            self.entry.options,
            source=self.source,
            utility_type=self.utility_type,
        )
        await runtime.ledger_store.async_initialize_sources(source_configs)
        if not source_configs:
            return 0

        session = await async_refresh_runtime_session_if_needed(self.hass, self.entry)
        premise_by_key = {premise.key: premise for premise in data.premises}
        now = datetime.now(SINGAPORE_TZ)
        request_count = 0
        for sensor_key, config in source_configs.items():
            premise = premise_by_key.get(config.premise_key)
            if premise is None or premise.jarvis_premise_id is None:
                continue
            request_start = (
                config.backfill_from if backfill else config.seed_effective_from
            )
            payload = await runtime.jarvis_client.get_history_charts(
                session,
                premise.jarvis_premise_id,
            )
            request_count += 1
            observations = _water_history_observations(
                payload,
                backfill_from=request_start,
                now=now,
            )
            await runtime.ledger_store.async_apply_observations(
                sensor_key,
                observations,
                now=now,
                require_two_sightings=self.require_two_sightings,
            )
        LOGGER.debug(
            "SP Group %s ledger source phase completed: request_count=%s "
            "source_count=%s",
            self.utility_type,
            request_count,
            len(source_configs),
        )
        return request_count

    async def _async_scheduled_refresh(self, now: datetime) -> None:
        before = _accepted_count(self.entry, self.utility_type)
        await self.async_request_refresh()
        after = _accepted_count(self.entry, self.utility_type)
        self._last_accepted_count = after
        local_now = now.astimezone(SINGAPORE_TZ)
        if after <= before and local_now.day < 7:
            retry_at = _next_day_at_configured_time(local_now, self.entry.options)
            self._unsub_timer = async_track_point_in_time(
                self.hass,
                self._async_scheduled_refresh,
                retry_at,
            )
            return
        self._schedule_next(local_now + timedelta(days=1))

# Compatibility import for tests/modules that still refer to the old name.
SpGroupDataUpdateCoordinator = SpGroupDiscoveryCoordinator


def next_water_run(now: datetime, options: Mapping[str, Any]) -> datetime:
    """Return the next configured monthly water run in Singapore time."""
    normalized = polling_options(options)
    local = now.astimezone(SINGAPORE_TZ)
    configured = datetime.combine(
        local.date().replace(day=min(normalized.water_poll_day, 28)),
        normalized.water_poll_time,
        tzinfo=SINGAPORE_TZ,
    )
    if configured <= local:
        if local.month == 12:
            configured = configured.replace(year=local.year + 1, month=1)
        else:
            configured = configured.replace(month=local.month + 1)
    return configured


def _next_day_at_configured_time(
    now: datetime,
    options: Mapping[str, Any],
) -> datetime:
    normalized = polling_options(options)
    return datetime.combine(
        (now + timedelta(days=1)).date(),
        normalized.water_poll_time,
        tzinfo=SINGAPORE_TZ,
    )


def _discovery_data(entry: SpGroupConfigEntry) -> SpGroupCoordinatorData | None:
    coordinator = entry.runtime_data.discovery_coordinator
    return None if coordinator is None else coordinator.data


def _source_configs_for_utility(
    data: SpGroupCoordinatorData,
    options: Mapping[str, Any],
    *,
    source: str,
    utility_type: str,
) -> dict[str, SpGroupSourceConfig]:
    return {
        key: config
        for key, config in source_configs_for_data(data, options).items()
        if config.source == source and config.utility_type == utility_type
    }


def _accepted_count(entry: SpGroupConfigEntry, utility_type: str) -> int:
    ledger_store = entry.runtime_data.ledger_store
    if ledger_store is None:
        return 0
    return sum(
        len(ledger_entry.accepted)
        for ledger_entry in ledger_store.snapshot.entries.values()
        if ledger_entry.utility_type == utility_type
    )


def _date_for_api(value: datetime) -> date:
    return value.astimezone(SINGAPORE_TZ).date()


def _electricity_ami_observations(
    payload: dict[str, Any],
    *,
    backfill_from: datetime,
    now: datetime,
) -> tuple[SpGroupBucketObservation, ...]:
    rows = _ami_rows(payload)
    observations: list[SpGroupBucketObservation] = []
    for row in rows:
        start = _parse_bucket_start(row.get("date"))
        value = _parse_decimal(row.get("consumption", row.get("amount")))
        if start is None or value is None or value < 0:
            continue
        end = start + timedelta(minutes=30)
        if start < backfill_from or end >= now:
            continue
        observations.append(
            SpGroupBucketObservation(
                bucket_key=start.isoformat(),
                start=start,
                end=end,
                value=value,
            )
        )
    return tuple(observations)


def _water_monthly_observations(
    payload: dict[str, Any],
    *,
    backfill_from: datetime,
    now: datetime,
) -> tuple[SpGroupBucketObservation, ...]:
    if not _is_water_volume_unit(payload.get("unit")):
        return ()
    rows = _ami_rows(payload)
    observations: list[SpGroupBucketObservation] = []
    for row in rows:
        start = _parse_month_start(row.get("date"))
        value = _parse_decimal(row.get("consumption", row.get("amount")))
        if start is None or value is None or value < 0:
            continue
        end = _next_month(start)
        if start < backfill_from or not closed_month_ready(end, now):
            continue
        observations.append(
            SpGroupBucketObservation(
                bucket_key=start.strftime("%Y-%m"),
                start=start,
                end=end,
                value=value,
            )
        )
    return tuple(observations)


def _water_history_observations(
    payload: dict[str, Any],
    *,
    backfill_from: datetime,
    now: datetime,
) -> tuple[SpGroupBucketObservation, ...]:
    """Parse the water section of a v4 history chart response."""
    _data_path = "jarvis/historyCharts/water"
    water = payload.get("water")
    if not isinstance(water, dict):
        if payload:
            LOGGER.warning(
                "SP Group %s: history response missing 'water' section: top_keys=%s",
                _data_path,
                list(payload.keys()),
            )
        return ()
    rows = water.get("data")
    if not isinstance(rows, list):
        if water:
            LOGGER.warning(
                "SP Group %s: 'water' section missing 'data' array: water_keys=%s",
                _data_path,
                list(water.keys()),
            )
        return ()
    observations: list[SpGroupBucketObservation] = []
    skipped_count = 0
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            skipped_count += 1
            continue
        period = row.get("period")
        if not isinstance(period, str) or not period:
            LOGGER.warning(
                "SP Group %s: rows[%d] missing or invalid 'period'",
                _data_path,
                i,
            )
            skipped_count += 1
            continue
        start = _parse_month_start(period)
        if start is None:
            LOGGER.warning(
                "SP Group %s: rows[%d] 'period'=%r failed to parse",
                _data_path,
                i,
                period,
            )
            skipped_count += 1
            continue
        unit = row.get("unit")
        if not _is_water_volume_unit(unit):
            LOGGER.debug(
                "SP Group %s: rows[%d] unit=%r not a recognized water volume",
                _data_path,
                i,
                unit,
            )
            skipped_count += 1
            continue
        consumption = row.get("consumption")
        if not isinstance(consumption, dict):
            LOGGER.warning(
                "SP Group %s: rows[%d] 'consumption' is %s, expected dict",
                _data_path,
                i,
                type(consumption).__name__,
            )
            skipped_count += 1
            continue
        value = _parse_decimal(consumption.get("current"))
        if value is None or value < 0:
            LOGGER.debug(
                "SP Group %s: rows[%d] consumption.current=%r invalid or negative",
                _data_path,
                i,
                consumption.get("current"),
            )
            skipped_count += 1
            continue
        # Normalize liters to cubic meters
        _liter_variants = {"l", "liter", "liters", "litre", "litres"}
        if isinstance(unit, str) and unit.strip().lower() in _liter_variants:
            value = value / Decimal("1000")
        end = _next_month(start)
        if start < backfill_from or not closed_month_ready(end, now):
            skipped_count += 1
            continue
        observations.append(
            SpGroupBucketObservation(
                bucket_key=start.strftime("%Y-%m"),
                start=start,
                end=end,
                value=value,
            )
        )
    if skipped_count and rows:
        LOGGER.debug(
            "SP Group %s: accepted=%d skipped=%d total=%d",
            _data_path,
            len(observations),
            skipped_count,
            len(rows),
        )
    return tuple(observations)


def _ami_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    _data_path = "jarvis/amiCharts"
    history = payload.get("history")
    if not isinstance(history, list):
        if payload:
            LOGGER.warning(
                "SP Group %s: expected 'history' list, got %s; top_keys=%s",
                _data_path,
                type(history).__name__,
                list(payload.keys()),
            )
        return []
    rows: list[dict[str, Any]] = []
    for i, group in enumerate(history):
        if not isinstance(group, dict):
            LOGGER.warning(
                "SP Group %s: history[%d] is %s, expected dict — skipping group",
                _data_path,
                i,
                type(group).__name__,
            )
            continue
        data = group.get("data")
        if not isinstance(data, list):
            LOGGER.warning(
                "SP Group %s: history[%d].data is %s, expected list — skipping group",
                _data_path,
                i,
                type(data).__name__,
            )
            continue
        for j, row in enumerate(data):
            if not isinstance(row, dict):
                LOGGER.warning(
                    "SP Group %s: history[%d].data[%d] is %s, expected dict — skipping row",
                    _data_path,
                    i,
                    j,
                    type(row).__name__,
                )
                continue
            if not isinstance(row.get("date"), str) or not row.get("date"):
                LOGGER.warning(
                    "SP Group %s: history[%d].data[%d] missing 'date' — skipping row",
                    _data_path,
                    i,
                    j,
                )
                continue
            value = row.get("consumption", row.get("amount"))
            if value is None:
                LOGGER.warning(
                    "SP Group %s: history[%d].data[%d] has no 'consumption' or 'amount' — skipping row",
                    _data_path,
                    i,
                    j,
                )
                continue
            rows.append(row)
    if not rows and history:
        LOGGER.warning(
            "SP Group %s: no extractable rows from %d groups (unit=%s, grouped_by=%s)",
            _data_path,
            len(history),
            payload.get("unit"),
            payload.get("grouped_by"),
        )
    return rows


def _parse_bucket_start(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    for fmt in ("%Y%m%d%H%M%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=SINGAPORE_TZ)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SINGAPORE_TZ)
    return parsed.astimezone(SINGAPORE_TZ)


def _parse_month_start(value: object) -> datetime | None:
    if isinstance(value, str):
        if len(value) == 6 and value.isdigit():
            try:
                return datetime.strptime(value, "%Y%m").replace(tzinfo=SINGAPORE_TZ)
            except ValueError:
                return None
        # Handle YYYY-MM (history chart period format, e.g. "2026-01")
        if len(value) == 7 and value[4] == "-":
            try:
                return datetime.strptime(value, "%Y-%m").replace(tzinfo=SINGAPORE_TZ)
            except ValueError:
                return None
        parsed = _parse_bucket_start(value)
        if parsed is None:
            return None
        return parsed.astimezone(SINGAPORE_TZ).replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    return None


def _parse_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _is_water_volume_unit(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    return normalized in {
        "cu m",
        "cu m3",
        "m³",
        "m3",
        "cubic meter",
        "cubic meters",
        "cubic metre",
        "cubic metres",
        "l",
        "liter",
        "liters",
        "litre",
        "litres",
    }


def _next_month(value: datetime) -> datetime:
    local = value.astimezone(SINGAPORE_TZ).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    if local.month == 12:
        return local.replace(year=local.year + 1, month=1)
    return local.replace(month=local.month + 1)


__all__ = [
    "SpGroupCoordinatorData",
    "SpGroupDataUpdateCoordinator",
    "SpGroupDiscoveryCoordinator",
    "SpGroupElectricityCoordinator",
    "SpGroupUtilityCoordinator",
    "SpGroupWaterCoordinator",
    "next_water_run",
]
