"""Gemini CLI arka ucu: ücretsiz günlük 1000 istek; hata/kota durumunda yerel model."""
from __future__ import annotations

import stat

import pytest

from jarvis.actions import dev_agent as da


def _fake_cli(tmp_path, body: str) -> str:
    p = tmp_path / "gemini"
    p.write_text("#!/usr/bin/env python3\nimport sys, json, os\nprompt = sys.stdin.read()\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


class _Local:
    def generate_content(self, c):
        return da._GeminiCliResponse("yerel-cevap")


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(da, "_GEMINI_CLI_OFF_UNTIL", 0.0)


def test_cli_answer_is_used_and_runs_in_empty_dir(tmp_path):
    cli = _fake_cli(tmp_path, "print(json.dumps({'response': 'KOD cwd=' + str(len(os.listdir('.'))) + "
                               "(' kurallar' if 'Do NOT use any tools' in prompt else '')}))\n")
    r = da._GeminiCli(cli, fallback=_Local()).generate_content("main.py yaz")
    assert r.text == "KOD cwd=0 kurallar"


def test_quota_error_falls_back_and_disables_for_hours(tmp_path, capsys):
    cli = _fake_cli(tmp_path, "print(json.dumps({'error': {'message': '429 RESOURCE_EXHAUSTED quota'}}))\n")
    r = da._GeminiCli(cli, fallback=_Local()).generate_content("x")
    assert r.text == "yerel-cevap" and "kota" in capsys.readouterr().out
    assert da._GEMINI_CLI_OFF_UNTIL - da.time.time() > 5 * 3600


def test_crash_without_fallback_raises(tmp_path):
    cli = _fake_cli(tmp_path, "sys.exit(1)\n")
    with pytest.raises(RuntimeError):
        da._GeminiCli(cli, fallback=None).generate_content("x")


def test_path_detection_respects_local_switch(tmp_path, monkeypatch):
    cli = _fake_cli(tmp_path, "print('x')\n")
    monkeypatch.setenv("JARVIS_GEMINI_CLI", cli)
    monkeypatch.setenv("JARVIS_DEVAGENT_BACKEND", "gemini")
    assert da._gemini_cli_path() == cli
    monkeypatch.setenv("JARVIS_DEVAGENT_BACKEND", "local")
    assert da._gemini_cli_path() is None


def test_plain_text_output_is_accepted(tmp_path):
    cli = _fake_cli(tmp_path, "print('duz metin cevap')\n")
    assert da._GeminiCli(cli).generate_content("x").text == "duz metin cevap"
