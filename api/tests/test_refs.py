import pytest

from ai_video_editor.refs import normalize_clip_ref


@pytest.mark.parametrize(
    "ref",
    ["06_kill_23m05s.mp4", "23m05s", "1385.0", "23:05", "1385", "02_hype_23m05s"],
)
def test_all_forms_normalise_to_nmnns(ref):
    assert normalize_clip_ref(ref) == "23m05s"


def test_leading_zero_minutes_dropped():
    assert normalize_clip_ref("7m27s") == normalize_clip_ref("07m27s") == "7m27s"


def test_opaque_ref_is_unchanged():
    assert normalize_clip_ref("hero-clip") == "hero-clip"
