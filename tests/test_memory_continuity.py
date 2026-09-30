"""Murat@goxs 2026-09-30: 'JARVIS unutuyor' — oturum kopunca konuşma sıfırlanıyordu."""
from __future__ import annotations

import json
import types as _t
from datetime import datetime, timedelta

from jarvis.actions import conversation_log as cl
from jarvis.memory import memory_manager as mm


def _write_log(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")


def test_recent_context_lists_last_turns_in_order(tmp_path, monkeypatch):
    log = tmp_path / "conversation_log.jsonl"
    now = datetime.now()
    _write_log(log, [
        {"timestamp": (now - timedelta(days=2)).isoformat(), "role": "user", "text": "çok eski"},
        {"timestamp": (now - timedelta(minutes=10)).isoformat(), "role": "user", "text": "kitap projesini yap"},
        {"timestamp": (now - timedelta(minutes=9)).isoformat(), "role": "jarvis", "text": "books_scraper hazır"},
    ])
    monkeypatch.setattr(cl, "LOG_PATH", log)
    ctx = cl.recent_context()
    assert "çok eski" not in ctx
    assert ctx.index("Murat: kitap projesini yap") < ctx.index("JARVIS: books_scraper hazır")


def test_recent_context_empty_without_log(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "LOG_PATH", tmp_path / "yok.jsonl")
    assert cl.recent_context() == ""


def test_long_term_memory_keeps_more_than_before(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "MEMORY_PATH", tmp_path / "long_term.json")
    for i in range(40):
        mm.update_memory({"notes": {f"not_{i}": {"value": f"Murat'ın {i}. notu: " + "x" * 60}}})
    kept = len(mm.load_memory()["notes"])
    assert kept >= 40, kept            # eski sınır (2200 karakter) ~20 notta siliyordu


def _fake_self(handle=None, age=0.0):
    import time
    return _t.SimpleNamespace(_resume_handle=handle, _resume_time=time.time() - age)


def test_config_resumes_session_or_injects_recent_turns(tmp_path, monkeypatch):
    from jarvis import main as jm
    monkeypatch.setattr(jm, "load_memory", lambda: {})
    monkeypatch.setattr(cl, "recent_context", lambda: "[SON KONUŞMALAR]\n10:00 Murat: merhaba\n")
    fresh = jm.JarvisLive._build_config(_fake_self())
    assert "[SON KONUŞMALAR]" in fresh.system_instruction
    assert fresh.context_window_compression is not None
    resumed = jm.JarvisLive._build_config(_fake_self("anahtar-123", age=60))
    assert resumed.session_resumption.handle == "anahtar-123"
    assert "[SON KONUŞMALAR]" not in resumed.system_instruction
    expired = jm.JarvisLive._build_config(_fake_self("eski", age=3 * 3600))
    assert expired.session_resumption.handle is None
