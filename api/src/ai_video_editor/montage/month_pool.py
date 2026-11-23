"""Month pool (R7): eligible plays cut with padding into a pinned folder the
cleanup guard never touches, so a month of fights survives Outplayed purges."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import date
from pathlib import Path

from ..config import settings
from ..editing import trim_clip
from ..league.play_models import Play

_log = logging.getLogger(__name__)

_ELIGIBLE_TYPES = frozenset({"teamfight", "objective_fight", "closeout"})


def pool_dir(month: str | None = None) -> Path:
    return settings.workspace_dir / "month_pool" / (month or date.today().strftime("%Y-%m"))


def is_eligible(play: Play) -> bool:
    """Owner-involved plays with trusted kill times (D16) of a pool type."""
    if not play.owner.involved or "offset_uncertain" in play.window.flags:
        return False
    if play.type in _ELIGIBLE_TYPES:
        return True
    return play.type == "skirmish" and play.result.enemy_deaths >= 3


def pool_play(asset_id: str, play: Play, source: str, *, pad_s: float = 10.0) -> Path | None:
    """Cut the padded play window into the pool. None when ineligible or failed."""
    if not is_eligible(play):
        return None
    folder = pool_dir()
    out = folder / f"{asset_id}_{play.id}.mp4"
    if out.exists():
        return out
    w, rt = play.window, play.recording_time
    start = max(0.0, (w.approach_s if w.approach_s is not None else rt.first_kill_s - 8.0) - pad_s)
    end = (w.outcome_s if w.outcome_s is not None else rt.last_kill_s + 3.0) + pad_s
    folder.mkdir(parents=True, exist_ok=True)
    ok, err = trim_clip(source, str(out), start, end)
    if not ok:
        _log.warning("month pool cut failed for %s: %s", play.id, err)
        return None
    index_path = folder / "index.json"
    entries: list[dict] = []
    if index_path.is_file():
        try:
            entries = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entries = []
    entries.append(
        {
            "asset_id": asset_id,
            "play_id": play.id,
            "type": play.type,
            "rank": play.impressive.rank,
            "start_s": start,
            "end_s": end,
            "file": out.name,
        }
    )
    index_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return out


def clear_pool(month: str | None = None) -> None:
    shutil.rmtree(pool_dir(month), ignore_errors=True)
