"""Owner HP-bar reader for League 1280x720 recordings (numpy only).

SHADOW MODE: `scan_hp_shadow` logs trade/escape events to
`<workspace_dir>/hp_shadow/<asset_id>.json`. Nothing becomes a candidate
unless `settings.hp_reader_enabled` is True (see candidates/service.py).
Spec: docs/specs/spec-content-direction.md 4.4 / R4.
"""

from __future__ import annotations

import json
import logging
import subprocess

import numpy as np

from ..config import settings

_log = logging.getLogger(__name__)

# (x, y, w, h) in a 1280x720 frame. ESTIMATE from LoL HUD proportions (green
# bar directly under the ability icons, bottom centre); not yet calibrated on
# a raw 720p frame. Adjust here only.
HP_BAR_ROI_720P: dict[str, tuple[int, int, int, int]] = {"owner": (465, 690, 350, 10)}

_FRAME_W, _FRAME_H = 1280, 720

# Event thresholds (spec 4.4).
_TRADE_DROP, _TRADE_DROP_WINDOW_S = 0.25, 6.0
_TRADE_RECOVER, _TRADE_RECOVER_WINDOW_S = 0.15, 20.0
_ESCAPE_HP, _ESCAPE_NO_DEATH_S = 0.15, 10.0


def read_owner_hp(frame: np.ndarray) -> float | None:
    """Fill fraction (0-1) of the owner's HP bar, or None if grey/absent.

    Green (healthy) or red (low) columns count as filled; white (shield) and
    dark pixels do not. Fill = leading filled run from the left / bar width.
    """
    x, y, w, h = HP_BAR_ROI_720P["owner"]
    if frame.ndim != 3 or frame.shape[0] < y + h or frame.shape[1] < x + w:
        return None
    roi = frame[y : y + h, x : x + w].astype(np.int16)
    r, g, b = roi[..., 0], roi[..., 1], roi[..., 2]
    green = (g > r + 40) & (g > b + 40)
    red = (r > g + 80) & (r > b + 80)
    filled_cols = (green | red).mean(axis=0) >= 0.5
    if not filled_cols.any():
        return None
    # Contiguous run from the left edge; stray filled columns to the right
    # (HUD noise, shield edge) must not inflate the reading.
    run = w if filled_cols.all() else int(np.argmin(filled_cols))
    return run / w if run else None


def scan_hp(video_path: str, *, step_s: float) -> list[tuple[float, float | None]]:
    """Sample the HP bar every `step_s` seconds via one ffmpeg raw-pipe pass."""
    x, y, w, h = HP_BAR_ROI_720P["owner"]
    vf = f"fps=1/{step_s},scale={_FRAME_W}:{_FRAME_H},crop={w}:{h}:{x}:{y}"
    cmd = [settings.ffmpeg_path, "-v", "error", "-i", video_path, "-vf", vf]
    cmd += ["-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=1800)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _log.warning("hp_bar: ffmpeg unavailable or timed out")
        return []
    if proc.returncode != 0:
        _log.warning("hp_bar: ffmpeg failed (rc=%s) for %s", proc.returncode, video_path)
        return []
    raw = proc.stdout
    size = w * h * 3
    out: list[tuple[float, float | None]] = []
    for i in range(len(raw) // size):
        roi = np.frombuffer(raw, np.uint8, size, i * size).reshape(h, w, 3)
        frame = np.zeros((_FRAME_H, _FRAME_W, 3), np.uint8)
        frame[y : y + h, x : x + w] = roi
        out.append((i * step_s, read_owner_hp(frame)))
    return out


def hp_events(samples: list[tuple[float, float | None]], deaths_s: list[float]) -> list[dict]:
    """Derive `trade` and `escape` events from an HP time series."""
    pts = [(t, v) for t, v in samples if v is not None]
    events: list[dict] = []

    i = 0
    while i < len(pts):
        t0, v0 = pts[i]
        trough = next(
            (
                j
                for j in range(i + 1, len(pts))
                if pts[j][0] - t0 <= _TRADE_DROP_WINDOW_S and v0 - pts[j][1] >= _TRADE_DROP
            ),
            None,
        )
        if trough is None:
            i += 1
            continue
        # Track the running minimum (HP may keep falling after the drop is
        # first met); recovery is measured from that minimum.
        rec = None
        vmin = pts[trough][1]
        for k in range(trough + 1, len(pts)):
            if pts[k][0] - pts[trough][0] > _TRADE_RECOVER_WINDOW_S:
                break
            vmin = min(vmin, pts[k][1])
            if pts[k][1] - vmin >= _TRADE_RECOVER:
                rec = k
                break
        if rec is None:
            i += 1
            continue
        events.append({"type": "trade", "start_s": t0, "end_s": pts[rec][0], "min_hp": vmin})
        i = rec + 1

    in_low = False
    for t, v in pts:
        if v > _ESCAPE_HP:
            in_low = False
        elif not in_low:
            in_low = True
            if not any(t < d <= t + _ESCAPE_NO_DEATH_S for d in deaths_s):
                events.append({"type": "escape", "start_s": t, "end_s": t, "min_hp": v})
    return sorted(events, key=lambda e: e["start_s"])


def scan_hp_shadow(
    asset_id: str, *, video_path: str, deaths_s: list[float] | None = None
) -> list[dict]:
    """Scan a video and log events to hp_shadow/<asset_id>.json (no candidates)."""
    samples = scan_hp(video_path, step_s=settings.hp_reader_step_s)
    events = hp_events(samples, deaths_s or [])
    out_dir = settings.workspace_dir / "hp_shadow"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {"asset_id": asset_id, "step_s": settings.hp_reader_step_s, "events": events}
    if not samples:
        payload["error"] = "no samples (ffmpeg failed or video had no frames)"
    (out_dir / f"{asset_id}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return events
