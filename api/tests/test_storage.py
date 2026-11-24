from pathlib import Path

import pytest

from ai_video_editor import storage


def test_vod_key_keeps_folder_layout(tmp_path: Path):
    p = tmp_path / "League of Legends" / "rec_1" / "a b.mp4"
    assert storage.vod_key(p, root=tmp_path) == "League of Legends/rec_1/a b.mp4"


def test_vod_key_rejects_paths_outside_root(tmp_path: Path):
    with pytest.raises(ValueError):
        storage.vod_key(tmp_path.parent / "elsewhere.mp4", root=tmp_path)


def test_local_path_for_key_mirrors_layout(tmp_path: Path):
    dest = storage.local_path_for_key("League of Legends/s/v.mp4", root=tmp_path)
    assert dest == (tmp_path / "League of Legends" / "s" / "v.mp4").resolve()


@pytest.mark.parametrize("key", ["../escape.mp4", "a/../../escape.mp4", ""])
def test_local_path_for_key_rejects_escapes(tmp_path: Path, key: str):
    with pytest.raises(ValueError):
        storage.local_path_for_key(key, root=tmp_path)


def test_budget_counts_overwrites_once():
    gb = 1024**3
    # 99 GB stored, re-upload a 2 GB object as a 4 GB file -> 101 GB.
    total = storage.projected_total(99 * gb, 2 * gb, 4 * gb)
    assert total == 101 * gb
    assert storage.over_budget(total, 100)
    assert not storage.over_budget(100 * gb, 100)


# --- labels (the `<key>.meta.json` sidecar) ---------------------------------


def test_meta_key_and_is_meta_key():
    assert storage.meta_key("League of Legends/s/v.mp4") == "League of Legends/s/v.mp4.meta.json"
    assert storage.is_meta_key("x.mp4.meta.json")
    assert not storage.is_meta_key("x.mp4")


def test_recorded_at_from_outplayed_name():
    name = "League of Legends_11-23-2026_21-30-50-0.mp4"
    assert storage.recorded_at_from_name(name) == "2026-11-23T21:30:50"
    assert storage.recorded_at_from_name("clip.mp4") is None


def _asset() -> dict:
    return {
        "id": "a1",
        "filename": "League of Legends_11-23-2026_21-30-50-0.mp4",
        "path": r"C:\x\League of Legends_11-23-2026_21-30-50-0.mp4",
        "game": "League of Legends_11-23-2026_20-56-5-730",
        "duration_seconds": 2073.984,
    }


def test_vod_label_without_riot_cache(tmp_path: Path):
    label = storage.vod_label(_asset(), riot_dir=tmp_path / "missing")
    assert label["asset_id"] == "a1"
    assert label["session"] == "League of Legends_11-23-2026_20-56-5-730"
    assert label["recorded_at"] == "2026-11-23T21:30:50"
    assert label["duration_s"] == 2074.0
    assert label["game"] is None
    assert (
        label["label"]
        == "League of Legends_11-23-2026_21-30-50-0.mp4 \u00b7 35 min \u00b7 2026-11-23"
    )


def test_vod_label_with_riot_cache_names_the_game_without_ids(tmp_path: Path):
    import json

    match = {
        "metadata": {"matchId": "NA1_1"},
        "info": {
            "gameDuration": 2091,
            "gameVersion": "16.23.1",
            "queueId": 420,
            "participants": [
                {
                    "puuid": "ME-PUUID",
                    "championName": "Zeri",
                    "kills": 8,
                    "deaths": 7,
                    "assists": 12,
                    "win": True,
                },
                {
                    "puuid": "OTHER",
                    "championName": "Jhin",
                    "kills": 1,
                    "deaths": 1,
                    "assists": 1,
                    "win": False,
                },
            ],
        },
    }
    (tmp_path / "match.json").write_text(json.dumps(match), encoding="utf-8")
    meta = {
        "match_id": "NA1_1",
        "confidence": "high",
        "offset_s": -17.0,
        "owner_puuid": "ME-PUUID",
        "riot_id": "name#tag",
    }
    (tmp_path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    label = storage.vod_label(_asset(), riot_dir=tmp_path)
    g = label["game"]
    assert (g["champion"], g["kills"], g["deaths"], g["assists"], g["win"]) == (
        "Zeri",
        8,
        7,
        12,
        True,
    )
    assert g["match_id"] == "NA1_1" and g["offset_s"] == -17.0 and g["queue_id"] == 420
    assert label["label"] == "Zeri 8/7/12 WIN \u00b7 35 min \u00b7 2026-11-23"
    dumped = json.dumps(label)
    assert "ME-PUUID" not in dumped and "name#tag" not in dumped


def test_merge_labels_drops_sidecars_and_attaches_labels():
    objects = [
        {"key": "a.mp4", "size": 10},
        {"key": "a.mp4.meta.json", "size": 1},
        {"key": "b.mp4", "size": 20},
    ]
    merged = storage.merge_labels(objects, {"a.mp4": {"label": "Zeri 8/7/12 WIN"}})
    assert [m["key"] for m in merged] == ["a.mp4", "b.mp4"]
    assert merged[0]["label"] == {"label": "Zeri 8/7/12 WIN"}
    assert merged[1]["label"] is None


def test_upload_sync_writes_the_label_sidecar(tmp_path: Path, monkeypatch):
    from ai_video_editor.routers import cloud

    vod = tmp_path / "v.mp4"
    vod.write_bytes(b"x" * 10)
    calls: dict = {}
    monkeypatch.setattr(storage, "client", lambda: object())
    monkeypatch.setattr(storage, "remote_size", lambda s3, b, k: None)
    monkeypatch.setattr(storage, "stored_bytes", lambda s3: 0)
    monkeypatch.setattr(storage, "upload_file", lambda s3, b, k, p: calls.setdefault("upload", k))
    monkeypatch.setattr(storage, "put_json", lambda s3, b, k, d: calls.setdefault("meta", (k, d)))
    result = cloud._upload_sync(vod, "League of Legends/s/v.mp4", _asset())
    assert calls["upload"] == "League of Legends/s/v.mp4"
    assert calls["meta"][0] == "League of Legends/s/v.mp4.meta.json"
    assert result["label"]["asset_id"] == "a1"
    assert result["already_uploaded"] is False


def test_upload_sync_label_failure_does_not_fail_the_upload(tmp_path: Path, monkeypatch):
    from ai_video_editor.routers import cloud

    vod = tmp_path / "v.mp4"
    vod.write_bytes(b"x" * 10)
    monkeypatch.setattr(storage, "client", lambda: object())
    monkeypatch.setattr(storage, "remote_size", lambda s3, b, k: 10)  # already uploaded

    def boom(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(storage, "put_json", boom)
    result = cloud._upload_sync(vod, "League of Legends/s/v.mp4", _asset())
    assert result["already_uploaded"] is True
    assert result["label"] is None
