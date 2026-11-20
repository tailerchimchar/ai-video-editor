"""Jev confidence cascade for the ranker.

Puts a cheap Jev pre-filter in front of the Haiku ranker. For each candidate:
  - Jev scores keep/reject + funny/hype/story in one batched request
  - Confident keeps go straight to the kept set (need timing from Haiku)
  - Confident rejects are dropped
  - Unsure candidates go to Haiku for full ranking

The cascade saves Haiku cost when Jev is confident. Fail-safe: any Jev
error or rules fallback marks candidates as unsure, so they go to Haiku.

Privacy boundary
----------------
Transcripts never leave the PC. The cascade supports two modes:

  - **PC path** (``build_candidate_state_pc``): includes transcript text
    from metadata. Use only in PC-side code where transcripts stay local.
  - **Box path** (``build_candidate_state_box``): excludes transcript fields
    entirely. Only includes Outplayed events (kill/death/assist) and
    per-clip JSON metadata. Safe for cloud/box-side code.

The ``CascadeMode`` enum selects which builder to use. Tests verify that
the box path never includes transcript fields.

Design choices
--------------
1. **One Jev request per batch** — Jev's ask_many batches all candidates
   in one HTTP call per unique state. Since we pass each candidate as
   its own state (different signals), this becomes one Jev request
   per candidate. The alternative (batch many candidates into one state)
   is not supported by Jev's System One model, which evaluates questions
   against one state at a time.

2. **Timing refinement for kept candidates** — Confident keeps still
   need in/out timing and a reason. We fold them into the same Haiku
   call as the unsure candidates rather than making a separate call.
   This minimizes Haiku calls: one call ranks unsure + refines kept,
   rather than two calls (one for unsure, one for timing). The ranker
   prompt already supports variable-length batches.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from .config import settings
from .cost_log import CostLog
from .decisions import Answer, BoolQuestion, ScoreQuestion, ask_many
from .decisions.models import DecisionError
from .models import RankedCandidate

if TYPE_CHECKING:
    from collections.abc import Callable

_log = logging.getLogger(__name__)


class CascadeMode(Enum):
    """Where the cascade is running. Determines what data goes to Jev."""

    PC = "pc"
    BOX = "box"


_TRANSCRIPT_FIELDS = frozenset({"text", "transcript_text", "transcript"})


@dataclass(frozen=True)
class JevScores:
    """Jev's assessment of one candidate."""

    keep: bool
    keep_probability: float
    funny_score: int
    hype_score: int
    story_score: int
    funny_probability: float
    hype_probability: float
    story_probability: float
    from_rules_fallback: bool = False


@dataclass
class CascadeStats:
    """Observability counters for one cascade batch."""

    total_candidates: int = 0
    jev_decided: int = 0
    jev_kept: int = 0
    jev_rejected: int = 0
    escalated_to_haiku: int = 0
    haiku_calls_skipped: int = 0
    jev_latency_ms: float = 0.0
    haiku_latency_ms: float = 0.0

    @property
    def escalation_rate(self) -> float:
        if self.total_candidates == 0:
            return 0.0
        return self.escalated_to_haiku / self.total_candidates


class JevScorer(Protocol):
    """Protocol for Jev scoring. Tests inject a fake."""

    def score_candidates(self, candidates: list[dict]) -> tuple[list[JevScores | None], int]:
        """Score each candidate. Returns (scores, input_tokens).
        Returns None in scores list for candidates that errored."""
        ...


class HaikuRanker(Protocol):
    """Protocol for Haiku ranking. Tests inject a fake."""

    def rank(
        self, game: str | None, candidates: list[dict]
    ) -> tuple[list[RankedCandidate], int, int]:
        """Rank candidates. Returns (results, input_tokens, output_tokens)."""
        ...


_KEEP_QUESTION = BoolQuestion(
    prompt=(
        "Is this candidate moment worth keeping for a gameplay highlight "
        "reel (skill, hype, humor or a story beat)? Consider the event type, "
        "timing metadata, and any transcript text. Voice-only moments "
        "(transcript_keyword with funny/hype text) can be excellent even if "
        "frames look boring — do not penalize audio-led candidates."
    )
)

_FUNNY_QUESTION = ScoreQuestion(
    prompt=(
        "How funny is this candidate (0 = not funny, 10 = hilarious)? "
        "Consider transcript text, event type context, and timing. "
        "Voice-only funny moments (laughter, jokes in transcript) are "
        "valid even without visual comedy."
    ),
    lo=0,
    hi=10,
)

_HYPE_QUESTION = ScoreQuestion(
    prompt=(
        "How hype is this candidate (0 = no hype, 10 = maximum hype)? "
        "Multi-kills, aces, clutches rate high. Single kills rate medium. "
        "Audio peaks alone rate low unless paired with a kill event."
    ),
    lo=0,
    hi=10,
)

_STORY_QUESTION = ScoreQuestion(
    prompt=(
        "How much story value does this candidate have (0 = none, 10 = "
        "pivotal moment)? Comebacks, throws, turning points rate high. "
        "Routine kills rate low."
    ),
    lo=0,
    hi=10,
)


def build_candidate_state_pc(candidate: dict) -> dict:
    """Build a Jev state dict for PC-side use. Includes transcript text.

    PC path: transcripts stay local, so we can include the text field
    from metadata for richer Jev scoring.
    """
    meta = candidate.get("metadata") or {}
    return {
        "candidate": {
            "source": candidate.get("source"),
            "event_type": candidate.get("event_type"),
            "start_seconds": candidate.get("start_seconds"),
            "end_seconds": candidate.get("end_seconds"),
            "confidence": candidate.get("confidence"),
            "text": meta.get("text"),
            "rationale": meta.get("rationale"),
            "promoted_from": meta.get("promoted_from"),
            "promoted_by_source": meta.get("promoted_by_source"),
            "kills": meta.get("kills"),
            "deaths": meta.get("deaths"),
            "assists": meta.get("assists"),
        }
    }


def build_candidate_state_box(candidate: dict) -> dict:
    """Build a Jev state dict for box-side use. NO transcript text.

    Box path: transcripts must never leave the PC. Only include Outplayed
    events (kill/death/assist) and per-clip JSON metadata. The 'text'
    field and any transcript-related fields are explicitly excluded.
    """
    meta = candidate.get("metadata") or {}
    state: dict[str, Any] = {
        "candidate": {
            "source": candidate.get("source"),
            "event_type": candidate.get("event_type"),
            "start_seconds": candidate.get("start_seconds"),
            "end_seconds": candidate.get("end_seconds"),
            "confidence": candidate.get("confidence"),
            "rationale": meta.get("rationale"),
            "promoted_from": meta.get("promoted_from"),
            "promoted_by_source": meta.get("promoted_by_source"),
            "kills": meta.get("kills"),
            "deaths": meta.get("deaths"),
            "assists": meta.get("assists"),
        }
    }
    return state


def state_contains_transcript(state: dict) -> bool:
    """Check if a state dict contains any transcript-related fields.

    Used by tests to verify the box path never leaks transcript data.
    """
    candidate = state.get("candidate") or {}
    return any(candidate.get(key) is not None for key in _TRANSCRIPT_FIELDS)


def _parse_jev_scores(answers: list[Answer]) -> JevScores:
    """Parse the 4 answers (keep, funny, hype, story) into JevScores."""
    keep_ans, funny_ans, hype_ans, story_ans = answers
    from_rules = any(a.backend == "rules" for a in answers)
    return JevScores(
        keep=bool(keep_ans.value) if keep_ans.value is not None else True,
        keep_probability=keep_ans.probability,
        funny_score=int(funny_ans.value) if funny_ans.value is not None else 5,
        hype_score=int(hype_ans.value) if hype_ans.value is not None else 5,
        story_score=int(story_ans.value) if story_ans.value is not None else 5,
        funny_probability=funny_ans.probability,
        hype_probability=hype_ans.probability,
        story_probability=story_ans.probability,
        from_rules_fallback=from_rules,
    )


class DefaultJevScorer:
    """Real Jev scorer using the decisions package."""

    def __init__(
        self,
        ask_many_fn: Callable[..., list[Answer]] | None = None,
        mode: CascadeMode = CascadeMode.PC,
    ) -> None:
        self._ask_many = ask_many_fn or ask_many
        self._mode = mode
        self._build_state = (
            build_candidate_state_pc if mode == CascadeMode.PC else build_candidate_state_box
        )

    def score_candidates(self, candidates: list[dict]) -> tuple[list[JevScores | None], int]:
        """Score all candidates. Uses one batched ask_many call.

        Each candidate becomes 4 questions (keep, funny, hype, story) with
        its own state dict. ask_many batches efficiently — one Jev request
        per unique state (which here is per candidate, since each has
        different signals).

        Returns (scores, estimated_input_tokens). Tokens are estimated as
        Jev doesn't report exact usage in the response we have access to.
        """
        if not candidates:
            return [], 0

        items: list[tuple] = []
        for candidate in candidates:
            state = self._build_state(candidate)
            items.extend(
                [
                    (_KEEP_QUESTION, state),
                    (_FUNNY_QUESTION, state),
                    (_HYPE_QUESTION, state),
                    (_STORY_QUESTION, state),
                ]
            )

        try:
            all_answers = self._ask_many(items)
        except DecisionError as exc:
            _log.warning("jev cascade: ask_many failed (%s), all candidates unsure", exc)
            return [None] * len(candidates), 0

        results: list[JevScores | None] = []
        for i in range(len(candidates)):
            base = i * 4
            answers = all_answers[base : base + 4]
            results.append(_parse_jev_scores(answers))

        estimated_tokens = len(candidates) * 150
        return results, estimated_tokens


@dataclass
class CascadeResult:
    """Result of the cascade ranking."""

    rankings: list[RankedCandidate]
    stats: CascadeStats
    cost_log: CostLog


def _has_thin_signals(candidate: dict) -> bool:
    """Check if a candidate has thin signals (voice-only or audio-only).
    These should not be confidently rejected based on Jev alone."""
    source = candidate.get("source")
    if source in ("audio_peak", "transcript_keyword"):
        meta = candidate.get("metadata") or {}
        if not meta.get("promoted_from"):
            return True
    return False


def cascade_rank(
    game: str | None,
    candidates: list[dict],
    *,
    jev_scorer: JevScorer | None = None,
    haiku_ranker: HaikuRanker | None = None,
    keep_threshold: float | None = None,
    reject_threshold: float | None = None,
    mode: CascadeMode = CascadeMode.PC,
    video_id: str | None = None,
    run_id: str | None = None,
) -> CascadeResult:
    """Run the Jev -> Haiku confidence cascade.

    Args:
        game: Game name for the Haiku ranker.
        candidates: List of candidate dicts to rank.
        jev_scorer: Injectable Jev scorer (tests use fakes).
        haiku_ranker: Injectable Haiku ranker (tests use fakes).
        keep_threshold: Confidence threshold for Jev keeps (default from config).
        reject_threshold: Confidence threshold for Jev rejects (default from config).
        mode: PC or BOX — determines if transcript text is included.
        video_id: For cost logging.
        run_id: For cost logging.

    Returns:
        CascadeResult with rankings, stats, and cost_log.
    """
    from .ranker import rank_candidates as default_haiku_ranker

    cost_log = CostLog(video_id=video_id, run_id=run_id)
    stats = CascadeStats(total_candidates=len(candidates))

    if not candidates:
        return CascadeResult(rankings=[], stats=stats, cost_log=cost_log)

    keep_thresh = (
        keep_threshold if keep_threshold is not None else settings.grokbot_jev_keep_threshold
    )
    reject_thresh = (
        reject_threshold if reject_threshold is not None else settings.grokbot_jev_reject_threshold
    )
    scorer = jev_scorer or DefaultJevScorer(mode=mode)
    ranker = haiku_ranker or _HaikuRankerAdapter(default_haiku_ranker)

    jev_start = time.perf_counter()
    jev_scores, jev_input_tokens = scorer.score_candidates(candidates)
    stats.jev_latency_ms = (time.perf_counter() - jev_start) * 1000

    cost_log.record_call(
        model=settings.typesafe_model,
        input_tokens=jev_input_tokens,
        cost_dollars=None,
    )

    confident_keeps: list[tuple[dict, JevScores]] = []
    unsure: list[dict] = []

    for candidate, scores in zip(candidates, jev_scores, strict=True):
        if scores is None or scores.from_rules_fallback:
            unsure.append(candidate)
            continue

        if scores.keep and scores.keep_probability >= keep_thresh:
            if _has_thin_signals(candidate):
                unsure.append(candidate)
            else:
                confident_keeps.append((candidate, scores))
                stats.jev_kept += 1
                stats.jev_decided += 1
        elif not scores.keep and scores.keep_probability >= reject_thresh:
            if _has_thin_signals(candidate):
                unsure.append(candidate)
            else:
                stats.jev_rejected += 1
                stats.jev_decided += 1
        else:
            unsure.append(candidate)

    stats.escalated_to_haiku = len(unsure) + len(confident_keeps)
    stats.haiku_calls_skipped = stats.jev_rejected

    haiku_candidates = unsure + [c for c, _ in confident_keeps]

    haiku_results: list[RankedCandidate] = []
    if haiku_candidates:
        haiku_start = time.perf_counter()
        haiku_results, haiku_input, haiku_output = ranker.rank(game, haiku_candidates)
        stats.haiku_latency_ms = (time.perf_counter() - haiku_start) * 1000

        haiku_cost = _estimate_haiku_cost(haiku_input, haiku_output)
        cost_log.record_call(
            model=settings.ranker_model,
            input_tokens=haiku_input,
            output_tokens=haiku_output,
            cost_dollars=haiku_cost,
        )

    haiku_by_id = {r.candidate_id: r for r in haiku_results}

    rankings: list[RankedCandidate] = []
    for candidate, scores in confident_keeps:
        cid = candidate.get("id", "")
        haiku_result = haiku_by_id.get(cid)
        if haiku_result:
            rankings.append(
                RankedCandidate(
                    candidate_id=cid,
                    keep=True,
                    funny_score=scores.funny_score / 10.0,
                    hype_score=scores.hype_score / 10.0,
                    story_score=scores.story_score / 10.0,
                    suggested_start_seconds=haiku_result.suggested_start_seconds,
                    suggested_end_seconds=haiku_result.suggested_end_seconds,
                    reason=haiku_result.reason,
                )
            )
        else:
            rankings.append(
                RankedCandidate(
                    candidate_id=cid,
                    keep=True,
                    funny_score=scores.funny_score / 10.0,
                    hype_score=scores.hype_score / 10.0,
                    story_score=scores.story_score / 10.0,
                    suggested_start_seconds=candidate.get("start_seconds", 0.0),
                    suggested_end_seconds=candidate.get("end_seconds", 0.0),
                    reason="Jev confident keep (timing from candidate bounds)",
                )
            )

    for result in haiku_results:
        is_kept_candidate = any(c.get("id") == result.candidate_id for c, _ in confident_keeps)
        if not is_kept_candidate:
            rankings.append(result)

    _log.info(
        "cascade_rank: candidates=%d jev_decided=%d (kept=%d rejected=%d) "
        "escalated_to_haiku=%d escalation_rate=%.2f haiku_calls_skipped=%d "
        "jev_latency_ms=%.0f haiku_latency_ms=%.0f",
        stats.total_candidates,
        stats.jev_decided,
        stats.jev_kept,
        stats.jev_rejected,
        stats.escalated_to_haiku,
        stats.escalation_rate,
        stats.haiku_calls_skipped,
        stats.jev_latency_ms,
        stats.haiku_latency_ms,
    )

    cost_log.emit()

    return CascadeResult(rankings=rankings, stats=stats, cost_log=cost_log)


def _estimate_haiku_cost(input_tokens: int, output_tokens: int) -> float:
    """Estimate Haiku cost from token counts.

    Haiku 4.5 pricing (as of 2026):
      - Input: $0.80 / 1M tokens
      - Output: $4.00 / 1M tokens
    """
    input_cost = (input_tokens / 1_000_000) * 0.80
    output_cost = (output_tokens / 1_000_000) * 4.00
    return round(input_cost + output_cost, 6)


class _HaikuRankerAdapter:
    """Adapts the existing rank_candidates function to the HaikuRanker protocol."""

    def __init__(self, rank_fn: Callable) -> None:
        self._rank = rank_fn

    def rank(
        self, game: str | None, candidates: list[dict]
    ) -> tuple[list[RankedCandidate], int, int]:
        from .ranker import _last_usage

        results = self._rank(game, candidates)
        usage = _last_usage.get("rank", {})
        return results, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


def rank_candidates_with_cascade(
    game: str | None,
    candidates: list[dict],
    *,
    mode: CascadeMode = CascadeMode.PC,
    video_id: str | None = None,
    run_id: str | None = None,
) -> list[RankedCandidate]:
    """Entry point that uses cascade if enabled, else falls back to direct Haiku.

    This is the function that replaces the existing rank_candidates call
    when the cascade is enabled.
    """
    if not settings.grokbot_jev_ranker_cascade_enabled:
        from .ranker import rank_candidates

        return rank_candidates(game, candidates)

    result = cascade_rank(game, candidates, mode=mode, video_id=video_id, run_id=run_id)
    return result.rankings
