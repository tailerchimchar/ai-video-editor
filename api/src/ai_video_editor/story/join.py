"""story-join: one video from several games' story.mp4, a card per game."""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

from ..config import settings
from .render import StoryRenderError, _concat, _render_card

_ROUND_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_\d{4}_([^_]+)_(\d+)-(\d+)-(\d+)$")


def round_label(round_name: str) -> str:
    """`2026-11-23_2056_zeri_8-7-12` -> `Zeri 8/7/12`; other shapes unchanged."""
    m = _ROUND_RE.match(round_name)
    if not m:
        return round_name
    _, champ, k, d, a = m.groups()
    return f"{champ.capitalize()} {k}/{d}/{a}"


def story_path(round_name: str) -> Path:
    return Path(settings.workspace_dir) / "story" / round_name / "story.mp4"


def default_out(rounds: list[str]) -> Path:
    m = _ROUND_RE.match(rounds[0])
    date = m.group(1) if m else rounds[0]
    champs = []
    for r in rounds:
        mm = _ROUND_RE.match(r)
        champs.append(mm.group(2) if mm else r)
    name = f"joined_{date}_{'-'.join(champs)}.mp4"
    return Path(settings.workspace_dir) / "story" / name


def join_stories(rounds: list[str], out: Path, *, cards: bool = True) -> Path:
    """Concatenate each round's story.mp4, with a `Game n · label` card before each."""
    out = Path(out)
    stories = [story_path(r) for r in rounds]
    for r, s in zip(rounds, stories, strict=True):
        if not s.is_file():
            raise StoryRenderError(f"story video missing for round {r}: {s}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="story_join_") as tmp:
        parts: list[str] = []
        for n, (r, s) in enumerate(zip(rounds, stories, strict=True), start=1):
            if cards:
                card = Path(tmp) / f"card_{n}.mp4"
                _render_card(f"Game {n} · {round_label(r)}", card)
                parts.append(card.as_posix())
            parts.append(s.as_posix())
        ok, err = _concat(parts, out.as_posix())
    if not ok:
        raise StoryRenderError(f"join concat failed: {err}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="story-join", description=__doc__.splitlines()[0])
    ap.add_argument("rounds", nargs="+", help="round names, in game order (at least 2)")
    ap.add_argument("--out", help="output mp4 (default WORKSPACE/story/joined_...)")
    ap.add_argument("--no-cards", action="store_true", help="skip the game cards")
    args = ap.parse_args(argv)
    if len(args.rounds) < 2:
        ap.error("need at least 2 rounds")
    out = Path(args.out) if args.out else default_out(args.rounds)
    try:
        res = join_stories(args.rounds, out, cards=not args.no_cards)
    except StoryRenderError as e:
        print(f"story-join: {e}", file=sys.stderr)
        return 1
    print(res)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
