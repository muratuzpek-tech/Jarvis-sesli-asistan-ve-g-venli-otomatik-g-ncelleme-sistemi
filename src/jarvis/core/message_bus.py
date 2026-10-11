"""message_bus.py — 10. MESSAGE BUS.

Beyinler birbirleriyle DOĞRUDAN import edip çağırmak yerine (bu, hangi
beynin kime ne zaman görev verdiğini izlenemez hale getirir) bu ortak
otobüs üzerinden konuşur. Şema, kullanıcının talimatındaki örnekle
birebir aynı:
  istek:  {message_id, from, to, task, priority, created_at, payload}
  sonuç:  {message_id, from, to, status, result}

SÜREÇ MODELİ NOTU: Beyinler ayrı OS process'leri DEĞİL (bkz.
brains/base_brain.py'nin baş notu), bu yüzden burada gerçek bir ağ/IPC
kuyruğu yok - MessageBus senkron bir yönlendirici + değişmez bir denetim
günlüğü. İleride gerçek process ayrımına geçilirse, SADECE bu dosyanın
içi (send() metodunun gövdesi) değişir - beyinlerin/orchestrator'ın
arayüzü AYNI kalır."""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from pathlib import Path

from jarvis.brains.base_brain import make_message
from jarvis.paths import logs_dir


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


LOGS_DIR = logs_dir()
BUS_LOG_PATH = LOGS_DIR / "message_bus.jsonl"
_write_lock = threading.Lock()


class UnknownAgent(Exception):
    pass


class MessageBus:
    def __init__(self) -> None:
        self._agents: dict[str, object] = {}

    def register(self, brain) -> None:
        self._agents[brain.NAME] = brain

    @staticmethod
    def _masked(entry: dict) -> dict:
        """Diske yazilacak kopya: istek payload'i ve yanit sonucu
        core/audit_log.mask_params ile maskelenir (password/token/key/
        secret/parola ... "***", icerik alanlari yalnizca uzunluk). Bellekteki
        mesaj DEGISMEZ; yurutme gercek degerleri kullanir."""
        from jarvis.core.audit_log import mask_params, redact_text
        out = dict(entry)
        if isinstance(out.get("payload"), dict):
            out["payload"] = mask_params(out["payload"], limit=500)
        result = out.get("result")
        if isinstance(result, dict):
            out["result"] = mask_params(result, limit=500)
        elif isinstance(result, (list, tuple)):
            out["result"] = mask_params({"result": result}, limit=500)["result"]
        elif isinstance(result, str):
            out["result"] = redact_text(result)
        return out

    def _log(self, entry: dict) -> None:
        try:
            with _write_lock:
                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                with open(BUS_LOG_PATH, "a", encoding="utf-8") as f:
                    entry = {"timestamp": datetime.now().isoformat(), **self._masked(entry)}
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[MessageBus] ⚠️ message_bus.jsonl yazılamadı: {e}")

    def send(self, from_agent: str, to_agent: str, task: str, payload: dict | None = None,
              priority: str = "medium") -> dict:
        """Bir isteği ilgili beyne yönlendirir, sonucu döner. Hedef beyin
        bilinmiyorsa UnknownAgent fırlatır - orchestrator bunu yakalayıp
        planı 'unknown agent' olarak işaretler, ASLA rastgele bir beyne
        düşmez."""
        message = make_message(from_agent, to_agent, task, payload, priority)
        self._log({"direction": "request", **message})

        brain = self._agents.get(to_agent)
        if brain is None:
            raise UnknownAgent(f"'{to_agent}' kayıtlı bir beyin değil.")

        response = brain.call(message)
        self._log({"direction": "response", **response})
        return response
