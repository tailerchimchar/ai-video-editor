"""Champion identification via Data Dragon template match.

Stage 1: cache the full set of LoL champion portraits from Data Dragon
as small grayscale numpy arrays under
`WORKSPACE/_cache/datadragon/<version>/champions/<name>.npy` (one-time
download per Data Dragon version).

Stage 2: extract one frame from a recording at mid-game, crop the
profile's `champion_portrait` region, normalize to the same shape, and
score every cached template via normalized cross-correlation. Return
the best match and its NCC score.

Image work goes through ffmpeg (resize + colorspace + raw pixel
output) so we never have to add a heavyweight image dep just for PNG
decoding — same I/O pattern `candidates/audio.py` already uses.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import httpx
import numpy as np

from ...config import settings
from ...profiles import region_box

_log = logging.getLogger(__name__)

_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
_CHAMPION_LIST_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
_PORTRAIT_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{name}.png"

# Templates AND the recording crop are normalized to this. Bigger =
# slower comparison but more discriminative; 64 is plenty for portraits.
_TEMPLATE_SIZE = 64

# NCC ranges in [-1, 1]; ~0.5+ is a confident match against the right
# portrait (the portraits are visually distinct).
DEFAULT_MIN_CONFIDENCE = 0.45

_HTTP_TIMEOUT = 15.0


def _cache_root() -> Path:
    return settings.workspace_dir / "_cache" / "datadragon"


def _png_to_array(png_bytes: bytes, size: int) -> np.ndarray | None:
    """Decode a PNG via ffmpeg → grayscale (size, size) float32 array."""
    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-i",
        "pipe:0",
        "-vf",
        f"scale={size}:{size},format=gray",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    try:
        result = subprocess.run(cmd, input=png_bytes, capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        _log.warning("PNG decode failed: %s", e)
        return None
    arr = np.frombuffer(result.stdout, dtype=np.uint8)
    if arr.size != size * size:
        return None
    return arr.reshape(size, size).astype(np.float32)


def _crop_from_recording(
    video_path: str,
    at_seconds: float,
    box: dict,
    size: int,
) -> np.ndarray | None:
    """Pull a single frame, crop `box` (fractions of the source frame),
    normalize to size x size grayscale, return as float32 numpy."""
    x, y, w, h = box["x"], box["y"], box["w"], box["h"]
    vf = f"crop=in_w*{w}:in_h*{h}:in_w*{x}:in_h*{y},scale={size}:{size},format=gray"
    cmd = [
        settings.ffmpeg_path,
        "-y",
        # Tolerate the OBS-source B-frame quirks the compile pipeline also handles.
        "-fflags",
        "+discardcorrupt",
        "-err_detect",
        "ignore_err",
        "-ss",
        str(at_seconds),
        "-i",
        video_path,
        "-vf",
        vf,
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        _log.warning("Frame extract failed: %s", e)
        return None
    arr = np.frombuffer(result.stdout, dtype=np.uint8)
    if arr.size != size * size:
        return None
    return arr.reshape(size, size).astype(np.float32)


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    """Normalized cross-correlation in [-1, 1]. 1.0 = identical pattern,
    robust to brightness/contrast shifts that the in-game HUD applies."""
    a = a - a.mean()
    b = b - b.mean()
    denom = float(a.std()) * float(b.std()) * a.size
    if denom == 0.0:
        return 0.0
    return float((a * b).sum() / denom)


def latest_version(client: httpx.Client | None = None) -> str | None:
    """Newest Data Dragon version string (semver-like, e.g. '14.10.1')."""
    own = client is None
    c = client or httpx.Client()
    try:
        versions = c.get(_VERSIONS_URL, timeout=_HTTP_TIMEOUT).json()
        return versions[0] if versions else None
    except Exception as e:
        _log.warning("Data Dragon versions fetch failed: %s", e)
        return None
    finally:
        if own:
            c.close()


def ensure_templates(version: str, client: httpx.Client | None = None) -> list[tuple[str, Path]]:
    """Download every champion portrait for `version` if not already
    cached, returning (name, path) pairs ready for comparison."""
    own = client is None
    c = client or httpx.Client()
    cache_dir = _cache_root() / version / "champions"
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        data = c.get(_CHAMPION_LIST_URL.format(version=version), timeout=_HTTP_TIMEOUT).json()
        names = list((data or {}).get("data", {}).keys())
    except Exception as e:
        _log.warning("Champion list fetch failed: %s", e)
        return []

    pairs: list[tuple[str, Path]] = []
    for name in names:
        np_path = cache_dir / f"{name}.npy"
        if not np_path.exists():
            try:
                png_bytes = c.get(
                    _PORTRAIT_URL.format(version=version, name=name),
                    timeout=_HTTP_TIMEOUT,
                ).content
            except Exception as e:
                _log.warning("Portrait fetch failed for %s: %s", name, e)
                continue
            arr = _png_to_array(png_bytes, _TEMPLATE_SIZE)
            if arr is None:
                continue
            np.save(np_path, arr)
        pairs.append((name, np_path))

    if own:
        c.close()
    return pairs


def detect_champion(
    asset: dict,
    duration: float,
    *,
    at_seconds: float | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict | None:
    """Identify the played champion via CV template match.

    Pure flow: read profile region → extract+normalize one frame → fetch
    template cache → NCC against every template → return best match.

    `at_seconds` defaults to roughly mid-game (clamped to [60, dur-60])
    so the HUD is up and the picked champion isn't covered by the
    death-cam UI. Returns None when the profile lacks the region, the
    frame can't be read, templates aren't available, or the best NCC
    falls below `min_confidence`.
    """
    box = region_box(asset.get("game"), "champion_portrait")
    if box is None:
        _log.info("No champion_portrait region for game=%r", asset.get("game"))
        return None

    if at_seconds is None:
        at_seconds = max(60.0, min(duration - 60.0, duration / 2.0))

    crop = _crop_from_recording(asset["path"], at_seconds, box.model_dump(), _TEMPLATE_SIZE)
    if crop is None:
        return None

    with httpx.Client() as client:
        version = latest_version(client)
        if version is None:
            return None
        templates = ensure_templates(version, client)
    if not templates:
        return None

    best_name: str | None = None
    best_score = -1.0
    for name, path in templates:
        try:
            tmpl = np.load(path)
        except Exception:
            continue
        score = _ncc(crop, tmpl)
        if score > best_score:
            best_score = score
            best_name = name

    if best_name is None or best_score < min_confidence:
        return None

    return {
        "name": best_name,
        "confidence": round(best_score, 3),
        "source": "cv",
        "datadragon_version": version,
        "sample_seconds": round(at_seconds, 2),
    }
