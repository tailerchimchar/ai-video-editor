"""Music module — beat detection, sync, download, and beat-tied effects
for the shorts pipeline. Open/closed by design: every "hook" is a
registry (`_DOWNLOAD_STRATEGIES`, `_BEAT_DETECTORS`, `_SYNC_STRATEGIES`,
`_EFFECT_BUILDERS`) that new implementations register into without
touching callers.

Public surface (what `shorts.py` imports):
    - download_music(url, name=None) -> Path
    - detect_beats(mp3_path, detector="librosa") -> BeatTrack
    - snap_clips_to_beats(clips, beats, strategy="kill_on_beat") -> SnappedClips
    - build_beat_effect_filter(effect, beats, ...) -> ffmpeg filter fragment
"""

from __future__ import annotations

from .beats import BeatTrack, detect_beats
from .download import download_music
from .effects import build_beat_effect_filter
from .sync import SnappedClips, snap_clips_to_beats

__all__ = [
    "BeatTrack",
    "SnappedClips",
    "build_beat_effect_filter",
    "detect_beats",
    "download_music",
    "snap_clips_to_beats",
]
