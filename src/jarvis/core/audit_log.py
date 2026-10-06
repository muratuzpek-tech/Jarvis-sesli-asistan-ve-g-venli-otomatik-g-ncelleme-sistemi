"""Structured audit trail — kim ne yapti, ne zaman, sonuc ne oldu.

Guvenlik-relevant her olay JSON Lines olarak kaydedilir. TEK denetim kaydi
(plan Adim 3.1): JARVIS_HOME/memory/audit.log (jarvis.paths.memory_dir).
security_gate kararlari, tool_gate.audit_entry, agent_loop ve Brain Team
onay/ret/yurutme olaylari ve araclarin kendi log_action kayitlari buraya
yazilir.

Ilkeler:
- Onay kodlari ASLA ham olarak loglanmaz (SHA-256 hash ya da maske).
- Arac argumanlarinda gizli (parola/anahtar/token...) ve icerik alanlari
  (mesaj metni, dosya icerigi, bellek degeri) maskelenir.
- "approved" yalnizca gercek kullanici onayinda True; onaysiz yurutme
  "executed" ile isaretlenir.
- Audit log hatasi asla ana islemi DURDURMAZ.
- Yazi basarisiz olursa stderr'e uyari yazilir.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from datetime import datetime, timezone, timedelta

_lock = threading.Lock()
_TR_TZ = timezone(timedelta(hours=3))

# extra alaninin ustune YAZAMAYACAGI korumali alanlar
_PROTECTED_FIELDS = frozenset({
    "timestamp", "module", "action", "detail",
    "risk", "approval_required", "result", "approval_hash",
    "source", "verdict", "fingerprint", "tool", "params",
    "approved", "executed", "task_id",
})

# extra icerisinde HASSAS kabul edilen anahtar desenleri
# (bu degerler ASLA ham loglanmaz, SHA-256 hash olarak kaydedilir)
_SENSITIVE_PATTERNS = (
    "code", "pass", "secret", "token", "key", "auth",
    "cred", "pin", "otp", "session", "parola", "sifre", "şifre",
)


# Degeri ICERIK olan arac argumanlari (kisisel/gizli olabilir): uzunlugu
# disinda hicbir sey loglanmaz.
_CONTENT_KEYS = frozenset({
    "content", "value", "message_text", "message", "text", "body",
    "new_text", "old_text", "new_content", "change_request",
})

_CODE_PATTERNS = (
    re.compile(r"(confirm_code\s*[=:]\s*['\"]?)[^\s'\",)]+", re.I),
    re.compile(r"(Onay kodu:\s*)\S+", re.I),
    re.compile(r"(ONAY_KODU:\s*)\S+", re.I),
)


def redact_text(text) -> str:
    """Serbest metindeki onay kodlarini maskeler."""
    out = str(text or "")
    for pattern in _CODE_PATTERNS:
        out = pattern.sub(r"\1***", out)
    return out


def _is_sensitive_key(key) -> bool:
    kl = str(key).lower()
    return any(p in kl for p in _SENSITIVE_PATTERNS)


def mask_params(params, limit: int = 80) -> dict:
    """Arac argumanlarinin denetim kaydina / onay metnine yazilabilir hali:
    gizli anahtar adlari "***", icerik alanlari yalnizca uzunluk; kalanlar
    limit karaktere kisaltilir ve onay kodu temizlenir. Ic ice sozlukler ayni
    kuralla islenir."""
    if not isinstance(params, dict):
        return {}
    out: dict = {}
    for k, v in params.items():
        kl = str(k).lower()
        if _is_sensitive_key(k):
            out[k] = "***"
        elif kl in _CONTENT_KEYS:
            out[k] = f"<{len(str(v))} karakter gizlendi>"
        elif isinstance(v, dict):
            out[k] = mask_params(v, limit)
        else:
            out[k] = redact_text(v)[:limit]
    return out


def format_params(params, limit: int = 80) -> str:
    """Kullaniciya/modele gosterilen onay metinleri icin "k=v, ..." -
    mask_params kurallariyla (gizli degerler ve icerik asla ham yazilmaz)."""
    return ", ".join(f"{k}={v}" for k, v in mask_params(params, limit).items())


def _get_log_path():
    from jarvis.paths import memory_dir
    return memory_dir() / "audit.log"


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]


def log_action(
    *,
    module: str,
    action: str,
    detail: str = "",
    risk: str = "low",
    approval_required: bool = False,
    approval_code: str = "",
    result: str = "",
    extra: dict | None = None,
    source: str | None = None,
    verdict: str | None = None,
    fingerprint: str | None = None,
    tool: str | None = None,
    params: dict | None = None,
    approved: bool | None = None,
    executed: bool | None = None,
    task_id: str | None = None,
) -> None:
    """Tek bir guvenlik-relevant islemi kaydet. Arac olaylari (kapi kararlari,
    agent_loop / Brain onay-ret-yurutme) ek alanlari doldurur:
    source, verdict, fingerprint (yalnizca hash), tool, params (maskeli),
    approved (gercek kullanici onayi), executed, task_id."""
    record: dict = {
        "timestamp": datetime.now(_TR_TZ).isoformat(),
        "module": str(module),
        "action": str(action),
        "detail": redact_text(detail)[:500],
        "risk": str(risk),
        "approval_required": bool(approval_required),
        "result": redact_text(result)[:500],
    }
    if approval_code:
        record["approval_hash"] = _hash_code(approval_code)
    if source is not None:
        record["source"] = str(source)
    if verdict is not None:
        record["verdict"] = str(verdict)
    if fingerprint:
        # Parmak izi ham argumanlari icerir; yalnizca hash'i yazilir.
        record["fingerprint"] = hashlib.sha256(str(fingerprint).encode("utf-8")).hexdigest()[:16]
    if tool is not None:
        record["tool"] = str(tool)
    if params is not None:
        record["params"] = mask_params(params)
    if approved is not None:
        record["approved"] = bool(approved)
    if executed is not None:
        record["executed"] = bool(executed)
    if task_id is not None:
        record["task_id"] = str(task_id)
    if extra:
        for k, v in extra.items():
            if k not in _PROTECTED_FIELDS:
                kl = k.lower()
                if any(p in kl for p in _SENSITIVE_PATTERNS):
                    # GUVENLIK: hassas deger ham olarak ASLA loglanmaz
                    record[f"{k}_sha256"] = _hash_code(str(v))
                elif isinstance(v, str):
                    record[k] = redact_text(v)
                else:
                    record[k] = v

    line = json.dumps(record, ensure_ascii=False, default=str)
    try:
        log_path = _get_log_path()
        with _lock:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:
        print(f"[AUDIT-ERROR] Log yazilamadi: {exc}", file=sys.stderr)


def log_tool_event(
    *,
    source: str,
    event: str,
    tool: str,
    params: dict | None = None,
    result="",
    approved: bool = False,
    executed: bool = False,
    verdict: str | None = None,
    risk: str = "high",
    task_id: str | None = None,
    detail: str = "",
) -> None:
    """agent_loop / Brain Team gibi kapinin henuz disinda kalan yollarin
    onay istegi, onay, ret ve yurutme olaylari. Parmak izi security_gate ile
    ayni kaynaktan (core/call_fingerprint) hesaplanir."""
    try:
        from jarvis.core.call_fingerprint import fingerprint as _fp
        fp = _fp(str(tool), dict(params or {}))
    except Exception:
        fp = None
    log_action(
        module=str(source), action=str(event), detail=detail, risk=risk,
        approval_required=(verdict == "needs_approval"), result=str(result),
        source=str(source), verdict=verdict, fingerprint=fp, tool=str(tool),
        params=dict(params or {}), approved=approved, executed=executed,
        task_id=task_id,
    )


def read_recent(count: int = 50) -> list[dict]:
    entries: list[dict] = []
    try:
        log_path = _get_log_path()
        with open(log_path, encoding="utf-8") as fh:
            for line in fh.readlines()[-count:]:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return entries


def query(**filters) -> list[dict]:
    entries = read_recent(count=5000)
    if not filters:
        return entries
    return [
        e for e in entries
        if all(str(e.get(k, "")) == str(v) for k, v in filters.items())
    ]
