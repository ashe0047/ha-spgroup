"""Test SP Group packaging and SDK import boundaries."""

from __future__ import annotations

import ast
import importlib
import json
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest
from homeassistant.helpers.service import _SERVICES_SCHEMA
from homeassistant.util.yaml import load_yaml_dict

PROJECT_ROOT = Path(__file__).parents[3]
INTEGRATION_ROOT = PROJECT_ROOT / "custom_components" / "sp_group"

PUBLIC_MOBILE_EXPORTS = (
    "MobileAuthClient",
    "JarvisClient",
    "ProfileClient",
    "YggdrasilClient",
    "MobileSession",
    "SpGroupMobileError",
)


def _python_files(root: Path) -> Iterable[Path]:
    """Return Python source files under root."""
    return sorted(
        path for path in root.rglob("*.py") if "__pycache__" not in path.parts
    )


def test_custom_component_does_not_import_root_or_private_sdk_modules() -> None:
    """HA code must not couple to root compatibility or SDK internals."""
    violations: list[str] = []

    for path in _python_files(INTEGRATION_ROOT):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "spgroup":
                        violations.append(f"{path}: import spgroup")
                    if alias.name.startswith("spgroup.mobile."):
                        violations.append(f"{path}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module == "spgroup":
                    violations.append(f"{path}: from spgroup import ...")
                if module.startswith("spgroup.mobile."):
                    violations.append(f"{path}: from {module} import ...")

    assert violations == []


def test_spgroup_mobile_public_exports_are_importable() -> None:
    """The public mobile SDK symbols expected by HA are importable."""
    mobile = importlib.import_module("spgroup.mobile")

    for export in PUBLIC_MOBILE_EXPORTS:
        assert hasattr(mobile, export)


def test_spgroup_mobile_imports_in_subprocess() -> None:
    """The active Python environment can import the SDK like HA will."""
    code = (
        "from spgroup.mobile import "
        "MobileAuthClient, JarvisClient, ProfileClient, YggdrasilClient, "
        "MobileSession, SpGroupMobileError; "
        "print('ok')"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "ok"


@pytest.mark.parametrize(
    ("field", "expected"),
    (
        ("domain", "sp_group"),
        ("name", "SP Group"),
        ("version", "0.2.0"),
        ("config_flow", True),
        ("iot_class", "cloud_polling"),
        ("integration_type", "hub"),
        ("requirements", ["spgroup>=0.1.0"]),
    ),
)
def test_manifest_packaging_boundary(field: str, expected: object) -> None:
    """Manifest preserves the selected HA custom integration boundary."""
    manifest = json.loads((INTEGRATION_ROOT / "manifest.json").read_text())

    assert manifest[field] == expected


def test_manifest_config_flow_matches_file_structure() -> None:
    """A config-flow manifest must be backed by config_flow.py."""
    manifest = json.loads((INTEGRATION_ROOT / "manifest.json").read_text())

    assert manifest["config_flow"] is True
    assert (INTEGRATION_ROOT / "config_flow.py").is_file()


def test_services_yaml_describes_registered_dev_service() -> None:
    """Registered services must have valid HA service metadata."""
    services_path = INTEGRATION_ROOT / "services.yaml"

    services = _SERVICES_SCHEMA(load_yaml_dict(str(services_path)))

    assert services["dev_force_statistics_compile"] == {"fields": {}}
