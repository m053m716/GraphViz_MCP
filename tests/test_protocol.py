from __future__ import annotations

import anyio
from pathlib import Path
from tempfile import TemporaryDirectory
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import server


def _structured(result):
    content = getattr(result, "structuredContent", None)
    if content is not None:
        return content
    return result.structured_content


async def _protocol_smoke() -> None:
    params = StdioServerParameters(command=str(__import__("sys").executable), args=[str(server.SERVER_ROOT / "server.py")])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "graphviz"
            assert initialized.instructions.startswith("Use GraphViz for state machines")
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {
                "graphviz_environment",
                "graphviz_validate",
                "graphviz_render",
                "graphviz_render_source",
                "graphviz_publish_markdown",
                "graphviz_sync",
            }
            environment = _structured(await session.call_tool("graphviz_environment", {}))
            assert environment["success"] is True

            with TemporaryDirectory() as temp:
                project = Path(temp)
                (project / "state-machine.dot").write_text(
                    "digraph External { Idle -> Connected [label=\"connect\"]; }\n",
                    encoding="utf-8",
                )
                (project / "README.md").write_text("# External project\n", encoding="utf-8")
                validate = _structured(
                    await session.call_tool(
                        "graphviz_validate",
                        {"project_dir": temp, "dot_path": "state-machine.dot"},
                    )
                )
                assert validate["success"] is True, validate

                render = _structured(
                    await session.call_tool(
                        "graphviz_render",
                        {"project_dir": temp, "dot_path": "state-machine.dot"},
                    )
                )
                assert render["success"] is True, render
                assert (project / "state-machine.svg").is_file()

                publish = _structured(
                    await session.call_tool(
                        "graphviz_publish_markdown",
                        {"project_dir": temp, "dot_path": "state-machine.dot", "markdown_path": "README.md"},
                    )
                )
                assert publish["success"] is True, publish
                sync = _structured(
                    await session.call_tool(
                        "graphviz_sync",
                        {"project_dir": temp, "dot_path": "state-machine.dot", "markdown_path": "README.md"},
                    )
                )
                assert sync["success"] is True and sync["updated_existing_reference"] is True, sync
                assert (project / "README.md").read_text(encoding="utf-8").count("![GraphViz diagram]") == 1


def test_stdio_protocol_level_smoke() -> None:
    anyio.run(_protocol_smoke)
