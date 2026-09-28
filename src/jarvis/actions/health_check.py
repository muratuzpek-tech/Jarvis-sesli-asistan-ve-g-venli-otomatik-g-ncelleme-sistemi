"""Jarvis'in kendi kendini kontrol etmesini saglayan saglik taramasi.

v2: AgentSpace'in "entegrasyon durumunu asla tahmin etme" fikrinden ilham
alinarak, her kontrol artik UC NET DURUMDAN birini raporluyor:
  - "ok"       : baglandi VE tam calisiyor
  - "degraded" : baglandi AMA bir sinirlama var (model yok, yavas, vb.)
  - "down"     : hic baglanti yok / kullanilamiyor
Eskiden "Ollama calisiyor ama model yok" gibi durumlar belirsiz sekilde
"basarili" (True) sayiliyordu - artik acikca "degraded" olarak isaretleniyor,
boylece Jarvis asla "muhtemelen calisir" gibi belirsiz bir sey soylemiyor.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()

# Her kontrol fonksiyonu (status, detail) dondurur; status "ok"/"degraded"/"down".
_ICONS = {"ok": "✅", "degraded": "⚠️", "down": "❌"}


def _check_ollama() -> tuple[str, str]:
    try:
        import requests
        resp = requests.get("http://localhost:11434/api/tags", timeout=3)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        if models:
            return "ok", f"Ollama çalışıyor, {len(models)} model yüklü ({', '.join(models[:3])})"
        return "degraded", "Ollama çalışıyor AMA hiç model indirilmemiş — 'ollama pull <model>' gerekiyor"
    except Exception as e:
        return "down", f"Ollama'ya ulaşılamıyor ({type(e).__name__}) — 'ollama serve' çalışıyor mu kontrol et"


def _check_microphone() -> tuple[str, str]:
    """Cihaz secimi actions/audio_devices.py'den gelir - main.py'nin
    gercek akista kullandigi ayni oncelik sirasi (kullanici tercihi →
    adinda 'mikrofon' gecen → yasakli olmayan → yasakli dahil her sey)."""
    try:
        import sounddevice as sd
        import numpy as np
        from jarvis.actions.audio_devices import candidates_with_tier, device_name

        tiers = candidates_with_tier("input")
        device, tier = tiers[0]
        if tier == "system_default" and len(tiers) == 1:
            return "down", "Kullanılabilir mikrofon cihazı bulunamadı"

        name = device_name(device)
        duration = 1.0
        recording = sd.rec(int(duration * 16000), samplerate=16000, channels=1,
                            dtype="int16", device=device)
        sd.wait()
        peak = int(np.abs(recording).max())
        if peak < 50:
            return "degraded", f"Mikrofon ('{name}') bağlı AMA şu an sessiz görünüyor (seviye: {peak}) — konuşurken tekrar dene"
        return "ok", f"Mikrofon ('{name}') sinyal alıyor (seviye: {peak})"
    except Exception as e:
        return "down", f"Mikrofon kontrolü başarısız: {type(e).__name__}: {e}"


def _check_speaker() -> tuple[str, str]:
    """Hoparloru gercekten CALDIRMADAN, secilen cikis cihazinin PortAudio
    tarafindan gecerli/acilabilir oldugunu dogrular. 'Ses gercekten
    duyuluyor mu' subjektif oldugu icin test edilemez, ama en azindan
    yanlislikla sanal bir kabloya/mapper'a dusup dusmedigimizi (main.py'de
    yasanan 'yanit veriyor ama sesi duyulmuyor' hatasinin tipik sebebi)
    tespit eder."""
    try:
        import sounddevice as sd
        from jarvis.actions.audio_devices import candidates_with_tier, device_name

        tiers = candidates_with_tier("output")
        device, tier = tiers[0]
        if tier == "system_default" and len(tiers) == 1:
            return "down", "Kullanılabilir hoparlör/kulaklık cihazı bulunamadı"

        name = device_name(device)
        try:
            sd.check_output_settings(device=device, samplerate=24000, channels=1, dtype="int16")
        except Exception as e:
            return "down", f"Hoparlör ('{name}') açılamıyor: {type(e).__name__}: {e}"

        if tier == "fallback_excluded":
            return "degraded", (f"Sadece sanal/eşleştirici bir çıkış bulundu ('{name}') "
                                 f"— gerçek hoparlör/kulaklık bağlı olmayabilir")
        if tier == "hostapi_unsupported":
            return "degraded", (f"Sadece WDM-KS üzerinden görülen bir çıkış bulundu ('{name}') "
                                 f"— bu sürücü modu bloklayıcı akışı desteklemiyor, gerçek "
                                 f"kullanımda açılamayabilir")
        return "ok", f"Hoparlör ('{name}') geçerli ve açılabiliyor"
    except Exception as e:
        return "down", f"Hoparlör kontrolü başarısız: {type(e).__name__}: {e}"


def _check_memory() -> tuple[str, str]:
    try:
        from jarvis.memory.memory_manager import load_memory
        mem = load_memory()
        total_facts = sum(len(v) for v in mem.values() if isinstance(v, dict))
        if total_facts == 0:
            return "degraded", "Hafıza dosyası okunabiliyor AMA hiç kayıtlı bilgi yok"
        return "ok", f"Hafıza dosyası okunabiliyor, {total_facts} kayıtlı bilgi var"
    except Exception as e:
        return "down", f"Hafıza dosyası okunamıyor: {type(e).__name__}: {e}"


def _check_python_files() -> tuple[str, str]:
    import ast
    broken = []
    checked = 0
    for py_file in list(BASE_DIR.glob("*.py")) + list((BASE_DIR / "actions").glob("*.py")) + list((BASE_DIR / "core").glob("*.py")):
        checked += 1
        try:
            ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as e:
            broken.append(f"{py_file.name} (satır {e.lineno})")
    if broken:
        return "down", f"{len(broken)}/{checked} dosyada sözdizimi hatası: {', '.join(broken[:3])}"
    return "ok", f"{checked} Python dosyasının tamamı sözdizimi olarak geçerli"


def _check_internet() -> tuple[str, str]:
    try:
        import requests
        import time
        t0 = time.time()
        requests.head("https://www.google.com", timeout=5)
        elapsed = time.time() - t0
        if elapsed > 2.0:
            return "degraded", f"İnternet bağlantısı var AMA yavaş ({elapsed:.1f}sn)"
        return "ok", "İnternet bağlantısı çalışıyor"
    except Exception:
        return "down", "İnternet bağlantısı yok veya çok yavaş"


def _check_api_key() -> tuple[str, str]:
    """NOT: bu build'de actions/key_rotation.py yok - o yuzden dogrudan
    config/api_keys.json'u okuyoruz. Bu sadece anahtarin DOSYADA VAR
    olup olmadigina bakar - GERCEKTEN gecerli/suresi dolmamis mi diye
    canli bir cagriyla test eder (self_improve gibi araclarin 'devre
    kesici acik' hatasiyla sessizce basarisiz oldugu durumlarin asil
    sebebini - genelde suresi dolmus bir anahtar - burada yakalariz)."""
    try:
        import json

        from jarvis.core.secure_config import api_keys_path
        api_path = api_keys_path()
        with open(api_path, encoding="utf-8") as f:
            cfg = json.load(f)
        key = (cfg.get("gemini_api_key") or "").strip()
        if not key:
            return "down", "config/api_keys.json içinde 'gemini_api_key' boş veya yok"
        if len(key) < 10 or key.upper() in {"YOUR_API_KEY_HERE", "CHANGEME", "TODO"}:
            return "degraded", "API anahtarı alanı dolu AMA geçersiz/placeholder görünüyor"
    except FileNotFoundError:
        from jarvis.core.secure_config import api_keys_path
        return "down", f"api_keys.json bulunamadı ({api_keys_path()})"
    except Exception as e:
        return "down", f"API anahtarı okunamıyor: {type(e).__name__}: {e}"

    # Dosyada bir anahtar VAR - ama bu onun hala GECERLI oldugu anlamina
    # gelmez (ozellikle sureli/ephemeral bir token ise). Kucuk, ucuz bir
    # canli cagriyla gercekten calisiyor mu diye test ediyoruz.
    try:
        from google import genai
        client = genai.Client(api_key=key)
        client.models.generate_content(model="gemini-flash-latest", contents="ping")
        return "ok", "Gemini API anahtarı yapılandırılmış ve GERÇEKTEN çalışıyor (canlı test edildi)"
    except Exception as e:
        msg = str(e)
        if any(code in msg for code in ("401", "403", "PERMISSION_DENIED", "UNAUTHENTICATED", "API_KEY_INVALID")):
            return "down", f"API anahtarı geçersiz veya süresi dolmuş (canlı test başarısız): {msg[:200]}"
        if any(code in msg for code in ("429", "RESOURCE_EXHAUSTED", "quota")):
            return "degraded", f"API anahtarı geçerli AMA kota/rate-limit aşılmış: {msg[:200]}"
        return "degraded", f"Anahtar dosyada var AMA canlı test edilemedi (ağ sorunu olabilir): {type(e).__name__}: {msg[:200]}"


def _check_file_delete() -> tuple[str, str]:
    """'Jarvis dosya silemiyor' sikayeti icin GERCEK, canli bir kanit uretir:
    sahte test dosyalari olusturup file_controller uzerinden GERCEKTEN
    silmeyi dener - kod okuyup 'boyle calismasi lazim' demek yerine, tam
    olarak kullanicinin yasadigi iki senaryoyu (uzantisiz isim, birlesik
    'desktop/isim' yolu) uctan uca test eder ve dosyanin GERCEKTEN diskten
    kalkip kalkmadigina bakar (sadece donen mesaja degil)."""
    import time
    from jarvis.actions.file_controller import file_controller, _get_desktop

    try:
        desktop = _get_desktop()
        if not desktop.is_dir():
            return "down", f"Masaüstü klasörü bulunamadı: {desktop}"
        stem = f"jarvis_saglik_testi_{int(time.time())}"
    except Exception as e:
        return "down", f"Test hazırlanamadı: {type(e).__name__}: {e}"

    def _try_scenario(path_param: str, name_param: str, filename: str) -> tuple[bool, str]:
        f = desktop / filename
        try:
            f.write_text("jarvis health check", encoding="utf-8")
        except Exception as e:
            return False, f"test dosyası oluşturulamadı: {type(e).__name__}: {e}"
        try:
            resp = file_controller(parameters={"action": "delete", "path": path_param, "name": name_param})
        except Exception as e:
            resp = f"{type(e).__name__}: {e}"
        ok = not f.exists()
        if not ok:
            try:
                f.unlink()
            except Exception:
                pass
        return ok, str(resp)

    # Senaryo 1: kullanıcı uzantı söylemeden isim verir (path/name ayrı)
    ok1, resp1 = _try_scenario("desktop", stem + "_a", stem + "_a.txt")
    # Senaryo 2: model path+name'i tek string'de birleştirir ("desktop/isim")
    ok2, resp2 = _try_scenario(f"desktop/{stem}_b", "", stem + "_b.txt")

    if ok1 and ok2:
        return "ok", "Uzantısız isim VE birleşik yol senaryoları gerçek dosyayla test edildi, ikisi de gerçekten siliyor"

    details = []
    if not ok1:
        details.append(f"uzantısız isim eşleşmesi başarısız (yanıt: {resp1})")
    if not ok2:
        details.append(f"birleşik yol ayrıştırması başarısız (yanıt: {resp2})")
    return "down", "; ".join(details)


def health_check(parameters: dict = None, response=None, player=None) -> str:
    """Tum kontrolleri calistirip, HER BIRI icin uc net durumdan birini
    (ok/degraded/down) iceren TEK bir ozet Turkce rapor dondurur. Asla
    'muhtemelen calisir' gibi belirsiz bir sey soylemez - her satir,
    gercekten test edilmis somut bir sonuc."""
    checks = [
        ("Ollama", _check_ollama),
        ("Mikrofon", _check_microphone),
        ("Hoparlör", _check_speaker),
        ("Hafıza", _check_memory),
        ("Kod dosyaları", _check_python_files),
        ("İnternet", _check_internet),
        ("API anahtarı", _check_api_key),
        ("Dosya silme", _check_file_delete),
    ]

    results = []
    counts = {"ok": 0, "degraded": 0, "down": 0}
    for label, fn in checks:
        try:
            status, detail = fn()
        except Exception as e:
            status, detail = "down", f"Kontrol sırasında beklenmeyen hata: {e}"
        counts[status] = counts.get(status, 0) + 1
        icon = _ICONS.get(status, "•")
        results.append(f"{icon} {label}: {detail}")
        if player:
            player.write_log(f"[HealthCheck] {icon} {label}: {detail}")

    if counts["down"] == 0 and counts["degraded"] == 0:
        header = "Tüm sistemler tam çalışır durumda."
    elif counts["down"] == 0:
        header = f"Her şey bağlı, ama {counts['degraded']} yerde sınırlama var:"
    else:
        header = f"{counts['down']} sistem tamamen kullanılamıyor, {counts['degraded']} yerde sınırlama var:"

    return header + "\n" + "\n".join(results)
