"""Narration: the owner's voice (and optional camera) recorded over a finished
video, then mixed into an export. Game-agnostic; see docs/specs/spec-narration.md.

Extension points (OCP): a new narratable video kind is a `NarrationTarget`
registered in `targets.py`; a new game's camera placement is a `[camera]` block
in `profiles/<game>.toml`; a new storage backend is a `NarrationStore`.
"""
