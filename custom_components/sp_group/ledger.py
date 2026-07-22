"""Persistent accumulation ledger for SP Group dashboard sensors."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .sources import SINGAPORE_TZ, SpGroupSourceConfig

_STORE_VERSION = 1
_STORE_MINOR_VERSION = 1
_WATER_GRACE_DAYS = 3
STATISTICS_STRATEGY_NATIVE_ANCHORED_V1 = "native_anchored_v1"
STATISTICS_STRATEGY_NATIVE_OFFSET_V2 = "native_offset_v2"
WARNING_REPAIR_REBUILT_MALFORMED_LEDGER = "repair_rebuilt_malformed_ledger"
WARNING_USER_REBUILT_LEDGER = "user_rebuilt_ledger"
WARNING_LEGACY_BACKFILL_FROM_DEFAULTED = "legacy_backfill_from_defaulted"
WARNING_LEGACY_STATISTICS_STRATEGY_MIGRATED = "legacy_statistics_strategy_migrated"
SOURCE_BACKFILL_NEVER_ATTEMPTED = "never_attempted"
SOURCE_BACKFILL_RUNNING = "running"
SOURCE_BACKFILL_SUCCEEDED = "succeeded"
SOURCE_BACKFILL_FAILED = "failed"
SOURCE_BACKFILL_STATUSES = frozenset(
    {
        SOURCE_BACKFILL_NEVER_ATTEMPTED,
        SOURCE_BACKFILL_RUNNING,
        SOURCE_BACKFILL_SUCCEEDED,
        SOURCE_BACKFILL_FAILED,
    }
)
WARNING_SOURCE_BACKFILL_INTERRUPTED = "source_backfill_interrupted"


class LedgerError(ValueError):
    """Raised when ledger state cannot be trusted."""


@dataclass(frozen=True, slots=True)
class SpGroupAcceptedBucket:
    """Immutable accepted bucket contribution."""

    bucket_key: str
    start: datetime
    end: datetime
    value: Decimal
    first_seen_at: datetime
    accepted_at: datetime

    def to_dict(self) -> dict[str, Any]:
        """Serialize the accepted bucket."""
        return {
            "bucket_key": self.bucket_key,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "value": str(self.value),
            "first_seen_at": self.first_seen_at.isoformat(),
            "accepted_at": self.accepted_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SpGroupAcceptedBucket:
        """Deserialize an accepted bucket."""
        return cls(
            bucket_key=_required_string(payload, "bucket_key"),
            start=_parse_datetime(_required_string(payload, "start")),
            end=_parse_datetime(_required_string(payload, "end")),
            value=_parse_decimal(_required_string(payload, "value")),
            first_seen_at=_parse_datetime(_required_string(payload, "first_seen_at")),
            accepted_at=_parse_datetime(_required_string(payload, "accepted_at")),
        )


@dataclass(frozen=True, slots=True)
class SpGroupPendingBucket:
    """Candidate bucket waiting for one more stable sighting."""

    bucket_key: str
    start: datetime
    end: datetime
    value: Decimal
    first_seen_at: datetime
    last_seen_at: datetime
    sighting_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Serialize the pending bucket."""
        return {
            "bucket_key": self.bucket_key,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "value": str(self.value),
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "sighting_count": self.sighting_count,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SpGroupPendingBucket:
        """Deserialize a pending bucket."""
        return cls(
            bucket_key=_required_string(payload, "bucket_key"),
            start=_parse_datetime(_required_string(payload, "start")),
            end=_parse_datetime(_required_string(payload, "end")),
            value=_parse_decimal(_required_string(payload, "value")),
            first_seen_at=_parse_datetime(_required_string(payload, "first_seen_at")),
            last_seen_at=_parse_datetime(_required_string(payload, "last_seen_at")),
            sighting_count=int(payload.get("sighting_count", 1)),
        )


@dataclass(frozen=True, slots=True)
class SpGroupLedgerEntry:
    """Ledger state for a seeded utility sensor."""

    sensor_key: str
    source: str
    utility_type: str
    epoch_id: str
    seed_value: Decimal
    seed_effective_from: datetime
    backfill_from: datetime
    latest_total: Decimal
    latest_accepted_at: datetime | None = None
    accepted: tuple[SpGroupAcceptedBucket, ...] = field(default_factory=tuple)
    pending: tuple[SpGroupPendingBucket, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    source_backfill_attempted_at: datetime | None = None
    source_backfill_status: str = SOURCE_BACKFILL_NEVER_ATTEMPTED
    source_backfill_started_at: datetime | None = None
    source_backfill_succeeded_at: datetime | None = None
    source_backfill_failed_at: datetime | None = None
    last_source_poll_succeeded_at: datetime | None = None
    statistics_imported_through: datetime | None = None
    statistics_reference_start: datetime | None = None
    statistics_reference_sum_offset: Decimal | None = None
    statistics_strategy_version: str | None = None
    statistics_warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_trusted_total(self) -> bool:
        """Return whether the ledger can publish a dashboard-ready total."""
        return self.latest_total >= 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize the ledger entry."""
        return {
            "sensor_key": self.sensor_key,
            "source": self.source,
            "utility_type": self.utility_type,
            "epoch_id": self.epoch_id,
            "seed_value": str(self.seed_value),
            "seed_effective_from": self.seed_effective_from.isoformat(),
            "backfill_from": self.backfill_from.isoformat(),
            "latest_total": str(self.latest_total),
            "latest_accepted_at": (
                self.latest_accepted_at.isoformat()
                if self.latest_accepted_at is not None
                else None
            ),
            "accepted": [bucket.to_dict() for bucket in self.accepted],
            "pending": [bucket.to_dict() for bucket in self.pending],
            "warnings": list(self.warnings),
            "source_backfill_attempted_at": (
                self.source_backfill_attempted_at.isoformat()
                if self.source_backfill_attempted_at is not None
                else None
            ),
            "source_backfill_status": self.source_backfill_status,
            "source_backfill_started_at": (
                self.source_backfill_started_at.isoformat()
                if self.source_backfill_started_at is not None
                else None
            ),
            "source_backfill_succeeded_at": (
                self.source_backfill_succeeded_at.isoformat()
                if self.source_backfill_succeeded_at is not None
                else None
            ),
            "source_backfill_failed_at": (
                self.source_backfill_failed_at.isoformat()
                if self.source_backfill_failed_at is not None
                else None
            ),
            "last_source_poll_succeeded_at": (
                self.last_source_poll_succeeded_at.isoformat()
                if self.last_source_poll_succeeded_at is not None
                else None
            ),
            "statistics_imported_through": (
                self.statistics_imported_through.isoformat()
                if self.statistics_imported_through is not None
                else None
            ),
            "statistics_reference_start": (
                self.statistics_reference_start.isoformat()
                if self.statistics_reference_start is not None
                else None
            ),
            "statistics_reference_sum_offset": (
                str(self.statistics_reference_sum_offset)
                if self.statistics_reference_sum_offset is not None
                else None
            ),
            "statistics_strategy_version": self.statistics_strategy_version,
            "statistics_warnings": list(self.statistics_warnings),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SpGroupLedgerEntry:
        """Deserialize a ledger entry."""
        accepted = payload.get("accepted", [])
        pending = payload.get("pending", [])
        accepted_tuple = tuple(
            SpGroupAcceptedBucket.from_dict(bucket)
            for bucket in accepted
            if isinstance(bucket, dict)
        )
        pending_tuple = tuple(
            SpGroupPendingBucket.from_dict(bucket)
            for bucket in pending
            if isinstance(bucket, dict)
        )
        latest_accepted = payload.get("latest_accepted_at")
        source_backfill_attempted = payload.get("source_backfill_attempted_at")
        seed_effective_from = _parse_datetime(
            _required_string(payload, "seed_effective_from")
        )
        statistics_imported_through = (
            _parse_datetime(statistics_imported_through_value)
            if isinstance(
                statistics_imported_through_value := payload.get(
                    "statistics_imported_through"
                ),
                str,
            )
            else None
        )
        statistics_reference_start = (
            _parse_datetime(statistics_reference_start_value)
            if isinstance(
                statistics_reference_start_value := payload.get(
                    "statistics_reference_start"
                ),
                str,
            )
            else None
        )
        statistics_reference_sum_offset = (
            _parse_decimal(statistics_reference_sum_offset_value)
            if isinstance(
                statistics_reference_sum_offset_value := payload.get(
                    "statistics_reference_sum_offset"
                ),
                str,
            )
            else None
        )
        statistics_strategy_version = payload.get("statistics_strategy_version")
        raw_statistics_warnings = tuple(
            warning
            for warning in payload.get("statistics_warnings", [])
            if isinstance(warning, str)
        )
        if (
            statistics_imported_through is not None
            and statistics_strategy_version != STATISTICS_STRATEGY_NATIVE_OFFSET_V2
        ):
            raw_statistics_warnings = tuple(
                dict.fromkeys(
                    (
                        *raw_statistics_warnings,
                        WARNING_LEGACY_STATISTICS_STRATEGY_MIGRATED,
                    )
                )
            )
        raw_warnings = tuple(
            warning
            for warning in payload.get("warnings", [])
            if isinstance(warning, str)
        )
        if isinstance(backfill_value := payload.get("backfill_from"), str):
            backfill_from = _parse_datetime(backfill_value)
            warnings = raw_warnings
        else:
            backfill_from = seed_effective_from
            warnings = tuple(
                dict.fromkeys(
                    (*raw_warnings, WARNING_LEGACY_BACKFILL_FROM_DEFAULTED)
                )
            )
        source_backfill_status = payload.get("source_backfill_status")
        if source_backfill_status not in SOURCE_BACKFILL_STATUSES:
            source_backfill_status = (
                SOURCE_BACKFILL_SUCCEEDED
                if (
                    isinstance(source_backfill_attempted, str)
                    and (accepted_tuple or statistics_imported_through is not None)
                )
                else SOURCE_BACKFILL_NEVER_ATTEMPTED
            )
        source_backfill_started_at = _optional_datetime(
            payload.get("source_backfill_started_at")
        )
        source_backfill_succeeded_at = _optional_datetime(
            payload.get("source_backfill_succeeded_at")
        )
        source_backfill_failed_at = _optional_datetime(
            payload.get("source_backfill_failed_at")
        )
        last_source_poll_succeeded_at = _optional_datetime(
            payload.get("last_source_poll_succeeded_at")
        )
        legacy_attempted_at = _optional_datetime(source_backfill_attempted)
        if source_backfill_status == SOURCE_BACKFILL_SUCCEEDED:
            source_backfill_succeeded_at = (
                source_backfill_succeeded_at
                or legacy_attempted_at
                or statistics_imported_through
            )
            last_source_poll_succeeded_at = (
                last_source_poll_succeeded_at or source_backfill_succeeded_at
            )
        return cls(
            sensor_key=_required_string(payload, "sensor_key"),
            source=_required_string(payload, "source"),
            utility_type=_required_string(payload, "utility_type"),
            epoch_id=_optional_epoch_id(payload),
            seed_value=_parse_decimal(_required_string(payload, "seed_value")),
            seed_effective_from=seed_effective_from,
            backfill_from=backfill_from,
            latest_total=_parse_decimal(_required_string(payload, "latest_total")),
            latest_accepted_at=(
                _parse_datetime(latest_accepted)
                if isinstance(latest_accepted, str)
                else None
            ),
            accepted=accepted_tuple,
            pending=pending_tuple,
            warnings=warnings,
            source_backfill_attempted_at=(
                legacy_attempted_at
                or source_backfill_succeeded_at
                or statistics_imported_through
            ),
            source_backfill_status=source_backfill_status,
            source_backfill_started_at=source_backfill_started_at,
            source_backfill_succeeded_at=source_backfill_succeeded_at,
            source_backfill_failed_at=source_backfill_failed_at,
            last_source_poll_succeeded_at=last_source_poll_succeeded_at,
            statistics_imported_through=(
                statistics_imported_through
            ),
            statistics_reference_start=statistics_reference_start,
            statistics_reference_sum_offset=statistics_reference_sum_offset,
            statistics_strategy_version=(
                statistics_strategy_version
                if isinstance(statistics_strategy_version, str)
                else None
            ),
            statistics_warnings=raw_statistics_warnings,
        )

    def source_poll_cursor_start(self, *, overlap: timedelta) -> datetime:
        """Return the earliest normal poll start needed to catch up safely."""
        pending_start = min((bucket.start for bucket in self.pending), default=None)
        if pending_start is not None:
            return max(self.seed_effective_from, pending_start - overlap)
        accepted_end = max((bucket.end for bucket in self.accepted), default=None)
        if accepted_end is not None:
            return max(self.seed_effective_from, accepted_end - overlap)
        return self.seed_effective_from


@dataclass(frozen=True, slots=True)
class SpGroupLedgerSnapshot:
    """Full persisted ledger snapshot."""

    entries: dict[str, SpGroupLedgerEntry] = field(default_factory=dict)
    malformed: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize the snapshot."""
        return {
            "entries": {
                key: entry.to_dict() for key, entry in sorted(self.entries.items())
            }
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> SpGroupLedgerSnapshot:
        """Deserialize persisted ledger state."""
        if payload is None:
            return cls()
        try:
            raw_entries = payload.get("entries")
            if not isinstance(raw_entries, dict):
                raise LedgerError("missing_entries")
            entries = {
                key: SpGroupLedgerEntry.from_dict(value)
                for key, value in raw_entries.items()
                if isinstance(key, str) and isinstance(value, dict)
            }
        except (LedgerError, ValueError, TypeError, InvalidOperation):
            return cls(malformed=True)
        return cls(entries=entries)


@dataclass(frozen=True, slots=True)
class SpGroupBucketObservation:
    """Normalized bucket observation from an SP payload."""

    bucket_key: str
    start: datetime
    end: datetime
    value: Decimal


class SpGroupLedgerStore:
    """Versioned HA storage wrapper for accumulation state."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the store."""
        self._store: Store[dict[str, Any]] = Store(
            hass,
            _STORE_VERSION,
            f"{DOMAIN}_{entry_id}_ledger",
            minor_version=_STORE_MINOR_VERSION,
        )
        self._lock = asyncio.Lock()
        self._snapshot = SpGroupLedgerSnapshot()

    @property
    def snapshot(self) -> SpGroupLedgerSnapshot:
        """Return the in-memory snapshot."""
        return self._snapshot

    async def async_load(self) -> SpGroupLedgerSnapshot:
        """Load ledger state from Home Assistant storage."""
        async with self._lock:
            raw_payload = await self._store.async_load()
            self._snapshot = SpGroupLedgerSnapshot.from_dict(raw_payload)
            return self._snapshot

    async def async_initialize_sources(
        self,
        configs: dict[str, SpGroupSourceConfig],
    ) -> SpGroupLedgerSnapshot:
        """Ensure a ledger entry exists for every enabled source config."""
        async with self._lock:
            if self._snapshot.malformed:
                return self._snapshot
            entries = dict(self._snapshot.entries)
            changed = False
            for sensor_key, config in configs.items():
                current = entries.get(sensor_key)
                if current is None:
                    entries[sensor_key] = _entry_from_config(sensor_key, config)
                    changed = True
                    continue
                if current.epoch_id == config.epoch_id:
                    continue
                if (
                    config.seed_value < current.latest_total
                    and not config.reset_confirmed
                ):
                    replacement_warning = "blocked_unconfirmed_lower_replacement"
                    if replacement_warning in current.warnings:
                        continue
                    entries[sensor_key] = _copy_entry_with_warnings(
                        current,
                        (*current.warnings, replacement_warning),
                    )
                    changed = True
                    continue
                warning = (
                    "user_confirmed_lower_replacement"
                    if config.seed_value < current.latest_total
                    else "user_replacement"
                )
                entries[sensor_key] = _entry_from_config(
                    sensor_key,
                    config,
                    warnings=(warning,),
                )
                changed = True
            next_snapshot = SpGroupLedgerSnapshot(entries=entries)
            if changed:
                await self._store.async_save(next_snapshot.to_dict())
            self._snapshot = next_snapshot
            return self._snapshot

    async def async_apply_observations(
        self,
        sensor_key: str,
        observations: tuple[SpGroupBucketObservation, ...],
        *,
        now: datetime,
        require_two_sightings: bool,
    ) -> SpGroupLedgerSnapshot:
        """Apply normalized bucket observations to one ledger entry."""
        async with self._lock:
            entry = self._snapshot.entries.get(sensor_key)
            if entry is None or self._snapshot.malformed:
                return self._snapshot
            updated = _apply_observations_to_entry(
                entry,
                observations,
                now=now.astimezone(SINGAPORE_TZ),
                require_two_sightings=require_two_sightings,
            )
            if updated == entry:
                return self._snapshot
            entries = dict(self._snapshot.entries)
            entries[sensor_key] = updated
            next_snapshot = SpGroupLedgerSnapshot(entries=entries)
            await self._store.async_save(next_snapshot.to_dict())
            self._snapshot = next_snapshot
            return self._snapshot

    async def async_rebuild_sources(
        self,
        configs: dict[str, SpGroupSourceConfig],
        *,
        sensor_keys: set[str] | None = None,
        warning: str = WARNING_USER_REBUILT_LEDGER,
    ) -> SpGroupLedgerSnapshot:
        """Rebuild selected ledger entries from stored source configs."""
        async with self._lock:
            entries = {} if self._snapshot.malformed else dict(self._snapshot.entries)
            selected = set(configs) if sensor_keys is None else sensor_keys
            changed = self._snapshot.malformed
            for sensor_key in sorted(selected):
                config = configs.get(sensor_key)
                if config is None:
                    continue
                entries[sensor_key] = _entry_from_config(
                    sensor_key,
                    config,
                    warnings=(warning,),
                )
                changed = True
            next_snapshot = SpGroupLedgerSnapshot(entries=entries)
            if changed:
                await self._store.async_save(next_snapshot.to_dict())
            self._snapshot = next_snapshot
            return self._snapshot

    async def async_update_statistics_metadata(
        self,
        sensor_key: str,
        *,
        imported_through: datetime | None = None,
        reference_start: datetime | None = None,
        reference_sum_offset: Decimal | None = None,
        strategy_version: str | None = None,
        warnings: tuple[str, ...] = (),
    ) -> SpGroupLedgerSnapshot:
        """Update statistics import metadata for a ledger entry."""
        async with self._lock:
            entry = self._snapshot.entries.get(sensor_key)
            if entry is None or self._snapshot.malformed:
                return self._snapshot
            updated = SpGroupLedgerEntry(
                sensor_key=entry.sensor_key,
                source=entry.source,
                utility_type=entry.utility_type,
                epoch_id=entry.epoch_id,
                seed_value=entry.seed_value,
                seed_effective_from=entry.seed_effective_from,
                backfill_from=entry.backfill_from,
                latest_total=entry.latest_total,
                latest_accepted_at=entry.latest_accepted_at,
                accepted=entry.accepted,
                pending=entry.pending,
                warnings=entry.warnings,
                source_backfill_attempted_at=entry.source_backfill_attempted_at,
                source_backfill_status=entry.source_backfill_status,
                source_backfill_started_at=entry.source_backfill_started_at,
                source_backfill_succeeded_at=entry.source_backfill_succeeded_at,
                source_backfill_failed_at=entry.source_backfill_failed_at,
                last_source_poll_succeeded_at=entry.last_source_poll_succeeded_at,
                statistics_imported_through=(
                    imported_through
                    if imported_through is not None
                    else entry.statistics_imported_through
                ),
                statistics_reference_start=(
                    reference_start
                    if reference_start is not None
                    else entry.statistics_reference_start
                ),
                statistics_reference_sum_offset=(
                    reference_sum_offset
                    if reference_sum_offset is not None
                    else entry.statistics_reference_sum_offset
                ),
                statistics_strategy_version=(
                    strategy_version
                    if strategy_version is not None
                    else entry.statistics_strategy_version
                ),
                statistics_warnings=tuple(dict.fromkeys(warnings)),
            )
            if updated == entry:
                return self._snapshot
            entries = dict(self._snapshot.entries)
            entries[sensor_key] = updated
            next_snapshot = SpGroupLedgerSnapshot(entries=entries)
            await self._store.async_save(next_snapshot.to_dict())
            self._snapshot = next_snapshot
            return self._snapshot

    async def async_mark_source_backfill_running(
        self,
        *,
        sensor_keys: set[str],
        started_at: datetime,
    ) -> SpGroupLedgerSnapshot:
        """Mark source backfill as running for selected entries."""
        return await self._async_update_source_backfill_status(
            sensor_keys,
            SOURCE_BACKFILL_RUNNING,
            timestamp=started_at,
        )

    async def async_mark_source_backfill_succeeded(
        self,
        *,
        sensor_keys: set[str],
        succeeded_at: datetime,
    ) -> SpGroupLedgerSnapshot:
        """Mark source backfill/poll success for selected entries."""
        return await self._async_update_source_backfill_status(
            sensor_keys,
            SOURCE_BACKFILL_SUCCEEDED,
            timestamp=succeeded_at,
        )

    async def async_mark_source_backfill_failed(
        self,
        *,
        sensor_keys: set[str],
        failed_at: datetime,
        warning: str | None = None,
    ) -> SpGroupLedgerSnapshot:
        """Mark source backfill as failed for selected entries."""
        return await self._async_update_source_backfill_status(
            sensor_keys,
            SOURCE_BACKFILL_FAILED,
            timestamp=failed_at,
            warning=warning,
        )

    async def async_mark_interrupted_source_backfills_failed(
        self,
        *,
        failed_at: datetime,
    ) -> SpGroupLedgerSnapshot:
        """Convert persisted running backfills from a previous HA run to failed."""
        sensor_keys = {
            key
            for key, entry in self._snapshot.entries.items()
            if entry.source_backfill_status == SOURCE_BACKFILL_RUNNING
        }
        if not sensor_keys:
            return self._snapshot
        return await self.async_mark_source_backfill_failed(
            sensor_keys=sensor_keys,
            failed_at=failed_at,
            warning=WARNING_SOURCE_BACKFILL_INTERRUPTED,
        )

    async def async_mark_source_backfill_attempted(
        self,
        *,
        attempted_at: datetime,
    ) -> SpGroupLedgerSnapshot:
        """Legacy compatibility wrapper for marking source backfill succeeded."""
        return await self.async_mark_source_backfill_succeeded(
            sensor_keys=set(self._snapshot.entries),
            succeeded_at=attempted_at,
        )

    async def _async_update_source_backfill_status(
        self,
        sensor_keys: set[str],
        status: str,
        *,
        timestamp: datetime,
        warning: str | None = None,
    ) -> SpGroupLedgerSnapshot:
        """Update source backfill status for selected entries."""
        async with self._lock:
            if self._snapshot.malformed:
                return self._snapshot
            entries: dict[str, SpGroupLedgerEntry] = {}
            changed = False
            for sensor_key, entry in self._snapshot.entries.items():
                if sensor_key not in sensor_keys:
                    entries[sensor_key] = entry
                    continue
                local_timestamp = timestamp.astimezone(SINGAPORE_TZ)
                warnings = entry.warnings
                if warning is not None and warning not in warnings:
                    warnings = (*warnings, warning)
                entries[sensor_key] = SpGroupLedgerEntry(
                    sensor_key=entry.sensor_key,
                    source=entry.source,
                    utility_type=entry.utility_type,
                    epoch_id=entry.epoch_id,
                    seed_value=entry.seed_value,
                    seed_effective_from=entry.seed_effective_from,
                    backfill_from=entry.backfill_from,
                    latest_total=entry.latest_total,
                    latest_accepted_at=entry.latest_accepted_at,
                    accepted=entry.accepted,
                    pending=entry.pending,
                    warnings=warnings,
                    source_backfill_attempted_at=(
                        local_timestamp
                        if status == SOURCE_BACKFILL_SUCCEEDED
                        else entry.source_backfill_attempted_at
                    ),
                    source_backfill_status=status,
                    source_backfill_started_at=(
                        local_timestamp
                        if status == SOURCE_BACKFILL_RUNNING
                        else entry.source_backfill_started_at
                    ),
                    source_backfill_succeeded_at=(
                        local_timestamp
                        if status == SOURCE_BACKFILL_SUCCEEDED
                        else entry.source_backfill_succeeded_at
                    ),
                    source_backfill_failed_at=(
                        local_timestamp
                        if status == SOURCE_BACKFILL_FAILED
                        else entry.source_backfill_failed_at
                    ),
                    last_source_poll_succeeded_at=(
                        local_timestamp
                        if status == SOURCE_BACKFILL_SUCCEEDED
                        else entry.last_source_poll_succeeded_at
                    ),
                    statistics_imported_through=entry.statistics_imported_through,
                    statistics_reference_start=entry.statistics_reference_start,
                    statistics_reference_sum_offset=(
                        entry.statistics_reference_sum_offset
                    ),
                    statistics_strategy_version=entry.statistics_strategy_version,
                    statistics_warnings=entry.statistics_warnings,
                )
                changed = True
            if not changed:
                return self._snapshot
            next_snapshot = SpGroupLedgerSnapshot(entries=entries)
            await self._store.async_save(next_snapshot.to_dict())
            self._snapshot = next_snapshot
            return self._snapshot


def _apply_observations_to_entry(
    entry: SpGroupLedgerEntry,
    observations: tuple[SpGroupBucketObservation, ...],
    *,
    now: datetime,
    require_two_sightings: bool,
) -> SpGroupLedgerEntry:
    accepted_by_key = {bucket.bucket_key: bucket for bucket in entry.accepted}
    pending_by_key = {bucket.bucket_key: bucket for bucket in entry.pending}
    warnings = list(entry.warnings)
    accepted = list(entry.accepted)
    pending: list[SpGroupPendingBucket] = []
    changed = False
    seen_observation_keys: set[str] = set()

    for observation in sorted(observations, key=lambda item: item.start):
        if observation.bucket_key in seen_observation_keys:
            if "duplicate_observation_keys" not in warnings:
                warnings.append("duplicate_observation_keys")
                changed = True
            continue
        seen_observation_keys.add(observation.bucket_key)
        if observation.start < entry.backfill_from:
            continue
        accepted_bucket = accepted_by_key.get(observation.bucket_key)
        if accepted_bucket is not None:
            if accepted_bucket.value != observation.value:
                warning = f"corrected_bucket:{observation.bucket_key}"
                if warning not in warnings:
                    warnings.append(warning)
                    changed = True
            continue
        current_pending = pending_by_key.pop(observation.bucket_key, None)
        if (
            current_pending is not None
            and current_pending.value == observation.value
            and require_two_sightings
        ):
            accepted.append(
                SpGroupAcceptedBucket(
                    bucket_key=observation.bucket_key,
                    start=observation.start,
                    end=observation.end,
                    value=observation.value,
                    first_seen_at=current_pending.first_seen_at,
                    accepted_at=now,
                )
            )
            changed = True
            continue
        if require_two_sightings:
            pending.append(
                SpGroupPendingBucket(
                    bucket_key=observation.bucket_key,
                    start=observation.start,
                    end=observation.end,
                    value=observation.value,
                    first_seen_at=(
                        current_pending.first_seen_at
                        if current_pending is not None
                        else now
                    ),
                    last_seen_at=now,
                    sighting_count=(
                        current_pending.sighting_count + 1
                        if current_pending is not None
                        else 1
                    ),
                )
            )
            changed = True
        else:
            accepted.append(
                SpGroupAcceptedBucket(
                    bucket_key=observation.bucket_key,
                    start=observation.start,
                    end=observation.end,
                    value=observation.value,
                    first_seen_at=now,
                    accepted_at=now,
                )
            )
            changed = True

    pending.extend(pending_by_key.values())
    latest_total = entry.seed_value + sum(
        (
            bucket.value
            for bucket in accepted
            if bucket.start >= entry.seed_effective_from
        ),
        Decimal("0"),
    )
    latest_accepted_at = max((bucket.end for bucket in accepted), default=None)
    updated = SpGroupLedgerEntry(
        sensor_key=entry.sensor_key,
        source=entry.source,
        utility_type=entry.utility_type,
        epoch_id=entry.epoch_id,
        seed_value=entry.seed_value,
        seed_effective_from=entry.seed_effective_from,
        backfill_from=entry.backfill_from,
        latest_total=latest_total,
        latest_accepted_at=latest_accepted_at,
        accepted=tuple(sorted(accepted, key=lambda item: item.start)),
        pending=tuple(sorted(pending, key=lambda item: item.start)),
        warnings=tuple(warnings),
        source_backfill_attempted_at=entry.source_backfill_attempted_at,
        source_backfill_status=entry.source_backfill_status,
        source_backfill_started_at=entry.source_backfill_started_at,
        source_backfill_succeeded_at=entry.source_backfill_succeeded_at,
        source_backfill_failed_at=entry.source_backfill_failed_at,
        last_source_poll_succeeded_at=entry.last_source_poll_succeeded_at,
        statistics_imported_through=entry.statistics_imported_through,
        statistics_reference_start=entry.statistics_reference_start,
        statistics_reference_sum_offset=entry.statistics_reference_sum_offset,
        statistics_strategy_version=entry.statistics_strategy_version,
        statistics_warnings=entry.statistics_warnings,
    )
    return updated if changed or updated.latest_total != entry.latest_total else entry


def _copy_entry_with_warnings(
    entry: SpGroupLedgerEntry,
    warnings: tuple[str, ...],
) -> SpGroupLedgerEntry:
    return SpGroupLedgerEntry(
        sensor_key=entry.sensor_key,
        source=entry.source,
        utility_type=entry.utility_type,
        epoch_id=entry.epoch_id,
        seed_value=entry.seed_value,
        seed_effective_from=entry.seed_effective_from,
        backfill_from=entry.backfill_from,
        latest_total=entry.latest_total,
        latest_accepted_at=entry.latest_accepted_at,
        accepted=entry.accepted,
        pending=entry.pending,
        warnings=warnings,
        source_backfill_attempted_at=entry.source_backfill_attempted_at,
        source_backfill_status=entry.source_backfill_status,
        source_backfill_started_at=entry.source_backfill_started_at,
        source_backfill_succeeded_at=entry.source_backfill_succeeded_at,
        source_backfill_failed_at=entry.source_backfill_failed_at,
        last_source_poll_succeeded_at=entry.last_source_poll_succeeded_at,
        statistics_imported_through=entry.statistics_imported_through,
        statistics_reference_start=entry.statistics_reference_start,
        statistics_reference_sum_offset=entry.statistics_reference_sum_offset,
        statistics_strategy_version=entry.statistics_strategy_version,
        statistics_warnings=entry.statistics_warnings,
    )


def _entry_from_config(
    sensor_key: str,
    config: SpGroupSourceConfig,
    *,
    warnings: tuple[str, ...] = (),
) -> SpGroupLedgerEntry:
    return SpGroupLedgerEntry(
        sensor_key=sensor_key,
        source=config.source,
        utility_type=config.utility_type,
        epoch_id=config.epoch_id,
        seed_value=config.seed_value,
        seed_effective_from=config.seed_effective_from,
        backfill_from=config.backfill_from,
        latest_total=config.seed_value,
        warnings=warnings,
    )


def closed_month_ready(bucket_end: datetime, now: datetime) -> bool:
    """Return whether a monthly bucket is outside the configured grace period."""
    ready_at = bucket_end.astimezone(SINGAPORE_TZ) + timedelta(days=_WATER_GRACE_DAYS)
    return now.astimezone(SINGAPORE_TZ) >= ready_at


def historical_state_at(
    entry: SpGroupLedgerEntry,
    at: datetime,
) -> Decimal | None:
    """Return a bridged cumulative state at a historical point."""
    point = at.astimezone(SINGAPORE_TZ)
    seed = entry.seed_effective_from
    if point > seed:
        return None
    buckets_by_start = {
        bucket.start.astimezone(SINGAPORE_TZ): bucket for bucket in entry.accepted
    }
    current = point
    total = Decimal("0")
    while current < seed:
        bucket = buckets_by_start.get(current)
        if bucket is None:
            return None
        total += bucket.value
        current = bucket.end.astimezone(SINGAPORE_TZ)
    if current != seed:
        return None
    return entry.seed_value - total


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise LedgerError("invalid_datetime") from err
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SINGAPORE_TZ)
    return parsed.astimezone(SINGAPORE_TZ)


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    return _parse_datetime(value)


def _parse_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as err:
        raise LedgerError("invalid_decimal") from err
    if not parsed.is_finite():
        raise LedgerError("invalid_decimal")
    return parsed


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise LedgerError(f"missing_{key}")
    return value


def _optional_epoch_id(payload: dict[str, Any]) -> str:
    value = payload.get("epoch_id")
    if isinstance(value, str) and value:
        return value
    seed_material = "|".join(
        (
            _required_string(payload, "sensor_key"),
            _required_string(payload, "source"),
            _required_string(payload, "seed_value"),
            _required_string(payload, "seed_effective_from"),
        )
    )
    return sha256(seed_material.encode()).hexdigest()[:16]


__all__ = [
    "LedgerError",
    "SpGroupBucketObservation",
    "SpGroupLedgerEntry",
    "SpGroupLedgerSnapshot",
    "SpGroupLedgerStore",
    "WARNING_REPAIR_REBUILT_MALFORMED_LEDGER",
    "WARNING_LEGACY_BACKFILL_FROM_DEFAULTED",
    "WARNING_USER_REBUILT_LEDGER",
    "SOURCE_BACKFILL_FAILED",
    "SOURCE_BACKFILL_NEVER_ATTEMPTED",
    "SOURCE_BACKFILL_RUNNING",
    "SOURCE_BACKFILL_SUCCEEDED",
    "WARNING_SOURCE_BACKFILL_INTERRUPTED",
    "closed_month_ready",
    "historical_state_at",
]
