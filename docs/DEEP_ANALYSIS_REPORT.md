"""DERIN ANALIZ RAPORU: ApprovalRegistry + ProcessLock + QueueConsistency Entegrasyonu.

TARİH: 2026-10-05
KONU: 5 yeni modülün gerçek uygulamada çalışıp çalışmayacağının analizi

═════════════════════════════════════════════════════════════════════════════

## 1. APPPROVALREGISTRY ENTEGRASYONU — FLOW ANALYSIS

### 1.1 GÜNCEL DURUM (Repo incelemesi)
- main.py: _JARVIS2_REGISTRY_AVAILABLE flag'i ve _gate.PendingSlotAdapter sistemi var
- dev_agent.py: Hala _pending_dev_agent dict'i kullanıyor (approval_registry.py yazıldı)
- brain_orchestrator.py: request_brain_team_approval() hala eski hook sistemi kullanıyor

### 1.2 PROBLEM: ApprovalRegistry ile main.py Entegrasyonu KOPUK

**KRİTİK BULGU**: main.py'nin onay sistemi (_gate) ile approval_registry.py **paralel** çalışıyor:

```python
# main.py (_gate sistemi — mevcut)
_decision = _gate.authorize(name, args)  # ← SECURITY_GATE'İN KENDİ VERDİĞİ KARAR
_grant = _store.take_grant(_decision)    # ← PENDING_SLOT'TAN ONAY ALIR

# approval_registry.py (YENİ — entegre edilmedi)
code = registry.register_request(...)    # ← AYRI BİR REGISTRY
request = registry.confirm(code)         # ← AYRI BİR ONAY SİSTEMİ

# DEĞİŞENİ: AYNI ONAY İKİ YERDE KAYDEDILIYOR!
```

**SENKRONIZASYON HATASI**:
1. dev_agent çağrısı → main.py security_gate authorize() → Effect.EXECUTE
2. dev_agent.get_confirmation_code() → ApprovalRegistry.register() (AYRI kod)
3. Kullanıcı "evet" → main.py _handle_agent_loop_reply() → approval_service.cancel()
4. ⚠️ DEV_AGENT'İN ApprovalRegistry KODU HIÇBIR ZAMAN CONFIRM() ÇAĞRILMIYOR!

**SONUÇ**: dev_agent onay kodu Gemini'ye geri verilse de, kullanıcı "evet" dediğinde 
ApprovalRegistry.confirm() asla çağrılmaz → proje başlamaz.

---

## 2. PROCESS LOCK VS THREADING RLOCK ÇATIŞMASI

### 2.1 GÜNCEL DURUM
- task_manager.py: threading.RLock() (_lock = threading.RLock())
- ProcessLockManager: PID-based lock (new)
- ExperimentTaskManager: ayrı threading.RLock()

### 2.2 PROBLEM: İkili Kilit Mekanizması

**Senaryo**:
```
T=0: Worker thread brain_orchestrator._tick()
  → task_manager.update() çağrısı
  → _lock.acquire() (threading.RLock) ✅
  → ProcessLockManager.acquire() çağrısı ← İKİ LOCK!

T=1: ExperimentTaskManager sandbox'tan
  → experiments.json yazma
  → AYNI process_lock'u tutuyor mu?
  → Yoksa ayrı bir lock file'a bakıyor mu?
```

**ÇATIŞMA**: ProcessLockManager PATH'ı:
```python
# approval_registry.py (LINE 41)
APPROVAL_REGISTRY_PATH = BASE_DIR / "config" / "approval_registry.json"

# process_lock.py (LINE 19)
QUEUES_LOCK_PATH = BASE_DIR / "config" / "queues_lock.json"

# ✅ paths.py (ACTUAL DATA DIR)
def config_dir() -> Path:
    return _sub("config")  # ← DATA_DIR içine yazılır!

# ❌ MISMATCH: BASE_DIR vs data_dir()
# approval_registry.py ve process_lock.py, paths.py'yi KULLANIYOR MU?
```

**ÖNEMLİ**: approval_registry.py ve process_lock.py, `paths.config_dir()` çağırmıyor!
Bunun yerine:
```python
BASE_DIR = _get_base_dir()  # ← KOD KLASÖRÜ (kaynak dizini)
APPROVAL_REGISTRY_PATH = BASE_DIR / "config"  # ← KOD KLASÖRÜNDE!
```

**SONUÇ**: Dosyalar KOD DIZİNİNE yazılmaya çalışılır — write permission hatası!
(Özellikle Windows "Program Files"'da kurulu veya freezed binary'de)

---

## 3. PAYLOAD RESTORE MEKANIZMASININ KALITESI

### 3.1 GÜNCEL KOD (task_manager.py:170-176)
```python
if lost:
    # RESTORE: Kayıp keys'ler eski payload'dan geri koy
    for key in lost:
        fields["payload"][key] = t["payload"][key]
    _logger.info(f"📋 Kaybolan payload keys restore edildi: {sorted(lost)}")
```

### 3.2 PROBLEM: Restore Sonrası Tekrar Kaybı

**Senaryo**:
```
1. Task payload: {"plan": [...], "step_index": 5, "history": [...]}
2. task_manager.update() çağrısı AYRI yerden:
   fields = {"payload": {"history": [], "step_index": 5}}  ← plan ÇIKTI
3. restore() çalışıyor:
   fields["payload"]["plan"] = t["payload"]["plan"]  ← restore edildi
4. AMA: _save() öncesinde brain_orchestrator._finish_step():
   t.update(fields)  ← TAMAMLAMA ÖNCESİ BAŞKA BİR THREAD
   t["payload"]["plan"] = []  ← TEKRAR SİLİNDİ
```

**ROOT CAUSE**: task_manager.update() ve brain_orchestrator._finish_step() 
AYNI `t` (task dict) referansını değiştiriyor → race condition.

**ÇÖZÜM EXİSTSMİ**: ❌ HAYIR. task_manager.py'de restore SADECE:
- `fields` dict'i kurtarıyor (yeni veri)
- `t` dict'i kurtarmıyor (mevcut bellekteki data)
- brain_orchestrator'ın kendi t.update() çağrısı, restore'u yok edebiliyor

---

## 4. CIRCULAR DEPENDENCY - DEADLOCK RİSKİ

### 4.1 DEPENDENCY CHAIN

```
A: ApprovalRegistry._save()
   → tempfile write → ProcessLockManager.heartbeat() gerekli mi? HAYIR
   → JSON dump → OK

B: ProcessLockManager._save()
   → tempfile write → QueueConsistencyChecker gerekli mi? HAYIR
   → JSON dump → OK

C: QueueConsistencyChecker.safe_load_json()
   → Path.read_text()
   → JSON parse
   → ✅ BAĞIMSIZ

D: task_manager._save()
   → ProcessLockManager.heartbeat()  ← HEARTBEAT
   → JSON dump
   → ✅ OK

CIRCULAR OLUP OLMADIĞINI KONTROL:
- A → B: ❌ YOK
- B → A: ❌ YOK
- C → A, B: ❌ YOK
- D → C: ✅ VAR (QueueConsistencyChecker optional)

✅ SONUÇ: Circular dependency YOK
```

### 4.2 FAKAT: Optional Import Hataları

```python
# task_manager.py (satırlar 41-50)
try:
    from jarvis.core.process_lock import get_process_lock
    self._process_lock = get_process_lock()
except Exception:
    self._process_lock = None  # ← SESSIZ BAŞARISIZLIK
```

**PROBLEM**: Import başarısız olursa → process_lock ASLA çalışmaz
→ eksik güvenlik, ama crash değil (degrate mode)

---

## 5. REAL SCENARIO — FULL FLOW RACE CONDITION

### 5.1 SENARYO: "Python Calculator Yaz"

```
T=0:00
├─ Kullanıcı: "Python calculator yaz"
├─ Gemini → _execute_tool("dev_agent", {description: "...calculator..."})
├─ security_gate.authorize() → Effect.EXECUTE ✅
└─ main.py:2343 → dev_agent() çağrısı başlıyor

T=0:01
├─ dev_agent.get_confirmation_code(description)
│  └─ ApprovalRegistry.register() → "ABC123"
│     (APPROVAL_REGISTRY_PATH'a yazılıyor — BASE_DIR/config/!!!)
├─ dev_agent sonuç döner: "Onay kodu: ABC123"
└─ Gemini bu kodu kullanıcıya bildiriyor

T=0:02
├─ Kullanıcı: "evet"
├─ main.py → _apply_spoken_confirmation()
├─ main.py → _handle_agent_loop_reply() ← YANLIŞ HANDLER!
│  (bu agent_loop onayı için, dev_agent değil)
├─ approval_service.cancel() ← dev_agent kodu TEMIZLENMIYOR
└─ brain_orchestrator AYNI ANDA:
   └─ _tick() → task update
      └─ task_manager.update() → process_lock.heartbeat()

T=0:03
├─ dev_agent resume ediyor:
│  └─ confirm_project_approval("ABC123")
│     └─ ApprovalRegistry.confirm("ABC123")
│        └─ "not_found" (main.py temizlemedi, ama registry'de hala VAR!)
├─ dev_agent hata döner: "ONAY HENÜZ ALINMADI"
└─ ❌ PROJE BAŞLAMIYOR
```

**KAZI**: ApprovalRegistry ile main.py onay sistemi **senkron değil**.

---

## 6. TEST COVERAGE ANALYSIS

### 6.1 YAZILAN TESTLER
```
✅ approval_registry.py:
   - __main__ → temel register/confirm/status testleri
   - fixture'lar AYIR DOSYADA YOK

❌ process_lock.py:
   - __main__ → acquire/release/heartbeat
   - Stale detection testi YOK
   - Windows vs Linux path handling testi YOK

❌ queue_consistency.py:
   - __main__ → checkpoint/payload_loss testi
   - JSON corruption real scenario testi YOK
   - ExperimentTaskManager entegrasyon testi YOK

❌ task_manager.py entegrasyon:
   - payload restore + concurrent access testi YOK
   - process_lock heartbeat testi YOK
   - race condition testi (task update during payload restore) YOK
```

### 6.2 YAZILMAMIS TESTLER (KRİTİK)

```python
# ❌ test_approval_registry_concurrent.py
# - T1: register("calculator_v1")
# - T2: AYNI ANDA register("calculator_v1") ← aynı code döner mi?
# - T3: confirm(code)
# - ⚠️ Race: heartbeat expire arada mı?

# ❌ test_process_lock_stale_detection.py
# - T1: acquire(pid=1234)
# - T2: PID 1234 "kill" et (process dead)
# - T3: acquire(pid=5678) ← stale lock devralır mı?
# - ⚠️ Windows'ta OpenProcess + CloseHandle race?

# ❌ test_task_manager_payload_restore.py
# - task: {"plan": [...], "step_index": 5}
# - update({"payload": {"step_index": 5}}) ← plan çıktı
# - restore çalıştı
# - brain_orchestrator._finish_step() t.update() yaptı
# - plan TEKRAR kayıp mı?

# ❌ test_dev_agent_approval_registry_flow.py
# - get_confirmation_code("calculator")
# - ← ApprovalRegistry register()
# - ← main.py _gate.authorize() → approval_service.cancel()
# - ← confirm_project_approval(code) ← NOT_FOUND mı?
```

---

## 7. YAPILANDIRMA — PATH TUTARLILIĞI

### 7.1 GÜNCEL PATH USAGE

```python
# paths.py (DOĞRU)
def data_dir() -> Path:
    env = os.environ.get("JARVIS_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    # Platform-specific veri klasörü (Windows: %LOCALAPPDATA%/MuratJARVIS)

def config_dir() -> Path:
    return _sub("config")  # ← data_dir() içine yaz

# approval_registry.py (❌ YANLIŞ)
BASE_DIR = _get_base_dir()  # ← KOD KLASÖRÜ
APPROVAL_REGISTRY_PATH = BASE_DIR / "config" / "approval_registry.json"
# Yazma hakkı olmayan yere yazılabilir!

# process_lock.py (❌ YANLIŞ)
QUEUES_LOCK_PATH = BASE_DIR / "config" / "queues_lock.json"
# Yazma hakkı olmayan yere yazılabilir!

# task_manager.py (✅ DOĞRU)
TASKS_PATH = tasks_dir() / "brain_tasks.json"  # ← paths.tasks_dir() kullanıyor
```

### 7.2 FİX GEREKLI

```python
# approval_registry.py'de
from jarvis.paths import config_dir
APPROVAL_REGISTRY_PATH = config_dir() / "approval_registry.json"

# process_lock.py'de
from jarvis.paths import config_dir
QUEUES_LOCK_PATH = config_dir() / "queues_lock.json"
```

---

## 8. GERIYE UYUMLULUK — BREAKING CHANGES

### 8.1 MEVCUT SISTEM
- agent_loop.py: kendi onay sistemi var (_pending_approval_task)
- brain_orchestrator.py: request_brain_team_approval() hook
- main.py: security_gate + PendingSlotAdapter

### 8.2 YAZILAN SISTEM
- ApprovalRegistry: merkezi registry
- ProcessLockManager: deadlock koruma
- QueueConsistencyChecker: payload integrity

### 8.3 UYUMLULUK KONTROL

```python
# ✅ task_manager.py'de ProcessLockManager optional
if self._process_lock:
    self._process_lock.heartbeat()
else:
    pass  # Eski davranış

# ✅ task_manager.py'de QueueConsistencyChecker optional
if self._consistency:
    self._consistency.checkpoint_task()
else:
    pass  # Eski davranış

# ❌ main.py - ApprovalRegistry entegrasyonu YAPILMADI
# - approval_registry.py sadece dosya olarak yazıldı
# - main.py'ye get_confirmation_code() çağrısı EKLENMEDI
# - dev_agent.py'de onay kodu üretme EKLENMEDI
# - main.py'de onay kodu doğrulama EKLENMEDI
# → Breaking change potansiyeli: Mevcut dev_agent onay sistemi çalışır mı?
```

---

## ÖZET — 8 BULGU

| # | BULGU | ŞİDDETİ | FİKS GEREKLI Mİ |
|---|-------|---------|-----------------|
| 1 | ApprovalRegistry ↔ main._gate senkron değil | 🔴 KRİTİK | ✅ EVET |
| 2 | PATH: BASE_DIR yerine data_dir() kullanılmadı | 🔴 KRİTİK | ✅ EVET |
| 3 | Payload restore + concurrent access race condition | 🔴 KRİTİK | ✅ EVET |
| 4 | ProcessLockManager optional import sessiz başarısız | 🟠 YÜKSEK | ✅ EVET |
| 5 | ExperimentTaskManager process_lock entegrasyonu YOK | 🟠 YÜKSEK | ✅ EVET |
| 6 | dev_agent onay kodu main._gate ile bağlanmadı | 🟠 YÜKSEK | ✅ EVET |
| 7 | Test coverage kritik senaryoları kaçırıyor | 🟡 ORTA | ✅ EVET |
| 8 | Circular dependency YOK ama graceful degradation VAR | 🟢 DÜŞÜK | ✓ OPTIONAL |

---

## ÖNERİ: UYGULAMA SIRASI

1️⃣ PATH PROBLEMI FİX (approval_registry, process_lock)
2️⃣ ApprovalRegistry ↔ main._gate Entegrasyonu
3️⃣ dev_agent + brain_orchestrator entegrasyonu
4️⃣ Concurrent test suite
5️⃣ ProcessLockManager ↔ ExperimentTaskManager
6️⃣ Integration tests
"""
