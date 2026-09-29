"""Canlı test 2026-09-29 (hata_saatleri): boş çıktıda model girdinin gerçek satırlarını görmeli."""
from __future__ import annotations

from jarvis.actions import dev_agent as da


def test_input_sample_hint_shows_field_indices(tmp_path):
    logs = tmp_path / "loglar" / "eski"
    logs.mkdir(parents=True)
    (logs / "app.1.log").write_text("2026-09-01 09:15:00 ERROR disk dolu\n", encoding="utf-8")
    (tmp_path / "loglar" / "resim.bin").write_bytes(b"\x00\x01")
    hint = da._input_sample_hint(f"python main.py {tmp_path / 'loglar'}", tmp_path)
    assert "[2]'ERROR'" in hint and "app.1.log" in hint and "resim.bin" not in hint


def test_input_sample_hint_empty_without_inputs(tmp_path):
    assert da._input_sample_hint("python main.py", tmp_path) == ""
    assert da._input_sample_hint("python main.py https://example.com", tmp_path) == ""
