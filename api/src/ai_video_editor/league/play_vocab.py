"""Play vocabulary: the YAML that defines each play type (spec 4.4).

The workspace file (`settings.game_hints_dir`, else `<workspace>/game_hints`)
wins over the packaged seed in `vlm/game_hints/`, like the examples files.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from ..config import settings
from ..vlm import hint_files


class PlayRule(BaseModel):
    type: str
    phase: list[str]
    propose: str
    start: dict[str, str]
    end: dict[str, str | float]
    vlm_confirms: list[str]
    facts_from_riot: list[str]
    examples: list[str] = Field(default_factory=list)


class PlayVocabulary(BaseModel):
    rules: dict[str, PlayRule]
    hash: str

    def rule_for(self, play_type: str) -> PlayRule:
        """The rule for `play_type`, else the `skirmish` rule. Raises
        ValueError when neither exists (a broken workspace override)."""
        rule = self.rules.get(play_type) or self.rules.get("skirmish")
        if rule is None:
            raise ValueError("vocabulary has no skirmish rule")
        return rule


def _vocab_path(game: str) -> Path:
    name = f"{game.lower().strip()}_plays.yaml"
    live = (settings.game_hints_dir or settings.workspace_dir / "game_hints") / name
    return live if live.is_file() else hint_files.HINTS_DIR / name


def load_vocab(game: str | None = "league") -> PlayVocabulary:
    raw = _vocab_path(game or "league").read_bytes()
    parsed = yaml.safe_load(raw) or {}
    rules = {t: PlayRule(type=t, **body) for t, body in parsed.items()}
    return PlayVocabulary(rules=rules, hash=hashlib.sha1(raw).hexdigest()[:12])
