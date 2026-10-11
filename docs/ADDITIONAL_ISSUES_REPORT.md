"""EKLENEN SORUNLAR (2026-10-05, İkinci Dalga).

Derin analiz raporundan sonra yapılan tarama ile 4 yeni sorun bulundu.
"""

## 9. CONCURRENT ONAY HANDLER KARIŞMASI

### 9.1 PROBLEM
```python
# main.py:1125 (Brain Team Onay)
def _handle_brain_team_approval_reply(self, text: str) -> bool:
    task_id = self._brain_pending_task_id
    # orch.approve(task_id) çağrısı

# main.py:1192 (Agent Loop Onay)
def request_agent_loop_approval(self, task_id: str, pending: dict, message: str) -> bool:
    self._set_pending_dangerous("agent_loop", fingerprint)
    self._agent_pending_task_id = task_id

# SORUN:
# - Değişkenler: _brain_pending_task_id ve _agent_pending_task_id AYRI
# - Lock: _confirmation_lock İKİSİ DE KILITLER
# - AMA: "evet" cevabı HANGI türe mi ait? USER YANLIŞ BİLİR!
```

**Senaryo**:
```
T=0: brain_orchestrator "Dosya sil?" → _brain_pending_task_id = "task123"
T=1: agent_loop AYNI ANDA "Komutu çalıştır?" → _agent_pending_task_id = "task456"
T=2: Kullanıcı "evet" diyor
T=3: main.py → _handle_brain_team_approval_reply()
   → orch.approve("task123")  ← YANLIŞ GÖREV ONAYLANDI!
   → Dosya SİLİNDİ, komutu değil!
```

**ŞİDDETİ**: 🔴 **KRİTİK** (Yanlış görev onaylanır)

---

## 10. EXCEPTION RECOVERY EKSIKLIKLERI

### 10.1 JSON CORRUPTION SENARYOSU

```python
# approval_registry.py:_load()
try:
    data = json.loads(self.path.read_text())
    if isinstance(data, dict):
        return data
except Exception as e:
    self._logger.warning(f"Yükleme başarısız: {e}")
    return {}  # ← SAF OK, boş registry

# AMA: _save() sırasında CORRUPTION olursa?
# T1: _load() başarılı → registry = {...}
# T2: ProcessLockManager._save() crash edilirse
#     → JSON yazılı, ama TAMAMLANMADI (.tmp hala EXISTS)
# T3: Sonraki _load() hangi dosyayı okur?
#     - .tmp mi? (unfinished JSON)
#     - eski approval_registry.json mi? (stale)
#     - İKİSİ DE corrupt mi?
```

**SORUN**: approval_registry.py ve process_lock.py, tempfile cleanup'ı guaranteed etmiyor:
```python
Path(temp_name).replace(self.path)  # ← Atomic mi? ❌
# Windows'ta: LockError olabilir
# .tmp file kalabilir
```

**ŞİDDETİ**: 🟠 **YÜKSEK** (Veri kaybı, stale state)

---

## 11. EXPERIMENTS.JSON vs BRAIN_TASKS.JSON ISOLATION KAYMASI

### 11.1 GÜNCEL DURUM

```python
# experiment_task_manager.py (sandbox deneyler)
EXPERIMENTS_PATH = _HERE / "experiments.json"  # ← SANDBOX KLASÖRÜNDE
_lock = threading.RLock()  # ← KENDİ LOCK

# task_manager.py (gerçek görevler)
TASKS_PATH = tasks_dir() / "brain_tasks.json"  # ← DATA_DIR
_lock = threading.RLock()  # ← KENDİ LOCK
```

### 11.2 PROBLEM: PAYLAŞIMLI RESOURCE YALNIZCA TEMIZLEMELER

```
Scenario: Memory Corruption Transition
─────────────────────────────────────

T=0: VirtualOrchestrator.run_experiment()
   └─ ExperimentTaskManager.update("EXP-20261005-001", payload={...})
      └─ experiments.json YAZILIR
      └─ _lock (RLock #1)

T=1: brain_orchestrator._tick()
   └─ BrainOrchestrator.tasks = TaskManager
      └─ task_manager.update("abc123", ...)
      └─ ProcessLockManager.acquire()  ← PATH: BASE_DIR/config/queues_lock.json
      └─ _lock (RLock #2) ← AYRI LOCK

T=2: Kullanıcı AYNI ANDA "sandbox'ta deney yap + dosya sil" isterse
   └─ main.py → intent_router → file_modification
      └─ task_manager.create() → ADD TO brain_tasks.json
      └─ _lock (RLock #3) ← AYRI LOCK
      └─ ProcessLockManager.acquire() ← CONFLICT?

T=3: Sandbox komutu çalışırsa (self_improve/virtual_orchestrator.py)
   └─ Diskte yapılan değişiklikleri ExperimentTaskManager'a yazarsa
      └─ Dosya paylaşımı? (experiments.json vs brain_tasks.json)
      └─ Process lock 2 farklı path'e mi bakıyor?
```

**SORULAR**:
1. ProcessLockManager AYRI instances mı? (Singleton mi?)
2. Aynı path'e concurrent write olunca ne oluyor?
3. Sandbox deney bir dosyayı değiştirirse, gerçek task bunu bilir mi?

**ŞİDDETİ**: 🟠 **YÜKSEK** (Silent data race)

---

## 12. TTL / EXPIRY TIMING ISSUES

### 12.1 RACE CONDITION

```python
# approval_registry.py:register_request()
with self._lock:
    registry = self._load()
    for req in registry.values():
        if req.is_valid(now):  # ← TTL CHECK
            return code  # ← IDEMPOTENT

    # Yeni isteği kaydet
    code = secrets.token_hex(3)
    request = ApprovalRequest(
        issued_at=now,
        expires_at=now + ttl_seconds,  # ← TTL SET
    )
    registry[code] = request
    self._save(registry)

# ARADA: ProcessLockManager.heartbeat() yapılıyor mu?
# Eğer heartbeat yapılmıyorsa → ProcessLock STALE olabilir
```

### 12.2 CLOCK SKEW PROBLEMI

```python
# approval_registry.py
import time
now = time.monotonic()  # ← Sistem saati
expires_at = now + 60

# Aynı anda:
# ProcessLockManager heartbeat() diğer saati kullanıyor mu?
# task_manager'ın datetime.now().isoformat() vs time.monotonic()
```

**ŞİDDETİ**: 🟡 **ORTA** (Nadir, sistem saati atlı anda tetiklenir)

---

## ÖZET — 4 YENİ BULGU

| # | BULGU | ŞİDDETİ | YENIDEN ÖZET |
|---|-------|---------|-------|
| 9 | Concurrent onay handler karışması | 🔴 KRİTİK | Yanlış görev onaylanabilir |
| 10 | Exception recovery eksiklikleri | 🟠 YÜKSEK | Veri kaybı, stale state |
| 11 | Experiments vs brain_tasks isolation | 🟠 YÜKSEK | Silent data race |
| 12 | TTL/expiry timing issues | 🟡 ORTA | Nadir ama crash potansiyeli |

---

## TOPLAM İSTATİSTİKLER

✅ **İlk Rapor**: 8 bulgu
✅ **Bu Rapor**: 4 yeni bulgu
✅ **TOPLAM**: 12 sorun

### Şiddete Göre Dağılım
- 🔴 **KRİTİK**: 4 (1, 2, 3, 9)
- 🟠 **YÜKSEK**: 5 (4, 5, 6, 10, 11)
- 🟡 **ORTA**: 2 (7, 12)
- 🟢 **DÜŞÜK**: 1 (8)
"""
