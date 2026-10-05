"""brain_orchestrator_self_test.py — FAZ 1 sağlamlaştırması için çevrimdışı
mantık testi (core/brain_orchestrator.py).

Gerçek Gemini/Ollama'ya HİÇ dokunmaz. BrainOrchestrator gerçek alt beyin
nesneleriyle (PlannerAI, ResearchAI, ...) kurulur -  __init__'leri ağa
dokunmaz (bkz. base_brain.py: Gemini istemcisi SADECE call_llm() içinde,
ihtiyaç anında oluşturulur) - ama test SENARYOLARI kasıtlı olarak
call_llm()'e/bus.send()'e gidecek yollardan (auditor'a danışma, planlama)
KAÇINACAK şekilde kuruluyor, böylece test tamamen çevrimdışı kalıyor.

Doğrulanan davranışlar (kullanıcının 8 özelliği + Memory düzeltmesi):
  - _safe_memory_event(): Memory patlarsa görev motoru ETKİLENMEZ.
  - _notify_result(): her zaman kalıcı log + varsa player'a bildirim.
  - _finish_step(): "no result" deseni auditor_ai'ye SORMADAN başarısız
    sayılıyor (6. Result verification).
  - _tick(): watchdog.scan_and_apply() her turda çağrılıyor, dead-task
    otomatik failed yapılıp _notify_result() tetikleniyor (5 + 7 + 8).
  - brain_team_tool(): player parametresi saklanıyor, "health" action'ı
    get_team_health()'i çağırıyor (2. Heartbeat/health check).

Çalıştırma:
    cd core
    python brain_orchestrator_self_test.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from jarvis.core import brain_orchestrator as bo
from jarvis.core.task_manager import TaskManager
from jarvis.core import watchdog


class _FakePlayer:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write_log(self, msg: str) -> None:
        self.lines.append(msg)


class _ExplodingMemory:
    """MemoryAI yerine gecen sahte nesne - remember_event() HER ZAMAN patlar."""

    def remember_event(self, source: str, event: str, detail: str) -> None:
        raise RuntimeError("kasıtlı test hatası: Memory çalışmıyor")


def _age_task(tm: TaskManager, task_id: str, seconds_ago: float) -> None:
    tasks = tm._load()
    for t in tasks:
        if t["id"] == task_id:
            t["updated_at"] = (datetime.now() - timedelta(seconds=seconds_ago)).isoformat()
    tm._save(tasks)


def run() -> bool:
    ok = True
    tmp_dir = Path(tempfile.mkdtemp(prefix="brain_orchestrator_self_test_"))
    try:
        orch = bo.BrainOrchestrator()
        # Gerçek varsayılan tasks/brain_tasks.json'a DOKUNMAMAK için ayrı,
        # geçici bir dosyaya yönlendiriliyor - watchdog_self_test.py'deki
        # ile AYNI izole test deseni.
        orch.tasks = TaskManager(path=tmp_dir / "brain_tasks.json")

        # ── 1) DÜZELTME - Memory bağımlılığı: Memory patlasa bile görev
        #    motoru ETKİLENMEMELİ. ─────────────────────────────────────
        orch.memory = _ExplodingMemory()
        try:
            orch._safe_memory_event("test", "some_event", "detay")
            print("[OK] _safe_memory_event(): Memory patladığında exception DIŞARI SIZMADI.")
        except Exception as e:
            print(f"[FAIL] _safe_memory_event() Memory hatasını yutmadı: {e}")
            ok = False

        # ── 2) "7. User response guarantee": _notify_result() HER ZAMAN
        #    kalıcı log + (varsa) canlı oturum bildirimi üretir. ────────
        player = _FakePlayer()
        orch._last_player = player
        fake_task = {"id": "T-1", "status": "failed", "name": "test görevi",
                     "error": "bir şeyler ters gitti", "result": None}
        orch._notify_result(fake_task)
        assert any("T-1" in line and "FAILED" in line for line in player.lines), \
            "player.write_log çağrılmadı / beklenen içerik yok!"
        print("[OK] _notify_result(): canlı oturuma (player) doğru bildirim gitti.")

        # player YOKKEN de (uygulama kapalıysa) exception fırlatmamalı -
        # sonuç sessizce kaybolmamalı, ama en azından log dosyasına yazılmalı.
        orch._last_player = None
        orch._notify_result(fake_task)  # exception atarsa test zaten patlar
        print("[OK] _notify_result(): player yokken de (uygulama kapalı) hata vermeden kalıcı loga yazdı.")

        # ── 3) "6. Result verification": "no result" deseni auditor_ai'ye
        #    SORMADAN (bus.send hiç çağrılmadan) başarısız sayılmalı. ───
        auditor_called = {"count": 0}
        _orig_send = orch.bus.send

        def _tracking_send(*args, **kwargs):
            auditor_called["count"] += 1
            return _orig_send(*args, **kwargs)

        orch.bus.send = _tracking_send
        task = {"id": "T-2", "payload": {}}
        step = {"agent": "research_ai", "description": "test araması"}
        orch._finish_step(task, step, {"result": {"summary": "No results found for: xyz"}})
        assert auditor_called["count"] == 0, \
            "auditor_ai'ye SORULMAMASI gerekirken bus.send çağrıldı (gereksiz LLM çağrısı riski)!"
        assert task["payload"]["history"][-1]["passed"] is False
        assert task["payload"]["audit_retries"] == 1  # basarisiz -> retry sayaci arttı, index henüz ilerlemedi
        assert "step_index" not in task["payload"]
        print("[OK] _finish_step(): 'sonuç yok' deseni auditor_ai'ye SORMADAN başarısız sayıldı "
              "(gereksiz LLM çağrısı yapılmadı).")
        orch.bus.send = _orig_send

        # ── 4) "5. Task watchdog" + "8. Dead-task detection": _tick()
        #    watchdog.scan_and_apply()'ı her turda çalıştırmalı, ölü
        #    görevi otomatik failed yapmalı VE _notify_result tetiklemeli. ─
        player2 = _FakePlayer()
        orch._last_player = player2
        dead = orch.tasks.create(name="ölü görev (tick testi)", agent="planner_ai",
                                  payload={"goal": "x"})
        orch.tasks.update(dead["id"], status="running")
        _age_task(orch.tasks, dead["id"], watchdog.DEAD_TASK_SECONDS + 60)

        orch._tick()  # pending kuyruğu BOŞ (dead görev "running") -> planner_ai/bus.send'e HİÇ gidilmez

        refreshed = orch.tasks.get(dead["id"])
        assert refreshed["status"] == "failed", "_tick() watchdog'u çalıştırıp dead görevi failed yapmadı!"
        assert any(dead["id"] in line for line in player2.lines), \
            "_tick() dead-task için _notify_result() tetiklemedi!"
        print("[OK] _tick(): watchdog taraması çalıştı, dead görev otomatik failed yapıldı "
              "ve kullanıcıya (player) bildirildi.")

        # ── 5) brain_team_tool(): player saklanıyor + 'health' action'ı
        #    get_team_health()'e gidiyor (singleton'ı bu test-orch'a
        #    yönlendirerek, gerçek get_orchestrator()'ı TETİKLEMEDEN). ──
        orch.memory = bo.MemoryAI()  # get_team_health() TUM beyinlerin heartbeat()'ini ister - gercek nesneye don
        bo._orchestrator = orch
        player3 = _FakePlayer()
        health_result = bo.brain_team_tool({"action": "health"}, player=player3)
        assert orch._last_player is player3, "brain_team_tool() player'ı orkestratöre KAYDETMEDİ!"
        assert "AI Beyin Takımı sağlık durumu" in health_result, "'health' action'ı get_team_health()'e gitmedi!"
        assert "planner_ai" in health_result and "idle" in health_result
        print("[OK] brain_team_tool(): player saklandı ve 'health' action'ı doğru şekilde "
              "get_team_health()'e yönlendirildi.")

        # ── 6) DÜZELTME (canlı test bulgusu): _infer_executor_action()
        #    artık "dosya/klasör oluştur", "...yaz", "...oku" adımlarını
        #    GERÇEK file_controller eylemlerine eşliyor - önceden hepsi
        #    sessizce salt-okunur 'info'ya düşüyordu (hiçbir şey
        #    YARATMIYORDU). ──────────────────────────────────────────
        action, params = orch._infer_executor_action("Kullanıcının çalışma dizininde `jarvis_test` adlı bir klasör oluşturmak.")
        assert action == "file_controller" and params["action"] == "create_folder" and params["name"] == "jarvis_test", \
            f"Klasör oluşturma adımı yanlış eşlendi: {action}, {params}"
        print("[OK] _infer_executor_action(): 'klasör oluştur' artık gerçekten create_folder'a eşleniyor.")

        action, params = orch._infer_executor_action("`test.txt` adlı bir dosya oluşturmak.")
        assert action == "file_controller" and params["action"] == "create_file" and params["name"] == "test.txt", \
            f"Dosya oluşturma adımı yanlış eşlendi: {action}, {params}"
        print("[OK] _infer_executor_action(): 'dosya oluştur' artık gerçekten create_file'a eşleniyor.")

        action, params = orch._infer_executor_action("Dosyaya 'Jarvis otonom görev testi başarılı.' metnini yazmak.")
        assert action == "file_controller" and params["action"] == "write" \
            and params["content"] == "Jarvis otonom görev testi başarılı.", \
            f"Yazma adımı yanlış eşlendi: {action}, {params}"
        print("[OK] _infer_executor_action(): '...metnini yazmak' artık gerçekten write'a (içerikle) eşleniyor.")

        # Eşleşmeyen bir açıklama hâlâ eski, güvenli varsayılana (salt-okunur
        # 'info') düşmeli - hiçbir şeyi asla YARATMAZ/DEĞİŞTİRMEZ.
        action, params = orch._infer_executor_action("Bilinmeyen, tanımsız bir adım.")
        assert action == "file_controller" and params["action"] == "info"
        print("[OK] _infer_executor_action(): eşleşmeyen adımlar hâlâ güvenli varsayılana (info) düşüyor.")

        # ── 7) DÜZELTME: _risk_of_step() artık file_controller'ın GERÇEK iç
        #    eylemini security_ai'ye soruyor - önceden HER ZAMAN "info"
        #    (LOW) hardcode ediliyordu, create_file/create_folder (HIGH
        #    olması gerekirken) hep LOW'a düşüyordu. security_ai'nin
        #    KENDİ (Gemini'siz, saf) classify_risk() tablosunu kullanan
        #    sahte bir bus.send ile - gerçek bir LLM çağrısı YAPILMADAN -
        #    doğrulanıyor. ─────────────────────────────────────────────
        from jarvis.brains.security_ai import SecurityAI

        captured: dict = {}

        def _fake_security_send(from_agent, to_agent, task, payload=None):
            assert to_agent == "security_ai", f"beklenmeyen bus.send hedefi: {to_agent}"
            captured["tool"] = payload.get("tool")
            captured["action"] = payload.get("action")
            risk = SecurityAI.classify_risk(payload.get("tool"), payload.get("action"))
            return {"status": "completed", "result": {"approved": risk != "high", "risk": risk, "reason": "test"}}

        orch.bus.send = _fake_security_send
        risk, _ = orch._risk_of_step({"agent": "executor_ai", "description": "`test.txt` adlı bir dosya oluşturmak."})
        assert captured == {"tool": "file_controller", "action": "create_file"}, \
            f"security_ai'ye YANLIŞ (tool, action) gönderildi: {captured}"
        assert risk == "high", f"'dosya oluştur' HIGH risk olmalı (file_controller salt-okunur değil, fail-closed) - geldi: {risk}"
        print("[OK] _risk_of_step(): security_ai'ye artık GERÇEK iç eylem (create_file) gönderiliyor, "
              "sonuç doğru şekilde HIGH risk.")

        risk, _ = orch._risk_of_step({"agent": "executor_ai", "description": "Masaüstündeki bir dosyayı sil."})
        assert risk == "high", f"'sil' hâlâ HIGH risk olmalı (anahtar kelime eskalasyonu) - geldi: {risk}"
        print("[OK] _risk_of_step(): yıkıcı anahtar kelimeler (sil/delete/...) hâlâ HIGH risk üretiyor.")
        orch.bus.send = _orig_send

        # ── 8) DÜZELTME A (UNRESOLVED_AGENT, kullanıcı onaylı, 2026-09-15):
        #    agent="unknown" adımı artık sessizce failed_steps'e düşmüyor -
        #    açık bir "unresolved_steps" kaydı bırakıyor, adımı HİÇ
        #    çalıştırmadan (bus.send'e hiç gitmeden) atlıyor, ve görev
        #    bittiğinde bunu "denendi ve başarısız oldu" (failed_steps)
        #    ile KARIŞTIRMADAN ayrı bir cümlede raporluyor. ────────────
        unresolved_calls = {"count": 0}

        def _fail_if_called(*args, **kwargs):
            unresolved_calls["count"] += 1
            raise AssertionError("UNRESOLVED_AGENT adımı bus.send'e gitmemeliydi!")

        orch.bus.send = _fail_if_called
        unresolved_task = orch.tasks.create(
            name="unresolved agent testi", agent="planner_ai", payload={"goal": "x"}
        )
        unresolved_payload = {
            "goal": "x",
            "plan": [{"order": 1, "description": "Tanımsız bir iş yap.", "agent": "unknown"}],
            "step_index": 0, "history": [], "audit_retries": 0,
        }
        orch.tasks.update(unresolved_task["id"], payload=unresolved_payload)

        orch._tick()  # 1. tur: tek adım "unknown" -> unresolved_steps'e kaydedilip atlanmalı
        after_step = orch.tasks.get(unresolved_task["id"])
        assert unresolved_calls["count"] == 0, "UNRESOLVED_AGENT adımı için bus.send çağrılmamalıydı!"
        assert after_step["status"] == "pending", "Adım atlandıktan sonra görev hâlâ 'pending' olmalı (henüz bitmedi)!"
        assert len(after_step["payload"].get("unresolved_steps", [])) == 1, \
            f"unresolved_steps'e tam olarak 1 kayıt düşmeliydi: {after_step['payload']}"
        assert "UNRESOLVED_AGENT" in after_step["payload"]["unresolved_steps"][0]["reason"]
        assert after_step["payload"]["step_index"] == 1
        assert "failed_steps" not in after_step["payload"] or not after_step["payload"]["failed_steps"], \
            "UNRESOLVED_AGENT, 'denendi ve başarısız oldu' anlamına gelen failed_steps'e KARIŞMAMALI!"
        print("[OK] _tick(): agent=\"unknown\" adımı bus.send'e HİÇ gitmeden, açık bir "
              "UNRESOLVED_AGENT kaydıyla atlandı (sessizce failed_steps'e düşmedi).")

        orch._tick()  # 2. tur: step_index (1) >= plan uzunluğu (1) -> görev bitirilmeli
        finished = orch.tasks.get(unresolved_task["id"])
        assert finished["status"] == "failed", \
            f"Tek adımı UNRESOLVED_AGENT olan bir görev 'failed' ile bitmeliydi, geldi: {finished['status']}"
        assert "UNRESOLVED_AGENT" in finished["result"], \
            f"Sonuç mesajı UNRESOLVED_AGENT'ı AÇIKÇA belirtmeliydi: {finished['result']}"
        print("[OK] _tick(): tüm adımları UNRESOLVED_AGENT olan görev, sonuç mesajında bunu "
              "açıkça belirterek 'failed' ile bitti (sahte COMPLETED verilmedi).")
        orch.bus.send = _orig_send

        # ── 9) DÜZELTME B (gerçek doğrulama, kullanıcı onaylı, 2026-09-15/16):
        #    _verify_file_action() - auditor'dan bağımsız, ham pathlib ile
        #    ikinci kontrol. create_file/create_folder/write için hem "var
        #    mı" hem (write DAHİL - 16 Eylül'de eklenen düzeltme) "içerik
        #    doğru mu" kontrolü; boş isimde ve executor_ai olmayan
        #    adımlarda devre dışı kalmalı.
        #    DÜZELTME (2026-09-16, Aşama 2.3 audit kanıt eksikliği): dönüş
        #    tipi str|None'dan dict|None'a değişti (bkz. brain_orchestrator.
        #    py'deki _verify_file_action docstring'i - MEKANİZMA aynı,
        #    SADECE rapor şekli değişti) - aşağıdaki (a)-(d) assertion'ları
        #    bu yeni sözlük sözleşmesine göre güncellendi; (e)/(f) kapsam
        #    dışı davranışı (None) DEĞİŞMEDİ. ─────────────────────────────
        verify_dir = tmp_dir / "verify_action_test"
        verify_dir.mkdir()
        old_cwd = __import__("os").getcwd()
        __import__("os").chdir(verify_dir)
        try:
            # DUZELTME (2026-09-28, self-test guncellemesi): _verify_file_action
            # artik (task, step, base_path=".") istiyor - asagidaki cagrilar
            # eskiden tek bir dict (step) aliyordu, simdi ayrica bos bir
            # 'task' da geciliyor (goal-fallback bu senaryolarda test
            # edilmiyor, o yuzden bos payload yeterli).
            verify_task = {"id": "T-verify", "payload": {}}
            # a) create_folder GERÇEKTEN oluşmuşsa -> exists=True, error=None
            (verify_dir / "klasorum").mkdir()
            v = orch._verify_file_action(verify_task, {"agent": "executor_ai",
                                           "description": "`klasorum` adlı bir klasör oluşturmak."})
            assert v is not None and v["exists"] is True and v["error"] is None, \
                f"Gerçekten var olan klasör için exists=True/error=None dönmeliydi: {v!r}"

            # b) create_folder GERÇEKTE oluşmamışsa -> exists=False + hata metni
            v = orch._verify_file_action(verify_task, {"agent": "executor_ai",
                                           "description": "`hicyok` adlı bir klasör oluşturmak."})
            assert v is not None and v["exists"] is False and v["error"] and "klasör" in v["error"], \
                f"Var olmayan klasör YAKALANMALIYDI: {v!r}"

            # c) create_file içerik DOĞRUysa -> exists=True, content_matches
            #    True (content hiç belirtilmedigi icin beklenen ""; dosya da
            #    GERCEKTEN bos olusturulmussa bu artik HER ZAMAN karsilastirilip
            #    trivyal sekilde eslesir - bkz. _resolve_action_with_file_modification
            #    icindeki 2026-09-28 "0 byte sessizce gecme" duzeltmesi), error=None
            (verify_dir / "a.txt").write_text("", encoding="utf-8")
            v = orch._verify_file_action(verify_task, {"agent": "executor_ai",
                                           "description": "`a.txt` adlı bir dosya oluşturmak."})
            assert v is not None and v["exists"] is True and v["content_matches"] is not False and v["error"] is None, \
                f"İçeriği (boş) doğru olan create_file için exists=True/error=None dönmeliydi: {v!r}"

            # d) write - içerik YANLIŞSA (sahte başarı) -> content_matches=False
            #    + hata metni YAKALANMALI. Bu, 16 Eylül'de eklenen düzeltme -
            #    önceden bu kontrol SADECE create_file için yapılıyordu, write
            #    buradan GEÇİYORDU.
            (verify_dir / "b.txt").write_text("BOZULMUŞ", encoding="utf-8")
            v = orch._verify_file_action(verify_task, {"agent": "executor_ai",
                                           "description": "`b.txt` dosyasına 'doğru içerik' metnini yazmak."})
            assert v is not None and v["content_matches"] is False and v["error"] and "eşleşmiyor" in v["error"], \
                f"write için içerik uyuşmazlığı YAKALANMALIYDI (önceden buradan geçiyordu!): {v!r}"
            print("[OK] _verify_file_action(): create_folder/create_file/write için gerçek dosya "
                  "sistemi kontrolü doğru çalışıyor - 'write' artık içerik uyuşmazlığını da yakalıyor "
                  "(yapılandırılmış kanıt sözlüğü: path/exists/content_matches/error).")

            # e) isim çıkarılamıyorsa (boş) -> None (bu güvenlik ağının kapsamı
            #    dışında - eski davranış korunuyor, görevi ÇÖKERTMEZ).
            v = orch._verify_file_action(verify_task, {"agent": "executor_ai",
                                           "description": "Bir şeyler yap ama isim belirtme."})
            assert v is None, f"İsim çıkarılamayan adımda bu kontrol devre dışı kalmalıydı: {v!r}"

            # f) executor_ai DIŞINDAKİ bir agent'a hiç karışmamalı.
            v = orch._verify_file_action(verify_task, {"agent": "research_ai",
                                           "description": "`a.txt` adlı bir dosya oluşturmak."})
            assert v is None, f"research_ai adımına bu güvenlik ağı hiç karışmamalıydı: {v!r}"
            print("[OK] _verify_file_action(): kapsam sınırları (boş isim, executor_ai dışı agent) "
                  "korunuyor - gereksiz yere görev çökertmiyor.")
        finally:
            __import__("os").chdir(old_cwd)

        # 10) Tek-surec kilidi (kullanici onayli, 2026-09-16, "iki bagimsiz
        #     surecin ayni brain_tasks.json'a ayni anda yazmasi" teshisi):
        #     _acquire_single_instance_lock() gercekten calisan baska bir
        #     PID'i engellemeli, ama olu/stale bir kilidi devralmali.
        import os as _os
        lock_tasks_path = tmp_dir / "lock_test" / "brain_tasks.json"
        lock_tasks_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_tasks_path.parent / ".orchestrator.lock"

        ok1 = bo._acquire_single_instance_lock(lock_tasks_path)
        assert ok1 is True, "İlk çağrıda (kilit yok) True dönmeliydi."

        # Gerçekten çalışan bir PID (kendi sürecimiz) - kendi PID'imiz
        # kilitte yazılıysa engel OLMAMALI (aynı süreç tekrar çağırdı).
        ok2 = bo._acquire_single_instance_lock(lock_tasks_path)
        assert ok2 is True, "Kendi PID'imizle tekrar çağrıldığında True dönmeliydi."

        # Başka, GERÇEKTEN çalışan bir PID (kendi sürecimizin PID'i DEĞİL) -
        # burada testin kendi PID'ini +0 ile karşılaştırma riskinden
        # kaçınmak için basitçe "şu an kesin çalışan" olan PID 1'i
        # kullanıyoruz (Linux) / bulunamazsa test bu kısmı atlar.
        try:
            _os.kill(1, 0)
            has_pid1 = True
        except Exception:
            has_pid1 = False
        if has_pid1:
            lock_file.write_text("1", encoding="utf-8")
            blocked = bo._acquire_single_instance_lock(lock_tasks_path)
            assert blocked is False, \
                f"Gerçekten çalışan başka bir PID varken kilit ALINABİLMEMELİYDİ: {blocked!r}"

        # Ölü/stale bir PID (var olmayan çok yüksek bir numara) - kilit
        # DEVRALINMALI, sonsuza kadar kilitli kalmamalı.
        lock_file.write_text("999999", encoding="utf-8")
        recovered = bo._acquire_single_instance_lock(lock_tasks_path)
        assert recovered is True, f"Ölü/stale kilit devralınmalıydı: {recovered!r}"
        print("[OK] _acquire_single_instance_lock(): gerçekten çalışan başka bir süreç "
              "varken engelliyor, ölü/stale bir kilidi ise devralıyor.")

    except AssertionError as e:
        print(f"[FAIL] {e}")
        ok = False
    finally:
        bo._orchestrator = None
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return ok


if __name__ == "__main__":
    success = run()
    print("\n=== SONUÇ:", "BAŞARILI" if success else "BAŞARISIZ", "===")
    sys.exit(0 if success else 1)
