"""Unit and integration tests for M4 (Antigravity CLI MCP plugin) & M5 (Hermes Agent v2 plugin)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANTIGRAVITY_PLUGIN_DIR = PROJECT_ROOT / ".agents" / "plugins" / "jobsuche"
_REPO_HERMES_PLUGIN_DIR = PROJECT_ROOT / "plugins" / "hermes" / "jobsuche"
_HOME_HERMES_PLUGIN_DIR = Path.home() / ".hermes" / "plugins" / "jobsuche"
HERMES_PLUGIN_DIR = _HOME_HERMES_PLUGIN_DIR if _HOME_HERMES_PLUGIN_DIR.is_dir() else _REPO_HERMES_PLUGIN_DIR


def test_antigravity_plugin_manifest_mcp_config_and_skill() -> None:
    plugin_json_path = ANTIGRAVITY_PLUGIN_DIR / "plugin.json"
    mcp_config_path = ANTIGRAVITY_PLUGIN_DIR / "mcp_config.json"
    mcp_server_path = ANTIGRAVITY_PLUGIN_DIR / "mcp_server.py"
    rules_path = ANTIGRAVITY_PLUGIN_DIR / "rules" / "AGENTS.md"
    skill_path = ANTIGRAVITY_PLUGIN_DIR / "skills" / "jobsuche" / "SKILL.md"

    for p in (plugin_json_path, mcp_config_path, mcp_server_path, rules_path, skill_path):
        assert p.is_file(), f"Missing Antigravity plugin file: {p}"

    manifest = json.loads(plugin_json_path.read_text(encoding="utf-8"))
    assert manifest["name"] == "jobsuche"
    assert manifest.get("version")
    assert manifest.get("description")

    mcp_cfg = json.loads(mcp_config_path.read_text(encoding="utf-8"))
    assert "mcpServers" in mcp_cfg and len(mcp_cfg["mcpServers"]) >= 1

    skill_text = skill_path.read_text(encoding="utf-8")
    assert skill_text.startswith("---\n")
    fm = yaml.safe_load(skill_text.split("---\n", 2)[1])
    assert fm["name"] == "jobsuche"


def test_hermes_plugin_manifest_and_register(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest_path = HERMES_PLUGIN_DIR / "plugin.yaml"
    init_path = HERMES_PLUGIN_DIR / "__init__.py"
    skill_path = HERMES_PLUGIN_DIR / "skills" / "jobsuche" / "SKILL.md"
    readme_path = HERMES_PLUGIN_DIR / "README.md"

    for p in (manifest_path, init_path, skill_path, readme_path):
        assert p.is_file(), f"Missing Hermes plugin file: {p}"

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 2
    assert manifest["kind"] == "standalone"
    assert manifest["name"] == "jobsuche"
    expected_tools = {
        "jobsuche_search",
        "jobsuche_monitor",
        "jobsuche_export_llm",
        "jobsuche_status",
    }
    assert set(manifest["provides_tools"]) == expected_tools

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_home"))
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins_jobsuche_test",
        str(init_path),
        submodule_search_locations=[str(HERMES_PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    mod: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules["hermes_plugins_jobsuche_test"] = mod
    spec.loader.exec_module(mod)

    registered_tools: dict[str, dict[str, Any]] = {}
    registered_skills: dict[str, dict[str, Any]] = {}

    class DummyCtx:
        def register_tool(
            self,
            name: str,
            toolset: str,
            schema: dict[str, Any],
            handler: Any,
            **kwargs: Any,
        ) -> None:
            registered_tools[name] = {
                "toolset": toolset,
                "schema": schema,
                "handler": handler,
                **kwargs,
            }

        def register_skill(
            self,
            name: str,
            path: str | Path,
            description: str = "",
            frontmatter: dict[str, Any] | None = None,
        ) -> None:
            registered_skills[name] = {
                "path": Path(path),
                "description": description,
                "frontmatter": frontmatter or {},
            }

        def register_hook(self, *args: Any, **kwargs: Any) -> None:
            return None

        def get_config(self, key: str, default: Any = None) -> Any:
            return default

        def on_unload(self, callback: Any) -> None:
            return None

    mod.register(DummyCtx())
    assert set(registered_tools.keys()) == expected_tools
    assert "jobsuche" in registered_skills
    assert registered_skills["jobsuche"]["path"].is_file()
