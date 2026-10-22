"""Test SP Group manifest metadata."""

import json
from pathlib import Path


def test_manifest_is_custom_cloud_polling_integration() -> None:
    """Manifest contains the expected custom integration metadata."""
    manifest = json.loads(
        Path("custom_components/sp_group/manifest.json").read_text()
    )

    assert manifest["domain"] == "sp_group"
    assert manifest["name"] == "SP Group"
    assert manifest["version"] == "0.2.1"
    assert manifest["config_flow"] is True
    assert manifest["iot_class"] == "cloud_polling"
    assert manifest["integration_type"] == "hub"
    assert manifest["requirements"] == ["spgroup>=0.1.0"]
