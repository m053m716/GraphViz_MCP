from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import server


ROOT = Path(__file__).parents[1]
STATE_DOT = ROOT / "testdata" / "state-machine.dot"
FLOW_DOT = ROOT / "testdata" / "flowchart.dot"


def test_environment_detection_and_version() -> None:
    result = server.graphviz_environment_impl()
    assert result["success"] is True, result
    assert Path(result["dot_executable_path"]).is_file()
    assert "graphviz version" in result["graphviz_version"].lower()
    assert "10.0.1" in result["graphviz_version"]
    assert result["server_repository_path"] == str(ROOT.resolve())
    assert result["maximum_layout_rows"] == 5
    assert result["maximum_layout_columns"] == 5
    assert result["maximum_nodes_per_image"] == 25


def test_validate_valid_dot() -> None:
    result = server.graphviz_validate_impl(str(ROOT), str(STATE_DOT))
    assert result["success"] is True, result
    assert result["errors"] == []
    assert result["layout"]["rows"] <= 5
    assert result["layout"]["columns"] <= 5
    assert result["layout"]["layout_compliant"] is True


def test_validate_invalid_dot(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.dot"
    invalid.write_text("digraph { this is not valid", encoding="utf-8")
    result = server.graphviz_validate_impl(str(tmp_path), "invalid.dot")
    assert result["success"] is False
    assert result["errors"] or result["stderr"]


@pytest.mark.parametrize("format", ["svg", "png"])
def test_render_formats(tmp_path: Path, format: str) -> None:
    (tmp_path / "diagram.dot").write_text(STATE_DOT.read_text(encoding="utf-8"), encoding="utf-8")
    result = server.graphviz_render_impl(str(tmp_path), "diagram.dot", format=format)
    assert result["success"] is True, result
    output = Path(result["output_path"])
    assert output.suffix == "." + format
    assert output.is_file() and output.stat().st_size > 0
    if format == "svg":
        assert output.read_text(encoding="utf-8").lstrip().startswith("<?xml") or "<svg" in output.read_text(encoding="utf-8")


def test_render_state_machine_and_flowchart(tmp_path: Path) -> None:
    for source in (STATE_DOT, FLOW_DOT):
        target = tmp_path / source.name
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        result = server.graphviz_render_impl(str(tmp_path), target.name)
        assert result["success"] is True, result
        assert Path(result["output_path"]).is_file()


def test_render_source_preserves_dot(tmp_path: Path) -> None:
    source = "digraph Source { A -> B [label=\"source\"]; }"
    result = server.graphviz_render_source_impl(str(tmp_path), source, "docs/source.dot")
    assert result["success"] is True, result
    assert (tmp_path / "docs" / "source.dot").read_text(encoding="utf-8") == source
    assert (tmp_path / "docs" / "source.svg").is_file()


@pytest.mark.parametrize(
    ("rankdir", "axis"),
    [("LR", "columns"), ("TB", "rows")],
)
def test_oversized_layout_is_rendered_as_parent_and_inner_images(
    tmp_path: Path, rankdir: str, axis: str
) -> None:
    source = f"digraph Wide {{ rankdir={rankdir}; n1 -> n2 -> n3 -> n4 -> n5 -> n6; }}"
    dot = tmp_path / "wide.dot"
    dot.write_text(source, encoding="utf-8")

    validation = server.graphviz_validate_impl(str(tmp_path), "wide.dot")
    assert validation["success"] is True, validation
    assert validation["layout"][axis] == 6
    assert validation["layout"]["requires_encapsulation"] is True

    result = server.graphviz_render_impl(str(tmp_path), "wide.dot")
    assert result["success"] is True, result
    assert result["encapsulated"] is True
    assert result["engine"] == "neato"
    assert result["requested_engine"] == "dot"
    assert Path(result["parent_dot_path"]).is_file()
    assert Path(result["output_path"]).is_file()

    kinds = [artifact["kind"] for artifact in result["artifacts"]]
    assert kinds.count("inner") == 2
    assert kinds.count("parent") == 1
    for artifact in result["artifacts"]:
        assert artifact["rows"] <= server.MAX_LAYOUT_ROWS
        assert artifact["columns"] <= server.MAX_LAYOUT_COLUMNS
        assert artifact["node_count"] <= server.MAX_LAYOUT_NODES
        assert Path(artifact["dot_path"]).is_file()
        assert Path(artifact["output_path"]).is_file()

    parent_svg = Path(result["output_path"]).read_text(encoding="utf-8")
    assert "wide.inner-001.svg" in parent_svg
    assert "wide.inner-002.svg" in parent_svg
    inner_svg = Path(result["artifacts"][0]["output_path"]).read_text(encoding="utf-8")
    assert ">n1<" in inner_svg


def test_five_by_five_boundary_does_not_encapsulate(tmp_path: Path) -> None:
    dot = tmp_path / "boundary.dot"
    dot.write_text(
        "digraph Boundary { rankdir=LR; n1 -> n2 -> n3 -> n4 -> n5; }",
        encoding="utf-8",
    )
    result = server.graphviz_render_impl(str(tmp_path), "boundary.dot")
    assert result["success"] is True, result
    assert result["encapsulated"] is False
    assert result["layout"]["columns"] == 5
    assert result["layout"]["layout_compliant"] is True


def test_undirected_graph_remains_undirected_when_encapsulated(tmp_path: Path) -> None:
    dot = tmp_path / "network.dot"
    dot.write_text(
        "graph Network { rankdir=LR; n1 -- n2 -- n3 -- n4 -- n5 -- n6; }",
        encoding="utf-8",
    )
    result = server.graphviz_render_impl(str(tmp_path), "network.dot")
    assert result["success"] is True, result
    assert result["encapsulated"] is True
    parent_source = Path(result["parent_dot_path"]).read_text(encoding="utf-8")
    inner_source = Path(result["artifacts"][0]["dot_path"]).read_text(encoding="utf-8")
    assert parent_source.startswith('graph "network_parent"')
    assert inner_source.startswith('graph "inner_001"')
    assert " -- " in inner_source


def test_missing_dot_and_invalid_project() -> None:
    missing = server.graphviz_validate_impl(str(ROOT), "missing.dot")
    assert missing["success"] is False
    invalid_project = server.graphviz_validate_impl(str(ROOT / "missing-project"), "x.dot")
    assert invalid_project["success"] is False


def test_rejects_path_traversal(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.dot"
    outside.write_text("digraph { A -> B }", encoding="utf-8")
    result = server.graphviz_validate_impl(str(tmp_path), "../outside.dot")
    assert result["success"] is False
    assert "escapes project_dir" in result["error"]
    render = server.graphviz_render_impl(str(tmp_path), "missing.dot", "../outside.svg")
    assert render["success"] is False


def test_rejects_invalid_format_and_engine(tmp_path: Path) -> None:
    source = tmp_path / "diagram.dot"
    source.write_text("digraph { A -> B }", encoding="utf-8")
    assert not server.graphviz_render_impl(str(tmp_path), "diagram.dot", format="jpg")["success"]
    assert not server.graphviz_render_impl(str(tmp_path), "diagram.dot", engine="bogus")["success"]


def test_subprocess_is_not_shell_based() -> None:
    source = inspect.getsource(server._run_graphviz)
    assert "shell=False" in source
    assert "shell=True" not in source


def test_markdown_relative_link_and_idempotent_update(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    diagrams = docs / "diagrams"
    diagrams.mkdir()
    (diagrams / "state.dot").write_text(STATE_DOT.read_text(encoding="utf-8"), encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text("# Project\n\n## State\n", encoding="utf-8")

    first = server.graphviz_publish_markdown_impl(
        str(tmp_path), "docs/diagrams/state.dot", "README.md", anchor="## State", alt_text="Controller state"
    )
    assert first["success"] is True, first
    assert first["updated_existing_reference"] is False
    assert first["markdown_relative_image_path"] == "docs/diagrams/state.svg"
    content = readme.read_text(encoding="utf-8")
    assert content.count("<!-- graphviz:docs/diagrams/state.dot -->") == 1
    assert "![Controller state](docs/diagrams/state.svg)" in content

    second = server.graphviz_publish_markdown_impl(
        str(tmp_path), "docs/diagrams/state.dot", "README.md", anchor="## State", alt_text="Updated state"
    )
    assert second["success"] is True, second
    assert second["updated_existing_reference"] is True
    content = readme.read_text(encoding="utf-8")
    assert content.count("graphviz:docs/diagrams/state.dot") == 2  # opening and closing markers
    assert content.count("docs/diagrams/state.svg") == 1
    assert "![Updated state]" in content


def test_markdown_publish_exposes_encapsulated_artifacts(tmp_path: Path) -> None:
    (tmp_path / "wide.dot").write_text(
        "digraph Wide { rankdir=LR; n1 -> n2 -> n3 -> n4 -> n5 -> n6; }",
        encoding="utf-8",
    )
    readme = tmp_path / "README.md"
    readme.write_text("# Wide graph\n", encoding="utf-8")

    result = server.graphviz_publish_markdown_impl(
        str(tmp_path), "wide.dot", "README.md", alt_text="Bounded wide graph"
    )
    assert result["success"] is True, result
    assert result["encapsulated"] is True
    assert [artifact["kind"] for artifact in result["artifacts"]] == [
        "inner",
        "inner",
        "parent",
    ]
    assert "![Bounded wide graph](wide.svg)" in readme.read_text(encoding="utf-8")
    assert (tmp_path / "wide.inner-001.svg").is_file()
    assert (tmp_path / "wide.inner-002.svg").is_file()


def test_markdown_publish_fixture_twice() -> None:
    fixture = ROOT / "testdata" / "markdown_publish"
    readme = fixture / "README.md"
    original = readme.read_text(encoding="utf-8")
    try:
        first = server.graphviz_publish_markdown_impl(str(fixture), "state-machine.dot", "README.md")
        second = server.graphviz_publish_markdown_impl(str(fixture), "state-machine.dot", "README.md")
        assert first["success"] and second["success"]
        assert second["updated_existing_reference"] is True
        assert readme.read_text(encoding="utf-8").count("![GraphViz diagram]") == 1
        rendered = fixture / "state-machine.svg"
        assert rendered.is_file() and "<svg" in rendered.read_text(encoding="utf-8")
    finally:
        readme.write_text(original, encoding="utf-8")
        rendered = fixture / "state-machine.svg"
        if rendered.exists():
            rendered.unlink()
