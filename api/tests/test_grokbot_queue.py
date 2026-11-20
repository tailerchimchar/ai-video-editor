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
        env("verdict", game="lol", clip_ref="c", verdict="style", rule="C:\\x"),
        env("verdict", game="lol", clip_ref="a;b", verdict="style", rule="r"),
        env("verdict", game="lol", clip_ref="c", verdict="style", rule="r", quote="`x`"),
        env("verdict", game="lol", clip_ref="c", verdict="style", rule="r", quote="$(id)"),
        env("verdict", game="lol", clip_ref="c", verdict="style", rule="a|b"),
        env("verdict", game="lol", clip_ref="c", verdict="style", rule="/etc/passwd"),
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
    ],
)
def test_path_forms_rejected(bad):
    with pytest.raises(QueueRejected):
        parse_item(preview(question=bad))


@pytest.mark.parametrize("good", ["and/or", "x/etc/x", "1/2 hp"])
def test_benign_slashes_allowed(good):
    parse_item(preview(question=good))


def test_has_quote_strips_whitespace():
    def mk(q):
        return parse_item(
            env("verdict", game="lol", clip_ref="c", verdict="style", rule="r", quote=q)
        )

    assert not mk("   ").has_quote
    assert mk("gg").has_quote
