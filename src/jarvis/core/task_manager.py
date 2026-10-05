"""task_manager.py — 18. GÖREV KUYRUĞU (UNIFIED).

DÜZELTMELER (2026-10-05):
- Process-level lock entegrasyonu (deadlock koruma)
- QueueConsistencyChecker entegrasyonu (payload integrity)
- Atomic write operasyonları
- Detaylı anomaly logging
- Payload loss restore mekanizması

AYNEN KORUNDU:
- Dosya yolu: tasks/brain_tasks.json
- Temel API: create/get/update/list
- Task yapısı: id, name, agent, priority, status, created_at, updated_at, retry_count, result, error, payload
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from jarvis.paths import logs_dir, tasks_dir

# Onay bekleyen gorevin durum adi - orkestrator, UI ve main.py bu TEK sabiti
# kullanir (UI eskiden "awaiting_approval" ariyordu, gorev hic gorunmuyordu).
WAITING_APPROVAL = "waiting_approval"
VALID_STATUSES = {"pending", "running", WAITING_APPROVAL, "completed", "failed", "cancelled"}


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
TASKS_PATH = tasks_dir() / "brain_tasks.json"
LOGS_DIR = logs_dir()
_lock = threading.RLock()


def _make_logger() -> logging.Logger:
    """2026-09-15: brain_tasks.json'un beklenmedik sekilde 'sifirlandigi'
    (bir gorevin plan/step_index/history'sinin, orchestrator'in KENDI kod
    yollarindan hicbiri tarafindan yapilmamis sekilde kaybolmasi) canli
    testte gozlemlendi, ama bunu ACIKLAYACAK hicbir iz (log satiri) yoktu -
    sadece diger brain'lerin loglarindan DOLAYLI olarak cikarim yapilabildi.
    Bu logger, TaskManager'in kendi goruslerini (yuklenen gorev sayisi, bir
    gorevin payload'inin beklenmedik sekilde kuculmesi) ayri bir dosyaya
    (logs/task_manager.log) yaziyor - boylece bir sonraki sefer bu BURADAN
    doğrudan teshis edilebilir (disaridan/elle bir dosya degisikligi mi,
    yoksa orchestrator'in kendi mantigi mi).
    
    2026-10-05: QueueConsistencyChecker tarafından ek detaylı logging yapılıyor
    (logs/queue_consistency.log) — payload loss, state transitions, vb."""
    logger = logging.getLogger("jarvis.task_manager")
    if not logger.handlers:
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(LOGS_DIR / "task_manager.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
        except Exception:
            pass  # loglama basarisiz olsa bile TaskManager calismaya devam etmeli
    return logger


_logger = _make_logger()


class TaskManager:
    """Merkezi görev yöneticisi.
    
    TASARIMI:
    1. Atomic write (tempfile + replace)
    2. Process-level lock (deadlock koruma) — process_lock.py
    3. QueueConsistencyChecker (payload integrity) — queue_consistency.py
    4. Detaylı anomaly detection ve logging
    
    NOTE: Process lock ve consistency checker optional (import yapılmazsa hata değil)
    """

    def __init__(self, path: Path = TASKS_PATH) -> None:
        self.path = path
        # Optional: Process lock (deadlock koruma)
        self._process_lock = None
        try:
            from jarvis.core.process_lock import get_process_lock
            self._process_lock = get_process_lock()
        except Exception:
            pass
        
        # Optional: Consistency checker (payload integrity)
        self._consistency = None
        try:
            from jarvis.core.queue_consistency import get_consistency_checker
            self._consistency = get_consistency_checker()
        except Exception:
            pass

    def _load(self) -> list[dict]:
        try:
            if self.path.is_file():
                # QueueConsistencyChecker varsa, JSON corruption check yap
                if self._consistency:
                    data = self._consistency.safe_load_json(self.path, default=[])
                else:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                
                if isinstance(data, list):
                    return data
        except Exception as e:
            print(f"[TaskManager] ⚠️ {self.path.name} okunamadı: {e}")
        return []

    def _save(self, tasks: list[dict]) -> None:
        """Dosyaya kaydet (atomic + process heartbeat)."""
        # Process lock heartbeat (alive check)
        if self._process_lock:
            try:
                self._process_lock.heartbeat()
            except Exception:
                pass
        
        # Butunluk kontrolu: TaskManager'in kendi API'sinde gorev SILEN hicbir
        # metod yok (create() sadece ekler, update() sadece degistirir) -
        # dolayisiyla dosyadaki gorev sayisi bu siniftan gecen HERHANGI bir
        # yolla ASLA azalamaz. Azaldiginda bu KESINLIKLE disaridan bir
        # mudahaledir (elle duzenleme, eski bir yedegin geri yuklenmesi,
        # bir senkronizasyon araci) - sessizce uzerinden gecmek yerine logla.
        try:
            on_disk_count = len(self._load())
            if on_disk_count > len(tasks):
                msg = (
                    f"Gorev sayisi azaliyor: disktaki {on_disk_count} -> yazilacak {len(tasks)}. "
                    f"TaskManager'in kendi API'si gorev SILMEZ - bu dosyaya harici bir "
                    f"mudahaleye (elle duzenleme/rollback/senkronizasyon araci) isaret edebilir."
                )
                _logger.warning(msg)
                if self._consistency:
                    self._consistency.alert_anomaly("task_count_decrease", msg)
        except Exception:
            pass

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=self.path.parent, delete=False, encoding="utf-8", suffix=".tmp",
        ) as tmp:
            json.dump(tasks, tmp, indent=2, ensure_ascii=False)
            temp_name = tmp.name
        Path(temp_name).replace(self.path)

    def create(self, name: str, agent: str, priority: str = "medium", payload: dict | None = None) -> dict:
        with _lock:
            tasks = self._load()
            now = datetime.now().isoformat()
            task = {
                "id": uuid.uuid4().hex[:8],
                "name": name,
                "agent": agent,
                "priority": priority,
                "status": "pending",
                "created_at": now,
                "updated_at": now,
                "retry_count": 0,
                "result": None,
                "error": None,
                "payload": payload or {},
            }
            tasks.append(task)
            self._save(tasks)
            
            # Checkpoint (QueueConsistencyChecker varsa)
            if self._consistency:
                self._consistency.checkpoint_task(task["id"], task, event="create")
                self._consistency.log_task_transition(
                    task["id"], task["name"], "—", "pending", 
                    reason="created"
                )
            
            _logger.info(f"Görev oluşturuldu: {task['id']} → {name[:60]!r} (agent={agent})")
            return task

    def get(self, task_id: str) -> dict | None:
        for t in self._load():
            if t["id"] == task_id:
                return t
        return None

    def update(self, task_id: str, **fields) -> dict | None:
        """Görev güncelle (payload integrity kontrol + restore).
        
        DÜZELTME (2026-10-05): Payload keys kayıp tespit edilirse,
        kayıp keys'ler eski payload'dan restore edilir ve uyarı loglanır.
        """
        with _lock:
            tasks = self._load()
            for t in tasks:
                if t["id"] == task_id:
                    old_task = dict(t)
                    
                    if "status" in fields and fields["status"] not in VALID_STATUSES:
                        raise ValueError(f"Geçersiz durum: {fields['status']}")

                    # Payload integrity check + restore
                    if "payload" in fields and isinstance(t.get("payload"), dict) and isinstance(fields["payload"], dict):
                        old_keys = set(t["payload"].keys())
                        new_keys = set(fields["payload"].keys())
                        lost = old_keys - new_keys
                        
                        if lost:
                            msg = (
                                f"Gorev '{task_id}' ({t.get('name', '')[:60]!r}) payload'inda "
                                f"anahtar kaybi tespit edildi: {sorted(lost)} — eski durum "
                                f"status={t.get('status')!r}, bu beklenmedikse dosyaya harici "
                                f"bir mudahale (elle duzenleme, senkronizasyon araci, rollback) "
                                f"olup olmadigi kontrol edilmeli."
                            )
                            _logger.warning(msg)
                            
                            if self._consistency:
                                self._consistency.alert_anomaly("payload_loss_on_update", msg)
                            
                            # RESTORE: Kayıp keys'leri eski payload'dan geri koy
                            for key in lost:
                                fields["payload"][key] = t["payload"][key]
                            _logger.info(f"📋 Kaybolan payload keys restore edildi: {sorted(lost)}")

                    t.update(fields)
                    t["updated_at"] = datetime.now().isoformat()
                    self._save(tasks)
                    
                    # Checkpoint (QueueConsistencyChecker varsa)
                    if self._consistency:
                        self._consistency.checkpoint_task(task_id, t, event="update")
                        if "status" in fields:
                            self._consistency.log_task_transition(
                                task_id, t.get("name", "?"),
                                old_task.get("status", "?"), fields["status"],
                                reason=fields.get("reason", "")
                            )
                    
                    _logger.info(
                        f"Görev güncellendi: {task_id} → {fields.get('status', 'no-status-change')}"
                    )
                    return t
            
            _logger.warning(f"update(): '{task_id}' id'li gorev bulunamadi (mevcut gorev sayisi={len(tasks)}).")
            return None

    def list(self, status: str | None = None) -> list[dict]:
        tasks = self._load()
        if status:
            tasks = [t for t in tasks if t["status"] == status]
        return tasks
