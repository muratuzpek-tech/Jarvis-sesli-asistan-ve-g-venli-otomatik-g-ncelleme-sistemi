"""Onay sertlestirmesi: (1) onay cumlesi yalnizca onay kelimelerinden
olusmali; (2) dev_agent confirm_code'u yalnizca kullanicinin SON turu bir
onaysa, kod verildikten sonra geldiyse ve kod suresi dolmadiysa kabul eder.

Ag yok, gercek HOME'a dokunulmaz (conftest HOME/JARVIS_HOME'u izole eder;
_build_project ve DB yolu tmp_path'e yonlendirilir)."""
import time

import pytest

import jarvis.actions.agent_board as board
import jarvis.actions.dev_agent as da
from jarvis.core.user_confirmation import is_confirmation, is_rejection
from jarvis.main import JarvisLive

REQUEST = "CSV dosyasındaki satırları sayan bir araç yaz ve sonucu rapor.txt dosyasına kaydet"

NOT_CONFIRMATIONS = [
    "Tamam ama önce ne yapacağını söyle",
    "ok şimdi hava durumuna bak",
    "evet, bu dosya ne?",
    "hayır", "evet ama iptal", "tamam dur", "no", "", "   ",
]
CONFIRMATIONS = ["evet", "tamam", "evet devam et", "onaylıyorum", "Evet devam edebilirsiniz",
                 "Evet.", "ok", "evet lütfen"]


# ── 1) saf onay fonksiyonu + JarvisLive._is_confirmation ──────────────────
@pytest.mark.parametrize("text", NOT_CONFIRMATIONS)
def test_extra_action_sentences_are_not_confirmation(text):
    assert is_confirmation(text) is False
    assert JarvisLive._is_confirmation(text) is False


@pytest.mark.parametrize("text", CONFIRMATIONS)
def test_pure_confirmations_are_confirmation(text):
    assert is_confirmation(text) is True
    assert JarvisLive._is_confirmation(text) is True


def test_reference_tokens_do_not_count_as_extra_action():
    # Kimlik/kod tek basina onay degildir; onay kelimesiyle birlikte ek eylem sayilmaz.
    assert is_confirmation("evet 52d6b6b2 confirm_code=abc123") is True
    assert is_confirmation("52d6b6b2") is False
    assert is_confirmation("evet 52d6b6b2 sil") is False


def test_rejection_detects_negative_words():
    assert is_rejection("hayır yapma") and is_rejection("evet ama iptal")
    assert not is_rejection("evet devam et")


# ── 2) dev_agent onay kapisi ──────────────────────────────────────────────
@pytest.fixture
def gate(monkeypatch, tmp_path):
    monkeypatch.setattr(da, "_pending_dev_agent", {})
    monkeypatch.setattr(da, "_task_to_code", {})
    monkeypatch.setattr(da, "_last_user_turn_at", None)
    monkeypatch.setattr(da, "_last_user_turn_text", None, raising=False)
    monkeypatch.setattr(board, "DB_PATH", tmp_path / "agent_board.db")
    built = []
    monkeypatch.setattr(da, "_build_project", lambda **kw: built.append(kw) or "Project 'x' is working")
    return built


def _code(preview: str) -> str:
    return preview.split("confirm_code='")[1].split("'")[0]


def _issue() -> str:
    return _code(da.dev_agent({"description": REQUEST}))


def _later(code: str, seconds: float = 5.0) -> float:
    return da._pending_dev_agent[code]["issued_at"] + seconds


def test_no_user_turn_tracking_fails_closed(gate):
    code = _issue()
    assert da._last_user_turn_at is None
    assert da.confirmation_problem(code) is not None
    assert da.dev_agent({"description": REQUEST, "confirm_code": code}).startswith("ONAY HENÜZ")
    assert gate == []


def test_rejection_turn_cancels_pending_code(gate):
    code = _issue()
    da.note_user_turn("hayır", now=_later(code))
    assert code not in da._pending_dev_agent
    assert da.dev_agent({"description": REQUEST, "confirm_code": code}).startswith("Onay kodu geçersiz")
    assert gate == []


def test_unrelated_sentence_is_not_confirmation(gate):
    code = _issue()
    da.note_user_turn("ok şimdi hava durumuna bak", now=_later(code))
    assert da.dev_agent({"description": REQUEST, "confirm_code": code}).startswith("ONAY HENÜZ")
    assert gate == [] and code in da._pending_dev_agent


def test_confirmation_turn_is_accepted(gate):
    code = _issue()
    da.note_user_turn("Evet devam edebilirsiniz", now=_later(code))
    assert "is working" in da.dev_agent({"description": REQUEST, "confirm_code": code})
    assert len(gate) == 1


def test_confirmation_inside_grace_window_is_rejected(gate):
    code = _issue()
    da.note_user_turn("evet", now=_later(code, 0.3))
    assert da.dev_agent({"description": REQUEST, "confirm_code": code}).startswith("ONAY HENÜZ")


def test_pending_code_expires_after_ttl(gate):
    code = _issue()
    da._pending_dev_agent[code]["issued_at"] = time.monotonic() - da.PENDING_TTL_S - 1
    da.note_user_turn("evet", now=time.monotonic())
    assert da.PENDING_TTL_S == 300
    assert da.dev_agent({"description": REQUEST, "confirm_code": code}).startswith("Onay kodu geçersiz")
    assert code not in da._pending_dev_agent and gate == []


def test_parallel_task_rejected_after_no(gate):
    code = _code(board.start_parallel_task({"description": REQUEST}))
    da.note_user_turn("hayır", now=_later(code))
    assert board.start_parallel_task({"description": REQUEST, "confirm_code": code}).startswith("Onay kodu")


def test_parallel_task_rejected_after_unrelated_sentence(gate):
    code = _code(board.start_parallel_task({"description": REQUEST}))
    da.note_user_turn("evet, bu dosya ne?", now=_later(code))
    assert board.start_parallel_task({"description": REQUEST, "confirm_code": code}).startswith("ONAY HENÜZ")


def test_parallel_task_rejected_without_tracking(gate):
    code = _code(board.start_parallel_task({"description": REQUEST}))
    assert board.start_parallel_task({"description": REQUEST, "confirm_code": code}).startswith("ONAY HENÜZ")
