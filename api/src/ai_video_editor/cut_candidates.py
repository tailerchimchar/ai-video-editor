"""Propose cut (start, end) options from event time + ffmpeg signals.

Pure — no ffmpeg, no network, no transcript text. Optional word
timestamps are `(start, end)` pairs only; never accept or store words.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import settings
from .cut_signals import CutSignals, QuietSpan

# Assist/death Overwolf windows drift 4-9 s; search wider than padding.
_WIDE_EVENTS = frozenset({"assist", "death"})
_WIDE_EXTRA_SECONDS = 6.0
_SNAP_TOLERANCE = 2.5
_QUIET_EDGE_TOLERANCE = 0.35
_LOUD_THRESHOLD_LUFS = -35.0
_DEAD_AIR_MIN = 0.4


@dataclass(frozen=True)
class CutFacts:
    seconds_before_event: float
    seconds_after_event: float
    duration: float
    longest_dead_air: float
    starts_on_quiet: bool
    ends_on_quiet: bool
    loud_peak_within_first_2s: bool
    cuts_through_loud_span: bool
    cuts_mid_speech: bool | None = None

    def as_state_dict(self) -> dict:
        """JSON-safe facts for a Jev state. Never includes speech text."""
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True)
class CutOption:
    start: float
    end: float
    facts: CutFacts
    label: str = ""


@dataclass(frozen=True)
class WordSpan:
    """Speech interval by timestamp only — no text payload."""

    start: float
    end: float


def _pre_post(event_type: str | None) -> tuple[float, float]:
    if event_type and event_type in settings.event_window_overrides:
        return settings.event_window_overrides[event_type]
    return settings.highlight_pre_seconds, settings.highlight_post_seconds


def _search_radius(event_type: str | None, pre: float, post: float) -> tuple[float, float]:
    extra = _WIDE_EXTRA_SECONDS if (event_type or "").lower() in _WIDE_EVENTS else 0.0
    return pre + extra, post + extra


def _clamp_window(
    start: float, end: float, *, duration: float, min_len: float = 1.0
) -> tuple[float, float]:
    start = max(0.0, start)
    end = min(duration, end) if duration > 0 else end
    if end - start < min_len:
        end = min(duration if duration > 0 else start + min_len, start + min_len)
    # Past-EOF / inverted windows stay degenerate (end <= start) so callers
    # can drop them — never invent end < start after a min_len bump.
    if end < start:
        return round(start, 2), round(start, 2)
    return round(start, 2), round(end, 2)


def _nearest_quiet_boundary(quiet: list[QuietSpan], target: float, *, prefer: str) -> float | None:
    """Snap `target` to a quiet-span edge within tolerance.

    prefer='start' → prefer silence_end (leave quiet before the cut-in).
    prefer='end'   → prefer silence_start (enter quiet after the cut-out).
    """
    best: float | None = None
    best_dist = _SNAP_TOLERANCE + 1.0
    for span in quiet:
        candidates = [span.end] if prefer == "start" else [span.start]
        # Also allow the opposite edge when nothing closer fits.
        candidates.append(span.start if prefer == "start" else span.end)
        for edge in candidates:
            dist = abs(edge - target)
            if dist <= _SNAP_TOLERANCE and dist < best_dist:
                best = edge
                best_dist = dist
    return best


def _nearest_scene(scenes: list[float], target: float) -> float | None:
    if not scenes:
        return None
    best = min(scenes, key=lambda t: abs(t - target))
    return best if abs(best - target) <= _SNAP_TOLERANCE else None


def _on_quiet(quiet: list[QuietSpan], t: float) -> bool:
    for span in quiet:
        if span.start - _QUIET_EDGE_TOLERANCE <= t <= span.end + _QUIET_EDGE_TOLERANCE:
            return True
    return False


def _longest_dead_air(quiet: list[QuietSpan], start: float, end: float) -> float:
    longest = 0.0
    for span in quiet:
        lo = max(span.start, start)
        hi = min(span.end, end)
        if hi - lo >= _DEAD_AIR_MIN:
            longest = max(longest, hi - lo)
    return round(longest, 3)


def _loud_steps_in(signals: CutSignals, start: float, end: float) -> list[tuple[float, float]]:
    return [(s.time, s.loudness) for s in signals.loudness if start <= s.time <= end]


def _loud_peak_within_first_2s(signals: CutSignals, start: float) -> bool:
    for _t, loud in _loud_steps_in(signals, start, start + 2.0):
        if loud >= _LOUD_THRESHOLD_LUFS:
            return True
    return False


def _cuts_through_loud_span(signals: CutSignals, start: float, end: float) -> bool:
    """True when either cut lands inside a loud (non-quiet) region."""
    for edge in (start, end):
        if _on_quiet(signals.quiet_spans, edge):
            continue
        nearby = [
            loud
            for t, loud in _loud_steps_in(signals, edge - 0.25, edge + 0.25)
            if loud >= _LOUD_THRESHOLD_LUFS
        ]
        if nearby:
            return True
    return False


def _cuts_mid_speech(words: list[WordSpan] | None, start: float, end: float) -> bool | None:
    if words is None:
        return None
    # True when start or end falls strictly inside a word interval.
    return any(w.start < start < w.end or w.start < end < w.end for w in words)


def compute_facts(
    start: float,
    end: float,
    event_time: float,
    signals: CutSignals,
    word_spans: list[WordSpan] | None = None,
) -> CutFacts:
    return CutFacts(
        seconds_before_event=round(event_time - start, 3),
        seconds_after_event=round(end - event_time, 3),
        duration=round(end - start, 3),
        longest_dead_air=_longest_dead_air(signals.quiet_spans, start, end),
        starts_on_quiet=_on_quiet(signals.quiet_spans, start),
        ends_on_quiet=_on_quiet(signals.quiet_spans, end),
        loud_peak_within_first_2s=_loud_peak_within_first_2s(signals, start),
        cuts_through_loud_span=_cuts_through_loud_span(signals, start, end),
        cuts_mid_speech=_cuts_mid_speech(word_spans, start, end),
    )


def _dedupe_options(options: list[CutOption]) -> list[CutOption]:
    seen: set[tuple[float, float]] = set()
    out: list[CutOption] = []
    for opt in options:
        key = (opt.start, opt.end)
        if key in seen:
            continue
        seen.add(key)
        out.append(opt)
    return out


def propose_cut_options(
    event_time: float,
    event_type: str | None,
    signals: CutSignals,
    *,
    duration_seconds: float,
    word_spans: list[WordSpan] | None = None,
    max_options: int = 5,
    seed_start: float | None = None,
    seed_end: float | None = None,
) -> list[CutOption]:
    """Build 3-5 (start, end) candidates snapped to quiet / scene cues.

    Always includes a base window. When ``seed_start``/``seed_end`` are set
    (legacy ``cut_window`` / cluster first→last kill span), that seed is the
    base and every option is expanded to cover it — never narrower than the
    legacy union. Otherwise the base is event±padding. Assist/death search
    a wider neighborhood for snap points (Overwolf drift).
    """
    pre, post = _pre_post(event_type)
    search_pre, search_post = _search_radius(event_type, pre, post)
    file_dur = duration_seconds

    pad_start, pad_end = _clamp_window(event_time - pre, event_time + post, duration=file_dur)
    if seed_start is not None and seed_end is not None and float(seed_end) > float(seed_start):
        base_start, base_end = _clamp_window(float(seed_start), float(seed_end), duration=file_dur)
    else:
        base_start, base_end = pad_start, pad_end

    options: list[CutOption] = []

    def _add(start: float, end: float, label: str) -> None:
        if end <= start:
            return
        # Event must sit inside the window.
        if not (start <= event_time <= end):
            return
        s, e = _clamp_window(start, end, duration=file_dur)
        # Never shrink below the seed/legacy union (Rampage / multi-kill).
        s = min(s, base_start)
        e = max(e, base_end)
        s, e = _clamp_window(s, e, duration=file_dur)
        if e <= s:
            return
        facts = compute_facts(s, e, event_time, signals, word_spans)
        options.append(CutOption(start=s, end=e, facts=facts, label=label))

    _add(base_start, base_end, "padding")

    # Snap start/end independently to quiet edges near the padding targets.
    q_start = _nearest_quiet_boundary(signals.quiet_spans, event_time - pre, prefer="start")
    q_end = _nearest_quiet_boundary(signals.quiet_spans, event_time + post, prefer="end")
    if q_start is not None or q_end is not None:
        _add(
            q_start if q_start is not None else base_start,
            q_end if q_end is not None else base_end,
            "quiet_snap",
        )

    # Scene-change snap near the same targets.
    sc_start = _nearest_scene(signals.scene_changes, event_time - pre)
    sc_end = _nearest_scene(signals.scene_changes, event_time + post)
    if sc_start is not None or sc_end is not None:
        _add(
            sc_start if sc_start is not None else base_start,
            sc_end if sc_end is not None else base_end,
            "scene_snap",
        )

    # Mixed: quiet in + scene out (and reverse) when both signals exist.
    if q_start is not None and sc_end is not None:
        _add(q_start, sc_end, "quiet_in_scene_out")
    if sc_start is not None and q_end is not None:
        _add(sc_start, q_end, "scene_in_quiet_out")

    # Wider padding variant for assist/death (or when we still need a 3rd).
    if (event_type or "").lower() in _WIDE_EVENTS or len(options) < 3:
        wide_start, wide_end = _clamp_window(
            event_time - search_pre,
            event_time + search_post,
            duration=file_dur,
        )
        _add(wide_start, wide_end, "wide_padding")

    # Slightly tighter post for celebration-light events when short on options.
    if len(options) < 3:
        tight_start, tight_end = _clamp_window(
            event_time - pre,
            event_time + max(2.0, post * 0.6),
            duration=file_dur,
        )
        _add(tight_start, tight_end, "tight_post")

    # Lead-heavy variant: more pre, same post.
    if len(options) < 3:
        lead_start, lead_end = _clamp_window(
            event_time - (pre + 2.0),
            event_time + post,
            duration=file_dur,
        )
        _add(lead_start, lead_end, "lead_heavy")

    deduped = _dedupe_options(options)
    # Prefer a stable order: padding first, then by duration ascending.
    deduped.sort(
        key=lambda o: (
            0 if o.label == "padding" else 1,
            o.facts.duration,
            o.start,
        )
    )
    # Guarantee 3-5 when possible; pad with small shifts of the base window.
    i = 1
    while len(deduped) < 3 and i <= 3:
        shift = 0.5 * i
        alt_s, alt_e = _clamp_window(base_start - shift, base_end + shift * 0.5, duration=file_dur)
        before = len(deduped)
        _add(alt_s, alt_e, f"shift_{i}")
        deduped = _dedupe_options(options)
        if len(deduped) == before:
            break
        i += 1

    return (
        deduped[: max(3, min(max_options, 5))]
        if deduped
        else [
            CutOption(
                start=base_start,
                end=base_end,
                facts=compute_facts(base_start, base_end, event_time, signals, word_spans),
                label="padding",
            )
        ]
    )


def fallback_padding_window(
    event_time: float,
    event_type: str | None,
    duration_seconds: float,
) -> tuple[float, float]:
    """Event time ± current padding — the safe fallback cut."""
    pre, post = _pre_post(event_type)
    return _clamp_window(event_time - pre, event_time + post, duration=duration_seconds)


def duration_range(event_type: str | None) -> tuple[float, float]:
    """Configured (min, max) clip duration for criteria checks."""
    pre, post = _pre_post(event_type)
    target = pre + post
    lo = max(3.0, target * 0.5)
    hi = min(float(settings.highlight_max_seconds), max(target * 1.75, target + 4.0))
    return round(lo, 2), round(hi, 2)
