"""test_brain_team_build.py — Aşama 2.3: FILE_MODIFICATION/BUILD zincirinin
GERÇEK cihazda uçtan uca (end-to-end) doğrulama testi.

BU BİR ÜRETİM ÖZELLİĞİ DEĞİL. main.py / ui.py / actions/intent_router.py /
actions/windows_shell.py / Brain Team'in (Planner, Executor, Auditor,
Security) iç mantığına TEK SATIR bile DOKUNULMADI. Bu script SADECE mevcut,
DEĞİŞTİRİLMEMİŞ core/brain_orchestrator.py::brain_team_tool() giriş noktasını
- main.py'nin Gemini function-calling'i normalde brain_team'i nasıl
çağırıyorsa TAM OLARAK AYNI şekilde - doğrudan çağırıp, GERÇEK
planner_ai -> executor_ai -> file_controller -> security_ai -> auditor_ai
zincirinin uçtan uca, gerçekten çalışıp çalışmadığını doğruluyor.

ÖNEMLİ - ÇALIŞTIRMADAN ÖNCE:
core/brain_orchestrator.py'nin tek-süreç kilidi (tasks/.orchestrator.lock)
var - eğer GERÇEK Jarvis (main.py) şu anda ÇALIŞIYORSA, bu script KENDİ
orchestrator'ını başlatamaz (kasıtlı güvenlik davranışı - iki sürecin aynı
tasks/brain_tasks.json'a aynı anda yazması engellenir). Bu yüzden:
  1) Jarvis'i (main.py) KAPAT.
  2) Bu scripti FINAL_BUILD klasörünün İÇİNDE çalıştır:
       python test_brain_team_build.py
  3) Test bitince Jarvis'i istersen tekrar aç.

Ne yapar:
  1. brain_team_tool(action="start", goal=...) ile GERÇEK bir görev
     oluşturur (main.py'nin ZATEN yaptığı ile birebir aynı çağrı).
  2. Mevcut, değiştirilmemiş arka plan orchestrator döngüsünü (zaten var
     olan start_background_loop/_tick mekanizması) bekler - görev
     completed/failed/waiting_approval olana kadar periyodik olarak
     durumu okur (varsayılan en fazla 150sn).
  3. Sonucu ayrıntılı yazdırır: durum, hangi agent/operation seçildi,
     onay gerekip gerekmediği (waiting_approval ise risk/neden), gerçek
     executor sonucu, denetim (audit) kararı.
  4. BAĞIMSIZ olarak (orchestrator'a hiç sormadan) jarvis_build_test.txt
     dosyasını doğrudan diskten okuyup içeriğin TAM OLARAK beklenenle
     eşleşip eşleşmediğini pathlib ile doğrular - "agent öyle dedi" ile
     "gerçekten öyle oldu" arasındaki fark burada netleşiyor.

Bu script hiçbir YENİ tool/executor eklemiyor - sadece main.py'nin zaten
kullandığı GERÇEK, kamuya açık arayüzü (brain_team_tool + TaskManager)
çağırıyor.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_CONTENT = "JARVIS BUILD TEST OK"
TARGET_FILE = PROJECT_ROOT / "jarvis_build_test.txt"

# Planner'ın (gerçek LLM) adım açıklamasında tırnak/backtick içinde net bir
# dosya adı + içerik üretmesi ihtimalini artırmak için goal metni kasıtlı
# olarak _extract_quoted() deseniyle (bkz. core/brain_orchestrator.py) uyumlu,
# çok açık ve TEK bir işlemle sınırlı yazıldı.
GOAL = (
    "Proje kök dizininde SADECE şu tek işlemi yap: `jarvis_build_test.txt` "
    "adlı YENİ bir dosya oluştur. Dosyanın içeriği TAM OLARAK şu olsun (tek "
    "karakter bile değiştirme, tırnak işaretlerini içeriğe dahil etme): "
    "'JARVIS BUILD TEST OK'. Başka HİÇBİR dosyaya dokunma, başka HİÇBİR "
    "işlem yapma, yedek alma isteği ekleme - bu SADECE tek adımlık bir dosya "
    "oluşturma testidir."
)

TIMEOUT_SECONDS = 150
POLL_INTERVAL_SECONDS = 5


def main() -> int:
    print("=" * 70)
    print("AŞAMA 2.3 — FILE_MODIFICATION/BUILD zinciri uçtan uca testi")
    print("=" * 70)

    try:
        from jarvis.core.brain_orchestrator import brain_team_tool, get_orchestrator
    except Exception as e:
        print(f"✗ core.brain_orchestrator import edilemedi: {e}")
        return 1

    if TARGET_FILE.exists():
        print(f"\n⚠ UYARI: '{TARGET_FILE.name}' zaten var, testten ÖNCEKİ içerik:")
        print(" ", repr(TARGET_FILE.read_text(encoding="utf-8", errors="replace")))
        print("  (Bu test dosyayı silmiyor/önceden dokunmuyor - sadece okuyor;")
        print("   görev başarılı olursa üzerine yazılması beklenen/doğal bir durumdur.)")

    print("\n[1/3] Görev oluşturuluyor (brain_team_tool action=start)...")
    try:
        start_msg = brain_team_tool(parameters={"action": "start", "goal": GOAL})
    except RuntimeError as e:
        print(f"✗ Görev başlatılamadı: {e}")
        print("  (Bu genellikle GERÇEK Jarvis (main.py) hâlâ çalışıyor demektir -")
        print("   önce onu kapatıp tekrar deneyin.)")
        return 1
    print("  ->", start_msg)

    task_id = None
    if "(id:" in start_msg:
        task_id = start_msg.split("(id:", 1)[1].split(")", 1)[0].strip()
    if not task_id:
        print("✗ Görev id'si dönen mesajdan çıkarılamadı, test durduruluyor.")
        return 1

    orch = get_orchestrator()
    print(f"\n[2/3] Görev (id={task_id}) izleniyor (en fazla {TIMEOUT_SECONDS}sn, "
          f"{POLL_INTERVAL_SECONDS}sn aralıklarla)...")
    deadline = time.time() + TIMEOUT_SECONDS
    task = None
    last_status = None
    while time.time() < deadline:
        task = orch.tasks.get(task_id)
        if task is None:
            print("✗ Görev kayıtlarda bulunamadı.")
            return 1
        if task["status"] != last_status:
            print(f"  ... durum: {task['status']}")
            last_status = task["status"]
        if task["status"] in ("completed", "failed", "waiting_approval", "cancelled"):
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    else:
        print(f"✗ {TIMEOUT_SECONDS}sn içinde görev bir sonuca ulaşmadı "
              f"(son görülen durum: {last_status}).")

    print("\n--- GÖREV DETAYI ---")
    print("Durum       :", task.get("status") if task else "?")
    print("Sonuç       :", task.get("result") if task else "?")
    print("Hata        :", task.get("error") if task else "?")
    payload = (task or {}).get("payload", {})
    for i, entry in enumerate(payload.get("history", [])):
        step = entry.get("step", {})
        print(f"  Adım {i + 1}: agent={step.get('agent')} operation={step.get('operation')} "
              f"açıklama={step.get('description', '')[:80]!r}")
        print(f"           passed={entry.get('passed')} audit={entry.get('audit')}")
        print(f"           sonuç={str(entry.get('result'))[:200]!r}")
    if payload.get("pending_step"):
        ps = payload["pending_step"]
        print("  ONAY BEKLEYEN ADIM:", ps.get("agent"), repr(ps.get("description", "")[:80]),
              "risk=", ps.get("risk"), "-", ps.get("reason"))
    if payload.get("unresolved_steps"):
        print("  UNRESOLVED_AGENT adımları:", payload["unresolved_steps"])
    if payload.get("failed_steps"):
        print("  Başarısız adımlar:", payload["failed_steps"])

    print("\n[3/3] BAĞIMSIZ dosya doğrulaması (orchestrator'a hiç sormadan, doğrudan disk):")
    ok = True
    if not TARGET_FILE.is_file():
        print(f"✗ '{TARGET_FILE}' diskte bulunamadı.")
        ok = False
    else:
        actual = TARGET_FILE.read_text(encoding="utf-8")
        print(f"  Dosya var        : {TARGET_FILE}")
        print(f"  Beklenen içerik  : {EXPECTED_CONTENT!r}")
        print(f"  Gerçek içerik    : {actual!r}")
        if actual == EXPECTED_CONTENT:
            print("  ✓ İçerik TAM OLARAK eşleşiyor.")
        else:
            print("  ✗ İçerik EŞLEŞMİYOR.")
            ok = False

    success = ok and bool(task) and task.get("status") == "completed"
    print("\n" + "=" * 70)
    print("SONUÇ:", "✓ BAŞARILI" if success else "✗ BAŞARISIZ / İNCELEME GEREKİYOR")
    print("=" * 70)
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
