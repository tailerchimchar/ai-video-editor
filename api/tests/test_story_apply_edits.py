"""`story-join --apply-edits`: hook edits -> managed extends -> re-render -> join."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_video_editor.config import settings
from ai_video_editor.story import apply_edits as ae
from ai_video_editor.story.hook_edits import (
    EditHook,
    EditPick,
    HookEdits,
    base_digest,
    edits_path,
    load_edits,
    save_edits,
)
from ai_video_editor.story.join_map import (
    JoinChapter,
    JoinGame,
    JoinHook,
    JoinHookPick,
    JoinMap,
    JoinOptions,
)
from ai_video_editor.story.planner import Chapter, StoryPlan
from ai_video_editor.story.render import StoryRenderError
from ai_video_editor.story.requests import Drop, Extend, StoryRequests, load_requests, save_requests

R1 = "2026-11-23_2056_zeri_8-7-12"
R2 = "2026-11-24_1010_jhin_3-1-9"
STEM = "joined_x"
OFF = 10.0


def _plan(asset: str) -> StoryPlan:
    return StoryPlan(
        asset_id=asset,
        chapters=[
            Chapter(title="a", start_s=100.0, end_s=130.0, kind="fight", play_id="p1"),
            Chapter(title="b", start_s=200.0, end_s=230.0, kind="fight", play_id="p2"),
        ],
        total_s=60.0,
        mic_stream=None,
        audio="mic_over_game",
    )


def _hook_pick(pid: str, rnd: str, at: float) -> JoinHookPick:
    return JoinHookPick(
        id=pid,
        chapter_id=f"{rnd}:p1",
        round=rnd,
        chapter_index=0,
        rec_start_s=110.0,
        rec_end_s=125.0,
        offset_s=OFF,
        at_s=at,
        video_start_s=at,
        video_end_s=at + 15,
        chapter_rec_start_s=100.0,
        chapter_rec_end_s=130.0,
        text="wow" if pid == "A" else "",
    )


def _game(rnd: str) -> JoinGame:
    return JoinGame(
        round=rnd,
        label=rnd,
        video_start_s=0,
        story_start_s=0,
        video_end_s=60,
        story_json="",
        digest_json=None,
        offset_s=OFF,
        chapters=[
            JoinChapter(
                title="a",
                kind="fight",
                video_start_s=0,
                game_start_s=90.0,
                game_end_s=120.0,
                kills=1,
                deaths=0,
                play_id="p1",
                hook_rec_start_s=110.0,
                hook_rec_end_s=125.0,
            ),
            JoinChapter(
                title="b",
                kind="fight",
                video_start_s=30,
                game_start_s=190.0,
                game_end_s=220.0,
                kills=0,
                deaths=1,
                play_id="p2",
            ),
        ],
    )


def _map(root: Path) -> JoinMap:
    return JoinMap.model_validate_json(
        (root / "story" / f"{STEM}.join.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    monkeypatch.setattr(settings, "workspace_dir", root)
    story = root / "story"
    for r, a in ((R1, "a1"), (R2, "a2")):
        (story / r).mkdir(parents=True)
        (story / r / "story.json").write_text(_plan(a).model_dump_json(), encoding="utf-8")
    hook = JoinHook(
        mode="play", title="T", picks=[_hook_pick("A", R1, 0.0), _hook_pick("B", R2, 20.0)]
    )
    jm = JoinMap(
        schema_version=2,
        video=str(story / f"{STEM}.mp4"),
        title="Series",
        duration_s=100,
        games=[_game(R1), _game(R2)],
        hook=hook,
        options=JoinOptions(subscribe_after=2, outro=True, music_start_s=3.0),
    )
    (story / f"{STEM}.join.json").write_text(jm.model_dump_json(), encoding="utf-8")
    return root


def _edits(root: Path, picks: list[EditPick], digest: str | None = None) -> HookEdits:
    e = HookEdits(
        video=STEM,
        base_digest=digest or base_digest(_map(root).hook),
        hook=EditHook(picks=picks, mode="play", title="T"),
    )
    save_edits(e)
    return e


class Rec:
    def __init__(self):
        self.renders: list[dict] = []
        self.joins: list[tuple] = []
        self.said: list[str] = []

    def render(self, **kw):
        self.renders.append(kw)

    def join(self, rounds, out, **kw):
        self.joins.append((rounds, out, kw))
        return Path(out)

    def run(self, **kw):
        return ae.apply_edits(STEM, say=self.said.append, render=self.render, join=self.join, **kw)


def test_apply_edits_refuses_stale_digest(ws):
    _edits(ws, [EditPick(id="A", chapter_id=f"{R1}:p1")], digest="sha256:old")
    r = Rec()
    with pytest.raises(StoryRenderError) as e:
        r.run()
    assert "sha256:old" in str(e.value)
    assert base_digest(_map(ws).hook) in str(e.value)
    assert r.joins == []
    r.run(force=True)
    assert len(r.joins) == 1


def test_apply_edits_needs_a_file_and_a_v2_map(ws):
    with pytest.raises(StoryRenderError, match="no edits file"):
        Rec().run()
    (ws / "story" / f"{STEM}.join.json").unlink()
    with pytest.raises(StoryRenderError):
        Rec().run()


def test_apply_edits_reorders_and_passes_offsets(ws):
    _edits(
        ws,
        [
            EditPick(id="A", chapter_id=f"{R1}:p1", at_s=30.0, end_s=2.0),
            EditPick(id="B", chapter_id=f"{R2}:p1", at_s=0.0),
        ],
    )
    r = Rec()
    out = r.run()
    [(rounds, o, kw)] = r.joins
    assert rounds == [R1, R2] and out == o
    first, second = kw["hook"]
    assert (first.game, first.chapter, first.id) == (2, 1, "B")
    assert (second.game, second.chapter, second.id, second.text) == (1, 1, "A", "wow")
    assert second.end_s == 2.0 and second.mode == "play"
    assert kw["subscribe_after"] == 2 and kw["outro"] is True
    assert kw["music_start_s"] == 3.0 and kw["title"] == "Series" and kw["hook_title"] == "T"
    assert r.renders == []  # 125 + 2 is inside the chapter (130)


def test_apply_edits_extends_the_chapter_once_and_renders_that_round(ws):
    _edits(
        ws,
        [
            EditPick(id="A", chapter_id=f"{R1}:p1", at_s=0.0, start_s=15.0, end_s=10.0),
            EditPick(id="B", chapter_id=f"{R2}:p1", at_s=40.0),
        ],
    )
    r = Rec()
    r.run()
    [ext] = load_requests(ws / "story" / R1).requests
    assert isinstance(ext, Extend) and ext.source == "clip_edit"
    assert ext.before_s == 5.0 and ext.after_s == 5.0  # 95..135 vs 100..130
    assert ext.at == 105.0  # game clock of the midpoint (115 - 10)
    assert r.renders == [{"asset_id": "a1", "round": R1}]
    assert load_requests(ws / "story" / R2).requests == []
    plan = _plan("a1")  # the re-render grew the chapter
    plan.chapters[0] = plan.chapters[0].model_copy(update={"start_s": 95.0, "end_s": 135.0})
    (ws / "story" / R1 / "story.json").write_text(plan.model_dump_json(), encoding="utf-8")
    # a second run replaces the managed request, never stacks a second one
    _edits(ws, [EditPick(id="A", chapter_id=f"{R1}:p1", start_s=16.0, end_s=10.0)])
    r2 = Rec()
    r2.run()
    [ext2] = load_requests(ws / "story" / R1).requests
    assert ext2.before_s == 6.0 and ext2.after_s == 5.0
    assert r2.renders == [{"asset_id": "a1", "round": R1}]


def test_apply_edits_keeps_other_requests_and_drops_stale_managed_ones(ws):
    d = ws / "story" / R1
    save_requests(
        d,
        StoryRequests(
            requests=[Drop(at=500), Extend(at=105, before_s=5, after_s=5, source="clip_edit")]
        ),
    )
    # the stored plan already has the extension, as a re-rendered story would
    plan = _plan("a1")
    plan.chapters[0] = plan.chapters[0].model_copy(update={"start_s": 95.0, "end_s": 135.0})
    (d / "story.json").write_text(plan.model_dump_json(), encoding="utf-8")
    _edits(ws, [EditPick(id="A", chapter_id=f"{R1}:p1")])  # back to the base window
    r = Rec()
    r.run()
    assert [type(x).__name__ for x in load_requests(d).requests] == ["Drop"]
    assert r.renders == [{"asset_id": "a1", "round": R1}]


def test_apply_edits_skips_unknown_chapters_and_says_so(ws):
    _edits(
        ws,
        [
            EditPick(id="A", chapter_id=f"{R1}:p1"),
            EditPick(id="Z", chapter_id=f"{R1}:gone", at_s=20.0),
        ],
    )
    r = Rec()
    r.run()
    assert any(f"skipped {R1}:gone" in s for s in r.said)
    [(_, _, kw)] = r.joins
    assert [p.id for p in kw["hook"]] == ["A"]


def test_apply_edits_deletes_the_file_after_the_join(ws):
    _edits(ws, [EditPick(id="A", chapter_id=f"{R1}:p1")])
    r = Rec()
    r.run()
    assert load_edits(STEM) is None and not edits_path(STEM).exists()

    def boom(*a, **k):
        raise StoryRenderError("ffmpeg died")

    _edits(ws, [EditPick(id="A", chapter_id=f"{R1}:p1")])
    with pytest.raises(StoryRenderError):
        ae.apply_edits(STEM, say=lambda s: None, render=r.render, join=boom)
    assert load_edits(STEM) is not None  # kept for a retry


def test_apply_edits_passes_both_pieces_of_a_split_chapter(ws):
    _edits(
        ws,
        [
            EditPick(id="A", chapter_id=f"{R1}:p1", at_s=0.0, start_s=2.0, end_s=-9.0),
            EditPick(id="A2", chapter_id=f"{R1}:p1", at_s=20.0, start_s=-8.0, end_s=3.0),
        ],
    )
    r = Rec()
    r.run()
    [(_, _, kw)] = r.joins
    a, b = kw["hook"]
    assert (a.game, a.chapter, a.start_s, a.end_s, a.id) == (1, 1, 2.0, -9.0, "A")
    assert (b.game, b.chapter, b.start_s, b.end_s, b.id) == (1, 1, -8.0, 3.0, "A2")
    assert r.renders == []


def test_apply_edits_resolves_an_extended_non_play_chapter(ws):
    mp = ws / "story" / f"{STEM}.join.json"
    jm = _map(ws)
    ch = jm.games[0].chapters[0]
    ch.play_id, ch.kind = None, "opening"
    jm.hook.picks[0].chapter_id = f"{R1}:opening@100.0"
    mp.write_text(jm.model_dump_json(), encoding="utf-8")
    plan = _plan("a1")  # as a previous apply left it: extended, so id and start moved
    plan.chapters[0] = plan.chapters[0].model_copy(
        update={"play_id": None, "kind": "opening", "start_s": 95.0, "end_s": 135.0}
    )
    (ws / "story" / R1 / "story.json").write_text(plan.model_dump_json(), encoding="utf-8")
    _edits(ws, [EditPick(id="A", chapter_id=f"{R1}:opening@100.0", start_s=15.0, end_s=10.0)])
    r = Rec()
    r.run()
    assert not any("skipped" in s for s in r.said)
    [(_, _, kw)] = r.joins
    assert [(p.game, p.chapter) for p in kw["hook"]] == [(1, 1)]


def test_apply_edits_re_resolves_after_the_re_render_shifts_chapters(ws):
    _edits(ws, [EditPick(id="A", chapter_id=f"{R1}:p1", start_s=15.0, end_s=10.0)])
    r = Rec()

    def render(**kw):
        r.renders.append(kw)
        plan = _plan("a1")
        front = Chapter(title="new", start_s=20.0, end_s=40.0, kind="fight", play_id="p0")
        plan.chapters = [
            front,
            plan.chapters[0].model_copy(update={"start_s": 95.0, "end_s": 135.0}),
            *plan.chapters[1:],
        ]
        (ws / "story" / R1 / "story.json").write_text(plan.model_dump_json(), encoding="utf-8")

    ae.apply_edits(STEM, say=r.said.append, render=render, join=r.join)
    assert len(r.renders) == 1
    [(_, _, kw)] = r.joins
    assert [(p.game, p.chapter) for p in kw["hook"]] == [(1, 2)]
