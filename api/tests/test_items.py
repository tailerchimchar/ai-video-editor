"""League item catalogue: Data Dragon cache, slang matching, owner purchases. No network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ai_video_editor.config import settings
from ai_video_editor.league.items import (
    ITEM_ALIASES,
    ItemInfo,
    Purchase,
    load_items,
    match_item_mentions,
    owner_purchases,
)

ITEM_JSON = {
    "data": {
        "6672": {
            "name": "Kraken Slayer",
            "from": ["1038", "1037"],
            "gold": {"total": 3100, "purchasable": True},
        },
        "6693": {
            "name": "Yun Tal Wildarrows",
            "from": ["1018"],
            "gold": {"total": 3000, "purchasable": True},
        },
        "3031": {
            "name": "Infinity Edge",
            "from": ["1038"],
            "gold": {"total": 3450, "purchasable": True},
        },
        "3153": {
            "name": "Blade of The Ruined King",
            "from": [],
            "gold": {"total": 3300, "purchasable": True},
        },
        "1055": {"name": "Doran's Blade", "gold": {"total": 450, "purchasable": True}},
        "2003": {
            "name": "Health Potion",
            "consumable": True,
            "gold": {"total": 50, "purchasable": True},
        },
        "9999": {"name": "Hidden Thing", "gold": {"total": 0, "purchasable": False}},
        "6694": {"name": "Cull", "gold": {"total": 450, "purchasable": True}},
    }
}


@pytest.fixture
def ws(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path)
    return tmp_path


def _client(calls: list[str]) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        if req.url.path.endswith("versions.json"):
            return httpx.Response(200, json=["14.1.1", "13.24.1"])
        if req.url.path.endswith("item.json"):
            return httpx.Response(200, json=ITEM_JSON)
        if req.url.path.endswith(".png"):
            return httpx.Response(200, content=b"\x89PNG")
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_models():
    it = ItemInfo(id=1, name="X", icon_path=Path("a.png"))
    assert it.from_ids == [] and it.gold_total == 0
    assert Purchase(item_id=1, t_rec_s=2.0).t_rec_s == 2.0


def test_load_items_downloads_once_then_offline(ws):
    calls: list[str] = []
    got = load_items(client=_client(calls))
    assert got[6672].name == "Kraken Slayer"
    assert got[6672].from_ids == [1038, 1037]
    assert got[6672].gold_total == 3100
    assert got[6672].icon_path == ws / "ddragon" / "14.1.1" / "item" / "6672.png"
    assert got[6672].icon_path.exists()
    n = len(calls)
    assert n > 0
    again = load_items(client=_client(calls))
    assert len(calls) == n  # no network on cache hit
    assert again.keys() == got.keys()


def test_load_items_explicit_version_cached_without_versions_call(ws):
    calls: list[str] = []
    load_items("14.1.1", client=_client(calls))
    assert not any("versions.json" in c for c in calls)
    calls.clear()
    load_items("14.1.1", client=_client(calls))
    assert calls == []


def test_load_items_drops_unpurchasable_keeps_consumable(ws):
    got = load_items(client=_client([]))
    assert 9999 not in got
    assert 2003 in got  # consumables stay in the catalogue; matcher gates them


@pytest.fixture
def cat(ws):
    return load_items(client=_client([]))


def test_match_slang_in_text_order(cat):
    text = "went yun tal because it scales better than kraken slayer"
    assert match_item_mentions(text, cat) == [6693, 6672]


def test_match_aliases_case_insensitive_word_boundary(cat):
    assert match_item_mentions("BORK then IE", cat) == [3153, 3031]
    assert match_item_mentions("kraken", cat) == [6672]
    assert match_item_mentions("I wasn't pied or tied", cat) == []  # 'ie' inside words
    assert match_item_mentions("dorans into cull", cat) == [1055, 6694]


def test_match_dedup_and_longest_first(cat):
    assert match_item_mentions("kraken slayer kraken", cat) == [6672]
    assert match_item_mentions("Doran's Blade", cat) == [1055]


def test_match_consumable_only_when_exact(cat):
    assert match_item_mentions("drank a potion", cat) == []
    assert match_item_mentions("health potion", cat) == [2003]


def test_aliases_pinned():
    assert ITEM_ALIASES["yun tal"] == "Yun Tal Wildarrows"
    assert ITEM_ALIASES["ldr"] == "Lord Dominik's Regards"
    assert ITEM_ALIASES["dorans"] == "Doran's Blade" == ITEM_ALIASES["doran's"]


def _tl(events):
    return {"info": {"frames": [{"events": events}]}}


def test_owner_purchases_filters_undo_and_others():
    ev = [
        {"type": "ITEM_PURCHASED", "participantId": 1, "itemId": 1055, "timestamp": 5000},
        {"type": "ITEM_PURCHASED", "participantId": 2, "itemId": 3031, "timestamp": 6000},
        {"type": "ITEM_PURCHASED", "participantId": 1, "itemId": 3031, "timestamp": 7000},
        {
            "type": "ITEM_UNDO",
            "participantId": 1,
            "beforeId": 3031,
            "afterId": 0,
            "timestamp": 8000,
        },
        {
            "type": "ITEM_UNDO",
            "participantId": 2,
            "beforeId": 3031,
            "afterId": 0,
            "timestamp": 8500,
        },
        {"type": "ITEM_PURCHASED", "participantId": 1, "itemId": 6672, "timestamp": 9000},
        {"type": "ITEM_SOLD", "participantId": 1, "itemId": 1055, "timestamp": 9500},
    ]
    got = owner_purchases(_tl(ev), 1, 10.0)
    assert [(p.item_id, p.t_rec_s) for p in got] == [(1055, 15.0), (6672, 19.0)]


def test_owner_purchases_undo_matches_latest_of_same_item():
    ev = [
        {"type": "ITEM_PURCHASED", "participantId": 1, "itemId": 2003, "timestamp": 1000},
        {"type": "ITEM_PURCHASED", "participantId": 1, "itemId": 2003, "timestamp": 2000},
        {
            "type": "ITEM_UNDO",
            "participantId": 1,
            "beforeId": 2003,
            "afterId": 0,
            "timestamp": 2500,
        },
    ]
    got = owner_purchases(_tl(ev), 1, 0.0)
    assert [p.t_rec_s for p in got] == [1.0]


def test_no_transcript_text_logged(cat, caplog):
    caplog.set_level("DEBUG")
    match_item_mentions("secret words kraken", cat)
    assert "secret" not in caplog.text


def test_real_timeline_smoke():
    p = Path(
        r"C:\Users\taile\Videos\ai_video_editor_output\riot\6c643d08-399a-4bc8-889c-0aeb9c73946d"
    )
    if not (p / "timeline.json").exists():
        pytest.skip("no real timeline")
    from ai_video_editor.league import riot_timeline as rt

    cached = rt.load_cached(p)
    tl = rt._from_cache(cached) if cached else None
    if tl is None:
        pytest.skip("cache unusable (riot_id mismatch)")
    raw = json.loads((p / "timeline.json").read_text(encoding="utf-8"))
    got = owner_purchases(raw, tl.owner_pid, tl.offset_s)
    assert got and all(isinstance(x, Purchase) for x in got)
