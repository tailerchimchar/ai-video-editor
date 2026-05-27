"""LoL-specific candidate sources.

Sources here follow the same `detect_*() -> list[dict]` shape as the
generic candidate sources (`audio_peak`, `transcript_keyword`, ...);
they emit identical row shapes. The LoL orchestrator (`league.orchestrator`)
is responsible for running them and resolving LoL-specific concerns
(e.g. per-recording Riot offset calibration).
"""
