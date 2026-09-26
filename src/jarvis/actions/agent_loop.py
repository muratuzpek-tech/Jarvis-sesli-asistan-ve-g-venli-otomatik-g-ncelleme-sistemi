"""
agent_loop.py — Jarvis'in arka planda, dakikada bir çalışan, hafızadaki
görev listesini işleyen otonom döngüsü.

KULLANICIYLA BİRLİKTE KARARLAŞTIRILAN TASARIM SINIRLARI:
  1. Görev listesini SADECE kullanıcı ekler (agent_loop tool'u ile,
     sesli/yazılı "şunu görev listeme ekle" komutuyla). Döngü kendi
     kendine yeni görev UYDURMAZ — sadece var olan bekleyen görevleri
     işler.
  2. Adımlar SADECE actions/tools_kopru.py'deki, zaten var olan ve
     güvenli sınırları test edilmiş araçlara yönlendirilir. Tanınmayan
     bir araç istenirse görev BAŞARISIZ sayılır — asla "o zaman kod
     yazıp çalıştırayım" gibi bir yedek yola düşülmez (bkz.
     tools_kopru.py'nin başındaki not — Downloads'ta bulunan başka
     Jarvis denemelerinde bunun ne kadar tehlikeli olabileceğini
     gördük).
  3. Yıkıcı/geri dönüşü zor bir adım (dosya silme/taşıma, bilgisayarı
     kapatma/yeniden başlatma, başkasına mesaj gönderme) ASLA doğrudan
     çalıştırılmaz — kullanıcıya (best-effort) bir bildirim gönderilir
     ve görev 'awaiting_approval' durumuna alınır. Kullanıcı normal
     şekilde Jarvis'e "onaylıyorum" diyene kadar hiçbir şey yapılmaz.
     TEK BİLİNÇLİ İSTİSNA: Downloads'ta keşfedilen bir aracın Jarvis'in
     KENDİ KODUNA entegre edilmesi (bkz. _run_discovery_scan altındaki
     not) — kullanıcı bunu AÇIKÇA, riski anlatıldıktan sonra istedi
     ("sormadan kendisi entegre etsin"), bu yüzden bu TEK adım onay
     BEKLEMEDEN çalışır; kalan tek güvenlik ağı entegrasyon.py'nin
     yedek+doğrulama+geri-alma standardıdır, kullanıcıya SONRADAN
     (whatsapp bildirimiyle) ne yapıldığı bildirilir.
  4. Tekrarlayan hatalar actions/resilience.py'nin error_log sistemiyle
     takip edilir — aynı sorun sürekli tekrarlarsa otomatik olarak daha
     sabırlı beklenir (devre kesici + artan backoff), sonsuz döngüyle
     API'yi/sistemine yormaz.
  5. Her tur (tick) bir görevde SADECE TEK bir adım ilerletir — çok
     adımlı bir hedef, birkaç turda tamamlanır. Bu, kullanıcının
     onaylaması gereken bir adımda döngünün "durup beklemesini",
     tamamı bir kerede patlak vermeden ilerlemesini sağlar.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from jarvis.actions.resilience import CircuitBreaker, call_with_resilience
from jarvis.actions.tools_kopru import (
    ALLOWED_TOOLS, TOOL_DESCRIPTIONS, NotAllowedTool, call_approved_tool,
    call_tool, is_destructive,
)
from jarvis.paths import memory_dir


TASKS_PATH      = memory_dir() / "agent_tasks.json"
LOG_PATH        = memory_dir() / "agent_loop_log.jsonl"

MAX_STEPS_PER_TASK = 20     # bir gorev bu kadar adimdan sonra otomatik "failed" sayilir
DEFAULT_INTERVAL_S = 60.0
MAX_PLANNING_RETRIES = 3    # geçici Gemini/Ollama hatasında sonsuz pending yok

_agent_breaker = CircuitBreaker(name="gemini-agentloop", failure_threshold=3, cooldown_seconds=60.0)

_loop_lock    = threading.Lock()
_loop_started = False
_tasks_lock   = threading.Lock()   # ayni anda dosyaya yazan iki thread olmasin


# --- Depolama --------------------------------------------------------------

def _load_tasks() -> list[dict]:
    try:
        if TASKS_PATH.is_file():
            data = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception as e:
        print(f"[AgentLoop] ⚠️ agent_tasks.json okunamadı: {e}")
    return []


def _save_tasks(tasks: list[dict]) -> None:
    import tempfile
    try:
        TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=TASKS_PATH.parent, delete=False,
            encoding="utf-8", suffix=".tmp",
        ) as tmp:
            json.dump(tasks, tmp, indent=2, ensure_ascii=False)
            temp_name = tmp.name
        Path(temp_name).replace(TASKS_PATH)
    except Exception as e:
        print(f"[AgentLoop] ⚠️ agent_tasks.json yazılamadı: {e}")


def _log_event(event: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        event = {"timestamp": datetime.now().isoformat(), **event}
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[AgentLoop] ⚠️ agent_loop_log.jsonl yazılamadı: {e}")


# --- Gemini planlama katmani -------------------------------------------------

def _get_api_key() -> str:
    from jarvis.core.secure_config import get_gemini_api_key
    return get_gemini_api_key()


def _get_model():
    from google import genai
    return genai.Client(api_key=_get_api_key())


_STEP_PROMPT_TEMPLATE = """Sen Jarvis'in arka planda calisan otonom gorev
motorusun. Bir hedefi, KESINLIKLE asagida listelenen araclari kullanarak,
TEK SEFERDE TEK ADIM ilerletiyorsun.

MUTLAK KURALLAR:
- SADECE asagida listelenen araclari kullan. Baska bir arac ADI UYDURMA.
- Hicbir sekilde Python kodu veya sistem komutu YAZMA/ONERME - sadece
  listelenen araclardan birini, parametreleriyle birlikte sec.
- Hedef zaten tamamlandiysa veya daha fazla adim gerekmiyorsa "done": true don.
- Hicbir arac uygun degilse veya emin degilsen, "tool": null ve "done": false
  don, "note" alaninda neden ilerleyemedigini acikla - ASLA uydurma bir arac
  adi kullanma.

KULLANILABILIR ARACLAR:
{tools_desc}

HEDEF: {goal}

SIMDIYE KADAR YAPILAN ADIMLAR:
{history}

SADECE gecerli JSON don, markdown/aciklama YOK:
{{
  "tool": "arac_adi" | null,
  "parameters": {{}},
  "done": true | false,
  "note": "bu adimda ne yapildigi/yapilamadigi hakkinda kisa aciklama"
}}
"""


def _strip_fences(text: str) -> str:
    text = re.sub(r"```(?:json)?", "", text).strip()
    return text.rstrip("`").strip()


def _decide_next_step(task: dict) -> dict:
    tools_desc = "\n".join(f"- {name}: {desc}" for name, desc in TOOL_DESCRIPTIONS.items())
    history = task.get("history", [])
    history_str = "\n".join(
        f"  {i+1}. [{h.get('tool')}] {h.get('note', '')} -> {str(h.get('result', ''))[:150]}"
        for i, h in enumerate(history)
    ) or "  (henüz adım atılmadı)"

    prompt = _STEP_PROMPT_TEMPLATE.format(
        tools_desc=tools_desc, goal=task.get("goal", ""), history=history_str,
    )

    def _call():
        client = _get_model()
        return client.models.generate_content(model="gemini-flash-latest", contents=prompt)

    from jarvis.actions.local_llm import generate_with_fallback
    raw_text = generate_with_fallback(
        lambda: call_with_resilience(_call, breaker=_agent_breaker, max_attempts=2, base_delay=2.0, max_delay=15.0),
        prompt_for_ollama=prompt,
        source="agent_loop",
    )
    text = _strip_fences(raw_text.strip())
    step = json.loads(text)
    if not isinstance(step, dict):
        raise ValueError("Model bir JSON nesnesi döndürmedi.")
    return step


# --- Gorev islemleri ---------------------------------------------------------

def add_task(goal: str) -> str:
    goal = (goal or "").strip()
    if not goal:
        return "Görev tanımı boş olamaz."
    task = {
        "id":         uuid.uuid4().hex[:8],
        "goal":       goal,
        "status":     "pending",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "history":    [],
        "pending_action": None,
    }
    with _tasks_lock:
        tasks = _load_tasks()
        tasks.append(task)
        _save_tasks(tasks)
    _log_event({"event": "task_added", "task_id": task["id"], "goal": goal})
    return f"Görev eklendi (id: {task['id']}): {goal}"


def list_tasks() -> str:
    tasks = _load_tasks()
    if not tasks:
        return "Görev listesi boş."
    lines = []
    for t in tasks[-15:]:
        extra = ""
        if t["status"] == "awaiting_approval" and t.get("pending_action"):
            pa = t["pending_action"]
            extra = f" — ONAY BEKLIYOR: [{pa.get('tool')}] {pa.get('note', '')}"
        lines.append(f"[{t['id']}] {t['status']}: {t['goal'][:80]}{extra}")
    return f"{len(tasks)} görev (son {len(lines)} tanesi):\n" + "\n".join(lines)


def _find_task(tasks: list[dict], task_id: str) -> dict | None:
    for t in tasks:
        if t["id"] == task_id:
            return t
    return None


def approve_task(task_id: str) -> str:
    """Onay bekleyen bir adimi GERCEKTEN calistirir - kullanici Jarvis'e
    normal konusma icinde 'onaylıyorum' dedigi an bu cagrilir."""
    with _tasks_lock:
        tasks = _load_tasks()
        task = _find_task(tasks, task_id)
        if task is None:
            return f"'{task_id}' id'li görev bulunamadı."
        if task["status"] != "awaiting_approval" or not task.get("pending_action"):
            return f"'{task_id}' onay bekleyen bir görev değil (durum: {task['status']})."

        pending = task["pending_action"]
        try:
            result = call_approved_tool(pending["tool"], pending["parameters"])
            task["history"].append({
                "tool": pending["tool"], "parameters": pending["parameters"],
                "note": pending.get("note", ""), "result": result, "approved": True,
            })
            task["pending_action"] = None
            task["status"] = "pending"   # devam adimlari icin donguye geri birak
            _save_tasks(tasks)
            _log_event({"event": "approved_and_executed", "task_id": task_id,
                        "tool": pending["tool"], "result": result})
            return f"Onaylandı ve gerçekleştirildi: {result}"
        except Exception as e:
            task["status"] = "failed"
            task["pending_action"] = None
            _save_tasks(tasks)
            _log_event({"event": "approved_but_failed", "task_id": task_id, "error": str(e)})
            return f"Onaylandı ama çalıştırılırken hata oluştu: {e}"


def deny_task(task_id: str) -> str:
    with _tasks_lock:
        tasks = _load_tasks()
        task = _find_task(tasks, task_id)
        if task is None:
            return f"'{task_id}' id'li görev bulunamadı."
        task["status"] = "failed"
        task["pending_action"] = None
        task["history"].append({"note": "Kullanıcı reddetti.", "result": "denied"})
        _save_tasks(tasks)
    _log_event({"event": "denied", "task_id": task_id})
    return f"'{task_id}' görevi iptal edildi."


# DUZELTME (kullanici onayli, 2026-09-16, "iptal et' dedigimde Jarvis
# 'remove komutu yok' diyor" teshisi): deny_task() SADECE onay bekleyen
# (awaiting_approval) bir ADIMI reddetmek icin tasarlanmisti - tool
# aciklamasi da modele bunu SADECE o baglamda kullanmasini soyluyordu.
# Kullanici SADECE onay bekleyen bir adimi degil, HENUZ ISLENMEKTE OLAN
# (pending) bir HEDEFI tamamen durdurmak/iptal etmek istedi - bu, deny'dan
# ANLAM OLARAK farkli (deny: "bu tek adimi reddediyorum", cancel: "bu
# hedefin tamamini artik istemiyorum"), o yuzden kullanicinin acik
# tercihiyle AYRI bir fonksiyon/aksiyon olarak eklendi (deny_task'in
# davranisi/anlami DEGISTIRILMEDI).
def cancel_task(task_id: str) -> str:
    """Bir hedefi TAMAMEN durdurur - durumu ne olursa olsun (pending,
    awaiting_approval, hatta done/failed) 'cancelled' yapar ki _tick()
    onu bir daha asla islemeye almasin (_tick sadece status=='pending'
    olanlari isler, 'cancelled' bu filtreye hicbir zaman uymaz)."""
    with _tasks_lock:
        tasks = _load_tasks()
        task = _find_task(tasks, task_id)
        if task is None:
            return f"'{task_id}' id'li görev bulunamadı."
        if task["status"] == "cancelled":
            return f"'{task_id}' görevi zaten iptal edilmişti."
        task["status"] = "cancelled"
        task["pending_action"] = None
        task["history"].append({"note": "Kullanıcı hedefi tamamen iptal etti.", "result": "cancelled"})
        _save_tasks(tasks)
    _log_event({"event": "cancelled", "task_id": task_id})
    return f"'{task_id}' görevi tamamen iptal edildi, bir daha işlenmeyecek."


def retry_task(task_id: str) -> str:
    """Requeue a terminal failed/cancelled task without losing its history."""
    with _tasks_lock:
        tasks = _load_tasks()
        task = _find_task(tasks, task_id)
        if task is None:
            return f"'{task_id}' id'li görev bulunamadı."
        if task.get("status") not in ("failed", "cancelled"):
            return f"'{task_id}' yeniden denenemez (durum: {task.get('status')})."
        task["status"] = "pending"
        task["updated_at"] = datetime.now().isoformat()
        task["pending_action"] = None
        task.pop("error", None)
        task["planning_retries"] = 0
        task.setdefault("history", []).append({"note": "Kullanıcı görevi yeniden denedi.", "result": "requeued"})
        _save_tasks(tasks)
    _log_event({"event": "retried", "task_id": task_id})
    return f"'{task_id}' görevi yeniden kuyruğa alındı."


def _notify_pending_approval(task: dict, tool: str, parameters: dict, note: str) -> None:
    """GUVENLIK NOTU (2026-09-15, YENIDEN UYGULANDI): Bu fonksiyon eskiden
    gercek _call_send_message('whatsapp') ile mesaj gonderiyordu.
    send_message.py'nin WhatsApp'ta alici aramasi TAM EŞLEŞME DEĞİL, ilk
    arama sonucunu secen bir alt-string aramasi - "me" gibi bir alici
    string'i, adinda "me" GECEN İLK kisiye (rastgele bir kisiye) gercekten
    mesaj gonderebiliyordu. Kullanici bunu kullaniciya SORMADAN gercek
    mesaj gittigini fark etti. Bu yuzden gercek gonderim TAMAMEN
    KALDIRILDI, sadece konsola loglaniyor. (Not: bu duzeltme bir kere daha
    kayboldu goruldu - muhtemelen self_improve/entegrasyon bu dosyayi
    yeniden yazdi; eger tekrar kaybolursa, kok nedenini bulup
    KALICI hale getirin - bkz. konusma gecmisi 2026-09-15.)"""
    print(f"[AgentLoop] ℹ️ Onay bekleyen adım (bildirim GÖNDERİLMEDİ - güvenlik "
          f"nedeniyle devre dışı): görev='{task['goal'][:60]}' araç=[{tool}] not={note}")


def _run_readonly_github_research(task: dict) -> bool:
    """GitHub salt-okunur görevini LLM planlayıcısından bağımsız yürütür."""
    goal = str(task.get("goal", ""))
    low = goal.casefold()
    if "github" not in low or not any(k in low for k in ("ara", "araştır", "proje", "açık kaynak")):
        return False
    query = "open source AI agent orchestration Python security testing"
    if "mikrofon" in low or "ses" in low:
        query = "open source speech recognition noise suppression Python"
    elif "ollama" in low or "yerel model" in low:
        query = "Ollama local LLM agent orchestration Python"
    elif "güvenli" in low or "security" in low:
        query = "open source secure AI agent sandbox Python"
    try:
        from jarvis.actions.github_arama import github_search
        result = github_search({"query": query, "min_stars": 10, "max_results": 5})
        task["status"] = "done"
        task["history"].append({"tool": "github_arama", "parameters": {"query": query},
                                 "note": "Salt-okunur; indirme/kurulum/çalıştırma yok.", "result": result})
        task["result"] = result
        _log_event({"event": "readonly_github_done", "task_id": task["id"], "query": query})
    except Exception as e:
        task["status"] = "failed"
        task["error"] = f"Salt-okunur GitHub araştırması başarısız: {e}"
        task["history"].append({"tool": "github_arama", "note": "Salt-okunur araştırma", "result": str(e)})
        _log_event({"event": "readonly_github_failed", "task_id": task["id"], "error": str(e)})
    return True


_MUTATING_FC_ACTIONS = {
    "create_file", "create_folder", "write", "find_replace",
    "delete", "delete_all_files", "move", "copy", "rename",
}


def _looks_like_file_mutation_goal(goal: str) -> bool:
    g = (goal or "").lower()
    return any(k in g for k in (
        "düzenle", "duzenle", "değiştir", "degistir", "yaz", "oluştur",
        "olustur", "sil", "taşı", "tasi", "kopyala", "yeniden adlandır",
    ))


def _has_mutating_file_step(history: list) -> bool:
    # DUZELTME (canli testte bulundu, 2026-09-22): model gecmiste HICBIR
    # gercek dosya degistirme adimi olmadan "done: true" diyebiliyordu ve
    # bu KORUKORUNE kabul ediliyordu (dosya "duzenlendi" denip aslinda hic
    # degismemisti). Simdi bir dosya-mutasyon hedefi icin, gecmiste
    # GERCEKTEN basarili bir mutasyon adimi arandi - yoksa "done" reddedilir.
    for h in history:
        if h.get("tool") == "file_controller":
            params = h.get("parameters") or {}
            if params.get("action") in _MUTATING_FC_ACTIONS:
                result = str(h.get("result", "")).lower()
                if not result.startswith(("could not", "access denied", "file not found", "not a file")):
                    return True
    return False


def _process_task(task: dict, tasks: list[dict]) -> None:
    task["status"] = "running"
    task["updated_at"] = datetime.now().isoformat()
    if _run_readonly_github_research(task):
        return
    step_count = len(task.get("history", []))
    if step_count >= MAX_STEPS_PER_TASK:
        task["status"] = "failed"
        task["history"].append({"note": f"{MAX_STEPS_PER_TASK} adım sınırı aşıldı.", "result": "max_steps"})
        _log_event({"event": "max_steps_exceeded", "task_id": task["id"]})
        return

    try:
        step = _decide_next_step(task)
    except Exception as e:
        error_text = str(e)
        quota_error = any(marker in error_text.upper() for marker in ("429", "RESOURCE_EXHAUSTED", "QUOTA EXCEEDED"))
        if quota_error:
            task["status"] = "failed"
            task["error"] = "Gemini API kotası doldu; kota yenilendiğinde görevi yeniden deneyin."
            task.setdefault("history", []).append({
                "note": "Planlama kota nedeniyle durduruldu.",
                "result": f"QUOTA_ERROR: {error_text}",
            })
            _log_event({"event": "planning_quota_exhausted", "task_id": task["id"]})
            return
        retries = int(task.get("planning_retries", 0)) + 1
        task["planning_retries"] = retries
        task.setdefault("history", []).append({
            "note": f"Planlama denemesi {retries}/{MAX_PLANNING_RETRIES} başarısız oldu.",
            "result": f"PLAN_ERROR: {error_text}",
        })
        _log_event({"event": "planning_failed", "task_id": task["id"], "error": error_text})
        if retries >= MAX_PLANNING_RETRIES:
            task["status"] = "failed"
            task["error"] = (
                f"Planner {MAX_PLANNING_RETRIES} denemede başarısız oldu; "
                f"Gemini/Ollama erişimini kontrol edin. Son hata: {e}"
            )
            _log_event({"event": "planning_failed_final", "task_id": task["id"], "error": task["error"]})
        # İlk iki geçici hatada pending kalır ve bir sonraki 60sn turunda
        # tekrar denenir; üçüncü hatada artık kullanıcıya açık failed verilir.
        return

    if step.get("done"):
        goal = task.get("goal", "")
        if _looks_like_file_mutation_goal(goal) and not _has_mutating_file_step(task.get("history", [])):
            task.setdefault("history", []).append({
                "note": ("Model görevi 'tamamlandı' saydı ama geçmişte gerçek bir "
                         "dosya değiştirme adımı bulunamadı - sahte tamamlanma "
                         "reddedildi, gerçek araç çağrısı zorlanacak."),
                "result": "REJECTED_FAKE_DONE",
            })
            _log_event({"event": "fake_done_rejected", "task_id": task["id"], "claimed_note": step.get("note", "")})
            return
        task["status"] = "done"
        task["planning_retries"] = 0
        task["history"].append({"note": step.get("note", "Tamamlandı."), "result": "done"})
        _log_event({"event": "task_done", "task_id": task["id"], "note": step.get("note", "")})
        return

    tool = step.get("tool")
    parameters = step.get("parameters") or {}
    note = step.get("note", "")

    if not tool:
        task["status"] = "failed"
        task["history"].append({"note": note or "Model uygun bir araç bulamadı.", "result": "no_tool"})
        _log_event({"event": "no_tool", "task_id": task["id"], "note": note})
        return

    if tool not in ALLOWED_TOOLS:
        # NotAllowedTool'u burada da kontrol ediyoruz (call_tool zaten
        # firlatir ama gorevi hemen ve acikca "failed" yapmak icin
        # erken donuyoruz) - ASLA bir "o zaman kod yaz" yedegine dusmuyoruz.
        task["status"] = "failed"
        task["history"].append({"note": f"Model tanınmayan bir araç istedi: '{tool}'.", "result": "not_allowed"})
        _log_event({"event": "not_allowed_tool", "task_id": task["id"], "tool": tool})
        return

    if is_destructive(tool, parameters):
        task["status"] = "awaiting_approval"
        task["pending_action"] = {"tool": tool, "parameters": parameters, "note": note}
        _log_event({"event": "awaiting_approval", "task_id": task["id"], "tool": tool, "note": note})
        _notify_pending_approval(task, tool, parameters, note)
        return

    try:
        result = call_tool(tool, parameters)
        task["planning_retries"] = 0
        task["history"].append({"tool": tool, "parameters": parameters, "note": note, "result": result})
        _log_event({"event": "step_ok", "task_id": task["id"], "tool": tool, "result": str(result)[:200]})
    except NotAllowedTool as e:
        task["status"] = "failed"
        task["history"].append({"note": str(e), "result": "not_allowed"})
        _log_event({"event": "not_allowed_tool", "task_id": task["id"], "tool": tool})
    except Exception as e:
        task["history"].append({"tool": tool, "parameters": parameters, "note": note, "result": f"HATA: {e}"})
        _log_event({"event": "step_failed", "task_id": task["id"], "tool": tool, "error": str(e)})
        # Tek bir adim hatasi gorevi hemen dusurmez - bir sonraki tick'te
        # model hatayi gorup farkli bir yaklasim deneyebilir. MAX_STEPS_PER_TASK
        # sonsuz donguye karsi ust siniri saglar.


def _notify_integration_result(result_msg: str) -> None:
    """GUVENLIK NOTU (2026-09-15, YENIDEN UYGULANDI) - ayni sebeple
    _notify_pending_approval'daki gibi gercek WhatsApp gonderimi
    KALDIRILDI, sadece konsola loglanir."""
    print(f"[AgentLoop] ℹ️ Entegrasyon sonucu (bildirim GÖNDERİLMEDİ - güvenlik "
          f"nedeniyle devre dışı): {result_msg}")


def _run_discovery_scan(tasks: list[dict]) -> bool:
    """Scan Downloads and turn each accepted candidate into an approval task.
    Discovery is intentionally read/analyze-only.  Integration writes executable
    code and therefore must remain behind the normal explicit approval path.
    """
    # Downloads may contain large, partially-downloaded folders or archives.
    # discovery.py recursively sizes and quarantines candidates, so running it
    # automatically can saturate disk I/O and RAM while a download is active.
    # Keep this feature opt-in; enable it deliberately with JARVIS_AUTO_DISCOVERY=1.
    if os.getenv("JARVIS_AUTO_DISCOVERY", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    try:
        from jarvis.actions.discovery import scan_downloads_once
    except Exception as e:
        print(f"[AgentLoop] ⚠️ discovery modülü yüklenemedi: {e}")
        return False
    try:
        found = scan_downloads_once()
    except Exception as e:
        print(f"[AgentLoop] ⚠️ Discovery taraması başarısız: {e}")
        return False

    changed = False
    for item in found:
        source_name = str(item.get("source_name", "bilinmeyen"))
        parameters = {
            "name": source_name,
            "description": item.get("description", ""),
            "quarantine_path": item.get("quarantine_path", ""),
        }
        task = {
            "id": uuid.uuid4().hex[:8],
            "goal": f"Keşfedilen aracı incele ve kaydet: {source_name}",
            "status": "awaiting_approval",
            "created_at": datetime.now().isoformat(),
            "history": [{
                "tool": "discovery_register",
                "parameters": parameters,
                "note": item.get("description", ""),
                "result": "Kullanıcı onayı bekleniyor; otomatik entegrasyon yapılmadı.",
            }],
            "pending_action": {
                "tool": "discovery_register",
                "parameters": parameters,
                "note": (
                    "İndirilen aday karantinada incelendi. Onay verilirse yalnızca "
                    "kayıt oluşturulur; Jarvis koduna otomatik entegrasyon yapılmaz."
                ),
            },
        }
        tasks.append(task)
        _log_event({
            "event": "discovery_awaiting_approval",
            "task_id": task["id"],
            "source_name": source_name,
        })
        _notify_pending_approval(
            task, "discovery_register", parameters, task["pending_action"]["note"]
        )
        changed = True
    return changed

def _tick() -> None:
    with _tasks_lock:
        tasks = _load_tasks()
        discovery_changed = _run_discovery_scan(tasks)
        # A task becomes running while its planner/tool step is in flight.
        # Selecting only pending tasks left every such task permanently stuck.
        active = [t for t in tasks if t.get("status") in ("pending", "running")]
        if active:
            active.sort(key=lambda item: str(item.get("created_at", "")))
            _process_task(active[0], tasks)
            _save_tasks(tasks)
        elif discovery_changed:
            _save_tasks(tasks)


def _worker_loop(interval_seconds: float) -> None:
    print(f"[AgentLoop] [OK] Başladı ({interval_seconds:.0f}sn'de bir kontrol).")
    while True:
        try:
            _tick()
        except Exception as e:
            print(f"[AgentLoop] ⚠️ Beklenmeyen hata: {e}")
        time.sleep(interval_seconds)


def start_background_loop(interval_seconds: float = DEFAULT_INTERVAL_S) -> None:
    global _loop_started
    with _loop_lock:
        if _loop_started:
            return
        _loop_started = True
        threading.Thread(
            target=_worker_loop, args=(interval_seconds,),
            daemon=True, name="AgentLoop",
        ).start()


# --- main.py'nin cagirdigi tool entrypoint'i --------------------------------

def agent_loop_tool(parameters: dict = None, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list")).lower().strip()

    if action == "add":
        return add_task(params.get("goal", ""))
    if action == "list":
        return list_tasks()
    if action == "approve":
        return approve_task(params.get("task_id", ""))
    if action == "deny":
        return deny_task(params.get("task_id", ""))
    # DUZELTME (kullanici onayli, 2026-09-16): "cancel" kanonik isim,
    # "remove" ise modelin kendiliginden denedigi (ve daha once
    # desteklenmedigi icin basarisiz olan) isim - ikisi de ayni fonksiyona
    # gider ki model hangisini secerse secsin calissin.
    if action in ("cancel", "remove"):
        return cancel_task(params.get("task_id", ""))
    if action == "retry":
        return retry_task(params.get("task_id", ""))
    return f"Bilinmeyen action: '{action}'. add/list/approve/deny/cancel/retry kullanın."
