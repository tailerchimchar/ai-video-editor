from ai_video_editor.config import settings
from ai_video_editor.league import play_vocab


def test_packaged_vocab_has_eight_rules(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "game_hints_dir", tmp_path)
    v = play_vocab.load_vocab()
    assert set(v.rules) == {
        "teamfight",
        "skirmish",
        "objective_fight",
        "closeout",
        "gank",
        "dive",
        "caught_out",
        "trade",
    }
    assert v.rules["caught_out"].end["max_after_last_kill_s"] == 2
    assert v.rules["gank"].start["fallback"] == "first_kill - 8"
    assert len(v.hash) == 12


_TWO = """
teamfight:
  phase: [mid]
  propose: big
  start: {story: approach, montage: engage, fallback: "first_kill - 8"}
  end: {rule: outcome, max_after_last_kill_s: 20}
  vlm_confirms: [a]
  facts_from_riot: [b]
trade:
  phase: [early]
  propose: small
  start: {story: approach, montage: engage, fallback: "first_kill - 8"}
  end: {rule: both back off, max_after_last_kill_s: 8}
  vlm_confirms: []
  facts_from_riot: []
"""


def test_workspace_vocab_overrides_packaged(monkeypatch, tmp_path):
    packaged = play_vocab.load_vocab()
    (tmp_path / "league_plays.yaml").write_text(_TWO, encoding="utf-8")
    monkeypatch.setattr(settings, "game_hints_dir", tmp_path)
    v = play_vocab.load_vocab()
    assert set(v.rules) == {"teamfight", "trade"}
    assert v.hash != packaged.hash
    assert v.rules["trade"].type == "trade"


def test_hash_changes_with_content(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "game_hints_dir", tmp_path)
    f = tmp_path / "league_plays.yaml"
    f.write_text(_TWO, encoding="utf-8")
    h1 = play_vocab.load_vocab().hash
    f.write_text(_TWO + "\n# edit\n", encoding="utf-8")
    assert play_vocab.load_vocab().hash != h1
