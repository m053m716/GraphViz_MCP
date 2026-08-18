"""A small, safe GraphViz MCP server using the stdio transport."""

from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


SERVER_ROOT = Path(__file__).resolve().parent
PROCESS_TIMEOUT_SECONDS = 120
MAX_DIAGNOSTIC_CHARS = 16_000
SUPPORTED_FORMATS = {"svg", "png", "pdf"}
SUPPORTED_ENGINES = {"dot", "neato", "fdp", "sfdp", "circo", "twopi"}
MAX_LAYOUT_ROWS = 5
MAX_LAYOUT_COLUMNS = 5
MAX_LAYOUT_NODES = MAX_LAYOUT_ROWS * MAX_LAYOUT_COLUMNS
LAYOUT_AXIS_TOLERANCE_POINTS = 1.0

INSTRUCTIONS = """Use GraphViz for state machines, state-transition diagrams, flowcharts, directed dependency graphs, architecture/data-flow diagrams, decision flows, and similar graph-structured technical documentation. Prefer a version-controlled .dot file as the editable source of truth; for repository documentation render SVG by default and update the relevant Markdown document with a relative link to the rendered diagram.

When an existing task creates or materially changes a state/flow diagram, prefer these GraphViz tools over hand-drawn ASCII diagrams or an unmaintainable raster-only diagram. Preserve repository conventions when a project explicitly mandates another diagram system, do not create diagrams merely for decoration, and do not rewrite unrelated Markdown. Treat .dot as the source and SVG/PNG/PDF as generated artifacts; favor SVG unless compatibility requires PNG. Keep both source and artifact when repository policy permits.

Never put more than five rows or five columns of nodes, or more than 25 nodes total, in one graph image. The tools inspect the computed GraphViz layout and enforce these limits. When a source needs a larger layout, render a parent overview whose encapsulation nodes link to separately rendered inner graphs; recursively encapsulate oversized overviews. Keep every parent and inner image within the same five-by-five limit, and retain the generated parent/inner .dot files beside the source so the hierarchy is reviewable.

All filesystem-writing tools require an explicit project directory (or CLAUDE_PROJECT_DIR when the argument is intentionally empty), keep paths inside that project, and use the GraphViz executable discovered from PATH. The server is stdio-only and never starts a daemon.
"""


@dataclass(frozen=True)
class _LayoutNode:
    index: int
    name: str
    attributes: dict[str, Any]
    x: float
    y: float
    row: int
    column: int


@dataclass(frozen=True)
class _LayoutEdge:
    tail: int
    head: int
    attributes: dict[str, Any]


@dataclass
class _HierarchyUnit:
    key: str
    label: str
    node_indexes: frozenset[int]
    output_path: Path


def _diagnostics(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_DIAGNOSTIC_CHARS:
        return text
    return text[:MAX_DIAGNOSTIC_CHARS] + "\n...[truncated]"


def _failure(error: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": error, **extra}


def _project_root(project_dir: str) -> Path:
    value = project_dir.strip() or os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if not value:
        raise ValueError("project_dir is required")
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError(f"project_dir does not resolve to a directory: {project_dir}")
    return path


def _inside(root: Path, supplied: str, *, label: str, must_exist: bool = False) -> Path:
    if not supplied or not supplied.strip():
        raise ValueError(f"{label} is required")
    candidate = Path(supplied).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=must_exist)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes project_dir") from exc
    return resolved


def _dot_executable() -> Path | None:
    configured = os.environ.get("GRAPHVIZ_DOT", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute() and candidate.is_file():
            return candidate.resolve()
        return None
    found = shutil.which("dot.exe") or shutil.which("dot")
    return Path(found).resolve() if found else None


def _engine_executable(engine: str) -> Path | None:
    found = shutil.which(engine + ".exe") or shutil.which(engine)
    return Path(found).resolve() if found else None


def _run_graphviz(executable: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *args],
        cwd=SERVER_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PROCESS_TIMEOUT_SECONDS,
        shell=False,
        check=False,
    )


def _validate_format_and_engine(format: str, engine: str) -> str | None:
    if format not in SUPPORTED_FORMATS:
        return f"unsupported format {format!r}; choose one of {sorted(SUPPORTED_FORMATS)}"
    if engine not in SUPPORTED_ENGINES:
        return f"unsupported engine {engine!r}; choose one of {sorted(SUPPORTED_ENGINES)}"
    return None


def _axis_levels(values: list[float], *, reverse: bool = False) -> list[float]:
    """Coalesce insignificant GraphViz coordinate jitter into visual rows/columns."""
    ordered = sorted(values, reverse=reverse)
    levels: list[float] = []
    for value in ordered:
        if not levels or abs(value - levels[-1]) > LAYOUT_AXIS_TOLERANCE_POINTS:
            levels.append(value)
    return levels


def _level_index(value: float, levels: list[float]) -> int:
    return min(range(len(levels)), key=lambda index: abs(levels[index] - value))


def _parse_position(value: Any) -> tuple[float, float]:
    parts = str(value).rstrip("!").split(",")
    if len(parts) < 2:
        raise ValueError(f"GraphViz returned an invalid node position: {value!r}")
    return float(parts[0]), float(parts[1])


def _inspect_layout(dot: Path, engine: str, *, fixed_positions: bool = False) -> dict[str, Any]:
    executable = _engine_executable(engine)
    if executable is None:
        return _failure(f"GraphViz engine {engine!r} was not found on PATH")
    try:
        arguments = (["-n2"] if fixed_positions else []) + ["-Tjson", str(dot)]
        result = _run_graphviz(executable, arguments)
    except subprocess.TimeoutExpired:
        return _failure("GraphViz layout inspection timed out", dot_path=str(dot))
    except OSError as exc:
        return _failure(f"could not execute GraphViz: {exc}", dot_path=str(dot))
    if result.returncode != 0:
        return _failure(
            "GraphViz could not compute a layout for inspection",
            dot_path=str(dot),
            stderr=_diagnostics(result.stderr),
        )
    try:
        model = json.loads(result.stdout)
        raw_nodes = model.get("objects", [])
        positions = [_parse_position(node["pos"]) for node in raw_nodes]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return _failure(f"could not inspect GraphViz JSON layout: {exc}", dot_path=str(dot))

    x_levels = _axis_levels([position[0] for position in positions]) if positions else []
    y_levels = _axis_levels([position[1] for position in positions], reverse=True) if positions else []
    nodes = [
        _LayoutNode(
            index=int(raw.get("_gvid", index)),
            name=str(raw.get("name", f"node_{index + 1}")),
            attributes=raw,
            x=x,
            y=y,
            row=_level_index(y, y_levels),
            column=_level_index(x, x_levels),
        )
        for index, (raw, (x, y)) in enumerate(zip(raw_nodes, positions))
    ]
    edges = [
        _LayoutEdge(int(edge["tail"]), int(edge["head"]), edge)
        for edge in model.get("edges", [])
        if "tail" in edge and "head" in edge
    ]
    rows = len(y_levels)
    columns = len(x_levels)
    compliant = (
        rows <= MAX_LAYOUT_ROWS
        and columns <= MAX_LAYOUT_COLUMNS
        and len(nodes) <= MAX_LAYOUT_NODES
    )
    return {
        "success": True,
        "directed": bool(model.get("directed", True)),
        "rows": rows,
        "columns": columns,
        "node_count": len(nodes),
        "max_rows": MAX_LAYOUT_ROWS,
        "max_columns": MAX_LAYOUT_COLUMNS,
        "max_nodes": MAX_LAYOUT_NODES,
        "layout_compliant": compliant,
        "requires_encapsulation": not compliant,
        "nodes": nodes,
        "edges": edges,
        "stderr": _diagnostics(result.stderr),
    }


def _public_layout(layout: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in layout.items()
        if key not in {"success", "nodes", "edges", "stderr", "directed"}
    }


_NODE_ATTRIBUTES = {
    "label",
    "shape",
    "style",
    "color",
    "fillcolor",
    "fontcolor",
    "fontname",
    "fontsize",
    "penwidth",
    "width",
    "height",
    "fixedsize",
    "margin",
    "image",
    "imagescale",
    "xlabel",
}
_EDGE_ATTRIBUTES = {
    "label",
    "color",
    "style",
    "fontcolor",
    "fontname",
    "fontsize",
    "penwidth",
    "arrowhead",
    "arrowtail",
    "dir",
    "decorate",
}


def _dot_quote(value: Any) -> str:
    # GraphViz escape strings deliberately use sequences such as \N, \G, \l,
    # and \n. JSON quoting would double those backslashes and change labels.
    text = str(value).replace("\r", r"\r").replace("\n", r"\n").replace('"', r'\"')
    trailing_backslashes = len(text) - len(text.rstrip("\\"))
    if trailing_backslashes % 2:
        text += "\\"
    return f'"{text}"'


def _dot_attribute_value(name: str, value: Any) -> str:
    text = str(value).strip()
    if name in {"label", "xlabel"} and text.startswith("<") and text.endswith(">"):
        return text
    return _dot_quote(value)


def _dot_attributes(attributes: dict[str, Any], allowed: set[str]) -> str:
    pairs = [
        f"{name}={_dot_attribute_value(name, attributes[name])}"
        for name in sorted(allowed)
        if name in attributes and attributes[name] not in (None, "")
    ]
    return f" [{', '.join(pairs)}]" if pairs else ""


def _grid_position(index: int) -> tuple[int, int]:
    return index // MAX_LAYOUT_COLUMNS, index % MAX_LAYOUT_COLUMNS


def _write_fixed_grid_dot(
    path: Path,
    graph_name: str,
    title: str,
    node_specs: list[tuple[str, dict[str, Any]]],
    edge_specs: list[tuple[str, str, dict[str, Any]]],
    *,
    directed: bool = True,
) -> tuple[int, int]:
    if len(node_specs) > MAX_LAYOUT_NODES:
        raise ValueError("an encapsulated graph cannot contain more than 25 nodes")
    graph_keyword = "digraph" if directed else "graph"
    edge_operator = "->" if directed else "--"
    lines = [
        f"{graph_keyword} {_dot_quote(graph_name)} {{",
        "  graph [layout=neato, overlap=false, splines=true, labelloc=t, "
        f"label={_dot_quote(title)}];",
    ]
    for index, (name, attributes) in enumerate(node_specs):
        row, column = _grid_position(index)
        positioned = dict(attributes)
        positioned["pos"] = f"{column * 180},{-row * 100}!"
        positioned["pin"] = "true"
        allowed = set(_NODE_ATTRIBUTES) | {"pos", "pin", "URL", "target", "tooltip"}
        lines.append(f"  {_dot_quote(name)}{_dot_attributes(positioned, allowed)};")
    for tail, head, attributes in edge_specs:
        lines.append(
            f"  {_dot_quote(tail)} {edge_operator} {_dot_quote(head)}"
            f"{_dot_attributes(attributes, _EDGE_ATTRIBUTES)};"
        )
    lines.append("}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    rows = (len(node_specs) + MAX_LAYOUT_COLUMNS - 1) // MAX_LAYOUT_COLUMNS
    columns = min(len(node_specs), MAX_LAYOUT_COLUMNS)
    return rows, columns


def _render_fixed_grid(source: Path, output: Path, format: str) -> dict[str, Any]:
    executable = _engine_executable("neato")
    if executable is None:
        return _failure("GraphViz engine 'neato' is required for bounded encapsulated layouts")
    verification = _inspect_layout(source, "neato", fixed_positions=True)
    if not verification["success"]:
        return verification
    if not verification["layout_compliant"]:
        return _failure(
            "generated encapsulated layout exceeded the enforced 5x5 limit",
            dot_path=str(source),
            layout=_public_layout(verification),
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = _run_graphviz(
            executable, ["-n2", f"-T{format}", "-o", str(output), str(source)]
        )
    except subprocess.TimeoutExpired:
        return _failure("GraphViz encapsulated rendering timed out", dot_path=str(source))
    except OSError as exc:
        return _failure(f"could not execute GraphViz: {exc}", dot_path=str(source))
    if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
        return _failure(
            "GraphViz encapsulated rendering failed or produced an empty output",
            dot_path=str(source),
            output_path=str(output),
            stderr=_diagnostics(result.stderr),
        )
    return {
        "success": True,
        "layout": _public_layout(verification),
        "stderr": _diagnostics(result.stderr),
    }


def _validate_dot(root: Path, dot: Path) -> dict[str, Any]:
    if dot.suffix.lower() != ".dot":
        return _failure("dot_path must refer to a .dot file", dot_path=str(dot))
    executable = _dot_executable()
    if executable is None:
        return _failure("GraphViz dot.exe was not found on PATH")
    try:
        result = _run_graphviz(executable, ["-Tdot", str(dot)])
    except subprocess.TimeoutExpired:
        return _failure("GraphViz validation timed out", dot_path=str(dot))
    except OSError as exc:
        return _failure(f"could not execute GraphViz: {exc}", dot_path=str(dot))
    stderr = _diagnostics(result.stderr)
    warning_lines = [line for line in result.stderr.splitlines() if "warning" in line.lower()]
    errors = [] if result.returncode == 0 else [line for line in result.stderr.splitlines() if line.strip()]
    if result.returncode != 0 and not errors:
        errors = [f"GraphViz exited with code {result.returncode}"]
    return {
        "success": result.returncode == 0,
        "dot_path": str(dot),
        "errors": errors,
        "warnings": warning_lines,
        "stderr": stderr,
    }


def _overview_specs(
    units: list[_HierarchyUnit], edges: list[_LayoutEdge]
) -> tuple[list[tuple[str, dict[str, Any]]], list[tuple[str, str, dict[str, Any]]]]:
    owner = {
        node_index: unit_index
        for unit_index, unit in enumerate(units)
        for node_index in unit.node_indexes
    }
    node_specs = []
    for unit in units:
        node_specs.append(
            (
                unit.key,
                {
                    "label": unit.label,
                    "shape": "folder",
                    "style": "filled",
                    "fillcolor": "#eef4ff",
                    "URL": unit.output_path.name,
                    "target": "_top",
                    "tooltip": f"Open {unit.label}",
                },
            )
        )
    counts: dict[tuple[int, int], int] = defaultdict(int)
    for edge in edges:
        tail_owner = owner.get(edge.tail)
        head_owner = owner.get(edge.head)
        if tail_owner is not None and head_owner is not None and tail_owner != head_owner:
            counts[(tail_owner, head_owner)] += 1
    edge_specs = [
        (
            units[tail].key,
            units[head].key,
            {"label": f"{count} connection" + ("s" if count != 1 else "")},
        )
        for (tail, head), count in sorted(counts.items())
    ]
    return node_specs, edge_specs


def _render_encapsulated(
    root: Path,
    dot: Path,
    output: Path,
    format: str,
    requested_engine: str,
    layout: dict[str, Any],
) -> dict[str, Any]:
    nodes: list[_LayoutNode] = layout["nodes"]
    edges: list[_LayoutEdge] = layout["edges"]
    node_by_index = {node.index: node for node in nodes}
    tiled: dict[tuple[int, int], list[_LayoutNode]] = defaultdict(list)
    for node in nodes:
        tiled[(node.row // MAX_LAYOUT_ROWS, node.column // MAX_LAYOUT_COLUMNS)].append(node)

    leaf_groups: list[list[_LayoutNode]] = []
    for tile_key in sorted(tiled):
        tile_nodes = sorted(
            tiled[tile_key], key=lambda node: (node.row, node.column, node.name)
        )
        leaf_groups.extend(
            tile_nodes[index : index + MAX_LAYOUT_NODES]
            for index in range(0, len(tile_nodes), MAX_LAYOUT_NODES)
        )

    artifacts: list[dict[str, Any]] = []
    units: list[_HierarchyUnit] = []
    for number, group_nodes in enumerate(leaf_groups, start=1):
        key = f"inner_{number:03d}"
        source_path = dot.with_name(f"{dot.stem}.inner-{number:03d}.dot")
        output_path = output.with_name(f"{output.stem}.inner-{number:03d}.{format}")
        included = frozenset(node.index for node in group_nodes)
        node_specs = [(node.name, node.attributes) for node in group_nodes]
        edge_specs = [
            (
                node_by_index[edge.tail].name,
                node_by_index[edge.head].name,
                edge.attributes,
            )
            for edge in edges
            if edge.tail in included and edge.head in included
        ]
        rows, columns = _write_fixed_grid_dot(
            source_path,
            key,
            f"{dot.stem}: inner view {number}",
            node_specs,
            edge_specs,
            directed=layout["directed"],
        )
        rendered = _render_fixed_grid(source_path, output_path, format)
        if not rendered["success"]:
            return rendered
        unit = _HierarchyUnit(
            key=key,
            label=f"Inner view {number}\n{len(group_nodes)} node"
            + ("s" if len(group_nodes) != 1 else ""),
            node_indexes=included,
            output_path=output_path,
        )
        units.append(unit)
        artifacts.append(
            {
                "kind": "inner",
                "dot_path": str(source_path),
                "output_path": str(output_path),
                "project_relative_output_path": output_path.relative_to(root).as_posix(),
                "rows": rows,
                "columns": columns,
                "node_count": len(group_nodes),
            }
        )

    level = 1
    while len(units) > MAX_LAYOUT_NODES:
        parent_units: list[_HierarchyUnit] = []
        for number, index in enumerate(range(0, len(units), MAX_LAYOUT_NODES), start=1):
            children = units[index : index + MAX_LAYOUT_NODES]
            key = f"group_{level}_{number:03d}"
            source_path = dot.with_name(f"{dot.stem}.group-{level}-{number:03d}.dot")
            output_path = output.with_name(
                f"{output.stem}.group-{level}-{number:03d}.{format}"
            )
            node_specs, edge_specs = _overview_specs(children, edges)
            rows, columns = _write_fixed_grid_dot(
                source_path,
                key,
                f"{dot.stem}: encapsulation group {level}.{number}",
                node_specs,
                edge_specs,
                directed=layout["directed"],
            )
            rendered = _render_fixed_grid(source_path, output_path, format)
            if not rendered["success"]:
                return rendered
            included = frozenset().union(*(child.node_indexes for child in children))
            unit = _HierarchyUnit(
                key=key,
                label=f"Group {level}.{number}\n{len(included)} nodes",
                node_indexes=included,
                output_path=output_path,
            )
            parent_units.append(unit)
            artifacts.append(
                {
                    "kind": "group",
                    "dot_path": str(source_path),
                    "output_path": str(output_path),
                    "project_relative_output_path": output_path.relative_to(root).as_posix(),
                    "rows": rows,
                    "columns": columns,
                    "node_count": len(children),
                }
            )
        units = parent_units
        level += 1

    parent_source = dot.with_name(f"{dot.stem}.parent.dot")
    node_specs, edge_specs = _overview_specs(units, edges)
    parent_rows, parent_columns = _write_fixed_grid_dot(
        parent_source,
        f"{dot.stem}_parent",
        f"{dot.stem}: parent overview",
        node_specs,
        edge_specs,
        directed=layout["directed"],
    )
    rendered = _render_fixed_grid(parent_source, output, format)
    if not rendered["success"]:
        return rendered
    root_artifact = {
        "kind": "parent",
        "dot_path": str(parent_source),
        "output_path": str(output),
        "project_relative_output_path": output.relative_to(root).as_posix(),
        "rows": parent_rows,
        "columns": parent_columns,
        "node_count": len(units),
    }
    artifacts.append(root_artifact)
    return {
        "success": True,
        "dot_path": str(dot),
        "source_path": str(dot),
        "parent_dot_path": str(parent_source),
        "output_path": str(output),
        "project_relative_output_path": output.relative_to(root).as_posix(),
        "format": format,
        "engine": "neato",
        "requested_engine": requested_engine,
        "encapsulated": True,
        "original_layout": _public_layout(layout),
        "layout": {
            "rows": parent_rows,
            "columns": parent_columns,
            "node_count": len(units),
            "max_rows": MAX_LAYOUT_ROWS,
            "max_columns": MAX_LAYOUT_COLUMNS,
            "max_nodes": MAX_LAYOUT_NODES,
            "layout_compliant": True,
            "requires_encapsulation": False,
        },
        "artifacts": artifacts,
        "stderr": rendered["stderr"],
    }


def graphviz_environment_impl() -> dict[str, Any]:
    executable = _dot_executable()
    if executable is None:
        return _failure(
            "GraphViz dot.exe was not found on PATH",
            dot_executable_path=None,
            graphviz_version=None,
            supported_output_formats=sorted(SUPPORTED_FORMATS),
            maximum_layout_rows=MAX_LAYOUT_ROWS,
            maximum_layout_columns=MAX_LAYOUT_COLUMNS,
            maximum_nodes_per_image=MAX_LAYOUT_NODES,
            server_working_directory=str(Path.cwd()),
            server_repository_path=str(SERVER_ROOT),
        )
    try:
        result = _run_graphviz(executable, ["-V"])
        version = _diagnostics((result.stderr or result.stdout).strip())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _failure(f"could not query GraphViz version: {exc}", dot_executable_path=str(executable))
    return {
        "success": result.returncode == 0,
        "dot_executable_path": str(executable),
        "graphviz_version": version,
        "supported_output_formats": sorted(SUPPORTED_FORMATS),
        "available_engines": {
            name: str(path) if (path := _engine_executable(name)) else None
            for name in sorted(SUPPORTED_ENGINES)
        },
        "server_working_directory": str(Path.cwd()),
        "server_repository_path": str(SERVER_ROOT),
        "maximum_layout_rows": MAX_LAYOUT_ROWS,
        "maximum_layout_columns": MAX_LAYOUT_COLUMNS,
        "maximum_nodes_per_image": MAX_LAYOUT_NODES,
        "oversized_layout_behavior": "render parent and inner encapsulated graph images",
        "stderr": _diagnostics(result.stderr),
    }


def graphviz_validate_impl(project_dir: str, dot_path: str) -> dict[str, Any]:
    try:
        root = _project_root(project_dir)
        dot = _inside(root, dot_path, label="dot_path", must_exist=True)
        if not dot.is_file():
            return _failure("dot_path does not refer to a file", dot_path=str(dot))
        validation = _validate_dot(root, dot)
        if not validation["success"]:
            return validation
        layout = _inspect_layout(dot, "dot")
        if not layout["success"]:
            return layout
        return {**validation, "layout": _public_layout(layout)}
    except (OSError, ValueError) as exc:
        return _failure(str(exc), dot_path=dot_path)


def graphviz_render_impl(
    project_dir: str,
    dot_path: str,
    output_path: str = "",
    format: str = "svg",
    engine: str = "dot",
) -> dict[str, Any]:
    format_error = _validate_format_and_engine(format, engine)
    if format_error:
        return _failure(format_error)
    try:
        root = _project_root(project_dir)
        dot = _inside(root, dot_path, label="dot_path", must_exist=True)
        if not dot.is_file():
            return _failure("dot_path does not refer to a file", dot_path=str(dot))
        validation = _validate_dot(root, dot)
        if not validation["success"]:
            return validation
        output = (
            _inside(root, output_path, label="output_path")
            if output_path.strip()
            else dot.with_suffix("." + format)
        )
        if output == dot:
            return _failure("output_path must not overwrite the .dot source")
        layout = _inspect_layout(dot, engine)
        if not layout["success"]:
            return layout
        if not layout["layout_compliant"]:
            return _render_encapsulated(root, dot, output, format, engine, layout)
        output.parent.mkdir(parents=True, exist_ok=True)
        executable = _engine_executable(engine)
        if executable is None:
            return _failure(f"GraphViz engine {engine!r} was not found on PATH")
        try:
            result = _run_graphviz(executable, [f"-T{format}", "-o", str(output), str(dot)])
        except subprocess.TimeoutExpired:
            return _failure("GraphViz rendering timed out", dot_path=str(dot))
        except OSError as exc:
            return _failure(f"could not execute GraphViz: {exc}", dot_path=str(dot))
        if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            return _failure(
                "GraphViz rendering failed or produced an empty output",
                dot_path=str(dot),
                output_path=str(output),
                stderr=_diagnostics(result.stderr),
            )
        return {
            "success": True,
            "dot_path": str(dot),
            "output_path": str(output),
            "project_relative_output_path": output.relative_to(root).as_posix(),
            "format": format,
            "engine": engine,
            "encapsulated": False,
            "layout": _public_layout(layout),
            "artifacts": [
                {
                    "kind": "graph",
                    "dot_path": str(dot),
                    "output_path": str(output),
                    "project_relative_output_path": output.relative_to(root).as_posix(),
                    "rows": layout["rows"],
                    "columns": layout["columns"],
                    "node_count": layout["node_count"],
                }
            ],
            "stderr": _diagnostics(result.stderr),
        }
    except (OSError, ValueError) as exc:
        return _failure(str(exc), dot_path=dot_path, output_path=output_path)


def graphviz_render_source_impl(
    project_dir: str,
    dot_source: str,
    dot_path: str = "diagram.dot",
    output_path: str = "",
    format: str = "svg",
    engine: str = "dot",
) -> dict[str, Any]:
    try:
        root = _project_root(project_dir)
        source_path = _inside(root, dot_path, label="dot_path")
        if source_path.suffix.lower() != ".dot":
            return _failure("dot_path must refer to a .dot file")
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(dot_source, encoding="utf-8", newline="\n")
        rendered = graphviz_render_impl(project_dir, str(source_path), output_path, format, engine)
        if not rendered["success"]:
            return rendered
        return {
            **rendered,
            "dot_path": str(source_path),
            "source_path": str(source_path),
            "rendered_path": rendered["output_path"],
        }
    except (OSError, ValueError) as exc:
        return _failure(str(exc), dot_path=dot_path)


def _markdown_block(marker: str, image_path: str, alt_text: str) -> str:
    return f"<!-- graphviz:{marker} -->\n![{alt_text}]({image_path})\n<!-- /graphviz:{marker} -->"


def _replace_or_insert_block(text: str, marker: str, block: str, anchor: str) -> tuple[str, bool]:
    escaped = re.escape(marker)
    opening = rf"<!--\s*graphviz:{escaped}\s*-->"
    closing = rf"<!--\s*/graphviz:{escaped}\s*-->"
    full_pattern = re.compile(opening + rf".*?{closing}", flags=re.IGNORECASE | re.DOTALL)
    match = full_pattern.search(text)
    if match:
        return text[: match.start()] + block + text[match.end() :], True
    # Also accept an older single-marker block, ending before the next managed
    # block or EOF, so setup can repair it without duplicating the image.
    legacy_pattern = re.compile(
        opening + r".*?(?=\r?\n\s*<!--\s*graphviz:|\Z)",
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = legacy_pattern.search(text)
    if match:
        return text[: match.start()] + block + text[match.end() :], True
    if anchor.strip():
        index = text.find(anchor)
        if index < 0:
            raise ValueError(f"anchor text was not found in markdown_path: {anchor}")
        line_end = text.find("\n", index)
        insertion = len(text) if line_end < 0 else line_end + 1
        prefix = "" if insertion == 0 or text[:insertion].endswith("\n\n") else "\n"
        suffix = "" if insertion == len(text) or text[insertion:].startswith("\n") else "\n"
        return text[:insertion] + prefix + block + suffix + text[insertion:], False
    if not text.strip():
        return block + "\n", False
    return text.rstrip() + "\n\n" + block + "\n", False


def graphviz_publish_markdown_impl(
    project_dir: str,
    dot_path: str,
    markdown_path: str,
    output_path: str = "",
    format: str = "svg",
    alt_text: str = "GraphViz diagram",
    anchor: str = "",
) -> dict[str, Any]:
    if format not in {"svg", "png"}:
        return _failure("Markdown publishing supports only svg or png")
    try:
        root = _project_root(project_dir)
        markdown = _inside(root, markdown_path, label="markdown_path", must_exist=True)
        if not markdown.is_file():
            return _failure("markdown_path does not refer to a file")
        rendered = graphviz_render_impl(project_dir, dot_path, output_path, format, "dot")
        if not rendered["success"]:
            return rendered
        dot = _inside(root, dot_path, label="dot_path", must_exist=True)
        output = Path(rendered["output_path"])
        marker = dot.relative_to(root).as_posix()
        relative_image = os.path.relpath(output, markdown.parent).replace(os.sep, "/")
        block = _markdown_block(marker, relative_image, alt_text or "GraphViz diagram")
        original = markdown.read_text(encoding="utf-8")
        updated, existing = _replace_or_insert_block(original, marker, block, anchor)
        if updated != original:
            markdown.write_text(updated, encoding="utf-8", newline="\n")
        return {
            **rendered,
            "success": True,
            "dot_path": str(dot),
            "rendered_path": str(output),
            "output_path": str(output),
            "markdown_path": str(markdown),
            "markdown_relative_image_path": relative_image,
            "updated_existing_reference": existing,
        }
    except (OSError, ValueError) as exc:
        return _failure(str(exc), markdown_path=markdown_path, dot_path=dot_path)


def graphviz_sync_impl(project_dir: str, dot_path: str, markdown_path: str = "") -> dict[str, Any]:
    if not markdown_path.strip():
        return graphviz_render_impl(project_dir, dot_path)
    return graphviz_publish_markdown_impl(project_dir, dot_path, markdown_path)


READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)

mcp = FastMCP("graphviz", instructions=INSTRUCTIONS)


@mcp.tool(annotations=READ_ONLY)
def graphviz_environment() -> dict[str, Any]:
    """Report GraphViz capabilities, server paths, and enforced 5x5 layout limits."""
    return graphviz_environment_impl()


@mcp.tool(annotations=READ_ONLY)
def graphviz_validate(project_dir: str, dot_path: str) -> dict[str, Any]:
    """Validate DOT and report whether its computed layout needs 5x5 encapsulation."""
    return graphviz_validate_impl(project_dir, dot_path)


@mcp.tool(annotations=WRITE)
def graphviz_render(
    project_dir: str,
    dot_path: str,
    output_path: str = "",
    format: str = "svg",
    engine: str = "dot",
) -> dict[str, Any]:
    """Render DOT, automatically creating parent/inner images above the 5x5 limit."""
    return graphviz_render_impl(project_dir, dot_path, output_path, format, engine)


@mcp.tool(annotations=WRITE)
def graphviz_render_source(
    project_dir: str,
    dot_source: str,
    dot_path: str = "diagram.dot",
    output_path: str = "",
    format: str = "svg",
    engine: str = "dot",
) -> dict[str, Any]:
    """Write DOT and render it with automatic parent/inner 5x5 encapsulation."""
    return graphviz_render_source_impl(project_dir, dot_source, dot_path, output_path, format, engine)


@mcp.tool(annotations=WRITE)
def graphviz_publish_markdown(
    project_dir: str,
    dot_path: str,
    markdown_path: str,
    output_path: str = "",
    format: str = "svg",
    alt_text: str = "GraphViz diagram",
    anchor: str = "",
) -> dict[str, Any]:
    """Render a bounded diagram hierarchy and update its root Markdown reference."""
    return graphviz_publish_markdown_impl(
        project_dir, dot_path, markdown_path, output_path, format, alt_text, anchor
    )


@mcp.tool(annotations=WRITE)
def graphviz_sync(project_dir: str, dot_path: str, markdown_path: str = "") -> dict[str, Any]:
    """Render a bounded diagram hierarchy and optionally refresh its root Markdown block."""
    return graphviz_sync_impl(project_dir, dot_path, markdown_path)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
