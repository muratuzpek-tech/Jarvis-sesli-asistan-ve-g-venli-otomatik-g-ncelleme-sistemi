"""
Otonom kendini geliştirme döngüsü: ÖNER → UYGULA → DOĞRULA → TUT/GERİ AL.

Kullanıcının açıkça istediği tasarım: "kendi yapsın ama doğrulama sistemi
gerekli, bu çalışıyor mu diye". Yani her çalıştırma şu adımları izler:

  1. Değişiklikten ÖNCE jarvis_backup_tool.py ile TAM proje yedeği alınır.
  2. Hedef dosya için modelden somut, tam dosya içeriği istenir.
  3. Yeni içerik dosyaya yazılır.
  4. DOĞRULAMA çalıştırılır: sözdizimi (ast.parse) + ayrı bir Python
     sürecinde gerçek import + (main.py/ui.py ise sadece sözdizimi, çünkü
     bunları import etmek ses/GUI donanımını açar).
  5. Doğrulama geçerse değişiklik TUTULUR ve memory/self_improve_log.jsonl'a
     yazılır. Geçmezse hata modele geri beslenir ve MAX_ATTEMPTS'e kadar
     tekrar denenir; hepsi başarısız olursa dosya ORİJİNAL haline döndürülür
     ve durum açıkça raporlanır.

BİLİNEREK YAPILMAYAN ŞEY: Downloads/internetten indirilen rastgele dosyaların
içeriğini otomatik "iyileştirme kaynağı" olarak almak. Buradaki doğrulama
(sözdizimi + import) sadece "açıkça bozuk mu" sorusuna cevap verir - sözdizimi
geçerli ama mantığı yanlış ya da kötü niyetli bir kodu YAKALAYAMAZ. Bu yüzden
bu modül SADECE projenin kendi kod dosyalarını (main.py, ui.py, jarvis_cli.py,
actions/, core/, dashboard/) hedef alabilir. Dışarıdan bir proje/dosya analiz
ettirmek (avenoxbeyin örneğindeki gibi) ayrı, elle tetiklenen bir inceleme -
bu otomatik uygula/doğrula döngüsüne asla girmez.
"""
from __future__ import annotations

import ast
import json
import re
import secrets
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from jarvis.paths import memory_dir

MAX_ATTEMPTS = 3
MODEL_NAME   = "gemini-flash-latest"


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = _get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
LOG_PATH        = memory_dir() / "self_improve_log.jsonl"

# Otomatik döngünün dokunabileceği TEK yer projenin kendi kodu - dışarıdan
# gelen hiçbir dosya buraya giremez.
ALLOWED_DIRS        = ("actions", "core", "dashboard")
ALLOWED_ROOT_FILES  = ("ui.py", "jarvis_cli.py")   # main.py korumali (PROTECTED_FILES)
NO_IMPORT_CHECK     = {"main", "ui"}  # bunlari import etmek ses/GUI acar - sadece sozdizimi kontrol edilir

# Onay bekleyen (henuz uygulanmamis) self_improve istekleri - file_controller.py'deki
# confirm_code deseniyle ayni mantik: ilk cagri hicbir seyi degistirmez, sadece bir kod
# doner; kullanici acikca onaylayip ayni kodla tekrar cagirilana kadar dosyaya dokunulmaz.
_pending_self_improve: dict[str, dict] = {}


# Guvenlik sinirini olusturan dosyalar: LLM'in yeniden yazdigi bir surum onay
# kapilarini sessizce kaldirabilir (bkz. agent_loop.py'deki "bu duzeltme bir
# kere daha kayboldu" notu). Otomatik iyilestirme bunlara ASLA dokunmaz;
# degisiklik yalnizca elle, gozden gecirilerek yapilir.
PROTECTED_FILES = frozenset({
    "main.py",
    "tool_gate.py",
    "actions/terminal_tool.py",
    "actions/tools_kopru.py",
    "actions/agent_loop.py",
    "actions/entegrasyon.py",
    "actions/file_controller.py",
    "actions/code_helper.py",
    "actions/computer_settings.py",
    "actions/self_improve.py",
    "core/approval_service.py",
    "core/brain_orchestrator.py",
})


def _is_protected(rel: Path) -> bool:
    return rel.as_posix() in PROTECTED_FILES


def _is_allowed_target(path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(BASE_DIR)
    except Exception:
        return False
    if _is_protected(rel):
        return False
    if len(rel.parts) == 1:
        return rel.parts[0] in ALLOWED_ROOT_FILES
    return rel.parts[0] in ALLOWED_DIRS and rel.suffix == ".py"


def _pick_target() -> Path | None:
    """Belirli bir dosya verilmezse: actions/ altında en uzun süredir bu
    döngüde incelenmemiş .py dosyasını seçer (round-robin gibi çalışır,
    her seferinde aynı dosyaya takılıp kalmaz)."""
    candidates = sorted(p for p in (BASE_DIR / "actions").glob("*.py")
                        if _is_allowed_target(p))
    if not candidates:
        return None

    reviewed_at: dict[str, str] = {}
    if LOG_PATH.exists():
        for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                reviewed_at[e["file"]] = e.get("timestamp", "")
            except Exception:
                continue

    def _key(p: Path) -> str:
        return reviewed_at.get(str(p.relative_to(BASE_DIR)), "")  # hic incelenmemis -> "" -> en basa gelir

    candidates.sort(key=_key)
    return candidates[0]


def _get_api_key() -> str:
    from jarvis.core.secure_config import get_gemini_api_key
    return get_gemini_api_key()


_BREAKER = None


def _get_breaker():
    global _BREAKER
    if _BREAKER is None:
        from jarvis.actions.resilience import CircuitBreaker
        _BREAKER = CircuitBreaker(name="gemini-selfimprove", failure_threshold=3, cooldown_seconds=60.0)
    return _BREAKER


def _get_model():
    from google import genai
    from jarvis.actions.resilience import call_with_resilience
    from jarvis.actions.local_llm import generate_with_fallback
    client  = genai.Client(api_key=_get_api_key())
    breaker = _get_breaker()

    class _Resp:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Model:
        def generate_content(self, prompt: str):
            # Gemini kota/devre kesici yuzunden kullanilamazsa (kullanicinin
            # acikca istedigi gibi) yerel Ollama'ya duser - bkz. local_llm.py.
            # Ollama da kullanilamazsa ORIJINAL Gemini hatasi degismeden
            # yukari firlatilir, hicbir sey sessizce yutulmaz.
            text = generate_with_fallback(
                lambda: call_with_resilience(
                    lambda: client.models.generate_content(model=MODEL_NAME, contents=prompt),
                    breaker=breaker,
                ),
                prompt_for_ollama=prompt,
                source="self_improve",
            )
            return _Resp(text)

    return _Model()


def _clean_code(text: str) -> str:
    text = text.strip()
    match = re.search(r"```[a-zA-Z]*\r?\n?(.*?)\r?\n?```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _get_public_names(source: str) -> set[str]:
    """Modül seviyesindeki (iç içe OLMAYAN), alt çizgiyle BAŞLAMAYAN
    fonksiyon/sınıf isimlerini döner - 'kamuya açık API' kabaca budur."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
    }


def _static_analysis_errors(target: Path) -> str | None:
    """ruff'un Pyflakes ('F') kurallarindan SADECE gercekten calisma-zamani
    hatasina isaret edenleri (tanimsiz isim, kullanilmadan once referans,
    fonksiyon disinda return/break/continue vb.) kontrol eder - stil/import-
    sirasi gibi kozmetik kurallari (E/W) KASITLI OLARAK atlar, cunku onlar
    'calisir ama guzel degil' demektir ve bu kontrolun gereksiz yere iyi bir
    degisikligi reddetmesine yol acar.

    Asagidaki GERCEK YASANAN OLAY'daki gibi bir hata (fonksiyon govdesinde
    kullanilan ama artik var olmayan bir isim) main.py/ui.py icin ozellikle
    onemli, cunku onlar GUI/ses donanimi actiklari icin import EDILEMEZ ve
    bugune kadar sadece ast.parse (colpak sozdizimi) ile kontrol ediliyorlardi
    - yani govde icindeki bir NameError hicbir zaman yakalanamiyordu. ruff
    kurulu degilse (opsiyonel dev bagimliligi) sessizce atlanir - bu kontrol
    EK bir guvenlik agidir, olmazsa olmaz degildir ve ana dogrulamayi
    engellememelidir."""
    import shutil
    ruff_path = shutil.which("ruff")
    if ruff_path is None:
        return None
    try:
        proc = subprocess.run(
            [ruff_path, "check",
             "--select=F821,F822,F823,F701,F702,F706", "--quiet", str(target)],
            capture_output=True, text=True, timeout=15, cwd=str(BASE_DIR),
        )
    except Exception:
        return None
    if proc.returncode != 0 and proc.stdout.strip():
        return proc.stdout.strip()[:600]
    return None


def _verify(target: Path, original: str = "") -> tuple[bool, str]:
    """'Açıkça bozuk mu' testi - bir DOĞRULUK garantisi DEĞİLDİR:
    1) sözdizimi (ast.parse), 2) kamuya açık fonksiyon/sınıfların SİLİNMEDİĞİ
    (bkz. aşağıdaki GERÇEK YAŞANAN OLAY), 3) statik analiz (ruff, F-kuralları
    - main.py/ui.py için özellikle önemli, bkz. _static_analysis_errors),
    4) main.py/ui.py DIŞINDAKI dosyalar için ayrı bir Python sürecinde
    gerçek import.

    GERÇEK YAŞANAN OLAY: yerel Ollama (kota bittiğinde devreye giren
    fallback - bkz. local_llm.py) computer_settings.py'yi 25KB'tan 1.5KB'a
    indirip ASIL 'computer_settings()' fonksiyonunu TAMAMEN SİLDİ - ama
    geriye kalan tek yardımcı fonksiyon (toggle_wifi) modül seviyesinde
    hiçbir şeye başvurmadığı için `import actions.computer_settings` YİNE
    DE BAŞARILI oldu (eksik isimler sadece fonksiyon GÖVDESİ içinde
    kullanılıyordu, import anında değil). Eski kontrol (sadece sözdizimi +
    import) bunu YAKALAYAMADI ve canlı sistemde tüm ses/parlaklık/wifi/
    kapatma komutlarını kıracaktı - kullanıcı fark edip bildirdi, elle geri
    alındı. Bu yüzden artık ORİJİNAL dosyadaki her kamuya açık isim YENİ
    dosyada da var mı diye AYRICA kontrol ediyoruz."""
    new_text = target.read_text(encoding="utf-8")
    try:
        ast.parse(new_text)
    except SyntaxError as e:
        return False, f"Söz dizimi hatası (satır {e.lineno}): {e.msg}"
    except Exception as e:
        return False, f"Dosya okunamadı: {type(e).__name__}: {e}"

    if original:
        missing = _get_public_names(original) - _get_public_names(new_text)
        if missing:
            return False, (
                f"Kamuya açık fonksiyon/sınıf(lar) kayboldu: {', '.join(sorted(missing))} — "
                f"içe aktarma başarılı olsa bile bu 'iyileştirme' işlevselliği siliyor, reddedildi."
            )

    static_issues = _static_analysis_errors(target)
    if static_issues:
        return False, f"Statik analiz (ruff) olası çalışma-zamanı hatası buldu:\n{static_issues}"

    rel = target.relative_to(BASE_DIR)
    mod_name = ".".join(rel.with_suffix("").parts)
    if mod_name in NO_IMPORT_CHECK:
        return True, "ok (sözdizimi + statik analiz kontrol edildi - GUI/ses donanımı açtığı için import edilmedi)"

    proc = subprocess.run(
        [sys.executable, "-c", f"import {mod_name}"],
        capture_output=True, text=True, timeout=30, cwd=str(BASE_DIR),
    )
    if proc.returncode != 0:
        return False, f"Import hatası ({mod_name}): {proc.stderr.strip()[-600:]}"
    return True, "ok"


def _log(entry: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {"timestamp": datetime.now().isoformat(), **entry}
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[SelfImprove] ⚠️ Log yazılamadı: {e}")


# Yerel bir modeli (örn. Ollama/ms-swift ile fine-tune edilecek) bu görevde
# eğitmek için veri seti - SADECE doğrulamadan (sözdizimi+ruff+API+import)
# GERÇEKTEN geçmiş, tutulan degisiklikler buraya yazilir. "query" alani
# modele TAM OLARAK canli calisirken verilen prompt ile birebir ayni -
# egitim/gercek kullanim arasinda format farki OLMAMASI icin bilerek boyle.
TRAINING_DATA_PATH = memory_dir() / "training_examples.jsonl"


def _log_training_example(prompt: str, completion: str, rel_str: str, goal: str) -> None:
    try:
        TRAINING_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now().isoformat(),
            "file": rel_str,
            "goal": goal,
            "query": prompt,
            "response": completion,
        }
        with open(TRAINING_DATA_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[SelfImprove] ⚠️ Eğitim verisi yazılamadı: {e}")


def _build_prompt(filename: str, goal: str, original: str, error_note: str = "") -> str:
    """Model prompt'unu tek bir yerden üretir - self_improve()'un canlı akışı
    VE eğitim verisi tohumlama scripti (seed_training_data.py) AYNI şablonu
    kullanır, böylece egitim verisi ile gerçek çalışma zamanı isteği arasında
    format farkı olmaz (bir fine-tune için bu tutarlılık kritik)."""
    return f"""You are an expert Python engineer improving ONE file inside a working,
production voice-assistant codebase (a Windows desktop app using PyQt6 and the Gemini Live API).

Goal: {goal}

Rules:
- Return ONLY the complete updated file content — no explanation, no markdown fences, no commentary.
- Preserve all existing public function/class names, signatures, and behavior unless the goal
  explicitly asks to change them.
- Do not remove functionality.
- Do NOT add any top-level example/demo/usage code (e.g. a bare `result = some_function(...)`
  sitting outside any function or `if __name__ == "__main__":` guard) — importing this module
  must have ZERO side effects. If the ORIGINAL file already had an `if __name__ == "__main__":`
  block, you may keep it, but never call a function before its own definition in the file.
- The result MUST be syntactically valid Python and MUST remain importable (no new missing
  dependencies).{error_note}

Current content of {filename}:
{original}

Updated content:"""


def self_improve(parameters: dict = None, player=None) -> str:
    """Tek bir hedef dosya için tam döngü: öner → uygula → doğrula →
    tut/geri al. parameters: {"file_path": str (opsiyonel), "goal": str (opsiyonel)}."""
    p = parameters or {}
    file_arg = (p.get("file_path") or "").strip()
    goal = (p.get("goal") or "").strip() or (
        "Kod kalitesini, hata yönetimini veya okunabilirliği iyileştir. "
        "Mevcut davranışı DEĞİŞTİRME, sadece iyileştir."
    )

    if file_arg:
        target = Path(file_arg)
        target = target if target.is_absolute() else (BASE_DIR / target)
        target = target.resolve()
    else:
        target = _pick_target()
        if target is None:
            return "İyileştirilecek bir dosya bulamadım (actions/ klasörü boş görünüyor)."

    if not _is_allowed_target(target):
        return (f"'{file_arg or target}' kendi kendini geliştirme kapsamının dışında. Sadece "
                f"projenin kendi kod dosyalarını (ui.py, jarvis_cli.py, actions/, core/, "
                f"dashboard/) otomatik değiştirebilirim; güvenlik/onay dosyalarına "
                f"(ör. main.py, tool_gate.py, terminal_tool.py) ve dışarıdan gelen "
                f"dosyalara dokunmam.")

    if not target.exists():
        return f"Dosya bulunamadı: {target}"

    rel_str = str(target.relative_to(BASE_DIR))

    # ONAY KAPISI: bu cagri gercekten dosyayi degistirmeden ONCE kullanicinin
    # acik onayini ister - file_controller.delete_all_files/move_file'daki
    # confirm_code deseniyle ayni. confirm_code verilmemisse hicbir yedek
    # alinmaz, modele hicbir istek gitmez, dosyaya dokunulmaz.
    confirm_code = (p.get("confirm_code") or "").strip()
    if not confirm_code:
        code = secrets.token_hex(3)
        _pending_self_improve[code] = {"target": target, "goal": goal}
        return (
            f"ONAY GEREKLİ: '{rel_str}' dosyası şu amaçla otomatik olarak değiştirilecek: "
            f"\"{goal}\". Bu adım dosyanın kaynak kodunu modelin ürettiği yeni içerikle "
            f"değiştirir (doğrulama başarısız olursa otomatik geri alınır, ama başarılı "
            f"olursa kalıcıdır). Kullanıcıya bunu tarif et; kullanıcı SESLİ/YAZILI olarak "
            f"açıkça onaylarsa (bir sonraki mesajında), self_improve'u aynı file_path/goal "
            f"ile ve confirm_code='{code}' parametresiyle TEKRAR çağır. Kullanıcı onaylamadan "
            f"bu kodu kendi kendine kullanma."
        )
    pending = _pending_self_improve.pop(confirm_code, None)
    if pending is None or pending["target"] != target:
        return "Onay kodu geçersiz veya süresi dolmuş. Önce confirm_code vermeden çağırıp yeni kod alın."
    goal = pending["goal"]

    original = target.read_text(encoding="utf-8")

    from jarvis.backup_tool import JarvisBackupTool
    backup_tool = JarvisBackupTool.for_jarvis()   # kok dizin tek yerden
    if player:
        player.write_log(f"[SelfImprove] '{rel_str}' için değişiklik öncesi tam yedek alınıyor...")
    print(f"[SelfImprove] Yedek alınıyor (hedef: {rel_str})...")
    backup_path = backup_tool.create_backup()

    model = _get_model()
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        error_note = f"\n\nÖnceki deneme bu hatayla doğrulamadan geçemedi, bunu düzelt:\n{last_error}" if last_error else ""
        prompt = _build_prompt(target.name, goal, original, error_note)

        try:
            response = model.generate_content(prompt)
            new_content = _clean_code(response.text)
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            print(f"[SelfImprove] ⚠️ Model çağrısı başarısız (deneme {attempt}): {last_error}")
            continue

        if not new_content.strip() or new_content.strip() == original.strip():
            last_error = "Model boş veya değişmemiş içerik döndürdü."
            continue

        target.write_text(new_content, encoding="utf-8")
        ok, detail = _verify(target, original=original)

        if ok:
            _log({"file": rel_str, "goal": goal, "attempt": attempt,
                  "status": "applied", "detail": detail, "backup": str(backup_path)})
            _log_training_example(prompt, new_content, rel_str, goal)
            msg = (f"'{rel_str}' güncellendi ve doğrulandı ({detail}). Tam proje yedeği: "
                   f"{backup_path.name}. Bir sorun görürsen jarvis_backup_tool.py ile geri "
                   f"alabilirsin.")
            if player:
                player.write_log(f"[SelfImprove] ✅ {msg}")
            print(f"[SelfImprove] ✅ {msg}")
            return msg

        print(f"[SelfImprove] ⚠️ Doğrulama başarısız (deneme {attempt}/{MAX_ATTEMPTS}): {detail}")
        last_error = detail
        target.write_text(original, encoding="utf-8")  # bir sonraki denemeden önce orijinale dön

    # Tum denemeler basarisiz - dosya zaten orijinaline geri donduruldu (yukarida),
    # ama garanti olsun diye tekrar yaziyoruz.
    target.write_text(original, encoding="utf-8")
    _log({"file": rel_str, "goal": goal, "status": "rolled_back",
          "reason": last_error, "backup": str(backup_path)})
    msg = (f"'{rel_str}' için {MAX_ATTEMPTS} deneme de doğrulamadan geçemedi, dosya ORİJİNAL "
           f"haline geri döndürüldü. Son hata: {last_error}")
    if player:
        player.write_log(f"[SelfImprove] ❌ {msg}")
    print(f"[SelfImprove] ❌ {msg}")
    return msg
