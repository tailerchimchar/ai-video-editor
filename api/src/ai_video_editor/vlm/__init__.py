"""VLM taste-layer module.

The VLM is a validation gate on top of the candidate-first pipeline.
Finders + ranker do their job; the VLM watches sampled frames of each
resulting clip and either passes it, requests a window fix, or flags
it as a false positive. A second whole-compilation pass reviews
pacing / cohesion / variety.

Loop mechanics + verdict schema + backend are game-agnostic —
per-game specifics live in `game_hints/<game>.md` (adding a new game
is a file, not code). The backend is Ollama-only today (free); the
`VLMBackend` protocol makes adding a hosted backend a one-file PR.

Import surface stays thin — callers use `validator.validate_clip` /
`validator.validate_compilation`, or the loop wrappers in `loops.py`.
Everything else is implementation detail.
"""
