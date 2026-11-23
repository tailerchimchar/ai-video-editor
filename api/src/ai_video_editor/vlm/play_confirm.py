"""VLM play confirmer (game understanding spec 4.3).

Riot proposes a play; this module watches the footage around it and says
where the play really starts and ends, how many champions were in it and
whether the first kill banner lines up with the Riot clock (D14). The model
labels single 960-px frames, each with its second burned in, by state
(none / approach / fighting / aftermath); `derive_confirmation` turns the
labels into a window anchored on the Riot kill times. A kill-less sweep
(off by default) finds trades and caught-out moments Riot cannot see.

`derive_confirmation` and `apply_confirmation` are pure; the rest shells out
to ffmpeg and a backend.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..anthropic_surface import SpendCapError
from ..candidates.probe import get_duration_seconds
from ..config import settings
from ..league.map_zones import zone_of
from ..league.phases import phase_of
from ..league.play_models import (
    Phases,
    Play,
    PlayFlags,
    PlayOwner,
    PlayResult,
    PlaySides,
    PlayTimes,
    PlayWindow,
)
from ..league.play_vocab import PlayRule, PlayVocabulary
from .backends.base import VLMBackend, VLMUnavailableError, run_with_retry
from .watch_montage import _compose_mosaic

_log = logging.getLogger(__name__)

FALLBACK_LEAD_S = 8.0
FALLBACK_TAIL_S = 3.0
BANNER_TOLERANCE_S = 5.0
SWEEP_MIN_CONFIDENCE = 0.6
SWEEP_PANELS_PER_CALL = 30
FRAMES_PER_PANEL = 4
CONFIRM_WIDTH_PX = 960
CONFIRM_MAX_TOKENS = 4096
FONT_PATH = Path("C:/Windows/Fonts/arial.ttf")
FrameState = Literal["none", "approach", "fighting", "aftermath"]


class FrameLabel(BaseModel):
    """The model's reading of one frame, keyed by its burned-in second."""

    second: int
    state: FrameState
    kill_banner: bool
    allies_visible: int
    enemies_visible: int


class FrameLabels(BaseModel):
    """The confirmer's raw reply: one label per frame."""

    frames: list[FrameLabel]
    flank: bool | None = None
    description: str = ""


class PlayConfirmation(BaseModel):
    """Derived from `FrameLabels`; every second is relative to the window start."""

    approach_s: float | None = None
    engage_s: float | None = None
    outcome_s: float | None = None
    first_banner_s: float | None = None
    allies_seen: int | None = None
    enemies_seen: int | None = None
    flank: bool | None = None
    description: str = ""
    confidence: float = 0.0


class PlaySweepFinding(BaseModel):
    t_s: float
    type: Literal["trade", "caught_out"]
    description: str = ""
    confidence: float = 0.0


class _SweepResult(BaseModel):
    findings: list[PlaySweepFinding] = Field(default_factory=list)


# --- pure conversion ----------------------------------------------------------


def _runs(frames: list[FrameLabel]) -> list[tuple[str, int, int]]:
    """(state, first index, last index) of each contiguous same-state run."""
    runs: list[tuple[str, int, int]] = []
    for i, f in enumerate(frames):
        if runs and runs[-1][0] == f.state:
            runs[-1] = (f.state, runs[-1][1], i)
        else:
            runs.append((f.state, i, i))
    return runs


def derive_confirmation(
    labels: FrameLabels,
    *,
    first_kill_rel_s: float,
    last_kill_rel_s: float,
    window_s: float,
) -> PlayConfirmation:
    """Window seconds from per-frame labels, anchored on the Riot kill times
    (all relative to the window start). Pure.

    engage: start of the `fighting` run holding the first kill (run start <=
    kill <= run end + one frame step), else the last `fighting` run starting
    before it. approach: start of the `approach` run right before that
    engage run (one `none` frame may sit between). outcome: first
    `aftermath`/`none` frame at or after the last kill. first banner: first
    `kill_banner` frame at or after engage. Counts: maxima over the engage run.
    """
    seen: dict[int, FrameLabel] = {}
    for f in labels.frames:
        if 0 <= f.second <= window_s + 1.0:
            seen.setdefault(f.second, f)
    frames = [seen[k] for k in sorted(seen)]
    secs = [f.second for f in frames]
    gaps = [b - a for a, b in pairwise(secs)]
    step = float(min(gaps)) if gaps else float(window_s)
    runs = _runs(frames)

    fighting = [(i, r) for i, r in enumerate(runs) if r[0] == "fighting"]
    engage_idx: int | None = None
    for i, (_, a, b) in fighting:
        if secs[a] <= first_kill_rel_s <= secs[b] + step:
            engage_idx = i
            break
    if engage_idx is None:
        before = [i for i, (_, a, _b) in fighting if secs[a] < first_kill_rel_s]
        engage_idx = before[-1] if before else None

    engage_s = approach_s = None
    allies = enemies = None
    if engage_idx is not None:
        _, a, b = runs[engage_idx]
        engage_s = float(secs[a])
        allies = max(f.allies_visible for f in frames[a : b + 1])
        enemies = max(f.enemies_visible for f in frames[a : b + 1])
        prev = runs[engage_idx - 1] if engage_idx >= 1 else None
        if prev is not None and prev[0] == "none" and prev[1] == prev[2] and engage_idx >= 2:
            prev = runs[engage_idx - 2]
        if prev is not None and prev[0] == "approach":
            approach_s = float(secs[prev[1]])

    outcome_s = next(
        (
            float(f.second)
            for f in frames
            if f.second >= last_kill_rel_s and f.state in ("aftermath", "none")
        ),
        None,
    )
    floor = engage_s if engage_s is not None else 0.0
    first_banner_s = next(
        (float(f.second) for f in frames if f.kill_banner and f.second >= floor), None
    )
    return PlayConfirmation(
        approach_s=approach_s,
        engage_s=engage_s,
        outcome_s=outcome_s,
        first_banner_s=first_banner_s,
        allies_seen=allies,
        enemies_seen=enemies,
        flank=labels.flank,
        description=labels.description,
        confidence=1.0 if engage_s is not None else 0.0,
    )


def apply_confirmation(
    play: Play,
    conf: PlayConfirmation | None,
    window_start_s: float,
    *,
    game_end_s: float | None,
    rule: PlayRule,
) -> Play:
    """Copy of `play` with its window, numbers and flank filled from `conf`.

    Each confirmed second must sit in its slot or falls back with a flag:
    `window_start <= approach < first_kill` (else first_kill - 8,
    `approach_unconfirmed`); `approach <= engage <= first_kill` (else the
    approach, `engage_unconfirmed`); `last_kill <= outcome <= min(last_kill
    + max_after_last_kill_s, game_end + 1)` (else last_kill + 3 within that
    cap, `outcome_unconfirmed`)."""
    out = play.model_copy(deep=True)
    first = play.recording_time.first_kill_s
    last = play.recording_time.last_kill_s
    flags: list[str] = []

    def rec(rel: float | None) -> float | None:
        return None if rel is None else window_start_s + rel

    approach = rec(conf.approach_s) if conf else None
    if approach is not None and not window_start_s <= approach < first:
        approach = None
    confirmed = approach is not None
    if approach is None:
        approach = max(0.0, first - FALLBACK_LEAD_S)
        flags.append("approach_unconfirmed")
    engage = rec(conf.engage_s) if conf else None
    if engage is None or not approach <= engage <= first:
        if conf is not None:
            flags.append("engage_unconfirmed")
        engage = approach

    cap = last + float(rule.end.get("max_after_last_kill_s", 20.0))
    if game_end_s is not None:
        cap = min(cap, game_end_s + 1.0)
    outcome = rec(conf.outcome_s) if conf else None
    if outcome is None or not last <= outcome <= cap:
        outcome = max(min(last + FALLBACK_TAIL_S, cap), engage)
        flags.append("outcome_unconfirmed")

    offset = None
    if conf is not None:
        banner = rec(conf.first_banner_s)
        if banner is not None and abs(banner - first) <= BANNER_TOLERANCE_S:
            offset = banner - first
        else:
            flags.append("offset_uncertain")

    out.window = PlayWindow(
        approach_s=approach,
        engage_s=engage,
        outcome_s=outcome,
        source="vlm" if confirmed else "fallback",
        offset_correction_s=offset,
        flags=flags,
    )
    if conf is not None:
        out.flags.flank = conf.flank
        if conf.allies_seen is not None and conf.enemies_seen is not None:
            out.sides.numbers_confirmed = f"{conf.allies_seen}v{conf.enemies_seen}"
            out.sides.numbers_source = "vlm_confirmed"
            if out.owner.involved:
                out.impressive.numbers_faced = conf.enemies_seen - (conf.allies_seen - 1)
    return out


def findings_to_plays(
    findings: list[PlaySweepFinding],
    *,
    start_idx: int,
    offset_s: float,
    phases: Phases | None,
) -> list[Play]:
    """Confident sweep findings as kill-less plays (`t_rec = t_game + offset_s`)."""
    plays: list[Play] = []
    keep = [f for f in findings if f.confidence >= SWEEP_MIN_CONFIDENCE]
    for i, f in enumerate(keep, start=start_idx):
        t_game = f.t_s - offset_s
        phase = phase_of(phases, t_game) if phases is not None else "mid"
        plays.append(
            Play(
                id=f"p{i:02d}",
                type=f.type,
                phase=phase,
                game_time=PlayTimes(first_kill_s=t_game, last_kill_s=t_game),
                recording_time=PlayTimes(first_kill_s=f.t_s, last_kill_s=f.t_s),
                window=PlayWindow(
                    approach_s=f.t_s - 10.0,
                    engage_s=f.t_s - 10.0,
                    outcome_s=f.t_s + 8.0,
                    source="vlm",
                ),
                zone=zone_of(None, None),
                sides=PlaySides(allies=[], enemies=[], numbers="?v?"),
                owner=PlayOwner(involved=False, role="absent", kills=0, assists=0, died=False),
                result=PlayResult(enemy_deaths=0, ally_deaths=0, outcome="even"),
                flags=PlayFlags(help_from_team=False),
            )
        )
    return plays


# --- ffmpeg -------------------------------------------------------------------


def _extract(
    video_path: str, out_dir: Path, *, start_s: float, end_s: float, vf: str
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_s:.3f}",
        "-t",
        f"{end_s - start_s:.3f}",
        "-i",
        video_path,
        "-vf",
        vf,
        "-q:v",
        "4",
        str(out_dir / "f_%04d.jpg"),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _log.warning("play window sampling failed: %s", exc)
        return []
    return sorted(out_dir.glob("f_*.jpg"))


def _seconds_drawtext() -> str:
    """drawtext burning the second since the window start, top-left."""
    font = "fontfile='C\\:/Windows/Fonts/arial.ttf':" if FONT_PATH.is_file() else ""
    return (
        f"drawtext={font}text='%{{eif\\:t\\:d}}s':x=10:y=10:fontsize=44:"
        "fontcolor=yellow:box=1:boxcolor=black@0.7"
    )


def sample_window(
    video_path: str,
    out_dir: Path,
    *,
    start_s: float,
    end_s: float,
    fps: float | None = None,
) -> list[Path]:
    """Single 960-px frames of `[start_s, end_s]` at `fps` (default
    `settings.play_confirm_fps`), each with its second burned in top-left."""
    fps = settings.play_confirm_fps if fps is None else fps
    vf = f"fps={fps},scale={CONFIRM_WIDTH_PX}:-2,{_seconds_drawtext()}"
    return _extract(video_path, out_dir, start_s=start_s, end_s=end_s, vf=vf)


def sample_panels(
    video_path: str, out_dir: Path, *, start_s: float, end_s: float, fps: float
) -> list[Path]:
    """Frames of `[start_s, end_s]` at `fps` packed into 2x2 mosaics (the sweep)."""
    vf = f"fps={fps},scale=480:-2"
    frames = _extract(video_path, out_dir, start_s=start_s, end_s=end_s, vf=vf)
    mosaics: list[Path] = []
    for i in range(0, len(frames), FRAMES_PER_PANEL):
        out = out_dir / f"m_{i // FRAMES_PER_PANEL:03d}.jpg"
        _compose_mosaic(frames[i : i + FRAMES_PER_PANEL], out, grid=(2, 2), quality=80)
        mosaics.append(out)
    return mosaics


# --- prompts ------------------------------------------------------------------

_HUD_BASE = (
    "You are watching League of Legends gameplay from the recording owner's point of view.\n"
    "HUD notes: health bars over champions are green or blue for allies and red for enemies.\n"
    'Kill banners are large centred texts such as "You have slain an enemy", '
    '"An ally has been slain", "<name> is on a rampage!", "Double Kill", "Triple Kill".\n'
    "The Tab scoreboard overlay is NOT a defect and NOT a cue: ignore it.\n"
)

_HUD = _HUD_BASE + (
    "You are given 2x2 panels in time order. Each panel holds four consecutive frames "
    "(top-left, top-right, bottom-left, bottom-right) sampled at {fps} frame(s) per second, "
    "so one panel spans {panel_s:.1f} seconds and frame k of the whole sequence is at "
    "second k / {fps}. Report every time in seconds from the first frame (second 0)."
)


def _confirm_system(rule: PlayRule, fps: float, n_frames: int) -> str:
    step = 1.0 / fps if fps > 0 else 0.0
    return (
        _HUD_BASE
        + f"You are given {n_frames} frames in time order, one every {step:g} seconds; each "
        "frame has its second burned in at the top-left in a yellow box.\n"
        f"Play type expected: {rule.type}. It ends at: {rule.end['rule']}.\n\n"
        "Label EVERY frame by its burned-in second with exactly one state: 'none' (no enemy "
        "champions near the owner), 'approach' (enemy champions visible and both sides "
        "positioning, no damage exchanged yet), 'fighting' (abilities and damage between "
        "champions), 'aftermath' (the fight is over: survivors walking away, recalling, "
        "taking an objective or tower). kill_banner is true only when a kill banner text is "
        "visible in that frame. allies_visible / enemies_visible count champions of each side "
        "on screen (owner included in allies). flank is true when the owner's side came from "
        "behind or the side. description is one plain sentence.\n"
        'Return ONLY JSON: {"frames": [{"second": 0, "state": "none", "kill_banner": false, '
        '"allies_visible": 1, "enemies_visible": 0}, ...], "flank": true|false|null, '
        '"description": "one sentence"}'
    )


def _confirm_user(play: Play, n_frames: int) -> str:
    return (
        f"Label all {n_frames} frames. A {play.type} is expected here "
        f"(Riot saw {play.sides.numbers}, {play.result.enemy_deaths} enemy deaths)."
    )


def _sweep_system(vocab: PlayVocabulary, fps: float) -> str:
    rules = "\n".join(
        f"- {t}: {vocab.rules[t].propose}; ends: {vocab.rules[t].end['rule']}"
        for t in ("trade", "caught_out")
        if t in vocab.rules
    )
    schema = json.dumps(_SweepResult.model_json_schema(), indent=2)
    return (
        _HUD.format(fps=fps, panel_s=FRAMES_PER_PANEL / fps)
        + f"\n\nFind moments with NO kill banner that still matter:\n{rules}\n"
        f"Report each at the second it peaks. Return ONLY JSON matching this schema:\n{schema}"
    )


# --- backend calls ------------------------------------------------------------


def confirm_play(
    video_path: str,
    play: Play,
    *,
    vocab: PlayVocabulary,
    backend: VLMBackend,
    fps: float | None = None,
    lead_s: float = 30.0,
    tail_s: float = 20.0,
) -> tuple[PlayConfirmation | None, float]:
    """(confirmation or None, window start in recording seconds).

    One backend call over single labelled frames (`max_tokens=4096`); the
    reply becomes a window through `derive_confirmation`. Never raises,
    except `SpendCapError`: the caller's budget is gone and it must stop."""
    fps = settings.play_confirm_fps if fps is None else fps
    first = play.recording_time.first_kill_s
    start = max(0.0, first - lead_s)
    end = play.recording_time.last_kill_s + tail_s
    duration = get_duration_seconds(video_path)
    if duration > 0:
        end = min(end, duration)
        start = min(start, max(0.0, end - 1.0))
    try:
        rule = vocab.rule_for(play.type)
    except ValueError as exc:
        _log.warning("play confirm skipped for %s: %s", play.id, exc)
        return None, start
    with tempfile.TemporaryDirectory(prefix="play_confirm_") as tmp:
        frames = sample_window(video_path, Path(tmp), start_s=start, end_s=end, fps=fps)
        if not frames:
            return None, start
        try:
            labels = run_with_retry(
                backend,
                frames=frames,
                system_prompt=_confirm_system(rule, fps, len(frames)),
                user_prompt=_confirm_user(play, len(frames)),
                response_model=FrameLabels,
                max_tokens=CONFIRM_MAX_TOKENS,
            )
        except SpendCapError:
            _log.info("play confirm stopped at %s: budget exhausted", play.id)
            raise
        except VLMUnavailableError as exc:
            _log.info("play confirm skipped: %s", exc)
            return None, start
        except Exception as exc:  # parse failure: fall back, never fatal
            _log.warning("play confirm failed for %s: %s", play.id, exc)
            return None, start
    conf = derive_confirmation(
        labels,
        first_kill_rel_s=first - start,
        last_kill_rel_s=play.recording_time.last_kill_s - start,
        window_s=end - start,
    )
    return conf, start


def sweep_game(
    video_path: str,
    *,
    duration_s: float,
    fps: float,
    backend: VLMBackend,
    vocab: PlayVocabulary,
    should_continue: Callable[[], bool] = lambda: True,
    on_budget_exhausted: Callable[[], None] = lambda: None,
) -> list[PlaySweepFinding]:
    """Kill-less trade / caught-out findings, times in recording seconds.
    A `SpendCapError` stops the loop and calls `on_budget_exhausted`."""
    span = SWEEP_PANELS_PER_CALL * FRAMES_PER_PANEL / fps
    findings: list[PlaySweepFinding] = []
    start = 0.0
    while start < duration_s and should_continue():
        end = min(start + span, duration_s)
        with tempfile.TemporaryDirectory(prefix="play_sweep_") as tmp:
            frames = sample_panels(video_path, Path(tmp), start_s=start, end_s=end, fps=fps)
            if frames:
                try:
                    result = run_with_retry(
                        backend,
                        frames=frames,
                        system_prompt=_sweep_system(vocab, fps),
                        user_prompt=f"This stretch is {end - start:.0f} seconds long.",
                        response_model=_SweepResult,
                    )
                    findings += [
                        f.model_copy(update={"t_s": f.t_s + start}) for f in result.findings
                    ]
                except SpendCapError as exc:
                    _log.info("play sweep stopped, budget exhausted: %s", exc)
                    on_budget_exhausted()
                    break
                except VLMUnavailableError as exc:
                    _log.info("play sweep stopped: %s", exc)
                    break
                except Exception as exc:
                    _log.warning("play sweep chunk failed: %s", exc)
        start = end
    return findings
