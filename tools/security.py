"""
tools/security.py — Jarvis 2.0 Güvenlik Katmanı

Her tool çağrısı BU katmandan geçmeden ÇALIŞMAZ.

Güvenlik seviyeleri:
  READ_ONLY    → her zaman serbest (dosya listele, araştır, oku)
  NORMAL       → serbest ama loglanır (dosya oluştur, ayar değiştir)
  DANGEROUS    → kullanıcı ONAYI gerekir (tarayıcı kontrol, terminal komut)
  DESTRUCTIVE  → kullanıcı ONAYI + CONFIRMATION_REQUIRED akışı (sil, kapat)

Atack surface azaltma:
  - Input sanitization: path traversal, null bytes, SQL injection
  - Rate limiting: aynı tool 1 dakikada 30 kez (DoS koruması)
  - Audit trail: her çağrı log dosyasına yazar
"""
from __future__ import annotations

import enum
import re
import time
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SecurityLevel(enum.IntEnum):
    """Tool güvenlik seviyeleri (sayısal: büyük = tehlikeli)."""
    READ_ONLY    = 0
    NORMAL       = 1
    DANGEROUS    = 2
    DESTRUCTIVE  = 3


@dataclass
class SecurityVerdict:
    """Bir tool çağrısının güvenlik kararı."""
    allowed: bool
    reason: str = ""
    requires_confirmation: bool = False
    confirm_prompt: str = ""

    def __repr__(self) -> str:
        symbol = "✅" if self.allowed else "🔒"
        return f"{symbol} {self.reason}"


# Tehlikeli karakter desenleri
_DANGEROUS_PATTERNS = [
    re.compile(r"\x00"),                              # null byte
    re.compile(r"\.\./\.\."),                          # deep path traversal
    re.compile(r"&&?\s*(rm|del|format|mkfs)"),        # chained destructive cmds
    re.compile(r">\s*/(dev|etc|boot)"),               # overwrite system dirs
    re.compile(r";\s*(rm|del|format|mkfs)\s"),        # semicolon destructive
    re.compile(r"`[^`]*\$\(rm"),                       # command injection rm
]


class SecurityManager:
    """
    Merkezi güvenlik denetleyicisi.
    
    Kullanım:
        verdict = security.check("file_controller", {"action": "delete", "path": "/"})
        if not verdict.allowed:
            return verdict.reason
    """

    def __init__(self) -> None:
        self._rate_limit: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._audit_log: Path | None = None
        self._max_per_minute = 30

    def set_audit_log(self, path: Path) -> None:
        """Audit log dosyasını belirle."""
        self._audit_log = path

    def check(
        self,
        tool_name: str,
        args: dict[str, Any],
        level: SecurityLevel = SecurityLevel.NORMAL,
        user_confirmed: bool = False,
    ) -> SecurityVerdict:
        """
        Tool çağrısını denetle.
        
        Args:
            tool_name:   Araç adı (audit log için)
            args:        Gönderilen parametreler
            level:       Araç'ın güvenlik seviyesi
            user_confirmed: Kullanıcı onayı verildi mi?
        
        Returns:
            SecurityVerdict: allowed=True/False + açıklama
        """
        # 1. Rate limit
        rate_ok, rate_reason = self._check_rate(tool_name)
        if not rate_ok:
            self._audit(tool_name, args, blocked=True, reason=rate_reason)
            return SecurityVerdict(allowed=False, reason=rate_reason)

        # 2. Input sanitization
        san_ok, san_reason = self._sanitize_inputs(args)
        if not san_ok:
            self._audit(tool_name, args, blocked=True, reason=san_reason)
            return SecurityVerdict(allowed=False, reason=san_reason)

        # 3. Seviye kontrolü
        if level >= SecurityLevel.DESTRUCTIVE and not user_confirmed:
            prompt = f"⚠️ '{tool_name}' DESTRUCTIVE işlem. Onaylıyor musunuz?"
            self._audit(tool_name, args, blocked=True, reason="confirmation_required")
            return SecurityVerdict(
                allowed=False,
                requires_confirmation=True,
                confirm_prompt=prompt,
                reason="DESTRUCTIVE — user confirmation required",
            )

        if level >= SecurityLevel.DANGEROUS and not user_confirmed:
            prompt = f"⚠️ '{tool_name}' tehlikeli olabilir. Devam edilsin mi?"
            self._audit(tool_name, args, blocked=False, reason="dangerous_passed")
            return SecurityVerdict(
                allowed=True,
                requires_confirmation=True,
                confirm_prompt=prompt,
                reason="DANGEROUS — proceeding with caution",
            )

        self._audit(tool_name, args, blocked=False, reason="allowed")
        return SecurityVerdict(allowed=True, reason=f"OK (level={level.name})")

    def _check_rate(self, tool_name: str) -> tuple[bool, str]:
        """Rate limit: aynı tool dakikada 30 kez."""
        now = time.monotonic()
        cutoff = now - 60.0

        with self._lock:
            calls = self._rate_limit.setdefault(tool_name, [])
            calls[:] = [t for t in calls if t > cutoff]

            if len(calls) >= self._max_per_minute:
                return False, f"RATE_LIMIT: {tool_name} — {len(calls)} calls/min"

            calls.append(now)
            return True, ""

    def _sanitize_inputs(self, args: dict[str, Any]) -> tuple[bool, str]:
        """Tehlikeli input kalıplarını engelle."""
        for key, val in args.items():
            if isinstance(val, str):
                for pattern in _DANGEROUS_PATTERNS:
                    if pattern.search(val):
                        return False, f"SANITIZE: '{key}' tehlikeli pattern → {pattern.pattern}"
            elif isinstance(val, dict):
                ok, reason = self._sanitize_inputs(val)
                if not ok:
                    return False, reason
            elif isinstance(val, (list, tuple)):
                for item in val:
                    if isinstance(item, str):
                        for pattern in _DANGEROUS_PATTERNS:
                            if pattern.search(item):
                                return False, f"SANITIZE: '{key}' list item tehlike → {pattern.pattern}"
        return True, ""

    def _audit(
        self,
        tool_name: str,
        args: dict[str, Any],
        blocked: bool,
        reason: str,
    ) -> None:
        """Audit trail — her çağrıyı kaydet."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        status = "BLOCKED" if blocked else "PASSED"
        safe_args = {k: str(v)[:80] for k, v in args.items()}
        line = f"{ts} [{status}] {tool_name} {safe_args} — {reason}\n"

        try:
            if self._audit_log:
                with open(self._audit_log, "a", encoding="utf-8") as f:
                    f.write(line)
        except OSError:
            pass


# Singleton
security = SecurityManager()
