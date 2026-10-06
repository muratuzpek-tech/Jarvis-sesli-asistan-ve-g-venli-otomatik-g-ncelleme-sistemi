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
  3. Açıkça salt-okunur işaretli olmayan her adım (tools_kopru.
     is_destructive, fail-closed: dosya değiştirme, mesaj gönderme,
     entegrasyon ve entegre edilmiş araçlar dahil) ASLA doğrudan
     çalıştırılmaz — görev 'awaiting_approval' durumuna alınır ve canlı
     oturuma (set_approval_hook) araç/argüman/hedef ile sorulur. Onay
     YALNIZCA kullanıcının gerçek bir sonraki turundaki "evet" cevabından
     gelir; modelin agent_loop aracıyla gönderdiği task_id onay sayılmaz.
     Keşfedilen bir aracın kaydı/entegrasyonu da istisnasız bu yoldan geçer.
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
    call_tool,
)
from jarvis import security_gate as _gate
from jarvis.paths import memory_dir
from jarvis.core.audit_log import format_params, log_action, log_tool_event


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


def _audit(event: str, task: dict, tool, parameters, *, result="", approved=False,
           executed=False, verdict=None) -> None:
    """Onay istegi, onay, ret ve yurutme olaylari TEK denetim kaydina
    (JARVIS_HOME/memory/audit.log). Kayit hatasi gorev akisini bozmaz."""
    try:
        log_tool_event(source="agent_loop", event=event, tool=str(tool or "?"),
                       params=parameters if isinstance(parameters, dict) else {},
                       result=str(result)[:500], approved=approved, executed=executed,
                       verdict=verdict, task_id=str(task.get("id", "")),
                       risk="low" if verdict == "allow" else "high")
    except Exception as e:
        print(f"[AgentLoop] ⚠️ audit yazılamadı: {e}")


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
    try:
        step = json.loads(text)
    except json.JSONDecodeError as decode_err:
        # Ollama/Gemini bazen kesik veya hafif bozuk JSON donuyor; eskiden
        # bu planlama denemesini dogrudan basarisiz sayiyordu.
        try:
            from json_repair import repair_json
        except ImportError:
            raise decode_err from None
        step = repair_json(text, return_objects=True)
        if not isinstance(step, dict) or not step:
            raise ValueError("Model çıktısı JSON'a onarılamadı.") from decode_err
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


def approve_task(task_id: str, *, expected_action: dict,
                 grant: _gate.Grant | None = None) -> str:
    """Onay bekleyen bir adimi GERCEKTEN calistirir. YALNIZCA main.py'nin
    kullanici-turu onay yolu (_handle_agent_loop_reply) cagirir; Gemini
    araci (agent_loop_tool) bu fonksiyona ulasamaz. expected_action,
    kullaniciya sorulan ve onun onayladigi adimdir: gorevdeki bekleyen adim
    o arada degistiyse hicbir sey calistirilmaz.

    Calistirmadan hemen once kapi sorulur (authorize, kaynak AGENT_LOOP):
    DENY -> calismaz; NEEDS_APPROVAL -> yalnizca depodaki kendi istegini
    tuketen kullanici onayinin Grant'i ile ve Grant'in parmak izi calisacak
    cagrininkiyle birebir ayniysa."""
    with _tasks_lock:
        tasks = _load_tasks()
        task = _find_task(tasks, task_id)
        if task is None:
            return f"'{task_id}' id'li görev bulunamadı."
        if task["status"] != "awaiting_approval" or not task.get("pending_action"):
            return f"'{task_id}' onay bekleyen bir görev değil (durum: {task['status']})."

        pending = task["pending_action"]
        decision = _gate.authorize(str(pending.get("tool") or ""),
                                   pending.get("parameters") or {}, _gate.Source.AGENT_LOOP)
        if decision.verdict is _gate.Verdict.DENY:
            _log_event({"event": "gate_denied", "task_id": task_id, "tool": pending.get("tool")})
            _audit("gate_denied", task, pending.get("tool"), pending.get("parameters"),
                   result=decision.reason, verdict=decision.verdict.value)
            return f"'{task_id}' adımı güvenlik kapısı tarafından reddedildi; çalıştırılmadı."
        if decision.verdict is _gate.Verdict.NEEDS_APPROVAL and (
                grant is None or grant.fingerprint != decision.call.fingerprint):
            _log_event({"event": "approval_missing", "task_id": task_id})
            _audit("approval_missing", task, pending.get("tool"), pending.get("parameters"),
                   result="geçerli kullanıcı onayı yok; çalıştırılmadı",
                   verdict=decision.verdict.value)
            return f"'{task_id}' için geçerli kullanıcı onayı yok; hiçbir şey çalıştırılmadı."
        if pending != expected_action:
            _log_event({"event": "approval_mismatch", "task_id": task_id})
            _audit("approval_mismatch", task, pending.get("tool"), pending.get("parameters"),
                   result="bekleyen adım onaylanan adımla aynı değil; çalıştırılmadı")
            return (f"'{task_id}' için bekleyen adım, onaylanan adımla aynı değil; "
                    "hiçbir şey çalıştırılmadı.")
        try:
            # Arac YALNIZCA kapinin cozdugu argumanlarla calisir (modelin /
            # planner'in confirm_code'u cagrinin parcasi degildir).
            params = _gate.prepare(decision, grant)
            result = call_approved_tool(pending["tool"], params)
            task["history"].append({
                "tool": pending["tool"], "parameters": pending["parameters"],
                "note": pending.get("note", ""), "result": result, "approved": True,
            })
            task["pending_action"] = None
            task["status"] = "pending"   # devam adimlari icin donguye geri birak
            _save_tasks(tasks)
            _log_event({"event": "approved_and_executed", "task_id": task_id,
                        "tool": pending["tool"], "result": result})
            _audit("approved_executed", task, pending["tool"], pending["parameters"],
                   result=result, approved=True, executed=True, verdict="needs_approval")
            return f"Onaylandı ve gerçekleştirildi: {result}"
        except Exception as e:
            task["status"] = "failed"
            task["pending_action"] = None
            _save_tasks(tasks)
            _log_event({"event": "approved_but_failed", "task_id": task_id, "error": str(e)})
            _audit("approved_failed", task, pending.get("tool"), pending.get("parameters"),
                   result=f"HATA: {e}", approved=True, executed=True, verdict="needs_approval")
            return f"Onaylandı ama çalıştırılırken hata oluştu: {e}"


def deny_task(task_id: str) -> str:
    with _tasks_lock:
        tasks = _load_tasks()
        task = _find_task(tasks, task_id)
        if task is None:
            return f"'{task_id}' id'li görev bulunamadı."
        pending = task.get("pending_action") or {}
        task["status"] = "failed"
        task["pending_action"] = None
        task["history"].append({"note": "Kullanıcı reddetti.", "result": "denied"})
        _save_tasks(tasks)
    _log_event({"event": "denied", "task_id": task_id})
    _audit("denied", task, pending.get("tool"), pending.get("parameters"),
           result="kullanıcı reddetti", verdict="needs_approval")
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
        pending = task.get("pending_action") or {}
        task["status"] = "cancelled"
        task["pending_action"] = None
        task["history"].append({"note": "Kullanıcı hedefi tamamen iptal etti.", "result": "cancelled"})
        _save_tasks(tasks)
    _log_event({"event": "cancelled", "task_id": task_id})
    _audit("cancelled", task, pending.get("tool") or "agent_loop", pending.get("parameters"),
           result="görev iptal edildi")
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
    print(f"[AgentLoop] ℹ️ Onay bekleyen adım: görev='{task['goal'][:60]}' "
          f"araç=[{tool}] not={note}")
    _announce(task)


# --- Kullanici onayi (main.py'nin canli oturumu uzerinden) -------------------
#
# Onay bekleyen bir adim, main.py'nin Brain Team icin de kullandigi tek
# kullanimlik, 60 sn TTL'li, parmak izi (gorev kimligi + arac + argumanlar)
# bagli onay yuvasina kaydedilir; onay yalnizca kullanicinin gercek bir
# sonraki turundaki "evet"/"hayir" cevabindan gelir. Modelin agent_loop
# araciyla gonderdigi task_id ya da kod onay sayilmaz (bkz. agent_loop_tool).
_approval_hook = None


def set_approval_hook(hook) -> None:
    """hook(task_id, pending_action, message) -> bool. Canli oturum bekleyen
    adimi kullaniciya sorabildiyse True doner. Kayit aninda bekleyen en eski
    gorev hemen sorulur (ör. onceki oturumdan kalan)."""
    global _approval_hook
    _approval_hook = hook
    announce_next_pending()


def get_pending_action(task_id: str) -> dict | None:
    """Gorev hala onay bekliyorsa bekleyen adimin bir kopyasi."""
    task = _find_task(_load_tasks(), task_id)
    if task is None or task.get("status") != "awaiting_approval":
        return None
    pending = task.get("pending_action")
    return json.loads(json.dumps(pending)) if isinstance(pending, dict) else None


def _pending_target(tool: str, params: dict) -> str:
    if tool == "file_controller":
        try:
            from jarvis.actions.file_controller import _resolve_path
            base = _resolve_path(str(params.get("path") or "desktop"))
            name = str(params.get("name") or "")
            target = str((base / name if name else base).resolve())
            if params.get("destination"):
                target += f" → {_resolve_path(str(params['destination'])).resolve()}"
            return target
        except Exception:
            return f"{params.get('path', '')}/{params.get('name', '')}"
    for key in ("receiver", "source_name", "quarantine_path", "path", "file_path", "query"):
        if params.get(key):
            return str(params[key])
    return "—"


def describe_pending_action(task: dict) -> str:
    pending = task.get("pending_action") or {}
    tool = str(pending.get("tool") or "?")
    params = pending.get("parameters") or {}
    # Gizli argumanlar "***", icerik alanlari yalnizca uzunluk.
    args = format_params(params, limit=120) or "—"
    return (
        f"Onay gerekiyor: arka plan görevi '{str(task.get('goal', ''))[:80]}' şu adımı "
        f"çalıştırmak istiyor. Araç: {tool}. Argümanlar: {args}. "
        f"Hedef: {_pending_target(tool, params)}. "
        f"Onaylamak için 'evet', iptal etmek için 'hayır' deyin. (görev id={task.get('id')})"
    )


def _announce(task: dict) -> bool:
    hook = _approval_hook
    pending = task.get("pending_action")
    if hook is None or not isinstance(pending, dict):
        return False
    try:
        return bool(hook(task["id"], json.loads(json.dumps(pending)),
                         describe_pending_action(task)))
    except Exception as e:
        print(f"[AgentLoop] ⚠️ Onay isteği iletilemedi: {e}")
        return False


def announce_next_pending() -> None:
    """Onay bekleyen en eski gorevi kullaniciya sorar. Ayni gorev zaten
    soruluyorsa ya da baska bir onay bekliyorsa canli oturum bunu reddeder
    (tekrar konusmaz, baskasinin onay yuvasini ezmez); _tick her turda
    yeniden dener."""
    if _approval_hook is None:
        return
    waiting = [t for t in _load_tasks()
               if t.get("status") == "awaiting_approval" and t.get("pending_action")]
    if waiting:
        waiting.sort(key=lambda t: str(t.get("created_at", "")))
        _announce(waiting[0])


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
        _audit("executed", task, "github_arama", {"query": query}, result=result,
               executed=True, verdict="allow")
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
        log_action(module="agent_loop", action="task_done",
                   detail=f"task={task['id']} goal={task.get('goal', '')[:100]}",
                   risk="medium", result="SUCCESS")
        return

    tool = step.get("tool")
    parameters = step.get("parameters") or {}
    note = step.get("note", "")

    if not tool:
        task["status"] = "failed"
        task["history"].append({"note": note or "Model uygun bir araç bulamadı.", "result": "no_tool"})
        _log_event({"event": "no_tool", "task_id": task["id"], "note": note})
        log_action(module="agent_loop", action="task_failed",
                   detail=f"task={task['id']} reason=no_tool",
                   risk="medium", result="FAILED")
        return

    if tool not in ALLOWED_TOOLS:
        # NotAllowedTool'u burada da kontrol ediyoruz (call_tool zaten
        # firlatir ama gorevi hemen ve acikca "failed" yapmak icin
        # erken donuyoruz) - ASLA bir "o zaman kod yaz" yedegine dusmuyoruz.
        task["status"] = "failed"
        task["history"].append({"note": f"Model tanınmayan bir araç istedi: '{tool}'.", "result": "not_allowed"})
        _log_event({"event": "not_allowed_tool", "task_id": task["id"], "tool": tool})
        return

    # Calistirmadan hemen once kapi (kaynak AGENT_LOOP): DENY -> calismaz,
    # NEEDS_APPROVAL -> kullaniciya sorulur, ALLOW -> kapinin cozdugu
    # argumanlarla calisir.
    decision = _gate.authorize(tool, parameters, _gate.Source.AGENT_LOOP)
    if decision.verdict is _gate.Verdict.DENY:
        task["status"] = "failed"
        task["history"].append({"tool": tool, "note": f"Güvenlik kapısı reddetti: {decision.reason}",
                                "result": "gate_denied"})
        _log_event({"event": "gate_denied", "task_id": task["id"], "tool": tool})
        _audit("gate_denied", task, tool, parameters, result=decision.reason,
               verdict=decision.verdict.value)
        return

    if decision.verdict is _gate.Verdict.NEEDS_APPROVAL:
        task["status"] = "awaiting_approval"
        task["pending_action"] = {"tool": tool, "parameters": parameters, "note": note}
        _log_event({"event": "awaiting_approval", "task_id": task["id"], "tool": tool, "note": note})
        _audit("approval_requested", task, tool, parameters, result=note, verdict="needs_approval")
        _notify_pending_approval(task, tool, parameters, note)
        return

    try:
        result = call_tool(tool, _gate.prepare(decision, None))
        task["planning_retries"] = 0
        task["history"].append({"tool": tool, "parameters": parameters, "note": note, "result": result})
        _log_event({"event": "step_ok", "task_id": task["id"], "tool": tool, "result": str(result)[:200]})
        _audit("executed", task, tool, parameters, result=result, executed=True, verdict="allow")
    except NotAllowedTool as e:
        task["status"] = "failed"
        task["history"].append({"note": str(e), "result": "not_allowed"})
        _log_event({"event": "not_allowed_tool", "task_id": task["id"], "tool": tool})
    except Exception as e:
        task["history"].append({"tool": tool, "parameters": parameters, "note": note, "result": f"HATA: {e}"})
        _log_event({"event": "step_failed", "task_id": task["id"], "tool": tool, "error": str(e)})
        _audit("execution_failed", task, tool, parameters, result=f"HATA: {e}",
               executed=True, verdict="allow")
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
        _audit("approval_requested", task, "discovery_register", parameters,
               result="keşif kaydı onay bekliyor", verdict="needs_approval")
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
            current = active[0]
            current["status"] = "running"
            current["updated_at"] = datetime.now().isoformat()
            _save_tasks(tasks)
        elif discovery_changed:
            _save_tasks(tasks)
    if not active:
        # Kullanici onceki soruya cevap vermeden baska bir seyle devam ettiyse
        # canli oturum bekleyen onayi temizler; gorev burada yeniden sorulur.
        announce_next_pending()
        return

    # LLM cagrisi (retry beklemeleri + Ollama yedegi dakikalar surebilir)
    # KILIT DISINDA yapilir. Eskiden kilit bu sure boyunca tutuluyordu;
    # o sirada add_task/approve/cancel cagiran thread (Qt arayuzu ya da
    # canli ses oturumunun event loop'u) dakikalarca donuyordu.
    _process_task(current, [])

    with _tasks_lock:
        latest = _load_tasks()
        for idx, task in enumerate(latest):
            if task.get("id") != current.get("id"):
                continue
            # Islem surerken kullanici gorevi iptal ettiyse onu ezme.
            if task.get("status") != "cancelled":
                latest[idx] = current
            break
        _save_tasks(latest)
    announce_next_pending()


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

def agent_loop_tool(parameters: dict | None = None, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "list")).lower().strip()

    if action == "add":
        return add_task(params.get("goal", ""))
    if action == "list":
        return list_tasks()
    if action == "approve":
        # Model onay VEREMEZ: modelin gonderdigi task_id ya da kod onay
        # sayilmaz. Onay yalnizca kullanicinin gercek "evet" cevabindan gelir
        # (main.py: _handle_agent_loop_reply).
        _log_event({"event": "model_approve_refused", "task_id": params.get("task_id", "")})
        return ("Bu araçla onay verilemez. Onay bekleyen adım kullanıcıya soruldu; "
                "yalnızca kullanıcının kendi 'evet' ya da 'hayır' cevabı geçerlidir.")
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
    return f"Bilinmeyen action: '{action}'. add/list/deny/cancel/retry kullanın."
