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

## Server instructions

The initialization `instructions` tell agents to use GraphViz for state machines, state transitions, flowcharts, directed dependency graphs, architecture/data-flow diagrams, decision flows, and similar graph-structured technical documentation. They also emphasize version-controlled `.dot` sources, SVG by default, managed Markdown references, repository conventions, and avoiding decorative or unrelated edits.

Every graph image is limited to at most five visual rows, five visual columns, and therefore 25 nodes. The server inspects the coordinates computed by the selected GraphViz engine. If either dimension or the node capacity is exceeded, it spatially partitions the source into bounded inner views and renders a parent overview with one encapsulation node per inner view. SVG overview nodes link to their inner SVG images. If an overview would itself exceed 5x5, the server recursively adds bounded intermediate overview images. The original `.dot` remains the source of truth, and generated `.parent.dot`, `.inner-NNN.dot`, and, when needed, `.group-L-NNN.dot` files make the rendered hierarchy reviewable.

## MCP tools

### `graphviz_environment`

Read-only; no arguments. Reports the actual `dot.exe` path, GraphViz version, supported formats, available engine paths, server working directory, server repository path, enforced row/column/node limits, and oversized-layout behavior.

### `graphviz_validate`

Read-only validation with no persistent rendered output:

```text
project_dir: string
dot_path: string
```

Returns `success`, `dot_path`, `errors`, `warnings`, bounded `stderr`, and a `layout` object containing the computed row/column counts, limits, `layout_compliant`, and `requires_encapsulation`. The source must be an existing `.dot` file inside `project_dir`. An oversized but valid source still validates successfully because rendering can safely encapsulate it.

### `graphviz_render`

Validates and renders an existing source:

```text
project_dir: string
dot_path: string
output_path: optional string
format: svg | png | pdf (default svg)
engine: dot | neato | fdp | sfdp | circo | twopi (default dot)
```

If `output_path` is omitted, the root artifact is placed next to the source with the selected extension. A layout within 5x5 renders normally. An oversized layout automatically produces the bounded parent/inner hierarchy described above. The requested path always identifies the root parent image; related inner images use deterministic names beside it. The result includes `encapsulated`, the original and root layout summaries, and an `artifacts` manifest with every generated source/image pair and its row, column, and node counts. Encapsulated views use fixed positions through `neato` so GraphViz cannot expand them beyond the enforced grid.

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

Repeated calls update the block rather than duplicating it. An `anchor` inserts the block after the matching line. Without an anchor, the block is appended only when the Markdown location is unambiguous. For an encapsulated diagram, the managed block references the root parent image; its encapsulation nodes lead to the separately rendered inner images when the output format supports links.

### `graphviz_sync`

Convenience operation. With `markdown_path`, it refreshes the managed Markdown block; without it, it validates and renders the source. The same 5x5 enforcement and automatic hierarchy apply in both cases.

## Security and path behavior

All caller-provided paths are resolved and must remain inside the explicit `project_dir`. Existing symlinks that resolve outside the project are rejected. Output cannot overwrite the `.dot` source. Generated hierarchy sources remain beside the original source, while hierarchy images remain beside the requested root image; all remain inside the project. Only the six known GraphViz engines and three expected output formats are accepted. Subprocesses use argument arrays, `shell=False`, `stdin=DEVNULL`, bounded timeouts, and bounded diagnostics. No caller-supplied executable, shell argument, recursive delete, or arbitrary command execution is available.

Write tools are marked with MCP write/idempotent annotations where supported; validation and environment inspection are marked read-only.

## Tests

The fixtures include a labeled controller state machine (`Idle`, `Connecting`, `Connected`, `Fault`, `Retrying`), a decision flowchart with a diamond and branches, and a Markdown publish fixture. The suite covers executable/version detection, valid and invalid DOT, SVG/PNG rendering, the exact 5-column boundary, automatic encapsulation of over-wide and over-tall layouts, parent-to-inner SVG links, per-artifact limits, missing and unsafe paths, invalid formats/engines, cross-project operations, relative links, managed-block creation/idempotence, and an actual MCP client/server STDIO session.

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
3. Call graphviz_validate with the target project_dir and inspect `layout.requires_encapsulation`.
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
