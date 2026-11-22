"""Jev scorecard: compare logged Jev answers with the owner's corrections.

`score` is pure; `main` reads the audit logs and corrections files and
prints a short table (`jev-scorecard`).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import settings
from .audit import AnswerRecord

_FORMATS = ("hype", "story", "voice_over", "funny", "arc_montage")
_FORMAT_WORDS = ("format", "montage", "short", "story", "hype", "funny", "voice")


@dataclass
class Scorecard:
    keep_precision: float | None
    format_agreement: float | None
    disagreements: list[dict] = field(default_factory=list)
    n_records: int = 0
    n_corrections: int = 0


def _not_a_keeper(correction: dict) -> bool:
    return "no-event" in (correction.get("tags") or []) or "not a highlight" in str(
        correction.get("rule") or ""
    )


def _confirmed_format(rule: str) -> str | None:
    """The format a correction's rule names, if it also names a format word."""
    lowered = rule.lower()
    if not any(word in lowered for word in _FORMAT_WORDS):
        return None
    for name in _FORMATS:
        if name in lowered or name.replace("_", " ") in lowered:
            return name
    return None


def score(records: list[AnswerRecord], corrections: list[dict]) -> Scorecard:
    bad_refs = {c.get("clip_ref") for c in corrections if _not_a_keeper(c) and c.get("clip_ref")}
    disagreements: list[dict] = []

    kept = [r for r in records if r.value is True and r.clip_ref]
    wrong = [r for r in kept if r.clip_ref in bad_refs]
    for r in wrong:
        disagreements.append(
            {"kind": "keep", "clip_ref": r.clip_ref, "jev": True, "owner": "not a keeper"}
        )
    keep_precision = (len(kept) - len(wrong)) / len(kept) if kept else None

    compared = agreed = 0
    for r in (r for r in records if isinstance(r.value, str) and r.clip_ref):
        for c in corrections:
            rule = str(c.get("rule") or "")
            if r.clip_ref not in rule:
                continue
            confirmed = _confirmed_format(rule.replace(r.clip_ref, ""))
            if confirmed is None:
                continue
            compared += 1
            if confirmed == r.value:
                agreed += 1
            else:
                disagreements.append(
                    {"kind": "format", "clip_ref": r.clip_ref, "jev": r.value, "owner": confirmed}
                )
            break
    format_agreement = agreed / compared if compared else None

    return Scorecard(
        keep_precision=keep_precision,
        format_agreement=format_agreement,
        disagreements=disagreements,
        n_records=len(records),
        n_corrections=len(corrections),
    )


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-scorecard", description=__doc__)
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--hints-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    log_dir = args.log_dir or settings.workspace_dir / "decisions"
    hints_dir = args.hints_dir or settings.game_hints_dir or settings.workspace_dir / "game_hints"
    records = []
    for path in sorted(log_dir.glob("jev-*.jsonl")) if log_dir.is_dir() else []:
        for row in _read_jsonl(path):
            try:
                if row.get("backend") == "jev":
                    records.append(AnswerRecord(**row))
            except TypeError:
                continue
    corrections = []
    for path in sorted(hints_dir.glob("*_examples.jsonl")) if hints_dir.is_dir() else []:
        corrections.extend(_read_jsonl(path))

    card = score(records, corrections)
    print(f"Jev answers scored:   {card.n_records}")
    print(f"Owner corrections:    {card.n_corrections}")
    print(f"Keep precision:       {_fmt(card.keep_precision)}")
    print(f"Format agreement:     {_fmt(card.format_agreement)}")
    for d in card.disagreements:
        print(f"  disagree [{d['kind']}] {d['clip_ref']}: jev={d['jev']} owner={d['owner']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
