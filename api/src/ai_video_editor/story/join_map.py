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
from pathlib import Path

from pydantic import BaseModel

from .planner import StoryPlan
from .suggest.placement import chapter_footage_starts

SUBSCRIBE_LEAD_S = 2.0  # the overlay starts this long into the game after the card
YOUTUBE_MIN_CHAPTER_S = 10.0  # YouTube ignores chapter lists with shorter entries


class JoinChapter(BaseModel):
    title: str
    kind: str
    video_start_s: float  # in the joined video
    game_start_s: float  # game clock (what the HUD shows)
    game_end_s: float
    kills: int
    deaths: int
    items: list[int] = []  # first-back / broll item ids shown in this chapter


class JoinGame(BaseModel):
    round: str
    label: str  # "Xayah 5/0/2"
    video_start_s: float  # where the game's card (or story) starts
    story_start_s: float  # where the game's footage starts (after its card)
    video_end_s: float
    story_json: str
    digest_json: str | None
    chapters: list[JoinChapter]


class JoinInsert(BaseModel):
    kind: str  # "subscribe"
    at_s: float  # in the joined video
    dur_s: float
    asset: str


class JoinMap(BaseModel):
    video: str
    title: str
    series: str | None = None
    duration_s: float
    games: list[JoinGame]
    inserts: list[JoinInsert] = []


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
) -> JoinMap:
    """`transition_s`: each game after the first starts this long before the
    previous one ends (the swipe overlaps them; owner 2026-11-26 dropped the
    title cards). `card_s`: a "Game n" card before each game (0 without cards).
    `story_card_s`: a story's per-chapter card length (stories with cards=False
    have none). `offsets_s`: each game's recording offset (game clock =
    recording - offset). `subscribe`: (after game n, 1-based; overlay
    duration; asset path), placed SUBSCRIBE_LEAD_S into game n+1."""
    games: list[JoinGame] = []
    t = 0.0
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
            )
            for c, s in zip(plan.chapters, starts, strict=True)
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
    return JoinMap(
        video=str(video),
        title=title,
        series=series,
        duration_s=round(t, 2),
        games=games,
        inserts=inserts,
    )


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
    if intro_s > 0:
        entries.append((0.0, "Intro"))
    for g in jm.games:
        for n, c in enumerate(g.chapters):
            name = chapter_name(c.title)
            name = name if n else f"{g.label}: {name}"
            entries.append((g.video_start_s if n == 0 else c.video_start_s, name))
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
