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
