"""secret-scan: invented strings only, values always masked."""

from __future__ import annotations

import pytest

from ai_video_editor.tools import secret_scan as ss

FAKE = {
    "anthropic": "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1",
    "riot": "RGAPI-" + "0123456789abcdef0123456789abcdef",
    "google_secret": "GOCSPX-" + "abcdEFGH12",
    "google_refresh": "1//0" + "abcdefghijklmnopqrstuvwxyz0123456789",
    "github_pat_classic": "ghp_" + "A" * 36,
    "github_pat": "github_pat_" + "B" * 40,
    "langfuse_public": "pk-lf-" + "0123456789abcdef0123",
    "langfuse_secret": "sk-lf-" + "0123456789abcdef0123",
    "xai": "xai-" + "C" * 40,
    "typesafe": "ts_" + "D" * 28,
}


@pytest.mark.parametrize("name", sorted(FAKE))
def test_every_pattern_hits_with_line_number(name):
    hits = ss.scan_text(f"ok line\nsomething {FAKE[name]} here\n")
    assert (name, 2) in hits


def test_typesafe_long_token_on_typesafe_line():
    assert ("typesafe", 1) in ss.scan_text("TYPESAFE_KEY=" + "x" * 44)
    assert ss.scan_text("x" * 44) == []


def test_clean_text_has_no_hits():
    assert ss.scan_text("just a normal digest with kills and a pentakill") == []


def test_scan_paths_masks_value_and_skips_env_and_binaries(tmp_path):
    f = tmp_path / "digest.json"
    f.write_text("{\n" + f'"k": "{FAKE["anthropic"]}"\n' + "}", encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(FAKE["anthropic"], encoding="utf-8")
    binary = tmp_path / "x.bin"
    binary.write_bytes(b"\x00\x01" + FAKE["anthropic"].encode())
    hits = ss.scan_paths([f, env, binary, tmp_path / "missing.txt"])
    assert [(h.path, h.line, h.pattern) for h in hits] == [(f, 2, "anthropic")]
    assert FAKE["anthropic"] not in repr(hits)


def test_scan_paths_walks_directories(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.md").write_text(FAKE["riot"], encoding="utf-8")
    assert len(ss.scan_paths([tmp_path])) == 1


def test_main_exit_codes_and_masking(tmp_path, capsys):
    clean = tmp_path / "clean.txt"
    clean.write_text("nothing here")
    assert ss.main([str(clean)]) == 0
    dirty = tmp_path / "dirty.txt"
    dirty.write_text(FAKE["xai"])
    assert ss.main([str(dirty)]) == 1
    out = capsys.readouterr().out
    assert "dirty.txt:1 xai" in out
    assert FAKE["xai"] not in out
