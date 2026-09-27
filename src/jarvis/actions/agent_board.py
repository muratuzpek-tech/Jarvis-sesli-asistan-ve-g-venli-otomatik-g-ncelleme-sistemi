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


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=20.0)
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


def _run_job_in_background(job_id: str, description: str, language: str, project_name: str) -> None:
    """Ayri bir thread'de calisir - dev_agent'in kendi (senkron, uzun surebilen)
    calisma dongusunu bloklamadan yurutur."""
    _update_job(job_id, status="running")
    try:
        from jarvis.actions.dev_agent import dev_agent

        result = dev_agent(parameters={
            "description": description,
            "language": language,
            "project_name": project_name,
        })
        res_str = str(result) if result is not None else ""
        _update_job(
            job_id,
            status="completed",
            result=res_str[:500],
            finished_at=datetime.now().isoformat(),
        )
    except Exception as e:
        logger.exception("Error executing background job %s: %s", job_id, e)
        _update_job(
            job_id,
            status="failed",
            result=f"{type(e).__name__}: {e}",
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
        args=(job_id, description, language, project_name),
        daemon=True,
        name=f"AgentBoard-{job_id}",
    )
    thread.start()

    _log_player(player, f"[AgentBoard] Yeni görev başlatıldı: {job_id} — {description}")
    return f"Görev arka planda başlatıldı (kimlik: {job_id}). Diğer işlerine devam edebilirsin, hazır olunca panoyu kontrol et."


def check_agent_board(parameters: dict[str, Any] | None = None, player: Any = None) -> str:
    """Tum gorevlerin (calisan/tamamlanan/hatali) ozetini dondurur."""
    with _lock:
        try:
            conn = _connect()
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, description, status, result FROM jobs ORDER BY started_at DESC LIMIT 10"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.error("Failed to query agent board: %s", e)
            return f"Pano veritabanı okunamadı: {e}"

    if not rows:
        return "Panoda hiç görev yok."

    lines: list[str] = []
    for r in rows:
        status = r["status"]
        icon = _STATUS_ICONS.get(status, "•")
        desc = (r["description"] or "")[:60]
        lines.append(f"{icon} [{r['id']}] {desc} — {status}")

    return "Ajan panosu:\n" + "\n".join(lines)
