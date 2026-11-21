import pytest

from ai_video_editor.grokbot import note


def test_note_event_shape():
    ev = note.note_event("  two   previews are up ", urgency="needs_you")
    assert ev.event == "claude.note"
    assert ev.priority is True
    assert ev.payload == {
        "text": "two previews are up",
        "urgency": "needs_you",
        "from": "claude-pc",
    }


@pytest.mark.parametrize("bad", ["", "   ", "x" * (note.MAX_CHARS + 1)])
def test_note_event_rejects_bad_text(bad):
    with pytest.raises(ValueError):
        note.note_event(bad)


def test_note_event_rejects_bad_urgency():
    with pytest.raises(ValueError):
        note.note_event("hi", urgency="urgent")


def test_send_note_emits_once_without_flush():
    seen = []
    ev = note.send_note("hello lead", emit_fn=seen.append, flush=False)
    assert seen == [ev]


def test_main_exits_2_on_empty_text(capsys):
    with pytest.raises(SystemExit) as exc:
        note.main(["   "])
    assert exc.value.code == 2
    assert "empty" in capsys.readouterr().err
