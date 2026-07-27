"""Runtime coordinators for the SP Group integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from spgroup.mobile import MobileSession, SpGroupMobileError

from .auth import async_refresh_runtime_session_if_needed
from .const import DEFAULT_UPDATE_TIMEOUT_SECONDS, DOMAIN
from .discovery import DiscoveryDataError, SpGroupCoordinatorData
from .discovery_service import async_fetch_discovery_data
from .electricity import (
    AMI_COVERAGE_INVALID,
    AMI_COVERAGE_NONE,
    AMI_COVERAGE_PARTIAL,
    ElectricitySourceError,
    classify_electricity_ami_month,
    compose_electricity_history_and_ami,
)
from .ledger import (
    PROVENANCE_AMI_GAP_ZERO,
    PROVENANCE_AMI_OBSERVED,
    PROVENANCE_AMI_OBSERVED_ZERO,
    SOURCE_COVERAGE_COVERED,
    SOURCE_COVERAGE_LAGGING,
    SOURCE_COVERAGE_MALFORMED,
    LedgerError,
    SpGroupBucketObservation,
    SpGroupKnownUnavailableRange,
    SpGroupSourceCoverage,
    SpGroupUnresolvedRange,
    closed_month_ready,
)
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


@dataclass(frozen=True, slots=True)
class SpGroupUtilityPollResult:
    """Summary of one utility poll."""

    request_count: int = 0
    source_keys: frozenset[str] = frozenset()
    accepted_delta: int = 0


@dataclass(frozen=True, slots=True)
class SpGroupParsedSource:
    """Normalized observations and provider coverage evidence."""

    observations: tuple[SpGroupBucketObservation, ...] = ()
    coverage: SpGroupSourceCoverage = SpGroupSourceCoverage()


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
            LOGGER.debug("SP Group %s API error: %s", "discovery", err)
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
        LOGGER.info("SP Group %s poll started: mode=normal", self.utility_type)
        try:
            async with asyncio.timeout(DEFAULT_UPDATE_TIMEOUT_SECONDS):
                result = await self._async_poll_sources(backfill=False)
        except ConfigEntryAuthFailed:
            raise
        except TimeoutError as err:
            raise UpdateFailed(_TIMEOUT_UPDATE_FAILED) from err
        except (LedgerError, OSError) as err:
            raise UpdateFailed(_LEDGER_UPDATE_FAILED) from err
        except SpGroupMobileError as err:
            LOGGER.debug("SP Group %s API error: %s", self.utility_type, err)
            raise UpdateFailed(_UTILITY_UPDATE_FAILED) from err
        if result.accepted_delta > 0:
            self._schedule_statistics_retry()
        return None

    async def async_poll_now(
        self,
        *,
        backfill: bool = False,
        poll_end: datetime | None = None,
    ) -> SpGroupUtilityPollResult:
        """Run one immediate utility poll and notify listeners.

        This intentionally bypasses the coordinator request-refresh debouncer for
        controlled backfill paths that need consecutive sightings.
        """
        mode = "backfill" if backfill else "manual"
        LOGGER.info("SP Group %s poll started: mode=%s", self.utility_type, mode)
        try:
            async with asyncio.timeout(DEFAULT_UPDATE_TIMEOUT_SECONDS):
                result = await self._async_poll_sources(
                    backfill=backfill,
                    now=poll_end,
                )
        except ConfigEntryAuthFailed:
            raise
        except TimeoutError as err:
            raise UpdateFailed(_TIMEOUT_UPDATE_FAILED) from err
        except (LedgerError, OSError) as err:
            raise UpdateFailed(_LEDGER_UPDATE_FAILED) from err
        except SpGroupMobileError as err:
            LOGGER.debug("SP Group %s API error: %s", self.utility_type, err)
            raise UpdateFailed(_UTILITY_UPDATE_FAILED) from err
        self.async_set_updated_data(None)
        if result.accepted_delta > 0:
            self._schedule_statistics_retry()
        return result

    async def _async_poll_sources(
        self,
        *,
        backfill: bool,
        now: datetime | None = None,
    ) -> SpGroupUtilityPollResult:
        """Poll enabled source configs for this utility and return request count."""
        runtime = self.entry.runtime_data
        if runtime.ledger_store is None:
            LOGGER.debug(
                "SP Group %s poll skipped: mode=%s reason=no_ledger_store",
                self.utility_type,
                "backfill" if backfill else "normal",
            )
            return SpGroupUtilityPollResult()
        data = _discovery_data(self.entry)
        if data is None:
            LOGGER.debug(
                "SP Group %s poll skipped: mode=%s reason=no_discovery_data",
                self.utility_type,
                "backfill" if backfill else "normal",
            )
            return SpGroupUtilityPollResult()
        source_configs = _source_configs_for_utility(
            data,
            self.entry.options,
            source=self.source,
            utility_type=self.utility_type,
        )
        await runtime.ledger_store.async_initialize_sources(source_configs)
        if not source_configs:
            LOGGER.debug(
                "SP Group %s poll skipped: mode=%s reason=no_sources",
                self.utility_type,
                "backfill" if backfill else "normal",
            )
            return SpGroupUtilityPollResult()

        session = await async_refresh_runtime_session_if_needed(self.hass, self.entry)
        premise_by_key = {premise.key: premise for premise in data.premises}
        now = now or datetime.now(SINGAPORE_TZ)
        if backfill and source_configs:
            seed_end = min(
                config.seed_effective_from for config in source_configs.values()
            )
            # Water monthly buckets need a 3-day closure grace period
            if any(c.utility_type == "water" for c in source_configs.values()):
                seed_end += timedelta(days=3)
            now = min(now, seed_end)
        mode = "backfill" if backfill else "normal"
        request_count = 0
        observation_count = 0
        accepted_before = _accepted_count(self.entry, self.utility_type)
        pending_before = _pending_count(self.entry, self.utility_type)
        polled_keys: set[str] = set()
        LOGGER.debug(
            "SP Group %s poll source phase started: mode=%s source_count=%d",
            self.utility_type,
            mode,
            len(source_configs),
        )
        for sensor_key, config in source_configs.items():
            premise = premise_by_key.get(config.premise_key)
            if premise is None or premise.jarvis_premise_id is None:
                LOGGER.debug(
                    "SP Group %s poll source skipped: mode=%s reason=no_premise",
                    self.utility_type,
                    mode,
                )
                continue
            ledger_entry = runtime.ledger_store.snapshot.entries.get(sensor_key)
            if (
                ledger_entry is not None
                and "hybrid_strategy_rebuild_required" in ledger_entry.warnings
            ):
                raise LedgerError("hybrid_strategy_rebuild_required")
            request_start = self._request_start(
                sensor_key,
                config,
                backfill=backfill,
            )
            LOGGER.debug(
                "SP Group %s poll request prepared: mode=%s start=%s end=%s",
                self.utility_type,
                mode,
                request_start.isoformat(),
                now.isoformat(),
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
            parsed = await self._async_parse_source(
                payload,
                session=session,
                premise_id=premise.jarvis_premise_id,
                backfill_from=request_start,
                now=now,
                backfill=backfill,
            )
            observation_count += len(parsed.observations)
            coverage = parsed.coverage
            if coverage.status == SOURCE_COVERAGE_MALFORMED:
                raise LedgerError("malformed_electricity_source_response")
            await runtime.ledger_store.async_apply_observations(
                sensor_key,
                parsed.observations,
                now=now,
                require_two_sightings=self.require_two_sightings,
                coverage=coverage,
            )
            polled_keys.add(sensor_key)
        accepted_after = _accepted_count(self.entry, self.utility_type)
        pending_after = _pending_count(self.entry, self.utility_type)
        LOGGER.info(
            "SP Group %s poll completed: mode=%s request_count=%d "
            "source_count=%d polled_count=%d observation_count=%d "
            "accepted_delta=%d pending_delta=%d",
            self.utility_type,
            mode,
            request_count,
            len(source_configs),
            len(polled_keys),
            observation_count,
            max(0, accepted_after - accepted_before),
            pending_after - pending_before,
        )
        return SpGroupUtilityPollResult(
            request_count=request_count,
            source_keys=frozenset(polled_keys),
            accepted_delta=max(0, accepted_after - accepted_before),
        )

    def _request_start(
        self,
        sensor_key: str,
        config: SpGroupSourceConfig,
        *,
        backfill: bool,
    ) -> datetime:
        """Return the request start for this source."""
        del sensor_key
        return config.backfill_from if backfill else config.seed_effective_from

    def _schedule_statistics_retry(self) -> None:
        """Schedule a statistics-only retry after accepted source buckets."""
        from .backfill import async_schedule_statistics_backfill

        status = async_schedule_statistics_backfill(
            self.hass,
            self.entry,
            name="sp_group_poll_statistics_backfill",
            force=True,
        )
        LOGGER.info(
            "SP Group %s poll statistics retry requested: status=%s",
            self.utility_type,
            status.value,
        )

    def _observations(
        self,
        payload: dict[str, Any],
        *,
        backfill_from: datetime,
        now: datetime,
    ) -> tuple[SpGroupBucketObservation, ...]:
        raise NotImplementedError

    def _parse_source(
        self,
        payload: dict[str, Any],
        *,
        backfill_from: datetime,
        now: datetime,
    ) -> SpGroupParsedSource:
        """Parse one provider response into observations and coverage."""
        return SpGroupParsedSource(
            observations=self._observations(
                payload,
                backfill_from=backfill_from,
                now=now,
            )
        )

    async def _async_parse_source(
        self,
        payload: dict[str, Any],
        *,
        session: MobileSession,
        premise_id: str,
        backfill_from: datetime,
        now: datetime,
        backfill: bool,
    ) -> SpGroupParsedSource:
        """Async parser hook for sources that need supporting evidence."""
        del session, premise_id, backfill
        return self._parse_source(payload, backfill_from=backfill_from, now=now)


class SpGroupElectricityCoordinator(SpGroupUtilityCoordinator):
    """Coordinate electricity AMI ledger polling."""

    source = SOURCE_MANUAL_SEED_AMI
    utility_type = "electric"
    grouped_by = "day"
    api_utility_type = "electric"
    require_two_sightings = False

    def __init__(self, hass: HomeAssistant, entry: SpGroupConfigEntry) -> None:
        """Initialize the electricity coordinator."""
        self._history_payload_cache: dict[str, dict[str, Any]] = {}
        options = polling_options(entry.options)
        super().__init__(
            hass,
            entry,
            name=f"{DOMAIN}_electricity",
            update_interval=options.electricity_poll_interval,
        )

    async def _async_poll_sources(
        self,
        *,
        backfill: bool,
        now: datetime | None = None,
    ) -> SpGroupUtilityPollResult:
        """Reset per-poll History cache before polling electricity sources."""
        self._history_payload_cache = {}
        return await super()._async_poll_sources(backfill=backfill, now=now)

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

    def _request_start(
        self,
        sensor_key: str,
        config: SpGroupSourceConfig,
        *,
        backfill: bool,
    ) -> datetime:
        """Return AMI polling start from backfill anchor or ledger cursor."""
        if backfill:
            return config.backfill_from
        ledger_store = self.entry.runtime_data.ledger_store
        if ledger_store is None:
            return config.seed_effective_from
        entry = ledger_store.snapshot.entries.get(sensor_key)
        if entry is None:
            return config.seed_effective_from
        return entry.source_poll_cursor_start()

    def _parse_source(
        self,
        payload: dict[str, Any],
        *,
        backfill_from: datetime,
        now: datetime,
    ) -> SpGroupParsedSource:
        """Parse AMI data and retain provider coverage evidence."""
        return _electricity_ami_source_poll(
            payload,
            backfill_from=backfill_from,
            now=now,
        )

    async def _async_parse_source(
        self,
        payload: dict[str, Any],
        *,
        session: MobileSession,
        premise_id: str,
        backfill_from: datetime,
        now: datetime,
        backfill: bool,
    ) -> SpGroupParsedSource:
        """Compose history only when closed AMI evidence needs it."""
        if not _electricity_ami_needs_history(
            payload,
            backfill_from=backfill_from,
            now=now,
            backfill=backfill,
        ):
            return self._parse_source(payload, backfill_from=backfill_from, now=now)
        jarvis_client = self.entry.runtime_data.jarvis_client
        history_payload = self._history_payload_cache.get(premise_id)
        if history_payload is None:
            history_payload = await jarvis_client.get_history_charts(
                session, premise_id
            )
            self._history_payload_cache[premise_id] = history_payload
        unit = payload.get("unit")
        if isinstance(unit, str) and unit.strip().lower() not in {
            "kwh",
            "kw h",
            "kilowatt-hour",
            "kilowatt-hours",
        }:
            raise LedgerError("unsupported_electricity_ami_unit")
        rows: list[tuple[datetime, Decimal]] = []
        seen_starts: set[datetime] = set()
        for row in _ami_rows(payload):
            start = _parse_bucket_start(row.get("date"))
            value = _parse_decimal(row.get("consumption", row.get("amount")))
            if start is None or value is None or value < 0:
                raise LedgerError("invalid_electricity_ami_row")
            if start in seen_starts:
                raise LedgerError("duplicate_electricity_ami_timestamp")
            seen_starts.add(start)
            rows.append((start, value))
        try:
            composition = compose_electricity_history_and_ami(
                history_payload,
                tuple(rows),
                backfill_from=backfill_from,
                now=now,
            )
        except ElectricitySourceError as err:
            raise LedgerError(str(err)) from err
        unresolved = tuple(
            SpGroupUnresolvedRange(
                cadence="30m",
                start=start,
                end=end,
                reason="terminal_provider_lag",
                first_seen_at=now,
                last_seen_at=now,
            )
            for start, end in composition.terminal_lag_ranges
        )
        known_unavailable = tuple(
            SpGroupKnownUnavailableRange(
                cadence=item.cadence,
                start=item.start,
                end=item.end,
                reason=item.reason,
                first_seen_at=now,
                last_seen_at=now,
            )
            for item in composition.known_unavailable_ranges
        )
        observations = composition.observations
        if not observations and unresolved:
            return SpGroupParsedSource(
                coverage=SpGroupSourceCoverage(
                    status=SOURCE_COVERAGE_LAGGING,
                    unresolved_ranges=unresolved,
                    known_unavailable_ranges=known_unavailable,
                )
            )
        return SpGroupParsedSource(
            observations=observations,
            coverage=SpGroupSourceCoverage(
                status=SOURCE_COVERAGE_COVERED,
                start=min((item.start for item in observations), default=None),
                end=max((item.end for item in observations), default=None),
                unresolved_ranges=unresolved,
                known_unavailable_ranges=known_unavailable,
            ),
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
        self._startup_catchup_task: asyncio.Task[None] | None = None
        self._last_accepted_count = 0

    @callback
    def async_start_schedule(self) -> None:
        """Start the water monthly schedule."""
        self.async_cancel_schedule()
        self._schedule_next(datetime.now(SINGAPORE_TZ))
        self.async_start_startup_catchup()

    @callback
    def async_cancel_schedule(self) -> None:
        """Cancel scheduled water polling."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
            LOGGER.debug("SP Group water schedule cancelled")
        if self._startup_catchup_task is not None:
            if hasattr(self.entry.runtime_data, "backfill_tasks"):
                self.entry.runtime_data.backfill_tasks.discard(
                    self._startup_catchup_task
                )
            self._startup_catchup_task.cancel()
            self._startup_catchup_task = None
            LOGGER.debug("SP Group water startup catch-up cancelled")

    @callback
    def async_start_startup_catchup(self) -> None:
        """Schedule a one-shot water catch-up if the monthly window was missed."""
        if (
            self._startup_catchup_task is not None
            and not self._startup_catchup_task.done()
        ):
            LOGGER.debug("SP Group water startup catch-up skipped: already_running")
            return
        now = datetime.now(SINGAPORE_TZ)
        if not _water_startup_catchup_needed(self.entry, now):
            try:
                reason = _water_startup_catchup_reason(self.entry, now)
            except AttributeError:
                reason = "not_needed"
            LOGGER.debug("SP Group water startup catch-up skipped: reason=%s", reason)
            return
        self._startup_catchup_task = self.hass.async_create_task(
            self._async_startup_catchup(),
            name="sp_group_water_startup_catchup",
        )
        LOGGER.info("SP Group water startup catch-up scheduled")
        if hasattr(self.entry.runtime_data, "backfill_tasks"):
            self.entry.runtime_data.backfill_tasks.add(self._startup_catchup_task)
            self._startup_catchup_task.add_done_callback(
                self.entry.runtime_data.backfill_tasks.discard
            )

    async def _async_startup_catchup(self) -> None:
        """Run one coordinator refresh for missed water monthly data."""
        current_task = asyncio.current_task()
        try:
            LOGGER.info("SP Group water startup catch-up started")
            await self.async_request_refresh()
        except asyncio.CancelledError:
            raise
        except UpdateFailed:
            LOGGER.warning(
                "SP Group water startup catch-up failed; coordinator retry remains "
                "available"
            )
            local_now = datetime.now(SINGAPORE_TZ)
            if local_now.day < 7:
                retry_at = _next_day_at_configured_time(local_now, self.entry.options)
                self._replace_timer(self._async_scheduled_refresh, retry_at)
        finally:
            if self._startup_catchup_task is current_task:
                self._startup_catchup_task = None
            LOGGER.info("SP Group water startup catch-up completed")

    def _replace_timer(
        self,
        callback_fn: Callable[[datetime], Coroutine[Any, Any, None]],
        when: datetime,
    ) -> None:
        """Replace the one active water timer without leaking its unsubscribe."""
        if self._unsub_timer is not None:
            self._unsub_timer()
        self._unsub_timer = async_track_point_in_time(
            self.hass,
            callback_fn,
            when,
        )

    def _schedule_next(self, now: datetime) -> None:
        next_run = next_water_run(now, self.entry.options)
        LOGGER.info(
            "SP Group water next scheduled poll: run_at=%s",
            next_run.isoformat(),
        )
        self._replace_timer(self._async_scheduled_refresh, next_run)

    async def _async_poll_sources(
        self,
        *,
        backfill: bool,
        now: datetime | None = None,
    ) -> SpGroupUtilityPollResult:
        """Poll water sources via GET /jarvis/v4/charts/{premise_id}.

        Water is not a smart meter so AMI endpoints return invalid kWh data.
        This override delegates to the v4 history chart endpoint instead.
        """
        runtime = self.entry.runtime_data
        if runtime.ledger_store is None:
            LOGGER.debug(
                "SP Group water poll skipped: mode=%s reason=no_ledger_store",
                "backfill" if backfill else "normal",
            )
            return SpGroupUtilityPollResult()
        data = _discovery_data(self.entry)
        if data is None:
            LOGGER.debug(
                "SP Group water poll skipped: mode=%s reason=no_discovery_data",
                "backfill" if backfill else "normal",
            )
            return SpGroupUtilityPollResult()
        source_configs = _source_configs_for_utility(
            data,
            self.entry.options,
            source=self.source,
            utility_type=self.utility_type,
        )
        await runtime.ledger_store.async_initialize_sources(source_configs)
        if not source_configs:
            LOGGER.debug(
                "SP Group water poll skipped: mode=%s reason=no_sources",
                "backfill" if backfill else "normal",
            )
            return SpGroupUtilityPollResult()

        session = await async_refresh_runtime_session_if_needed(self.hass, self.entry)
        premise_by_key = {premise.key: premise for premise in data.premises}
        now = now or datetime.now(SINGAPORE_TZ)
        if backfill and source_configs:
            seed_end = min(
                config.seed_effective_from for config in source_configs.values()
            )
            # Water monthly buckets need a 3-day closure grace period
            if any(c.utility_type == "water" for c in source_configs.values()):
                seed_end += timedelta(days=3)
            now = min(now, seed_end)
        mode = "backfill" if backfill else "normal"
        request_count = 0
        observation_count = 0
        accepted_before = _accepted_count(self.entry, self.utility_type)
        pending_before = _pending_count(self.entry, self.utility_type)
        polled_keys: set[str] = set()
        payload_by_premise: dict[str, dict[str, Any]] = {}
        LOGGER.debug(
            "SP Group water poll source phase started: mode=%s source_count=%d",
            mode,
            len(source_configs),
        )
        for sensor_key, config in source_configs.items():
            premise = premise_by_key.get(config.premise_key)
            if premise is None or premise.jarvis_premise_id is None:
                LOGGER.debug(
                    "SP Group water poll source skipped: mode=%s reason=no_premise",
                    mode,
                )
                continue
            request_start = self._request_start(
                sensor_key,
                config,
                backfill=backfill,
            )
            LOGGER.debug(
                "SP Group water poll request prepared: mode=%s start=%s end=%s",
                mode,
                request_start.isoformat(),
                now.isoformat(),
            )
            payload = payload_by_premise.get(premise.jarvis_premise_id)
            if payload is None:
                payload = await runtime.jarvis_client.get_history_charts(
                    session,
                    premise.jarvis_premise_id,
                )
                payload_by_premise[premise.jarvis_premise_id] = payload
                request_count += 1
            parsed = _water_history_source_poll(
                payload,
                backfill_from=request_start,
                now=now,
            )
            observations = parsed.observations
            observation_count += len(observations)
            if parsed.coverage.status == SOURCE_COVERAGE_MALFORMED:
                raise LedgerError("malformed_water_source_response")
            await runtime.ledger_store.async_apply_observations(
                sensor_key,
                observations,
                now=now,
                require_two_sightings=self.require_two_sightings,
                coverage=parsed.coverage,
            )
            polled_keys.add(sensor_key)
        accepted_after = _accepted_count(self.entry, self.utility_type)
        pending_after = _pending_count(self.entry, self.utility_type)
        LOGGER.info(
            "SP Group %s poll completed: mode=%s request_count=%d "
            "source_count=%d polled_count=%d observation_count=%d "
            "accepted_delta=%d pending_delta=%d",
            self.utility_type,
            mode,
            request_count,
            len(source_configs),
            len(polled_keys),
            observation_count,
            max(0, accepted_after - accepted_before),
            pending_after - pending_before,
        )
        return SpGroupUtilityPollResult(
            request_count=request_count,
            source_keys=frozenset(polled_keys),
            accepted_delta=max(0, accepted_after - accepted_before),
        )

    def _request_start(
        self,
        sensor_key: str,
        config: SpGroupSourceConfig,
        *,
        backfill: bool,
    ) -> datetime:
        """Return the persisted source cursor for water polling."""
        if backfill:
            return config.backfill_from
        ledger_store = self.entry.runtime_data.ledger_store
        if ledger_store is None:
            return config.seed_effective_from
        entry = ledger_store.snapshot.entries.get(sensor_key)
        if entry is None:
            return config.seed_effective_from
        return entry.source_poll_cursor_start()

    async def _async_scheduled_refresh(self, now: datetime) -> None:
        before = _accepted_count(self.entry, self.utility_type)
        await self.async_request_refresh()
        after = _accepted_count(self.entry, self.utility_type)
        self._last_accepted_count = after
        local_now = now.astimezone(SINGAPORE_TZ)
        if after <= before and local_now.day < 7:
            retry_at = _next_day_at_configured_time(local_now, self.entry.options)
            LOGGER.info(
                "SP Group water scheduled poll retry queued: accepted_delta=%d "
                "retry_at=%s",
                max(0, after - before),
                retry_at.isoformat(),
            )
            self._replace_timer(self._async_scheduled_refresh, retry_at)
            return
        LOGGER.debug(
            "SP Group water scheduled poll completed: accepted_delta=%d",
            max(0, after - before),
        )
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


def _water_startup_catchup_needed(
    entry: SpGroupConfigEntry,
    now: datetime,
) -> bool:
    """Return whether a one-shot water catch-up should run after startup."""
    return _water_startup_catchup_reason(entry, now) == "needed"


def _water_startup_catchup_reason(
    entry: SpGroupConfigEntry,
    now: datetime,
) -> str:
    """Return a sanitized reason for water startup catch-up scheduling."""
    data = _discovery_data(entry)
    if data is None:
        return "no_discovery_data"
    source_configs = _source_configs_for_utility(
        data,
        entry.options,
        source=SOURCE_MANUAL_SEED_MONTHLY,
        utility_type="water",
    )
    if not source_configs:
        return "no_sources"
    local_now = now.astimezone(SINGAPORE_TZ)
    options = polling_options(entry.options)
    configured_this_month = datetime.combine(
        local_now.date().replace(day=min(options.water_poll_day, 28)),
        options.water_poll_time,
        tzinfo=SINGAPORE_TZ,
    )
    if local_now < configured_this_month:
        return "before_configured_time"
    ledger_store = entry.runtime_data.ledger_store
    if ledger_store is None or ledger_store.snapshot.malformed:
        return "ledger_unavailable"
    latest_closed_month_start = _latest_ready_month_start(local_now)
    if latest_closed_month_start is None:
        return "no_ready_month"
    for sensor_key in source_configs:
        ledger_entry = ledger_store.snapshot.entries.get(sensor_key)
        if ledger_entry is None:
            return "needed"
        if _missing_closed_water_months(
            ledger_entry,
            latest_closed_month_start,
        ):
            return "needed"
    return "already_current"


def _latest_ready_month_start(now: datetime) -> datetime | None:
    """Return the latest closed monthly bucket that passed the water grace period."""
    local = now.astimezone(SINGAPORE_TZ)
    current_month = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    previous_month = _previous_month(current_month)
    if closed_month_ready(current_month, local):
        return previous_month
    older_month = _previous_month(previous_month)
    return older_month if closed_month_ready(previous_month, local) else None


def _missing_closed_water_months(
    entry: Any,
    latest_closed_month_start: datetime,
) -> tuple[datetime, ...]:
    """Return every expected closed month missing from one water ledger."""
    seed_month = entry.seed_effective_from.astimezone(SINGAPORE_TZ).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    accepted_months = {
        bucket.start.astimezone(SINGAPORE_TZ).replace(
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        for bucket in entry.accepted
    }
    missing: list[datetime] = []
    cursor = seed_month
    while cursor <= latest_closed_month_start:
        if cursor not in accepted_months:
            missing.append(cursor)
        cursor = _next_month(cursor)
    return tuple(missing)


def _previous_month(value: datetime) -> datetime:
    local = value.astimezone(SINGAPORE_TZ).replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    if local.month == 1:
        return local.replace(year=local.year - 1, month=12)
    return local.replace(month=local.month - 1)


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
    return None if coordinator is None else getattr(coordinator, "data", None)


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


def _pending_count(entry: SpGroupConfigEntry, utility_type: str) -> int:
    ledger_store = entry.runtime_data.ledger_store
    if ledger_store is None:
        return 0
    return sum(
        len(ledger_entry.pending)
        for ledger_entry in ledger_store.snapshot.entries.values()
        if ledger_entry.utility_type == utility_type
    )


def _date_for_api(value: datetime) -> date:
    return value.astimezone(SINGAPORE_TZ).date()


def _half_hour_slots(start: datetime, end: datetime) -> tuple[datetime, ...]:
    """Return completed half-hour starts in the requested polling window."""
    cursor = start.astimezone(SINGAPORE_TZ).replace(
        minute=0 if start.minute < 30 else 30,
        second=0,
        microsecond=0,
    )
    slots: list[datetime] = []
    while cursor + timedelta(minutes=30) <= end:
        if cursor >= start:
            slots.append(cursor)
        cursor += timedelta(minutes=30)
    return tuple(slots)


def _coalesce_unresolved_ranges(
    ranges: list[SpGroupUnresolvedRange],
) -> tuple[SpGroupUnresolvedRange, ...]:
    """Merge adjacent terminal lag buckets into one retry range."""
    if not ranges:
        return ()
    merged: list[SpGroupUnresolvedRange] = []
    for current in sorted(ranges, key=lambda item: item.start):
        if merged and merged[-1].end == current.start:
            previous = merged[-1]
            merged[-1] = SpGroupUnresolvedRange(
                cadence=previous.cadence,
                start=previous.start,
                end=current.end,
                reason=previous.reason,
                first_seen_at=previous.first_seen_at,
                last_seen_at=current.last_seen_at,
            )
        else:
            merged.append(current)
    return tuple(merged)


def _monthly_missing_ranges(
    observations: tuple[SpGroupBucketObservation, ...],
    *,
    backfill_from: datetime,
    now: datetime,
) -> tuple[SpGroupUnresolvedRange, ...]:
    """Return proven missing closed months between returned water rows."""
    ranges: list[SpGroupUnresolvedRange] = []
    ordered = sorted(observations, key=lambda item: item.start)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        cursor = _next_month(previous.start)
        while cursor < current.start:
            end = _next_month(cursor)
            if cursor >= backfill_from and closed_month_ready(end, now):
                ranges.append(
                    SpGroupUnresolvedRange(
                        cadence="month",
                        start=cursor,
                        end=end,
                        reason="missing_closed_month",
                        first_seen_at=now,
                        last_seen_at=now,
                    )
                )
            cursor = end
    return tuple(ranges)


def _electricity_ami_source_poll(
    payload: dict[str, Any],
    *,
    backfill_from: datetime,
    now: datetime,
) -> SpGroupParsedSource:
    """Parse AMI data for open/full months without History composition."""
    raw_history = payload.get("history")
    if not isinstance(raw_history, list):
        return SpGroupParsedSource(
            coverage=SpGroupSourceCoverage(status=SOURCE_COVERAGE_MALFORMED)
        )
    unit = payload.get("unit")
    if isinstance(unit, str) and unit.strip().lower() not in {
        "kwh",
        "kw h",
        "kilowatt-hour",
        "kilowatt-hours",
    }:
        return SpGroupParsedSource(
            coverage=SpGroupSourceCoverage(status=SOURCE_COVERAGE_MALFORMED)
        )
    rows: list[tuple[datetime, Decimal]] = []
    malformed_row = False
    for row in _ami_rows(payload):
        start = _parse_bucket_start(row.get("date"))
        value = _parse_decimal(row.get("consumption", row.get("amount")))
        if start is None or value is None or value < 0:
            malformed_row = True
            continue
        rows.append((start, value))
    if malformed_row or len({start for start, _ in rows}) != len(rows):
        return SpGroupParsedSource(
            coverage=SpGroupSourceCoverage(status=SOURCE_COVERAGE_MALFORMED)
        )
    rows.sort(key=lambda item: item[0])
    completed_rows = [
        (start, value) for start, value in rows if start + timedelta(minutes=30) <= now
    ]
    positive = [(start, value) for start, value in completed_rows if value > 0]
    if not positive:
        rows_by_month: dict[datetime, dict[datetime, Decimal]] = {}
        for start, value in completed_rows:
            month = _parse_month_start(start.isoformat())
            if month is not None:
                rows_by_month.setdefault(month, {})[start] = value
        full_zero_observations: list[SpGroupBucketObservation] = []
        for month, month_rows in rows_by_month.items():
            classification = classify_electricity_ami_month(month, month_rows)
            if classification.coverage == "full_ami":
                full_zero_observations.extend(
                    SpGroupBucketObservation(
                        bucket_key=start.isoformat(),
                        start=start,
                        end=start + timedelta(minutes=30),
                        value=Decimal("0"),
                        provenance=PROVENANCE_AMI_OBSERVED_ZERO,
                    )
                    for start in month_rows
                )
        if full_zero_observations:
            return SpGroupParsedSource(
                observations=tuple(
                    sorted(full_zero_observations, key=lambda item: item.start)
                ),
                coverage=SpGroupSourceCoverage(
                    status=SOURCE_COVERAGE_COVERED,
                    start=full_zero_observations[0].start,
                    end=full_zero_observations[-1].end,
                ),
            )
        status = SOURCE_COVERAGE_LAGGING
        return SpGroupParsedSource(coverage=SpGroupSourceCoverage(status=status))

    row_by_start = dict(completed_rows)
    first_positive = positive[0][0]
    last_positive = positive[-1][0]
    slots = tuple(
        slot
        for slot in _half_hour_slots(backfill_from, now)
        if slot + timedelta(minutes=30) <= now
    )
    unresolved: list[SpGroupUnresolvedRange] = []
    observations_list: list[SpGroupBucketObservation] = []
    terminal_month = _parse_month_start(now.isoformat())
    for slot in slots:
        if slot in row_by_start:
            value = row_by_start[slot]
            observations_list.append(
                SpGroupBucketObservation(
                    bucket_key=slot.isoformat(),
                    start=slot,
                    end=slot + timedelta(minutes=30),
                    value=value,
                    provenance=(
                        PROVENANCE_AMI_OBSERVED_ZERO
                        if value == 0
                        else PROVENANCE_AMI_OBSERVED
                    ),
                )
            )
        elif slot < first_positive or first_positive <= slot <= last_positive:
            observations_list.append(
                SpGroupBucketObservation(
                    bucket_key=slot.isoformat(),
                    start=slot,
                    end=slot + timedelta(minutes=30),
                    value=Decimal("0"),
                    provenance=PROVENANCE_AMI_GAP_ZERO,
                )
            )
        elif slot > last_positive and terminal_month == _parse_month_start(
            last_positive.isoformat()
        ):
            unresolved.append(
                SpGroupUnresolvedRange(
                    cadence="30m",
                    start=slot,
                    end=slot + timedelta(minutes=30),
                    reason="terminal_provider_lag",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
    observations = tuple(observations_list)
    return SpGroupParsedSource(
        observations=observations,
        coverage=SpGroupSourceCoverage(
            status=SOURCE_COVERAGE_COVERED,
            start=observations[0].start if observations else None,
            end=observations[-1].end if observations else None,
            unresolved_ranges=_coalesce_unresolved_ranges(unresolved),
        ),
    )


def _electricity_ami_needs_history(
    payload: dict[str, Any],
    *,
    backfill_from: datetime,
    now: datetime,
    backfill: bool,
) -> bool:
    """Return whether closed-month history evidence is required."""
    rows_by_month: dict[datetime, dict[datetime, Decimal]] = {}
    duplicate_months: set[datetime] = set()
    for row in _ami_rows(payload):
        start = _parse_bucket_start(row.get("date"))
        value = _parse_decimal(row.get("consumption", row.get("amount")))
        if (
            start is None
            or value is None
            or value < 0
            or start + timedelta(minutes=30) > now
        ):
            continue
        month = _parse_month_start(start.isoformat())
        if month is not None:
            rows = rows_by_month.setdefault(month, {})
            if start in rows:
                duplicate_months.add(month)
            rows[start] = value
    request_month = _parse_month_start(backfill_from.isoformat())
    if request_month is not None and _next_month(request_month) <= now:
        if request_month not in rows_by_month:
            return True
    for month, rows in rows_by_month.items():
        month_end = _next_month(month)
        if month_end > now:
            continue
        classification = classify_electricity_ami_month(
            month,
            rows,
            window_start=max(month, backfill_from),
            window_end=min(month_end, now),
            duplicate=month in duplicate_months,
        )
        if classification.coverage in {
            AMI_COVERAGE_NONE,
            AMI_COVERAGE_PARTIAL,
            AMI_COVERAGE_INVALID,
        }:
            return True
    return False


def _water_history_source_poll(
    payload: dict[str, Any],
    *,
    backfill_from: datetime,
    now: datetime,
) -> SpGroupParsedSource:
    """Parse water history and derive only proven internal month gaps."""
    water = payload.get("water")
    if not isinstance(water, dict) or not isinstance(water.get("data"), list):
        return SpGroupParsedSource(
            coverage=SpGroupSourceCoverage(status=SOURCE_COVERAGE_MALFORMED)
        )
    observations = _water_history_observations(
        payload,
        backfill_from=backfill_from,
        now=now,
    )
    if not observations:
        return SpGroupParsedSource(
            coverage=SpGroupSourceCoverage(status=SOURCE_COVERAGE_LAGGING)
        )
    return SpGroupParsedSource(
        observations=observations,
        coverage=SpGroupSourceCoverage(
            status=SOURCE_COVERAGE_COVERED,
            start=min(item.start for item in observations),
            end=max(item.end for item in observations),
            unresolved_ranges=_monthly_missing_ranges(
                observations,
                backfill_from=backfill_from,
                now=now,
            ),
        ),
    )


def _electricity_ami_observations(
    payload: dict[str, Any],
    *,
    backfill_from: datetime,
    now: datetime,
) -> tuple[SpGroupBucketObservation, ...]:
    return _electricity_ami_source_poll(
        payload,
        backfill_from=backfill_from,
        now=now,
    ).observations


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
                    "SP Group %s: history[%d].data[%d] is %s, expected dict; "
                    "skipping row",
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
                    "SP Group %s: history[%d].data[%d] has no 'consumption' "
                    "or 'amount'; skipping row",
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
    "SpGroupUtilityPollResult",
    "SpGroupWaterCoordinator",
    "next_water_run",
]
