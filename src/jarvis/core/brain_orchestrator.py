"""brain_orchestrator.py — 9. BRAIN ORCHESTRATOR.

Bütün beyinlerin yöneticisi. Kendisi bir "her işi yapan AI" DEĞİLDİR -
sadece hangi beynin çalışacağını belirler, görev gönderir, sonuçları
toplar, görev durumunu takip eder, hata durumlarını yönetir.

MİMARİ (agent_loop.py ile AYNI, kanıtlanmış desen - "25. İLK AŞAMA"
maddesindeki aşamalı yaklaşım yerine kullanıcı "hepsini tek seferde"
istedi, ama YÜRÜTME MODELİ agent_loop.py'nin ZATEN çalışan modeliyle
BİREBİR aynı tutuldu): brain_team_tool(action="start", ...) SADECE bir
görev kaydı oluşturur ve HEMEN döner - Jarvis'in canlı sesli oturumunu
ASLA bloklamaz. Gerçek iş, ayrı bir arka plan thread'inde (_worker_loop),
her turda (tick) SADECE TEK BİR ADIM ilerleyerek yürütülür.

PLANNER -> EXECUTOR ÇEVİRİSİ HAKKINDA BİLİNÇLİ BASİTLEŞTİRME (şeffaflık
için burada belirtiliyor): planner_ai doğal dilde adım açıklamaları
üretir ("YolPaylaş için rakip analizi yap" gibi), ama executor_ai SADECE
sabit, yapılandırılmış eylem adları kabul eder (_ALLOWED_ACTIONS).
Aradaki çeviriyi _infer_executor_action() yapıyor - basit anahtar kelime
eşleştirmesi. Bu, ilk faz için yeterli ama planner'ın yapılandırılmış
(function-calling tarzı) çıktı üretmesi 2. faz için doğal bir iyileştirme
olurdu.

RİSK KAPISI (14 + 15. bölümler): coder_ai'ye giden HER adım, açıklamada
riskli bir anahtar kelime geçen HER adım, KOŞULSUZ olarak HIGH risk
sayılır ve kullanıcı onayı beklemeden ASLA çalıştırılmaz. coder_ai'ye
gitmeden ÖNCE backup_tool.create_backup() ÇAĞRILIR (14. BACKUP KURALI).

FAZ 1 SAĞLAMLAŞTIRMASI (kullanıcı talimatı, 2026-09-15): aşağıdaki 8
özellik + 1 düzeltme eklendi:
  1. Timeout sistemi        -> brains/base_brain.py: _run_with_timeout()
  2. Heartbeat/health check -> brains/base_brain.py: heartbeat() + bu
                               dosyada get_team_health()
  3. Retry sistemi          -> zaten actions/resilience.py'de vardı
                               (call_with_resilience) - değişmedi
  4. Fallback (Gemini->Ollama) -> zaten actions/local_llm.py'de vardı
                               (generate_with_fallback) - değişmedi
  5. Task watchdog          -> core/watchdog.py (YENİ) - bkz. _tick()
  6. Result verification    -> _finish_step(): auditor'a gitmeden önce
                               deterministik "sahte tamamlanma" kontrolü
  7. User response guarantee -> _notify_result() - görev bittiğinde canlı
                               oturuma (varsa) VE kalıcı bir log dosyasına
                               yazılır, sessizce kaybolmaz
  8. Dead-task detection    -> core/watchdog.py (aynı modül, 5 ile birlikte)

DÜZELTME - MEMORY BAĞIMLILIĞI: Görev motoru (FAZ 1) Memory'nin ÇALIŞIYOR
OLMASINA bağımlı OLMAMALI - Memory sadece hata/retry geçmişi ve öğrenme
için YARDIMCI bir bileşen. Bu yüzden TÜM self.memory.remember_event(...)
çağrıları artık _safe_memory_event() üzerinden yapılıyor: Memory çağrısı
patlarsa SADECE loglanır, görev motorunun akışı ASLA durmaz/etkilenmez.

GÖREV ZİNCİRİ DÜZELTMESİ (kullanıcı onaylı analiz raporu, 2026-09-15,
"A+B+C"): canlı testte ve kod incelemesinde bulunan iki gerçek eksiklik
giderildi - YENİ bir agent/dosya YIĞINI EKLENMEDİ, sadece mevcut kopukluk
düzeltildi:
  A. UNRESOLVED_AGENT: "agent=unknown" adımlar önceden sessizce
     failed_steps'e düşüyordu ("hiç denenmedi" ile "denenip başarısız
     oldu" ayırt edilemiyordu). Artık ayrı bir payload["unresolved_steps"]
     listesine, NEDEN açıklamasıyla birlikte kaydediliyor ve görev
     sonucunda kullanıcıya AÇIKÇA bildiriliyor (bkz. _tick()).
  B. Gerçek doğrulama: auditor_ai'nin "passed" kararı TAMAMEN LLM'in metin
     yargısına dayanıyordu, dosya sistemi hiç sorgulanmıyordu (canlı
     testte bir "sahte başarı" tam olarak buradan kaynaklandı). Artık
     _finish_step() içinde, executor_ai + file_controller create/write
     adımları için BAĞIMSIZ, pathlib tabanlı bir ikinci kontrol var
     (_verify_file_action) - auditor "passed=True" dese BİLE, dosya
     gerçekten yoksa/içerik uyuşmuyorsa sonuç passed=False'a çevrilir
     (tersi asla olmaz). Ayrıca actions/file_controller.py'nin
     create_file/create_folder/write fonksiyonları artık kendi
     yazdıklarını AYNI çağrı içinde tekrar okuyup doğruluyor."""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path
from jarvis.paths import logs_dir

from jarvis.brains.planner_ai import PlannerAI
from jarvis.brains.research_ai import ResearchAI
from jarvis.brains.coder_ai import CoderAI
from jarvis.brains.security_ai import SecurityAI
from jarvis.brains.memory_ai import MemoryAI
from jarvis.brains.executor_ai import ExecutorAI
from jarvis.brains.auditor_ai import AuditorAI
from jarvis.core.message_bus import MessageBus
from jarvis.core.task_manager import TaskManager, TASKS_PATH, WAITING_APPROVAL
from jarvis.core import watchdog
from jarvis.actions import capability_resolver

DEFAULT_INTERVAL_S = 20.0
MAX_AUDIT_ROUNDS = 2   # ayni adim en fazla bu kadar kez tekrar denenir

# "6. Result verification": bir arastirma sonucunun ic metni acikca "sonuc
# yok" diyorsa (findings listesi BOS OLMASA BILE - bkz. asagidaki not),
# bunu auditor_ai'ye (yavas, ucretli bir LLM cagrisi) gondermeden ONCE
# yakalamak icin. NEDEN GEREKLI: auditor_ai.py'nin kendi on-kontrolu SADECE
# findings listesinin TAMAMEN BOS olup olmadigina bakiyor - ama gercek bir
# canli calistirmada gorduk ki bir research_ai sonucu findings=["No results
# found for: ..."] gibi, listesi DOLU ama ICERIGI aslinda "hicbir sey
# bulunamadi" diyen bir metin dondurebiliyor - bu durumda eski kontrol
# yanlislikla "bulgu var" sanip auditor'a gonderiyordu (auditor genelde
# yine de passed=False diyordu ama bu GARANTI degildi ve bosuna bir LLM
# cagrisi harciyordu).
_NO_RESULT_PATTERNS = (
    "no results found", "no results were found", "sonuç bulunamadı",
    "bulunamadı.", "hiçbir sonuç", "hiçbir şey bulunamadı",
)

# Aciklamada gecerse adimi KOŞULSUZ HIGH risk yapan anahtar kelimeler -
# security_ai.classify_risk'in (tool, action) tablosunun kapsayamadigi,
# planner'in serbest metin adimlarini yakalamak icin (bkz. dosya basi not).
_HIGH_RISK_KEYWORDS = (
    "sil", "delete", "kaldır", "kapat", "shutdown", "restart", "yeniden başlat",
    "gönder", "mesaj", "whatsapp", "telegram", "yükle", "kur ", "install",
    "rollback", "geri al", "şifrele", "encrypt", "decrypt", "şifresini çöz",
)

# DÜZELTME (2026-09-15 canlı test bulgusu): _infer_executor_action() ÖNCEDEN
# sadece yedek/backup, rollback, github kalıplarını tanıyordu - bunların
# dışındaki HER ŞEY ("dosya oluştur", "klasöre yaz", ...) sessizce
# file_controller/info'ya (salt-okunur, HİÇBİR ŞEY YARATMAYAN bir sorgu)
# düşüyordu. auditor_ai (6. Result verification) bunu doğru şekilde
# yakalayıp adımı başarısız sayıyordu (istenen tam olarak buydu) ama ALTTAKİ
# gerçek eksiklik - executor'ın dosya/klasör oluşturmayı/yazmayı hiç
# BİLMEMESİ - düzeltilmemiş kalıyordu. Aşağıdaki yardımcı, planner'ın serbest
# metninde geçen `test.txt` / 'Merhaba dünya.' gibi tırnak/backtick içindeki
# parçaları ayıklar: boşluksuz kısa bir parça DOSYA/KLASÖR ADI, boşluk içeren
# cümle gibi bir parça İÇERİK sayılır - basit ama planner'ın gözlemlenen
# gerçek çıktılarıyla (canlı testte görüldüğü gibi) uyumlu bir sezgisel kural.
_QUOTED_RE = re.compile(r"[`'\"]([^`'\"]+)[`'\"]")


def _extract_quoted(description: str) -> tuple[str, str]:
    matches = [m.strip() for m in _QUOTED_RE.findall(description) if m.strip()]
    if not matches:
        return "", ""
    if len(matches) == 1:
        # Tek tirnakli/backtick'li parca varsa eski sezgi aynen korunur:
        # icinde bosluk varsa ICERIK, yoksa AD sayilir (geriye donuk uyumlu).
        q = matches[0]
        return ("", q) if " " in q else (q, "")
    # DUZELTME (gercek canli hata, 2026-09-28, "jarvis_test_kaydi.txt 0
    # byte" bulgusu): iki (veya daha fazla) tirnakli/backtick'li parca
    # varsa, rol ARTIK ic boslugun olup olmamasina degil SIRAYA gore
    # belirleniyor - ilki AD, ikincisi ICERIK sayilir. Eski "bosluk var mi"
    # sezgisi, "JARVIS_DIRECT_WRITE_OK" gibi TEK KELIMELIK (bosluksuz) ama
    # GERCEK bir icerik oldugunda - bu projenin kendi HelloProof/
    # SQLiteProof/vb. testlerinde defalarca kullanilan TAM OLARAK bu bicimde
    # - onu SESSIZCE gormezden geliyordu: iki quoted parca da bosluksuzsa
    # (ör. `dosya.txt` ve `ICERIK_TEK_KELIME`), content hicbir zaman
    # dolmuyordu. Iki ayri parca acikca verildiginde hangisinin ad
    # hangisinin icerik oldugu zaten SIRADAN belli - bosluk kontrolüne
    # gerek yok.
    return matches[0], matches[1]


class BrainOrchestrator:
    def __init__(self) -> None:
        self.bus = MessageBus()
        self.tasks = TaskManager()
        self.planner = PlannerAI()
        self.research = ResearchAI()
        self.coder = CoderAI()
        self.security = SecurityAI()
        self.memory = MemoryAI()
        self.executor = ExecutorAI()
        self.auditor = AuditorAI()
        for brain in (self.planner, self.research, self.coder, self.security,
                      self.memory, self.executor, self.auditor):
            self.bus.register(brain)

        self._file_locks: dict[str, threading.Lock] = {}
        self._file_locks_guard = threading.Lock()
        self._loop_lock = threading.Lock()
        self._loop_started = False

        # "7. User response guarantee": brain_team_tool() bir 'player'
        # (canli oturumun UI/log baglantisi) ile cagrildiginda burada
        # saklanir - boylece ARKA PLAN thread'i (_worker_loop), bir gorev
        # cok sonra bittiginde, orijinal cagriyi yapan session hala
        # ayaktaysa ona geri bildirim verebilir. Yoksa (uygulama kapanmis,
        # player None) SADECE kalici log dosyasina yazilir - bkz. _notify_result.
        self._last_player = None
        self._results_logger = self._make_results_logger()

    def _make_results_logger(self):
        import logging
        logger = logging.getLogger("jarvis.task_results")
        if not logger.handlers:
            try:
                handler = logging.FileHandler(logs_dir() / "task_results.log", encoding="utf-8")
                handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
                logger.addHandler(handler)
                logger.setLevel(logging.INFO)
            except Exception:
                pass
        return logger

    def _file_lock(self, path: str) -> threading.Lock:
        with self._file_locks_guard:
            if path not in self._file_locks:
                self._file_locks[path] = threading.Lock()
            return self._file_locks[path]

    def _safe_memory_event(self, source: str, event: str, detail: str) -> None:
        """DÜZELTME - Memory bağımlılığı: görev motoru Memory'nin sağlıklı
        olmasına bağımlı OLMAMALI. Bu sarmalayıcı olmadan önce
        self.memory.remember_event(...) doğrudan çağrılıyordu - Memory
        (ör. bozuk bir dosya, disk dolu, vb.) hata fırlatırsa bu, bir
        görevin planlanmasını/ilerlemesini/tamamlanmasını ÇÖKERTEBİLİRDİ.
        Şimdi: Memory sadece hata geçmişi/öğrenme için YARDIMCI bir
        bileşen - başarısız olursa SADECE loglanır, akış devam eder."""
        try:
            self.memory.remember_event(source, event, detail)
        except Exception as e:
            print(f"[BrainTeam] ⚠️ Memory olayı kaydedilemedi (görev motoru ETKİLENMEDİ): {e}")

    def _notify_result(self, task: dict) -> None:
        """"7. User response guarantee": bir görev completed/failed/
        cancelled/waiting_approval durumuna ULAŞTIĞINDA çağrılır. ÜÇ
        şeyi GARANTİ eder: (a) varsa canlı oturuma (player.write_log)
        best-effort YAZILI bildirim, (b) varsa VE destekliyorsa (player.speak)
        best-effort SESLİ bildirim - YENİ (2026-09-16, router mimarisi
        Aşama 1/3, "Brain Team sonucu hiç sesle dönmüyor" düzeltmesi;
        önceden bu fonksiyon SADECE yazılı log'a düşüyordu, Gemini'nin
        konuşma bağlamına hiç girmiyordu), (c) HER ZAMAN logs/task_results.log'a
        kalıcı bir satır - player olmasa/uygulama kapalı olsa/speak
        desteklemese BİLE sonuç sessizce kaybolmaz, kullanıcı sonradan bu
        log'a ya da brain_team(action="status")'a bakarak öğrenebilir."""
        status = task.get("status")
        name = task.get("name", "")[:100]
        result_text = task.get("result") or task.get("error") or ""
        summary = f"[{status.upper()}] (id={task['id']}) {name} — {result_text}"
        try:
            self._results_logger.info(summary)
        except Exception as e:
            print(f"[BrainTeam] ⚠️ Sonuç logu yazılamadı: {e}")

        player = self._last_player
        if player is not None:
            try:
                log_fn = getattr(player, "write_log", None)
                if not callable(log_fn):
                    ui = getattr(player, "ui", None)
                    log_fn = getattr(ui, "write_log", None)
                if not callable(log_fn):
                    raise AttributeError("player.write_log veya player.ui.write_log bulunamadı")
                log_fn(f"[BrainTeam] {summary}")
            except Exception as e:
                print(f"[BrainTeam] ⚠️ Canlı oturuma bildirim başarısız (sonuç yine de loglandı): {e}")

            # YENİ: 'player' JarvisLive gibi bir speak() yeteneğine SAHİPSE
            # (main.py::_execute_tool()'daki brain_team dalı artık player=self
            # geçiyor), görevin sonucunu Gemini'nin konuşma bağlamına da
            # sokuyoruz ki Jarvis bunu GERÇEKTEN sesle söyleyebilsin. 'player'
            # sadece write_log destekleyen eski bir nesneyse (ör. test
            # double'ları, self.ui) bu blok SESSİZCE atlanır - davranış
            # DEĞİŞMEZ.
            speak_fn = getattr(player, "speak", None)
            if callable(speak_fn):
                try:
                    speak_prompt = (
                        f"[BRAIN_TEAM_SONUC] AI Beyin Takımı'na verilen \"{name}\" "
                        f"hedefi {status} durumuna ulaştı. Gerçek sonuç: "
                        f"\"{result_text}\". Bunu kullanıcıya kısaca, doğal bir "
                        f"dille özetle - teknik id/durum kodu okuma, sadece "
                        f"gerçek sonucu anlat."
                    )
                    speak_fn(speak_prompt)
                except Exception as e:
                    print(f"[BrainTeam] ⚠️ Sesli bildirim başarısız (sonuç yine de loglandı): {e}")

    def _describe_pending_step(self, task: dict, step: dict) -> dict:
        """Onay isteginde gosterilecek arac/eylem/hedef. Hedef, adimi GERCEKTEN
        calistiracak karar noktasiyla (_resolve_action_with_file_modification)
        ayni yerden cozulur - kullanicinin gordugu yol, yazilacak yoldur."""
        if step.get("agent") == "coder_ai":
            return {"tool": "coder_ai", "action": "modify_critical_file",
                    "target": step.get("file_path") or "—"}
        base_path = task.get("payload", {}).get("_active_folder", ".")
        try:
            tool, params = self._resolve_action_with_file_modification(task, step, base_path)
        except Exception:
            tool, params = step.get("tool") or step.get("agent") or "?", dict(step.get("parameters") or {})
        action = params.get("action") or step.get("action") or "?"
        target = "—"
        if tool == "file_controller":
            try:
                from jarvis.actions.file_controller import _resolve_path
                base = _resolve_path(str(params.get("path") or "."))
                name = params.get("name") or ""
                target = str((base / name if name else base).resolve())
                if params.get("destination"):
                    target += f" → {_resolve_path(str(params['destination'])).resolve()}"
            except Exception:
                target = f"{params.get('path', '')}/{params.get('name', '')}"
        elif params:
            target = ", ".join(f"{k}={str(v)[:60]}" for k, v in params.items() if k != "content")
        return {"tool": tool, "action": action, "target": target}

    def _request_approval(self, task: dict) -> None:
        """HIGH riskli adim beklemeye alindiginda kullaniciya ONAY ISTEGI
        gonderir (sonuc ozeti degil). Canli oturum (JarvisLive) bunu
        request_brain_team_approval ile tek kullanimlik, parmak izi bagli
        onay mekanizmasina kaydeder; onay ancak kullanicinin gercek bir
        sonraki turundan gelir."""
        step = task["payload"].get("pending_step") or {}
        info = self._describe_pending_step(task, step)
        message = (
            f"Onay gerekiyor: AI Beyin Takımı '{info['tool']}' aracıyla "
            f"'{info['action']}' işlemi yapacak. Hedef: {info['target']}. "
            f"Onaylamak için 'evet', iptal etmek için 'hayır' deyin. "
            f"(görev id={task['id']}, risk={step.get('risk', '?')})"
        )
        try:
            self._results_logger.info(f"[{WAITING_APPROVAL.upper()}] (id={task['id']}) {message}")
        except Exception as e:
            print(f"[BrainTeam] ⚠️ Onay istegi loglanamadi: {e}")

        player = self._last_player
        if player is None:
            return
        hook = getattr(player, "request_brain_team_approval", None)
        try:
            if callable(hook):
                hook(task["id"], step, message)
                return
            log_fn = getattr(player, "write_log", None) or getattr(getattr(player, "ui", None), "write_log", None)
            if callable(log_fn):
                log_fn(f"[BrainTeam] {message}")
            speak_fn = getattr(player, "speak", None)
            if callable(speak_fn):
                speak_fn(f"[BRAIN_TEAM_ONAY_ISTEGI] {message}")
        except Exception as e:
            print(f"[BrainTeam] ⚠️ Onay istegi iletilemedi (loglandi): {e}")

    def get_team_health(self) -> str:
        """"2. Heartbeat/health check": her beynin gerçekten çalışıyor mu
        yoksa takılı mı olduğunu (status + o durumda ne kadar süredir
        olduğunu) dışarıya insan-okunabilir şekilde raporlar."""
        brains = (self.planner, self.research, self.coder, self.security,
                  self.memory, self.executor, self.auditor)
        lines = []
        for brain in brains:
            hb = brain.heartbeat()
            flag = "⚠️ " if hb["status"] == "running" and hb["seconds_in_state"] > 60 else ""
            lines.append(
                f"{flag}{hb['name']}: {hb['status']} ({hb['seconds_in_state']:.0f}sn bu durumda)"
                + (f" — son hata: {hb['last_error']}" if hb["last_error"] else "")
            )
        return "AI Beyin Takımı sağlık durumu:\n" + "\n".join(lines)

    # ── Dışarıya açık, HIZLI fonksiyonlar (Jarvis'in canlı oturumunu bloklamaz) ──

    def start_goal(self, goal: str) -> str:
        goal = (goal or "").strip()
        if not goal:
            return "Hedef boş olamaz."
        task = self.tasks.create(name=goal, agent="planner_ai", priority="medium",
                                  payload={"goal": goal})
        self._safe_memory_event("orchestrator", "goal_started", goal)
        return f"AI beyin takımı görevlendirildi (id: {task['id']}): {goal}"

    def list_status(self) -> str:
        all_tasks = self.tasks.list()
        if not all_tasks:
            return "AI beyin takımının bekleyen/tamamlanmış görevi yok."
        lines = []
        for t in all_tasks[-15:]:
            extra = ""
            if t["status"] == WAITING_APPROVAL and t["payload"].get("pending_step"):
                ps = t["payload"]["pending_step"]
                extra = f" — ONAY BEKLİYOR: [{ps.get('agent')}] {ps.get('description', '')[:60]} (risk={ps.get('risk')})"
            lines.append(f"[{t['id']}] {t['status']}: {t['name'][:70]}{extra}")
        return f"{len(all_tasks)} takım görevi (son {len(lines)} tanesi):\n" + "\n".join(lines)

    def approve(self, task_id: str) -> str:
        task = self.tasks.get(task_id)
        if task is None:
            return f"'{task_id}' id'li AI takım görevi bulunamadı."
        if task["status"] != WAITING_APPROVAL or not task["payload"].get("pending_step"):
            return f"'{task_id}' onay bekleyen bir AI takım görevi değil (durum: {task['status']})."

        step = task["payload"]["pending_step"]
        try:
            result = self._execute_step(task, step)
            task["payload"]["pending_step"] = None
            self._finish_step(task, step, result)
            self.tasks.update(task_id, status="pending", payload=task["payload"])
            return f"Onaylandı ve gerçekleştirildi: {str(result)[:200]}"
        except Exception as e:
            task["payload"]["pending_step"] = None
            self.tasks.update(task_id, status="failed", error=str(e), payload=task["payload"])
            self._notify_result(self.tasks.get(task_id))
            return f"Onaylandı ama çalıştırılırken hata oluştu: {e}"

    def deny(self, task_id: str) -> str:
        task = self.tasks.get(task_id)
        if task is None:
            return f"'{task_id}' id'li AI takım görevi bulunamadı."
        task["payload"]["pending_step"] = None
        self.tasks.update(task_id, status="cancelled", payload=task["payload"])
        self._safe_memory_event("orchestrator", "task_denied", task["name"])
        self._notify_result(self.tasks.get(task_id))
        return f"'{task_id}' AI takım görevi iptal edildi."

    # ── Adım çalıştırma yardımcıları ─────────────────────────────────────

    def _infer_from_structured_parameters(self, parameters, description, base_path=".", forced_action=None):
        # Planner capability alanini doldurmasa bile, LLM kendi
        # inisiyatifiyle kullanisli parametreler uretmis olabilir (canli
        # testte gozlemlendi: capability bos, ama parameters =
        # {"target": "Desktop/x.txt", "content": "..."}). Bu veri varsa,
        # zayif metin-tahminine DUSMEDEN once DOGRUDAN kullanilir.
        if not isinstance(parameters, dict) or not parameters:
            return None
        raw_target = (parameters.get("target") or parameters.get("path")
                      or parameters.get("file") or parameters.get("filename")
                      or parameters.get("file_name") or parameters.get("name") or "")
        raw_target = str(raw_target).strip()
        if not raw_target:
            return None
        content = parameters.get("content", "")
        if not isinstance(content, str):
            return None
        norm = raw_target.strip("/")
        parts = [p for p in norm.split("/") if p]
        name = parts[-1] if parts else ""
        if not name or "." not in name:
            return None
        if len(parts) > 1:
            path = "/".join(parts[:-1])
        else:
            # DUZELTME (canli testte bulundu, 2026-09-22): planner sadece
            # dosya adi verip (ornegin "file_name": "x.txt") konum HIC
            # belirtmezse, duz "." varsayilanina dusmek yerine aciklama
            # metninde "masaustu/indirilenler/belgeler" gibi bilinen bir
            # konum var mi diye main.py'nin ZATEN test edilmis
            # match_file_modification() fonksiyonuyla kontrol ediyoruz -
            # ayni konum-tespit mantigini burada TEKRAR YAZMAK yerine.
            path = base_path
            try:
                from jarvis.actions.intent_router import match_file_modification
                loc_hint = match_file_modification(description)
                if loc_hint and loc_hint.get("path") and loc_hint.get("path") != ".":
                    path = loc_hint["path"]
            except Exception:
                pass
        if forced_action:
            action = forced_action
        else:
            d = description.lower()
            is_create = any(k in d for k in ("oluştur", "yarat", "create")) or not d
            action = "create_file" if is_create else "write"
        return "file_controller", {"action": action, "path": path or base_path, "name": name, "content": content}

    def _infer_executor_action(self, description: str, base_path: str = ".") -> tuple[str, dict]:
        # DÜZELTME (kullanici onayli, 2026-09-16, "path-context" - E2E test
        # sirasinda bulundu): bu fonksiyon ONCEDEN HER ZAMAN "path": "."
        # sabitliyordu - yani bir adimda "jarvis_test" klasoru olussa bile,
        # bir SONRAKI adim (dosya olustur/yaz/oku) o klasorun ICINE degil,
        # hep ayni duz calisma dizinine gidiyordu (kullanicinin orijinal
        # ornek senaryosu - "klasor olustur, ICINE dosya koy" - bu yuzden
        # HICBIR ZAMAN gercekten ic ice calismiyordu). Simdi cagiran taraf
        # (_execute_step/_verify_file_action), o gorevde en son basariyla
        # oluşturulmus klasoru "base_path" olarak GECEBILIYOR - varsayilan
        # hala "." (eski davranis, geriye donuk uyumlu; bu parametreyi
        # vermeyen her cagri - testler dahil - eskisi gibi calismaya devam
        # eder).
        d = description.lower()
        if "yedek" in d or "backup" in d:
            return "backup_create", {}
        if "rollback" in d or "geri al" in d:
            return "backup_rollback", {}
        if "github" in d:
            return "github_search", {"query": description}

        # YENI DUZELTME (canli testte bulundu - "X yerine Y yaz" gorevi):
        # asagidaki tirnak-tabanli isim/icerik cikarimi, tirnak icindeki
        # HERHANGI bir metni (bir kod satirindaki string literal bile olsa)
        # korukorune dosya adi saniyordu. "'X' yerine 'Y'" KESIN kalibi
        # bulunursa, bunu bir bul-degistir islemi olarak ele al.
        def _extract_replace_pair(desc: str):
            m = re.search(r'''['"]([^'"]+)['"][^'"]*?yerine[^'"]*?['"]([^'"]+)['"]''', desc, re.IGNORECASE)
            return (m.group(1), m.group(2)) if m else None

        def _extract_target_filename(desc: str):
            without_quotes = re.sub(r'''['"][^'"]*['"]''', " ", desc)
            m = re.search(r"\b(\S+?\.[A-Za-z0-9]{1,10})\b", without_quotes)
            return m.group(1).strip() if m else None

        replace_pair = _extract_replace_pair(description)
        if replace_pair is not None:
            old_text, new_text = replace_pair
            filename = _extract_target_filename(description)
            if filename:
                return "file_controller", {
                    "action": "find_replace", "path": base_path, "name": filename,
                    "old_text": old_text, "new_text": new_text,
                }
            return "file_controller", {"action": "info", "path": base_path}

        # DÜZELTME (bkz. dosya başı '_QUOTED_RE' notu): "dosya/klasör
        # oluştur", "...yaz", "...oku" gibi ÇOK YAYGIN executor adımları
        # artık gerçek file_controller eylemlerine (create_file/
        # create_folder/write/read) eşleniyor - önceden hiçbiri
        # tanınmıyordu, hepsi sessizce salt-okunur 'info'ya düşüyordu.
        name, content = _extract_quoted(description)
        is_create = any(k in d for k in ("oluştur", "yarat", "create"))
        is_write = ("write" in d) or ("yaz" in d and "yazılım" not in d and "yazar" not in d)
        is_read = ("oku" in d) or ("read" in d)

        if is_create and ("klasör" in d or "folder" in d):
            return "file_controller", {"action": "create_folder", "path": base_path, "name": name}
        if is_create and ("dosya" in d or "file" in d):
            return "file_controller", {"action": "create_file", "path": base_path, "name": name, "content": content}
        if is_write:
            return "file_controller", {"action": "write", "path": base_path, "name": name, "content": content}
        if is_read:
            return "file_controller", {"action": "read", "path": base_path, "name": name}

        # Hiçbiri eşleşmezse ESKİ, güvenli varsayılan: salt-okunur bilgi
        # sorgusu (hiçbir şeyi asla YARATMAZ/DEĞİŞTİRMEZ/SİLMEZ).
        return "file_controller", {"action": "info", "path": base_path}

    def _resolve_action_with_file_modification(self, task: dict, step: dict, base_path: str = ".") -> tuple[str, dict]:
        """DUZELTME (canli testte bulundu, 2026-09-22): main.py'nin
        [FILE_MODIFICATION] deterministik yolu dogru cikardigi action/path/
        name/content bilgisini task["payload"]["file_modification"]'a
        koyuyordu ama SADECE _execute_step() bunu okuyordu -
        _verify_file_action() (auditor'in bagimsiz dosya kontrolu) hala eski,
        naif _infer_executor_action() ile description'dan TEKRAR tahmin
        yurutuyordu. Ikisi FARKLI sonuc uretince (gercek dosya DOGRU
        yazilmis olsa bile), _finish_step()'teki "bagimsiz kanit" mekanizmasi
        auditor'in dogru "passed=True" kararini YANLIS sekilde
        "passed=False"a ceviriyordu - yani GERCEK basariyi SAHTE
        basarisizlik olarak raporluyordu (ilk bulunan "sahte basari"
        hatasinin aynadaki yansimasi, canli testte brain_test_final.txt ile
        kanitlandi). Bu fonksiyon TEK bir karar noktasi olarak hem
        _execute_step hem _verify_file_action tarafindan ORTAK kullanilir,
        boylece iki yer bir daha ASLA birbirinden sapamaz."""
        file_mod = task.get("payload", {}).get("file_modification")
        if (
            isinstance(file_mod, dict)
            and file_mod.get("action") in ("create_file", "write")
            and file_mod.get("name")
        ):
            action = "file_controller"
            params = {
                "action": file_mod.get("action"),
                "path": file_mod.get("path") or base_path,
                "name": file_mod.get("name"),
                "content": file_mod.get("content", ""),
            }
            if file_mod.get("action") == "write":
                params["append"] = bool(file_mod.get("append", False))
        elif isinstance(file_mod, dict) and file_mod.get("action"):
            # delete/move/list/... : file_modification'daki eylem AYNEN
            # kullanilir - aciklamadan yeniden tahmin edilmez.
            action = "file_controller"
            params = {k: v for k, v in file_mod.items() if v not in (None, "")}
            params.setdefault("path", base_path)
        else:
            action, params = self._resolve_executor_call(step, base_path)

        # DUZELTME (code review bulgusu, 2026-09-28, PR #3): asagidaki icerik
        # geri-kazanim + "sessizce bos yazma" korumasi ONCEDEN SADECE
        # _execute_step()'in kendi govdesinde, bu fonksiyonun DONUSUNDEN
        # SONRA uygulaniyordu - yani _execute_step gercekten dogru (goal'den
        # geri kazanilmis) icerikle yaziyordu, ama _verify_file_action()
        # (bu fonksiyonu AYRI/BAGIMSIZ tekrar cagirdigi icin) o geri
        # kazanimi HIC GORMUYORDU: kendi params'inda content hala "" kalip
        # GERCEKTEN basarili bir yazmayi ("Gercek icerik burada" gibi) bos
        # ("") ile karsilastirip YANLIS BICIMDE basarisiz sayiyordu (canli
        # kod incelemesinde bulundu, gercek fixture ile dogrulandi). Bu
        # dosyanin basindaki notun tam olarak uyardigi kalibin bir baskasi:
        # "TEK bir karar noktasi ... boylece iki yer bir daha ASLA
        # birbirinden sapamaz" - icerik geri kazanimi da simdi BURAYA,
        # tek karar noktasina tasindi, boylece _execute_step VE
        # _verify_file_action HER ZAMAN AYNI (nihai) content'i gorur.
        if (
            action == "file_controller"
            and params.get("action") in ("create_file", "write")
            and not params.get("content")
        ):
            try:
                goal_name, goal_content = _extract_quoted(task["payload"].get("goal", ""))
                if (
                    goal_content
                    and params.get("name")
                    and (not goal_name or goal_name == params.get("name"))
                ):
                    params["content"] = goal_content
            except Exception:
                pass

            # DUZELTME (gercek canli hata, 2026-09-28, "jarvis_test_kaydi.txt
            # 0 byte" bulgusu): yukaridaki geri kazanim denemesi de
            # basarisiz olursa (goal metninde de tirnak/backtick icinde
            # icerik yoksa - GERCEK kullanici komutlarinda neredeyse hic
            # olmuyor, insanlar icerigi tirnaga almaz), content hala ""
            # kaliyordu ve bu SESSIZCE file_controller'a gonderiliyordu:
            # create_file/write kendi ic dogrulamasinda gonderilen icerik
            # zaten "" oldugu icin "" == "" GECERLI sayilip GERCEKTEN
            # basarili donuyordu - 0 byte'lik bir dosya "File created"
            # diye raporlaniyordu. _verify_file_action (bagimsiz denetim)
            # da content bos oldugu icin icerigi hic KARSILASTIRMIYORDU
            # (content_matches=None, "kontrol edilmedi" anlaminda), yani
            # bu guvenlik agi da bu spesifik hatayi YAKALAYAMIYORDU
            # (canli kanit: '/home/murat/.../jarvis_test_kaydi.txt' ->
            # exists=True, bytes=0, content="").
            #
            # Kok neden NLP tarafinda: _extract_quoted() SADECE tirnak/
            # backtick icindeki metni icerik sayar; bu duzeltilemeyecek
            # kadar kirilgan bir sezgi (dogal dilde icerik neredeyse hic
            # tirnaklanmaz). Cozum: SESSIZCE YANLIS (bos) bir deger
            # uretip basarili gibi davranmak yerine ACIKCA BASARISIZ OL -
            # kullanici acikca "bos dosya" istemedigi surece, icerigi
            # cikarilamayan bir create_file/write adimi ARTIK sessizce
            # 0 byte'lik "basarili" bir dosya uretmiyor; adim FAILED
            # olarak isaretlenip planner/kullaniciya gercek nedeni
            # bildiriliyor (bkz. _tick()/approve() - RuntimeError burada
            # failed_steps'e duser, gorev sessizce "basarili" sayilmaz).
            # DUZELTME (mevcut e2e_file_task_self_test.py ile bulundu):
            # yukaridaki ilk versiyon HER bos-content create_file/write
            # adimini hata sayiyordu - ama bu, GECERLI bir plan deseninde
            # (once bos dosya OLUSTUR, SONRA AYRI bir adimda GERCEK
            # icerigi yaz - bkz. o test dosyasinin 2/3. adimlari) yanlis
            # pozitif uretiyordu: "adı X olan bir dosya oluştur" gibi bir
            # adimin acikca icerikle hicbir ilgisi yok, bos content
            # burada TAMAMEN normal/beklenen. Ayirt edici gercek sinyal,
            # content'in bos olmasi DEGIL - bu ADIMIN AÇIKLAMASININ
            # (yalnizca BU adimin - genel "goal" degil, cunku goal daha
            # sonraki bir adimdan bahsediyor olabilir) zaten bir icerik
            # yazma niyeti tasimasi AMA yine de content'in bos kalmasidir.
            if not params.get("content"):
                desc_lower = step.get("description", "").lower()
                has_content_intent = (
                    params.get("action") == "write"
                    or (
                        any(k in desc_lower for k in ("yaz", "içine", "içerik", "write", "content"))
                        and not any(k in desc_lower for k in ("yazılım", "yazar", "yazıcı"))
                    )
                )
                explicitly_empty = any(
                    k in desc_lower
                    for k in ("boş dosya", "boş bir dosya", "içeriksiz", "empty file", "boşalt", "temizle")
                )
                if has_content_intent and not explicitly_empty:
                    raise RuntimeError(
                        f"İçerik belirlenemedi: '{params.get('name')}' için yazılacak "
                        f"GERÇEK içerik, adım açıklamasından veya hedeften çıkarılamadı "
                        f"(içerik tırnak/backtick içinde değil). Sessizce 0 byte'lık bir "
                        f"dosya oluşturup başarılı saymak yerine bu adım BAŞARISIZ "
                        f"sayıldı - planner'ın 'parameters.content' alanını açıkça "
                        f"doldurması gerekiyor. Kasıtlı olarak boş bir dosya isteniyorsa "
                        f"adımda açıkça 'boş dosya' belirtin."
                    )

        return action, params

    def _resolve_executor_call(self, step: dict, base_path: str = ".") -> tuple[str, dict]:
        """YENİ mimari (kullanıcı talimatı, Capability Registry + Agent/Tool
        Resolver, 2026-09-16): executor_ai'ye ne çağrılacağını üç katmanlı,
        SIRALI bir öncelikle belirler - _infer_executor_action() (legacy
        keyword eşleştirmesi) TAMAMEN ÇÖPE ATILMADI, en sondaki güvenli
        fallback olarak AYNEN kalıyor (geriye dönük uyumluluk, madde 7):
          1. Adımda AÇIKÇA bir "capability" alanı varsa (bugün Planner'ın
             kendisi bunu ÜRETMİYOR - planner_ai.py'ye dokunulmadı - ama
             ileride yapılandırılmış bir girdi gelirse), capability_resolver.
             resolve_structured() ile GERÇEK kayıtlara karşı doğrulanır.
             Geçersizse ValueError fırlatır - SESSİZCE 2./3. adıma DÜŞMEZ,
             açıkça REJECT edilir (TEST 6/7: bilinmeyen tool/capability).
          2. Yoksa, capability_resolver.resolve_capability() serbest metni
             GERÇEK windows_system komutlarıyla eşleştirmeye çalışır -
             eşleşme yoksa (None) sessizce 3. adıma düşer.
          3. Hiçbiri eşleşmezse, ESKİ _infer_executor_action() (değişmedi)
             aynen çalışmaya devam eder - legacy görevler bozulmaz."""
        desc = step.get("description", "")
        capability = step.get("capability")
        if capability:
            tool = step.get("tool")
            # DUZELTME (canli testte bulundu, 2026-09-22): model capability
            # alanina GERCEK bir capability adi degil, dogrudan bir
            # file_controller EYLEM adi (or. "create_file") yazabiliyor -
            # "tool" alanini da bos birakinca resolve_structured() tool'u
            # capability ile ayni sayip ("create_file" ALLOWED_TOOLS'ta
            # olmadigi icin) "Bilinmeyen tool" diye SESSIZCE REJECT
            # ediyordu - adim history'ye bile yazilmadan gorev "failed"
            # oluyordu (canli testte brain_test_final4.txt ile kanitlandi,
            # history=[]). Bilinen, GUVENLI bir file_controller eylem
            # adiysa ve "tool" ya bos ya da zaten capability/file_controller
            # ile ayniysa, bunu GERCEK bir capability degil bir
            # eylem-adi-hatasi olarak ele alip yapilandirilmis
            # parametrelerle cozuyoruz - genuine bilinmeyen/supheli bir
            # capability icin REJECT davranisi (asagidaki
            # resolve_structured cagrisi) DEGISMEDEN kaliyor.
            _KNOWN_FILE_ACTIONS = {
                "create_file", "create_folder", "write", "find_replace",
                "move", "copy", "delete", "find", "list", "info",
                "disk_usage", "extract",
            }
            if capability in _KNOWN_FILE_ACTIONS and (not tool or tool in (capability, "file_controller")):
                structured = self._infer_from_structured_parameters(
                    step.get("parameters"), desc, base_path, forced_action=capability)
                if structured is not None:
                    return structured
            resolved = capability_resolver.resolve_structured(capability, tool, step.get("parameters"))
            return resolved["action"], resolved["parameters"]

        resolved = capability_resolver.resolve_capability(desc)
        if resolved is not None:
            return resolved["action"], resolved["parameters"]

        structured = self._infer_from_structured_parameters(step.get("parameters"), desc, base_path)
        if structured is not None:
            return structured

        return self._infer_executor_action(desc, base_path)

    def _resolve_step_call(self, task: dict | None, step: dict) -> tuple[str, dict]:
        """Risk degerlendirmesinin, onay metninin ve yurutmenin ORTAK karar
        noktasi: executor adiminin GERCEKTEN calistiracagi (tool, params)."""
        task = task or {"payload": {}}
        base_path = task.get("payload", {}).get("_active_folder", ".")
        return self._resolve_action_with_file_modification(task, step, base_path)

    def _risk_of_step(self, step: dict, task: dict | None = None) -> tuple[str, str]:
        """Riski BELİRLEYEN yer security_ai beynidir (deterministik kural
        tablosu + Gemini'den sadece okunabilir açıklama) - burada SADECE
        hangi (tool, action) çiftinin security_ai'ye sorulacağı seçilir, ve
        planner'ın serbest metin açıklamasında security_ai'nin tablosunun
        yakalayamayacağı bir anahtar kelime varsa risk YUKARI yuvarlanır
        (asla aşağı - şüpheli durumda güvenli tarafta kal)."""
        agent = step.get("agent")
        desc = step.get("description", "")

        if agent == "research_ai":
            return "low", "Salt-okunur araştırma/analiz."

        if agent == "coder_ai" and step.get("operation") == "analyze":
            # YENİ (2026-09-16, UNRESOLVED_AGENT düzeltmesi): SADECE OKUMA/
            # ANALİZ - research_ai'nin salt-okunur dalıyla AYNI muamele
            # (LOW risk, otomatik ilerler, onay beklemez). Dosya hiçbir
            # zaman DEĞİŞTİRİLMEDİĞİ için (bkz. _execute_step()'in
            # operation="analyze" dalı - coder_ai dry_run=True ile
            # çağrılıyor) "modify_critical_file"ın HIGH riskine tabi
            # TUTULMUYOR - aksi halde her analiz isteği, hiçbir dosya
            # değişmeyecek olsa bile, gereksiz yere kullanıcı onayında
            # beklerdi (tam da bu görevin çözmeye çalıştığı sorunun bir
            # başka biçimi olurdu).
            return "low", "Salt-okunur dosya analizi (dry_run, hiçbir değişiklik yazılmaz)."

        if agent == "coder_ai":
            # operation == "modify" (ya da operation alanı olmayan eski
            # görevler icin planner_ai.py'nin varsayılanı) - ESKİ DAVRANIŞ
            # AYNEN KORUNDU: her zaman HIGH risk, kullanıcı onayı zorunlu.
            tool, action = "coder_ai", "modify_critical_file"
        else:  # executor_ai
            # YENİ mimari (2026-09-16): önce _resolve_executor_call() dene
            # (capability_resolver -> yapılandırılmış/serbest-metin ->
            # legacy fallback sırası). Yapılandırılmış bir capability/tool
            # GEÇERSİZSE (TEST 6/7) burada ValueError fırlar - risk aşaması
            # bu yüzden ÇÖKMEMELİ (aksi halde _tick() bu adımı asla
            # ilerletemez, sonsuz döngüye girer): gerçek REJECT, bu try
            # başarısız olsa bile, _execute_step() içindeki mevcut
            # try/except tarafından düzgünce yakalanıp failed_steps'e
            # düşecek - burada sadece güvenli bir yer tutucuyla devam edilir.
            # Risk, _execute_step'in calistiracagi AYNI cozumlemeden sorulur
            # (eskiden yalnizca aciklamadan tahmin ediliyordu: "notlar3.txt
            # olustur" -> info/LOW, yurutme -> create_file). Cozumleme hata
            # verirse fail-closed HIGH.
            try:
                inferred_action, inferred_params = self._resolve_step_call(task, step)
            except Exception as e:
                return "high", (f"Adım çözümlenemedi ({type(e).__name__}: {e}); "
                                f"güvenli tarafta kalınarak HIGH risk.")
            if inferred_action == "file_controller":
                # DÜZELTME: burası ÖNCEDEN HER ZAMAN "info" (LOW risk)
                # hardcode ediyordu - yani _infer_executor_action()'ın
                # GERÇEKTE hangi iç eylemi (create_file/create_folder/
                # delete/move/...) seçtiği risk değerlendirmesine hiç
                # yansımıyordu. security_ai salt-okunur eylemler dışındaki
                # her file_controller eylemine HIGH diyor - ama hiç
                # SORULMUYORDU. Artık gerçek iç eylem security_ai'ye soruluyor.
                tool, action = "file_controller", inferred_params.get("action", "info")
            elif inferred_action == "windows_system":
                # windows_system SALT-OKUNUR (madde 11) - is_destructive()
                # zaten bunu yıkıcı SAYMIYOR; security_ai.classify_risk de
                # tanımadığı (tool, action) çiftleri için varsayılan LOW
                # döner (brains/security_ai.py satır 76) - burada AYRICA
                # bir HIGH/MEDIUM kuralı eklenmedi, mevcut varsayılan davranış
                # zaten doğru sonucu veriyor.
                tool, action = "windows_system", inferred_params.get("command_name")
            else:
                tool, action = inferred_action, None

        resp = self.bus.send("orchestrator", "security_ai", "risk değerlendirmesi",
                              payload={"tool": tool, "action": action, "target": desc})
        sec_result = resp.get("result", {}) if resp.get("status") == "completed" else {}
        risk = sec_result.get("risk", "high")   # security_ai cagrisi basarisiz olursa GUVENLI TARAFTA kal
        reason = sec_result.get("reason", "Security AI değerlendirmesi alınamadı - güvenli tarafta kalınıyor.")

        if any(k in desc.lower() for k in _HIGH_RISK_KEYWORDS) and risk != "high":
            risk = "high"
            reason += " (+ açıklamada yüksek riskli bir anahtar kelime tespit edildi, risk yükseltildi)"

        return risk, reason

    def _execute_step(self, task: dict, step: dict):
        agent = step.get("agent")
        desc = step.get("description", "")

        if agent == "research_ai":
            resp = self.bus.send("orchestrator", "research_ai", desc, payload={"query": desc})
            return resp

        if agent == "coder_ai":
            file_path = step.get("file_path") or task["payload"].get("file_path")
            if not file_path:
                raise RuntimeError("coder_ai adımı için hedef dosya belirtilmemiş.")
            operation = step.get("operation", "modify")
            lock = self._file_lock(file_path)
            if not lock.acquire(blocking=False):
                raise RuntimeError(f"'{file_path}' şu anda başka bir adım tarafından değiştiriliyor - lütfen tekrar deneyin.")
            try:
                if operation == "analyze":
                    # YENİ (2026-09-16, UNRESOLVED_AGENT düzeltmesi): SADECE
                    # OKUMA/ANALİZ - backup YOK, coder_ai.py'nin KENDİSİ
                    # DEĞİŞTİRİLMEDİ; onun ÖNCEDEN VAR olan dry_run=True
                    # yolu kullanılıyor (dosyayı okur, model'e analiz
                    # ettirir, "new_content" üretir ama HİÇBİR ZAMAN diske
                    # yazmaz - bkz. coder_ai.py: `if dry_run: ... return`).
                    # change_request'i AÇIKÇA "değişiklik önerme, sadece
                    # analiz et" şeklinde çerçeveliyoruz ki model yanlışlıkla
                    # gerçek bir değişiklik "önermeye" çalışmasın; dry_run
                    # zaten HER durumda yazmayı engelliyor, bu sadece net bir
                    # istek. Analiz sonucu (summary) task geçmişine, executor_
                    # ai/file_controller adımlarıyla AYNI yerde (_finish_step
                    # -> history) kaydediliyor - ayrı bir mekanizma icat
                    # edilmedi.
                    analyze_request = (
                        "Bu bir ANALİZ isteğidir - dosyayı DEĞİŞTİRME, hiçbir "
                        "değişiklik ÖNERME. \"new_content\" alanına dosyanın "
                        "MEVCUT içeriğini AYNEN, tek karakter bile değiştirmeden "
                        "geri ver. \"summary\" alanına şu analiz isteğinin "
                        "YANITINI (gerçek analiz sonucunu) yaz: " + desc
                    )
                    resp = self.bus.send("orchestrator", "coder_ai", desc,
                                          payload={"file_path": file_path,
                                                   "change_request": analyze_request,
                                                   "dry_run": True})
                    return resp

                # operation == "modify" (ya da operation alanı olmayan eski
                # görevler) - ESKİ DAVRANIŞ AYNEN KORUNDU: file lock (yukarıda
                # zaten alındı) -> backup -> coder_ai (GERÇEK yazma) ->
                # coder_ai.py'nin kendi ast.parse sözdizimi kontrolü ->
                # (bu metodun dönüşünden sonra _finish_step -> Auditor ->
                # gerekirse rollback). Hiçbir satır değiştirilmedi.
                # 14. BACKUP KURALI: ONCE backup, SONRA degisiklik.
                backup_resp = self.bus.send("orchestrator", "executor_ai", "backup_create",
                                             payload={"action": "backup_create", "params": {}})
                resp = self.bus.send("orchestrator", "coder_ai", desc,
                                      payload={"file_path": file_path, "change_request": desc})
                resp.setdefault("result", {})
                if isinstance(resp["result"], dict):
                    resp["result"]["backup"] = backup_resp.get("result")
                return resp
            finally:
                lock.release()

        if agent == "executor_ai":
            # DÜZELTME (path-context, 2026-09-16): bu görevde daha önce
            # başarıyla oluşturulmuş bir klasör varsa (bkz. _finish_step),
            # bu adım o klasörün İÇİNDE çalışsın - eskiden hep "." (düz
            # çalışma dizini) kullanılıyordu.
            # YENİ mimari (2026-09-16, 2026-09-22 birlestirildi): action/params
            # karari artik TEK bir ortak metotta (_resolve_action_with_
            # file_modification) - hem burasi hem _verify_file_action AYNI
            # sonucu uretir, bir daha birbirinden sapmaz (bkz. o metodun
            # docstring'i).
            action, params = self._resolve_step_call(task, step)

            # DUZELTME (code review bulgusu, 2026-09-28, PR #3): icerik
            # geri-kazanim + "sessizce bos yazma" korumasi ARTIK yukaridaki
            # _resolve_action_with_file_modification() icinde uygulaniyor -
            # boylece _verify_file_action() de AYNI (nihai) content'i gorur.
            # Burada TEKRARLANMIYOR (bkz. o metodun docstring/yorumlari).

            return self.bus.send("orchestrator", "executor_ai", desc, payload={"action": action, "params": params})

        raise RuntimeError(f"Bilinmeyen/uygun olmayan beyin: {agent!r}")

    def _verify_file_action(self, task: dict, step: dict, base_path: str = ".") -> dict | None:
        """DÜZELTME B (gerçek doğrulama, kullanıcı onaylı): auditor_ai'nin
        "passed" kararı TAMAMEN Gemini'nin metin yargısına dayanıyor -
        dosya sistemini hiç sorgulamıyor (bkz. dosya başı 2026-09-15 notu).
        Bu fonksiyon, executor_ai + file_controller'ın create_file/
        create_folder/write adımları için, auditor'dan BAĞIMSIZ (onu
        ÇAĞIRMADAN, ham pathlib ile) bir kontrol yapar. Diğer agent'lara
        (research_ai/coder_ai) veya file_controller'ın info/list/read gibi
        salt-okunur eylemlerine hiç karışmaz - kapsamı BİLEREK dar tutuldu
        (yeni bir doğrulama alt-sistemi değil, tek bir güvenlik ağı).

        Dönüş: bu adım kapsam dışıysa (executor_ai değil, file_controller
        değil, action create_file/create_folder/write değil, ya da isim
        çıkarılamadıysa) None - eski davranışla BİREBİR aynı, bu güvenlik
        ağı devre dışı kalır. Kapsam İÇİNDEYSE HER ZAMAN bir kanıt sözlüğü
        döner: {"path": str, "exists": bool, "content_matches": bool|None,
        "error": str|None} - "content_matches" sadece dosya içeriği
        gerçekten karşılaştırıldıysa (create_file/write + content varsa)
        True/False olur, aksi halde None (karşılaştırılmadı) kalır.

        DÜZELTME (2026-09-16, Aşama 2.3 audit kanıt eksikliği): dönüş tipi
        str|None'dan dict|None'a değişti - MEKANİZMANIN KENDİSİ (aynı
        pathlib path çözümü, aynı is_file()/is_dir(), aynı read_text()
        karşılaştırması) TEK SATIR değişmeden aynen korundu, SADECE sonucun
        NASIL RAPORLANDIĞI (serbest metin yerine yapılandırılmış kanıt)
        değişti - bkz. _finish_step()'teki kullanım notu."""
        if step.get("agent") != "executor_ai":
            return None
        try:
            action, params = self._resolve_action_with_file_modification(task, step, base_path)
        except Exception:
            return None  # bu güvenlik ağının kendisi ASLA görevi çökertmemeli
        if action != "file_controller":
            return None
        inner = params.get("action")
        name = params.get("name")
        if inner not in ("create_file", "create_folder", "write") or not name:
            return None  # isim çıkarılamadıysa bu kontrol atlanır - eski davranış (info) zaten korunuyor

        # DUZELTME (canli testte bulundu, 2026-09-22): duz "Path(path)/name"
        # birlestirmesi file_controller'in "desktop"/"downloads"/"documents"
        # gibi KISAYOLLARINI hic taniamiyordu - path="desktop" oldugunda
        # CALISMA DIZINI icinde var olmayan bir "desktop/" klasorune
        # bakiyordu, gercek ~/Desktop yerine. Sonuc: dosya GERCEKTEN dogru
        # icerikle yazilmis olsa bile bu bagimsiz kontrol "diskte
        # bulunamadi" diyip GERCEK basariyi sahte basarisizliga
        # ceviriyordu. file_controller'in KENDI yol cozumleyicisini
        # kullanarak ayni kisayollari (desktop/downloads/documents/...)
        # dogru sekilde gercek klasore cevirir.
        from jarvis.actions.file_controller import _resolve_path as _fc_resolve_path
        target = _fc_resolve_path(params.get("path", ".")) / name
        evidence: dict = {"path": str(target), "exists": False, "content_matches": None, "error": None}
        try:
            if inner == "create_folder":
                evidence["exists"] = target.is_dir()
                if not evidence["exists"]:
                    evidence["error"] = f"'{target}' bir klasör olarak diskte bulunamadı."
                return evidence
            evidence["exists"] = target.is_file()
            if not evidence["exists"]:
                evidence["error"] = f"'{target}' bir dosya olarak diskte bulunamadı."
                return evidence
            # DUZELTME (kullanici onayli, 2026-09-15, E2E test sirasinda
            # bulundu): bu kontrol eskiden SADECE "create_file" icin
            # icerik karsilastiriyordu - "write" adimlari (dosyaya ICERIK
            # yazma, tam olarak kullanicinin orijinal ornek senaryosundaki
            # 3. adim) bu guvenlik agindan GECIYORDU: sadece dosyanin VAR
            # OLDUGU kontrol ediliyor, ICERIGIN dogru oldugu HIC kontrol
            # edilmiyordu - "sahte basari"nin tam olarak yakalanmasi
            # gereken yerde bir bosluktu (E2E testin 2. senaryosunda bu
            # yuzden bozulmus icerik _verify_file_action tarafindan degil,
            # testin KENDI ayri son kontrolu tarafindan yakalandi).
            # file_controller.write_file() ile AYNI append/overwrite
            # mantigiyla simdi "write" icin de icerik karsilastiriliyor.
            #
            # DUZELTME (gercek canli hata, 2026-09-28): bu kontrol ONCEDEN
            # SADECE "params.get('content')" DOLU ise calisiyordu - yani
            # content zaten yukari akiste ("_execute_step") bos kaldiysa bu
            # bagimsiz dogrulama devre disi kalip content_matches=None
            # donuyordu, "kontrol edilmedi" _finish_step tarafindan "aksi
            # ispatlanmadi" gibi degerlendirilip basariya cevriliyordu (bkz.
            # _finish_step: "content_matches is not False" -> None de gecer).
            # Artik content bos OLSA BILE karsilastirma HER ZAMAN yapiliyor -
            # gercekten bos bir dosya istenmisse (content="" beklenen) bu
            # trivyal sekilde eslesir (True), ama dosyada BASKA/eski bir
            # icerik kalmissa (ör. write basarisiz/yarim kaldi ya da eski
            # dosya ustune yazilmadi) artik bu da YAKALANIR (False) - eskiden
            # sessizce gozden kaciyordu.
            if inner in ("create_file", "write"):
                actual = target.read_text(encoding="utf-8")
                content = params.get("content", "")
                ok = actual.endswith(content) if (inner == "write" and params.get("append")) else (actual == content)
                evidence["content_matches"] = ok
                if not ok:
                    evidence["error"] = f"'{target}' içeriği beklenenle eşleşmiyor."
        except Exception as e:
            evidence["error"] = f"'{target}' kontrol edilirken hata: {e}"
        return evidence

    def _finish_step(self, task: dict, step: dict, result) -> None:
        # "6. Result verification": agent "tamamlandı" dedi diye görev
        # otomatik başarılı SAYILMASIN. Önce ucuz, deterministik bir metin
        # kontrolü yap (bkz. _NO_RESULT_PATTERNS dosya başı notu) - sonuç
        # açıkça "bulunamadı/sonuç yok" diyorsa auditor_ai'ye (yavaş,
        # ücretli bir LLM çağrısı) SORMADAN başarısız say. Aksi halde,
        # eskisi gibi auditor_ai'nin değerlendirmesine başvur.
        result_text = str(result).lower()
        active_folder = task["payload"].get("_active_folder", ".")

        # DÜZELTME (2026-09-16, Aşama 2.3'te bulunan audit kanıt eksikliği):
        # mevcut, DEĞİŞTİRİLMEMİŞ bağımsız dosya doğrulaması (_verify_file_
        # action - aynı pathlib mekanizması) artık auditor_ai'yi ÇAĞIRMADAN
        # ÖNCE hesaplanıyor. Canlı testte bulunan gerçek kusur: executor
        # GERÇEKTEN doğru dosyayı doğru içerikle oluşturuyordu, ama
        # auditor_ai SADECE executor'ın serbest metin sonucunu ("File
        # created: X") görüp emin olamıyor, passed=False diyordu - görev
        # sonsuza kadar tekrar deniyordu (gereksiz retry). Şimdi bu kanıt
        # (path/exists/content_matches) auditor_ai'ye payload'ın bir
        # parçası olarak VERİLİYOR, VE kanıt kapsam içinde KESİN bir
        # sonuç gösteriyorsa (uygulanabilir olduğu HER durumda) bu
        # deterministik kanıt auditor'ın (yanılabilir) metin yargısının
        # ÖNÜNE geçiyor - security_ai'nin risk kararının bir LLM çağrısına
        # BIRAKILMAMASIYLA AYNI felsefe (dosya başı notu): dosya sisteminin
        # GERÇEK durumu bir LLM yorumundan daha güvenilir kabul edilir.
        # Bu ARTIK İKİ YÖNLÜ çalışıyor (önceden SADECE True->False'du,
        # "tersi asla olmaz" deniyordu - o kısıtlama TAM OLARAK bu bug'ın
        # kaynağıydı): kanıt açıkça BAŞARILI gösteriyorsa auditor'ın
        # False'unu True'ya çevirir, kanıt açıkça BAŞARISIZ gösteriyorsa
        # (eskisi gibi) auditor'ın True'sunu False'a çevirir. Kanıt
        # kapsam DIŞINDAYSA (file_evidence is None - research_ai/coder_ai
        # veya file_controller'ın info/list/read gibi eylemleri) davranış
        # HİÇ DEĞİŞMEDİ, tamamen auditor'ın kendi kararı geçerli.
        file_evidence = self._verify_file_action(task, step, active_folder)

        if any(p in result_text for p in _NO_RESULT_PATTERNS):
            passed = False
            audit_result = {
                "passed": False,
                "reason": "Deterministik kontrol: sonuç metni açıkça 'bulunamadı/sonuç yok' diyor "
                          "(auditor_ai'ye sorulmadı).",
            }
        else:
            audit_payload = {"step_description": step.get("description", ""), "agent_result": result}
            if file_evidence is not None:
                audit_payload["independent_file_verification"] = file_evidence
            audit = self.bus.send("orchestrator", "auditor_ai", "adım denetimi", payload=audit_payload)
            audit_result = audit.get("result", {})
            passed = bool(audit_result.get("passed"))

            if file_evidence is not None:
                verified_ok = bool(file_evidence.get("exists")) and file_evidence.get("content_matches") is not False
                if verified_ok and not passed:
                    passed = True
                    audit_result = {
                        "passed": True,
                        "reason": "Auditor emin olamadı, ama bağımsız pathlib doğrulaması dosyanın "
                                  "gerçekten oluşturulduğunu ve (varsa) içeriğin birebir eşleştiğini "
                                  "kanıtladı.",
                        "independent_file_verification": file_evidence,
                    }
                elif not verified_ok:
                    passed = False
                    audit_result = {
                        "passed": False,
                        "reason": file_evidence.get("error") or "Bağımsız doğrulama başarısız.",
                        "independent_file_verification": file_evidence,
                    }
                else:
                    audit_result = {**audit_result, "independent_file_verification": file_evidence}

        history = task["payload"].setdefault("history", [])
        history.append({"step": step, "result": result, "audit": audit_result, "passed": passed})
        self._safe_memory_event(step.get("agent", "?"), "step_finished",
                                 f"{step.get('description', '')[:120]} -> passed={passed}")
        if passed:
            task["payload"]["step_index"] = task["payload"].get("step_index", 0) + 1
            task["payload"]["audit_retries"] = 0
            # DÜZELTME (path-context, 2026-09-16): bu adım GERÇEKTEN bir
            # klasör oluşturduysa (yukarıdaki bağımsız doğrulama da bunu
            # onayladı), bir SONRAKİ executor_ai adımı artık bu klasörün
            # İÇİNDE çalışsın - bkz. _execute_step/_infer_executor_action.
            # Sadece True->yeni-deger yonunde; hicbir zaman GERIYE (daha
            # once basariyla girilmis bir klasorden CIKMAYA) neden olmaz.
            if step.get("agent") == "executor_ai":
                try:
                    _, inferred_params = self._infer_executor_action(
                        step.get("description", ""), active_folder)
                    if (inferred_params.get("action") == "create_folder"
                            and inferred_params.get("name")):
                        task["payload"]["_active_folder"] = str(
                            Path(inferred_params.get("path", ".")) / inferred_params["name"])
                except Exception:
                    pass  # bu, iyilestirici bir yardimci - asla gorevi cokertmemeli

            # Son adim basariyla bittiyse bir sonraki 20sn'lik tick'i
            # bekleme; gorevi AYNI tick icinde finalize et.
            plan = task["payload"].get("plan", [])
            current_index = task["payload"].get("step_index", 0)
            if plan and current_index >= len(plan):
                failed_steps = task["payload"].get("failed_steps", [])
                unresolved_steps = task["payload"].get("unresolved_steps", [])
                final_status = "failed" if (failed_steps or unresolved_steps) else "completed"
                total = len(plan)
                succeeded = total - len(failed_steps) - len(unresolved_steps)
                parts = [f"{succeeded}/{total} adim basarili."]
                if failed_steps:
                    parts.append(f"{len(failed_steps)} adim denendi ve basarisiz oldu.")
                if unresolved_steps:
                    parts.append(
                        f"{len(unresolved_steps)} adim desteklenmedigi icin "
                        f"(UNRESOLVED_AGENT) hic denenmedi."
                    )
                task["payload"]["finalized_in_finish_step"] = True
                task["result"] = " ".join(parts)
                task["status"] = final_status
                self.tasks.update(
                    task["id"],
                    status=final_status,
                    payload=task["payload"],
                    result=task["result"],
                )
                self._safe_memory_event(
                    "orchestrator",
                    "task_finished",
                    f"{task['name']} -> {final_status}",
                )
                self._notify_result(self.tasks.get(task["id"]))
        else:
            retries = task["payload"].get("audit_retries", 0) + 1
            task["payload"]["audit_retries"] = retries
            if retries > MAX_AUDIT_ROUNDS:
                # coder_ai basarisiz oldugunda rollback dene (14. bolum).
                # YENI (2026-09-16, UNRESOLVED_AGENT duzeltmesi): operation
                # == "analyze" ISE rollback DENENMEZ - analiz adimi hicbir
                # backup_create cagirmadi (bkz. _execute_step), yani geri
                # alinacak hicbir sey yok; yine de rollback denenirse en son
                # BASKA bir modify adiminin yedegini anlamsizca geri
                # yukleyip veri kaybina yol acabilirdi. operation == "modify"
                # (ya da eski/operation'siz gorevler) icin davranis AYNEN
                # KORUNDU.
                if step.get("agent") == "coder_ai" and step.get("operation") != "analyze":
                    try:
                        self.bus.send("orchestrator", "executor_ai", "rollback",
                                       payload={"action": "backup_rollback", "params": {}})
                    except Exception:
                        pass
                task["payload"]["step_index"] = task["payload"].get("step_index", 0) + 1
                task["payload"]["audit_retries"] = 0
                task["payload"].setdefault("failed_steps", []).append(step.get("description", ""))

    # ── Arka plan döngüsü (agent_loop.py ile AYNI desen) ─────────────────

    def _tick(self) -> None:
        # "5. Task watchdog" + "8. Dead-task detection": her turda, asıl
        # işten ÖNCE, kuyruktaki TÜM aktif görevleri tara. core/watchdog.py
        # kendi içinde hiçbir exception'ı dışarı sızdırmaz (bkz. o dosyanın
        # docstring'i) - yine de burada bir güvenlik ağı olarak sarmalanıyor,
        # çünkü bu yardımcı adım ANA görev döngüsünü ASLA durduramamalı.
        try:
            watchdog_actions = watchdog.scan_and_apply(self.tasks)
            for action in watchdog_actions:
                if action.get("action") == "marked_dead":
                    self._notify_result(self.tasks.get(action["task_id"]))
        except Exception as e:
            print(f"[BrainTeam] ⚠️ Watchdog taraması başarısız (görev motoru ETKİLENMEDİ): {e}")

        pending = self.tasks.list(status="pending")
        if not pending:
            return
        task = pending[0]
        payload = task["payload"]

        if "plan" not in payload:
            # Planner/LLM çağrısı ağ kotası veya geçici servis yoğunluğu
            # nedeniyle uzun sürebilir.  Görevi çağrıdan önce running yap;
            # aksi halde kullanıcı görev gerçekten işlenirken yanıltıcı
            # biçimde pending görüyordu.
            self.tasks.update(task["id"], status="running")
            plan_resp = self.bus.send("orchestrator", "planner_ai", "plan oluştur",
                                       payload={"goal": payload["goal"]})
            if plan_resp.get("status") != "completed":
                self.tasks.update(task["id"], status="failed", error=str(plan_resp.get("result")))
                self._safe_memory_event("orchestrator", "planning_failed", str(plan_resp.get("result"))[:200])
                self._notify_result(self.tasks.get(task["id"]))
                return
            payload["plan"] = plan_resp["result"]["steps"]
            payload["step_index"] = 0
            payload["history"] = []
            payload["audit_retries"] = 0
            self.tasks.update(task["id"], status="pending", payload=payload)
            return  # bu turda SADECE planlama yapildi, bir sonraki turda ilk adima gecilir

        plan = payload["plan"]
        idx = payload.get("step_index", 0)
        if idx >= len(plan):
            failed_steps = payload.get("failed_steps", [])
            unresolved_steps = payload.get("unresolved_steps", [])
            # DÜZELTME A (devamı): "denendi ve başarısız oldu" (failed_steps)
            # ile "hiç denenmedi çünkü uygun agent yoktu" (unresolved_steps)
            # ARTIK ayrı sayılıyor ve sonuç mesajında AÇIKÇA ayrıştırılıyor -
            # önceden ikisi de aynı "failed_steps" torbasına giriyordu.
            status = "failed" if (failed_steps or unresolved_steps) else "completed"
            total = len(plan)
            succeeded = total - len(failed_steps) - len(unresolved_steps)
            parts = [f"{succeeded}/{total} adım başarılı."]
            if failed_steps:
                parts.append(f"{len(failed_steps)} adım denendi ve başarısız oldu.")
            if unresolved_steps:
                parts.append(f"{len(unresolved_steps)} adım desteklenmediği için (UNRESOLVED_AGENT) hiç denenmedi.")
            self.tasks.update(task["id"], status=status, payload=payload,
                               result=" ".join(parts))
            self._safe_memory_event("orchestrator", "task_finished", f"{task['name']} -> {status}")
            self._notify_result(self.tasks.get(task["id"]))
            return

        step = plan[idx]
        if step.get("agent") == "unknown":
            # Planner agent belirleyemediyse mevcut capability resolver'i dene.
            description = step.get("description", "").strip()
            resolved = None

            try:
                if description:
                    resolved = capability_resolver.resolve_capability(description)
            except Exception as resolver_error:
                print(f"[BrainTeam] Resolver hatasi: {resolver_error}")

            if resolved:
                capability = resolved.get("capability")
                tool = resolved.get("tool")
                action = resolved.get("action")
                parameters = resolved.get("parameters") or {}

                if capability and tool and action:
                    step["agent"] = "executor_ai"
                    step["capability"] = capability
                    step["tool"] = tool
                    step["action"] = action
                    step["parameters"] = parameters
                    step["operation"] = "execute"

                    payload["plan"][idx] = step
                    self.tasks.update(task["id"], payload=payload)

                    print(
                        "[BrainTeam] Resolver -> executor_ai: "
                        f"{capability}/{tool}/{action}"
                    )
                else:
                    resolved = None

            if not resolved:
                reason = (
                    "UNRESOLVED_AGENT: Planner agent belirleyemedi ve "
                    "capability_resolver guvenli bir eslesme bulamadi. "
                    "Adim calistirilmadan atlandi."
                )

                payload.setdefault("unresolved_steps", []).append({
                    "description": description,
                    "reason": reason,
                })

                payload["step_index"] = idx + 1
                self.tasks.update(task["id"], payload=payload)
                return

        risk, reason = self._risk_of_step(step, task)
        if risk == "high":
            step_with_risk = {**step, "risk": risk, "reason": reason}
            payload["pending_step"] = step_with_risk
            self.tasks.update(task["id"], status=WAITING_APPROVAL, payload=payload)
            print(f"[BrainTeam] ℹ️ Onay bekleyen adım (id={task['id']}): "
                  f"[{step.get('agent')}] {step.get('description', '')[:80]} — risk={risk} ({reason})")
            # Eskiden burada kullaniciya HIC bildirim gitmiyordu; gorev
            # sessizce, suresiz bekliyordu.
            self._request_approval(self.tasks.get(task["id"]))
            return

        try:
            result = self._execute_step(task, step)
            self._finish_step(task, step, result)
            self.tasks.update(task["id"], payload=payload)
        except Exception as e:
            payload.setdefault("failed_steps", []).append(step.get("description", ""))
            payload["step_index"] = idx + 1
            self.tasks.update(task["id"], payload=payload)
            self._safe_memory_event(step.get("agent", "?"), "step_error", str(e)[:200])

    def _worker_loop(self, interval_seconds: float) -> None:
        print(f"[BrainTeam] ✅ AI Beyin Takımı başladı ({interval_seconds:.0f}sn'de bir kontrol).")
        while True:
            try:
                self._tick()
            except Exception as e:
                print(f"[BrainTeam] ⚠️ Beklenmeyen orchestrator hatası: {e}")
            time.sleep(interval_seconds)

    def start_background_loop(self, interval_seconds: float = DEFAULT_INTERVAL_S) -> None:
        with self._loop_lock:
            if self._loop_started:
                return
            self._loop_started = True
            threading.Thread(target=self._worker_loop, args=(interval_seconds,),
                              daemon=True, name="BrainOrchestrator").start()


# --- Tek-surec kilidi (kullanici onayli, 2026-09-16, "brain_tasks.json ---
# birden fazla surecin ayni anda uzerine yazmasi" teshisi):
#
# get_orchestrator() asagida SADECE surec-ici (in-process) bir singleton -
# eger IKI AYRI Python sureci (ör. Jarvis kisayoluna iki kez tiklanmasi,
# ya da self_improvement/virtual_brain'in bagimsiz calistirilmasi) ayni
# anda get_orchestrator()'i cagirirsa, HER SUREC kendi BAGIMSIZ
# BrainOrchestrator'ini ve arka plan tick dongusunu yaratir - ikisi de
# ayni tasks/brain_tasks.json'a kilitsiz yazar. Canli teste bagli
# tarihte gozlemlenen gorev kaybi (bkz. core/task_manager.py'deki teshis
# notu) BUNUNLA aciklaniyor. Asagidaki basit PID kilit dosyasi
# (tasks/.orchestrator.lock), YENI bir bagimlilik (psutil vb.) eklemeden,
# ikinci sureci ORCHESTRATOR BASLAMADAN ONCE engeller.


def _pid_running(pid: int) -> bool:
    """Verilen PID'li bir surec hala calisiyor mu? Emin olunamayan/
    beklenmedik durumda GUVENLI tarafta hata yapip 'calisiyor' say (yani
    yanlislikla IKINCI bir orchestrator baslatmaktansa, yanlislikla
    baslatmayi reddetmeyi tercih et)."""
    if sys.platform.startswith("win"):
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return True
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except Exception:
            return True


def _acquire_single_instance_lock(tasks_path: Path) -> bool:
    """tasks/.orchestrator.lock icine bu surecin PID'ini yazar - ama
    SADECE dosyada ZATEN CALISAN baska bir surecin PID'i yoksa (yani eski/
    canavar bir kilit dosyasini, o surec artik yoksa, sessizce devralir).
    Kilit dosyasi yazilamazsa (izin sorunu vb.) GUVENLI tarafta calismaya
    izin ver - bu, mevcut sistemi bozacak YENI bir engelleyici hata
    kaynagi olmamali."""
    lock_path = tasks_path.parent / ".orchestrator.lock"
    try:
        if lock_path.is_file():
            try:
                old_pid = int(lock_path.read_text(encoding="utf-8").strip())
            except Exception:
                old_pid = None
            if old_pid and old_pid != os.getpid() and _pid_running(old_pid):
                return False
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception:
        return True


_orchestrator: BrainOrchestrator | None = None
_orchestrator_lock = threading.Lock()


def get_orchestrator() -> BrainOrchestrator:
    global _orchestrator
    with _orchestrator_lock:
        if _orchestrator is None:
            if not _acquire_single_instance_lock(TASKS_PATH):
                raise RuntimeError(
                    "AI Beyin Takımı başlatılamadı: 'tasks/brain_tasks.json' "
                    "üzerinde ZATEN çalışan başka bir Jarvis süreci tespit "
                    "edildi (kilit dosyası meşgul: tasks/.orchestrator.lock). "
                    "İki sürecin aynı görev dosyasına aynı anda yazması veri "
                    "kaybına yol açabildiği için ikinci süreç başlatılmadı. "
                    "Önce diğer Jarvis penceresini/sürecini kapatın."
                )
            _orchestrator = BrainOrchestrator()
            _orchestrator.start_background_loop()
        return _orchestrator


def brain_team_tool(parameters: dict = None, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "status")).lower().strip()
    orch = get_orchestrator()

    # "7. User response guarantee": bu çağrıyı yapan canlı oturumu sakla -
    # böylece ARKA PLAN thread'i (bu çağrı çoktan dönmüş olsa bile), bir
    # görev çok sonra bittiğinde kullanıcıya geri bildirim verebilir
    # (bkz. __init__'teki _last_player notu ve _notify_result()).
    if player is not None:
        orch._last_player = player

    if action == "start":
        return orch.start_goal(params.get("goal", ""))
    if action == "status":
        return orch.list_status()
    if action == "approve":
        return orch.approve(params.get("task_id", ""))
    if action == "deny":
        return orch.deny(params.get("task_id", ""))
    if action == "health":
        # "2. Heartbeat/health check": kullanıcının doğrudan sorabileceği
        # bir action - her beynin gerçekten çalışıp çalışmadığını gösterir.
        return orch.get_team_health()
    if action == "agents":
        # YENİ (kullanıcı talimatı madde 4, Agent Registry): her beynin
        # gerçek modül/sınıf adını, kaynak koddan doğrulanmış "real"
        # bilgisini ve executor_ai için GERÇEKTEN çalıştırabildiği
        # capability listesini gösterir - capability_resolver.
        # get_agent_registry() dışında YENİ bir agent sistemi kurulmadı,
        # bu sadece onu okunabilir metne çeviren ince bir sunum katmanı.
        registry = capability_resolver.get_agent_registry()
        lines = ["AI Beyin Takımı - Agent Registry:"]
        for name, entry in registry.items():
            real = "✅ gerçek" if entry["real"] else "❌ BULUNAMADI"
            caps = f" — capabilities: {', '.join(entry['capabilities'])}" if entry["capabilities"] else ""
            lines.append(f"{name}: {entry['module']}.{entry['class']} ({real}){caps}")
        return "\n".join(lines)
    return f"Bilinmeyen action: '{action}'. start/status/approve/deny/health/agents kullanın."
