"""The understanding pass: Riot proposes plays, the VLM confirms them, the
result is frozen to `WORKSPACE/plays/<asset_id>.json` (spec 4.3).

The frozen file is reused until the play vocabulary changes (`vocab_hash`)
or the caller forces a rerun. Free-text VLM descriptions go to a separate
prose sidecar and never reach the digest.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path

from ..anthropic_surface import spent_usd
from ..candidates.probe import get_duration_seconds
from ..config import settings
from ..vlm.backends.base import VLMBackend
from ..vlm.client import select_backend
from ..vlm.play_confirm import (
    PlayConfirmation,
    apply_confirmation,
    confirm_play,
    findings_to_plays,
    sweep_game,
)
from .phases import phases_for
from .play_models import GameModel, Play
from .play_vocab import PlayVocabulary
from .plays import play_text, propose_plays, rank_plays
from .riot_timeline import RiotTimeline

_log = logging.getLogger(__name__)

SWEEP_MIN_REMAINING_USD = 0.25
SPEND_PREFIX = "play_"


def plays_path(asset_id: str) -> Path:
    return Path(settings.workspace_dir) / "plays" / f"{asset_id}.json"


def _label(backend: VLMBackend, feature: str) -> None:
    """Point the backend's ledger rows at `feature` (no-op for free backends)."""
    with contextlib.suppress(AttributeError):
        backend.feature = feature  # type: ignore[attr-defined]


def _load_frozen(path: Path, vocab: PlayVocabulary) -> GameModel | None:
    if not path.is_file():
        return None
    try:
        model = GameModel.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _log.warning("unreadable plays file %s: %s", path, exc)
        return None
    if model.vocab_hash != vocab.hash:
        return None
    for p in model.plays:
        p.text = play_text(p)
    return model


def understand_game(
    asset_id: str,
    video_path: str,
    timeline: RiotTimeline,
    *,
    vocab: PlayVocabulary,
    budget_usd: float,
    fps: float,
    sweep_fps: float,
    sweep: bool,
    force: bool = False,
    out_path: Path | None = None,
    backend: VLMBackend | None = None,
    ledger_db: Path | None = None,
) -> GameModel:
    path = out_path or plays_path(asset_id)
    if not force:
        frozen = _load_frozen(path, vocab)
        if frozen is not None:
            return frozen

    def spent() -> float:
        return spent_usd(asset_id, feature_prefix=SPEND_PREFIX, db_path=ledger_db)

    phases = phases_for(timeline)
    proposed = propose_plays(timeline, phases=phases) if phases is not None else []
    for p in proposed:
        p.vocab_hash = vocab.hash

    state: dict[str, VLMBackend | None] = {"backend": backend}

    def get_backend() -> VLMBackend:
        if state["backend"] is None:
            state["backend"] = select_backend(asset_id=asset_id)
        return state["backend"]

    prose: dict[str, str] = {}
    done: list[Play] = []
    out_of_budget = False
    for play in rank_plays(proposed):
        conf: PlayConfirmation | None = None
        start = 0.0
        out_of_budget = out_of_budget or spent() >= budget_usd
        if not out_of_budget:
            be = get_backend()
            _label(be, "play_confirm")
            conf, start = confirm_play(video_path, play, vocab=vocab, backend=be, fps=fps)
        rule = vocab.rules.get(play.type) or vocab.rules["skirmish"]
        confirmed = apply_confirmation(play, conf, start, game_end_s=timeline.game_end_s, rule=rule)
        if conf is not None and conf.description:
            prose[confirmed.id] = conf.description
        done.append(confirmed)

    if sweep and phases is not None and budget_usd - spent() >= SWEEP_MIN_REMAINING_USD:
        be = get_backend()
        _label(be, "play_sweep")
        duration = get_duration_seconds(video_path)
        findings = sweep_game(
            video_path, duration_s=duration, fps=sweep_fps, backend=be, vocab=vocab
        )
        extra = findings_to_plays(
            findings, start_idx=len(done) + 1, offset_s=timeline.offset_s, phases=phases
        )
        for p in extra:
            near_kill_play = any(
                d.recording_time.first_kill_s - 10
                <= p.recording_time.first_kill_s
                <= d.recording_time.last_kill_s + 10
                for d in done
            )
            if not near_kill_play:
                p.vocab_hash = vocab.hash
                done.append(p)
                prose[p.id] = next(
                    (f.description for f in findings if f.t_s == p.recording_time.first_kill_s), ""
                )

    for p in done:
        p.text = play_text(p)
    done.sort(key=lambda p: p.game_time.first_kill_s)
    rank_plays(done)
    model = GameModel(
        phases=phases,
        plays=done,
        vocab_hash=vocab.hash,
        budget_spent_usd=round(spent(), 4),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")
    path.with_suffix(".prose.json").write_text(
        json.dumps(prose, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return model
