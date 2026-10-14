from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "skills" / "graphviz-mcp" / "SKILL.md"


def test_agent_skill_mirrors_match_canonical() -> None:
    source = CANONICAL.read_text(encoding="utf-8")
    for mirror in (
        ROOT / ".agents" / "skills" / "graphviz-mcp" / "SKILL.md",
        ROOT / ".claude" / "skills" / "graphviz-mcp" / "SKILL.md",
    ):
        assert mirror.is_file()
        assert mirror.read_text(encoding="utf-8") == source
