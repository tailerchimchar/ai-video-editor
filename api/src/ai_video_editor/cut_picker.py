"""Pick a cut window by asking Jev criteria questions over cut options.

Pure aside from the injected asker. Never calls Haiku. Rules-backed or
errored answers count as UNSURE (do not pass a criterion). Falls back to
event±padding with `flagged=True` when nothing passes confidently.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .cut_candidates import (
    CutOption,
    WordSpan,
    duration_range,
    fallback_padding_window,
    propose_cut_options,
)
from .cut_signals import CutSignals, FfmpegRunner, extract_cut_signals
from .decisions.models import Answer, BoolQuestion, DecisionError, TypedQuestion

_log = logging.getLogger(__name__)

# Confident pass: Jev says True with enough calibrated probability.
_MIN_CONFIDENCE = 0.6
# Minimum lead-in (seconds before event) for the lead-in criterion hint.
_MIN_LEAD_IN = 1.5

AskMany = Callable[[list[tuple[TypedQuestion, dict]]], list[Answer]]

_CRITERIA: tuple[tuple[str, str], ...] = (
    (
        "hook_within_2s",
        "Does option {oid} have a hook (loud or interesting peak) within "
        "the first 2 seconds? Use loud_peak_within_first_2s and related facts.",
    ),
    (
        "no_dead_air",
        "Does option {oid} avoid dead air longer than 1.5 seconds? "
        "Use longest_dead_air (must be <= 1.5).",
    ),
    (
        "duration_in_range",
        "Is option {oid}'s duration within duration_min..duration_max?",
    ),
    (
        "event_with_lead_in",
        "Is the event inside option {oid}'s window with enough lead-in "
        "(seconds_before_event >= min_lead_in)?",
    ),
    (
        "no_mid_speech",
        "Does option {oid} avoid cutting mid-speech? If cuts_mid_speech is "
        "absent, answer true. If present, it must be false.",
    ),
)


@dataclass(frozen=True)
class CutPick:
    start: float
    end: float
    flagged: bool
    criteria_passed: int
    option_index: int | None
    jev_ms: float = 0.0
    input_tokens: int = 0
    label: str = ""


@dataclass
class CutRunStats:
    clips: int = 0
    jev_requests: int = 0
    flagged: int = 0
    fallback: int = 0
    ffmpeg_ms: float = 0.0
    jev_ms: float = 0.0
    input_tokens: int = 0
    shadow_lines: list[str] = field(default_factory=list)

    def log_summary(self) -> None:
        _log.info(
            "jev_clip_cut run clips=%d jev_requests=%d flagged=%d fallback=%d "
            "ffmpeg_ms=%.0f jev_ms=%.0f input_tokens=%d",
            self.clips,
            self.jev_requests,
            self.flagged,
            self.fallback,
            self.ffmpeg_ms,
            self.jev_ms,
            self.input_tokens,
        )


def _option_state_payload(options: list[CutOption], event_type: str | None) -> dict:
    """One JSON state for all options of a clip — no transcript text."""
    dmin, dmax = duration_range(event_type)
    return {
        "event_type": event_type or "clip",
        "duration_min": dmin,
        "duration_max": dmax,
        "min_lead_in": _MIN_LEAD_IN,
        "options": [
            {
                "id": str(i),
                "start": opt.start,
                "end": opt.end,
                "label": opt.label,
                **opt.facts.as_state_dict(),
                "duration_min": dmin,
                "duration_max": dmax,
                "min_lead_in": _MIN_LEAD_IN,
            }
            for i, opt in enumerate(options)
        ],
    }


def _build_items(
    options: list[CutOption], event_type: str | None
) -> tuple[dict, list[tuple[TypedQuestion, dict]], list[tuple[int, str]]]:
    """Shared state + (question, state) items + (option_index, criterion) map."""
    state = _option_state_payload(options, event_type)
    items: list[tuple[TypedQuestion, dict]] = []
    index: list[tuple[int, str]] = []
    for i, _opt in enumerate(options):
        for key, template in _CRITERIA:
            # Skip mid-speech criterion when no word timestamps were supplied
            # (facts.cuts_mid_speech is None) — still ask; prompt says treat
            # absent as true so Jev can pass it.
            prompt = template.format(oid=i)
            items.append((BoolQuestion(prompt=prompt), state))
            index.append((i, key))
    return state, items, index


def _is_confident_pass(answer: Answer) -> bool:
    if answer.backend != "jev":
        return False  # rules / anything else → UNSURE
    if answer.value is not True:
        return False
    return float(answer.probability) >= _MIN_CONFIDENCE


class _TokenAccumulatingTransport:
    """Wrap a Jev transport and sum input_tokens across requests."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.input_tokens = 0
        self.requests = 0

    def ask(self, state: dict, questions: dict[str, dict]) -> dict[str, dict]:
        result = self._inner.ask(state, questions)  # type: ignore[attr-defined]
        self.requests += 1
        usage = getattr(self._inner, "last_usage", None) or {}
        if isinstance(usage, dict):
            self.input_tokens += int(usage.get("input_tokens") or 0)
        return result


def _default_jev_ask_many(
    items: list[tuple[TypedQuestion, dict]],
) -> tuple[list[Answer], int]:
    """Call Jev only (never Haiku). Returns (answers, input_tokens)."""
    from .decisions.backends import jev

    transport = jev.default_transport()
    if transport is None:
        raise DecisionError("Jev is not configured: set TYPESAFE_API_KEY")
    wrapped = _TokenAccumulatingTransport(transport)
    answers = jev.answer_many(items, transport=wrapped)  # type: ignore[arg-type]
    return answers, wrapped.input_tokens


def pick_cut(
    options: list[CutOption],
    *,
    event_time: float,
    event_type: str | None,
    duration_seconds: float,
    ask_many: AskMany | None = None,
) -> CutPick:
    """Ask Jev once (all options x criteria, shared state) and pick.

    `ask_many` is injectable for tests. When omitted, uses Jev HTTP.
    """
    fb_start, fb_end = fallback_padding_window(event_time, event_type, duration_seconds)
    if not options:
        return CutPick(
            start=fb_start, end=fb_end, flagged=True, criteria_passed=0, option_index=None
        )

    _state, items, index = _build_items(options, event_type)
    t0 = time.perf_counter()
    tokens = 0
    try:
        if ask_many is None:
            answers, tokens = _default_jev_ask_many(items)
        else:
            answers = ask_many(items)
            # Injected askers may attach tokens on a `.input_tokens` attr.
            tokens = int(getattr(ask_many, "input_tokens", 0) or 0)
    except DecisionError as exc:
        _log.warning("cut_picker: Jev error (%s); falling back to padding", exc)
        return CutPick(
            start=fb_start,
            end=fb_end,
            flagged=True,
            criteria_passed=0,
            option_index=None,
            jev_ms=(time.perf_counter() - t0) * 1000.0,
            input_tokens=tokens,
            label="fallback_error",
        )
    except Exception as exc:
        _log.warning("cut_picker: unexpected error (%s); falling back", exc)
        return CutPick(
            start=fb_start,
            end=fb_end,
            flagged=True,
            criteria_passed=0,
            option_index=None,
            jev_ms=(time.perf_counter() - t0) * 1000.0,
            input_tokens=tokens,
            label="fallback_error",
        )
    jev_ms = (time.perf_counter() - t0) * 1000.0

    # Any rules-backed answer in the batch → treat that criterion as UNSURE.
    # If *every* answer is rules-backed, fall back entirely.
    if answers and all(a.backend != "jev" for a in answers):
        return CutPick(
            start=fb_start,
            end=fb_end,
            flagged=True,
            criteria_passed=0,
            option_index=None,
            jev_ms=jev_ms,
            input_tokens=tokens,
            label="fallback_rules",
        )

    passes: list[int] = [0] * len(options)
    for answer, (opt_i, _key) in zip(answers, index, strict=True):
        if _is_confident_pass(answer):
            passes[opt_i] += 1

    best_i = max(range(len(options)), key=lambda i: (passes[i], -options[i].facts.duration))
    best_pass = passes[best_i]
    if best_pass <= 0:
        return CutPick(
            start=fb_start,
            end=fb_end,
            flagged=True,
            criteria_passed=0,
            option_index=None,
            jev_ms=jev_ms,
            input_tokens=tokens,
            label="fallback_none",
        )

    best = options[best_i]
    return CutPick(
        start=best.start,
        end=best.end,
        flagged=False,
        criteria_passed=best_pass,
        option_index=best_i,
        jev_ms=jev_ms,
        input_tokens=tokens,
        label=best.label,
    )


def pick_cuts_batch(
    clip_jobs: list[dict],
    *,
    ask_many: AskMany | None = None,
) -> list[CutPick]:
    """Pick cuts for many clips.

    Each job: `{options, event_time, event_type, duration_seconds}`.
    When feasible (injected asker or Jev), all clips share one batched
    ask by merging options into one state with clip-scoped ids. Falls
    back to per-clip `pick_cut` when the batch ask fails.
    """
    if not clip_jobs:
        return []
    if len(clip_jobs) == 1:
        job = clip_jobs[0]
        return [
            pick_cut(
                job["options"],
                event_time=job["event_time"],
                event_type=job.get("event_type"),
                duration_seconds=job["duration_seconds"],
                ask_many=ask_many,
            )
        ]

    # Build one mega-state: options keyed "c{clip}_o{opt}".
    mega_options: list[dict] = []
    items: list[tuple[TypedQuestion, dict]] = []
    index: list[tuple[int, int, str]] = []  # clip_i, opt_i, criterion
    for clip_i, job in enumerate(clip_jobs):
        dmin, dmax = duration_range(job.get("event_type"))
        for opt_i, opt in enumerate(job["options"]):
            oid = f"c{clip_i}_o{opt_i}"
            mega_options.append(
                {
                    "id": oid,
                    "clip": clip_i,
                    "start": opt.start,
                    "end": opt.end,
                    "label": opt.label,
                    **opt.facts.as_state_dict(),
                    "duration_min": dmin,
                    "duration_max": dmax,
                    "min_lead_in": _MIN_LEAD_IN,
                    "event_type": job.get("event_type") or "clip",
                }
            )
            for key, template in _CRITERIA:
                items.append((BoolQuestion(prompt=template.format(oid=oid)), {}))
                index.append((clip_i, opt_i, key))

    state = {"options": mega_options, "min_lead_in": _MIN_LEAD_IN}
    items = [(q, state) for q, _ in items]

    t0 = time.perf_counter()
    tokens = 0
    try:
        if ask_many is None:
            answers, tokens = _default_jev_ask_many(items)
        else:
            answers = ask_many(items)
            tokens = int(getattr(ask_many, "input_tokens", 0) or 0)
    except Exception as exc:
        _log.warning("cut_picker batch failed (%s); per-clip fallback", exc)
        return [
            pick_cut(
                job["options"],
                event_time=job["event_time"],
                event_type=job.get("event_type"),
                duration_seconds=job["duration_seconds"],
                ask_many=ask_many,
            )
            for job in clip_jobs
        ]
    jev_ms = (time.perf_counter() - t0) * 1000.0

    if answers and all(a.backend != "jev" for a in answers):
        out: list[CutPick] = []
        for job in clip_jobs:
            fb_s, fb_e = fallback_padding_window(
                job["event_time"], job.get("event_type"), job["duration_seconds"]
            )
            out.append(
                CutPick(
                    start=fb_s,
                    end=fb_e,
                    flagged=True,
                    criteria_passed=0,
                    option_index=None,
                    jev_ms=jev_ms / max(len(clip_jobs), 1),
                    input_tokens=tokens // max(len(clip_jobs), 1),
                    label="fallback_rules",
                )
            )
        return out

    passes: list[list[int]] = [[0] * len(job["options"]) for job in clip_jobs]
    for answer, (clip_i, opt_i, _key) in zip(answers, index, strict=True):
        if _is_confident_pass(answer):
            passes[clip_i][opt_i] += 1

    picks: list[CutPick] = []
    per_tokens = tokens // max(len(clip_jobs), 1)
    per_ms = jev_ms / max(len(clip_jobs), 1)
    for clip_i, job in enumerate(clip_jobs):
        opts: list[CutOption] = job["options"]
        fb_start, fb_end = fallback_padding_window(
            job["event_time"], job.get("event_type"), job["duration_seconds"]
        )
        if not opts or max(passes[clip_i]) <= 0:
            picks.append(
                CutPick(
                    start=fb_start,
                    end=fb_end,
                    flagged=True,
                    criteria_passed=0,
                    option_index=None,
                    jev_ms=per_ms,
                    input_tokens=per_tokens,
                    label="fallback_none",
                )
            )
            continue
        best_i = max(
            range(len(opts)),
            key=lambda i: (passes[clip_i][i], -opts[i].facts.duration),
        )
        best = opts[best_i]
        picks.append(
            CutPick(
                start=best.start,
                end=best.end,
                flagged=False,
                criteria_passed=passes[clip_i][best_i],
                option_index=best_i,
                jev_ms=per_ms,
                input_tokens=per_tokens,
                label=best.label,
            )
        )
    return picks


def resolve_clip_cut(
    path: str,
    event_time: float,
    event_type: str | None,
    duration_seconds: float,
    *,
    word_spans: list[WordSpan] | None = None,
    ask_many: AskMany | None = None,
    runner: FfmpegRunner | None = None,
    search_pad: float = 12.0,
) -> tuple[CutPick, float, CutSignals]:
    """ffmpeg signals → options → Jev pick for one clip. Fail-safe.

    Returns `(pick, ffmpeg_ms, signals)`. On signal failure still proposes
    options from empty signals and lets the picker fall back if needed.
    """
    pre_r = max(search_pad, 8.0)
    window = (
        max(0.0, event_time - pre_r),
        min(duration_seconds, event_time + pre_r) if duration_seconds > 0 else event_time + pre_r,
    )
    signals, ffmpeg_ms = extract_cut_signals(path, window, runner=runner)
    options = propose_cut_options(
        event_time,
        event_type,
        signals,
        duration_seconds=duration_seconds,
        word_spans=word_spans,
    )
    pick = pick_cut(
        options,
        event_time=event_time,
        event_type=event_type,
        duration_seconds=duration_seconds,
        ask_many=ask_many,
    )
    return pick, ffmpeg_ms, signals


def shadow_log_line(
    *,
    event_type: str | None,
    legacy_start: float,
    legacy_end: float,
    pick: CutPick,
) -> str:
    """One comparison line for shadow mode (no player names / quotes)."""
    return (
        f"jev_clip_cut shadow event={event_type or 'clip'} "
        f"delta_start={pick.start - legacy_start:.2f} "
        f"delta_end={pick.end - legacy_end:.2f} "
        f"criteria_passed={pick.criteria_passed} flagged={pick.flagged} "
        f"jev_ms={pick.jev_ms:.0f} input_tokens={pick.input_tokens} "
        f"label={pick.label or '-'}"
    )
