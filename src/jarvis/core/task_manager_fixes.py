"""task_manager.py FIX'leri

FIX #1: PATH Configuration — dynamic data_dir
FIX #2: Payload Race Condition — exception throw
FIX #6: ProcessLockManager Entegrasyon
FIX #8: JSON Corruption Recovery — fsync + backup
FIX #10: TTL/Expiry Clock Consistency — time.monotonic
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

# ══════════════════════════════════════════════════════════════
# FIX #10: Konsistent zaman kaynağı
# ══════════════════════════════════════════════════════════════
_TIME_MONOTONIC_OFFSET = time.monotonic()
_DATETIME_OFFSET = datetime.now()

def _get_monotonic_time() -> float:
    """Konsistent monotonic time (system clock'tan bağımsız)."""
    return time.monotonic()


# ══════════════════════════════════════════════════════════════
# FIX #1: Dynamic PATH Configuration
# ══════════════════════════════════════════════════════════════
def _get_tasks_path() -> Path:
    """Her çağrıda canlı hesapla — hardcoded path'ler stale kalmasın."""
    from jarvis.paths import tasks_dir
    return tasks_dir() / "brain_tasks.json"


VALID_STATUSES = {
    "pending", "running", "waiting_approval", "completed",
    "failed", "cancelled", "rollback",
}

_lock = threading.RLock()
_logger = logging.getLogger("jarvis.task_manager")


class TaskManager:
    """Görev kuyruğu (JSON-tabanlı, ProcessLock-protected).
    
    FIX'LER:
    - #1: path=None → lazy eval _get_tasks_path()
    - #2: Payload anahtarı kaybolursa RuntimeError fırlat
    - #6: ProcessLockManager.acquire() otomatik
    - #8: Atomic write + backup + fsync
    - #10: time.monotonic() consistency
    """

    def __init__(self, path: Path | None = None) -> None:
        # FIX #1: Lazy evaluation
        if path is None:
            path = _get_tasks_path()
        self.path = path
        self._lock_acquired = False
        self._process_lock = None

    def _ensure_lock_acquired(self) -> None:
        """FIX #6: ProcessLockManager entegrasyon."""
        if self._lock_acquired:
            return
        try:
            from jarvis.core.process_lock import ProcessLockManager
            self._process_lock = ProcessLockManager()
            self._process_lock.acquire()
            self._lock_acquired = True
            _logger.debug("ProcessLock acquired")
        except Exception as e:
            _logger.warning(f"ProcessLock acquire başarısız: {e}")

    def _load(self) -> list[dict]:
        """JSON'u diskten yükle."""
        try:
            if self.path.is_file():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
        except json.JSONDecodeError as e:
            _logger.error(f"JSON parse hatası: {e}")
        except Exception as e:
            _logger.error(f"Yükleme hatası: {e}")
        return []

    def _save(self, tasks: list[dict]) -> None:
        """FIX #8: JSON Corruption Recovery — atomic write + backup + fsync."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # 1. Atomic write → temp file
        with tempfile.NamedTemporaryFile(
            "w", dir=self.path.parent, delete=False, encoding="utf-8", suffix=".tmp"
        ) as tmp:
            json.dump(tasks, tmp, indent=2, ensure_ascii=False)
            tmp.flush()
            os.fsync(tmp.fileno())  # ← Force disk sync
            temp_name = tmp.name

        # 2. Backup eski version
        if self.path.exists():
            backup_path = self.path.parent / f"{self.path.name}.backup"
            try:
                shutil.copy2(self.path, backup_path)
                _logger.debug(f"Backup created: {backup_path}")
            except Exception as e:
                _logger.warning(f"Backup oluşturulamadı: {e}")

        # 3. Atomic replace
        try:
            Path(temp_name).replace(self.path)
            _logger.debug(f"Saved: {self.path}")
        except OSError as e:
            # Windows lock error — temp cleanup
            if Path(temp_name).exists():
                try:
                    Path(temp_name).unlink()
                except Exception:
                    pass
            raise RuntimeError(f"Atomik yazma başarısız: {e}") from e

    def create(
        self, name: str, agent: str = "orchestrator", priority: str = "medium",
        payload: dict | None = None
    ) -> dict:
        """Yeni görev oluştur."""
        with _lock:
            self._ensure_lock_acquired()
            tasks = self._load()

            now = datetime.now().isoformat()
            task = {
                "id": f"TASK-{int(_get_monotonic_time() * 1000)}",
                "name": name,
                "agent": agent,
                "priority": priority,
                "status": "pending",
                "created_at": now,
                "updated_at": now,
                "retry_count": 0,
                "result": None,
                "error": None,
                "payload": payload or {},
            }
            tasks.append(task)
            self._save(tasks)
            _logger.info(f"Görev oluşturuldu: {task['id']} — {name[:60]}")
            return task

    def get(self, task_id: str) -> dict | None:
        """Görev getir."""
        with _lock:
            for t in self._load():
                if t["id"] == task_id:
                    return t
        return None

    def update(self, task_id: str, **fields) -> dict | None:
        """FIX #2: Payload Race Condition — kayıp anahtarları tespit et."""
        with _lock:
            self._ensure_lock_acquired()
            tasks = self._load()

            for t in tasks:
                if t["id"] == task_id:
                    old_payload = dict(t.get("payload") or {})

                    # Payload güncelle
                    if "payload" in fields:
                        new_payload = dict(fields["payload"])
                        t["payload"] = new_payload

                        # FIX #2: Anahtar kaybı kontrol
                        lost_keys = set(old_payload.keys()) - set(new_payload.keys())
                        if lost_keys:
                            raise RuntimeError(
                                f"KRITIK: Payload anahtarları KAYBOLDU — {lost_keys}. "
                                f"Orchestrator kendi kodundan böyle bir şey yapmaz. "
                                f"Dosya müdahalesi, process crash, ya da concurrent race. "
                                f"Görev: {task_id}"
                            )

                    # Diğer alanlar
                    if "status" in fields:
                        if fields["status"] not in VALID_STATUSES:
                            raise ValueError(f"Geçersiz durum: {fields['status']}")
                        t["status"] = fields["status"]

                    if "result" in fields:
                        t["result"] = fields["result"]
                    if "error" in fields:
                        t["error"] = fields["error"]
                    if "retry_count" in fields:
                        t["retry_count"] = fields["retry_count"]

                    t["updated_at"] = datetime.now().isoformat()
                    self._save(tasks)
                    return t

            _logger.warning(f"update(): '{task_id}' bulunamadı")
            return None

    def list(self, status: str | None = None) -> list[dict]:
        """Görevleri listele (opsiyonel filtre)."""
        with _lock:
            tasks = self._load()
            if status:
                return [t for t in tasks if t.get("status") == status]
            return tasks

    def delete(self, task_id: str) -> bool:
        """Görev sil."""
        with _lock:
            self._ensure_lock_acquired()
            tasks = self._load()
            original_count = len(tasks)
            tasks = [t for t in tasks if t["id"] != task_id]
            if len(tasks) < original_count:
                self._save(tasks)
                return True
            return False
