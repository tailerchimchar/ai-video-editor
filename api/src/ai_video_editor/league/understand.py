"""The understanding pass: Riot proposes plays, the VLM confirms them, the
result is frozen to `WORKSPACE/plays/<asset_id>.json` (spec 4.3).

The frozen file is reused until the caller forces a rerun; a changed play
vocabulary (`vocab_hash`) only logs a stale-hash warning (R6). Spend goes
to its own ledger key `plays:<asset_id>`, outside the per-asset cap (D15).
Each run gets its own full `game_understanding_budget_usd`: the backend's
cap is the key's spend at the start of the run plus the budget. Running out
(the run's spend reaching the budget, or a `SpendCapError`) stops the pass
and sets `budget_exhausted`. Owner-involved
plays are confirmed first, in rank order (D14). Free-text VLM descriptions
go to a separate prose sidecar and never reach the digest.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path

from ..anthropic_surface import SpendCapError, spent_usd
from ..candidates.probe import get_duration_seconds
from ..config import settings
from ..vlm.backends.anthropic_backend import AnthropicBackend
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
from .plays import objectives_from, play_text, propose_plays, rank_plays
from .riot_timeline import RiotTimeline

_log = logging.getLogger(__name__)

SWEEP_MIN_REMAINING_USD = 0.25
SPEND_PREFIX = "play_"
BUDGET_CAP_NAME = "GAME_UNDERSTANDING_BUDGET_USD"


def plays_path(asset_id: str) -> Path:
    return Path(settings.workspace_dir) / "plays" / f"{asset_id}.json"


def spend_key(asset_id: str) -> str:
    """Ledger key of the understanding pass (D15), like `story:<asset>`."""
    return f"plays:{asset_id}"


def default_backend(asset_id: str, *, cap_usd: float) -> VLMBackend:
    """Sonnet (`play_confirm_model`) on the `plays:<asset>` key, refusing calls
    once the key's ledger total reaches `cap_usd`, when the configured backend
    is anthropic; else the configured backend."""
    if settings.vlm_backend.lower() == "anthropic":
        return AnthropicBackend(
            asset_id=spend_key(asset_id),
            model=settings.play_confirm_model,
            feature="play_confirm",
            cap_usd=cap_usd,
            cap_name=BUDGET_CAP_NAME,
        )
    return select_backend(asset_id=asset_id)


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
        _log.warning(
            "plays file %s was built with vocabulary %s, current is %s; "
            "returning it as is (rerun with force to rebuild)",
            path,
            model.vocab_hash,
            vocab.hash,
        )
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

    def ledger() -> float:
        return spent_usd(spend_key(asset_id), feature_prefix=SPEND_PREFIX, db_path=ledger_db)

    # Per run: the loop stops once this run has spent `budget_usd`, and the
    # backend refuses calls once the key's total reaches baseline + budget.
    baseline = ledger()

    def spent() -> float:
        return ledger() - baseline

    phases = phases_for(timeline)
    proposed = propose_plays(timeline, phases=phases) if phases is not None else []
    for p in proposed:
        p.vocab_hash = vocab.hash

    state: dict[str, VLMBackend | None] = {"backend": backend}

    def get_backend() -> VLMBackend:
        if state["backend"] is None:
            state["backend"] = default_backend(asset_id, cap_usd=baseline + budget_usd)
        return state["backend"]

    prose: dict[str, str] = {}
    done: list[Play] = []
    exhausted = False
    ranked = rank_plays(proposed)
    # Owner plays first (rank order), then absent plays while budget remains.
    queue = [p for p in ranked if p.owner.involved] + [p for p in ranked if not p.owner.involved]
    for play in queue:
        conf: PlayConfirmation | None = None
        start = 0.0
        if not exhausted and spent() >= budget_usd:
            _log.warning("understanding pass stopped: run budget $%.2f spent", budget_usd)
            exhausted = True
        if not exhausted:
            be = get_backend()
            _label(be, "play_confirm")
            try:
                conf, start = confirm_play(video_path, play, vocab=vocab, backend=be, fps=fps)
            except SpendCapError as exc:
                _log.warning("understanding pass stopped: %s", exc)
                exhausted = True
        try:
            rule = vocab.rule_for(play.type)
        except ValueError as exc:
            _log.warning("play %s kept unconfirmed: %s", play.id, exc)
            done.append(play)
            continue
        confirmed = apply_confirmation(play, conf, start, game_end_s=timeline.game_end_s, rule=rule)
        if conf is not None and conf.description:
            prose[confirmed.id] = conf.description
        done.append(confirmed)

    can_sweep = not exhausted and budget_usd - spent() >= SWEEP_MIN_REMAINING_USD
    if sweep and phases is not None and can_sweep:
        be = get_backend()
        _label(be, "play_sweep")
        duration = get_duration_seconds(video_path)
        hit: list[bool] = []
        findings = sweep_game(
            video_path,
            duration_s=duration,
            fps=sweep_fps,
            backend=be,
            vocab=vocab,
            should_continue=lambda: spent() < budget_usd,
            on_budget_exhausted=lambda: hit.append(True),
        )
        exhausted = exhausted or bool(hit)
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
        objectives=objectives_from(timeline),
        vocab_hash=vocab.hash,
        budget_spent_usd=round(spent(), 4),
        budget_exhausted=exhausted,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(model.model_dump_json(indent=2), encoding="utf-8")
    path.with_suffix(".prose.json").write_text(
        json.dumps(prose, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return model
