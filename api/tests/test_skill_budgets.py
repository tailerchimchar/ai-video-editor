"""Expert skill files have a line budget (spec-expert-panel D4, D23).

SKILL.md is loaded in full every time an expert runs, so it must stay
small. Detail belongs in dictionary.md, appendix/ or craft/, and log.md.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / ".claude" / "skills"

EXPERTS = ("gaming-expert", "viewer-expert", "editing-expert", "mentor")
MAX_LINES = 400


@pytest.mark.parametrize("expert", EXPERTS)
def test_expert_skill_md_within_budget(expert: str) -> None:
    path = SKILLS_DIR / expert / "SKILL.md"
    assert path.exists(), f"{path} missing"
    lines = path.read_text(encoding="utf-8").count("\n") + 1
    assert lines <= MAX_LINES, (
        f"{expert}/SKILL.md is {lines} lines (max {MAX_LINES}); "
        "move detail into dictionary.md / appendix/ / log.md"
    )


@pytest.mark.parametrize("expert", ("gaming-expert", "viewer-expert", "editing-expert"))
def test_expert_folder_has_learning_files(expert: str) -> None:
    folder = SKILLS_DIR / expert
    for name in ("dictionary.md", "log.md"):
        assert (folder / name).exists(), f"{expert}/{name} missing"
    assert (REPO_ROOT / ".claude" / "agents" / f"{expert}.md").exists()


def test_agents_dir_is_not_gitignored() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".claude/agents/" not in [line.strip() for line in gitignore]
