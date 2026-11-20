import json
import uuid

import pytest

from ai_video_editor.grokbot.queue import (
    AppliedLedger,
    QueueRejected,
    parse_item,
    parse_sleep_flag,
)


def env(kind, **kw):
    d = {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "author_bot": "lead",
        "created": "2026-11-20T00:00:00Z",
        "schema_version": 1,
    }
    d.update(kw)
    return json.dumps(d)


def preview(**kw):
    base = {"asset_id": "a1", "start_s": 1.0, "end_s": 5.0, "question": "is this a payoff?"}
    base.update(kw)
    return env("preview_request", **base)


def plan(effects=(), cards=(), sfx=()):
    return {
        "format": "short",
        "effects": list(effects),
        "cards": list(cards),
        "sfx": list(sfx),
    }


def edit(p):
    return env("edit_plan", compilation_id="c1", base_version="v1", plan=p)


def test_parse_preview_request_ok():
    assert parse_item(preview()).kind == "preview_request"


def test_extra_field_is_rejected():
    with pytest.raises(QueueRejected, match="extra"):
        parse_item(preview(slowmo=True))


@pytest.mark.parametrize(
    "text",
    [
        preview(question="see https://x"),
        env("verdict", game="league", clip_ref="c", verdict="style", rule="C:\\x"),
        env("verdict", game="league", clip_ref="a;b", verdict="style", rule="r"),
        env("verdict", game="league", clip_ref="c", verdict="style", rule="r", quote="`x`"),
        env("verdict", game="league", clip_ref="c", verdict="style", rule="r", quote="$(id)"),
        env("verdict", game="league", clip_ref="c", verdict="style", rule="a|b"),
        env("verdict", game="league", clip_ref="c", verdict="style", rule="/etc/passwd"),
    ],
)
def test_hostile_strings_rejected(text):
    with pytest.raises(QueueRejected):
        parse_item(text)


def test_hostile_nested_in_plan():
    p = plan()
    p["rejected"] = [{"wanted": "x", "why": "see http://evil"}]
    with pytest.raises(QueueRejected):
        parse_item(edit(p))


@pytest.mark.parametrize(
    "kw",
    [
        {"start_s": 0, "end_s": 25},
        {"start_s": 5, "end_s": 3},
        {"start_s": -1, "end_s": 3},
        {"asset_id": "a", "compilation_id": "c"},
        {"asset_id": None},
    ],
)
def test_preview_window_limits(kw):
    with pytest.raises(QueueRejected):
        parse_item(preview(**kw))


OK_EFFECT = {"clip_ref": "c", "kind": "zoom", "at": 0.0}


def test_edit_plan_ok():
    assert parse_item(edit(plan([OK_EFFECT]))).kind == "edit_plan"


def test_edit_plan_with_timed_effect_rejected():
    with pytest.raises(QueueRejected, match="timed effects are not applied yet"):
        parse_item(edit(plan([{**OK_EFFECT, "at": 1.5}])))


def test_nested_extra_key_rejected():
    with pytest.raises(QueueRejected, match="extra"):
        parse_item(edit(plan([{**OK_EFFECT, "slowmo": 1}])))


def test_hostile_in_effect_params_key_and_value():
    with pytest.raises(QueueRejected):
        parse_item(edit(plan([{**OK_EFFECT, "params": {"k": "see https://x"}}])))
    with pytest.raises(QueueRejected):
        parse_item(edit(plan([{**OK_EFFECT, "params": {"a;b": "v"}}])))


def test_edit_plan_with_cards_or_sfx_rejected():
    with pytest.raises(QueueRejected):
        parse_item(edit(plan(cards=[{"text": "hi"}])))
    with pytest.raises(QueueRejected):
        parse_item(edit(plan(sfx=[{"clip_ref": "c", "at": 0, "sound": "boom"}])))


def test_deploy_tag_format():
    assert parse_item(env("deploy", tag="v1.2.3")).tag == "v1.2.3"
    with pytest.raises(QueueRejected):
        parse_item(env("deploy", tag="1.2"))


def test_discriminator_unknown_kind_rejected():
    with pytest.raises(QueueRejected):
        parse_item(env("nuke"))


def test_invalid_json_rejected():
    with pytest.raises(QueueRejected):
        parse_item("{nope")


def test_sleep_wake_ok():
    assert parse_item(env("sleep")).kind == "sleep"
    assert parse_item(env("wake")).kind == "wake"


def test_ledger_dedupes(tmp_path):
    led = AppliedLedger(tmp_path / "l.db")
    assert not led.seen("x")
    led.mark("x", "deploy", "ok")
    led.mark("x", "deploy", "other")
    assert led.seen("x")
    assert led.outcomes() == {"x": "ok"}


def test_sleep_flag(tmp_path):
    assert not parse_sleep_flag(tmp_path)
    (tmp_path / "SLEEP").write_text("")
    assert parse_sleep_flag(tmp_path)


def test_sleep_flag_directory_is_false(tmp_path):
    (tmp_path / "SLEEP").mkdir()
    assert not parse_sleep_flag(tmp_path)


def test_deeply_nested_rejected():
    with pytest.raises(QueueRejected):
        parse_item("[" * 10000 + "]" * 10000)


def test_too_large_rejected():
    with pytest.raises(QueueRejected, match="item too large"):
        parse_item(preview(question="x" * 70000))


@pytest.mark.parametrize(
    "bad",
    [
        "/etc",
        "/passwd",
        "a,/etc/x",
        "a:/etc/x",
        "[/etc/x",
        "//host/share/x",
        "\\\\host\\share",
        "../x",
        "a/../x",
        "..\\x",
        "x /etc",
        "\\windows\\x",
        "a\\b",
        "./etc/x",
        "a && b",
        "x > y",
        "x < y",
    ],
)
def test_path_forms_rejected(bad):
    with pytest.raises(QueueRejected):
        parse_item(preview(question=bad))


@pytest.mark.parametrize("good", ["and/or", "x/etc/x", "1/2 hp", "v1.2.3"])
def test_benign_slashes_allowed(good):
    parse_item(preview(question=good))


def test_has_quote_strips_whitespace():
    def mk(q):
        return parse_item(
            env("verdict", game="league", clip_ref="c", verdict="style", rule="r", quote=q)
        )

    assert not mk("   ").has_quote
    assert mk("gg").has_quote


def verdict(game):
    return env("verdict", game=game, clip_ref="c", verdict="style", rule="r", quote="gg")


@pytest.mark.parametrize("game", ["league", "valorant"])
def test_verdict_game_known_ok(game):
    assert parse_item(verdict(game)).game == game


@pytest.mark.parametrize(
    "game", ["lol", "League", "a/b", "d:x", "x", "league ", "../league", "fortnite", ""]
)
def test_verdict_game_must_be_a_known_game(game):
    with pytest.raises(QueueRejected):
        parse_item(verdict(game))


def music_plan(song_id):
    return edit({"format": "short", "music": {"song_id": song_id}})


@pytest.mark.parametrize("song_id", ["song-1", "abc_DEF_09", "a" * 64])
def test_music_song_id_library_ids_ok(song_id):
    assert parse_item(music_plan(song_id)).plan.music.song_id == song_id


@pytest.mark.parametrize(
    "song_id", ["sub/dir/file.mp3", "C:file.mp3", "file.mp3", "a b", "", "a" * 65, "x\\y"]
)
def test_music_song_id_must_be_a_library_id(song_id):
    with pytest.raises(QueueRejected):
        parse_item(music_plan(song_id))


def test_validation_reason_never_echoes_input():
    secret = "transcript line that must stay home"
    with pytest.raises(QueueRejected) as e:
        parse_item(preview(start_s=secret))
    assert secret not in e.value.reason
    assert "start_s" in e.value.reason


def test_importing_queue_models_emits_no_shadow_warning():
    # A fresh interpreter: reloading in-process would split the model classes.
    import subprocess
    import sys
    from pathlib import Path

    r = subprocess.run(
        [sys.executable, "-W", "error::UserWarning", "-c", "import ai_video_editor.grokbot.queue"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    assert "shadows an attribute" not in r.stderr
