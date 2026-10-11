"""experiment_task_manager.py — SANAL BEYİN DENEY KUYRUĞU (AŞAMA B).

"JARVIS — FAZ 9 Uyumlu Sanal Beyin / Digital Twin Geliştirme Promptu"
belgesinin §10 (Task Manager Uyumu) ve §11 (Deney Kimliği) maddelerinin
uygulanışı.

NEDEN core/task_manager.py'nin KENDİSİ DEĞİL, AYRI BİR DOSYA:
core/task_manager.TaskManager, core/brain_orchestrator.py'nin arka plan
döngüsü (_worker_loop -> _tick) tarafından her ~20 saniyede bir okunan
tasks/brain_tasks.json'u yönetiyor - o dosyaya eklenen HER "pending" görev,
gerçek Orchestrator tarafından OTOMATİK OLARAK ÇALIŞTIRILIR (gerçek
research_ai/coder_ai/executor_ai çağrılır, coder_ai GERÇEK dosyaları
değiştirebilir). Sanal Beyin'in deneyleri bu dosyaya eklenirse, "deney"
olmaktan çıkıp gerçek, denetimsiz görevlere dönüşür - tam da bu belgenin
§14 ve §43'te engellemeye çalıştığı şey.

Bu yüzden ExperimentTaskManager, core.task_manager.TaskManager ile AYNI
arayüzü (create/get/update/list) ve AYNI ispatlanmış atomik-yazma desenini
(tempfile + Path.replace) kullanır - YENİ bir mekanizma icat edilmedi -
ama TAMAMEN AYRI bir dosyaya (bu modülle aynı klasördeki experiments.json)
yazar. Gerçek Orchestrator bu dosyanın varlığından haberdar değildir ve
onu asla okumaz/çalıştırmaz.

Durumlar (§10, core/task_manager.VALID_STATUSES + belgenin eklediği
"rollback"):
    pending, running, waiting_approval, completed, failed, cancelled, rollback
"""
from __future__ import annotations

import json
import logging
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from jarvis.core.p0_critical_fixes import VirtualBrainStateManager

VALID_STATUSES = {
    "pending", "running", "waiting_approval", "completed",
    "failed", "cancelled", "rollback",
}

_HERE = Path(__file__).resolve().parent
EXPERIMENTS_PATH = VirtualBrainStateManager.get_experiments_path()
LOGS_DIR = VirtualBrainStateManager.get_experiments_path().parent / "logs"
_lock = threading.RLock()


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("jarvis.virtual_brain.experiment_task_manager")
    if not logger.handlers:
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(LOGS_DIR / "experiment_task_manager.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
        except Exception:
            pass
    return logger


_logger = _make_logger()


def _next_experiment_id(existing: list[dict]) -> str:
    """§11 formatı: EXP-YYYYMMDD-NNN (o gün için artan sıra numarası)."""
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"EXP-{today}-"
    seq = 0
    for t in existing:
        eid = (t.get("payload") or {}).get("experiment_id", "")
        if eid.startswith(prefix):
            try:
                seq = max(seq, int(eid[len(prefix):]))
            except ValueError:
                pass
    return f"{prefix}{seq + 1:03d}"


class ExperimentTaskManager:
    """core.task_manager.TaskManager ile AYNI arayüz (create/get/update/list),
    AYNI atomik-yazma deseni - ama tamamen ayrı bir dosyada. Gerçek
    Orchestrator/TaskManager bundan haberdar değildir."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or VirtualBrainStateManager.get_experiments_path()

    def _load(self) -> list[dict]:
        try:
            if self.path.is_file():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data
        except Exception as e:
            _logger.warning(f"{self.path.name} okunamadı: {e}")
        return []

    def _save(self, tasks: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=self.path.parent, delete=False, encoding="utf-8", suffix=".tmp",
        ) as tmp:
            json.dump(tasks, tmp, indent=2, ensure_ascii=False)
            temp_name = tmp.name
        Path(temp_name).replace(self.path)

    def create(self, name: str, agent: str = "virtual_orchestrator",
               priority: str = "medium", payload: dict | None = None) -> dict:
        """payload; §10'da sayılan opsiyonel alanları taşıyabilir:
        experiment_id, proposal_id, sandbox_path, security_level,
        benchmark_result, audit_result, approval_status. experiment_id
        verilmezse otomatik EXP-YYYYMMDD-NNN üretilir."""
        with _lock:
            tasks = self._load()
            payload = dict(payload or {})
            payload.setdefault("experiment_id", _next_experiment_id(tasks))
            payload.setdefault("history", [])

            now = datetime.now().isoformat()
            task = {
                "id": payload["experiment_id"],
                "name": name,
                "agent": agent,
                "priority": priority,
                "status": "pending",
                "created_at": now,
                "updated_at": now,
                "retry_count": 0,
                "result": None,
                "error": None,
                "payload": payload,
            }
            tasks.append(task)
            self._save(tasks)
            _logger.info(f"Deney oluşturuldu: {task['id']} — {name[:80]!r}")
            return task

    def get(self, task_id: str) -> dict | None:
        for t in self._load():
            if t["id"] == task_id:
                return t
        return None

    def update(self, task_id: str, **fields) -> dict | None:
        with _lock:
            tasks = self._load()
            for t in tasks:
                if t["id"] == task_id:
                    if "status" in fields and fields["status"] not in VALID_STATUSES:
                        raise ValueError(f"Geçersiz durum: {fields['status']}")
                    t.update(fields)
                    t["updated_at"] = datetime.now().isoformat()
                    self._save(tasks)
                    return t
            _logger.warning(f"update(): '{task_id}' id'li deney bulunamadı.")
            return None

    def list(self, status: str | None = None) -> list[dict]:
        tasks = self._load()
        if status:
            tasks = [t for t in tasks if t["status"] == status]
        return tasks
