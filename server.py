"""A small, safe GraphViz MCP server using the stdio transport."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations


SERVER_ROOT = Path(__file__).resolve().parent
PROCESS_TIMEOUT_SECONDS = 120
MAX_DIAGNOSTIC_CHARS = 16_000
SUPPORTED_FORMATS = {"svg", "png", "pdf"}
SUPPORTED_ENGINES = {"dot", "neato", "fdp", "sfdp", "circo", "twopi"}

INSTRUCTIONS = """Use GraphViz for state machines, state-transition diagrams, flowcharts, directed dependency graphs, architecture/data-flow diagrams, decision flows, and similar graph-structured technical documentation. Prefer a version-controlled .dot file as the editable source of truth; for repository documentation render SVG by default and update the relevant Markdown document with a relative link to the rendered diagram.

When an existing task creates or materially changes a state/flow diagram, prefer these GraphViz tools over hand-drawn ASCII diagrams or an unmaintainable raster-only diagram. Preserve repository conventions when a project explicitly mandates another diagram system, do not create diagrams merely for decoration, and do not rewrite unrelated Markdown. Treat .dot as the source and SVG/PNG/PDF as generated artifacts; favor SVG unless compatibility requires PNG. Keep both source and artifact when repository policy permits.

All filesystem-writing tools require an explicit project directory (or CLAUDE_PROJECT_DIR when the argument is intentionally empty), keep paths inside that project, and use the GraphViz executable discovered from PATH. The server is stdio-only and never starts a daemon.
"""


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


def graphviz_environment_impl() -> dict[str, Any]:
    executable = _dot_executable()
    if executable is None:
        return _failure(
            "GraphViz dot.exe was not found on PATH",
            dot_executable_path=None,
            graphviz_version=None,
            supported_output_formats=sorted(SUPPORTED_FORMATS),
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
        "stderr": _diagnostics(result.stderr),
    }


def graphviz_validate_impl(project_dir: str, dot_path: str) -> dict[str, Any]:
    try:
        root = _project_root(project_dir)
        dot = _inside(root, dot_path, label="dot_path", must_exist=True)
        if not dot.is_file():
            return _failure("dot_path does not refer to a file", dot_path=str(dot))
        return _validate_dot(root, dot)
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
    """Report the actual GraphViz executable, version, formats, and server paths."""
    return graphviz_environment_impl()


@mcp.tool(annotations=READ_ONLY)
def graphviz_validate(project_dir: str, dot_path: str) -> dict[str, Any]:
    """Validate a .dot source without creating a rendered artifact."""
    return graphviz_validate_impl(project_dir, dot_path)


@mcp.tool(annotations=WRITE)
def graphviz_render(
    project_dir: str,
    dot_path: str,
    output_path: str = "",
    format: str = "svg",
    engine: str = "dot",
) -> dict[str, Any]:
    """Validate and render a repository-local .dot source through GraphViz."""
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
    """Write a repository-local .dot source, validate it, and render it."""
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
    """Render a diagram and create or update one managed relative Markdown reference."""
    return graphviz_publish_markdown_impl(
        project_dir, dot_path, markdown_path, output_path, format, alt_text, anchor
    )


@mcp.tool(annotations=WRITE)
def graphviz_sync(project_dir: str, dot_path: str, markdown_path: str = "") -> dict[str, Any]:
    """Validate/render a diagram and optionally refresh its managed Markdown block."""
    return graphviz_sync_impl(project_dir, dot_path, markdown_path)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
