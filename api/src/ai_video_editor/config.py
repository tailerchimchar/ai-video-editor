from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    outplayed_media_dir: Path
    workspace_dir: Path
    ffmpeg_path: str = "ffmpeg"
    database_path: Path = Path("ai_video_editor.db")

    # ffmpeg encoder used for compile/per-clip re-encodes. libx264 is the
    # portable CPU default (works everywhere). For NVIDIA GPUs with
    # driver >= 570, set FFMPEG_VIDEO_CODEC=h264_nvenc plus
    # FFMPEG_VIDEO_CODEC_OPTS="-preset p4 -cq 23 -rc vbr" for a ~2-3x
    # encode speedup with no quality loss.
    ffmpeg_video_codec: str = "libx264"
    ffmpeg_video_codec_opts: str = "-preset veryfast -crf 20"

    # ffmpeg's drawtext filter needs an explicit font file on Windows
    # (no fontconfig there). Arial is shipped with every Windows install
    # so it's a safe default; override if you want a different look.
    caption_font_path: str = "C:/Windows/Fonts/arial.ttf"

    # --- Phase 2: AI analyzer ---
    anthropic_api_key: str = ""
    # One structured call ranks the whole candidate batch. Haiku is plenty
    # for a scoring/extraction task and ~10x cheaper than Opus — chosen as
    # the default to conserve credits (override via RANKER_MODEL).
    ranker_model: str = "claude-haiku-4-5"
    # Hard ceiling on rank() API calls per server process — refuses the
    # call past this cap so nothing can drain the Anthropic balance.
    anthropic_max_rank_calls: int = 25

    # Riot API candidate source (League). Free dev key from
    # developer.riotgames.com. Source stays inert until all three are set.
    riot_api_key: str = ""
    riot_id: str = ""  # "gameName#tagLine"
    riot_region: str = "americas"  # regional routing: americas|asia|europe|sea
    riot_match_lookback: int = 20  # recent matches to consider for correlation
    # Constant record-start lead/lag (s). Kills map to game-clock time;
    # nudge this if clips land consistently early/late.
    riot_sync_offset_seconds: float = 0.0
    # IANA tz of the recording filenames' local timestamps (DST-aware).
    # Used to convert the filename's record-start time to UTC for
    # match correlation. e.g. America/Chicago, America/New_York.
    recording_timezone: str = "America/Chicago"
    # Exact per-recording offset (game-clock → VOD seconds), from one
    # human ground-truth point. When set, it is authoritative and the
    # unreliable audio/OCR auto-detection is bypassed entirely.
    riot_offset_override_seconds: float | None = None
    # Below this audio↔kill lock z-score, fall back to an OCR clock read.
    riot_offset_min_quality: float = 2.0
    # tesseract binary for the OCR cross-check (full path if not on PATH).
    tesseract_path: str = "tesseract"

    # Transcription (local faster-whisper — private, $0, no upload).
    # base = fast/decent; small/medium = better but slower. CPU is slow
    # on long VODs; transcribe_max_seconds guards pathological inputs.
    whisper_model: str = "base"
    # cpu is the reliable default (no CUDA libs needed). Set to "cuda"
    # only if the CUDA runtime (cublas/cudnn) is installed — "auto" will
    # try CUDA and hard-fail if those DLLs are missing.
    whisper_device: str = "cpu"  # cpu|cuda
    whisper_compute_type: str = "int8"  # int8 = CPU-friendly
    transcribe_max_seconds: float = 5400.0  # 90 min hard cap

    # RAG semantic search. Local ONNX embeddings (no upload). bge-small
    # is 384-dim, ~90MB, fast on CPU. Chunk transcripts in rolling
    # windows — Whisper segments alone are too short to embed well.
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384
    chunk_window_seconds: float = 25.0
    chunk_stride_seconds: float = 25.0  # set < window for overlap

    # Langfuse tracing
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"

    # Candidate generation tuning knobs
    # Files at/under this duration are treated as Outplayed event clips;
    # longer files are full session recordings (probe showed a clean
    # 39s -> 932s gap, so 120s is a safe default).
    outplayed_clip_max_seconds: float = 120.0
    analyze_max_candidates: int = 15
    analyze_peak_threshold: float = 0.6
    analyze_window_padding: float = 4.0
    analyze_score_threshold: float = 0.5

    # Highlight clip windowing. For sources with an exact moment (Riot
    # kill), the cut is anchor - pre .. anchor + post. Wider = more
    # context/lead-up; tune to taste.
    highlight_pre_seconds: float = 7.5
    highlight_post_seconds: float = 7.5

    # Per-event-type window overrides. Different events deserve different
    # pacing: a kill needs short pre + long post (milk the celebration),
    # a teamfight needs long pre + medium post (the buildup matters), a
    # baron needs medium pre + long post (objective + flex).
    #
    # Semantics by source:
    # - riot_api / anchor sources: anchor - pre .. anchor + post
    # - audio_peak / transcript / fallback sources: the LLM ranker's
    #   suggested window is EXTENDED by (pre, post) on each side. The
    #   ranker can still tighten; this restores breathing room.
    #
    # Falls back to (highlight_pre_seconds, highlight_post_seconds) for
    # event types not listed AND for anchor sources without event_type.
    # For fallback sources without event_type, no padding is applied
    # (preserves prior behavior).
    event_window_overrides: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: {
            # Riot kill anchors — short pre, long post (milk celebration)
            "kill": (3.0, 8.0),
            "ace": (5.0, 12.0),
            "pentakill": (5.0, 12.0),
            "quadrakill": (4.0, 10.0),
            "teamfight": (8.0, 6.0),
            "baron": (4.0, 10.0),
            "dragon": (3.0, 7.0),
            # CV/KDA — emitted by candidates/cv_kda.py
            "death": (4.0, 4.0),  # short context both sides, less milking
            "assist": (3.0, 6.0),  # like a kill but lighter
            # audio_peak source (loud regions)
            "funny_audio": (3.0, 6.0),
            # transcript_keyword source — emits these two categories
            "hype_callout": (3.0, 6.0),
            "funny_callout": (3.0, 6.0),
            # Generic fallback (rarely used directly — most candidates carry
            # a specific event_type from their source)
            "clip": (5.0, 8.0),
        }
    )

    # Post-rank clustering: kept rankings whose windows overlap or sit
    # within this many seconds of each other are merged into one fused
    # clip before compile. Kills the "10 clips of the same teamfight"
    # problem. Set to 0 to disable.
    cluster_gap_seconds: float = 30.0

    # Narrative compile mode — splits the reel into three sections in
    # recording order: intro (warmup/greeting) → main (best plays) →
    # outro (post-game commentary/reflection). Each section pulls from
    # clips whose suggested_start falls inside its time window; within
    # the section, the top by hype_score are taken, then sorted by time.
    #
    # Recording-end is derived from the rankings' max suggested_end_seconds
    # (proxy for total duration — works because we only need to know
    # which clips are "near the end"). Intro is anchored to t=0.
    narrative_intro_seconds: float = 600.0  # first 10 min of recording
    narrative_outro_seconds: float = 600.0  # last 10 min of recording
    narrative_intro_max_clips: int = 2
    narrative_outro_max_clips: int = 1

    # Safety guards for the audio-peak path (long recordings).
    # Skip audio analysis on recordings longer than this (seconds).
    analyze_audio_max_seconds: float = 3600.0
    # Refuse to extract a WAV unless this much free disk remains (MB),
    # on top of the estimated WAV size — prevents filling the drive.
    min_free_disk_mb: int = 1024

    # LLM ranker call bounds.
    ranker_max_retries: int = 2
    ranker_timeout_seconds: float = 120.0

    # --- VLM (vision language model) taste layer -------------------------
    # A local VLM validates each cut and the whole compilation. Loop
    # mechanics + verdict schema are game-agnostic; per-game specifics
    # live in vlm/game_hints/<game>.md. Ships Ollama-only ($0); a paid
    # backend can be added via the same VLMBackend protocol later.
    vlm_enabled: bool = True
    vlm_backend: str = "ollama"  # only "ollama" today; hosted later
    vlm_max_clip_iter: int = 5
    vlm_max_comp_iter: int = 3
    vlm_frame_samples_clip: int = 8
    vlm_frame_samples_comp: int = 40
    # Ollama backend
    vlm_ollama_url: str = "http://localhost:11434"
    # Model ladder — tried in order until one is reachable + pulled.
    # Defaults tuned for 6 GB VRAM boxes (GTX 1660 class); bump on RTX
    # cards. Setting either to empty string disables that tier.
    vlm_model_primary: str = "qwen3-vl:4b"
    vlm_model_fallback: str = "qwen3-vl:2b"
    # Bounded per-call HTTP timeout for the VLM. On slower GPUs a call
    # may take 20-30s; this cap prevents a wedged Ollama from stalling
    # a whole compile forever.
    vlm_call_timeout_seconds: float = 120.0

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()  # type: ignore[call-arg]
