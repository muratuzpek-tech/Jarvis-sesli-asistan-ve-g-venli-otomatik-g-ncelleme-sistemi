"""Kiralık GPU sunucusu: ayarlıysa ve cevap veriyorsa kullanılır, yoksa yerel model."""
from __future__ import annotations

from jarvis.actions import dev_agent as da


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def _setup(monkeypatch, tags=None, fail=False):
    import requests
    monkeypatch.setattr(da, "_REMOTE_STATE", {"checked_at": -1e9, "result": None})
    calls = []

    def fake_get(url, timeout=0):
        calls.append(url)
        if fail:
            raise requests.ConnectionError("kapalı")
        return _Resp(tags or {"models": []})
    monkeypatch.setattr(requests, "get", fake_get)
    return calls


def test_not_configured_means_local(monkeypatch):
    monkeypatch.delenv("JARVIS_REMOTE_OLLAMA_URL", raising=False)
    assert da._remote_ollama() is None


def test_remote_used_when_model_present(monkeypatch):
    monkeypatch.setenv("JARVIS_REMOTE_OLLAMA_URL", "http://localhost:11435/")
    monkeypatch.delenv("JARVIS_REMOTE_MODEL", raising=False)
    calls = _setup(monkeypatch, {"models": [{"name": "devstral:24b"}]})
    assert da._remote_ollama() == ("http://localhost:11435", "devstral:24b")
    assert da._remote_ollama() == ("http://localhost:11435", "devstral:24b")
    assert len(calls) == 1          # 60 sn önbellek


def test_remote_down_falls_back_to_local(monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_REMOTE_OLLAMA_URL", "http://localhost:11435")
    _setup(monkeypatch, fail=True)
    assert da._remote_ollama() is None
    assert "yerel model" in capsys.readouterr().out


def test_missing_model_falls_back_to_local(monkeypatch):
    monkeypatch.setenv("JARVIS_REMOTE_OLLAMA_URL", "http://localhost:11435")
    monkeypatch.setenv("JARVIS_REMOTE_MODEL", "qwen3-coder:30b")
    _setup(monkeypatch, {"models": [{"name": "devstral:24b"}]})
    assert da._remote_ollama() is None
