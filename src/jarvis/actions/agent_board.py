"""AgentSpace'ten ilham alinan, basit bir 'paralel ajan panosu'.

Birden fazla dev_agent gorevini AYNI ANDA (arka plan thread'lerinde)
baslatmayi ve durumlarini (bekliyor/calisiyor/tamamlandi/hata) tek bir
"pano" komutuyla gormeyi saglar. Tam AgentSpace degil (mobil yok, bulut
yok, gorsel ofis yok), ama ayni temel fikir: bircok isi ayni anda
baslatip, ilerlemeyi tek yerden takip etmek.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.paths import memory_dir

logger = logging.getLogger(__name__)

_ALLOWED_JOB_COLUMNS = frozenset({"description", "status", "result", "started_at", "finished_at"})
_STATUS_ICONS = {
    "pending": "⏳",
    "running": "🔄",
    "completed": "✅",
    "failed": "❌",
}
_STATUS_LABELS = {
    "pending": "bekliyor",
    "running": "başladı, çalışıyor",
    "completed": "bitti — başarılı",
    "failed": "bitti — başarısız",
}


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


DB_PATH = memory_dir() / "agent_board.db"
_lock = threading.Lock()


def _log_player(player: Any, message: str) -> None:
    if player is not None:
        try:
            log_fn = getattr(player, "write_log", None)
            if callable(log_fn):
                log_fn(message)
        except Exception as exc:
            logger.debug("Failed to log to player: %s", exc)


def _connect(timeout: float = 20.0) -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT,
            started_at TEXT NOT NULL,
            finished_at TEXT
        )
    """)
    return conn


def _update_job(job_id: str, **fields: Any) -> None:
    valid_fields = {k: v for k, v in fields.items() if k in _ALLOWED_JOB_COLUMNS}
    if not valid_fields:
        return

    with _lock:
        try:
            conn = _connect()
            try:
                with conn:
                    column_sql = {
                        "description": '"description" = ?',
                        "status": '"status" = ?',
                        "result": '"result" = ?',
                        "started_at": '"started_at" = ?',
                        "finished_at": '"finished_at" = ?',
                    }
                    set_clause = ", ".join(column_sql[k] for k in valid_fields)
                    params = list(valid_fields.values()) + [job_id]
                    conn.execute("UPDATE jobs SET " + set_clause + " WHERE id = ?", params)  # nosec B608: columns come only from column_sql allowlist.
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.error("Failed to update job %s: %s", job_id, exc)


def _run_job_in_background(job_id: str, description: str, language: str, project_name: str,
                           confirm_code: str = "") -> None:
    """Ayri bir thread'de calisir - dev_agent'in kendi (senkron, uzun surebilen)
    calisma dongusunu bloklamadan yurutur."""
    _update_job(job_id, status="running")
    try:
        from jarvis.actions.dev_agent import _build_succeeded, dev_agent, split_result_summary

        result = dev_agent(parameters={
            "description": description,
            "language": language,
            "project_name": project_name,
            "confirm_code": confirm_code,
        })
        res_str = str(result) if result is not None else ""
        # DUZELTME (2026-09-28): eskiden onay kodu hic verilmedigi icin dev_agent
        # yalnizca "ONAY GEREKLİ" donuyor, proje HIC olusmuyor ama is yine de
        # "completed" gorunuyordu. Artik yalnizca gercek basari "completed".
        summary, body = split_result_summary(res_str)
        succeeded = "Durum: BAŞARILI" in summary if summary else _build_succeeded(res_str)
        # Sonuc blogu (durum, klasor, eksik modul) kesilmesin diye basta saklanir.
        stored = f"{summary}\n\n{body[:500]}" if summary else res_str[:500]
        _update_job(
            job_id,
            status="completed" if succeeded else "failed",
            result=stored,
            finished_at=datetime.now().isoformat(),
        )
    except Exception as e:
        logger.exception("Error executing background job %s: %s", job_id, e)
        _update_job(
            job_id,
            status="failed",
            result=f"Durum: BAŞARISIZ\nHata: {type(e).__name__}: {str(e)[:300]}",
            finished_at=datetime.now().isoformat(),
        )


def start_parallel_task(parameters: dict[str, Any] | None = None, player: Any = None) -> str:
    """Yeni bir dev_agent gorevini ARKA PLANDA baslatir, hemen doner
    (bekletmez). Durumu daha sonra check_agent_board ile sorgulanir."""
    p = parameters or {}
    description = str(p.get("description", "") or "").strip()
    if not description:
        return "Görev açıklaması gerekli."
    language = str(p.get("language", "python") or "python").strip()
    project_name = str(p.get("project_name", "") or "").strip()
    confirm_code = str(p.get("confirm_code", "") or "").strip()

    from jarvis.actions.dev_agent import confirmation_problem, dev_agent

    # Arka plan gorevi de ayni onay kapisindan gecer: once onizleme + kod,
    # kullanici acikca onaylayinca ayni kodla ikinci cagri isi baslatir.
    if not confirm_code:
        preview = dev_agent(parameters={"description": description, "language": language,
                                        "project_name": project_name})
        if "confirm_code='" in preview:
            preview += (" ARKA PLAN İÇİN: onaydan sonra dev_agent yerine start_parallel_task'ı aynı "
                        "description/language/project_name ve bu confirm_code ile çağır.")
        return preview
    problem = confirmation_problem(confirm_code)
    if problem:
        return problem

    job_id = uuid.uuid4().hex[:8]
    now_iso = datetime.now().isoformat()

    with _lock:
        try:
            conn = _connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO jobs (id, description, status, started_at) VALUES (?, ?, 'pending', ?)",
                        (job_id, description, now_iso),
                    )
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.error("Failed to insert job into database: %s", exc)
            return f"Görev veritabanına kaydedilemedi: {exc}"

    thread = threading.Thread(
        target=_run_job_in_background,
        args=(job_id, description, language, project_name, confirm_code),
        daemon=True,
        name=f"AgentBoard-{job_id}",
    )
    thread.start()

    _log_player(player, f"[AgentBoard] Yeni görev başlatıldı: {job_id} — {description}")
    return (f"Görev arka planda başlatıldı (kimlik: {job_id}). Diğer işlerine devam edebilirsin; "
            f"durum, proje klasörü ve sonuç için check_agent_board(job_id='{job_id}') ile panoyu kontrol et.")


def _clock(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M:%S") if iso else ""
    except ValueError:
        return ""


def _job_line(r: dict[str, Any]) -> str:
    status = r["status"]
    icon = _STATUS_ICONS.get(status, "•")
    desc = (r["description"] or "")[:60]
    times = f"başladı {_clock(r['started_at'])}"
    if r["finished_at"]:
        times += f", bitti {_clock(r['finished_at'])}"
    return f"{icon} [{r['id']}] {desc} — {_STATUS_LABELS.get(status, status)} ({times})"


def _summary_lines(result: str | None) -> list[str]:
    """Saklanan sonuctan durum disindaki ozet satirlari (klasor, eksik modul, hata)."""
    keep = ("Proje klasörü:", "Eksik modül:", "Hata:")
    return [line for line in (result or "").splitlines() if line.startswith(keep)]


_JOB_COLUMNS = "id, description, status, result, started_at, finished_at"


def list_jobs(job_id: str = "", limit: int = 10, timeout: float | None = 2.0) -> list[dict[str, Any]]:
    """Gorevleri (en yeni once) duz sozluk listesi olarak dondurur; job_id
    verilirse yalnizca o gorev. timeout: _lock ve sqlite icin bekleme suresi
    (saniye); None = eskisi gibi sinirsiz kilit + 20 sn sqlite. Kilit zamaninda
    alinamazsa sqlite3.OperationalError firlatir (pano Jarvis'i bekletmesin)."""
    acquired = _lock.acquire() if timeout is None else _lock.acquire(timeout=timeout)
    if not acquired:
        raise sqlite3.OperationalError("pano kilidi meşgul")
    try:
        conn = _connect(20.0 if timeout is None else timeout)
        try:
            conn.row_factory = sqlite3.Row
            if job_id:
                rows = conn.execute(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,)).fetchall()  # nosec B608: sabit sutun listesi.
            else:
                rows = conn.execute(
                    f"SELECT {_JOB_COLUMNS} FROM jobs ORDER BY started_at DESC LIMIT ?",  # nosec B608: sabit sutun listesi.
                    (max(0, int(limit)),),
                ).fetchall()
        finally:
            conn.close()
    finally:
        _lock.release()
    return [dict(r) for r in rows]


def check_agent_board(parameters: dict[str, Any] | None = None, player: Any = None) -> str:
    """Tum gorevlerin (bekliyor/basladi/bitti/basarisiz) ozetini dondurur;
    job_id verilirse o gorevin ayrintili sonucunu."""
    job_id = str((parameters or {}).get("job_id", "") or "").strip()
    try:
        rows = list_jobs(job_id=job_id, timeout=None)
    except sqlite3.Error as e:
        logger.error("Failed to query agent board: %s", e)
        return f"Pano veritabanı okunamadı: {e}"

    if job_id:
        if not rows:
            return f"Panoda '{job_id}' kimlikli görev bulunamadı."
        r = rows[0]
        detail = _job_line(r)
        if r["result"]:
            detail += "\n" + r["result"]
        return detail

    if not rows:
        return "Panoda hiç görev yok."

    lines: list[str] = []
    for r in rows:
        lines.append(_job_line(r))
        lines.extend(f"    {line}" for line in _summary_lines(r["result"]))

    return "Ajan panosu:\n" + "\n".join(lines)
