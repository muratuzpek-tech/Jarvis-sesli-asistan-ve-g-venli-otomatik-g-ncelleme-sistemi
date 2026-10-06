"""tests/test_agent_board_list_jobs.py — panoya yapisal (sozluk) is listesi

check_agent_board yalnizca bicimlendirilmis metin donduruyordu; pano icin
list_jobs() sozluk listesi verir (kisa sqlite zaman asimi, _lock altinda).
check_agent_board artik onun ustune kurulu ve CIKTISI DEGISMEZ: metin testleri
eski kodda da yesil olmali (davranis korumasi).

Veritabani tmp_path altinda; gercek MuratJARVIS verisine dokunulmaz.
"""
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, 'src')

from jarvis.actions import agent_board as ab


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "agent_board.db"
    monkeypatch.setattr(ab, "DB_PATH", path)
    return path


ROWS = [
    ("aaaa1111", "hesap makinesi yaz", "completed",
     "--- SONUÇ ---\nDurum: BAŞARILI\nProje klasörü: /p/hesap\n\ngovde metni",
     "2026-10-06T10:00:00", "2026-10-06T10:05:30"),
    ("bbbb2222", "not defteri yaz", "failed",
     "Durum: BAŞARISIZ\nEksik modül: notlar\nHata: ValueError: bozuk",
     "2026-10-06T11:00:00", "2026-10-06T11:02:00"),
    ("cccc3333", "oyun yaz", "running", None, "2026-10-06T12:00:00", None),
]


def _insert(rows=ROWS) -> None:
    conn = ab._connect()
    try:
        with conn:
            conn.executemany(
                "INSERT INTO jobs (id, description, status, result, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", rows)
    finally:
        conn.close()


# ── metin cikti korumasi (eski kodda da yesil) ──

def test_check_agent_board_summary_text_unchanged(db):
    _insert()
    assert ab.check_agent_board({}) == (
        "Ajan panosu:\n"
        "🔄 [cccc3333] oyun yaz — başladı, çalışıyor (başladı 12:00:00)\n"
        "❌ [bbbb2222] not defteri yaz — bitti — başarısız (başladı 11:00:00, bitti 11:02:00)\n"
        "    Eksik modül: notlar\n"
        "    Hata: ValueError: bozuk\n"
        "✅ [aaaa1111] hesap makinesi yaz — bitti — başarılı (başladı 10:00:00, bitti 10:05:30)\n"
        "    Proje klasörü: /p/hesap"
    )


def test_check_agent_board_detail_and_empty_text_unchanged(db):
    assert ab.check_agent_board({}) == "Panoda hiç görev yok."
    _insert()
    assert ab.check_agent_board({"job_id": "aaaa1111"}) == (
        "✅ [aaaa1111] hesap makinesi yaz — bitti — başarılı (başladı 10:00:00, bitti 10:05:30)\n"
        + ROWS[0][3]
    )
    assert ab.check_agent_board({"job_id": "yok"}) == "Panoda 'yok' kimlikli görev bulunamadı."


# ── list_jobs ──

def test_list_jobs_returns_plain_dicts_newest_first(db):
    _insert()
    jobs = ab.list_jobs()
    assert [j["id"] for j in jobs] == ["cccc3333", "bbbb2222", "aaaa1111"]
    assert all(type(j) is dict for j in jobs)
    assert set(jobs[0]) == {"id", "description", "status", "result", "started_at", "finished_at"}
    assert jobs[0]["finished_at"] is None
    assert ab.list_jobs(job_id="bbbb2222")[0]["status"] == "failed"
    assert ab.list_jobs(job_id="yok") == []


def test_list_jobs_limit_and_empty(db):
    assert ab.list_jobs() == []
    _insert([(f"id{n:06d}", f"is {n}", "pending", None, f"2026-10-06T10:{n:02d}:00", None)
             for n in range(15)])
    assert len(ab.list_jobs()) == 10
    assert len(ab.list_jobs(limit=3)) == 3


def test_list_jobs_gives_up_quickly_when_lock_is_held(db):
    _insert()
    ab._lock.acquire()
    try:
        with pytest.raises(sqlite3.OperationalError):
            ab.list_jobs(timeout=0.2)
    finally:
        ab._lock.release()


def test_list_jobs_concurrent_with_writes(db):
    _insert()
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer():
        try:
            n = 0
            while not stop.is_set():
                ab._update_job("cccc3333", status="running", result=f"tur {n}")
                n += 1
        except BaseException as e:   # pragma: no cover
            errors.append(e)

    def reader():
        try:
            while not stop.is_set():
                assert len(ab.list_jobs(timeout=5)) == 3
        except BaseException as e:   # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    stop.wait(0.5)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert not errors, errors
