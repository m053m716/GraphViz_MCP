"""Configuration helpers and console-script entry points for GraphViz MCP.

This module powers three console scripts declared in ``pyproject.toml``:

* ``graphviz-mcp``          -- run the stdio MCP server (delegates to ``server.main``).
* ``graphviz-mcp-vscode``   -- write repository-local VS Code / Codex / Claude artifacts
                               (``.mcp.json``, ``.codex/config.toml``, and rule/AGENTS files).
* ``graphviz-mcp-install``  -- upsert the *global* user configuration for Claude Code
                               (``~/.claude.json``) and Codex (``~/.codex/config.toml``),
                               optionally detecting and installing GraphViz itself.

All configuration edits are reconciled in place: existing unrelated settings, servers,
comments, and formatting are preserved, and the GraphViz entry is upserted so repeated
runs never produce duplicates. A timestamped backup is written before any file is changed.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

# The MCP server identifier used as the key in every client configuration.
SERVER_NAME = "graphviz"

# Timeouts mirrored into the Codex configuration (seconds).
CODEX_STARTUP_TIMEOUT_SEC = 15
CODEX_TOOL_TIMEOUT_SEC = 120

# Minimum GraphViz release the server is verified against. When --install-graphviz is used
# and the discovered `dot` is older than this, an upgrade is attempted.
MINIMUM_GRAPHVIZ_VERSION = (10, 0, 1)

SERVER_ROOT = Path(__file__).resolve().parent
SERVER_SCRIPT = SERVER_ROOT / "server.py"


# ---------------------------------------------------------------------------
# Small shared utilities
# ---------------------------------------------------------------------------


def _log(message: str) -> None:
    """Write a status line to stderr so stdout stays free for machine output."""
    print(message, file=sys.stderr)


def _timestamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup(path: Path) -> Path | None:
    """Copy ``path`` to a timestamped sibling before it is modified in place."""
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}.{_timestamp()}.bak")
    shutil.copy2(path, backup)
    _log(f"  backup: {backup}")
    return backup


def _launch_python() -> Path:
    """Return the interpreter MCP clients should launch for the server.

    Preference order: an adjacent virtual environment (``.venv``) if present, then the
    interpreter currently running this script. The result is an absolute path so the
    generated configuration is portable to any client working directory.
    """
    for candidate in (
        SERVER_ROOT / ".venv" / "Scripts" / "python.exe",  # Windows venv
        SERVER_ROOT / ".venv" / "bin" / "python",  # POSIX venv
    ):
        if candidate.exists():
            return candidate.resolve()
    return Path(sys.executable).resolve()


def _server_entry() -> dict[str, Any]:
    """The canonical stdio launch entry shared by every client configuration."""
    return {
        "type": "stdio",
        "command": str(_launch_python()),
        "args": [str(SERVER_SCRIPT)],
    }


# ---------------------------------------------------------------------------
# JSON configuration (Claude Code: .claude.json, .mcp.json)
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


def _upsert_mcp_json(
    path: Path,
    entry: dict[str, Any],
    *,
    servers_key: str = "mcpServers",
) -> bool:
    """Merge the GraphViz stdio entry into a Claude-style JSON config.

    ``servers_key`` is ``mcpServers`` for both ``~/.claude.json`` and ``.mcp.json``.
    Unrelated top-level keys and other servers are preserved; the ``graphviz`` server is
    replaced with the canonical entry. Returns ``True`` when the file changed on disk.
    """
    data = _load_json(path)
    servers = data.get(servers_key)
    if not isinstance(servers, dict):
        servers = {}
    before = json.dumps(servers.get(SERVER_NAME), sort_keys=True)
    after = json.dumps(entry, sort_keys=True)
    servers[SERVER_NAME] = entry
    data[servers_key] = servers

    rendered = json.dumps(data, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == rendered:
        return False
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    verb = "updated" if before != after or before != "null" else "added"
    _log(f"  {verb} {SERVER_NAME} in {path}")
    return True


# ---------------------------------------------------------------------------
# TOML configuration (Codex: config.toml)
# ---------------------------------------------------------------------------


def _toml_escape(value: str) -> str:
    """Escape a string for a TOML basic (double-quoted) string."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _codex_table(table_header: str, entry: dict[str, Any]) -> str:
    """Render the ``[<table_header>]`` block for the GraphViz Codex server."""
    lines = [f"[{table_header}]"]
    command = _toml_escape(entry["command"])
    lines.append(f'command = "{command}"')
    args = ", ".join(f'"{_toml_escape(a)}"' for a in entry["args"])
    lines.append(f"args = [{args}]")
    lines.append(f'cwd = "{_toml_escape(str(SERVER_ROOT))}"')
    lines.append(f"startup_timeout_sec = {CODEX_STARTUP_TIMEOUT_SEC}")
    lines.append(f"tool_timeout_sec = {CODEX_TOOL_TIMEOUT_SEC}")
    lines.append("enabled = true")
    return "\n".join(lines) + "\n"


def _strip_toml_table(text: str, header: str) -> str:
    """Remove an existing ``[header]`` table (and its body) from TOML text.

    The table body runs from its header line up to the next table header at column 0 or
    end of file. Comment lines immediately preceding the header that belong to it are left
    in place; only the managed table body is removed so surrounding content is preserved.
    """
    pattern = re.compile(
        r"(?m)^\[" + re.escape(header) + r"\][^\n]*\n"  # the header line
        r"(?:(?!^\[).*\n?)*",  # body lines until next table header
    )
    return pattern.sub("", text)


def _upsert_codex_toml(path: Path, entry: dict[str, Any], *, table_header: str) -> bool:
    """Upsert ``[<table_header>]`` for GraphViz into a Codex ``config.toml``.

    Existing TOML content, comments, and other servers are preserved. ``tomllib`` validates
    the merged result. Returns ``True`` when the file changed on disk.
    """
    original = path.read_text(encoding="utf-8") if path.exists() else ""

    remainder = _strip_toml_table(original, table_header).rstrip()
    block = _codex_table(table_header, entry)
    if remainder:
        rendered = remainder + "\n\n" + block
    else:
        rendered = block

    # Validate the merged document before writing anything.
    tomllib.loads(rendered)

    if original == rendered:
        return False
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    _log(f"  upserted [{table_header}] in {path}")
    return True


# ---------------------------------------------------------------------------
# Rule / guidance files
# ---------------------------------------------------------------------------


RULE_TEXT = """\
# GraphViz diagrams

Use the GraphViz MCP tools for state machines, state-transition diagrams, flowcharts,
directed dependency graphs, and architecture/data-flow diagrams. Treat a version-controlled
`.dot` file as the source of truth and render SVG by default. Keep any single graph within
five rows, five columns, and 25 nodes; the tools enforce this and encapsulate larger graphs
into a linked parent/inner hierarchy. Do not create decorative diagrams or edit unrelated
Markdown.

## Typical usage patterns

- **Author from scratch:** call `graphviz_render_source` with `project_dir`, the DOT
  `dot_source`, and a repository-relative `dot_path` (e.g. `docs/diagrams/state.dot`). The
  `.dot` source is kept beside the rendered artifact.
- **Re-render an existing source:** call `graphviz_render` with `project_dir` and `dot_path`
  after editing the `.dot` file; choose `format` (`svg`/`png`/`pdf`) and `engine` as needed.
- **Check before committing:** call `graphviz_validate` and inspect
  `layout.requires_encapsulation` to see whether the graph exceeds the 5x5 / 25-node limit.
- **Publish into Markdown:** call `graphviz_publish_markdown` with the target `markdown_path`
  and an optional `anchor` (an exact heading or line) to insert/refresh one managed image
  block. Repeated calls update the block in place rather than duplicating it.
- **Refresh everything:** call `graphviz_sync` to re-render a source and, when `markdown_path`
  is supplied, refresh its managed Markdown block.
- **Inspect the environment:** call `graphviz_environment` (no arguments) to confirm the
  resolved `dot` path, version, engines, and enforced limits.
"""

# Sentinel markers so the guidance can be upserted inside a shared AGENTS.md without
# duplicating on repeated runs.
_AGENTS_BEGIN = "<!-- graphviz-mcp:begin -->"
_AGENTS_END = "<!-- graphviz-mcp:end -->"


def _write_rule_file(path: Path) -> bool:
    """Create or refresh a standalone rule file. Returns True when it changed."""
    rendered = RULE_TEXT
    if path.exists() and path.read_text(encoding="utf-8") == rendered:
        return False
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    _log(f"  wrote {path}")
    return True


def _upsert_marked_section(path: Path, body: str) -> bool:
    """Upsert a marker-delimited GraphViz section into a Markdown file.

    The section is delimited by hidden HTML comment markers so repeated runs replace it in
    place instead of appending duplicates. Content outside the markers is preserved. When the
    file does not exist it is created containing only the section. Returns True on change.
    """
    section = f"{_AGENTS_BEGIN}\n{body.rstrip()}\n{_AGENTS_END}\n"
    original = path.read_text(encoding="utf-8") if path.exists() else ""

    marker = re.compile(
        re.escape(_AGENTS_BEGIN) + r".*?" + re.escape(_AGENTS_END) + r"\n?",
        re.DOTALL,
    )
    if marker.search(original):
        rendered = marker.sub(section, original)
    elif original.strip():
        rendered = original.rstrip() + "\n\n" + section
    else:
        rendered = section

    if original == rendered:
        return False
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    _log(f"  upserted GraphViz section in {path}")
    return True


def _upsert_agents_section(path: Path) -> bool:
    """Upsert the concise GraphViz guidance section inside an AGENTS.md."""
    return _upsert_marked_section(path, RULE_TEXT)


# Fuller, human-facing usage guidance for a project README.
README_TEXT = """\
## GraphViz MCP diagrams

This repository is configured to use the [GraphViz MCP server](https://graphviz.org/) for
maintainable technical diagrams. Claude Code (`.mcp.json`) and Codex (`.codex/config.toml`)
launch the stdio server automatically; no daemon or open server workspace is required.

Treat a version-controlled `.dot` file as the source of truth and SVG as the default
generated artifact (PNG for compatibility, PDF for publication). Any single graph is limited
to five rows, five columns, and 25 nodes; larger graphs are automatically encapsulated into a
linked parent/inner hierarchy whose generated `.dot` files are kept beside the source.

### Typical workflows

**Author a new diagram and keep the source under version control**

> Use the GraphViz MCP tools to document this state machine. Keep the DOT source under
> `docs/diagrams/`, render it to SVG, and link the diagram from the relevant Markdown.

**Publish or refresh a diagram inside a Markdown document**

```text
graphviz_publish_markdown(
    project_dir=".",
    dot_path="docs/diagrams/state-flow.dot",
    markdown_path="README.md",
    format="svg",
    anchor="## State flow",
)
```

The managed image block is inserted once and updated in place on later calls:

```markdown
<!-- graphviz:docs/diagrams/state-flow.dot -->
![State flow](docs/diagrams/state-flow.svg)
<!-- /graphviz:docs/diagrams/state-flow.dot -->
```

**Validate before committing**

Call `graphviz_validate` and inspect `layout.requires_encapsulation` to confirm the graph
fits within the enforced limits before rendering or publishing.

### Available tools

| Tool | Purpose |
| --- | --- |
| `graphviz_environment` | Report the resolved `dot` path, version, engines, and limits. |
| `graphviz_validate` | Validate a `.dot` source and report the computed layout. |
| `graphviz_render` | Render an existing `.dot` source to SVG/PNG/PDF. |
| `graphviz_render_source` | Write a `.dot` source and render it in one call. |
| `graphviz_publish_markdown` | Render and insert/refresh a managed Markdown image block. |
| `graphviz_sync` | Re-render a source and optionally refresh its Markdown block. |
"""


def _upsert_readme_section(path: Path) -> bool:
    """Upsert the fuller GraphViz usage section inside a project README."""
    return _upsert_marked_section(path, README_TEXT)


# ---------------------------------------------------------------------------
# GraphViz executable detection and installation
# ---------------------------------------------------------------------------


def _candidate_graphviz_dirs() -> list[Path]:
    """Enumerate likely GraphViz bin directories for the current OS.

    On Windows this includes every fixed drive letter crossed with the common
    ``Program Files`` install layouts; on POSIX it includes the usual prefixes.
    """
    system = platform.system()
    dirs: list[Path] = []
    if system == "Windows":
        # Enumerate drive letters that actually exist.
        drives = [f"{chr(letter)}:\\" for letter in range(ord("A"), ord("Z") + 1)]
        drives = [d for d in drives if Path(d).exists()]
        subpaths = [
            "Program Files\\Graphviz\\bin",
            "Program Files (x86)\\Graphviz\\bin",
            "Graphviz\\bin",
            "ProgramData\\chocolatey\\bin",
        ]
        for drive in drives:
            for sub in subpaths:
                dirs.append(Path(drive) / sub)
        localapp = os.environ.get("LOCALAPPDATA")
        if localapp:
            dirs.append(Path(localapp) / "Programs" / "Graphviz" / "bin")
    else:
        for prefix in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/opt/local/bin"):
            dirs.append(Path(prefix))
    return dirs


def _dot_executable_name() -> str:
    return "dot.exe" if platform.system() == "Windows" else "dot"


def find_graphviz() -> Path | None:
    """Locate the GraphViz ``dot`` executable via PATH, ``GRAPHVIZ_DOT``, or common dirs."""
    override = os.environ.get("GRAPHVIZ_DOT", "").strip()
    if override:
        p = Path(override)
        if p.is_file():
            return p.resolve()

    on_path = shutil.which("dot")
    if on_path:
        return Path(on_path).resolve()

    name = _dot_executable_name()
    for directory in _candidate_graphviz_dirs():
        candidate = directory / name
        if candidate.is_file():
            return candidate.resolve()
    return None


def _dot_version(executable: Path) -> tuple[int, ...] | None:
    """Return the GraphViz version as a numeric tuple by running ``dot -V``.

    GraphViz reports the version on stderr, e.g. ``dot - graphviz version 10.0.1 (...)``.
    Returns ``None`` when the executable cannot be run or the version cannot be parsed.
    """
    try:
        proc = subprocess.run(
            [str(executable), "-V"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = f"{proc.stderr}\n{proc.stdout}"
    match = re.search(r"version\s+(\d+(?:\.\d+)*)", text, re.IGNORECASE)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _version_ok(version: tuple[int, ...] | None) -> bool:
    """Whether ``version`` meets or exceeds ``MINIMUM_GRAPHVIZ_VERSION``."""
    if version is None:
        # Unknown version: do not force an upgrade on an otherwise working install.
        return True
    return version >= MINIMUM_GRAPHVIZ_VERSION


def _format_version(version: tuple[int, ...] | None) -> str:
    return ".".join(str(part) for part in version) if version else "unknown"


def _install_command(upgrade: bool = False) -> list[str] | None:
    """Return the package-manager command that installs or upgrades GraphViz on this OS.

    When ``upgrade`` is true, the command form that updates an already-installed package is
    returned where the package manager distinguishes the two. Returns ``None`` when no
    supported package manager is available.
    """
    system = platform.system()
    if system == "Windows" and shutil.which("winget"):
        verb = "upgrade" if upgrade else "install"
        return ["winget", verb, "--id", "Graphviz.Graphviz", "-e",
                "--accept-package-agreements", "--accept-source-agreements"]
    if system == "Darwin" and shutil.which("brew"):
        return ["brew", "upgrade", "graphviz"] if upgrade else ["brew", "install", "graphviz"]
    if system == "Linux":
        if shutil.which("apt-get"):
            # apt-get install upgrades the package when a newer candidate is available.
            return ["sudo", "apt-get", "install", "-y", "--only-upgrade", "graphviz"] if upgrade \
                else ["sudo", "apt-get", "install", "-y", "graphviz"]
        if shutil.which("dnf"):
            return ["sudo", "dnf", "upgrade", "-y", "graphviz"] if upgrade \
                else ["sudo", "dnf", "install", "-y", "graphviz"]
        if shutil.which("pacman"):
            return ["sudo", "pacman", "-S", "--noconfirm", "graphviz"]
    return None


def _run_install(command: list[str]) -> bool:
    """Run an install/upgrade command, logging the outcome. Returns True on success."""
    _log(f"  Running: {' '.join(command)}")
    try:
        subprocess.run(command, check=True)
        return True
    except (subprocess.CalledProcessError, OSError) as exc:
        _log(f"  GraphViz install/upgrade failed: {exc}")
        return False


def _ensure_graphviz(auto_install: bool) -> Path | None:
    """Detect GraphViz and check its version.

    When ``dot`` is missing, or is present but older than ``MINIMUM_GRAPHVIZ_VERSION``, the
    exact package-manager command is reported. With ``auto_install`` the command is executed:
    a fresh install when missing, or an upgrade when the installed version is too old.
    """
    found = find_graphviz()

    if found:
        version = _dot_version(found)
        if _version_ok(version):
            _log(f"GraphViz found: {found} (version {_format_version(version)})")
            return found

        # Present but too old.
        _log(f"GraphViz {_format_version(version)} at {found} is older than the required "
             f"{_format_version(MINIMUM_GRAPHVIZ_VERSION)}.")
        command = _install_command(upgrade=True)
        if command is None:
            _log("  No supported package manager detected. Update GraphViz from "
                 "https://graphviz.org/download/ and re-run.")
            return found
        if not auto_install:
            _log(f"  To upgrade automatically, re-run with --install-graphviz "
                 f"(this will run: {' '.join(command)}).")
            return found
        if not _run_install(command):
            return found
        upgraded = find_graphviz()
        new_version = _dot_version(upgraded) if upgraded else None
        if upgraded and _version_ok(new_version):
            _log(f"GraphViz upgraded: {upgraded} (version {_format_version(new_version)})")
        else:
            _log(f"  Upgrade ran but GraphViz still reports {_format_version(new_version)}; "
                 "open a new shell so PATH updates take effect, or update manually.")
        return upgraded or found

    # Not found at all.
    _log("GraphViz 'dot' executable was not found on PATH or in common locations.")
    command = _install_command()
    if command is None:
        _log("  No supported package manager detected. Install GraphViz from "
             "https://graphviz.org/download/ and re-run.")
        return None
    if not auto_install:
        _log(f"  To install it automatically, re-run with --install-graphviz "
             f"(this will run: {' '.join(command)}).")
        return None
    if not _run_install(command):
        return None
    installed = find_graphviz()
    if installed:
        _log(f"GraphViz installed: {installed} "
             f"(version {_format_version(_dot_version(installed))})")
    else:
        _log("  GraphViz installation ran but 'dot' still was not found; "
             "you may need to open a new shell so PATH updates take effect.")
    return installed


# ---------------------------------------------------------------------------
# Entry point: graphviz-mcp-vscode (repository-local artifacts)
# ---------------------------------------------------------------------------


def vscode_main(argv: list[str] | None = None) -> int:
    """Write repository-local VS Code / Codex / Claude MCP artifacts."""
    parser = argparse.ArgumentParser(
        prog="graphviz-mcp-vscode",
        description="Write repository-local GraphViz MCP artifacts (.mcp.json, "
                    ".codex/config.toml, rule files) for a local VS Code repository.",
    )
    parser.add_argument(
        "project_dir",
        nargs="?",
        default=".",
        help="Target repository directory (default: current directory).",
    )
    args = parser.parse_args(argv)

    project = Path(args.project_dir).expanduser().resolve()
    if not project.is_dir():
        _log(f"error: project directory does not exist: {project}")
        return 2

    entry = _server_entry()
    _log(f"Writing GraphViz MCP artifacts into {project}")

    changed = False
    # 1. Claude Code project-scoped MCP config (.mcp.json).
    changed |= _upsert_mcp_json(project / ".mcp.json", entry)
    # 2. Codex project-scoped MCP config (.codex/config.toml).
    changed |= _upsert_codex_toml(
        project / ".codex" / "config.toml", entry, table_header=f"mcp_servers.{SERVER_NAME}"
    )
    # 3. Concise rule files for each client's conventions.
    changed |= _write_rule_file(project / ".claude" / "rules" / "graphviz.md")
    changed |= _upsert_agents_section(project / "AGENTS.md")
    # 4. Human-facing usage guidance in the project README.
    changed |= _upsert_readme_section(project / "README.md")

    if not changed:
        _log("All GraphViz MCP artifacts were already up to date.")
    _log("Done. Reload the Claude Code / Codex VS Code extension to pick up the changes.")
    return 0


# ---------------------------------------------------------------------------
# Entry point: graphviz-mcp-install (global user configuration)
# ---------------------------------------------------------------------------


def install_main(argv: list[str] | None = None) -> int:
    """Upsert the global user configuration for Claude Code and Codex."""
    parser = argparse.ArgumentParser(
        prog="graphviz-mcp-install",
        description="Register the GraphViz MCP server in the global user configuration "
                    "for Claude Code (~/.claude.json) and Codex (~/.codex/config.toml).",
    )
    parser.add_argument(
        "--install-graphviz",
        action="store_true",
        help="If the GraphViz 'dot' executable cannot be found, attempt to install it "
             "with the platform package manager (winget/brew/apt/dnf/pacman).",
    )
    parser.add_argument(
        "--skip-graphviz-check",
        action="store_true",
        help="Do not detect or install GraphViz; only write client configuration.",
    )
    args = parser.parse_args(argv)

    if not args.skip_graphviz_check:
        _ensure_graphviz(auto_install=args.install_graphviz)

    home = Path.home()
    entry = _server_entry()
    _log("Registering GraphViz MCP in global user configuration")

    changed = False
    # Claude Code global config.
    changed |= _upsert_mcp_json(home / ".claude.json", entry)
    changed |= _write_rule_file(home / ".claude" / "rules" / "graphviz.md")
    # Codex global config.
    changed |= _upsert_codex_toml(
        home / ".codex" / "config.toml", entry, table_header=f"mcp_servers.{SERVER_NAME}"
    )
    changed |= _upsert_agents_section(home / ".codex" / "AGENTS.md")

    if not changed:
        _log("Global GraphViz MCP configuration was already up to date.")
    _log("Done. Restart or reload Claude Code and Codex to pick up the new server.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(vscode_main())
