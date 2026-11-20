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


# ---- Grok Bot skill files (spec-grokbot-team section 3, 10) ----

GROKBOT_DIR = REPO_ROOT / "docs" / "grokbot"
BOT_FILES = ("lead", "editor", "devops", "viewer")
GROKBOT_FILES = (*BOT_FILES, "queue-format", "setup")
BOT_REQUIRED_STRINGS = (
    "never quote another player",
    "never include summoner names",
    "default",
    "sleep",
)


@pytest.mark.parametrize("name", GROKBOT_FILES)
def test_grokbot_doc_within_budget(name: str) -> None:
    path = GROKBOT_DIR / f"{name}.md"
    assert path.exists(), f"{path} missing"
    lines = path.read_text(encoding="utf-8").count("\n") + 1
    assert lines <= MAX_LINES, f"docs/grokbot/{name}.md is {lines} lines (max {MAX_LINES})"


@pytest.mark.parametrize("name", BOT_FILES)
def test_bot_file_has_required_strings(name: str) -> None:
    path = GROKBOT_DIR / f"{name}.md"
    assert path.exists(), f"{path} missing"
    text = path.read_text(encoding="utf-8")
    for needle in BOT_REQUIRED_STRINGS:
        assert needle in text, f"docs/grokbot/{name}.md missing required string {needle!r}"


def test_queue_format_examples_parse() -> None:
    import json
    import re

    from ai_video_editor.grokbot.queue import parse_item

    path = GROKBOT_DIR / "queue-format.md"
    assert path.exists(), f"{path} missing"
    blocks = re.findall(r"```json\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL)
    # Only parse blocks that look like queue items (have a "kind" field), not event examples
    queue_blocks = []
    for block in blocks:
        try:
            data = json.loads(block)
            if isinstance(data, dict) and "kind" in data:
                queue_blocks.append(block)
        except (json.JSONDecodeError, ValueError):
            continue
    kinds = {parse_item(block).kind for block in queue_blocks}
    assert kinds == {"edit_plan", "preview_request", "verdict", "deploy", "sleep", "wake"}


def _edit_plan_example():
    import re

    from ai_video_editor.grokbot.queue import parse_item

    text = (GROKBOT_DIR / "queue-format.md").read_text(encoding="utf-8")
    for block in re.findall(r"```json\n(.*?)```", text, re.DOTALL):
        item = parse_item(block)
        if item.kind == "edit_plan":
            return item
    raise AssertionError("queue-format.md has no edit_plan example")


def test_queue_format_edit_plan_example_executes(compilation) -> None:
    """The example must survive the real validator, ref resolution and preflight."""
    from ai_video_editor.compile import load_spec, resolve_clip_ref
    from ai_video_editor.edit_plan import EditPlan, validate_plan
    from ai_video_editor.routers.plan_executor import EditsPlanExecutor

    comp_id, folder = compilation
    plan = EditPlan.model_validate(_edit_plan_example().plan.model_dump(mode="json"))
    assert validate_plan(plan, known_sounds=set()) == []
    spec = load_spec(folder)
    refs = {c.clip_ref for c in plan.source_clips} | {e.clip_ref for e in plan.effects}
    for ref in refs | set(plan.order):
        resolve_clip_ref(spec, ref)  # raises KeyError on a ref the executor rejects
    assert EditsPlanExecutor(folder, comp_id).preflight(plan) == []


def test_editor_doc_lists_only_renderable_effect_kinds() -> None:
    from ai_video_editor.registry import REGISTRY
    from ai_video_editor.routers.plan_executor import _MUTATOR_KINDS

    for name in ("editor", "queue-format"):
        text = (GROKBOT_DIR / f"{name}.md").read_text(encoding="utf-8")
        for kind in _MUTATOR_KINDS:
            assert kind in text, f"{name}.md does not list renderable kind {kind!r}"
        # `music` and `sfx` are plan sections with their own rules, not effect kinds.
        for kind in set(REGISTRY) - set(_MUTATOR_KINDS) - {"music", "sfx"}:
            assert kind not in text, f"{name}.md lists {kind!r}, which a plan cannot render"
    example_kinds = {e.kind for e in _edit_plan_example().plan.effects}
    assert example_kinds <= set(_MUTATOR_KINDS)
