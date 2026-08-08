"""Packaging surfaces that drift silently: the registry manifest vs the package.

server.json is what the MCP Registry serves to clients; pyproject.toml is what PyPI
serves. They name the same release, so a version bump that touches one but not the
other would publish a manifest pointing at a package version that does not exist.
This test makes that a red build instead of a broken listing.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_registry_manifest_matches_the_package() -> None:
    manifest = json.loads((ROOT / "server.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    assert manifest["version"] == project["version"]
    [package] = manifest["packages"]
    assert package["version"] == project["version"]
    assert package["identifier"] == project["name"]
    assert package["registryType"] == "pypi"
    # The hosted remote must be the real production URL — a wrong one in the registry
    # sends every discovering client to a dead endpoint.
    assert manifest["remotes"][0]["url"] == "https://pse.sakayandgo.com/mcp"
