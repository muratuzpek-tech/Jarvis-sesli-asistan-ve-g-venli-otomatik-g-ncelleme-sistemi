"""agent_panel_state.py — dev agent panosu icin GUVENLI, salt okunur durum.

agentic_coder.live_status() ve agent_board.list_jobs() ciktisindan yalnizca
IZIN LISTESINDEKI alanlarla JSON'a donusturulebilir bir sozluk uretir. Bu
modul HTTP sunucusu ya da arayuz icermez; onlar bu ciktiyi aynen sunar.

Ilkeler:
- Izin listesi: girdide ne olursa olsun yalnizca asagida adi gecen alanlar
  cikar; tipler dogrulanir, bilinmeyen degerler "bilinmiyor"/0/"" olur.
- Her metin clean_text'ten gecer: ortamdaki gizli degerler (GEMINI_API_KEY
  vb.) ve onekleri, ev dizini yollari (yalnizca son bilesen kalir), Google
  anahtar bicimi / ?key= parametresi, onay kodlari (audit_log.redact_text) ve
  bilinen kimlik bilgisi kaliplari (memory.sanitizer) maskelenir; satirlar
  birlestirilir, uzunluk sinirlanir. Maskeleme KESMEDEN ONCE yapilir.
- Pano sonuc govdesi, dosya icerigi, ham model ciktisi, prompt, audit.log,
  konusma kaydi gibi kaynaklari HIC okumaz.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.core.audit_log import redact_text
from jarvis.memory.sanitizer import sanitize

SCHEMA_VERSION = 1

_MAX_TEXT = 120
_MAX_DESC = 80
_MAX_RUNS = 10
_MAX_JOBS = 10
_MAX_FILES = 50
_MAX_EVENTS = 20
_MAX_SUMMARY = 4
_MAX_INT = 10**9
_SCAN_CAP = 4000          # maskelemeden once metin bu uzunlukta kesilir

_RUN_STATUSES = frozenset({"çalışıyor", "bitti", "başarısız"})
_JOB_STATUSES = frozenset({"pending", "running", "completed", "failed"})
_PROVIDERS = frozenset({"gemini", "ollama", "özel"})
_REJECT_KEYS = ("reject", "eval", "lock", "method", "ruff")
_SUMMARY_PREFIXES = ("Durum:", "Proje klasörü:", "Eksik modül:", "Hata:")
_ID_RE = re.compile(r"[0-9A-Za-z]{1,16}")

# main._KEY_LIKE_RE'nin esdegeri (jarvis.main import edilmez: PyQt/ses/Gemini
# yukler). AIza onekinden sonra HER uzunluk maskelenir: kesilmis bir anahtar
# parcasi da gecmesin.
_KEY_LIKE_RE = re.compile(r"AIza[0-9A-Za-z_\-]*|([?&]key=)[^&\s]+")
_SECRET_ENV_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
_MIN_SECRET_PREFIX = 8
_PATH_TAIL = r"(?P<tail>[/\\][^\s'\"`,;()<>]*)?"
_GENERIC_HOMES = r"~|/home/[^/\s]+|/Users/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+"


def _secret_values() -> list[str]:
    out = []
    for name in _SECRET_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if len(value) >= _MIN_SECRET_PREFIX:
            out.append(value)
    return out


def _scrub_secret(text: str, secret: str) -> str:
    # Tam deger ve (metin kesilmis olabilecegi icin) en az 8 karakterlik onekleri.
    for n in range(len(secret), _MIN_SECRET_PREFIX - 1, -1):
        if secret[:n] in text:
            text = text.replace(secret[:n], "***")
    return text


def _home_re() -> re.Pattern:
    alts = []
    try:
        home = str(Path.home())
        if len(home) > 1:
            alts.append(re.escape(home.rstrip("/\\")))
    except (RuntimeError, KeyError):
        pass
    alts.append(_GENERIC_HOMES)
    return re.compile(f"(?:{'|'.join(alts)}){_PATH_TAIL}")


def _last_component(match: re.Match) -> str:
    """Ev dizini altindaki yol -> son bilesen (proje/dosya adi); ev dizininin
    kendisi -> "~"."""
    parts = [p for p in re.split(r"[/\\]", match.group("tail") or "") if p]
    return parts[-1] if parts else "~"


def clean_text(value: Any, limit: int = _MAX_TEXT) -> str:
    """Panoya gidecek her metni maskeler ve sinirlar (tek satir)."""
    if value is None:
        return ""
    text = " ".join(str(value).split())[:_SCAN_CAP]
    for secret in _secret_values():
        text = _scrub_secret(text, secret)
    text = _home_re().sub(_last_component, text)
    text = _KEY_LIKE_RE.sub(lambda m: (m.group(1) or "") + "***", text)
    text = redact_text(text)
    text = sanitize(text)
    return text[:max(0, limit)]


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(value, 0), _MAX_INT)


def _choice(value: Any, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "bilinmiyor"


def _ident(value: Any) -> str:
    return value if isinstance(value, str) and _ID_RE.fullmatch(value) else "?"


def _iso(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        return datetime.fromisoformat(value).isoformat(timespec="seconds")
    except ValueError:
        return ""


def _items(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _run(raw: Mapping) -> dict:
    files = []
    for f in _items(raw.get("files"))[:_MAX_FILES]:
        if isinstance(f, Mapping):
            files.append({"name": clean_text(f.get("name")), "size": _int(f.get("size"))})
    rejects_raw = raw.get("rejects") if isinstance(raw.get("rejects"), Mapping) else {}
    model_raw = raw.get("model") if isinstance(raw.get("model"), Mapping) else {}
    events = [clean_text(e) for e in _items(raw.get("events")) if isinstance(e, str)]
    return {
        "task_id": _ident(raw.get("task_id")),
        "project": clean_text(raw.get("project"), 60),
        "description": clean_text(raw.get("description"), _MAX_DESC),
        "status": _choice(raw.get("status"), _RUN_STATUSES),
        "iteration": _int(raw.get("iteration")),
        "max_iterations": _int(raw.get("max_iterations")),
        "files": files,
        "rejects": {k: _int(rejects_raw.get(k)) for k in _REJECT_KEYS},
        "model": {"provider": _choice(model_raw.get("provider"), _PROVIDERS),
                  "name": clean_text(model_raw.get("name"), 60)},
        "events": events[-_MAX_EVENTS:],
        "reason": clean_text(raw.get("reason"), 60),
    }


def _summary_line(line: str) -> str:
    prefix = "Proje klasörü:"
    if line.startswith(prefix):
        # Ev dizini disinda olsa da klasorun yalnizca adi gosterilir.
        name = [p for p in re.split(r"[/\\]", line[len(prefix):].strip()) if p]
        return clean_text(f"{prefix} {name[-1] if name else ''}")
    return clean_text(line)


def _job(raw: Mapping) -> dict:
    result = raw.get("result") if isinstance(raw.get("result"), str) else ""
    summary = [_summary_line(line) for line in result.splitlines()
               if line.startswith(_SUMMARY_PREFIXES)][:_MAX_SUMMARY]
    return {
        "id": _ident(raw.get("id")),
        "description": clean_text(raw.get("description"), _MAX_DESC),
        "status": _choice(raw.get("status"), _JOB_STATUSES),
        "started_at": _iso(raw.get("started_at")),
        "finished_at": _iso(raw.get("finished_at")),
        "summary": summary,
    }


def build_panel_state(live_runs: Iterable[Any], jobs: Iterable[Any]) -> dict:
    """Saf donusum: girdiyi okur, yalnizca izinli alanlarla yeni bir sozluk
    dondurur (json.dumps ile dogrudan serilestirilebilir)."""
    runs = [_run(r) for r in _items(live_runs) if isinstance(r, Mapping)][:_MAX_RUNS]
    out_jobs = [_job(j) for j in _items(jobs) if isinstance(j, Mapping)][:_MAX_JOBS]
    return {"schema": SCHEMA_VERSION, "runs": runs, "jobs": out_jobs}


def collect_panel_state(job_timeout: float = 2.0) -> dict:
    """Canli kaynaklardan okur ve build_panel_state'e verir. Bir kaynak
    okunamazsa yalnizca sabit bir hata etiketi eklenir (istisna metni panoya
    gitmez); bu fonksiyon istisna firlatmaz."""
    errors: list[str] = []
    runs: list = []
    jobs: list = []
    try:
        from tools.developer import agentic_coder
        runs = agentic_coder.live_status()
    except Exception:
        errors.append("agentic_coder okunamadı")
    try:
        from jarvis.actions import agent_board
        jobs = agent_board.list_jobs(timeout=job_timeout)
    except Exception:
        errors.append("agent_board okunamadı")
    try:
        state = build_panel_state(runs, jobs)
    except Exception:
        state = {"schema": SCHEMA_VERSION, "runs": [], "jobs": []}
        errors.append("pano durumu üretilemedi")
    state["errors"] = errors
    return state
