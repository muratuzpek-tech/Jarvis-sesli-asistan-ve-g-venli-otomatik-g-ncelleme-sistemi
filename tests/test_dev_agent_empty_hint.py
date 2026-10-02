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


def test_digits_only_price_parsing_is_flagged():
    """Murat@goxs 2026-09-29 book_scraper: '£51.77' → 5177, 0 kitap bulundu."""
    code = "price = float(''.join(filter(str.isdigit, price_text)))\n"
    hint = da._known_code_hint({"utils/helpers.py": code})
    assert "utils/helpers.py" in hint and "5177" in hint and "resp.content" in hint
    assert da._known_code_hint({"a.py": "price = float(re.search(r'\\d+', t).group())"}) == ""
    assert da._known_code_hint({"a.py": "x = float(''.join(c for c in s if c.isdigit()))"}) != ""


def test_repeated_failure_raises_temperature_only_for_that_fix(monkeypatch):
    import os
    monkeypatch.delenv("JARVIS_OLLAMA_TEMP", raising=False)
    assert da._ollama_options()["temperature"] == 0.2
    monkeypatch.setenv("JARVIS_OLLAMA_TEMP", "0.7")
    assert da._ollama_options()["temperature"] == 0.7
    monkeypatch.delenv("JARVIS_OLLAMA_TEMP")
    assert "JARVIS_OLLAMA_TEMP" not in os.environ
