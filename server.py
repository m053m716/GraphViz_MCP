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

Never put more than five rows or five columns of nodes, or more than 25 nodes total, in one graph image. Author the complete meaningful graph first; do not pre-split it by drawing coordinates or replace components with anonymous folders. The tools count dot ranks and their occupancy, reflow small oversized layouts into one bounded image, and partition larger graphs by connectivity. Supply labeled cluster subgraphs or node mcp_group attributes to express subsystem or workflow boundaries. Each detail panel must explain a relationship: retain real node names, edge labels, and incoming/outgoing boundary context. Overview nodes summarize their contents and link to detail panels. Inspect the artifacts and boundary_connections in tool results; revise grouping if a panel does not answer a useful question. Keep each view within the same five-by-five limit including boundary nodes, and retain generated .bounded.dot, parent/inner .dot sources and images. Publish/sync includes detail images in Markdown because SVG links often do not work in embedded images.

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
        # GraphViz includes subgraphs/clusters in objects; they are not nodes.
        raw_nodes = [item for item in model.get("objects", []) if "pos" in item]
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
    # For dot, ranks are the visual rows (TB/BT) or columns (LR/RL).
    # Counting every off-centre coordinate on the other axis splits small trees.
    if engine == "dot" and not fixed_positions:
        if model.get("rankdir", "TB") in {"LR", "RL"}:
            rows = max((sum(node.column == i for node in nodes) for i in range(columns)), default=0)
        else:
            columns = max((sum(node.row == i for node in nodes) for i in range(rows)), default=0)
    memberships: dict[int, tuple[int, str]] = {}
    for item in model.get("objects", []):
        if "pos" in item or not item.get("name", "").startswith("cluster"):
            continue
        members = item.get("nodes", [])
        for member in members:
            if member not in memberships or len(members) < memberships[member][0]:
                memberships[member] = (len(members), str(item.get("label", item["name"])))
    for node in nodes:
        if node.index in memberships:
            node.attributes.setdefault("mcp_group", memberships[node.index][1])
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
        "requires_bounded_layout": not compliant,
        "requires_encapsulation": len(nodes) > MAX_LAYOUT_NODES,
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
    row, column = divmod(index, MAX_LAYOUT_COLUMNS)
    # Fold sequential flows at the end of each row instead of drawing a long
    # diagonal back across the preceding row.
    return row, MAX_LAYOUT_COLUMNS - 1 - column if row % 2 else column


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
    # Dimensions in GraphViz JSON are inches; fixed neato positions are points.
    # Reserve enough space for long labels, including generated portal labels.
    def extent(attrs: dict[str, Any], axis: str) -> float:
        label = str(attrs.get("label", "")).replace(r"\n", "\n")
        lines = label.splitlines() or [""]
        font = float(attrs.get("fontsize", 14))
        estimate = max(map(len, lines)) * font * 0.65 if axis == "width" else len(lines) * font * 1.3
        return max(float(attrs.get(axis, 0)) * 72, estimate + 24)

    x_step = max((extent(attrs, "width") for _, attrs in node_specs), default=132) + 60
    y_step = max((extent(attrs, "height") for _, attrs in node_specs), default=52) + 60
    for index, (name, attributes) in enumerate(node_specs):
        row, column = _grid_position(index)
        positioned = dict(attributes)
        positioned["pos"] = f"{column * x_step},{-row * y_step}!"
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
    units: list[_HierarchyUnit], edges: list[_LayoutEdge],
    node_by_index: dict[int, _LayoutNode],
    *, directed: bool = True,
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
                    "shape": "box",
                    "style": "filled",
                    "fillcolor": "#eef4ff",
                    "URL": unit.output_path.name,
                    "target": "_top",
                    "tooltip": f"Open {unit.label}",
                },
            )
        )
    connections: dict[tuple[int, int], list[str]] = defaultdict(list)
    for edge in edges:
        tail_owner = owner.get(edge.tail)
        head_owner = owner.get(edge.head)
        if tail_owner is not None and head_owner is not None and tail_owner != head_owner:
            separator = " → " if directed else " — "
            detail = str(edge.attributes.get("label") or
                         _node_label(node_by_index[edge.tail]) + separator + _node_label(node_by_index[edge.head]))
            if not directed and tail_owner > head_owner:
                tail_owner, head_owner = head_owner, tail_owner
            connections[(tail_owner, head_owner)].append(detail)
    edge_specs = [
        (
            units[tail].key,
            units[head].key,
            {"label": _summary(details)},
        )
        for (tail, head), details in sorted(connections.items())
    ]
    return node_specs, edge_specs


def _node_label(node: _LayoutNode) -> str:
    label = str(node.attributes.get("label", node.name))
    return label.replace(r"\N", node.name).replace(r"\n", " / ").replace("\n", " / ")


def _summary(labels: list[str]) -> str:
    unique = list(dict.fromkeys(labels))
    return "\n".join(unique[:3]) + (f"\n… +{len(unique) - 3} more" if len(unique) > 3 else "")


def _partition_nodes(nodes: list[_LayoutNode], edges: list[_LayoutEdge]) -> list[list[_LayoutNode]]:
    """Deterministic connectivity heuristic; semantic groups constrain growth.

    Keep twenty owned nodes at most, reserving five slots for boundary context.
    Siblings with a shared neighbour may join a panel, but unrelated components
    do not get mixed merely because their drawing coordinates are nearby.
    """
    by_index = {node.index: node for node in nodes}
    adjacent: dict[int, set[int]] = {node.index: set() for node in nodes}
    for edge in edges:
        if edge.tail != edge.head:
            adjacent[edge.tail].add(edge.head)
            adjacent[edge.head].add(edge.tail)
    remaining = set(by_index)
    groups = []
    while remaining:
        seed = min(remaining, key=lambda i: (-len(adjacent[i]), i))
        hint = by_index[seed].attributes.get("mcp_group", "")
        selected = {seed}
        remaining.remove(seed)
        while len(selected) < MAX_LAYOUT_NODES - 5:
            neighbours = set().union(*(adjacent[i] for i in selected))
            candidates = [i for i in remaining
                          if by_index[i].attributes.get("mcp_group", "") == hint
                          and (adjacent[i] & selected or adjacent[i] & neighbours
                               or (not neighbours and not adjacent[i]))]
            if not candidates:
                break
            chosen = min(candidates, key=lambda i: (-len(adjacent[i] & selected),
                                                    -len(adjacent[i] & neighbours), i))
            selected.add(chosen)
            remaining.remove(chosen)
        groups.append(sorted((by_index[i] for i in selected), key=lambda n: (n.row, n.column, n.index)))
    return groups


def _panel_label(nodes: list[_LayoutNode]) -> str:
    hints = list(dict.fromkeys(str(n.attributes["mcp_group"]) for n in nodes if n.attributes.get("mcp_group")))
    contents = _summary([_node_label(n) for n in nodes])
    # A subsystem may span several panels. Show representative members as well
    # as its name so the overview never becomes identical subsystem boxes.
    return (_summary(hints) + "\n" if hints else "") + contents


def _render_bounded(root: Path, dot: Path, output: Path, format: str,
                    engine: str, layout: dict[str, Any]) -> dict[str, Any]:
    """Small but long layouts need reflow, not a one-folder hierarchy."""
    source = dot.with_name(f"{dot.stem}.bounded.dot")
    nodes = sorted(layout["nodes"], key=lambda n: (n.row, n.column, n.index))
    names = {n.index: n.name for n in nodes}
    _write_fixed_grid_dot(source, dot.stem, dot.stem,
                         [(n.name, n.attributes) for n in nodes],
                         [(names[e.tail], names[e.head], e.attributes) for e in layout["edges"]],
                         directed=layout["directed"])
    rendered = _render_fixed_grid(source, output, format)
    if not rendered["success"]:
        return rendered
    return {
        "success": True, "dot_path": str(dot), "source_path": str(dot),
        "output_path": str(output), "project_relative_output_path": output.relative_to(root).as_posix(),
        "format": format, "engine": "neato", "requested_engine": engine,
        "encapsulated": False, "reflowed": True, "original_layout": _public_layout(layout),
        "layout": rendered["layout"], "stderr": rendered["stderr"],
        "artifacts": [{"kind": "bounded", "dot_path": str(source), "output_path": str(output),
                       "project_relative_output_path": output.relative_to(root).as_posix(),
                       **{key: rendered["layout"][key] for key in ("rows", "columns", "node_count")}}],
    }


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
    leaf_groups = _partition_nodes(nodes, edges)
    owner = {node.index: number for number, group in enumerate(leaf_groups, start=1) for node in group}

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
        # Every cut edge appears in both incident panels, with its real endpoint
        # names, original label, direction and a link to the other panel.
        cut_edges = [e for e in edges if (e.tail in included) != (e.head in included)]
        remote_groups = sorted({owner[e.head if e.tail in included else e.tail] for e in cut_edges})
        buckets = [[g] for g in remote_groups[:4]]
        if len(remote_groups) > 4:
            buckets.append(remote_groups[4:])
        portals = {}
        for index, bucket in enumerate(buckets):
            portal = f"__boundary_{index}"
            while portal in {name for name, _ in node_specs}:
                portal += "_"
            remote_nodes = sorted({e.head if e.tail in included else e.tail for e in cut_edges
                                   if owner[e.head if e.tail in included else e.tail] in bucket})
            target = (output.with_name(f"{output.stem}.inner-{bucket[0]:03d}.{format}")
                      if len(bucket) == 1 else output)
            node_specs.append((portal, {
                "label": "External: " + _summary([_node_label(node_by_index[i]) for i in remote_nodes]),
                "shape": "box", "style": "dashed", "color": "#64748b",
                "URL": target.name, "target": "_top", "tooltip": "Open connected panel or overview",
            }))
            for group in bucket:
                portals[group] = portal
        boundary_connections = []
        for edge in cut_edges:
            remote = edge.head if edge.tail in included else edge.tail
            tail, head = node_by_index[edge.tail], node_by_index[edge.head]
            attributes = dict(edge.attributes)
            detail = f"{_node_label(tail)} → {_node_label(head)}" if layout["directed"] else f"{_node_label(tail)} — {_node_label(head)}"
            attributes["label"] = detail + ("\n" + str(attributes["label"]) if attributes.get("label") else "")
            edge_specs.append((tail.name if edge.tail in included else portals[owner[remote]],
                               head.name if edge.head in included else portals[owner[remote]], attributes))
            boundary_connections.append({"tail": tail.name, "head": head.name,
                                         "label": edge.attributes.get("label", ""),
                                         "panel": f"inner_{owner[remote]:03d}"})
        panel_label = _panel_label(group_nodes)
        rows, columns = _write_fixed_grid_dot(
            source_path,
            key,
            f"{dot.stem}: {panel_label}",
            node_specs,
            edge_specs,
            directed=layout["directed"],
        )
        rendered = _render_fixed_grid(source_path, output_path, format)
        if not rendered["success"]:
            return rendered
        unit = _HierarchyUnit(
            key=key,
            label=panel_label,
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
                "node_count": len(node_specs),
                "label": panel_label,
                "owned_nodes": [node.name for node in group_nodes],
                "boundary_connections": boundary_connections,
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
            node_specs, edge_specs = _overview_specs(children, edges, node_by_index, directed=layout["directed"])
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
                label=_summary([child.label for child in children]),
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
    node_specs, edge_specs = _overview_specs(units, edges, node_by_index, directed=layout["directed"])
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
            "requires_bounded_layout": False,
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
        "oversized_layout_behavior": "reflow small graphs; partition larger graphs by semantics/connectivity with boundary context",
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
            if layout["node_count"] <= MAX_LAYOUT_NODES:
                return _render_bounded(root, dot, output, format, engine, layout)
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
        panels = []
        for number, artifact in enumerate((a for a in rendered["artifacts"] if a["kind"] == "inner"), start=1):
            relative = os.path.relpath(artifact["output_path"], markdown.parent).replace(os.sep, "/")
            panels.append(f"\n\n![Detail panel {number}]({relative})")
        if panels:
            closing = f"\n<!-- /graphviz:{marker} -->"
            block = block.removesuffix(closing) + "".join(panels) + closing
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
    """Validate DOT; report whether it needs bounded reflow or semantic parcelization."""
    return graphviz_validate_impl(project_dir, dot_path)


@mcp.tool(annotations=WRITE)
def graphviz_render(
    project_dir: str,
    dot_path: str,
    output_path: str = "",
    format: str = "svg",
    engine: str = "dot",
) -> dict[str, Any]:
    """Render DOT; reflow small graphs or create meaningful panels with boundary context."""
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
    """Write complete DOT, then render with bounded reflow or semantic detail panels."""
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
    """Render bounded diagrams and embed the overview and detail panels in Markdown."""
    return graphviz_publish_markdown_impl(
        project_dir, dot_path, markdown_path, output_path, format, alt_text, anchor
    )


@mcp.tool(annotations=WRITE)
def graphviz_sync(project_dir: str, dot_path: str, markdown_path: str = "") -> dict[str, Any]:
    """Render bounded diagrams and optionally refresh overview/detail images in Markdown."""
    return graphviz_sync_impl(project_dir, dot_path, markdown_path)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
