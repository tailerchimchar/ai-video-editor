"""Single source of truth for what this pipeline can render.

Every effect the editing-expert may propose is one `EffectSpec` in
`REGISTRY`. The expert-facing `arsenal.md` is generated from it
(`python -m ai_video_editor.registry --write`), and tests fail when the
registry and the MCP server drift apart. Adding an effect means adding a
params model and one registry entry here, nothing else.

The MCP server is a sibling package and is never imported; tool names
are plain strings checked against its source by the tests.
"""

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import BaseModel

EffectKind = Literal[
    "zoom",
    "focus",
    "caption",
    "caption_video",
    "beat_flash",
    "sfx",
    "music",
    "card_text",
    "card_logo",
]
Scope = Literal["clip", "compilation", "short"]

# Effects that exist in code or in the panel's vocabulary but cannot be
# rendered today. The editor must reject or substitute them.
NOT_RENDERABLE = ("slow_mo", "speed_ramp", "transition", "pulse_zoom", "beat_snap", "time_scoped")

# Edit-plan format names (editing-expert SKILL.md). `edit_plan.validate_plan` reads this list.
FORMATS = ("hype_montage", "story_short", "vo_short", "funny_short", "arc_montage")
_FORMATS_NOT_RENDERABLE = ("arc_montage",)

ARSENAL_PATH = (
    Path(__file__).resolve().parents[3] / ".claude" / "skills" / "editing-expert" / "arsenal.md"
)


class ZoomParams(BaseModel):
    factor: float = 2.0
    roi: str = "center"


class FocusParams(BaseModel):
    x: float
    y: float
    radius: float
    dim: float


class CaptionParams(BaseModel):
    text: str | None = None  # None: caption from the transcript
    aspect: str = "16:9"


class CaptionVideoParams(BaseModel):
    segments: list[dict]
    aspect: str = "9:16"


class BeatFlashParams(BaseModel):
    beat_flash: bool = True
    beat_flash_source: str = "kills"


class SfxParams(BaseModel):
    sfx: list[dict]


class MusicParams(BaseModel):
    music_path: str


class CardTextParams(BaseModel):
    text: str
    duration: float = 2.5


class CardLogoParams(BaseModel):
    name: str
    preset: str | None = None
    duration: float = 2.5


class EffectSpec(BaseModel):
    """One renderable effect: its typed params and the MCP tool that runs it.

    `params_model` fields use the MCP tool's own parameter names so a plan
    entry can be passed straight through. `also_tools` are sibling tools
    that apply or adjust the same effect.
    """

    kind: EffectKind
    params_model: type[BaseModel]
    scope: Scope
    mcp_tool: str
    description: str
    also_tools: tuple[str, ...] = ()


def _specs(*specs: EffectSpec) -> dict[EffectKind, EffectSpec]:
    return {spec.kind: spec for spec in specs}


REGISTRY: dict[EffectKind, EffectSpec] = _specs(
    EffectSpec(
        kind="zoom",
        params_model=ZoomParams,
        scope="clip",
        mcp_tool="zoom_clip",
        description="Zoom on an ROI preset (center, scoreline_lol, minimap_lol, full).",
        also_tools=("zoom_compilation_clip",),
    ),
    EffectSpec(
        kind="focus",
        params_model=FocusParams,
        scope="clip",
        mcp_tool="focus_clip",
        description="Dim everything outside a circle at (x, y) fractions of the frame.",
        also_tools=("focus_compilation_clip",),
    ),
    EffectSpec(
        kind="caption",
        params_model=CaptionParams,
        scope="clip",
        mcp_tool="caption_clip",
        description=(
            "Caption a whole clip; omit text to use the transcript. Classic style via "
            "`caption_clip`, tiktok style via `tiktok_caption_clip`."
        ),
        also_tools=(
            "caption_compilation_clip",
            "tiktok_caption_clip",
            "tiktokify_clip_captions",
            "segment_caption_clip",
            "edit_clip_captions",
            "add_clip_caption",
            "remove_clip_caption",
        ),
    ),
    EffectSpec(
        kind="caption_video",
        params_model=CaptionVideoParams,
        scope="short",
        mcp_tool="caption_video",
        description="Burn timed caption segments onto a whole video by path.",
    ),
    EffectSpec(
        kind="beat_flash",
        params_model=BeatFlashParams,
        scope="short",
        mcp_tool="compile_shorts",
        description="Flash on detected beats or kills; also a flag on the montage tools.",
    ),
    EffectSpec(
        kind="sfx",
        params_model=SfxParams,
        scope="short",
        mcp_tool="add_sfx_at",
        description="One-shot sound effects at timestamps; ducks the source audio.",
    ),
    EffectSpec(
        kind="music",
        params_model=MusicParams,
        scope="short",
        mcp_tool="compile_shorts",
        description="Music bed under a short or montage (flat mix, no envelope).",
    ),
    EffectSpec(
        kind="card_text",
        params_model=CardTextParams,
        scope="compilation",
        mcp_tool="create_text_intro",
        description="Text intro card.",
        also_tools=("render_intro",),
    ),
    EffectSpec(
        kind="card_logo",
        params_model=CardLogoParams,
        scope="compilation",
        mcp_tool="create_intro",
        description="Logo intro card (Noodlz preset).",
        also_tools=("set_default_intro", "update_intro"),
    ),
)

EFFECT_TOOLS: frozenset[str] = frozenset(
    tool for spec in REGISTRY.values() for tool in (spec.mcp_tool, *spec.also_tools)
)


class ToolGroup(NamedTuple):
    """A set of MCP tools that are not effects but that the panel uses."""

    what: str
    tools: tuple[str, ...]


SOUND_LIBRARY = (
    ToolGroup("extract an SFX from footage into the library", ("extract_sfx",)),
    ToolGroup("download / cache a song (YouTube URL or local file)", ("download_music",)),
)

CARD_PLACEMENT = (
    ToolGroup(
        "place an intro in a compilation",
        (
            "set_compilation_intro",
            "insert_compilation_intro_at",
            "insert_compilation_intro_after",
            "clear_compilation_intro",
        ),
    ),
)

# `compile_shorts` is listed under the beat_flash and music effects.
STRUCTURE = (
    ToolGroup(
        "cut a highlight from a source by window",
        ("cut_highlights", "batch_clip_highlights", "trim_source_to_clip"),
    ),
    ToolGroup(
        "cut on ground-truth kill anchors (zero LLM cost, Outplayed log)",
        ("auto_cut_kills_from_source", "list_kill_anchors"),
    ),
    ToolGroup("compile a reel from ranked candidates", ("compile_highlights",)),
    ToolGroup(
        "shorts by topic (compile via `compile_shorts`)", ("list_shorts_topics", "list_shorts")
    ),
    ToolGroup(
        "montages",
        (
            "compile_outplayed_montage",
            "preview_outplayed_montage",
            "compile_cross_asset_montage",
            "preview_cross_asset_montage",
            "compile_custom_montage",
        ),
    ),
    ToolGroup(
        "reorder / insert / remove / extend clips in a compilation",
        (
            "reorder_compilation_clips",
            "reorder_compilation_clips_explicit",
            "insert_compilation_clip",
            "remove_compilation_clip",
            "extend_compilation_clip",
        ),
    ),
    ToolGroup("undo", ("list_compilation_history", "revert_compilation")),
    ToolGroup(
        "finalize / clean up",
        ("finalize_compilation", "cleanup_compilation", "set_compilation_labels"),
    ),
    ToolGroup("split a long VOD into games", ("split_vod_into_games",)),
)

REVIEW = (
    ToolGroup("VLM critic over the whole montage (~$0.02/60s)", ("watch_montage",)),
    ToolGroup("VLM review of a compilation", ("vlm_review_compilation",)),
    ToolGroup("content-type router", ("list_session_buckets",)),
    ToolGroup(
        "editorial corrections memory",
        ("mentor_record_correction", "mentor_recent_corrections"),
    ),
    ToolGroup(
        "transcripts and search",
        ("get_transcript", "transcribe_asset", "search_clips"),
    ),
    ToolGroup(
        "analysis and ranking",
        ("analyze_asset", "rank_asset", "generate_candidates", "list_candidates"),
    ),
    ToolGroup(
        "intro lookups",
        ("list_intro_presets", "get_default_intro", "list_intros", "get_intro"),
    ),
)

# Library, ingest, listing and housekeeping tools: they never change a
# rendered video, so the editor has no reason to name them.
_PLUMBING_TOOLS = frozenset(
    {
        "scan_assets",
        "list_assets",
        "get_job",
        "index_asset",
        "list_compilations",
        "list_compilation_clips",
        "regenerate_clip_thumbnails",
        "regenerate_thumbnail",
        "regenerate_asset_thumbnail",
        "detect_champion",
        "delete_vod_source",
        "backfill_asset_durations",
        "vlm_health",
        "ingest_vod_url",
        "get_feedback_summary",
        "list_cross_asset_shorts",
        "list_custom_shorts",
        "list_outplayed_shorts",
        "list_highlights_clips",
        "list_highlights_sessions",
        "list_outplayed_clips",
    }
)

NON_EFFECT_TOOLS: frozenset[str] = _PLUMBING_TOOLS.union(
    *(
        group.tools
        for groups in (SOUND_LIBRARY, CARD_PLACEMENT, STRUCTURE, REVIEW)
        for group in groups
    )
)


def _code(names: Sequence[str]) -> str:
    return ", ".join(f"`{name}`" for name in names)


def _table(header: tuple[str, ...], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "---|" * len(header),
    ]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _group_table(groups: Sequence[ToolGroup]) -> list[str]:
    return _table(("op", "MCP tool"), [(g.what, _code(g.tools)) for g in groups])


def _effect_rows(kinds: Sequence[EffectKind]) -> list[list[str]]:
    rows = []
    for kind in kinds:
        spec = REGISTRY[kind]
        params = _code(spec.params_model.model_fields) or "none"
        tools = _code((spec.mcp_tool, *spec.also_tools))
        rows.append([f"`{kind}`", params, spec.scope, tools, spec.description])
    return rows


_EFFECT_HEADER = ("kind", "params", "scope", "MCP tool", "what it does")
_CLIP_KINDS: tuple[EffectKind, ...] = (
    "zoom",
    "focus",
    "caption",
    "caption_video",
    "beat_flash",
)
_SOUND_KINDS: tuple[EffectKind, ...] = ("sfx", "music")
_CARD_KINDS: tuple[EffectKind, ...] = ("card_text", "card_logo")


def _formats_line() -> str:
    parts = [
        f"`{fmt}`" + (" (planned, not renderable yet)" if fmt in _FORMATS_NOT_RENDERABLE else "")
        for fmt in FORMATS
    ]
    return " · ".join(parts)


def render_arsenal_md() -> str:
    """Render the expert-facing arsenal from the registry."""
    lines = [
        "# Arsenal: what this pipeline can render today",
        "",
        "GENERATED by registry.py. Do not edit by hand: change the registry, then run",
        "`python -m ai_video_editor.registry --write`.",
        "",
        "## Formats (edit plan `format`)",
        _formats_line(),
        "",
        "## Per-clip and per-video effects (edit plan `effects[].kind`)",
        "",
        *_table(_EFFECT_HEADER, _effect_rows(_CLIP_KINDS)),
        "",
        f"Not renderable yet (reject or substitute): {_code(NOT_RENDERABLE)}.",
        "",
        "## Sound (edit plan `sfx[].sound`, `music`)",
        "",
        *_table(_EFFECT_HEADER, _effect_rows(_SOUND_KINDS)),
        "",
        *_group_table(SOUND_LIBRARY),
        "",
        "SFX names come from `WORKSPACE/media_library/<game>/sfx/`. Ask the session",
        "to list the folder before naming one.",
        "",
        "## Cards and intros (edit plan `cards[]`)",
        "",
        *_table(_EFFECT_HEADER, _effect_rows(_CARD_KINDS)),
        "",
        *_group_table(CARD_PLACEMENT),
        "",
        "## Structure (edit plan `order`, `source_clips`)",
        "",
        *_group_table(STRUCTURE),
        "",
        "## Review (not effects, but the panel uses them)",
        "",
        *_group_table(REVIEW),
        "",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Render the editing-expert arsenal.")
    parser.add_argument(
        "--write", action="store_true", help="rewrite arsenal.md instead of printing"
    )
    args = parser.parse_args(argv)
    text = render_arsenal_md()
    if args.write:
        ARSENAL_PATH.write_text(text, encoding="utf-8", newline="\n")
    else:
        print(text)


if __name__ == "__main__":
    main()
