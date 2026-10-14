---
name: graphviz-mcp
description: Use the GraphViz MCP tools to validate, render, and publish maintainable bounded DOT diagrams for technical documentation. Apply when a task involves a state machine, workflow, flowchart, dependency graph, architecture/data-flow graph, or similar graph-structured diagram; do not use it for decorative diagrams or when a project mandates another diagram system.
metadata:
  short-description: Render bounded GraphViz diagrams from DOT sources
---

# GraphViz MCP

Use the `graphviz` MCP server dependency over its stdio transport. Keep the complete,
meaningful `.dot` source under version control and treat SVG as the default documentation
artifact. All write operations require an explicit `project_dir`; every supplied path must
remain inside that project.

## Workflow

1. Call `graphviz_environment` when GraphViz availability, supported engines, or enforced
   limits are unknown.
2. Call `graphviz_validate` for an existing `.dot` source before rendering when the layout
   or source validity is uncertain.
3. Use `graphviz_render` to render an existing source. Prefer `format="svg"` and the
   default `engine="dot"`; choose another supported engine only when the graph needs it.
4. Use `graphviz_render_source` when authoring a new source. It writes the caller-provided
   `.dot` file before rendering, so the source remains editable and reviewable.
5. Use `graphviz_publish_markdown` when the rendered diagram belongs in Markdown. Use
   `graphviz_sync` to refresh an existing diagram, optionally including its managed Markdown
   block.

## Layout rules

- Keep each rendered view within five rows, five columns, and 25 nodes.
- Author the complete graph first. The server reflows small oversized graphs and partitions
  larger ones into bounded overview/detail panels while retaining real node names, labels,
  and boundary context.
- For large graphs, express meaningful boundaries with labeled DOT clusters or node
  `mcp_group` attributes. Inspect `artifacts`, `owned_nodes`, and `boundary_connections` in
  the result; revise grouping if a detail panel does not explain a useful relationship.
- Preserve the original `.dot` and generated bounded/parent/inner sources when repository
  policy permits. Do not use a generated inner panel as the next source of truth.

## Tool selection

| Tool | Use |
| --- | --- |
| `graphviz_environment` | Inspect GraphViz paths, version, engines, and limits. |
| `graphviz_validate` | Check a repository-local DOT source and its computed layout. |
| `graphviz_render` | Render an existing DOT source with automatic bounded handling. |
| `graphviz_render_source` | Write a complete DOT source and render it in one operation. |
| `graphviz_publish_markdown` | Render and insert/update an idempotent Markdown image block. |
| `graphviz_sync` | Re-render a source, with optional Markdown refresh. |

The server is stdio-only and does not start a daemon. Its tools use the GraphViz executable
found on `PATH`; do not substitute arbitrary executables or shell commands.
