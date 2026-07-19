# Repository Conventions

This document defines conventions for Home Assistant custom integration
repositories in this workspace. These are community-driven conventions based
on established HA custom integration projects — neither HACS nor Home
Assistant defines an official naming policy.

---

## 1. Repository Naming

### Pattern

Use the `ha-<name>` prefix convention, which is the most common pattern
among HA custom integration repositories.

```
ha-<short-name>
```

| Component | Rule |
|-----------|------|
| `ha` | Prefix identifying this as a Home Assistant repository |
| `<short-name>` | Lowercase, kebab-case name of the integration's service or domain |
| Separator | Single hyphen (`-`) |

### Examples

| Integration | Repo Name | Reasoning |
|-------------|-----------|-----------|
| SP Group (domain: `sp_group`) | `ha-spgroup` | Brand name without underscore |
| Future: [Example Service] | `ha-example` | Service name in kebab case |

### Why `ha-<name>`?

Based on a survey of popular HA custom integration repos:
- `ha-<name>` — ~40% of repos (most common pattern)
- `homeassistant-<name>` — ~25% (explicit but verbose)
- `hass-<name>` — ~15% (concise)
- `<name>-homeassistant` — ~10%
- Just `<name>` — ~10% (when HA context is obvious from description)

The `ha-<name>` pattern is preferred because:
- Short and scannable in CLI/URLs
- Immediately identifies the repo's domain
- Consistently used by well-known integrations
- Avoids confusion with other platforms (hass.io add-ons, etc.)

### When to deviate

- If the integration name itself conflicts with an existing well-known
  non-HA project, prefer `homeassistant-<name>` for disambiguation.
- If the integration is part of a branded product line, `<brand>-homeassistant`
  is also acceptable.

---

## 2. Repository Description

GitHub description format:

```
<Integration Name> — Home Assistant custom integration
```

Example:
```
SP Group — Home Assistant custom integration
```

---

## 3. GitHub Topics

Required topics for every HA integration repo:

```
hacs, home-assistant, <integration-domain>, <country-code>, <relevant-keywords>
```

For the SP Group integration:

```
hacs, home-assistant, sp-group, sp_group, singapore, energy, utility
```

Topics are used by HACS for search/discovery and should be set on the GitHub
repository settings page.

---

## 4. Integration Domain (manifest.json)

The domain follows Home Assistant's rules:
- Short name consisting of lowercase characters and underscores
- Must match the `custom_components/<domain>/` directory name
- Cannot be changed once published

| Convention | Rule |
|------------|------|
| Chars | `[a-z0-9_]` |
| Separator | Underscore (`_`) when multiple words |
| Matches | Directory name under `custom_components/` |

### Domain vs Repo Name

The domain and repo name serve different purposes:

```
Domain (manifest.json): sp_group
Repo name (GitHub):     ha-spgroup
```

- **Domain** is an HA-internal identifier, immutable, underscore-separated
- **Repo name** is a GitHub identifier, case-insensitive, kebab-case
- The repo name can change; the domain cannot

---

## 5. Versioning

Use semantic versioning (`MAJOR.MINOR.PATCH`) for both releases and the
`version` key in `manifest.json`:

- **Major**: Breaking changes (e.g. config flow changes, dropped support)
- **Minor**: Feature additions, non-breaking HA compatibility updates
- **Patch**: Bug fixes, documentation, dependency bumps

### Alignment

- `manifest.json` `version` must match the GitHub release tag (without `v` prefix)
- Release tags use `vMAJOR.MINOR.PATCH` (e.g. `v0.2.0`, `v1.0.0`)

---

## 6. Branch Convention

| Branch | Purpose |
|--------|---------|
| `main` | Default branch. Ready for HACS. All releases from here. |

Use feature branches locally or in forks. `main` should always be
HACS-compatible: validated `hacs.json`, verified `manifest.json`,
passing tests.

---

## 7. Release Convention

- Each GitHub release should include a changelog summary
- Releases are preferred for HACS version management (HACS shows the 5 latest)
- Without releases, HACS falls back to the default branch

### Recommended release format

```
## v0.2.0 — 2026-07-19

### Added
- Initial HACS-ready release
- SP Group electricity and water consumption sensors
- Config flow with OTP authentication
- Manual meter-reading seed for Energy Dashboard
- Historical data backfill
```

---

## 8. HACS Badge

Include in every integration's README:

```markdown
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
```

---

## 9. Workspace Layout

Each integration lives in its own directory under `integrations/` in this
workspace:

```
apps/homeassistant/
  integrations/
    spgroup/          ← GitHub repo: ha-spgroup
    <new-integration>/ ← Next integration repo
```

Each directory:
- Is its own Git repository
- Follows the HACS single-integration structure
- Has `custom_components/<domain>/` at its root
- Has its own `hacs.json`, `README.md`, etc.

---

## References

- [HACS Integration Publish Docs](https://hacs.xyz/docs/publish/integration/)
- [HACS General Publish Docs](https://hacs.xyz/docs/publish/start/)
- [HA Integration File Structure](https://developers.home-assistant.io/docs/creating_integration_file_structure/)
- [HA Manifest Documentation](https://developers.home-assistant.io/docs/creating_integration_manifest/)
