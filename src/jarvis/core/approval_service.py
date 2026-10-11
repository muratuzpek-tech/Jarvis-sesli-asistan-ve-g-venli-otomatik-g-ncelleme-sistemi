"""Application-owned approval tokens for risky tool actions.

The language model can request an approval, but it cannot mint or consume one.
A token is single-use and expires quickly.  The caller must still obtain an
explicit user response between preview and execution.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class PendingApproval:
    code: str
    action: str
    summary: str
    issued_at: float
    expires_at: float


class ApprovalService:
    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self._ttl = float(ttl_seconds)
        self._pending: dict[str, PendingApproval] = {}
        self._lock = threading.Lock()
        self._last_user_turn = 0.0

    def mark_user_turn(self) -> None:
        """Record a real user turn; model-generated follow-up calls do not call this."""
        with self._lock:
            self._last_user_turn = time.monotonic()

    def request(self, action: str, summary: str) -> PendingApproval:
        now = time.monotonic()
        item = PendingApproval(
            code=secrets.token_urlsafe(9),
            action=str(action),
            summary=str(summary),
            issued_at=now,
            expires_at=now + self._ttl,
        )
        with self._lock:
            self._purge_locked(now)
            self._pending[item.code] = item
        return item

    def consume(
        self,
        code: str,
        action: str,
        summary: str | None = None,
        require_user_turn: bool = True,
    ) -> bool:
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            item = self._pending.get(str(code or "").strip())
            if item is None or item.action != action:
                return False
            if summary is not None and item.summary != summary:
                return False
            if require_user_turn and self._last_user_turn <= item.issued_at:
                return False
            del self._pending[item.code]
            return True

    def cancel(self, code: str) -> bool:
        """Kullanici reddettiginde bekleyen onayi hemen gecersiz kilar
        (TTL dolana kadar yasamasin)."""
        with self._lock:
            return self._pending.pop(str(code or "").strip(), None) is not None

    def peek(self, code: str, action: str) -> PendingApproval | None:
        now = time.monotonic()
        with self._lock:
            self._purge_locked(now)
            item = self._pending.get(str(code or "").strip())
            return item if item and item.action == action else None

    def _purge_locked(self, now: float) -> None:
        expired = [code for code, item in self._pending.items() if item.expires_at <= now]
        for code in expired:
            self._pending.pop(code, None)


approval_service = ApprovalService()
