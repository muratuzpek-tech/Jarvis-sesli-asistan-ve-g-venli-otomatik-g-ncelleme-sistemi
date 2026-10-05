"""P0 KRITIK FIX'ler

FIX #13: Plugin Security Gate Integration (fail-closed)
FIX #14: ProcessLock Real OS-Level Lock
FIX #15: Virtual Brain State → User Data Dir
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

# ══════════════════════════════════════════════════════════════
# FIX #13: Plugin Security Gate Integration (FAIL-CLOSED)
# ══════════════════════════════════════════════════════════════

class PluginSecurityGate:
    """FIX #13: Plugin'leri merkezi security_gate'e bağla.
    
    SORUN: src/jarvis/core/agent.py'daki plugin çağrıları
    tool_gate/security_gate'ten geçmeden doğrudan handler(args)
    çalıştırılıyor → onay yok, audit yok.
    
    ÇÖZÜM: Plugin → ResolvedCall → authorize() → prepare() → execute()
    
    DEFAULT: Plugin'ler kapatılı (DENY)
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("jarvis.plugin_security_gate")
        self._lock = threading.RLock()
        # Allowlist: izin verilen plugin'ler
        self._allowlist: set[str] = set()

    def register_plugin(self, name: str, safety_level: str = "normal") -> None:
        """Plugin'i kaydet ve seviye belirle.
        
        Args:
            name: Plugin adı
            safety_level: "readonly", "normal", "dangerous", "destructive"
        """
        with self._lock:
            if safety_level in ("readonly", "normal"):
                self._allowlist.add(name)
                self._logger.info(f"Plugin '{name}' seviye={safety_level} kaydedildi")
            else:
                self._logger.warning(f"Plugin '{name}' seviye={safety_level} → KAPATILDI (P0 policy)")

    def authorize_plugin_call(
        self,
        plugin_name: str,
        handler_name: str,
        args: dict,
    ) -> tuple[bool, str]:
        """Plugin çağrısını yetkilendir.
        
        Returns:
            (authorized: bool, reason: str)
        """
        from jarvis.security_gate import authorize, Source, resolve

        with self._lock:
            # FIX #13: Allowlist kontrol
            if plugin_name not in self._allowlist:
                reason = f"Plugin '{plugin_name}' allowlist dışında (FAIL-CLOSED policy)"
                self._logger.warning(f"BLOCKED: {reason}")
                return False, reason

        # Merkezi security_gate'ten geç
        call = resolve(plugin_name, args, Source.PLUGIN)
        decision = authorize(plugin_name, args, Source.PLUGIN)

        if decision.verdict.value == "allow":
            self._logger.info(f"Plugin '{plugin_name}' ALLOW: {decision.reason}")
            return True, decision.reason
        elif decision.verdict.value == "needs_approval":
            self._logger.warning(f"Plugin '{plugin_name}' onay bekliyor: {decision.reason}")
            return False, "onay gerekiyor"
        else:
            self._logger.error(f"Plugin '{plugin_name}' DENY: {decision.reason}")
            return False, decision.reason


# Singleton
_PLUGIN_GATE = PluginSecurityGate()


def get_plugin_gate() -> PluginSecurityGate:
    """Plugin güvenlik kapısı (singleton)."""
    return _PLUGIN_GATE


# ══════════════════════════════════════════════════════════════
# FIX #14: ProcessLock Real OS-Level Lock
# ══════════════════════════════════════════════════════════════

class OSLevelProcessLock:
    """FIX #14: ProcessLock → gerçek OS-level lock (fcntl/Windows).
    
    SORUN: Eski ProcessLockManager dosya exists() → write() arasında
    yarış penceresi bırakıyordu. İki Jarvis süreci aynı state dosyasını
    eşzamanlı kilitleyebiliyordu.
    
    ÇÖZÜM:
    - Unix: fcntl.flock() (advisory, atomik)
    - Windows: msvcrt.locking() (mandatory, atomik)
    
    Açılmamış döndürmez; lock dosyasını kilit süresi boyunca açık tutar.
    """

    def __init__(self, lock_file: Path | None = None) -> None:
        from jarvis.paths import data_dir
        if lock_file is None:
            lock_file = data_dir() / "run" / "jarvis.lock"
        self.lock_file = lock_file
        self._lock_fd: int | None = None
        self._lock_held = False
        self._logger = logging.getLogger("jarvis.os_level_lock")

    def acquire(self, timeout: float = 10.0) -> bool:
        """OS-level kilit al.
        
        Args:
            timeout: Kaç saniye bekleneceği (not implemented for all platforms)
        
        Returns:
            True: kilit başarılı
            False: kilit başarısız (timeout/hata)
        """
        if self._lock_held:
            return True

        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Kilit dosyasını aç (create if needed)
            self._lock_fd = os.open(
                str(self.lock_file),
                os.O_CREAT | os.O_WRONLY | os.O_CLOEXEC,
                0o600,
            )

            # OS-specific lock
            if sys.platform == "win32":
                import msvcrt
                try:
                    msvcrt.locking(self._lock_fd, msvcrt.LK_NBLCK, 1)
                    self._lock_held = True
                    self._logger.info(f"OS-level lock acquired: {self.lock_file}")
                    return True
                except OSError as e:
                    if self._lock_fd is not None:
                        os.close(self._lock_fd)
                        self._lock_fd = None
                    self._logger.warning(f"Lock acquisition failed (Windows): {e}")
                    return False
            else:
                # Unix: fcntl.flock()
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self._lock_held = True
                    self._logger.info(f"OS-level lock acquired: {self.lock_file}")
                    return True
                except BlockingIOError:
                    os.close(self._lock_fd)
                    self._lock_fd = None
                    self._logger.warning(f"Lock already held (would block)")
                    return False

        except Exception as e:
            if self._lock_fd is not None:
                try:
                    os.close(self._lock_fd)
                except Exception:
                    pass
                self._lock_fd = None
            self._logger.error(f"Lock acquisition error: {e}")
            return False

    def release(self) -> bool:
        """Kilit bırak."""
        if not self._lock_held or self._lock_fd is None:
            return False

        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self._lock_fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

            os.close(self._lock_fd)
            self._lock_fd = None
            self._lock_held = False
            self._logger.info(f"OS-level lock released: {self.lock_file}")
            return True

        except Exception as e:
            self._logger.error(f"Lock release error: {e}")
            return False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


# ══════════════════════════════════════════════════════════════
# FIX #15: Virtual Brain State → User Data Dir
# ══════════════════════════════════════════════════════════════

class VirtualBrainStateManager:
    """FIX #15: Sanal Beyin state'ini kaynak dizininden çıkar.
    
    SORUN: experiments.json, src/jarvis/self_improvement/virtual_brain/
    orchestrator/ içinde tutuluyor → paket kirli, read-only kurulum bozuk,
    güncelleme state kaybı.
    
    ÇÖZÜM: experiments.json, sandbox/ (user data dir) altına taşı.
    
    Yeni lokasyon:
    - Linux: ~/.local/share/MuratJARVIS/sandbox/experiments.json
    - macOS: ~/Library/Application Support/MuratJARVIS/sandbox/experiments.json
    - Windows: %LOCALAPPDATA%\\MuratJARVIS\\sandbox\\experiments.json
    """

    @staticmethod
    def get_experiments_path() -> Path:
        """Deneyler JSON dosyasının doğru konumu (user data dir).
        
        ESKI (YANLIŞ):
            src/jarvis/.../orchestrator/experiments.json
        
        YENİ (DOĞRU):
            ~/.local/share/MuratJARVIS/sandbox/experiments.json
        """
        from jarvis.paths import data_dir
        sandbox_dir = data_dir() / "sandbox"
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        return sandbox_dir / "experiments.json"

    @staticmethod
    def get_reports_dir() -> Path:
        """Deney raporları dizini (user data dir).
        
        ESKI (YANLIŞ):
            src/jarvis/.../virtual_brain/reports/
        
        YENİ (DOĞRU):
            ~/.local/share/MuratJARVIS/sandbox/reports/
        """
        from jarvis.paths import data_dir
        reports_dir = data_dir() / "sandbox" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        return reports_dir

    @staticmethod
    def get_hypotheses_path() -> Path:
        """Hipotez kaydı dosyası (user data dir).
        
        ESKI (YANLIŞ):
            src/jarvis/.../virtual_brain/hypotheses.jsonl
        
        YENİ (DOĞRU):
            ~/.local/share/MuratJARVIS/sandbox/hypotheses.jsonl
        """
        from jarvis.paths import data_dir
        sandbox_dir = data_dir() / "sandbox"
        sandbox_dir.mkdir(parents=True, exist_ok=True)
        return sandbox_dir / "hypotheses.jsonl"

    @staticmethod
    def migrate_from_source_tree() -> None:
        """ESKI konumdan YENİ konuma taşı (one-time migration).
        
        Adımlar:
        1. Eski dosyaları bul (src/jarvis/self_improvement/...)
        2. User data dir'e kopyala
        3. Eski dosyaları sil
        4. Log yaz
        """
        import shutil

        logger = logging.getLogger("jarvis.vb_state_migration")

        # ESKI konumlar (source tree)
        source_root = Path(__file__).resolve().parent.parent.parent / "self_improvement" / "virtual_brain"
        old_experiments = source_root / "orchestrator" / "experiments.json"
        old_reports = source_root / "reports"
        old_hypotheses = source_root / "hypotheses.jsonl"

        # YENİ konumlar (user data dir)
        new_experiments = VirtualBrainStateManager.get_experiments_path()
        new_reports = VirtualBrainStateManager.get_reports_dir()
        new_hypotheses = VirtualBrainStateManager.get_hypotheses_path()

        moved = []

        # experiments.json taşı
        if old_experiments.exists() and not new_experiments.exists():
            try:
                shutil.copy2(old_experiments, new_experiments)
                old_experiments.unlink()
                moved.append("experiments.json")
                logger.info(f"Migrated: {old_experiments} → {new_experiments}")
            except Exception as e:
                logger.error(f"Migration failed for experiments.json: {e}")

        # reports/ taşı
        if old_reports.exists() and old_reports.is_dir() and not new_reports.exists():
            try:
                shutil.copytree(old_reports, new_reports)
                shutil.rmtree(old_reports)
                moved.append("reports/")
                logger.info(f"Migrated: {old_reports} → {new_reports}")
            except Exception as e:
                logger.error(f"Migration failed for reports/: {e}")

        # hypotheses.jsonl taşı
        if old_hypotheses.exists() and not new_hypotheses.exists():
            try:
                shutil.copy2(old_hypotheses, new_hypotheses)
                old_hypotheses.unlink()
                moved.append("hypotheses.jsonl")
                logger.info(f"Migrated: {old_hypotheses} → {new_hypotheses}")
            except Exception as e:
                logger.error(f"Migration failed for hypotheses.jsonl: {e}")

        if moved:
            logger.warning(
                f"Virtual Brain state migrated from source tree to user data dir: {moved}. "
                f"Please delete {source_root} if it's empty."
            )


# ══════════════════════════════════════════════════════════════
# Integration: main.py'de kullanım
# ══════════════════════════════════════════════════════════════

# FIX #14: ProcessLock yerine OS-level lock
# OLD: from jarvis.core.process_lock import ProcessLockManager
# NEW:
# lock = OSLevelProcessLock()
# if lock.acquire(timeout=10.0):
#     try:
#         # Kritik section
#         ...
#     finally:
#         lock.release()

# FIX #15: Virtual Brain state konumu
# OLD: from jarvis.self_improvement.virtual_brain.orchestrator.experiment_task_manager import EXPERIMENTS_PATH
# NEW:
# from jarvis.core.state_migration import VirtualBrainStateManager
# experiments_path = VirtualBrainStateManager.get_experiments_path()

# FIX #13: Plugin çağrısı
# OLD: self.plugins.call(name, args)  # no gate
# NEW:
# gate = get_plugin_gate()
# ok, reason = gate.authorize_plugin_call(name, handler_name, args)
# if ok:
#     result = self.plugins.call(name, args)
# else:
#     return f"Plugin '{name}' blocked: {reason}"
