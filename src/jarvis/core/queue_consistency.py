"""queue_consistency.py — KUYRUK BÜTÜNLÜĞÜ KONTROLÜ.

SORUN (2026-10-05):
- task_manager.py payload integrity warnings → logs/task_manager.log
- AMA bu loglar ASLA OKUNMUYOR ve kullanıcıya RAPOR EDİLMİYOR
- ExperimentTaskManager JSON bozulması → JSONDecodeError → sessiz veri kaybı
- Dev_agent/brain_team onayları → task yazarken payload kayıyor

ÇÖZÜM: Queue Consistency Checker
1. TaskManager.update() → consistency check + detailed logging
2. ExperimentTaskManager._load() → JSON corruption detection
3. Checkpoint logging: Her task state değişiminde snapshot
4. Anomaly detection: Payload loss → alert + log
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import sys

from jarvis.paths import data_dir

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return data_dir()

BASE_DIR = _get_base_dir()
CONSISTENCY_LOG_PATH = BASE_DIR / "logs" / "queue_consistency.log"


@dataclass
class TaskSnapshot:
    """Task state snapshot — change tracking için."""
    task_id: str
    timestamp: float
    status: str
    payload_keys: set  # Hangi anahtar varsa
    retry_count: int
    error: str | None
    
    def __hash__(self):
        return hash(self.task_id)
    
    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "status": self.status,
            "payload_keys": sorted(self.payload_keys),
            "retry_count": self.retry_count,
            "error": self.error,
        }


class QueueConsistencyChecker:
    """Kuyruk bütünlüğü kontrolü.
    
    TASARIMI:
    1. Checkpoint logging: Her task update'ında snapshot
    2. Integrity checks: payload keys loss detection
    3. JSON corruption handling: ExperimentTaskManager için
    4. Alert system: Anomaliler bulunca detaylı log + exception
    """
    
    def __init__(self, log_path: Path = CONSISTENCY_LOG_PATH):
        self.log_path = log_path
        self._logger = self._make_logger()
        self._snapshots: dict[str, TaskSnapshot] = {}  # Last known state per task_id
    
    def _make_logger(self) -> logging.Logger:
        logger = logging.getLogger("jarvis.queue_consistency")
        if not logger.handlers:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                handler = logging.FileHandler(self.log_path, encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
                logger.addHandler(handler)
                logger.setLevel(logging.INFO)
            except Exception:
                pass
        return logger
    
    def checkpoint_task(self, task_id: str, task: dict, event: str = "update") -> TaskSnapshot:
        """Task state snapshot al (change tracking).
        
        Args:
            task_id: Task identifier
            task: Task dict (full content)
            event: What happened? ("create", "update", "confirm", "reject", etc.)
            
        Returns:
            TaskSnapshot
        """
        import time
        snapshot = TaskSnapshot(
            task_id=task_id,
            timestamp=time.monotonic(),
            status=task.get("status", "unknown"),
            payload_keys=set(task.get("payload", {}).keys()) if isinstance(task.get("payload"), dict) else set(),
            retry_count=task.get("retry_count", 0),
            error=task.get("error"),
        )
        
        # Önceki state ile karşılaştır
        old_snapshot = self._snapshots.get(task_id)
        if old_snapshot:
            lost_keys = old_snapshot.payload_keys - snapshot.payload_keys
            gained_keys = snapshot.payload_keys - old_snapshot.payload_keys
            
            if lost_keys:
                self._logger.warning(
                    f"[INTEGRITY_LOSS] Task '{task_id}' ({task.get('name', '')[:60]!r}): "
                    f"payload anahtar kayıp → {sorted(lost_keys)} silindi. "
                    f"Event: {event}, status: {old_snapshot.status} → {snapshot.status}"
                )
            
            if gained_keys:
                self._logger.debug(
                    f"[PAYLOAD_GAIN] Task '{task_id}': yeni anahtarlar → {sorted(gained_keys)}"
                )
        
        self._snapshots[task_id] = snapshot
        self._logger.debug(
            f"[CHECKPOINT] Task '{task_id}' ({event}): "
            f"status={snapshot.status}, payload_keys={sorted(snapshot.payload_keys)}, "
            f"retry={snapshot.retry_count}"
        )
        
        return snapshot
    
    def check_payload_integrity(
        self, 
        task_id: str, 
        old_task: dict, 
        new_task: dict,
        allowed_key_loss: list[str] | None = None
    ) -> bool:
        """Payload integrity kontrolü.
        
        Args:
            task_id: Task identifier
            old_task: Previous state
            new_task: New state
            allowed_key_loss: Hangi keys kaybetmesine izin var? (None = hiçbiri)
            
        Returns:
            True if OK, False if integrity error
        """
        old_keys = set(old_task.get("payload", {}).keys()) if isinstance(old_task.get("payload"), dict) else set()
        new_keys = set(new_task.get("payload", {}).keys()) if isinstance(new_task.get("payload"), dict) else set()
        
        lost_keys = old_keys - new_keys
        allowed = set(allowed_key_loss or [])
        unexpected_loss = lost_keys - allowed
        
        if unexpected_loss:
            self._logger.error(
                f"[INTEGRITY_ERROR] Task '{task_id}' ({old_task.get('name', '')[:60]!r}): "
                f"Beklenmedik payload kaybı → {sorted(unexpected_loss)}. "
                f"Old payload keys: {sorted(old_keys)}, "
                f"New payload keys: {sorted(new_keys)}. "
                f"Status: {old_task.get('status')} → {new_task.get('status')}"
            )
            return False
        
        return True
    
    def detect_json_corruption(self, file_path: Path) -> tuple[bool, str | None]:
        """JSON corruption detection.
        
        Args:
            file_path: Path to JSON file
            
        Returns:
            (is_valid, error_message)
        """
        try:
            if not file_path.is_file():
                return True, None
            
            content = file_path.read_text(encoding="utf-8")
            if not content.strip():
                return False, "File is empty"
            
            data = json.loads(content)
            
            # Tip kontrolü
            if isinstance(data, dict):
                return True, None
            elif isinstance(data, list):
                return True, None
            else:
                return False, f"Invalid JSON root type: {type(data)}"
        
        except json.JSONDecodeError as e:
            msg = f"JSON decode error at line {e.lineno}, col {e.colno}: {e.msg}"
            self._logger.error(f"[JSON_CORRUPTION] {file_path}: {msg}")
            return False, msg
        except Exception as e:
            msg = f"Unexpected error: {type(e).__name__}: {e}"
            self._logger.error(f"[JSON_CORRUPTION] {file_path}: {msg}")
            return False, msg
    
    def safe_load_json(self, file_path: Path, default: Any = None) -> Any:
        """JSON güvenli yükleme (corruption'a karşı).
        
        Args:
            file_path: JSON file path
            default: Yükleme başarısızsa döndürülecek değer
            
        Returns:
            Parsed JSON, or default if error
        """
        is_valid, error_msg = self.detect_json_corruption(file_path)
        
        if not is_valid:
            self._logger.warning(
                f"[SAFE_LOAD] {file_path} corrupted, returning default. Error: {error_msg}"
            )
            return default
        
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except Exception as e:
            self._logger.error(f"[SAFE_LOAD] Failed to load {file_path}: {e}")
            return default
    
    def log_task_transition(
        self,
        task_id: str,
        task_name: str,
        old_status: str,
        new_status: str,
        reason: str = ""
    ) -> None:
        """Task state geçişini log et.
        
        Args:
            task_id: Task identifier
            task_name: Task name (for context)
            old_status: Previous status
            new_status: New status
            reason: Geçiş nedeni
        """
        self._logger.info(
            f"[TRANSITION] Task '{task_id}' ({task_name[:60]!r}): "
            f"{old_status} → {new_status}" +
            (f" (reason: {reason})" if reason else "")
        )
    
    def alert_anomaly(self, anomaly_type: str, details: str) -> None:
        """Kuyruk anomalisi uyarısı.
        
        Args:
            anomaly_type: Type of anomaly ("payload_loss", "status_conflict", etc.)
            details: Anomaly details
        """
        self._logger.error(
            f"[ANOMALY] {anomaly_type}: {details}"
        )
    
    def get_integrity_report(self, max_issues: int = 50) -> str:
        """Bütünlük raporu oluştur.
        
        Returns:
            Human-readable report
        """
        report_lines = [
            "=== QUEUE CONSISTENCY REPORT ===",
            f"Generated at: {datetime.now().isoformat()}",
            f"Tracked tasks: {len(self._snapshots)}",
            "",
            "Recent snapshots:",
        ]
        
        for i, (task_id, snap) in enumerate(list(self._snapshots.items())[-max_issues:]):
            report_lines.append(
                f"  [{i+1}] {task_id}: status={snap.status}, "
                f"payload_keys={sorted(snap.payload_keys)}, retry={snap.retry_count}"
            )
        
        return "\n".join(report_lines)


# Singleton
_checker: QueueConsistencyChecker | None = None


def get_consistency_checker() -> QueueConsistencyChecker:
    """Global consistency checker instance."""
    global _checker
    if _checker is None:
        _checker = QueueConsistencyChecker()
    return _checker


if __name__ == "__main__":
    # Test
    checker = QueueConsistencyChecker()
    
    print("Test 1: Checkpoint task")
    task1 = {
        "id": "task_001",
        "name": "Test Task",
        "status": "pending",
        "payload": {"plan": [...], "step_index": 0},
        "retry_count": 0,
        "error": None,
    }
    snap1 = checker.checkpoint_task("task_001", task1, event="create")
    print(f"  Snapshot: {snap1.to_dict()}")
    
    print("Test 2: Payload integrity check (OK)")
    task1_updated = {**task1, "status": "running"}
    is_ok = checker.check_payload_integrity("task_001", task1, task1_updated)
    print(f"  Integrity OK: {is_ok}")
    assert is_ok
    
    print("Test 3: Payload integrity check (LOSS)")
    task1_loss = {
        "id": "task_001",
        "name": "Test Task",
        "status": "completed",
        "payload": {},  # plan ve step_index kayboldu!
        "retry_count": 1,
        "error": None,
    }
    is_ok = checker.check_payload_integrity("task_001", task1, task1_loss)
    print(f"  Integrity check failed (expected): {not is_ok}")
    assert not is_ok
    
    print("Test 4: JSON corruption detection")
    from pathlib import Path
    import tempfile
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        f.write("{invalid json")
        tmp_path = Path(f.name)
    
    try:
        is_valid, error = checker.detect_json_corruption(tmp_path)
        print(f"  Corruption detected (expected): not is_valid={not is_valid}, error={error[:50]}...")
        assert not is_valid
    finally:
        tmp_path.unlink()
    
    print("Test 5: Report")
    report = checker.get_integrity_report()
    print(f"  Report:\n{report}")
    
    print("✅ QueueConsistencyChecker tests passed")
