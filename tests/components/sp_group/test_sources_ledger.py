"""Test SP Group manual seed source and ledger rules."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from custom_components.sp_group.discovery import (
    SpGroupCoordinatorData,
    SpGroupFutureSensorKey,
    SpGroupPremise,
    SpGroupUtilityAccount,
)
from custom_components.sp_group.ledger import (
    SOURCE_BACKFILL_FAILED,
    SOURCE_BACKFILL_NEVER_ATTEMPTED,
    SOURCE_BACKFILL_RUNNING,
    SOURCE_BACKFILL_SUCCEEDED,
    SOURCE_COVERAGE_COVERED,
    WARNING_SOURCE_BACKFILL_INTERRUPTED,
    WARNING_USER_REBUILT_LEDGER,
    SpGroupBucketObservation,
    SpGroupLedgerEntry,
    SpGroupLedgerSnapshot,
    SpGroupSourceCoverage,
    SpGroupUnresolvedRange,
    closed_month_ready,
    historical_state_at,
)
from custom_components.sp_group.sources import (
    SINGAPORE_TZ,
    SOURCE_MANUAL_SEED_AMI,
    SOURCE_MANUAL_SEED_MONTHLY,
    build_source_config,
    next_half_hour_boundary,
    next_month_boundary,
    source_configs_for_data,
)
from homeassistant.core import HomeAssistant


def test_seed_boundaries_are_derived_without_overlap() -> None:
    """Seed effective dates move to the next non-overlapping bucket."""
    half_hour, aligned = next_half_hour_boundary(
        datetime(2026, 7, 15, 13, 17, tzinfo=SINGAPORE_TZ)
    )
    month, month_aligned = next_month_boundary(
        datetime(2026, 7, 15, 10, 0, tzinfo=SINGAPORE_TZ)
    )

    assert half_hour == datetime(2026, 7, 15, 13, 30, tzinfo=SINGAPORE_TZ)
    assert not aligned
    assert month == datetime(2026, 8, 1, 0, 0, tzinfo=SINGAPORE_TZ)
    assert not month_aligned


async def test_ledger_publishes_seed_then_accepts_two_sighting_buckets(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """A seed is trusted immediately; AMI buckets need two stable sightings."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-1")
    await store.async_load()
    config = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_electric",
            premise_key="opaque_premise",
            utility_type="electric",
        ),
        seed_value="100.0",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    snapshot = await store.async_initialize_sources({"opaque_electric": config})

    entry = snapshot.entries["opaque_electric"]
    assert entry.latest_total == Decimal("100.0")
    assert entry.accepted == ()

    observation = SpGroupBucketObservation(
        bucket_key="2026-07-15T13:00:00+08:00",
        start=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
        end=datetime(2026, 7, 15, 13, 30, tzinfo=SINGAPORE_TZ),
        value=Decimal("1.25"),
    )
    snapshot = await store.async_apply_observations(
        "opaque_electric",
        (observation,),
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=True,
    )
    assert snapshot.entries["opaque_electric"].latest_total == Decimal("100.0")
    assert len(snapshot.entries["opaque_electric"].pending) == 1

    snapshot = await store.async_apply_observations(
        "opaque_electric",
        (observation,),
        now=datetime(2026, 7, 15, 20, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=True,
    )
    assert snapshot.entries["opaque_electric"].latest_total == Decimal("101.25")
    assert len(snapshot.entries["opaque_electric"].accepted) == 1


async def test_source_backfill_state_transitions_and_interrupted_recovery(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """Source backfill state moves through running, failed, and succeeded."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-source-state")
    await store.async_load()
    config = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_electric",
            premise_key="opaque_premise",
            utility_type="electric",
        ),
        seed_value="100.0",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": config})

    snapshot = await store.async_mark_source_backfill_running(
        sensor_keys={"opaque_electric"},
        started_at=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    assert (
        snapshot.entries["opaque_electric"].source_backfill_status
        == SOURCE_BACKFILL_RUNNING
    )

    snapshot = await store.async_mark_interrupted_source_backfills_failed(
        failed_at=datetime(2026, 7, 15, 15, 0, tzinfo=SINGAPORE_TZ),
    )
    entry = snapshot.entries["opaque_electric"]
    assert entry.source_backfill_status == SOURCE_BACKFILL_FAILED
    assert WARNING_SOURCE_BACKFILL_INTERRUPTED in entry.warnings

    snapshot = await store.async_mark_source_backfill_succeeded(
        sensor_keys={"opaque_electric"},
        succeeded_at=datetime(2026, 7, 15, 16, 0, tzinfo=SINGAPORE_TZ),
    )
    entry = snapshot.entries["opaque_electric"]
    assert entry.source_backfill_status == SOURCE_BACKFILL_SUCCEEDED
    assert entry.source_backfill_attempted_at == datetime(
        2026, 7, 15, 16, 0, tzinfo=SINGAPORE_TZ
    )
    assert entry.last_source_poll_succeeded_at == datetime(
        2026, 7, 15, 16, 0, tzinfo=SINGAPORE_TZ
    )


def test_legacy_source_backfill_attempt_migrates_conservatively() -> None:
    """Legacy attempted timestamps only imply success when data evidence exists."""
    base = {
        "sensor_key": "opaque_electric",
        "source": SOURCE_MANUAL_SEED_AMI,
        "utility_type": "electric",
        "epoch_id": "epoch",
        "seed_value": "100",
        "seed_effective_from": "2026-07-15T13:00:00+08:00",
        "backfill_from": "2026-07-15T13:00:00+08:00",
        "latest_total": "100",
        "source_backfill_attempted_at": "2026-07-15T14:00:00+08:00",
        "accepted": [],
        "pending": [],
    }

    assert (
        SpGroupLedgerEntry.from_dict(base).source_backfill_status
        == SOURCE_BACKFILL_NEVER_ATTEMPTED
    )
    assert (
        SpGroupLedgerEntry.from_dict(
            {
                **base,
                "statistics_imported_through": "2026-07-15T14:00:00+08:00",
            }
        ).source_backfill_status
        == SOURCE_BACKFILL_SUCCEEDED
    )


async def test_source_poll_cursor_derives_from_pending_then_accepted(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """Polling cursor is derived from persisted pending/accepted buckets."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-cursor")
    await store.async_load()
    config = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_electric",
            premise_key="opaque_premise",
            utility_type="electric",
        ),
        seed_value="100",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 18, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": config})
    observation = SpGroupBucketObservation(
        bucket_key="bucket-1500",
        start=datetime(2026, 7, 15, 15, 0, tzinfo=SINGAPORE_TZ),
        end=datetime(2026, 7, 15, 15, 30, tzinfo=SINGAPORE_TZ),
        value=Decimal("1"),
    )
    snapshot = await store.async_apply_observations(
        "opaque_electric",
        (observation,),
        now=datetime(2026, 7, 15, 16, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=True,
    )
    assert snapshot.entries["opaque_electric"].source_poll_cursor_start() == datetime(
        2026, 7, 15, 15, 0, tzinfo=SINGAPORE_TZ
    )

    snapshot = await store.async_apply_observations(
        "opaque_electric",
        (observation,),
        now=datetime(2026, 7, 15, 17, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=True,
    )
    assert snapshot.entries["opaque_electric"].source_poll_cursor_start() == datetime(
        2026, 7, 15, 15, 30, tzinfo=SINGAPORE_TZ
    )


async def test_source_poll_cursor_prioritizes_proven_unresolved_range(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """An older proven gap is retried before pending or accepted data."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-unresolved-cursor")
    await store.async_load()
    config = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_electric",
            premise_key="opaque_premise",
            utility_type="electric",
        ),
        seed_value="100",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 18, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": config})
    gap = SpGroupUnresolvedRange(
        cadence="30m",
        start=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
        end=datetime(2026, 7, 15, 14, 30, tzinfo=SINGAPORE_TZ),
        reason="missing_internal_bucket",
        first_seen_at=datetime(2026, 7, 15, 18, 0, tzinfo=SINGAPORE_TZ),
        last_seen_at=datetime(2026, 7, 15, 18, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_apply_observations(
        "opaque_electric",
        (),
        now=datetime(2026, 7, 15, 18, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=True,
        coverage=SpGroupSourceCoverage(
            status=SOURCE_COVERAGE_COVERED,
            start=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
            end=datetime(2026, 7, 15, 15, 0, tzinfo=SINGAPORE_TZ),
            unresolved_ranges=(gap,),
        ),
    )
    entry = store.snapshot.entries["opaque_electric"]
    assert entry.unresolved_ranges == (gap,)
    assert entry.source_poll_cursor_start() == gap.start


async def test_accepted_correction_is_recorded_without_mutating_trusted_total(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """Provider corrections remain explicit repair work."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-correction")
    await store.async_load()
    config = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_electric",
            premise_key="opaque_premise",
            utility_type="electric",
        ),
        seed_value="100",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 18, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": config})
    observation = SpGroupBucketObservation(
        bucket_key="bucket-1500",
        start=datetime(2026, 7, 15, 15, 0, tzinfo=SINGAPORE_TZ),
        end=datetime(2026, 7, 15, 15, 30, tzinfo=SINGAPORE_TZ),
        value=Decimal("1"),
    )
    await store.async_apply_observations(
        "opaque_electric",
        (observation,),
        now=datetime(2026, 7, 15, 16, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=False,
    )
    corrected = SpGroupBucketObservation(
        bucket_key=observation.bucket_key,
        start=observation.start,
        end=observation.end,
        value=Decimal("0.5"),
    )
    snapshot = await store.async_apply_observations(
        "opaque_electric",
        (corrected,),
        now=datetime(2026, 7, 15, 17, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=False,
    )
    entry = snapshot.entries["opaque_electric"]
    assert entry.latest_total == Decimal("101")
    assert entry.accepted[0].value == Decimal("1")
    assert entry.corrections[0].candidate_value == Decimal("0.5")
    assert "corrected_bucket:bucket-1500" in entry.warnings


def test_ledger_round_trip_preserves_recovery_metadata() -> None:
    """New recovery fields round-trip without exposing raw payloads."""
    entry = SpGroupLedgerEntry(
        sensor_key="opaque_electric",
        source=SOURCE_MANUAL_SEED_AMI,
        utility_type="electric",
        epoch_id="epoch",
        seed_value=Decimal("100"),
        seed_effective_from=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
        backfill_from=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
        latest_total=Decimal("100"),
        unresolved_ranges=(
            SpGroupUnresolvedRange(
                cadence="30m",
                start=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
                end=datetime(2026, 7, 15, 14, 30, tzinfo=SINGAPORE_TZ),
                reason="missing_internal_bucket",
                first_seen_at=datetime(2026, 7, 15, 18, 0, tzinfo=SINGAPORE_TZ),
                last_seen_at=datetime(2026, 7, 15, 18, 0, tzinfo=SINGAPORE_TZ),
            ),
        ),
        source_coverage_status=SOURCE_COVERAGE_COVERED,
    )
    restored = SpGroupLedgerEntry.from_dict(entry.to_dict())
    assert restored.unresolved_ranges == entry.unresolved_ranges
    assert restored.source_coverage_status == SOURCE_COVERAGE_COVERED
    assert "payload" not in str(entry.to_dict()).lower()


async def test_ledger_subtracts_pre_seed_history_and_adds_post_seed_buckets(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """Backfill overlap equations are anchored at the physical seed."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-equations")
    await store.async_load()
    sensor_key = SpGroupFutureSensorKey(
        key="opaque_electric",
        premise_key="opaque_premise",
        utility_type="electric",
    )
    config = build_source_config(
        sensor_key=sensor_key,
        seed_value="10000",
        seed_observed_at="2026-07-15T03:30:00+08:00",
        backfill_from="2026-07-15T02:00:00+08:00",
        now=datetime(2026, 7, 15, 6, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": config})
    observations = (
        SpGroupBucketObservation(
            bucket_key="pre-0200",
            start=datetime(2026, 7, 15, 2, 0, tzinfo=SINGAPORE_TZ),
            end=datetime(2026, 7, 15, 2, 30, tzinfo=SINGAPORE_TZ),
            value=Decimal("20"),
        ),
        SpGroupBucketObservation(
            bucket_key="pre-0230",
            start=datetime(2026, 7, 15, 2, 30, tzinfo=SINGAPORE_TZ),
            end=datetime(2026, 7, 15, 3, 0, tzinfo=SINGAPORE_TZ),
            value=Decimal("30"),
        ),
        SpGroupBucketObservation(
            bucket_key="pre-0300",
            start=datetime(2026, 7, 15, 3, 0, tzinfo=SINGAPORE_TZ),
            end=datetime(2026, 7, 15, 3, 30, tzinfo=SINGAPORE_TZ),
            value=Decimal("50"),
        ),
        SpGroupBucketObservation(
            bucket_key="post-0330",
            start=datetime(2026, 7, 15, 3, 30, tzinfo=SINGAPORE_TZ),
            end=datetime(2026, 7, 15, 4, 0, tzinfo=SINGAPORE_TZ),
            value=Decimal("5"),
        ),
        SpGroupBucketObservation(
            bucket_key="post-0400",
            start=datetime(2026, 7, 15, 4, 0, tzinfo=SINGAPORE_TZ),
            end=datetime(2026, 7, 15, 4, 30, tzinfo=SINGAPORE_TZ),
            value=Decimal("7"),
        ),
        SpGroupBucketObservation(
            bucket_key="post-0430",
            start=datetime(2026, 7, 15, 4, 30, tzinfo=SINGAPORE_TZ),
            end=datetime(2026, 7, 15, 5, 0, tzinfo=SINGAPORE_TZ),
            value=Decimal("3"),
        ),
    )

    snapshot = await store.async_apply_observations(
        "opaque_electric",
        observations,
        now=datetime(2026, 7, 15, 6, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=False,
    )

    entry = snapshot.entries["opaque_electric"]
    assert entry.latest_total == Decimal("10015")
    assert historical_state_at(
        entry,
        datetime(2026, 7, 15, 3, 0, tzinfo=SINGAPORE_TZ),
    ) == Decimal("9950")
    assert historical_state_at(
        entry,
        datetime(2026, 7, 15, 2, 30, tzinfo=SINGAPORE_TZ),
    ) == Decimal("9920")
    assert historical_state_at(
        entry,
        datetime(2026, 7, 15, 2, 0, tzinfo=SINGAPORE_TZ),
    ) == Decimal("9900")
    assert entry.latest_total != Decimal("10115")


async def test_accepted_bucket_corrections_do_not_rewrite_total(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """Historical source corrections are warnings, not silent rewrites."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-2")
    await store.async_load()
    config = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_electric",
            premise_key="opaque_premise",
            utility_type="electric",
        ),
        seed_value="100.0",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": config})
    first = SpGroupBucketObservation(
        bucket_key="bucket-1",
        start=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
        end=datetime(2026, 7, 15, 13, 30, tzinfo=SINGAPORE_TZ),
        value=Decimal("1.25"),
    )
    await store.async_apply_observations(
        "opaque_electric",
        (first,),
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=False,
    )
    corrected = SpGroupBucketObservation(
        bucket_key="bucket-1",
        start=first.start,
        end=first.end,
        value=Decimal("9.99"),
    )
    snapshot = await store.async_apply_observations(
        "opaque_electric",
        (corrected,),
        now=datetime(2026, 7, 16, 14, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=False,
    )

    entry = snapshot.entries["opaque_electric"]
    assert entry.latest_total == Decimal("101.25")
    assert "corrected_bucket:bucket-1" in entry.warnings


async def test_ledger_deduplicates_observation_keys_within_one_batch(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """One endpoint response cannot accept multiple rows for one bucket key."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-duplicate-batch")
    await store.async_load()
    config = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_water",
            premise_key="opaque_premise",
            utility_type="water",
        ),
        seed_value="100.0",
        seed_observed_at="2026-07-01T00:00:00+08:00",
        backfill_from="2026-06-01T00:00:00+08:00",
        now=datetime(2026, 7, 15, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_water": config})
    duplicate_observations = (
        SpGroupBucketObservation(
            bucket_key="2026-06",
            start=datetime(2026, 6, 1, tzinfo=SINGAPORE_TZ),
            end=datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ),
            value=Decimal("2.5"),
        ),
        SpGroupBucketObservation(
            bucket_key="2026-06",
            start=datetime(2026, 6, 1, tzinfo=SINGAPORE_TZ),
            end=datetime(2026, 7, 1, tzinfo=SINGAPORE_TZ),
            value=Decimal("3.5"),
        ),
    )

    snapshot = await store.async_apply_observations(
        "opaque_water",
        duplicate_observations,
        now=datetime(2026, 7, 15, tzinfo=SINGAPORE_TZ),
        require_two_sightings=False,
    )

    entry = snapshot.entries["opaque_water"]
    assert len(entry.accepted) == 1
    assert entry.accepted[0].value == Decimal("2.5")
    assert entry.latest_total == Decimal("100.0")
    assert entry.warnings == ("duplicate_observation_keys",)


def test_malformed_ledger_snapshot_is_not_trusted() -> None:
    """Malformed persisted state is marked unsafe instead of partially used."""
    snapshot = SpGroupLedgerSnapshot.from_dict({"entries": {"x": {"bad": "data"}}})

    assert snapshot.malformed
    assert snapshot.entries == {}


def test_source_configs_must_match_current_discovery_identity() -> None:
    """Manually edited source options cannot bind to the wrong utility."""
    data = SpGroupCoordinatorData(
        premises=(
            SpGroupPremise(
                key="opaque_premise",
                active=True,
                signed_up=True,
                shared=False,
                contestable_type=None,
                accounts=(
                    SpGroupUtilityAccount(
                        key="opaque_account",
                        premise_key="opaque_premise",
                        utility_types=("ELECTRIC",),
                    ),
                ),
            ),
        )
    )
    sensor_key = data.future_sensor_keys()[0]
    valid = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )

    assert source_configs_for_data(
        data,
        {"source_config": {sensor_key.key: valid.to_dict()}},
    ) == {sensor_key.key: valid}

    wrong_premise = valid.to_dict() | {"premise_key": "other_premise"}
    wrong_utility = valid.to_dict() | {
        "utility_type": "water",
        "source": SOURCE_MANUAL_SEED_MONTHLY,
    }
    wrong_source = valid.to_dict() | {"source": SOURCE_MANUAL_SEED_MONTHLY}
    missing_discovery_key = valid.to_dict() | {
        "source": SOURCE_MANUAL_SEED_AMI,
    }

    for payload in (wrong_premise, wrong_utility, wrong_source):
        assert source_configs_for_data(
            data,
            {"source_config": {sensor_key.key: payload}},
        ) == {}
    assert source_configs_for_data(
        data,
        {"source_config": {"unknown_sensor": missing_discovery_key}},
    ) == {}


def test_monthly_grace_policy() -> None:
    """Water monthly buckets need the closed-month grace period."""
    bucket_end = datetime(2026, 8, 1, tzinfo=SINGAPORE_TZ)

    assert not closed_month_ready(
        bucket_end,
        bucket_end + timedelta(days=2, hours=23),
    )
    assert closed_month_ready(bucket_end, bucket_end + timedelta(days=3))


def test_ledger_repr_is_privacy_safe() -> None:
    """Ledger models use opaque keys and do not expose raw SP identifiers."""
    entry = SpGroupLedgerEntry(
        sensor_key="opaque_sensor",
        source="manual_seed_plus_ami_deltas",
        utility_type="electric",
        epoch_id="epoch",
        seed_value=Decimal("100"),
        seed_effective_from=datetime(2026, 7, 15, tzinfo=SINGAPORE_TZ),
        backfill_from=datetime(2026, 7, 15, tzinfo=SINGAPORE_TZ),
        latest_total=Decimal("100"),
    )

    text = repr(entry)
    assert "ACC-1" not in text
    assert "PREM-1" not in text
    assert "JARVIS" not in text


async def test_ledger_blocks_unconfirmed_lower_replacement(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """A manually edited lower replacement config cannot lower the ledger."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-3")
    await store.async_load()
    sensor_key = SpGroupFutureSensorKey(
        key="opaque_electric",
        premise_key="opaque_premise",
        utility_type="electric",
    )
    first = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": first})
    lower = build_source_config(
        sensor_key=sensor_key,
        seed_value="50.0",
        seed_observed_at="2026-07-15T13:30:00+08:00",
        backfill_from="2026-07-15T13:30:00+08:00",
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    snapshot = await store.async_initialize_sources({"opaque_electric": lower})

    entry = snapshot.entries["opaque_electric"]
    assert entry.latest_total == Decimal("100.0")
    assert "blocked_unconfirmed_lower_replacement" in entry.warnings


async def test_ledger_accepts_confirmed_lower_replacement(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """An explicitly confirmed replacement starts a new reset epoch."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-4")
    await store.async_load()
    sensor_key = SpGroupFutureSensorKey(
        key="opaque_electric",
        premise_key="opaque_premise",
        utility_type="electric",
    )
    first = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": first})
    replacement = build_source_config(
        sensor_key=sensor_key,
        seed_value="50.0",
        seed_observed_at="2026-07-15T13:30:00+08:00",
        backfill_from="2026-07-15T13:30:00+08:00",
        reset_confirmed=True,
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    snapshot = await store.async_initialize_sources(
        {"opaque_electric": replacement}
    )

    entry = snapshot.entries["opaque_electric"]
    assert entry.latest_total == Decimal("50.0")
    assert entry.accepted == ()
    assert entry.pending == ()
    assert entry.epoch_id == replacement.epoch_id
    assert entry.warnings == ("user_confirmed_lower_replacement",)


async def test_ledger_rebuild_resets_selected_entry_to_stored_seed(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """An explicit rebuild clears accepted and pending buckets for one sensor."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-rebuild")
    await store.async_load()
    sensor_key = SpGroupFutureSensorKey(
        key="opaque_electric",
        premise_key="opaque_premise",
        utility_type="electric",
    )
    config = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": config})
    await store.async_apply_observations(
        "opaque_electric",
        (
            SpGroupBucketObservation(
                bucket_key="bucket-1",
                start=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
                end=datetime(2026, 7, 15, 13, 30, tzinfo=SINGAPORE_TZ),
                value=Decimal("1.25"),
            ),
        ),
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=False,
    )

    snapshot = await store.async_rebuild_sources(
        {"opaque_electric": config},
        sensor_keys={"opaque_electric"},
    )

    entry = snapshot.entries["opaque_electric"]
    assert entry.latest_total == Decimal("100.0")
    assert entry.accepted == ()
    assert entry.pending == ()
    assert entry.warnings == (WARNING_USER_REBUILT_LEDGER,)


async def test_legacy_source_and_ledger_epoch_fallbacks_match(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """Legacy options and ledger entries do not reset on upgrade."""
    from custom_components.sp_group.ledger import (
        SpGroupAcceptedBucket,
        SpGroupLedgerStore,
    )
    from custom_components.sp_group.sources import SpGroupSourceConfig

    hass.config.config_dir = str(tmp_path)
    sensor_key = "opaque_electric"
    source_payload = {
        "enabled": True,
        "premise_key": "opaque_premise",
        "utility_type": "electric",
        "source": "manual_seed_plus_ami_deltas",
        "seed_value": "100.0",
        "seed_observed_at": "2026-07-15T13:00:00+08:00",
        "seed_effective_from": "2026-07-15T13:00:00+08:00",
        "boundary_aligned": True,
    }
    config = SpGroupSourceConfig.from_dict(sensor_key, source_payload)
    accepted = SpGroupAcceptedBucket(
        bucket_key="bucket-1",
        start=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
        end=datetime(2026, 7, 15, 13, 30, tzinfo=SINGAPORE_TZ),
        value=Decimal("1.25"),
        first_seen_at=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
        accepted_at=datetime(2026, 7, 15, 20, 0, tzinfo=SINGAPORE_TZ),
    )
    legacy_snapshot = SpGroupLedgerSnapshot.from_dict(
        {
            "entries": {
                sensor_key: {
                    "sensor_key": sensor_key,
                    "source": "manual_seed_plus_ami_deltas",
                    "utility_type": "electric",
                    "seed_value": "100.0",
                    "seed_effective_from": "2026-07-15T13:00:00+08:00",
                    "latest_total": "101.25",
                    "accepted": [accepted.to_dict()],
                    "pending": [],
                    "warnings": [],
                }
            }
        }
    )
    store = SpGroupLedgerStore(hass, "entry-5")
    store._snapshot = legacy_snapshot  # noqa: SLF001

    snapshot = await store.async_initialize_sources({sensor_key: config})

    entry = snapshot.entries[sensor_key]
    assert entry.latest_total == Decimal("101.25")
    assert entry.accepted == (accepted,)
    assert entry.warnings == ("legacy_backfill_from_defaulted",)


async def test_ledger_save_failure_does_not_publish_unpersisted_total(
    hass: HomeAssistant,
    tmp_path,  # noqa: ANN001
) -> None:
    """In-memory ledger state is not advanced before storage succeeds."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-6")
    await store.async_load()
    sensor_key = SpGroupFutureSensorKey(
        key="opaque_electric",
        premise_key="opaque_premise",
        utility_type="electric",
    )
    config = build_source_config(
        sensor_key=sensor_key,
        seed_value="100.0",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": config})
    observation = SpGroupBucketObservation(
        bucket_key="bucket-1",
        start=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
        end=datetime(2026, 7, 15, 13, 30, tzinfo=SINGAPORE_TZ),
        value=Decimal("1.25"),
    )

    async def _fail_save(payload: object) -> None:
        raise OSError("disk full")

    store._store.async_save = _fail_save  # type: ignore[method-assign] # noqa: SLF001
    try:
        await store.async_apply_observations(
            "opaque_electric",
            (observation,),
            now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
            require_two_sightings=False,
        )
    except OSError:
        pass

    assert store.snapshot.entries["opaque_electric"].latest_total == Decimal("100.0")
    assert store.snapshot.entries["opaque_electric"].accepted == ()


async def test_ledger_source_poll_cursor_falls_back_to_seed_when_empty(
    hass: HomeAssistant,
    tmp_path,
) -> None:
    """An empty ledger entry returns seed_effective_from as the cursor."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore
    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-empty-cursor")
    await store.async_load()
    config = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_electric",
            premise_key="opaque_premise",
            utility_type="electric",
        ),
        seed_value="100",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 18, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": config})
    entry = store.snapshot.entries["opaque_electric"]
    assert entry.pending == ()
    assert entry.accepted == ()
    assert entry.unresolved_ranges == ()
    assert entry.source_poll_cursor_start() == datetime(
        2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ
    )
    assert entry.source_poll_cursor_start() == datetime(
        2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ
    )


async def test_ledger_concurrent_apply_observations_lock_serializes(
    hass: HomeAssistant,
    tmp_path,
) -> None:
    """Concurrent apply calls are serialized by the ledger lock without data loss."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore
    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-concurrent")
    await store.async_load()
    config = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_electric",
            premise_key="opaque_premise",
            utility_type="electric",
        ),
        seed_value="100",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 18, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": config})
    bucket_a = SpGroupBucketObservation(
        bucket_key="bucket-a",
        start=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
        end=datetime(2026, 7, 15, 13, 30, tzinfo=SINGAPORE_TZ),
        value=Decimal("1.0"),
    )
    bucket_b = SpGroupBucketObservation(
        bucket_key="bucket-b",
        start=datetime(2026, 7, 15, 13, 30, tzinfo=SINGAPORE_TZ),
        end=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
        value=Decimal("2.0"),
    )
    async def _apply_a() -> None:
        await store.async_apply_observations(
            "opaque_electric",
            (bucket_a,),
            now=datetime(2026, 7, 15, 19, 0, tzinfo=SINGAPORE_TZ),
            require_two_sightings=False,
        )
    async def _apply_b() -> None:
        await store.async_apply_observations(
            "opaque_electric",
            (bucket_b,),
            now=datetime(2026, 7, 15, 19, 0, tzinfo=SINGAPORE_TZ),
            require_two_sightings=False,
        )
    await asyncio.gather(_apply_a(), _apply_b())
    entry = store.snapshot.entries["opaque_electric"]
    accepted_keys = {b.bucket_key for b in entry.accepted}
    assert "bucket-a" in accepted_keys
    assert "bucket-b" in accepted_keys
    assert entry.latest_total == Decimal("103.0")


async def test_ledger_concurrent_apply_observations_same_bucket(
    hass: HomeAssistant,
    tmp_path,
) -> None:
    """Concurrent apply calls for same bucket: first accepted, second records correction."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore
    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-same-bucket")
    await store.async_load()
    config = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_electric",
            premise_key="opaque_premise",
            utility_type="electric",
        ),
        seed_value="100",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 18, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": config})
    bucket_v1 = SpGroupBucketObservation(
        bucket_key="bucket-same",
        start=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
        end=datetime(2026, 7, 15, 13, 30, tzinfo=SINGAPORE_TZ),
        value=Decimal("1.0"),
    )
    bucket_v2 = SpGroupBucketObservation(
        bucket_key="bucket-same",
        start=datetime(2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ),
        end=datetime(2026, 7, 15, 13, 30, tzinfo=SINGAPORE_TZ),
        value=Decimal("2.0"),
    )
    async def _apply_v1() -> None:
        await store.async_apply_observations(
            "opaque_electric",
            (bucket_v1,),
            now=datetime(2026, 7, 15, 19, 0, tzinfo=SINGAPORE_TZ),
            require_two_sightings=False,
        )
    async def _apply_v2() -> None:
        await store.async_apply_observations(
            "opaque_electric",
            (bucket_v2,),
            now=datetime(2026, 7, 15, 19, 0, tzinfo=SINGAPORE_TZ),
            require_two_sightings=False,
        )
    await asyncio.gather(_apply_v1(), _apply_v2())
    entry = store.snapshot.entries["opaque_electric"]
    assert len(entry.accepted) == 1
    assert entry.accepted[0].value == Decimal("1.0")
    assert any("corrected_bucket:bucket-same" in w for w in entry.warnings)


async def test_ledger_preserves_cursor_on_retention_exceeded(
    hass: HomeAssistant,
    tmp_path,
) -> None:
    """A retention-exceeded coverage response does not advance the cursor."""
    from custom_components.sp_group.ledger import SpGroupLedgerStore

    hass.config.config_dir = str(tmp_path)
    store = SpGroupLedgerStore(hass, "entry-retention")
    await store.async_load()
    config = build_source_config(
        sensor_key=SpGroupFutureSensorKey(
            key="opaque_electric",
            premise_key="opaque_premise",
            utility_type="electric",
        ),
        seed_value="100",
        seed_observed_at="2026-07-15T13:00:00+08:00",
        backfill_from="2026-07-15T13:00:00+08:00",
        now=datetime(2026, 7, 15, 14, 0, tzinfo=SINGAPORE_TZ),
    )
    await store.async_initialize_sources({"opaque_electric": config})
    snapshot = await store.async_apply_observations(
        "opaque_electric",
        (),
        now=datetime(2026, 7, 15, 15, 0, tzinfo=SINGAPORE_TZ),
        require_two_sightings=False,
        coverage=SpGroupSourceCoverage(status="retention_exceeded"),
    )
    entry = snapshot.entries["opaque_electric"]
    assert entry.source_coverage_status == "retention_exceeded"
    assert entry.latest_total == Decimal("100")
    assert entry.accepted == ()
    assert entry.last_source_poll_succeeded_at is not None
    assert entry.source_poll_cursor_start() == datetime(
        2026, 7, 15, 13, 0, tzinfo=SINGAPORE_TZ
    )
