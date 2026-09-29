"""Gemini kotası dolunca arka plan işleri Gemini'yi tekrar tekrar denemez; önce Groq."""
from __future__ import annotations

import pytest

from jarvis.actions import local_llm as ll


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(ll, "_GEMINI_QUOTA_UNTIL", 0.0)


def test_quota_error_latches_and_uses_cloud(monkeypatch):
    calls = []

    def gemini():
        calls.append(1)
        raise RuntimeError("429 RESOURCE_EXHAUSTED quota")
    monkeypatch.setattr(ll, "cloud_generate", lambda p: "groq-cevap")
    monkeypatch.setattr(ll, "ollama_generate", lambda p, timeout=0: "yerel")
    assert ll.generate_with_fallback(gemini, "soru", source="test") == "groq-cevap"
    assert ll.generate_with_fallback(gemini, "soru", source="test") == "groq-cevap"
    assert calls == [1], "kota dolduktan sonra Gemini tekrar denenmemeli"


def test_without_cloud_falls_to_ollama_then_raises(monkeypatch):
    def gemini():
        raise RuntimeError("503 UNAVAILABLE")
    monkeypatch.setattr(ll, "cloud_generate", lambda p: None)
    monkeypatch.setattr(ll, "ollama_generate", lambda p, timeout=0: "yerel")
    assert ll.generate_with_fallback(gemini, "x") == "yerel"
    assert ll._GEMINI_QUOTA_UNTIL == 0.0          # geçici hata kota sayılmaz
    monkeypatch.setattr(ll, "ollama_generate", lambda p, timeout=0: None)
    with pytest.raises(RuntimeError):
        ll.generate_with_fallback(gemini, "x")


def test_gemini_success_is_unchanged(monkeypatch):
    class R:
        text = " gemini "
    monkeypatch.setattr(ll, "cloud_generate", lambda p: pytest.fail("çağrılmamalı"))
    assert ll.generate_with_fallback(lambda: R(), "x") == "gemini"
