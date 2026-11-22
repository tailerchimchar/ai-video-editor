"""Clip-ref normalisation: one canonical `NmNNs` form for every spelling of a clip's time.

Experts, audit logs, corrections and file names name the same clip as
`06_kill_23m05s.mp4`, `23m05s`, `23:05` or `1385.0`; comparing them needs
one form. Refs that carry no time (opaque ids) are returned unchanged.
"""

from __future__ import annotations

import re

_NM_NS = re.compile(r"(\d+)m(\d+)s")
_MM_SS = re.compile(r"(\d+):(\d{2})")
_SECONDS = re.compile(r"\d+(?:\.\d+)?")


def _fmt(total_s: int) -> str:
    return f"{total_s // 60}m{total_s % 60:02d}s"


def normalize_clip_ref(ref: str) -> str:
    text = ref.strip()
    if m := _NM_NS.search(text):
        return _fmt(int(m.group(1)) * 60 + int(m.group(2)))
    if m := _MM_SS.fullmatch(text):
        return _fmt(int(m.group(1)) * 60 + int(m.group(2)))
    if _SECONDS.fullmatch(text):
        return _fmt(int(float(text)))
    return ref
