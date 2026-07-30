# HACS Integration Alignment Checklist

This document captures the requirements for publishing a custom integration
through HACS (Home Assistant Community Store). Use it as a reusable reference
for this integration and any future ones added to this workspace.

---

## 1. Repository Structure

HACS requires exactly **one integration per repository**:

```
repo-root/
  hacs.json                    ← HACS manifest (required)
  README.md                    ← User-facing documentation (required)
  custom_components/
    <domain>/                  ← Named after the integration domain
      __init__.py              ← Integration entry point
      manifest.json            ← HA manifest (required)
      brand/                   ← Brand assets directory
        logo.png               ← Logo (required for defaults)
        icon.png               ← Icon in brand directory (required for defaults)
      ...                       ← All other integration files
  .github/                     ← Optional: GitHub workflows, PR templates
  docs/                        ← Optional: additional documentation
  tests/                       ← Optional: test suite
```

**Key rule from HACS:**
> _There must only be one integration per repository, i.e. there can only be one
> subdirectory to `ROOT_OF_THE_REPO/custom_components/`. If there are more than
> one, only the first one will be managed._

All files needed for the integration to run must live inside
`custom_components/<domain>/`.

---

## 2. HACS Manifest (`hacs.json`)

Required at the repository root. The `name` key is the minimum, but additional
keys improve the user experience.

### Supported Keys

| Key | Required | Type | Description |
|-----|----------|------|-------------|
| `name` | Yes | string | Display name shown in HACS UI |
| `content_in_root` | No | bool | `true` if `custom_components/<domain>/` is at the repo root (default: `false`, meaning it's in a subdirectory) |
| `country` | No | string | ISO 3166-1 alpha-2 country code (e.g. `"SG"`) |
| `homeassistant` | No | string | Minimum HA version required (e.g. `"2026.7.2"`) |
| `persistent_directory` | No | string | Relative path within the integration directory that HACS preserves across upgrades (e.g. `"ledger"`) |
| `render_readme` | No | bool | Whether to render the README inside the HACS integration detail view |
| `zip_release` | No | bool | Whether release assets are zipped. Requires `filename` key |
| `filename` | No | string | Name of the file HACS should look for (required with `zip_release`) |
| `hide_default_branch` | No | bool | Hide the default branch from the version selector |

### Example

```json
{
    "name": "SP Group",
    "country": "SG",
    "homeassistant": "2026.7.2",
    "render_readme": true,
    "persistent_directory": "ledger"
}
```

---

## 3. HA Manifest (`manifest.json`)

Required inside `custom_components/<domain>/`. Must minimally define `domain`
and `name`. For custom integrations, `version` is also required.

### Required Keys

| Key | Description |
|-----|-------------|
| `domain` | Short name with characters and underscores. Must match the directory name. Cannot be changed once set. |
| `name` | Human-readable integration name |
| `version` | **Required for custom integrations.** Valid version per PEP 440 (e.g. `"0.2.0"`) |

### Commonly Used Optional Keys

| Key | Description |
|-----|-------------|
| `codeowners` | GitHub usernames of maintainers (e.g. `["@ashe0047"]`) |
| `config_flow` | `true` if the integration uses a config flow for setup |
| `dependencies` | HA integration domains this integration depends on |
| `after_dependencies` | HA integration domains to set up before this one (non-essential) |
| `documentation` | URL to the integration's documentation page |
| `integration_type` | `"hub"`, `"device"`, `"entity"`, `"hardware"`, `"virtual"`, or `"service"` |
| `iot_class` | IoT connectivity class (`"cloud_polling"`, `"local_polling"`, `"assumed_state"`, `"cloud_push"`, `"local_push"`) |
| `issue_tracker` | URL to GitHub issues for the integration |
| `quality_scale` | HA Quality Scale level (`"bronze"`, `"silver"`, `"gold"`, `"platinum"`, `"legacy"`, `"internal"`) |
| `requirements` | Python package dependencies (e.g. `["spgroup>=1.0.0"]`) |

### Example

```json
{
    "domain": "sp_group",
    "name": "SP Group",
    "codeowners": ["@ashe0047"],
    "config_flow": true,
    "dependencies": [],
    "documentation": "https://github.com/ashe0047/spgroup-ha",
    "integration_type": "hub",
    "iot_class": "cloud_polling",
    "quality_scale": "bronze",
    "requirements": ["spgroup>=0.1.0"],
    "version": "0.2.0"
}
```

See the [official HA manifest documentation](https://developers.home-assistant.io/docs/creating_integration_manifest/)
for the complete reference.

---

## 4. Brand Assets

HACS validation checks for brand assets in the integration directory.

### Required Files (for default repository submissions)

| File | Location | Format |
|------|----------|--------|
| `logo.png` | `custom_components/<domain>/brand/logo.png` | Square PNG |
| `icon.png` | `custom_components/<domain>/brand/icon.png` | Square PNG |

The `brand/` directory must live **inside** the integration directory
(`custom_components/<domain>/brand/`), not at the repo root.

Assets are optional for custom repositories but required for default repository
inclusion.

---

## 5. README.md

Required at the repository root. Should cover:

- What the integration does
- Prerequisites (accounts, hardware, etc.)
- Installation via HACS (with HACS badge)
- Configuration instructions
- Supported devices/features
- Known limitations
- Troubleshooting

HACS displays the README in the integration detail view when
`render_readme: true` is set in `hacs.json`.

### HACS Badge

```markdown
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
```

---

## 6. GitHub Repository Settings

These must be configured on GitHub directly (not in the repo files):

| Setting | Requirement |
|---------|-------------|
| **Description** | A short description of the integration. Used in HACS discovery. |
| **Topics** | At least relevant topics (e.g. `hacs`, `home-assistant`, `sp-group`, `singapore`, `energy`, `homeassistant`) |
| **Issues** | Must be enabled. Required for user bug reports. |
| **Visibility** | Public repository |
| **Releases** | At least one release. Preferred for HACS version management. |

### Release Guidelines

- Use semantic versioning (e.g. `v0.2.0`, `v1.0.0`)
- Each release should contain complete integration code
- HACS presents the 5 latest releases to users during download/upgrade
- If releases are not published, HACS will use the default branch files

---

## 7. HACS Validation Checklist

Use this checklist before submitting a new integration:

### Repository Structure
- [ ] Repo has exactly one integration (one `custom_components/<domain>/`)
- [ ] `custom_components/` directory exists at repo root
- [ ] All runtime files live inside `custom_components/<domain>/`
- [ ] `hacs.json` exists at repo root with at least `name`

### HA Manifest (`custom_components/<domain>/manifest.json`)
- [ ] `domain` matches the directory name
- [ ] `name` is set
- [ ] `version` is set (required for custom integrations)
- [ ] `documentation` URL is valid and points to the correct repo
- [ ] `issue_tracker` URL is valid (if using one)
- [ ] `requirements` list is accurate

### Brand Assets
- [ ] `brand/logo.png` exists in integration directory (required for defaults)
- [ ] `brand/icon.png` exists in integration directory (required for defaults)

### README
- [ ] `README.md` exists at repo root
- [ ] Installation instructions are included
- [ ] HACS badge is shown (optional but recommended)

### GitHub Settings
- [ ] Repository is public
- [ ] Repository has a description
- [ ] Repository has topics set
- [ ] Issues are enabled
- [ ] At least one release is published

---

## 8. HACS Validation Tools

Before submitting a PR to add your repo as a HACS default:

1. **Run the local HACS validation:**
   HACS itself runs automated checks during the review process, including:
   - Repo structure validation
   - Manifest validation
   - Brand asset validation
   - Single-integration check

2. **Use the hacs-pyscript action (optional):**
   The [hacs-pyscript](https://github.com/hacs/integration) is a custom
   integration that validates HACS repository structure.

3. **Manual review checklist:**
   Before a PR, HACS maintainers will check:
   - [ ] Repo is not archived
   - [ ] Repo has at least one release
   - [ ] Submitter is the owner or a major contributor
   - [ ] All automated checks pass
   - [ ] Integration has a brand directory with at least `icon.png`

---

## 9. Per-Integration Checklist Template

Copy this for each new integration when setting up:

```
### <Integration Name>

- [ ] **hacs.json** created at repo root
- [ ] **manifest.json** has `domain`, `name`, `version`, `documentation`
- [ ] `documentation` URL points to the correct GitHub repo
- [ ] `issue_tracker` URL points to GitHub issues
- [ ] **Brand assets** (`brand/logo.png`, `brand/icon.png`) exist
- [ ] **README.md** covers installation, configuration, known limitations
- [ ] **Version** bumped for initial HACS-ready release
- [ ] **GitHub settings**: description, topics, issues enabled
- [ ] **At least one release** published on GitHub
- [ ] **Tests** pass (`mise run python:test`)
- [ ] **Lint** passes (`mise run python:lint`)
```

---

## References

- [HACS Publish Docs — Integration](https://hacs.xyz/docs/publish/integration/)
- [HACS Publish Docs — General](https://hacs.xyz/docs/publish/start/)
- [HACS Repository Types FAQ](https://hacs.xyz/docs/faq/repository_types/)
- [HA Manifest Documentation](https://developers.home-assistant.io/docs/creating_integration_manifest/)
- [HA Integration File Structure](https://developers.home-assistant.io/docs/creating_integration_file_structure/)
- [HA Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
