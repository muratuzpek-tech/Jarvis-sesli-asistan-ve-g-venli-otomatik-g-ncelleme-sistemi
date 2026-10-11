"""tests/test_background_task_result.py

Gorev 5: arka plan (start_parallel_task -> dev_agent) gorevi bitince sonuc
metninde proje klasoru yolu ve durum (basarili/basarisiz, eksik modul adi)
acikca yazar; check_agent_board baslandi/bitti/basarisiz durumunu, klasoru ve
eksik modulu gosterir, job_id ile tek gorevin ayrintisi sorgulanabilir.

Eskiden: dev_agent'in bircok donusu (kota, "dosya yazilamadi", duzeltme
sirasinda kota) klasor yolunu hic yazmiyordu; pano yalnizca "completed" /
"failed" kelimesini gosteriyor, sonucu 500 karakterde kesiyordu.
AG/LLM CAGRISI YOK: _build_project taklit edilir.
"""
from __future__ import annotations

import threading
import time

import pytest

import jarvis.actions.agent_board as board
import jarvis.actions.dev_agent as da

REQUEST = "CSV dosyasındaki satırları sayan bir araç yaz ve sonucu rapor.txt dosyasına kaydet"


@pytest.fixture
def stub_build(monkeypatch, tmp_path):
    monkeypatch.setattr(da, "_pending_dev_agent", {})
    monkeypatch.setattr(da, "_last_user_turn_at", None)
    monkeypatch.setattr(da, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(board, "DB_PATH", tmp_path / "agent_board.db")
    state = {"result": "", "dir": "sayac"}

    def fake_build(**kw):
        outcome = kw.get("outcome")
        if outcome is not None and state["dir"]:
            outcome["project_dir"] = da.PROJECTS_DIR / state["dir"]
        return state["result"]

    monkeypatch.setattr(da, "_build_project", fake_build)
    return state


def _confirmed_run(params: dict) -> str:
    code = da.dev_agent(dict(params)).split("confirm_code='")[1].split("'")[0]
    da.note_user_turn("evet", now=time.monotonic() + 5)
    return da.dev_agent({**params, "confirm_code": code})


def test_success_result_names_folder_and_status(stub_build):
    stub_build["result"] = "Project 'sayac' is working, sir. Built in 1 attempt."
    out = _confirmed_run({"description": REQUEST})
    assert "Durum: BAŞARILI" in out
    assert f"Proje klasörü: {da.PROJECTS_DIR / 'sayac'}" in out
    assert "Eksik modül" not in out


def test_failure_without_path_in_message_still_names_folder(stub_build):
    stub_build["result"] = "Rate limit reached during fix. Project saved, check it manually in VSCode."
    out = _confirmed_run({"description": REQUEST})
    assert "Durum: BAŞARISIZ" in out
    assert f"Proje klasörü: {da.PROJECTS_DIR / 'sayac'}" in out


def test_failure_lists_missing_modules(stub_build):
    stub_build["result"] = (
        "I couldn't fully fix 'sayac' after 5 attempts, sir.\n\nLast error:\n"
        "ModuleNotFoundError: No module named 'pandas'\n"
        "ModuleNotFoundError: No module named 'openpyxl.styles'\nNo module named 'pandas'")
    out = _confirmed_run({"description": REQUEST})
    assert "Durum: BAŞARISIZ" in out
    assert "Eksik modül: pandas, openpyxl.styles" in out


def test_build_that_never_created_a_folder_says_so(stub_build):
    stub_build["result"], stub_build["dir"] = "Planning failed: bad json", ""
    out = _confirmed_run({"description": REQUEST})
    assert "Durum: BAŞARISIZ" in out and "Proje klasörü: oluşturulmadı" in out


def test_preview_and_missing_info_answers_get_no_result_block(stub_build):
    assert "Durum:" not in da.dev_agent({"description": REQUEST})
    assert "Durum:" not in da.dev_agent({"description": "[Hedef URL] sitesini kazı"})


def _wait_for_board(job_id: str, statuses: tuple[str, ...]) -> str:
    for _ in range(200):
        text = board.check_agent_board({"job_id": job_id})
        if any(s in text for s in statuses):
            return text
        time.sleep(0.02)
    raise AssertionError(board.check_agent_board({"job_id": job_id}))


def _start(params: dict) -> str:
    code = board.start_parallel_task(dict(params)).split("confirm_code='")[1].split("'")[0]
    da.note_user_turn("evet", now=time.monotonic() + 5)
    started = board.start_parallel_task({**params, "confirm_code": code})
    return started.split("kimlik: ")[1].split(")")[0]


def test_board_shows_started_then_finished_with_folder(stub_build):
    release = threading.Event()
    stub_build["result"] = "Project 'sayac' is working, sir."
    original = da._build_project

    def slow_build(**kw):
        release.wait(5)
        return original(**kw)

    da._build_project = slow_build
    try:
        job = _start({"description": REQUEST})
        running = _wait_for_board(job, ("başladı",))
        assert "başladı" in running and "bitti" not in running
    finally:
        release.set()
        da._build_project = original
    done = _wait_for_board(job, ("bitti", "başarısız"))
    assert "bitti — başarılı" in done
    assert f"Proje klasörü: {da.PROJECTS_DIR / 'sayac'}" in done
    summary = board.check_agent_board()
    assert job in summary and "bitti — başarılı" in summary and "sayac" in summary


def test_board_shows_failure_and_missing_module_even_for_long_output(stub_build):
    stub_build["result"] = ("I couldn't fully fix 'sayac' after 5 attempts, sir.\n\nLast error:\n"
                            + "x" * 2000 + "\nModuleNotFoundError: No module named 'requests'")
    job = _start({"description": REQUEST})
    done = _wait_for_board(job, ("bitti", "başarısız"))
    assert "başarısız" in done and "Eksik modül: requests" in done
    assert f"Proje klasörü: {da.PROJECTS_DIR / 'sayac'}" in done


def test_board_reports_crash_as_failed(stub_build, monkeypatch):
    def boom(**kw):
        raise RuntimeError("model çöktü")
    monkeypatch.setattr(da, "_build_project", boom)
    job = _start({"description": REQUEST})
    done = _wait_for_board(job, ("başarısız",))
    assert "RuntimeError: model çöktü" in done


def test_board_unknown_job_id(stub_build):
    assert "bulunamadı" in board.check_agent_board({"job_id": "yok12345"})
