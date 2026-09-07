"""Tests for the configuration-reconciliation helpers and console-script entry points."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

import graphviz_mcp_setup as setup


def test_upsert_mcp_json_creates_and_preserves(tmp_path: Path) -> None:
    path = tmp_path / ".claude.json"
    path.write_text(
        json.dumps(
            {
                "someUnrelatedSetting": True,
                "mcpServers": {"latex": {"type": "stdio", "command": "py", "args": ["l.py"]}},
            }
        ),
        encoding="utf-8",
    )
    entry = setup._server_entry()

    assert setup._upsert_mcp_json(path, entry) is True
    data = json.loads(path.read_text(encoding="utf-8"))

    # Unrelated setting and the other server survive; graphviz is added.
    assert data["someUnrelatedSetting"] is True
    assert data["mcpServers"]["latex"]["command"] == "py"
    assert data["mcpServers"]["graphviz"] == entry

    # Re-running is a no-op (idempotent, no duplicate entries).
    assert setup._upsert_mcp_json(path, entry) is False


def test_upsert_codex_toml_replaces_stale_and_preserves(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "# header comment\n"
        'model = "gpt-5"\n\n'
        "[mcp_servers.latex]\n"
        'command = "py"\n'
        'args = ["l.py"]\n\n'
        "[mcp_servers.graphviz]\n"
        'command = "STALE"\n'
        'args = ["old.py"]\n'
        "enabled = false\n\n"
        "[history]\n"
        'persistence = "save-all"\n',
        encoding="utf-8",
    )
    entry = setup._server_entry()

    assert setup._upsert_codex_toml(path, entry, table_header="mcp_servers.graphviz") is True
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))

    # Unrelated content preserved.
    assert parsed["model"] == "gpt-5"
    assert parsed["mcp_servers"]["latex"]["command"] == "py"
    assert parsed["history"]["persistence"] == "save-all"

    # Stale graphviz entry fully replaced (no leftover enabled=false, canonical command).
    gv = parsed["mcp_servers"]["graphviz"]
    assert gv["command"] == entry["command"]
    assert gv["args"] == entry["args"]
    assert gv["enabled"] is True
    assert gv["startup_timeout_sec"] == setup.CODEX_STARTUP_TIMEOUT_SEC

    # There is exactly one graphviz table header.
    text = path.read_text(encoding="utf-8")
    assert text.count("[mcp_servers.graphviz]") == 1

    # Idempotent.
    assert setup._upsert_codex_toml(path, entry, table_header="mcp_servers.graphviz") is False


def test_strip_toml_table_keeps_dotted_subtable() -> None:
    text = (
        "[mcp_servers.graphviz]\n"
        'command = "x"\n\n'
        "[mcp_servers.graphviz.env]\n"
        'FOO = "bar"\n\n'
        "[other]\n"
        "k = 1\n"
    )
    out = setup._strip_toml_table(text, "mcp_servers.graphviz")
    assert "[mcp_servers.graphviz.env]" in out
    assert "[other]" in out
    # The exact table header line is gone.
    assert "[mcp_servers.graphviz]\n" not in out


def test_upsert_marked_section_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "AGENTS.md"
    path.write_text("Existing agent notes.\n", encoding="utf-8")

    assert setup._upsert_agents_section(path) is True
    first = path.read_text(encoding="utf-8")
    assert "Existing agent notes." in first
    assert setup._AGENTS_BEGIN in first

    # Second run does not change the file or duplicate the section.
    assert setup._upsert_agents_section(path) is False
    assert path.read_text(encoding="utf-8").count(setup._AGENTS_BEGIN) == 1


def test_vscode_main_writes_all_artifacts(tmp_path: Path) -> None:
    rc = setup.vscode_main([str(tmp_path)])
    assert rc == 0

    assert (tmp_path / ".mcp.json").is_file()
    assert (tmp_path / ".codex" / "config.toml").is_file()
    assert (tmp_path / ".claude" / "rules" / "graphviz.md").is_file()
    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / "README.md").is_file()

    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "graphviz_publish_markdown" in readme

    # The JSON config validates and targets the server script.
    data = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert data["mcpServers"]["graphviz"]["args"][0].endswith("server.py")


def test_vscode_main_missing_dir_returns_error(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    assert setup.vscode_main([str(missing)]) == 2


def test_find_graphviz_and_install_command_shapes() -> None:
    # find_graphviz returns either None or an existing file.
    found = setup.find_graphviz()
    assert found is None or found.is_file()
    # The install command, when known, is a non-empty argument list.
    command = setup._install_command()
    assert command is None or (isinstance(command, list) and command)


def test_version_ok_boundary() -> None:
    minimum = setup.MINIMUM_GRAPHVIZ_VERSION
    below = minimum[:-1] + (minimum[-1] - 1,)
    above = minimum[:-1] + (minimum[-1] + 1,)
    assert setup._version_ok(minimum) is True
    assert setup._version_ok(above) is True
    assert setup._version_ok(below) is False
    # An unparseable/unknown version must not force an upgrade of a working install.
    assert setup._version_ok(None) is True


def test_install_command_install_vs_upgrade_differ_when_supported() -> None:
    install = setup._install_command(upgrade=False)
    upgrade = setup._install_command(upgrade=True)
    # Both are None together (no package manager) or both are lists.
    assert (install is None) == (upgrade is None)
    if install is not None:
        assert isinstance(upgrade, list) and upgrade
        # winget/brew/dnf/apt use a distinct verb or flag for upgrades.
        if install[0] in {"winget", "brew", "dnf"} or "apt-get" in install[0:2]:
            assert install != upgrade


def test_dot_version_parses_real_executable() -> None:
    found = setup.find_graphviz()
    if found is None:
        pytest.skip("GraphViz not installed in this environment")
    version = setup._dot_version(found)
    # Either a numeric tuple or None (unparseable); when parsed it is non-empty ints.
    assert version is None or (isinstance(version, tuple) and all(isinstance(p, int) for p in version) and version)


def test_ensure_graphviz_offers_but_does_not_run_without_flag(monkeypatch) -> None:
    fake = Path("dot")
    monkeypatch.setattr(setup, "find_graphviz", lambda: fake)
    # Report a version below the minimum.
    below = setup.MINIMUM_GRAPHVIZ_VERSION[:-1] + (setup.MINIMUM_GRAPHVIZ_VERSION[-1] - 1,)
    monkeypatch.setattr(setup, "_dot_version", lambda _p: below)
    # A recognizable install command so the offer path is exercised.
    monkeypatch.setattr(setup, "_install_command", lambda upgrade=False: ["pkg", "upgrade"])

    ran: list[list[str]] = []
    monkeypatch.setattr(setup, "_run_install", lambda cmd: ran.append(cmd) or True)

    result = setup._ensure_graphviz(auto_install=False)
    assert result == fake
    assert ran == []  # nothing installed without the flag

    result = setup._ensure_graphviz(auto_install=True)
    assert ran == [["pkg", "upgrade"]]  # upgrade attempted with the flag
