"""Kabul testi yazımı ve hakemlik Gemini'ye gider; ulaşılamazsa yerel modele düşer."""
from __future__ import annotations

from jarvis.actions import dev_agent as da


class _M:
    def __init__(self, text, fail=False):
        self.text, self.fail = text, fail

    def generate_content(self, prompt):
        if self.fail:
            raise RuntimeError("kota")
        return self


def test_judge_uses_gemini_by_default(monkeypatch):
    calls = []
    monkeypatch.delenv("JARVIS_DEVAGENT_JUDGE", raising=False)
    monkeypatch.setattr(da, "_get_model", lambda name, prefer="": calls.append(prefer) or _M(f"cevap-{prefer or 'yerel'}"))
    assert da._judge_generate("x", log=lambda m: None) == "cevap-gemini"
    assert calls == ["gemini"]


def test_judge_falls_back_to_local_when_gemini_fails(monkeypatch):
    monkeypatch.delenv("JARVIS_DEVAGENT_JUDGE", raising=False)
    monkeypatch.setattr(da, "_get_model", lambda name, prefer="": _M("", fail=True) if prefer else _M("yerel"))
    msgs = []
    assert da._judge_generate("x", log=msgs.append) == "yerel"
    assert msgs and "yerel model" in msgs[0]


def test_judge_can_be_forced_local(monkeypatch):
    monkeypatch.setenv("JARVIS_DEVAGENT_JUDGE", "local")
    calls = []
    monkeypatch.setattr(da, "_get_model", lambda name, prefer="": calls.append(prefer) or _M("yerel"))
    assert da._judge_generate("x", log=lambda m: None) == "yerel" and calls == [""]
