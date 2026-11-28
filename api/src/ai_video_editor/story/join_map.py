"""The map of a joined video (`<joined>.join.json`): where each game and chapter
sits in the joined mp4, pointers back to each game's story.json / digest.json,
and the inserts (the subscribe overlay).

It is a map, not a merged digest: per-game data stays in the per-game files
(owner 2026-11-26), so nothing goes stale when one game is re-rendered. Pure:
callers pass durations and plans; `youtube_chapters` renders the description
chapter list from the map.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from .planner import Chapter, StoryPlan
from .suggest.placement import chapter_footage_starts

if TYPE_CHECKING:
    from .bookends import HookClip, HookMode, HookPick

SUBSCRIBE_LEAD_S = 2.0  # the overlay starts this long into the game after the card
YOUTUBE_MIN_CHAPTER_S = 10.0  # YouTube ignores chapter lists with shorter entries


def chapter_id_from(round_name: str, kind: str, play_id: str | None, rec_start_s: float) -> str:
    """A chapter's stable id: `<round>:<play id>`, or `<round>:<kind>@<start>`
    (recording seconds) for chapters that are not a play."""
    return f"{round_name}:{play_id or f'{kind}@{rec_start_s:.1f}'}"


def chapter_id(round_name: str, chapter: Chapter) -> str:
    return chapter_id_from(round_name, chapter.kind, chapter.play_id, chapter.start_s)


class JoinChapter(BaseModel):
    title: str
    kind: str
    video_start_s: float  # in the joined video
    game_start_s: float  # game clock (what the HUD shows)
    game_end_s: float
    kills: int
    deaths: int
    items: list[int] = []  # first-back / broll item ids shown in this chapter
    kill_times_s: list[float] = []  # game clock, like game_start_s
    death_times_s: list[float] = []
    play_id: str | None = None
    hook_rec_start_s: float | None = None  # base hook window, recording seconds
    hook_rec_end_s: float | None = None  # None: no events, or too short for a hook


class JoinGame(BaseModel):
    round: str
    label: str  # "Xayah 5/0/2"
    video_start_s: float  # where the game's card (or story) starts
    story_start_s: float  # where the game's footage starts (after its card)
    video_end_s: float
    story_json: str
    digest_json: str | None
    chapters: list[JoinChapter]
    offset_s: float = 0.0  # the game's recording offset (game clock = recording - offset)


class JoinInsert(BaseModel):
    kind: str  # "subscribe" | "hook" | "outro"
    at_s: float  # in the joined video
    dur_s: float
    asset: str


class JoinHookEvent(BaseModel):
    rec_s: float  # recording seconds
    kind: Literal["kill", "death"]


class JoinHookPick(BaseModel):
    id: str
    chapter_id: str
    round: str
    chapter_index: int  # 0-based in the game's story plan
    rec_start_s: float  # the base window, recording seconds
    rec_end_s: float
    offset_s: float  # the game's recording offset (game clock = recording - offset)
    start_s: float = 0.0  # owner extend: begin this much earlier
    end_s: float = 0.0  # owner extend: end this much later
    at_s: float  # where the clip starts in the hook, without the swipes
    video_start_s: float  # where its piece sits in the joined video
    video_end_s: float
    events: list[JoinHookEvent] = []
    chapter_rec_start_s: float
    chapter_rec_end_s: float
    text: str = ""


class JoinHook(BaseModel):
    mode: Literal["tease", "play"] = "tease"
    title: str | None = None
    picks: list[JoinHookPick] = []


class JoinOptions(BaseModel):
    subscribe_after: int | None = 1
    outro: bool = False
    music_start_s: float = 0.0


class JoinMap(BaseModel):
    schema_version: int = 1  # 2: carries the hook block
    video: str
    title: str
    series: str | None = None
    duration_s: float
    intro_s: float = 0.0  # the hook before game 1 (game 1 starts here)
    outro_s: float = 0.0  # the end card after the last game
    games: list[JoinGame]
    inserts: list[JoinInsert] = []
    hook: JoinHook | None = None
    options: JoinOptions = JoinOptions()


def _hook_base(plan: StoryPlan, index: int, mode: str) -> dict[str, float]:
    """`hook_rec_*` for one chapter: its base hook window, or nothing when the
    chapter has no kills or deaths or is too short for a hook."""
    from .bookends import hook_window_rec
    from .render import StoryRenderError

    c = plan.chapters[index]
    if not (c.kill_times_s or c.death_times_s):
        return {}
    try:
        start, end = hook_window_rec(plan, index, mode)  # type: ignore[arg-type]
    except StoryRenderError:
        return {}
    return {"hook_rec_start_s": start, "hook_rec_end_s": end}


def build_join_map(
    *,
    video: Path,
    title: str,
    rounds: list[str],
    labels: list[str],
    plans: list[StoryPlan],
    offsets_s: list[float],
    story_durations_s: list[float],
    card_s: float,
    story_card_s: float,
    story_jsons: list[Path],
    digest_jsons: list[Path | None],
    series: str | None = None,
    subscribe: tuple[int, float, Path] | None = None,
    transition_s: float = 0.0,
    lead_s: float = 0.0,
    outro: tuple[float, Path] | None = None,
    hook: JoinHook | None = None,
    options: JoinOptions | None = None,
    hook_mode: str = "tease",
) -> JoinMap:
    """`transition_s`: each game after the first starts this long before the
    previous one ends (the swipe overlaps them; owner 2026-11-26 dropped the
    title cards). `card_s`: a "Game n" card before each game (0 without cards).
    `story_card_s`: a story's per-chapter card length (stories with cards=False
    have none). `offsets_s`: each game's recording offset (game clock =
    recording - offset). `subscribe`: (after game n, 1-based; overlay
    duration; asset path), placed SUBSCRIBE_LEAD_S into game n+1. `lead_s`:
    where game 1 starts (after the hook; the swipe overlap already taken
    off). `outro`: (duration, file) of the end card, swiped in after the
    last game. `hook`: the hook block (makes this a version 2 map);
    `options`: the settings the join was made with. `hook_mode`: the mode each
    chapter's base hook window (`hook_rec_*`) is computed for."""
    games: list[JoinGame] = []
    t = lead_s
    for i, r in enumerate(rounds):
        if i:
            t -= transition_s
        story_start = t + card_s
        plan = plans[i]
        starts = chapter_footage_starts(plan, story_card_s)
        chapters = [
            JoinChapter(
                title=c.title,
                kind=c.kind,
                video_start_s=round(story_start + s, 2),
                game_start_s=round(c.start_s - offsets_s[i], 1),
                game_end_s=round(c.end_s - offsets_s[i], 1),
                kills=len(c.kill_times_s),
                deaths=len(c.death_times_s),
                items=[b.item_id for b in c.broll],
                kill_times_s=[round(t - offsets_s[i], 1) for t in c.kill_times_s],
                death_times_s=[round(t - offsets_s[i], 1) for t in c.death_times_s],
                play_id=c.play_id,
                **_hook_base(plan, n, hook_mode),
            )
            for n, (c, s) in enumerate(zip(plan.chapters, starts, strict=True))
        ]
        end = story_start + story_durations_s[i]
        games.append(
            JoinGame(
                round=r,
                label=labels[i],
                video_start_s=round(t, 2),
                story_start_s=round(story_start, 2),
                video_end_s=round(end, 2),
                story_json=str(story_jsons[i]),
                digest_json=str(digest_jsons[i]) if digest_jsons[i] else None,
                chapters=chapters,
                offset_s=offsets_s[i],
            )
        )
        t = end
    inserts: list[JoinInsert] = []
    if subscribe is not None:
        after, dur, asset = subscribe
        if 1 <= after < len(games):
            at = games[after].story_start_s + SUBSCRIBE_LEAD_S
            inserts.append(
                JoinInsert(kind="subscribe", at_s=round(at, 2), dur_s=dur, asset=str(asset))
            )
    if lead_s > 0:
        inserts.insert(0, JoinInsert(kind="hook", at_s=0.0, dur_s=round(lead_s, 2), asset=""))
    outro_s = 0.0
    if outro is not None:
        outro_s, outro_file = outro
        start = t - transition_s
        inserts.append(
            JoinInsert(kind="outro", at_s=round(start, 2), dur_s=outro_s, asset=str(outro_file))
        )
        t = start + outro_s
    return JoinMap(
        schema_version=2 if hook is not None else 1,
        video=str(video),
        title=title,
        series=series,
        duration_s=round(t, 2),
        intro_s=round(lead_s, 2),
        outro_s=round(outro_s, 2),
        games=games,
        inserts=inserts,
        hook=hook,
        options=options or JoinOptions(),
    )


def hook_block(
    rounds: list[str],
    plans: list[StoryPlan],
    picks: list[HookPick],
    clips: list[HookClip],
    starts_s: list[float],
    durations_s: list[float],
    offsets_s: list[float],
    *,
    mode: HookMode,
    title: str | None,
) -> JoinHook:
    """The hook block: one entry per pick with its base window (recording
    seconds, before the owner's offsets), events, chapter bounds and its
    place on the hook track (`at_s` = cumulative rendered piece lengths,
    freeze included, without the swipe). `rounds`, `plans` and `offsets_s` are per game;
    `clips`, `starts_s` (in the joined video) and `durations_s` are per pick."""
    from .bookends import hook_window_rec

    out: list[JoinHookPick] = []
    at = 0.0
    for i, p in enumerate(picks):
        plan = plans[p.game - 1]
        ch = plan.chapters[p.chapter - 1]
        rec_start, rec_end = hook_window_rec(plan, p.chapter - 1, p.mode)
        events = [JoinHookEvent(rec_s=t, kind="kill") for t in ch.kill_times_s]
        events += [JoinHookEvent(rec_s=t, kind="death") for t in ch.death_times_s]
        out.append(
            JoinHookPick(
                id=p.id or uuid.uuid4().hex[:8],
                chapter_id=chapter_id(rounds[p.game - 1], ch),
                round=rounds[p.game - 1],
                chapter_index=p.chapter - 1,
                rec_start_s=rec_start,
                rec_end_s=rec_end,
                offset_s=offsets_s[p.game - 1],
                start_s=p.start_s,
                end_s=p.end_s,
                at_s=round(at, 2),
                video_start_s=starts_s[i],
                video_end_s=round(starts_s[i] + durations_s[i], 2),
                events=sorted(
                    (e for e in events if ch.start_s <= e.rec_s <= ch.end_s),
                    key=lambda e: e.rec_s,
                ),
                chapter_rec_start_s=ch.start_s,
                chapter_rec_end_s=ch.end_s,
                text=p.text,
            )
        )
        at += durations_s[i]
    return JoinHook(mode=mode, title=title, picks=out)


_PLAY_TITLE = re.compile(
    r"^(?P<numbers>\d+v\d+) (?P<kind>[a-z_]+) in [a-z_]+,.*?,\s*"
    r"(?P<result>won|lost|even) (?P<score>\d+-for-\d+)(?P<rest>.*)$"
)
_OBJECTIVE = re.compile(r"then ([A-Z_/]+)")


def chapter_name(title: str) -> str:
    """A viewer-facing chapter name. Planner play titles such as
    "4v3 objective_fight in top_lane, owner follow_up, won 2-for-1, then
    DRAGON/AIR_DRAGON" become "4v3 objective fight · won 2-for-1 · air dragon";
    other titles are kept."""
    m = _PLAY_TITLE.match(title)
    if not m:
        return title
    parts = [f"{m['numbers']} {m['kind'].replace('_', ' ')}", f"{m['result']} {m['score']}"]
    obj = _OBJECTIVE.search(m["rest"])
    if obj:
        parts.append(obj.group(1).split("/")[-1].replace("_", " ").lower())
    return " · ".join(parts)


def _clock(s: float) -> str:
    s = int(s)
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def youtube_chapters(jm: JoinMap, *, intro_s: float = 0.0) -> str:
    """Description chapter list: one line per game and per chapter, first at
    0:00, every entry at least YOUTUBE_MIN_CHAPTER_S long (shorter ones fold
    into the previous entry)."""
    entries: list[tuple[float, str]] = []
    if max(intro_s, jm.intro_s) > 0:
        entries.append((0.0, "Intro"))
    for g in jm.games:
        for n, c in enumerate(g.chapters):
            name = chapter_name(c.title)
            name = name if n else f"{g.label}: {name}"
            entries.append((g.video_start_s if n == 0 else c.video_start_s, name))
    for ins in jm.inserts:
        if ins.kind == "outro":
            entries.append((ins.at_s, "Outro"))
    if not entries:
        return ""
    entries[0] = (0.0, entries[0][1])
    kept: list[tuple[float, str]] = []
    for i, (t, name) in enumerate(entries):
        nxt = entries[i + 1][0] if i + 1 < len(entries) else jm.duration_s
        if kept and nxt - t < YOUTUBE_MIN_CHAPTER_S:
            continue
        kept.append((t, name))
    return "\n".join(f"{_clock(t)} {name}" for t, name in kept) + "\n"
