"""Ücretsiz bulut modeli (Groq, OpenAI uyumlu) arka ucu."""
from __future__ import annotations

import pytest

from jarvis.actions import dev_agent as da


class _Resp:
    def __init__(self, code, data=None, text="", headers=None):
        self.status_code, self._d, self.text, self.headers = code, data, text, headers or {}

    def json(self):
        return self._d


class _Local:
    def generate_content(self, c):
        return da._GeminiCliResponse("yerel")


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(da, "_CLOUD_OFF_UNTIL", 0.0)
    monkeypatch.setattr(da.time, "sleep", lambda s: None)


def _post(monkeypatch, responses, seen=None):
    import requests
    it = iter(responses)

    def fake(url, json=None, headers=None, timeout=0):
        if seen is not None:
            seen.append((url, json, headers))
        return next(it)
    monkeypatch.setattr(requests, "post", fake)


def test_answer_is_returned_and_key_sent_only_in_header(monkeypatch):
    seen = []
    _post(monkeypatch, [_Resp(200, {"choices": [{"message": {"content": "KOD"}}]})], seen)
    r = da._CloudLLM("https://x/v1", "m", "gsk_gizli", fallback=_Local()).generate_content("yaz")
    assert r.text == "KOD"
    url, body, headers = seen[0]
    assert url == "https://x/v1/chat/completions" and headers["Authorization"] == "Bearer gsk_gizli"
    assert "gsk_gizli" not in str(body)


def test_minute_limit_waits_once_then_succeeds(monkeypatch):
    _post(monkeypatch, [_Resp(429, text="rate limit per minute", headers={"retry-after": "2"}),
                        _Resp(200, {"choices": [{"message": {"content": "OK"}}]})])
    assert da._CloudLLM("u", "m", "k", fallback=_Local()).generate_content("x").text == "OK"


def test_daily_limit_falls_back_and_disables_for_hours(monkeypatch, capsys):
    _post(monkeypatch, [_Resp(429, text="Rate limit reached ... tokens per day (TPD)")])
    assert da._CloudLLM("u", "m", "gsk_x", fallback=_Local()).generate_content("x").text == "yerel"
    out = capsys.readouterr().out
    assert "günlük" in out and "gsk_x" not in out
    assert da._CLOUD_OFF_UNTIL - da.time.time() > 5 * 3600


def test_too_big_request_uses_local_without_disabling(monkeypatch):
    _post(monkeypatch, [_Resp(413, text="Request too large for model")])
    assert da._CloudLLM("u", "m", "k", fallback=_Local()).generate_content("x").text == "yerel"
    assert da._CLOUD_OFF_UNTIL == 0.0


def test_no_key_means_no_cloud(monkeypatch):
    monkeypatch.delenv("JARVIS_DEVAGENT_BACKEND", raising=False)
    monkeypatch.delenv("JARVIS_CLOUD_LLM_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    import jarvis.core.secure_config as sc
    monkeypatch.setattr(sc, "load_config", lambda: {})
    assert da._cloud_llm() is None
    monkeypatch.setenv("GROQ_API_KEY", "gsk_testanahtar12345")
    assert da._cloud_llm() == (da.CLOUD_LLM_DEFAULT_URL, da.CLOUD_LLM_DEFAULT_MODEL, "gsk_testanahtar12345")
    monkeypatch.setenv("JARVIS_DEVAGENT_BACKEND", "local")
    assert da._cloud_llm() is None


def test_tokens_per_minute_limit_waits_as_told_and_retries(monkeypatch):
    """Canlı deneme 2026-09-30: 3. dosyada TPM 429 → 10 dk yerel modele düşülmüştü."""
    waits = []
    monkeypatch.setattr(da.time, "sleep", lambda s: waits.append(s))
    tpm = ('{"error":{"message":"Rate limit reached for model on tokens per minute (TPM): Limit 8000, '
           'Used 7000, Requested 3000. Please try again in 7.5s."}}')
    _post(monkeypatch, [_Resp(429, text=tpm), _Resp(429, text=tpm),
                        _Resp(200, {"choices": [{"message": {"content": "OK"}}]})])
    assert da._CloudLLM("u", "m", "k", fallback=_Local()).generate_content("x").text == "OK"
    assert waits == [8.5, 8.5] and da._CLOUD_OFF_UNTIL == 0.0
