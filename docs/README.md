# SP Group Documentation Index

This directory documents the SP Group Home Assistant integration. The current
implementation and the normative ADRs are authoritative; issue reports and
plans are not behavioral contracts unless their status explicitly says so.

## Naming convention

Documentation filenames use lowercase kebab-case and a type prefix when the
directory does not already provide the type:

```text
docs/adr/adr-<domain>-<decision>.md
docs/issues/issue-<domain>-<problem>.md
docs/plans/plan-<domain>-<change>.md
docs/guides/<subject>.md
docs/references/<domain>-<subject>.md
docs/testing/<subject>.md
```

`README.md` and the append-only `context-tracker.md` are conventional
exceptions. Renames must update repository-relative links and historical
references in the same change; compatibility stubs are not retained after a
successful link migration.

## Authority map

| Topic | Canonical document |
| --- | --- |
| Electricity gaps, no-data, provider failures | `adr/adr-electricity-gap-and-no-data-semantics.md` |
| Partial-month AMI/History precedence | `adr/adr-hybrid-backfill-partial-month.md` |
| Ledger acceptance, cursor, and coverage lifecycle | `adr/adr-ledger-bucket-lifecycle.md` and `adr/adr-polling-window-exact-end.md` |
| Provider-owned statistics and Recorder reconciliation | `adr/adr-provider-owned-external-statistics.md` |
| Sensor state and entity contract | `adr/adr-sensor-platform.md` |
| Water cadence and monthly closure | `adr/adr-water-polling.md` |
| Coordinator failure and polling boundaries | `adr/adr-coordinator-hierarchy.md` |
| Authentication/session persistence | `adr/adr-auth-session-management.md` |
| Config flow behavior | `adr/adr-config-flow-wizard.md` |
| Backfill lifecycle and repairs | `adr/adr-backfill-once-then-repair.md` |
| Test/development helpers | `adr/adr-developer-tools.md` and `guides/testing.md` |

When documents overlap, the canonical document above wins. Superseded ADRs and
historical narratives remain discoverable but must not be used to infer current
behavior.

## Maintained guidance

- [Developer guidelines](guides/development.md)
- [Testing guidelines](guides/testing.md)
- [HA runtime validation](testing/ha-runtime-validation.md)
- [Integration roadmap](guides/ha-integration-roadmap.md)
- [Payload contracts](guides/ha-payload-contracts.md)
- [HACS checklist](guides/hacs-integration-checklist.md)
- [Repository conventions](guides/repo-conventions.md)
- [Manual seed design](guides/manual-seed-ami-accumulation-design.md)
- [Energy Dashboard direction](references/ha-energy-dashboard-direction.md)
- [Statistics and sensor interaction](references/ha-statistics-sensor-interaction.md)

## Lifecycle classifications

Each ADR, issue, and plan has a `Status` and `Authority` section. The allowed
classifications are `Normative`, `Maintained`, `Active`, `Implemented`,
`Closed`, `Superseded`, `Historical`, and `Needs implementation audit`.

### ADRs

All files under `adr/` are indexed by the authority map above. The AMI-zero
and seed-statistics ADRs contain historical amendments; their current sections
point to the canonical replacement decisions.

### Issues

Issues are investigations and operational records. Active or unresolved items
must identify a next action. Closed and historical items retain their evidence
and resolution but do not define runtime behavior.

### Plans

Plans describe implementation work and must be marked `Active`, `Implemented`,
or `Superseded`. An implemented plan must name the production code and tests
that verify it.

## Complete inventory

### Top-level and testing

`context-tracker.md` is an append-only decision log and is intentionally not
rewritten or lifecycle-normalized. Other top-level and testing documents are
maintained guidance:

`guides/development.md`, `guides/ha-integration-roadmap.md`, `guides/ha-payload-contracts.md`,
`guides/hacs-integration-checklist.md`, `guides/manual-seed-ami-accumulation-design.md`,
`guides/repo-conventions.md`, `guides/testing.md`, `testing/ha-runtime-validation.md`.

Classification: all of the files in this group are `Maintained`.

### ADRs

`adr-ami-zero-coverage-semantics.md`, `adr-auth-session-management.md`,
`adr-backfill-once-then-repair.md`, `adr-config-flow-wizard.md`,
`adr-coordinator-hierarchy.md`, `adr-developer-tools.md`,
`adr-electricity-gap-and-no-data-semantics.md`,
`adr-hybrid-backfill-partial-month.md`, `adr-ledger-bucket-lifecycle.md`,
`adr-polling-window-exact-end.md`, `adr-provider-owned-external-statistics.md`,
`adr-seed-statistics-alignment.md`, `adr-sensor-platform.md`,
`adr-two-sighting-removal.md`, `adr-water-polling.md`.

Classification: current implementation ADRs are `Normative`;
`adr-ami-zero-coverage-semantics.md` and `adr-seed-statistics-alignment.md`
are `Superseded`/`Historical` where their amendments identify older behavior.

| ADR file | Classification |
| --- | --- |
| `adr-ami-zero-coverage-semantics.md` | Superseded / Historical |
| `adr-auth-session-management.md` | Normative |
| `adr-backfill-once-then-repair.md` | Normative |
| `adr-config-flow-wizard.md` | Normative |
| `adr-coordinator-hierarchy.md` | Normative |
| `adr-developer-tools.md` | Normative |
| `adr-electricity-gap-and-no-data-semantics.md` | Normative |
| `adr-hybrid-backfill-partial-month.md` | Normative |
| `adr-ledger-bucket-lifecycle.md` | Normative |
| `adr-polling-window-exact-end.md` | Normative |
| `adr-provider-owned-external-statistics.md` | Normative |
| `adr-seed-statistics-alignment.md` | Superseded / Historical |
| `adr-sensor-platform.md` | Normative |
| `adr-two-sighting-removal.md` | Normative |
| `adr-water-polling.md` | Normative |

### Issues

`issue-auth-refresh-reload-cycle.md`, `issue-auth0-session-timeout.md`,
`issue-ha-config-flow-jarvis-contestable-correlation.md`,
`issue-ha-config-flow-snake-case-labels.md`,
`issue-ha-config-flow-stale-already-in-progress.md`,
`issue-historical-seed-backfill-retry-robustness.md`,
`issue-july-23-stats-gap-polling-cursor.md`, `issue-phase-a-setup-blocking.md`,
`issue-production-readiness-checklist.md`,
`issue-source-timeline-recovery-and-water-parity.md`,
`issue-statistics-recorder-readback-false-conflict.md`,
`issue-statistics-seed-boundary-hour-gap.md`.

Classification:

- `issue-auth-refresh-reload-cycle.md` and `issue-statistics-seed-boundary-hour-gap.md` — `Needs implementation audit`.
- `issue-production-readiness-checklist.md` — `Active`.
- Closed config-flow incidents — `Closed`.
- Resolved cursor, timeline, provider-failure, retry, and Recorder incidents — `Implemented` or `Historical`.

| Issue file | Classification |
| --- | --- |
| `issue-auth-refresh-reload-cycle.md` | Needs implementation audit |
| `issue-auth0-session-timeout.md` | Historical |
| `issue-ha-config-flow-jarvis-contestable-correlation.md` | Closed |
| `issue-ha-config-flow-snake-case-labels.md` | Closed |
| `issue-ha-config-flow-stale-already-in-progress.md` | Closed |
| `issue-historical-seed-backfill-retry-robustness.md` | Implemented / Historical |
| `issue-july-23-stats-gap-polling-cursor.md` | Historical |
| `issue-phase-a-setup-blocking.md` | Historical / Implemented |
| `issue-production-readiness-checklist.md` | Active |
| `issue-source-timeline-recovery-and-water-parity.md` | Implemented |
| `issue-statistics-recorder-readback-false-conflict.md` | Implemented |
| `issue-statistics-seed-boundary-hour-gap.md` | Needs implementation audit |

### Plans

`plan-auth-store-refactor.md`, `plan-backfill-once-refactor.md`, `plan-phase-a-refactor.md`,
`plan-provider-owned-external-statistics.md`,
`plan-statistics-backfill-full-coverage-hardening.md`,
`plan-statistics-recorder-readback-fix.md`.

Classification:

- `plan-auth-store-refactor.md` and `plan-statistics-backfill-full-coverage-hardening.md` — `Needs implementation audit`.
- `plan-backfill-once-refactor.md` — `Implemented` pending final verification wording.
- `plan-phase-a-refactor.md` — `Implemented`.
- `plan-provider-owned-external-statistics.md` and `plan-statistics-recorder-readback-fix.md` — `Superseded`/`Implemented`.

| Plan file | Classification |
| --- | --- |
| `plan-auth-store-refactor.md` | Needs implementation audit |
| `plan-backfill-once-refactor.md` | Implemented |
| `plan-phase-a-refactor.md` | Implemented |
| `plan-provider-owned-external-statistics.md` | Superseded |
| `plan-statistics-backfill-full-coverage-hardening.md` | Needs implementation audit |
| `plan-statistics-recorder-readback-fix.md` | Superseded / Implemented |

### References

`ha-energy-dashboard-direction.md`, `ha-statistics-sensor-interaction.md`.

Classification: both reference documents are `Maintained`.

## Maintenance rules

- Update the canonical ADR before changing dependent guidance.
- Mark completed or replaced plans/issues; do not silently delete evidence.
- Keep `context-tracker.md` append-only.
- Use repository-relative links and verify them with the documentation checker.
- Current behavior claims must name the relevant implementation and test paths.
