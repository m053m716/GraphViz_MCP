# GraphViz MCP

GraphViz MCP is a local, Windows-friendly Model Context Protocol server for creating maintainable technical diagrams. It exposes GraphViz over STDIO, so Claude Code and Codex launch a short-lived server process automatically for each client session. No terminal process, HTTP daemon, Windows service, or open GraphViz_MCP workspace is required.

## Architecture

```text
Claude Code / Codex VS Code extension
        | STDIO MCP
        v
.venv\Scripts\python.exe server.py
        |
        v
GraphViz -> .dot source + bounded parent/inner .svg/.png/.pdf artifacts
```

The target repository is always supplied as `project_dir`; it does not have to be this server repository. A `.dot` file is the editable source of truth. SVG is the default documentation artifact, PNG is available for compatibility, and PDF is available for publication workflows.

## Prerequisites and GraphViz discovery

GraphViz must be installed and available on `PATH`. This environment was verified with:

```powershell
Get-Command dot
where.exe dot
dot -V
```

The verified executable is `C:\Program Files\Graphviz\bin\dot.exe`, GraphViz `10.0.1 (20240210.2158)`. The `graphviz_environment` tool performs the same discovery at runtime and reports the absolute executable path and version. The server never installs or selects another GraphViz distribution. An optional `GRAPHVIZ_DOT` environment variable can point to a specific absolute executable when a client needs an explicit override.

## Python setup and STDIO launch

The isolated environment uses Python 3.12 and is located at `C:\MyRepos\Python\GraphViz_MCP\.venv`. To recreate it:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[test]"
```

The MCP clients launch:

```text
C:\MyRepos\Python\GraphViz_MCP\.venv\Scripts\python.exe
    C:\MyRepos\Python\GraphViz_MCP\server.py
```

STDOUT is reserved for MCP protocol traffic. GraphViz output is written to requested files and bounded diagnostics are returned in tool results; the server does not print debugging text to STDOUT.

## Console scripts

Installing the package (`pip install -e .`) exposes three console scripts under
`[project.scripts]`. They reconcile every configuration change in place: existing unrelated
settings, servers, comments, and formatting are preserved; the `graphviz` entry is upserted
so repeated runs never create duplicates; and a timestamped `.bak` backup is written before
any file changes.

### `graphviz-mcp`

Runs the stdio MCP server (equivalent to launching `server.py`). MCP clients normally invoke
the interpreter and `server.py` directly, but this script is a stable, PATH-resolvable entry
point.

```powershell
# Run the server over stdio (a client normally launches this for you).
.venv\Scripts\graphviz-mcp.exe
```

### `graphviz-mcp-vscode [project_dir]`

Writes the repository-local artifacts a local VS Code repository needs (defaults to the
current directory):

- `.mcp.json` — Claude Code project-scoped `mcpServers.graphviz` stdio entry.
- `.codex/config.toml` — Codex project-scoped `[mcp_servers.graphviz]` table with `command`,
  `args`, `cwd`, `startup_timeout_sec`, `tool_timeout_sec`, and `enabled`.
- `.claude/rules/graphviz.md` — concise Claude Code rule.
- `AGENTS.md` — a marker-delimited GraphViz guidance section (created or upserted).
- `README.md` — a marker-delimited GraphViz usage section documenting the typical workflows
  and available tools (created or upserted).

The generated stdio command targets the adjacent `.venv` interpreter when present, otherwise
the interpreter running the script; `args` points at this repository's `server.py`.

```powershell
# Configure the repository in the current directory.
.venv\Scripts\graphviz-mcp-vscode.exe

# Or point at a specific repository.
.venv\Scripts\graphviz-mcp-vscode.exe C:\MyRepos\SomeProject
```

### `graphviz-mcp-install`

Registers the server in the **global** user configuration:

- `~/.claude.json` — top-level `mcpServers.graphviz`.
- `~/.claude/rules/graphviz.md` — concise global rule.
- `~/.codex/config.toml` — `[mcp_servers.graphviz]` table.
- `~/.codex/AGENTS.md` — marker-delimited GraphViz guidance section.

Before writing configuration it detects the GraphViz `dot` executable via `PATH`,
`GRAPHVIZ_DOT`, and common install locations (enumerated drive letters and `Program Files`
layouts on Windows; standard prefixes on POSIX). It also reads `dot -V` and compares it
against the minimum verified GraphViz version. If `dot` is missing, or present but older than
that minimum, it prints the exact platform command (winget/brew/apt/dnf/pacman) — an install
command when missing, an upgrade command when out of date — and only executes it when re-run
with `--install-graphviz`. Use `--skip-graphviz-check` to write configuration without touching
GraphViz detection.

```powershell
# Register globally; report (but do not run) any needed GraphViz install/upgrade.
.venv\Scripts\graphviz-mcp-install.exe

# Register globally and install GraphViz if missing, or upgrade it if it is too old.
.venv\Scripts\graphviz-mcp-install.exe --install-graphviz

# Register globally and skip GraphViz detection entirely.
.venv\Scripts\graphviz-mcp-install.exe --skip-graphviz-check
```

On POSIX shells the scripts are on `PATH` after `pip install -e .` (or use
`.venv/bin/graphviz-mcp-install`):

```bash
graphviz-mcp-vscode ./my-project
graphviz-mcp-install --install-graphviz
```

## Server instructions

The initialization `instructions` tell agents to use GraphViz for state machines, state transitions, flowcharts, directed dependency graphs, architecture/data-flow diagrams, decision flows, and similar graph-structured technical documentation. They also emphasize version-controlled `.dot` sources, SVG by default, managed Markdown references, repository conventions, and avoiding decorative or unrelated edits.

Every graph image is limited to five visual rows, five visual columns, and 25 nodes, including boundary context. For `dot`, rows/columns mean ranks and maximum rank occupancy (swapped for LR/RL); staggered nodes in different ranks do not count as extra columns. Other engines use coordinate bands. Small oversized layouts (at most 25 nodes) reflow into one bounded image with a `.bounded.dot` source, avoiding a needless folder overview.

Larger graphs use deterministic connectivity grouping, preferring labeled cluster subgraphs or node `mcp_group` attributes as semantic boundaries. Each panel owns at most 20 nodes and reserves up to five boundary portals. Connected neighbours are preferred, followed by siblings sharing a neighbour; unrelated components are kept separate except for isolated nodes. This is a heuristic, not an inference of domain meaning: supply semantic groups and inspect the results. For example, `api [mcp_group="HTTP ingress"]` and `store [mcp_group="Persistence"]` give the tool meaningful subsystem boundaries. Small groups remain separate even when they could fit in a larger panel.

Each crossing edge appears in both incident detail panels with real endpoint names, its original label/style/direction, and a portal to the connected panel. Portals aggregate remote endpoints by panel; when more than five destination panels are involved, four get individual portals and the rest share a portal linking to the overview. The manifest lists every crossing edge without truncation. Overview boxes summarize their actual contents and edge labels summarize their interactions (up to three distinct descriptions). SVG links open detail images. Overviews exceeding 25 nodes gain bounded intermediate levels. The original `.dot` remains the source of truth; derived `.parent.dot`, `.inner-NNN.dot`, and `.group-L-NNN.dot` sources remain reviewable beside it.

## Updating an existing installation

Restart the MCP client/server to load the changed implementation and tool descriptions. An editable installation or a client launching this checkout's `server.py` picks up Python changes on restart; a non-editable installation needs reinstalling. To refresh package metadata and console scripts as well, run from this repository:

```powershell
.venv\Scripts\python.exe -m pip install -e ".[test]"
.venv\Scripts\graphviz-mcp-install.exe --skip-graphviz-check
# Repeat for repositories with their own local guidance/configuration:
.venv\Scripts\graphviz-mcp-vscode.exe C:\MyRepos\Python\dnd-scribe
```

The install command refreshes global Codex AGENTS.md and Claude rules; the vscode command refreshes project AGENTS.md, rules, README guidance, and MCP configuration. Both preserve unrelated content and create backups. They also migrate the exact known legacy unmarked GraphViz paragraph, preventing conflicting duplicate guidance. Customized unmarked paragraphs are preserved and should be reviewed manually. No GraphViz binary upgrade is needed for this change.

Existing SVGs are not changed by installation. After restarting, call `graphviz_sync` on each original source (for example `docs/diagrams/http-boundary.dot`), with `markdown_path` when it has a managed documentation block. Do not use a generated `.inner-*.dot` as the source. Old generated files are not automatically deleted; the new `artifacts` manifest identifies the current output set so obsolete panels can be reviewed and removed separately.

## Parcelization example

<!-- graphviz:testdata/http-boundary.dot -->
![HTTP boundary with all clients and endpoints connected](testdata/http-boundary.svg)
<!-- /graphviz:testdata/http-boundary.dot -->

The nine-node HTTP boundary regression now renders as one four-rank graph with at most four nodes per rank. Its service client and service health endpoint retain their connections rather than becoming an isolated two-node panel.

## MCP tools

### `graphviz_environment`

Read-only; no arguments. Reports the actual `dot.exe` path, GraphViz version, supported formats, available engine paths, server working directory, server repository path, enforced row/column/node limits, and oversized-layout behavior.

### `graphviz_validate`

Read-only validation with no persistent rendered output:

```text
project_dir: string
dot_path: string
```

Returns `success`, `dot_path`, `errors`, `warnings`, bounded `stderr`, and a `layout` object containing computed row/column counts, limits, `layout_compliant`, `requires_bounded_layout` (reflow or splitting), and `requires_encapsulation` (more than 25 nodes). The source must be an existing `.dot` file inside `project_dir`. An oversized but valid source still validates successfully.

### `graphviz_render`

Validates and renders an existing source:

```text
project_dir: string
dot_path: string
output_path: optional string
format: svg | png | pdf (default svg)
engine: dot | neato | fdp | sfdp | circo | twopi (default dot)
```

If `output_path` is omitted, the root artifact is placed next to the source with the selected extension. A layout within 5x5 renders normally. A small oversized layout returns `reflowed: true`, `encapsulated: false` and one `bounded` artifact. Larger graphs return `encapsulated: true` with the hierarchy described above. The requested output path always identifies the root image. The `artifacts` manifest includes every source/image pair and its row, column, and total node counts; inner artifacts also include a descriptive `label`, `owned_nodes`, and complete `boundary_connections` with original endpoint IDs, labels and destination panel keys. Derived views use fixed positions through `neato`, with spacing based on node dimensions and labels, so GraphViz cannot expand them beyond the enforced grid.

### `graphviz_render_source`

Writes or updates a repository-local `.dot`, validates it, and renders it:

```text
project_dir: string
dot_source: string
dot_path: optional string (default diagram.dot)
output_path: optional string
format: svg | png | pdf (default svg)
engine: allowed engine (default dot)
```

The caller-provided `.dot` source remains in the project after rendering. When encapsulation is required, the derived parent/inner `.dot` sources also remain in the project.

### `graphviz_publish_markdown`

Validates, renders, and creates or updates one idempotent managed Markdown block:

```text
project_dir: string
dot_path: string
markdown_path: string
output_path: optional string
format: svg | png (default svg)
alt_text: optional string
anchor: optional exact text or heading
```

The managed block identifies the source and uses a correct relative image link:

```markdown
<!-- graphviz:docs/diagrams/controller-state.dot -->
![Controller state flow](docs/diagrams/controller-state.svg)
<!-- /graphviz:docs/diagrams/controller-state.dot -->
```

Repeated calls update the block rather than duplicating it. An `anchor` inserts the block after the matching line. Without an anchor, the block is appended. For an encapsulated diagram, the managed block embeds both the overview and every detail panel. Readers can see the actual relationships even when their Markdown viewer disables links inside SVG images or uses PNG.

### `graphviz_sync`

Convenience operation. With `markdown_path`, it refreshes the managed Markdown block; without it, it validates and renders the source. The same 5x5 enforcement and automatic hierarchy apply in both cases.

## Security and path behavior

All caller-provided paths are resolved and must remain inside the explicit `project_dir`. Existing symlinks that resolve outside the project are rejected. Output cannot overwrite the `.dot` source. Generated hierarchy sources remain beside the original source, while hierarchy images remain beside the requested root image; all remain inside the project. Only the six known GraphViz engines and three expected output formats are accepted. Subprocesses use argument arrays, `shell=False`, `stdin=DEVNULL`, bounded timeouts, and bounded diagnostics. No caller-supplied executable, shell argument, recursive delete, or arbitrary command execution is available.

Write tools are marked with MCP write/idempotent annotations where supported; validation and environment inspection are marked read-only.

## Tests

The fixtures include a controller state machine, decision flowchart, HTTP boundary regression and Markdown publishing. Tests cover small-graph reflow (SVG/PNG/PDF), semantic clusters and node grouping hints, labeled boundary edges, hub-and-spoke graphs, recursive overviews and portal overflow, per-artifact bounds, guidance migration, path validation, idempotent publishing, and an actual MCP client/server STDIO session.

Run the complete suite:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

## Global Claude Code registration

The user-scoped Claude configuration is `%USERPROFILE%\.claude.json`, specifically its top-level `mcpServers` object. The setup registers both `latex` and `graphviz` with absolute Python and server paths. Existing unrelated settings and servers are preserved. A timestamped backup is created before each configuration change. The GraphViz server entry is equivalent to:

```json
"graphviz": {
  "type": "stdio",
  "command": "C:\\MyRepos\\Python\\GraphViz_MCP\\.venv\\Scripts\\python.exe",
  "args": ["C:\\MyRepos\\Python\\GraphViz_MCP\\server.py"]
}
```

The concise global rule is `%USERPROFILE%\.claude\rules\graphviz.md`. Claude Code or its VS Code extension must be reloaded before a new user-scoped MCP entry is visible; this repository cannot verify extension UI connectivity itself.

## Global Codex registration

The user-scoped Codex configuration is `%USERPROFILE%\.codex\config.toml`. The setup preserves its existing TOML and adds `[mcp_servers.graphviz]` with absolute `command`, `args`, `cwd`, `startup_timeout_sec = 15`, `tool_timeout_sec = 120`, and `enabled = true`. The existing `latex` entry is preserved when present or restored from the verified LaTeX_MCP path if the audit finds it absent. `%USERPROFILE%\.codex\AGENTS.md` contains the concise `## GraphViz diagrams` guidance section and is not duplicated on repeated runs.

Codex must be restarted or its MCP settings reloaded before the new server appears in the extension UI. No Codex CLI is required.

## Typical state-machine workflow

```text
1. Choose the target repository and its existing docs/figure convention.
2. Create or update docs/diagrams/controller-state.dot without manually cramming more than 5x5 nodes into one view.
3. Call graphviz_validate with the target project_dir and inspect `layout.requires_bounded_layout` and `layout.requires_encapsulation`.
4. Call graphviz_publish_markdown with the relevant Markdown file; it creates parent/inner views when required.
5. Review the source, generated hierarchy `.dot` files, and SVG artifacts in version control.
```

Example request:

> Document this controller state machine. Use the GraphViz MCP tools, keep the DOT source under `docs/diagrams`, render it to SVG, and link the diagram from the relevant Markdown documentation.

## Typical Markdown publishing workflow

```text
graphviz_publish_markdown(
    project_dir="C:\\MyRepos\\SomeProject",
    dot_path="docs\\diagrams\\state-flow.dot",
    markdown_path="README.md",
    format="svg",
    anchor="## State flow"
)
```

Individual repositories do not need copies of GraphViz_MCP. They only need their own `.dot` source and generated artifact if project policy versions artifacts.

## Troubleshooting

- If `graphviz_environment` fails, confirm `Get-Command dot`, `where.exe dot`, and `dot -V`; use `GRAPHVIZ_DOT` only with a verified absolute executable.
- If a path is rejected, use an explicit absolute `project_dir` and ensure every source, output, and Markdown path is inside it.
- If Markdown insertion fails, supply an exact heading or anchor so the tool has an unambiguous insertion point.
- If a client does not list the server, validate the JSON/TOML, then reload the Claude Code or Codex VS Code extension/session. This implementation does not claim extension-level connectivity without that UI check.
- The server process is intentionally client-managed; do not start a persistent terminal or HTTP process.

<!-- handoff-mcp:begin (managed by `handoff-mcp init --vscode`) -->
## Handoff MCP — session memory for agents

This repo has the `handoff` MCP server configured (see `.mcp.json` /
`.codex/config.toml`). It is a durable, project-scoped place to leave
breadcrumbs between sessions. Use it instead of re-deriving context.

**At the start of a session**, call `handoff_list` to reload where prior work
stopped and what to do next, and `todo_list` for outstanding next steps. This is
cheaper and more reliable than re-reading the whole transcript.

**While working**, when you find something that must be done but is not the
current focus, call `todo_add` rather than holding it in the conversation.

**When context gets heavy** (stale greps, large logs, finished sub-tasks pile
up), call `context_report`, then `context_compact` — it returns a
summarise-then-handoff procedure and can persist the summary as a handoff in one
call.

**At the end of a work chunk**, call `handoff_add` with a summary, next steps,
and the few key facts (file paths, decisions, gotchas) the next worker needs. A
fresh session can then resume from `handoff_list` alone.

**Close the loop** with `todo_update` (done/dropped) and `handoff_resolve` so
the open lists stay a true worklist.

| Tool | Use it to |
| --- | --- |
| `handoff_list` | Reload breadcrumbs at session start. |
| `handoff_add` | Record where you stopped and what is next. |
| `handoff_resolve` | Mark a handoff done. |
| `todo_add` / `todo_list` / `todo_update` | Track next-step TODOs. |
| `project_status` | Counts of open todos and handoffs. |
| `context_report` / `context_compact` | Notice and shrink a bloated context window. |

Every tool is scoped to this project only; there is no way to reach another
project's data. Full reference: `docs/TOOL_GUIDE.md`.
<!-- handoff-mcp:end -->
