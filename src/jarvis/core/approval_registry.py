"""approval_registry.py — MERKEZI ONAY SİSTEMİ (ÜNİFİKASYON).

SORUN (2026-10-05):
- main.py: _tool_confirm_code tuple
- dev_agent.py: _pending_dev_agent dict
- brain_orchestrator.py: request_brain_team_approval()
→ 3 AYRI MEKANIZMA, ETKİLEŞİM YOKTUR

ÇÖZÜM: Tek bir registry. Tüm onaylar:
- dev_agent request
- brain_team approval
- sandbox experiment
- terminal command
Hepsi buradan geçer. TTL, senkronizasyon, conflict detection merkezi.

BAŞARILI VE İSPATLANMIŞ DESEN: actor_approval_service.py
(Bu modül onun genelleştirilmiş halidir.)
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any
from enum import Enum

# Başlatma
import sys
def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR = _get_base_dir()
APPROVAL_REGISTRY_PATH = BASE_DIR / "config" / "approval_registry.json"


class ApprovalType(Enum):
    """Onay türleri — hangi sistem tarafından çağrıldı?"""
    DEV_AGENT = "dev_agent"          # actions/dev_agent.py → proje yazma
    BRAIN_TEAM = "brain_team"        # brain_orchestrator.py → step approval
    SANDBOX_EXPERIMENT = "sandbox"   # virtual_brain → deney çalıştırma
    TERMINAL_COMMAND = "terminal"    # main.py → sistem komutu
    FILE_MODIFICATION = "file_mod"   # intent_router → dosya yazma
    CUSTOM = "custom"                # Diğer amaçlar


@dataclass
class ApprovalRequest:
    """Onay isteği kaydı."""
    code: str                          # one-time confirmation code
    approval_type: ApprovalType
    fingerprint: str                   # action integrity hash (action tekrar çağrılırsa aynı code döner)
    issued_at: float                   # monotonic time
    expires_at: float                  # TTL sonrası invalid
    metadata: Dict[str, Any]           # Type-specific data
    status: str = "pending"            # pending, confirmed, rejected, expired
    confirmed_at: Optional[float] = None
    user_message: Optional[str] = None # Kullanıcının "evet"/"hayır" mesajı

    def is_valid(self, now: float) -> bool:
        """TTL'si geçmiş mi?"""
        return self.status == "pending" and now < self.expires_at

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def to_dict(self) -> dict:
        """JSON serialize için."""
        d = asdict(self)
        d["approval_type"] = self.approval_type.value
        return d

    @staticmethod
    def from_dict(d: dict) -> ApprovalRequest:
        """JSON deserialize için."""
        d2 = dict(d)
        d2["approval_type"] = ApprovalType(d2["approval_type"])
        return ApprovalRequest(**d2)


class ApprovalRegistry:
    """Merkezi onay sistemi.
    
    TASARIMI:
    1. Tüm onaylar REGISTRY'de saklanır
    2. Same-action = same code (idempotency)
    3. Expiry + cleanup
    4. Atomic file operations (race condition'a karşı)
    """

    def __init__(self, path: Path = APPROVAL_REGISTRY_PATH):
        self.path = path
        self._lock = threading.RLock()
        self._logger = self._make_logger()

    def _make_logger(self) -> logging.Logger:
        logger = logging.getLogger("jarvis.approval_registry")
        if not logger.handlers:
            try:
                log_dir = self.path.parent.parent / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                handler = logging.FileHandler(log_dir / "approval_registry.log", encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
                logger.addHandler(handler)
                logger.setLevel(logging.INFO)
            except Exception:
                pass
        return logger

    def _load(self) -> Dict[str, ApprovalRequest]:
        """Disk'ten yükle."""
        try:
            if self.path.is_file():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return {code: ApprovalRequest.from_dict(req) for code, req in data.items()}
        except Exception as e:
            self._logger.error(f"Registry yükleme hatası: {e}")
        return {}

    def _save(self, registry: Dict[str, ApprovalRequest]) -> None:
        """Disk'e kaydet (atomic)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {code: req.to_dict() for code, req in registry.items()}
        with tempfile.NamedTemporaryFile(
            "w", dir=self.path.parent, delete=False, encoding="utf-8", suffix=".tmp"
        ) as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            temp_name = tmp.name
        Path(temp_name).replace(self.path)

    def register_request(
        self,
        approval_type: ApprovalType,
        fingerprint: str,
        metadata: dict | None = None,
        ttl_seconds: int = 60,
    ) -> str:
        """Onay isteği kaydet.
        
        AYNI fingerprint'e sahip isteğe AYNI code döner (idempotent).
        TTL sonrası otomatik "expired" olur.
        
        Args:
            approval_type: Onay türü (DEV_AGENT, BRAIN_TEAM, vb.)
            fingerprint: Action integrity hash (same action = same code)
            metadata: Type-specific metadata
            ttl_seconds: Kaç saniye valid kalacak?
            
        Returns:
            one-time confirmation code
        """
        import time
        now = time.monotonic()
        
        with self._lock:
            registry = self._load()
            
            # Aynı fingerprint'e sahip pending request var mı?
            for code, req in registry.items():
                if (req.approval_type == approval_type and
                    req.fingerprint == fingerprint and
                    req.is_valid(now)):
                    self._logger.info(
                        f"Aynı istek tekrar → aynı code: {code} (type={approval_type.value}, fp={fingerprint[:16]}...)"
                    )
                    return code
            
            # Yeni isteği kaydet
            code = secrets.token_hex(3)
            request = ApprovalRequest(
                code=code,
                approval_type=approval_type,
                fingerprint=fingerprint,
                issued_at=now,
                expires_at=now + ttl_seconds,
                metadata=metadata or {},
            )
            registry[code] = request
            self._save(registry)
            
            self._logger.info(
                f"Onay kaydedildi: {code} (type={approval_type.value}, expires={ttl_seconds}s)"
            )
            return code

    def confirm(self, code: str, now: float | None = None) -> ApprovalRequest | None:
        """Onay kodunu onayla.
        
        Returns:
            Onayland ıysa ApprovalRequest, aksi halde None
        """
        import time
        now = now or time.monotonic()
        
        with self._lock:
            registry = self._load()
            request = registry.get(code)
            
            if request is None:
                self._logger.warning(f"Onay kodu bulunamadı: {code}")
                return None
            
            if request.is_expired(now):
                self._logger.warning(f"Onay kodu süresi dolmuş: {code}")
                request.status = "expired"
                self._save(registry)
                return None
            
            request.status = "confirmed"
            request.confirmed_at = now
            self._save(registry)
            
            self._logger.info(f"Onay onaylandı: {code} (type={request.approval_type.value})")
            return request

    def reject(self, code: str, reason: str = "") -> ApprovalRequest | None:
        """Onay kodunu reddet."""
        import time
        now = time.monotonic()
        
        with self._lock:
            registry = self._load()
            request = registry.get(code)
            
            if request is None:
                return None
            
            if request.is_expired(now):
                request.status = "expired"
            else:
                request.status = "rejected"
                request.user_message = reason
            
            self._save(registry)
            self._logger.info(f"Onay reddedildi: {code} (reason={reason[:60]})")
            return request

    def get_status(self, code: str, now: float | None = None) -> str:
        """Onay kodunun durumu.
        
        Returns: "pending", "confirmed", "rejected", "expired"
        """
        import time
        now = now or time.monotonic()
        
        with self._lock:
            registry = self._load()
            request = registry.get(code)
            
            if request is None:
                return "not_found"
            
            if request.is_expired(now):
                return "expired"
            
            return request.status

    def cleanup_expired(self) -> int:
        """Süresi dolan onayları temizle.
        
        Returns: Silinen onay sayısı
        """
        import time
        now = time.monotonic()
        
        with self._lock:
            registry = self._load()
            original_count = len(registry)
            registry = {
                code: req for code, req in registry.items()
                if not req.is_expired(now)
            }
            self._save(registry)
            cleaned = original_count - len(registry)
            if cleaned > 0:
                self._logger.info(f"Süresi dolan onaylar temizlendi: {cleaned}")
            return cleaned

    def get_request(self, code: str) -> ApprovalRequest | None:
        """Onay isteğini getir."""
        with self._lock:
            registry = self._load()
            return registry.get(code)


# Singleton instance
_registry: ApprovalRegistry | None = None


def get_registry() -> ApprovalRegistry:
    """Global registry instance."""
    global _registry
    if _registry is None:
        _registry = ApprovalRegistry()
    return _registry


if __name__ == "__main__":
    # Test
    reg = ApprovalRegistry()
    
    # İlk istek
    code1 = reg.register_request(
        ApprovalType.DEV_AGENT,
        fingerprint="test_dev_agent_project_v1",
        metadata={"description": "test project"}
    )
    print(f"Code 1: {code1}")
    
    # Aynı istek → aynı code
    code2 = reg.register_request(
        ApprovalType.DEV_AGENT,
        fingerprint="test_dev_agent_project_v1",
        metadata={"description": "test project"}
    )
    print(f"Code 2 (should be same): {code2}")
    assert code1 == code2, "Idempotency failed!"
    
    # Onay
    req = reg.confirm(code1)
    print(f"Status: {req.status if req else 'None'}")
    
    print("✅ ApprovalRegistry tests passed")
