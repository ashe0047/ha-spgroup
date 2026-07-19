# SP Group Home Assistant Integration

Home Assistant custom integration for SP Group (Singapore Power) utility data.
This integration consumes the [spgroup SDK](../../../python-packages/spgroup/) as its
mobile API transport layer.

## Commands

This repo is pinned to Python `3.13` through `mise.toml`.

```bash
mise run python:install
mise run python:test
mise run python:lint
mise run ha:check
```

## SDK Dependency

The SDK is installed from a sibling checkout during development:

```bash
python -m pip install -e ../../../python-packages/spgroup
```

`mise run python:install` runs that command together with HA test/lint tooling.

The HA integration also includes a vendored SDK fallback at
`custom_components/sp_group/vendor/spgroup/` for environments where the SDK is
not pip-installed. This is a frozen snapshot; SDK updates require re-vendoring.
For SDK development, see the [SDK repo](../../../python-packages/spgroup/).
