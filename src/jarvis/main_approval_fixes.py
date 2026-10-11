"""main.py FIX'leri

FIX #3: ApprovalRegistry Singleton Entegrasyon
FIX #4: Concurrent Handler Mutual Exclusion (Brain Team ↔ Agent Loop)
FIX #5: Optional Import Error Handling
"""
from __future__ import annotations

import threading
import time

# ══════════════════════════════════════════════════════════════
# FIX #5: Optional Import Error Handling
# ══════════════════════════════════════════════════════════════
try:
    from jarvis.core.approval_registry import get_registry, ApprovalType
    _APPROVAL_REGISTRY = get_registry()
    _REGISTRY_AVAILABLE = True
except ImportError as e:
    _APPROVAL_REGISTRY = None
    _REGISTRY_AVAILABLE = False
    print(f"[WARN] ApprovalRegistry import başarısız: {e}")


# ══════════════════════════════════════════════════════════════
# FIX #4: Concurrent Handler Mutual Exclusion
# ══════════════════════════════════════════════════════════════
class ApprovalSlot:
    """Tek bir onay slotu — AYNI ANDA sadece BİR handler bekleyebilir.
    
    Sorun #4 çözümü:
    - Brain Team onayı gelirken Agent Loop onayı GELEMEZ
    - Ayırt edici: action_type (brain_team vs agent_loop)
    - Parmak izi: task_id + tool + parameters
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.action_type: str | None = None  # brain_team, agent_loop, None
        self.task_id: str | None = None
        self.fingerprint: str | None = None
        self.issued_at: float | None = None
        self.expires_at: float | None = None
        self.granted: bool = False

    def request(self, action_type: str, task_id: str, fingerprint: str, ttl: float = 60.0) -> bool:
        """Onay iste. AYNI ANDA başka onay varsa False döner."""
        with self.lock:
            now = time.monotonic()

            # Mevcut onay süresi dolmuş mu?
            if self.expires_at and now >= self.expires_at:
                self._clear()

            # Başka onay bekleniyor mu?
            if self.action_type is not None:
                return False

            # Aynı istek mi? (idempotent)
            if (self.action_type == action_type and
                self.task_id == task_id and
                self.fingerprint == fingerprint and
                self.expires_at and now < self.expires_at):
                return True

            # Yeni isteği kaydet
            self.action_type = action_type
            self.task_id = task_id
            self.fingerprint = fingerprint
            self.issued_at = now
            self.expires_at = now + ttl
            self.granted = False
            return True

    def grant(self) -> bool:
        """Kullanıcı onay verdi."""
        with self.lock:
            if self.action_type is None:
                return False
            if self.expires_at and time.monotonic() >= self.expires_at:
                self._clear()
                return False
            self.granted = True
            return True

    def consume(self, action_type: str, task_id: str, fingerprint: str) -> bool:
        """Onay tüket (tek kullanım)."""
        with self.lock:
            now = time.monotonic()

            # Geçerli mi?
            if self.action_type != action_type:
                return False
            if self.task_id != task_id:
                return False
            if self.fingerprint != fingerprint:
                return False
            if self.expires_at and now >= self.expires_at:
                self._clear()
                return False
            if not self.granted:
                return False

            # Tüket
            self._clear()
            return True

    def cancel(self) -> None:
        """Onayı iptal et."""
        with self.lock:
            self._clear()

    def pending(self) -> tuple[str, str, str] | None:
        """(action_type, task_id, fingerprint) veya None."""
        with self.lock:
            if self.action_type and time.monotonic() < (self.expires_at or 0):
                return (self.action_type, self.task_id or "", self.fingerprint or "")
            return None

    def _clear(self) -> None:
        """Slot temizle."""
        self.action_type = None
        self.task_id = None
        self.fingerprint = None
        self.issued_at = None
        self.expires_at = None
        self.granted = False


# ══════════════════════════════════════════════════════════════
# FIX #3: ApprovalRegistry Singleton Entegrasyon
# ══════════════════════════════════════════════════════════════
_APPROVAL_SLOT = ApprovalSlot()


def _action_fingerprint(action: str, params: dict) -> str:
    """Aksiyon parmak izi (idempotency)."""
    import hashlib
    import json
    content = json.dumps({"action": action, "params": params}, sort_keys=True)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════
# JarvisLive FIX'leri (snippet)
# ══════════════════════════════════════════════════════════════
class JarvisLiveApprovalMixin:
    """JarvisLive'a eklenecek approval methods."""

    _approval_slot = _APPROVAL_SLOT
    _confirmation_lock = threading.RLock()

    # ── Brain Team Onay (FIX #4) ────────────────────────
    def request_brain_team_approval(self, task_id: str, step: dict, message: str) -> bool:
        """Brain Team'in adım onayını iste.
        
        FIX #4: Concurrent handler mutual exclusion
        - Başka onay zaten bekleniyor mu?
        - Aynı görev aynı adımı tekrar sormuyor
        """
        params = {"task_id": task_id, "step": step.get("id", "")}
        fingerprint = _action_fingerprint("brain_team", params)

        # Onay iste (AYNI ANDA başka onay varsa False)
        ok = self._approval_slot.request("brain_team", task_id, fingerprint)
        if not ok:
            pending = self._approval_slot.pending()
            if pending:
                action, tid, _ = pending
                return action == "brain_team" and tid == task_id
            return False

        # Kullanıcıya sor
        try:
            self.ui.write_log(f"[BRAIN_TEAM_ONAY] {message}")
        except Exception:
            pass
        self.speak(
            f"[BRAIN_TEAM_ONAY_ISTEGI] {message}\n"
            f"Bunu kullanıcıya adım, hedef ve argümanlarla birlikte sor.\n"
            f"Onayı yalnızca kullanıcının kendi 'evet'/'hayır' cevabı verir."
        )
        return True

    def _handle_brain_team_reply(self, text: str) -> bool:
        """Brain Team onay cevabı işle.
        
        FIX #4: Doğru task'ı tespit et (pending slot'tan)
        """
        pending = self._approval_slot.pending()
        if not pending or pending[0] != "brain_team":
            return False

        _, task_id, fingerprint = pending

        confirmed = self._is_confirmation(text)
        rejected = self._is_rejection(text)

        if not (confirmed or rejected):
            return False

        if confirmed:
            # Onay ver
            self._approval_slot.grant()

            # FIX #3: ApprovalRegistry ile senkronize
            if _REGISTRY_AVAILABLE and _APPROVAL_REGISTRY:
                try:
                    code = _APPROVAL_REGISTRY.register_request(
                        approval_type=ApprovalType.BRAIN_TEAM,
                        fingerprint=fingerprint,
                        metadata={"task_id": task_id},
                        ttl_seconds=60,
                    )
                    _APPROVAL_REGISTRY.confirm(code)
                except Exception as e:
                    print(f"[WARN] Registry sync başarısız: {e}")

            from jarvis.core.brain_orchestrator import get_orchestrator
            orch = get_orchestrator()
            result = orch.approve(task_id)
        else:
            # Ret
            self._approval_slot.cancel()
            from jarvis.core.brain_orchestrator import get_orchestrator
            orch = get_orchestrator()
            result = orch.deny(task_id)

        try:
            self.ui.write_log(f"[BRAIN_TEAM_SONUC] {result}")
        except Exception:
            pass
        self.speak(f"[BRAIN_TEAM_ONAY_SONUC] {result}. Bunu kullanıcıya bildir.")
        return True

    # ── Agent Loop Onay (FIX #4) ────────────────────────
    def request_agent_loop_approval(
        self, task_id: str, pending: dict, message: str
    ) -> bool:
        """Agent Loop'un adım onayını iste.
        
        FIX #4: Concurrent handler mutual exclusion
        - Brain Team onayı zaten bekleniyor mu?
        - Aynı görev aynı adımı tekrar sormuyor
        """
        params = {
            "task_id": task_id,
            "tool": pending.get("tool", ""),
            "parameters": pending.get("parameters", {}),
        }
        fingerprint = _action_fingerprint("agent_loop", params)

        # Onay iste (AYNI ANDA başka onay varsa False)
        ok = self._approval_slot.request("agent_loop", task_id, fingerprint)
        if not ok:
            pending_slot = self._approval_slot.pending()
            if pending_slot:
                action, tid, _ = pending_slot
                return action == "agent_loop" and tid == task_id
            return False

        # Kullanıcıya sor
        try:
            self.ui.write_log(f"[AGENT_LOOP_ONAY] {message}")
        except Exception:
            pass
        self.speak(
            f"[AGENT_LOOP_ONAY_ISTEGI] {message}\n"
            f"Araç, argümanlar ve hedefle birlikte sor.\n"
            f"Onayı yalnızca kullanıcının kendi cevabı verir."
        )
        return True

    def _handle_agent_loop_reply(self, text: str) -> bool:
        """Agent Loop onay cevabı işle.
        
        FIX #4: Doğru task'ı tespit et (pending slot'tan)
        """
        pending = self._approval_slot.pending()
        if not pending or pending[0] != "agent_loop":
            return False

        _, task_id, fingerprint = pending

        confirmed = self._is_confirmation(text)
        rejected = self._is_rejection(text)

        if not (confirmed or rejected):
            return False

        if confirmed:
            # Onay ver
            self._approval_slot.grant()

            # FIX #3: ApprovalRegistry ile senkronize
            if _REGISTRY_AVAILABLE and _APPROVAL_REGISTRY:
                try:
                    code = _APPROVAL_REGISTRY.register_request(
                        approval_type=ApprovalType.CUSTOM,  # agent_loop için custom
                        fingerprint=fingerprint,
                        metadata={"task_id": task_id, "type": "agent_loop"},
                        ttl_seconds=60,
                    )
                    _APPROVAL_REGISTRY.confirm(code)
                except Exception as e:
                    print(f"[WARN] Registry sync başarısız: {e}")

            # Agent loop'u çalıştır
            import jarvis.core.agent_loop as agent_loop_mod
            result = agent_loop_mod.approve_task(task_id)
        else:
            # Ret
            self._approval_slot.cancel()
            result = "Agent loop adımı reddedildi."

        try:
            self.ui.write_log(f"[AGENT_LOOP_SONUC] {result}")
        except Exception:
            pass
        self.speak(f"[AGENT_LOOP_ONAY_SONUC] {result}. Bunu kullanıcıya bildir.")
        return True

    # ── Helper methods ──────────────────────────────────
    @staticmethod
    def _is_confirmation(text: str) -> bool:
        """'evet' mi?"""
        normalized = text.lower().strip()
        confirmations = {
            "onaylıyorum", "onayliyorum", "evet", "evet onaylıyorum",
            "evet yap", "tamam", "tamam onayla", "approve", "approve it",
            "yes do it", "devam et",
        }
        return any(c in normalized for c in confirmations)

    @staticmethod
    def _is_rejection(text: str) -> bool:
        """'hayır' mı?"""
        normalized = text.lower().strip()
        rejections = {
            "hayır", "hayir", "vazgeç", "vazgec", "iptal", "iptal et",
            "no", "don't", "dont", "cancel", "decline",
        }
        return any(r in normalized for r in rejections)
