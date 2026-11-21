"""Jev confidence cascade for the ranker.

Puts a cheap Jev pre-filter in front of the Haiku ranker. For each candidate:
  - Jev scores keep/reject + funny/hype/story (one HTTP request per candidate)
  - Confident rejects are dropped (skip Haiku)
  - Confident keeps still go to Haiku for timing; Haiku's keep verdict is trusted
  - Unsure / non-Jev / mid-band candidates go to Haiku for full ranking

The cascade saves Haiku tokens on confident rejects only. Fail-safe: any Jev
error, rules/haiku backend answers, or a mass-reject (keeps+unsure == 0)
routes candidates to Haiku so a miscalibrated Jev cannot zero out a video.

Privacy boundary
----------------
Transcripts never leave the PC on the box path. The cascade supports two modes:

  - **PC path** (``build_candidate_state_pc``): includes transcript text
    from metadata. Use only in PC-side code where transcripts stay local.
    Note: PC mode sends transcript text to TypeSafe (api.typesafe.ai).
  - **Box path** (``build_candidate_state_box``): excludes transcript fields
    entirely from both the Jev state and the Haiku payload. Safe for
    cloud/box-side code.

The ``CascadeMode`` enum selects which builder to use. Tests verify that
the box path never includes transcript fields in the serialized body.

Design choices
--------------
1. **One Jev request per candidate** — Jev's ask_many batches questions
   that share a state. Each candidate has its own state, so this is one
   HTTP request per candidate (counted against TYPESAFE_MAX_CALLS).

2. **Confident keeps still visit Haiku** — Keeps need in/out timing. They
   fold into the same Haiku call as unsure candidates. Haiku may still
   drop a keep (``keep=False`` is trusted). Only confident rejects skip
   Haiku entirely.

3. **Confident routing requires backend==\"jev\"** — Answers from rules or
   Haiku decision backends are treated as unsure so enabling the cascade
   with DECISIONS_BACKEND=haiku cannot add paid calls *and* drop
   candidates.
"""

from __future__ import annotations

import copy
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

# If Jev confidently rejects every candidate, route the whole batch to
# Haiku rather than returning zero rankings.
_MASS_REJECT_FAILSAFE = True


class CascadeMode(Enum):
    """Where the cascade is running. Determines what data goes to Jev/Haiku."""

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
    # True when answers did not come from the Jev backend (rules/haiku
    # fallback). Those candidates are treated as unsure.
    from_rules_fallback: bool = False


@dataclass(frozen=True)
class ScorerUsage:
    """Usage reported by a JevScorer. Tokens are never estimated.

    ``backend`` must be \"jev\" for cascade cost logging to record calls.
    ``request_count`` is the number of HTTP requests made (one per
    candidate when each has a distinct state). ``input_tokens`` is None
    unless the transport surfaces a real count.
    """

    input_tokens: int | None = None
    request_count: int = 0
    backend: str = "jev"


@dataclass
class CascadeStats:
    """Observability counters for one cascade batch.

    Metrics:
      - escalated_unsure: candidates Jev couldn't decide (go to Haiku for full ranking)
      - timing_refine: confident keeps sent to Haiku for in/out timing (+ keep veto)
      - haiku_candidates_saved: confidently rejected candidates that never reach Haiku
      - haiku_call_skipped: 1 if nothing needed Haiku in this batch, else 0
      - escalation_rate: escalated_unsure / total_candidates (true uncertainty rate)
      - mass_reject_failsafe: 1 if mass-reject guard fired and everything went to Haiku
    """

    total_candidates: int = 0
    jev_decided: int = 0
    jev_kept: int = 0
    jev_rejected: int = 0
    escalated_unsure: int = 0
    timing_refine: int = 0
    haiku_candidates_saved: int = 0
    haiku_call_skipped: int = 0
    mass_reject_failsafe: int = 0
    jev_latency_ms: float = 0.0
    haiku_latency_ms: float = 0.0

    @property
    def escalation_rate(self) -> float:
        """True uncertainty rate: unsure candidates / total candidates."""
        if self.total_candidates == 0:
            return 0.0
        return self.escalated_unsure / self.total_candidates


class JevScorer(Protocol):
    """Protocol for Jev scoring. Tests inject a fake."""

    def score_candidates(
        self, candidates: list[dict]
    ) -> tuple[list[JevScores | None], ScorerUsage]:
        """Score each candidate. Returns (scores, usage).
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

    PC path: transcripts stay local to the owner's machine before the
    TypeSafe call; the text field is included for richer Jev scoring.
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


def scrub_candidate_transcripts(candidate: dict) -> dict:
    """Shallow-copy a candidate with transcript fields removed from metadata.

    Used in BOX mode before sending candidates to Haiku so a box-safe
    cascade does not ship transcript text off the machine.
    """
    scrubbed = copy.copy(candidate)
    meta = dict(candidate.get("metadata") or {})
    for key in _TRANSCRIPT_FIELDS:
        meta.pop(key, None)
    scrubbed["metadata"] = meta
    return scrubbed


def state_contains_transcript(state: dict) -> bool:
    """Check if a state dict contains any transcript-related fields.

    Used by tests to verify the box path never leaks transcript data.
    Prefer asserting against ``json.dumps`` of the transport body for
    serialization-level coverage.
    """
    candidate = state.get("candidate") or {}
    return any(candidate.get(key) is not None for key in _TRANSCRIPT_FIELDS)


def _parse_jev_scores(answers: list[Answer]) -> JevScores:
    """Parse the 4 answers (keep, funny, hype, story) into JevScores.

    Any non-jev backend (rules, haiku) marks ``from_rules_fallback`` so
    confident routing only applies to real Jev answers.
    """
    keep_ans, funny_ans, hype_ans, story_ans = answers
    from_non_jev = any(a.backend != "jev" for a in answers)
    return JevScores(
        keep=bool(keep_ans.value) if keep_ans.value is not None else True,
        keep_probability=keep_ans.probability,
        funny_score=int(funny_ans.value) if funny_ans.value is not None else 5,
        hype_score=int(hype_ans.value) if hype_ans.value is not None else 5,
        story_score=int(story_ans.value) if story_ans.value is not None else 5,
        funny_probability=funny_ans.probability,
        hype_probability=hype_ans.probability,
        story_probability=story_ans.probability,
        from_rules_fallback=from_non_jev,
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

    def score_candidates(
        self, candidates: list[dict]
    ) -> tuple[list[JevScores | None], ScorerUsage]:
        """Score all candidates via ask_many.

        Each candidate becomes 4 questions with its own state dict, which
        means one Jev HTTP request per candidate when the backend is jev.

        Tokens are never estimated: ``input_tokens`` stays None unless a
        future transport surfaces real usage. Cost logging only records
        calls when every answer came from backend==\"jev\".
        """
        if not candidates:
            return [], ScorerUsage(request_count=0, backend="jev")

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
            return [None] * len(candidates), ScorerUsage(request_count=0, backend="error")

        results: list[JevScores | None] = []
        for i in range(len(candidates)):
            base = i * 4
            answers = all_answers[base : base + 4]
            results.append(_parse_jev_scores(answers))

        if any(s is None or s.from_rules_fallback for s in results):
            # rules/haiku/error — do not claim Jev spend
            return results, ScorerUsage(input_tokens=None, request_count=0, backend="rules")

        return results, ScorerUsage(
            input_tokens=None,
            request_count=len(candidates),
            backend="jev",
        )


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
    jev_scores, jev_usage = scorer.score_candidates(candidates)
    stats.jev_latency_ms = (time.perf_counter() - jev_start) * 1000

    if jev_usage.backend == "jev" and jev_usage.request_count > 0:
        # One cost record per HTTP request. Tokens stay None unless the
        # transport surfaces a real count — never estimate.
        for _ in range(jev_usage.request_count):
            cost_log.record_call(
                model=settings.typesafe_model,
                input_tokens=jev_usage.input_tokens,
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

    # Mass-reject fail-safe: if nothing would reach Haiku and everything
    # was confidently rejected, send the whole batch to Haiku instead of
    # returning zero rankings (legacy behaviour ranked them all).
    if _MASS_REJECT_FAILSAFE and stats.jev_rejected > 0 and not confident_keeps and not unsure:
        _log.warning(
            "cascade: mass-reject fail-safe — routing all %d candidates to Haiku",
            len(candidates),
        )
        unsure = list(candidates)
        stats.mass_reject_failsafe = 1
        stats.haiku_candidates_saved = 0
        stats.escalated_unsure = len(unsure)
        stats.timing_refine = 0
    else:
        stats.escalated_unsure = len(unsure)
        stats.timing_refine = len(confident_keeps)
        stats.haiku_candidates_saved = stats.jev_rejected

    haiku_candidates = unsure + [c for c, _ in confident_keeps]
    # Mass-reject path put every candidate in unsure; don't double-add keeps.
    if stats.mass_reject_failsafe:
        haiku_candidates = list(unsure)

    stats.haiku_call_skipped = 1 if not haiku_candidates else 0

    haiku_results: list[RankedCandidate] = []
    if haiku_candidates:
        haiku_payload = haiku_candidates
        if mode == CascadeMode.BOX:
            haiku_payload = [scrub_candidate_transcripts(c) for c in haiku_candidates]

        haiku_start = time.perf_counter()
        haiku_results, haiku_input, haiku_output = ranker.rank(game, haiku_payload)
        stats.haiku_latency_ms = (time.perf_counter() - haiku_start) * 1000

        # Dollars from client usage only — never estimate from a price table.
        cost_log.record_call(
            model=settings.ranker_model,
            input_tokens=haiku_input,
            output_tokens=haiku_output,
            cost_dollars=None,
        )

    haiku_by_id = {r.candidate_id: r for r in haiku_results}

    rankings: list[RankedCandidate] = []
    for candidate, scores in confident_keeps:
        cid = candidate.get("id", "")
        haiku_result = haiku_by_id.get(cid)
        if haiku_result is None:
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
        elif not haiku_result.keep:
            # Trust Haiku's drop — do not force keep=True with Jev scores.
            rankings.append(haiku_result)
        else:
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

    keep_ids = {c.get("id") for c, _ in confident_keeps}
    for result in haiku_results:
        if result.candidate_id not in keep_ids:
            rankings.append(result)

    _log.info(
        "cascade_rank: candidates=%d jev_decided=%d (kept=%d rejected=%d) "
        "escalated_unsure=%d timing_refine=%d escalation_rate=%.2f "
        "haiku_candidates_saved=%d haiku_call_skipped=%d mass_reject_failsafe=%d "
        "jev_latency_ms=%.0f haiku_latency_ms=%.0f",
        stats.total_candidates,
        stats.jev_decided,
        stats.jev_kept,
        stats.jev_rejected,
        stats.escalated_unsure,
        stats.timing_refine,
        stats.escalation_rate,
        stats.haiku_candidates_saved,
        stats.haiku_call_skipped,
        stats.mass_reject_failsafe,
        stats.jev_latency_ms,
        stats.haiku_latency_ms,
    )

    cost_log.set_cascade_metrics(
        total_candidates=stats.total_candidates,
        jev_decided=stats.jev_decided,
        jev_kept=stats.jev_kept,
        jev_rejected=stats.jev_rejected,
        escalated_unsure=stats.escalated_unsure,
        timing_refine=stats.timing_refine,
        escalation_rate=stats.escalation_rate,
        haiku_candidates_saved=stats.haiku_candidates_saved,
        haiku_call_skipped=stats.haiku_call_skipped,
    )

    cost_log.emit()

    return CascadeResult(rankings=rankings, stats=stats, cost_log=cost_log)


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

    Wired from ``routers.analyze._run_rank_job`` behind
    ``GROKBOT_JEV_RANKER_CASCADE_ENABLED`` (default off).
    """
    if not settings.grokbot_jev_ranker_cascade_enabled:
        from .ranker import rank_candidates

        return rank_candidates(game, candidates)

    result = cascade_rank(game, candidates, mode=mode, video_id=video_id, run_id=run_id)
    return result.rankings
