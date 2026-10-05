"""process_lock.py — PID-BASED DEADLOCK KORUMASI.

SORUN (2026-10-05):
- core/task_manager.py sadece threading.RLock kullanıyor
- Eğer process crash ederken dosya kitliyse → next worker dead-lock yer
- ExperimentTaskManager JSON partial-write → JSONDecodeError → silent data loss

ÇÖZÜM: Process-level lock
1. Lock dosyası (queues_lock.json) PID + heartbeat içerir
2. Dead process kontrolü: stale lock'ı 30 saniye sonra silebilir
3. Atomic lock operations
"""
from __future__ import annotations

import json
import os
import tempfile
import logging
from pathlib import Path
from dataclasses import dataclass, asdict

import sys
from jarvis.paths import data_dir


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return data_dir()

BASE_DIR = _get_base_dir()
QUEUES_LOCK_PATH = BASE_DIR / "config" / "queues_lock.json"


@dataclass
class ProcessLock:
    """Lock bilgisi."""
    pid: int                 # Process ID holding the lock
    holder_name: str        # WHO holds it (for debugging)
    acquired_at: float      # When was it locked?
    heartbeat_at: float     # Last heartbeat (alive check)
    
    def is_stale(self, now: float, stale_after_seconds: int = 30) -> bool:
        """Lock'u tutan process ölü mü?"""
        return (now - self.heartbeat_at) > stale_after_seconds
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @staticmethod
    def from_dict(d: dict) -> ProcessLock:
        return ProcessLock(**d)


class ProcessLockManager:
    """Process-level file locking.
    
    TASARIMI:
    1. Lock dosyası single JSON entry: {pid, holder_name, acquired_at, heartbeat_at}
    2. Lock almak: CAS (compare-and-swap) operation
    3. Heartbeat: holder process lock'u tutuyor mu? (alive check)
    4. Release: atomic delete
    5. Stale detection: 30 sn'den eski → force release
    """
    
    def __init__(self, lock_path: Path = QUEUES_LOCK_PATH, stale_timeout: int = 30):
        self.lock_path = lock_path
        self.stale_timeout = stale_timeout
        self.pid = os.getpid()
        from jarvis.core.p0_critical_fixes import OSLevelProcessLock
        self._os_lock = OSLevelProcessLock(lock_file=lock_path)
        self._held = False
        self._holder_name = "unknown"
        self._logger = self._make_logger()
    
    def _make_logger(self) -> logging.Logger:
        logger = logging.getLogger("jarvis.process_lock")
        if not logger.handlers:
            try:
                log_dir = self.lock_path.parent.parent / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                handler = logging.FileHandler(log_dir / "process_lock.log", encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
                logger.addHandler(handler)
                logger.setLevel(logging.INFO)
            except Exception:
                pass
        return logger
    
    def _load_lock(self) -> ProcessLock | None:
        """Lock dosyasını oku."""
        try:
            if self.lock_path.is_file():
                data = json.loads(self.lock_path.read_text(encoding="utf-8"))
                return ProcessLock.from_dict(data)
        except Exception as e:
            self._logger.warning(f"Lock dosyası okuma hatası: {e}")
        return None
    
    def _save_lock(self, lock: ProcessLock) -> None:
        """Lock dosyasını kaydet (atomic)."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=self.lock_path.parent, delete=False, encoding="utf-8", suffix=".tmp"
        ) as tmp:
            json.dump(lock.to_dict(), tmp, ensure_ascii=False)
            temp_name = tmp.name
        Path(temp_name).replace(self.lock_path)
    
    def _delete_lock(self) -> None:
        """Lock dosyasını sil."""
        try:
            if self.lock_path.is_file():
                self.lock_path.unlink()
        except Exception as e:
            self._logger.warning(f"Lock silme hatası: {e}")
    
    def acquire(self, holder_name: str = "unknown", timeout: float = 5.0) -> bool:
        """Lock'u al (blocking).
        
        Args:
            holder_name: Who is holding it? (for logging)
            timeout: Kaç saniye bekleme?
            
        Returns:
            True if acquired, False if timeout
        """
        if self._held:
            return True
        acquired = self._os_lock.acquire(timeout=max(0.0, timeout))
        if acquired:
            self._held = True
            self._holder_name = holder_name
            self._logger.info(f"OS-level lock alındı: pid={self.pid}, holder={holder_name}")
            return True
        self._logger.error(f"Lock timeout: {holder_name} ({timeout}s)")
        return False
    
    def heartbeat(self) -> None:
        """Lock'u tutan process "ben hala hayattayım" diyor."""
        # OS lock process sonlandığında otomatik bırakılır; heartbeat dosyası
        # artık sahiplik mekanizması değildir ve yarış penceresi oluşturmaz.
        if self._held:
            return
    
    def release(self) -> None:
        """Lock'u bırak."""
        if not self._held:
            self._logger.warning("Lock'u serbest bırakamadık: bu manager lock sahibi değil")
            return
        if self._os_lock.release():
            self._held = False
            self._logger.info(f"Lock serbest bırakıldı: pid={self.pid}")
    
    def is_locked(self) -> bool:
        """Lock var mı?"""
        if self._held:
            return True
        from jarvis.core.p0_critical_fixes import OSLevelProcessLock
        probe = OSLevelProcessLock(lock_file=self.lock_path)
        if probe.acquire(timeout=0.0):
            probe.release()
            return False
        return True
    
    def force_release_stale(self) -> bool:
        """Stale lock'ları zorla serbest bırak."""
        # OS-level locks are released by the kernel when the owner dies;
        # deleting the lock file would be unsafe while another process owns it.
        return False


# Singleton
_lock_manager: ProcessLockManager | None = None


def get_process_lock() -> ProcessLockManager:
    """Global lock manager instance."""
    global _lock_manager
    if _lock_manager is None:
        _lock_manager = ProcessLockManager()
    return _lock_manager


if __name__ == "__main__":
    # Test
    mgr = ProcessLockManager()
    
    print("Test 1: Lock acquire")
    success = mgr.acquire("test_holder")
    print(f"  Acquired: {success}")
    assert success, "Failed to acquire lock"
    
    print("Test 2: Lock is_locked")
    assert mgr.is_locked(), "Lock should be acquired"
    
    print("Test 3: Heartbeat")
    mgr.heartbeat()
    print("  Heartbeat sent")
    
    print("Test 4: Release")
    mgr.release()
    assert not mgr.is_locked(), "Lock should be released"
    
    print("✅ ProcessLockManager tests passed")
