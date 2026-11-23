"""VLM play confirmer (game understanding spec 4.3).

Riot proposes a play; this module watches the footage around it and says
where the play really starts and ends, how many champions were in it and
whether the first kill banner lines up with the Riot clock. A kill-less
sweep finds trades and caught-out moments Riot cannot see.

`apply_confirmation` is pure; the rest shells out to ffmpeg and a backend.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

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


class PlayConfirmation(BaseModel):
    """What the VLM saw; every second is relative to the sampled window start."""

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


def apply_confirmation(
    play: Play,
    conf: PlayConfirmation | None,
    window_start_s: float,
    *,
    game_end_s: float | None,
    rule: PlayRule,
) -> Play:
    """Copy of `play` with its window, numbers and flank filled from `conf`."""
    out = play.model_copy(deep=True)
    first = play.recording_time.first_kill_s
    last = play.recording_time.last_kill_s
    flags: list[str] = []

    def rec(rel: float | None) -> float | None:
        return None if rel is None else window_start_s + rel

    approach = rec(conf.approach_s) if conf else None
    if approach is not None and approach >= first:
        approach = None  # an approach after the first kill is nonsense
    confirmed = approach is not None
    if approach is None:
        approach = max(0.0, first - FALLBACK_LEAD_S)
        flags.append("approach_unconfirmed")
    engage = rec(conf.engage_s) if conf else None
    if engage is None:
        engage = approach

    cap = last + float(rule.end.get("max_after_last_kill_s", 20.0))
    if game_end_s is not None:
        cap = min(cap, game_end_s + 1.0)
    outcome = rec(conf.outcome_s) if conf else None
    if outcome is not None:
        outcome = min(outcome, cap)
    if outcome is None or outcome <= engage:
        outcome = last + FALLBACK_TAIL_S
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


def sample_window(
    video_path: str, out_dir: Path, *, start_s: float, end_s: float, fps: float
) -> list[Path]:
    """Frames of `[start_s, end_s]` at `fps`, packed into 2x2 mosaics (JPEG paths)."""
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
        f"fps={fps},scale=480:-2",
        "-q:v",
        "4",
        str(out_dir / "f_%04d.jpg"),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _log.warning("play window sampling failed: %s", exc)
        return []
    frames = sorted(out_dir.glob("f_*.jpg"))
    mosaics: list[Path] = []
    for i in range(0, len(frames), FRAMES_PER_PANEL):
        out = out_dir / f"m_{i // FRAMES_PER_PANEL:03d}.jpg"
        _compose_mosaic(frames[i : i + FRAMES_PER_PANEL], out, grid=(2, 2), quality=80)
        mosaics.append(out)
    return mosaics


# --- prompts ------------------------------------------------------------------

_HUD = (
    "You are watching League of Legends gameplay from the recording owner's point of view.\n"
    "HUD notes: health bars over champions are green or blue for allies and red for enemies.\n"
    'The kill banner reads "You have slain an enemy", "An ally has been slain", '
    '"Double Kill", "Triple Kill" and so on.\n'
    "The Tab scoreboard overlay is NOT a defect and NOT a cue: ignore it.\n"
    "You are given 2x2 panels in time order. Each panel holds four consecutive frames "
    "(top-left, top-right, bottom-left, bottom-right) sampled at {fps} frame(s) per second, "
    "so one panel spans {panel_s:.1f} seconds and frame k of the whole sequence is at "
    "second k / {fps}. Report every time in seconds from the first frame (second 0)."
)


def _confirm_system(rule: PlayRule, fps: float) -> str:
    schema = json.dumps(PlayConfirmation.model_json_schema(), indent=2)
    return (
        _HUD.format(fps=fps, panel_s=FRAMES_PER_PANEL / fps)
        + f"\n\nPlay type under review: {rule.type}.\n"
        f"Where it starts: {rule.start.get('story', 'approach')} is when the owner's side "
        f"first commits toward the fight; {rule.start.get('montage', 'engage')} is the "
        "first contact.\n"
        f"Where it ends: {rule.end['rule']} (never more than "
        f"{rule.end.get('max_after_last_kill_s')} seconds after the last kill).\n\n"
        f"Return ONLY JSON matching this schema (null for anything you cannot see):\n{schema}\n\n"
        "approach_s, engage_s and outcome_s are seconds from the first frame. first_banner_s "
        "is when the first kill banner of the play appears. allies_seen and enemies_seen "
        "count champions actually involved (owner included in allies). flank is true when "
        "the owner's side came from behind or the side. description is one plain sentence."
    )


def _confirm_user(play: Play, window_s: float) -> str:
    return (
        f"The sampled window is {window_s:.0f} seconds long. A {play.type} is expected here "
        f"(Riot saw {play.sides.numbers}, {play.result.enemy_deaths} enemy deaths). "
        "Confirm or correct it."
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
    fps: float,
    lead_s: float = 30.0,
    tail_s: float = 20.0,
) -> tuple[PlayConfirmation | None, float]:
    """(confirmation or None, window start in recording seconds). Never raises."""
    first = play.recording_time.first_kill_s
    start = max(0.0, first - lead_s)
    end = play.recording_time.last_kill_s + tail_s
    duration = get_duration_seconds(video_path)
    if duration > 0:
        end = min(end, duration)
        start = min(start, max(0.0, end - 1.0))
    rule = vocab.rules.get(play.type) or vocab.rules["skirmish"]
    with tempfile.TemporaryDirectory(prefix="play_confirm_") as tmp:
        frames = sample_window(video_path, Path(tmp), start_s=start, end_s=end, fps=fps)
        if not frames:
            return None, start
        try:
            conf = run_with_retry(
                backend,
                frames=frames,
                system_prompt=_confirm_system(rule, fps),
                user_prompt=_confirm_user(play, end - start),
                response_model=PlayConfirmation,
            )
        except VLMUnavailableError as exc:
            _log.info("play confirm skipped: %s", exc)
            return None, start
        except Exception as exc:  # parse failure, spend cap: fall back, never fatal
            _log.warning("play confirm failed for %s: %s", play.id, exc)
            return None, start
    return conf, start


def sweep_game(
    video_path: str,
    *,
    duration_s: float,
    fps: float,
    backend: VLMBackend,
    vocab: PlayVocabulary,
) -> list[PlaySweepFinding]:
    """Kill-less trade / caught-out findings, times in recording seconds."""
    span = SWEEP_PANELS_PER_CALL * FRAMES_PER_PANEL / fps
    findings: list[PlaySweepFinding] = []
    start = 0.0
    while start < duration_s:
        end = min(start + span, duration_s)
        with tempfile.TemporaryDirectory(prefix="play_sweep_") as tmp:
            frames = sample_window(video_path, Path(tmp), start_s=start, end_s=end, fps=fps)
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
                except VLMUnavailableError as exc:
                    _log.info("play sweep stopped: %s", exc)
                    break
                except Exception as exc:
                    _log.warning("play sweep chunk failed: %s", exc)
        start = end
    return findings
