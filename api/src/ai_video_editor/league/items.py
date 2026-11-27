"""League item catalogue (Data Dragon), slang matching, owner purchase list.

`load_items` caches `item.json` plus every purchasable item's icon under
`WORKSPACE/ddragon/<version>/`; the network is only touched on a cache
miss. `match_item_mentions` finds item names (and slang from
`ITEM_ALIASES`) in a mic line; transcript text is never logged.
`owner_purchases` reads ITEM_PURCHASED / ITEM_UNDO events from a raw
MATCH-V5 timeline and returns them in recording seconds.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import httpx
from pydantic import BaseModel

from ..config import settings
from .candidates.champion import latest_version

_log = logging.getLogger(__name__)

_ITEM_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/item.json"
_ICON_URL = "https://ddragon.leagueoflegends.com/cdn/{version}/img/item/{id}.png"
_HTTP_TIMEOUT = 30.0


class ItemInfo(BaseModel):
    id: int
    name: str
    icon_path: Path  # WORKSPACE/ddragon/<version>/item/<id>.png
    from_ids: list[int] = []  # build path components (Data Dragon "from")
    gold_total: int = 0
    consumable: bool = False


class Purchase(BaseModel):
    item_id: int
    t_rec_s: float  # recording seconds


# slang (lowercase) -> canonical item name
ITEM_ALIASES: dict[str, str] = {
    "yun tal": "Yun Tal Wildarrows",
    "yuntal": "Yun Tal Wildarrows",
    "kraken": "Kraken Slayer",
    "bork": "Blade of The Ruined King",
    "botrk": "Blade of The Ruined King",
    "ie": "Infinity Edge",
    "pd": "Phantom Dancer",
    "rfc": "Rapid Firecannon",
    "ldr": "Lord Dominik's Regards",
    "cull": "Cull",
    "doran's": "Doran's Blade",
    "dorans": "Doran's Blade",
}


# --- catalogue ---


def _cache_dir(version: str) -> Path:
    return Path(settings.workspace_dir) / "ddragon" / version


def _parse_catalogue(raw: dict, version: str) -> dict[int, ItemInfo]:
    out: dict[int, ItemInfo] = {}
    icon_dir = _cache_dir(version) / "item"
    for key, it in (raw.get("data") or {}).items():
        gold = it.get("gold") or {}
        if gold.get("purchasable") is False or not it.get("name"):
            continue
        iid = int(key)
        out[iid] = ItemInfo(
            id=iid,
            name=it["name"],
            icon_path=icon_dir / f"{iid}.png",
            from_ids=[int(x) for x in it.get("from") or []],
            gold_total=int(gold.get("total") or 0),
            consumable=bool(it.get("consumed") or it.get("consumable")),
        )
    return out


def _cached_versions() -> list[str]:
    root = Path(settings.workspace_dir) / "ddragon"
    if not root.is_dir():
        return []
    return sorted(
        (d.name for d in root.iterdir() if (d / "item.json").exists()),
        key=lambda v: [int(p) if p.isdigit() else 0 for p in v.split(".")],
        reverse=True,
    )


def load_items(
    version: str | None = None, client: httpx.Client | None = None
) -> dict[int, ItemInfo]:
    """Purchasable items by id. Cached; network only on a cache miss.

    With no `version` the newest cached version is used offline; if
    nothing is cached the latest Data Dragon version is fetched.
    Returns {} when offline with an empty cache."""
    own = client is None
    c = client or httpx.Client()
    try:
        if version is None:
            cached = _cached_versions()
            version = cached[0] if cached else latest_version(c)
            if version is None:
                return {}
        d = _cache_dir(version)
        item_json = d / "item.json"
        if item_json.exists():
            try:
                return _parse_catalogue(json.loads(item_json.read_text("utf-8")), version)
            except (OSError, ValueError):
                _log.warning("item cache unreadable for %s; refetching", version)
        try:
            raw = c.get(_ITEM_URL.format(version=version), timeout=_HTTP_TIMEOUT).json()
        except Exception as e:
            _log.warning("Data Dragon item fetch failed: %s", e)
            return {}
        items = _parse_catalogue(raw, version)
        (d / "item").mkdir(parents=True, exist_ok=True)
        for it in items.values():
            if it.icon_path.exists():
                continue
            try:
                r = c.get(_ICON_URL.format(version=version, id=it.id), timeout=_HTTP_TIMEOUT)
                if r.status_code == 200:
                    it.icon_path.write_bytes(r.content)
            except Exception as e:
                _log.warning("item icon %s fetch failed: %s", it.id, e)
        # written last: its presence marks a complete cache
        item_json.write_text(json.dumps(raw), encoding="utf-8")
        return items
    finally:
        if own:
            c.close()


# --- mention matching ---


def _norm(s: str) -> str:
    return s.lower().replace(chr(0x2019), "'")


def match_item_mentions(text: str, items: dict[int, ItemInfo]) -> list[int]:
    """Item ids named in `text` (names + slang), first-mention order, deduped.

    Case-insensitive, word-boundary, longest phrase first (so "kraken
    slayer" is one hit, not two). Consumables only match by exact name."""
    by_name: dict[str, int] = {}
    for it in items.values():
        by_name.setdefault(_norm(it.name), it.id)
    phrases: dict[str, int] = dict(by_name)
    for alias, canon in ITEM_ALIASES.items():
        iid = by_name.get(_norm(canon))
        if iid is not None and not items[iid].consumable:
            phrases.setdefault(_norm(alias), iid)
    if not phrases:
        return []
    ordered = sorted(phrases, key=len, reverse=True)
    pattern = re.compile(r"(?<![\w'])(" + "|".join(re.escape(p) for p in ordered) + r")(?![\w])")
    found: list[int] = []
    for m in pattern.finditer(_norm(text)):
        iid = phrases[m.group(1)]
        if iid not in found:
            found.append(iid)
    return found


# --- owner purchases ---


def owner_purchases(raw_timeline: dict, owner_pid: int, offset_s: float) -> list[Purchase]:
    """The owner's ITEM_PURCHASED events in recording seconds
    (`t_game + offset_s`), minus purchases reverted by ITEM_UNDO (the undo
    cancels the latest not-yet-undone purchase of its `beforeId`)."""
    events: list[dict] = []
    for frame in (raw_timeline.get("info") or {}).get("frames", []):
        for ev in frame.get("events", []):
            if ev.get("participantId") == owner_pid and ev.get("type") in (
                "ITEM_PURCHASED",
                "ITEM_UNDO",
            ):
                events.append(ev)
    events.sort(key=lambda e: e.get("timestamp", 0))
    kept: list[dict] = []
    for ev in events:
        if ev["type"] == "ITEM_PURCHASED":
            kept.append(ev)
            continue
        before = ev.get("beforeId")
        for i in range(len(kept) - 1, -1, -1):
            if kept[i].get("itemId") == before:
                del kept[i]
                break
    return [
        Purchase(item_id=int(e["itemId"]), t_rec_s=round(e["timestamp"] / 1000.0 + offset_s, 3))
        for e in kept
    ]


# --- first back ---

SHOP_VISIT_S = 10.0  # purchases this soon after the first one are the same visit


def first_back_main_item(
    purchases: list[Purchase], items: dict[int, ItemInfo], *, first_back_rec_s: float
) -> Purchase | None:
    """The main item of the first back: the most expensive non-consumable
    catalogue item (by Data Dragon gold total) bought within SHOP_VISIT_S of
    the first back's first purchase (`first_back_rec_s`, recording seconds).
    The earliest wins a tie. None when the visit bought nothing that counts."""
    visit = [
        p
        for p in purchases
        if first_back_rec_s - 1e-6 <= p.t_rec_s <= first_back_rec_s + SHOP_VISIT_S
        and p.item_id in items
        and not items[p.item_id].consumable
    ]
    if not visit:
        return None
    return max(visit, key=lambda p: (items[p.item_id].gold_total, -p.t_rec_s))
