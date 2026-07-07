"""Beat-tied visual effects — return ffmpeg filter fragments that
overlay pulses/flashes tied to a beat grid.

Open/closed: `_EFFECT_BUILDERS` maps an effect name to a function that
returns a filter fragment (string). Adding a new effect = new function
+ one dict entry.

Effects are designed to CHAIN — a caller can compose "flash + pulse
zoom" by taking multiple builders' outputs and stringing them together.

Note: builders return filter chains meant to be applied to a video
label the caller controls. Each builder takes (in_label, out_label,
beats, ...) and returns a filter fragment string.
"""

from __future__ import annotations

from collections.abc import Callable

# ---------------------------------------------------------------------
# Effect builders (open/closed hook — register new effects here)
# ---------------------------------------------------------------------


def _flash_expr(beats: tuple[float, ...], duration_seconds: float) -> str:
    """Build the `enable=` expression that turns the flash on for
    `duration_seconds` right at each beat time.

    ffmpeg accepts arbitrary expressions here — we OR together
    `between(t,B,B+D)` for each beat.
    """
    parts = [
        f"between(t\\,{beat:.4f}\\,{beat + duration_seconds:.4f})"
        for beat in beats
    ]
    if not parts:
        # `enable=0` — never on
        return "0"
    return "+".join(parts)


def _build_flash(
    in_label: str,
    out_label: str,
    beats: tuple[float, ...],
    *,
    intensity: float = 0.3,
    duration_seconds: float = 0.08,
) -> str:
    """Subtle white pulse on each beat via curves gamma bump.

    `intensity` = 0.0 (no flash) .. 1.0 (blown out). 0.3 is subtle.
    `duration_seconds` = how long each flash stays on (~1 frame at 30fps
    ≈ 0.033; 0.08 = 2-3 frames).
    """
    if not beats:
        return f"[{in_label}]null[{out_label}]"
    # Clamp intensity to a safe range
    intensity = max(0.0, min(1.0, intensity))
    if intensity <= 0.0:
        return f"[{in_label}]null[{out_label}]"
    enable = _flash_expr(beats, duration_seconds)
    # Split so we can overlay a whitened copy over the original when
    # `enable` is truthy. eq=brightness pushes toward white; use the
    # timeline enable to switch it on only at beat frames.
    return (
        f"[{in_label}]eq=brightness={intensity:.3f}:"
        f"enable='{enable}'[{out_label}]"
    )


def _build_pulse_zoom(
    in_label: str,
    out_label: str,
    beats: tuple[float, ...],
    *,
    scale: float = 1.05,
    duration_seconds: float = 0.15,
) -> str:
    """Placeholder: a subtle zoom-in punch on each beat. Currently
    passes through — full implementation requires per-frame
    `scale=iw*expr:ih*expr` with a time-triggered ramp, which is
    non-trivial in one filter. Queued follow-up."""
    _ = beats, scale, duration_seconds
    return f"[{in_label}]null[{out_label}]"


_EFFECT_BUILDERS: dict[str, Callable[..., str]] = {
    "flash": _build_flash,
    "pulse_zoom": _build_pulse_zoom,
}


# ---------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------


def build_beat_effect_filter(
    effect: str,
    in_label: str,
    out_label: str,
    beats: tuple[float, ...],
    **kwargs,
) -> str:
    """Return an ffmpeg filter fragment that applies `effect` at the
    given beat times. The fragment reads from `[in_label]` and writes
    to `[out_label]` — caller chains it into their `-filter_complex`.

    Extra kwargs are forwarded to the effect builder (`intensity`,
    `duration_seconds`, `scale`, etc.).

    Raises `ValueError` for unknown effect names.
    """
    if effect not in _EFFECT_BUILDERS:
        raise ValueError(
            f"unknown beat effect {effect!r}; "
            f"expected one of {sorted(_EFFECT_BUILDERS)}"
        )
    return _EFFECT_BUILDERS[effect](in_label, out_label, beats, **kwargs)
