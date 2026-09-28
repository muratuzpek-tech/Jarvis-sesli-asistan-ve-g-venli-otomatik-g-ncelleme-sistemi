"""
SYSTEM_READ niyet yönlendiricisi — Aşama 2 / Adım 1 (minimal, deterministik).

AMAÇ:
Kullanıcı doğal dilde "çalışan işlemleri listele" gibi bir şey söylediğinde,
Gemini'nin kendi araç seçimine (ör. system_status) bırakmadan, DOĞRUDAN ve
DETERMİNİSTİK olarak windows_system -> process_list'e yönlendirmek için tek
bir eşleştirme fonksiyonu sunar.

KAPSAM (bilinçli olarak dar tutuldu):
Bu modül SADECE metin eşleştirir — hiçbir şey ÇALIŞTIRMAZ, hiçbir subprocess
açmaz, hiçbir dosyaya dokunmaz. Gerçek çalıştırma main.py tarafından, mevcut
actions/tools_kopru.py::ALLOWED_TOOLS["windows_system"] üzerinden yapılır
(bu modül onu import bile etmez). v1 sadece "process_list" komutunu
üretebilir — başka hiçbir command_name döndürmez.

Genişletme notu: yeni bir SYSTEM_READ komutu (ör. service_list) eklemek
istenirse, buraya yeni bir desen listesi + dönüş değeri eklenir; main.py'ye
veya tools_kopru.py'ye dokunmaya gerek kalmaz.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# "çalışan işlemleri listele" ve çok yakın doğal dil varyasyonları.
# Türkçe karakterli ve ASCII-yakın (çalışan/calisan gibi) biçimler ayrı ayrı
# eklendi çünkü kullanıcı girişi klavye/transkripsiyon farkına göre
# değişebilir. Kelime sırası biraz esnek tutuldu (\s+ / basit birleşim)
# ama v1 kasıtlı olarak SADECE bu tek niyeti (process_list) kapsıyor.
_PROCESS_LIST_PATTERNS = [
    r"çalışan\s+işlemleri",
    r"calisan\s+islemleri",
    r"çalışan\s+programları",
    r"calisan\s+programlari",
    r"çalışan\s+processleri",
    r"calisan\s+processleri",
    r"bilgisayardaki\s+işlemleri",
    r"bilgisayardaki\s+islemleri",
    r"hangi\s+programlar\s+çalışıyor",
    r"hangi\s+programlar\s+calisiyor",
    r"işlemleri\s+(göster|listele)",
    r"islemleri\s+(goster|listele)",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _PROCESS_LIST_PATTERNS]


def match_system_read(text: str) -> str | None:
    """Metin SYSTEM_READ -> process_list ile eşleşiyorsa 'process_list'
    döndürür, aksi halde None döner. v1 başka HİÇBİR command_name üretmez —
    bu yüzden dönüş değeri her zaman ya 'process_list' ya da None'dur."""
    if not text:
        return None
    t = text.strip()
    for pattern in _COMPILED:
        if pattern.search(t):
            return "process_list"
    return None


# ─────────────────────────────────────────────────────────────────────────
# FILE_ANALYSIS niyet yönlendiricisi — Aşama 2 / Adım 2 (minimal, deterministik)
#
# AMAÇ: Kullanıcı "brains/planner_ai.py dosyasını analiz et" gibi bir SADECE
# OKUMA/ANALİZ isteği söylediğinde, Gemini'nin code_helper / self_improve /
# brain_team arasında serbestçe (rastgele) seçim yapmasına bırakmadan,
# DOĞRUDAN ve DETERMİNİSTİK olarak FILE_ANALYSIS -> brain_team -> coder_ai ->
# operation=analyze yoluna yönlendirmek için bir eşleştirme fonksiyonu sunar.
#
# Bu modül burada da HİÇBİR ŞEY ÇALIŞTIRMAZ/YAZMAZ/SİLMEZ - sadece metni
# eşleştirir ve (salt-okunur) dosya sistemini SADECE var olup olmadığını
# kontrol etmek için okur. Gerçek analiz görevi main.py tarafından, mevcut
# core/brain_orchestrator.py'nin DEĞİŞTİRİLMEMİŞ görev/adım motoru üzerinden
# oluşturulur.
#
# GÜVENLİK KOŞULLARI (üçü de sağlanmazsa None döner, hiçbir yönlendirme olmaz):
#   1. Metinde gerçek/proje içi bir dosya yolu bulunmalı VE bu yol projede
#      GERÇEKTEN var olmalı (asla dosya yolu uydurulmaz).
#   2. Niyet analiz/inceleme/okuma/açıklama olmalı.
#   3. "değiştir", "düzelt", "yaz", "sil", "ekle" gibi bir DEĞİŞİKLİK fiili
#      GEÇMEMELİ - geçerse bu kural devre dışı kalır (FILE_MODIFICATION için
#      ayrı, ileride yapılacak bir kuralın konusu - burada ELE ALINMIYOR).

_ANALYZE_KEYWORDS = ("analiz", "incele", "açıkla", "acikla", "oku")

# Bilinçli olarak DAR bir liste - kullanıcının verdiği örneklerle birebir.
# "yaz" ayrı ele alınıyor çünkü "yazılım"/"yazar" gibi kelimelerin içinde de
# geçiyor (bkz. core/brain_orchestrator.py'deki AYNI sorunun AYNI çözümü,
# _infer_executor_action() içindeki is_write kontrolü - burada da aynı
# desen tekrarlandı, YENİ bir çözüm icat edilmedi).
_MODIFY_KEYWORDS_SIMPLE = ("değiştir", "degistir", "düzelt", "duzelt", "sil", "ekle", "kaldır", "kaldir")

# Dosya adı/yolu gibi görünen bir "kelime.uzantı" (veya "klasör/dosya.uzantı")
# deseni. Kasıtlı olarak basit tutuldu - v1 sadece AÇIKÇA bir uzantı taşıyan
# tokenleri dosya yolu adayı sayıyor.
_FILE_TOKEN_RE = re.compile(r"([A-Za-zÇĞİÖŞÜçğıöşü0-9_\-./\\]+\.[A-Za-z0-9]{1,6})")

# Proje ağacında bir dosya ararken atlanacak, ilgisiz/hacimli/gürültülü
# klasörler - arama YANLIŞLIKLA cache/sanal-ortam/yedek içinden bir dosya
# bulup onu "gerçek" proje dosyasıymış gibi sunmasın diye.
_SKIP_DIR_NAMES = {
    "__pycache__", ".venv", "venv", ".git", "node_modules", "memory", "logs",
    "tasks", "self_improvement", ".jarvis_backup",
}


def _has_modify_verb(t: str) -> bool:
    if any(k in t for k in _MODIFY_KEYWORDS_SIMPLE):
        return True
    # "yaz" -> "yazılım"/"yazar" gibi yanlış-pozitifleri dışla (bkz. üstteki not).
    if ("yaz" in t) and ("yazılım" not in t) and ("yazilim" not in t) and ("yazar" not in t):
        return True
    return False


def _has_analyze_intent(t: str) -> bool:
    return any(k in t for k in _ANALYZE_KEYWORDS)


def _extract_file_token(text: str) -> str | None:
    for m in _FILE_TOKEN_RE.finditer(text):
        token = m.group(1).strip().strip(":;,.")
        if token:
            return token
    return None


def resolve_project_file(token: str, project_root: str | Path) -> str | None:
    """Verilen dosya adı/yolunu (token), proje kökü (project_root) içinde
    DOĞRULAR. SADECE dosya sistemini OKUR - hiçbir şey yazmaz/silmez/değiştirmez.

    - token bir yol ayracı içeriyorsa (ör. 'brains/planner_ai.py'), SADECE o
      TAM yolun proje içinde gerçekten bir dosya olarak var olup olmadığı
      kontrol edilir; varsa aynen (proje-köküne göre) döndürülür.
    - token çıplak bir dosya adıysa (ör. 'planner_ai.py'), proje ağacında
      (bilinen gereksiz klasörler atlanarak) o isimde dosya aranır. TEK ve
      NET bir eşleşme varsa proje-köküne-göre yolu döndürülür; hiç ya da
      birden fazla eşleşme varsa None döner - dosya yolu ASLA uydurulmaz
      (bkz. brains/planner_ai.py'deki AYNI ilke: "Hangi dosya olduğundan
      EMİN DEĞİLSEN ... asla dosya yolu uydurma")."""
    project_root = Path(project_root)
    normalized = token.replace("\\", "/")

    if "/" in normalized:
        candidate = project_root / normalized
        if candidate.is_file():
            return normalized
        return None

    matches = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".")]
        if normalized in filenames:
            rel = Path(dirpath).relative_to(project_root) / normalized
            matches.append(str(rel).replace("\\", "/"))

    if len(matches) == 1:
        return matches[0]
    return None


def match_file_analysis(text: str, project_root: str | Path) -> str | None:
    """Metin FILE_ANALYSIS koşullarının HEPSİNİ karşılıyorsa, projeye göre
    doğrulanmış GERÇEK dosya yolunu döndürür; aksi halde None. Hiçbir şey
    çalıştırmaz/yazmaz - sadece metin + salt-okunur dosya sistemi kontrolü."""
    if not text:
        return None
    t = text.strip()
    if _has_modify_verb(t):
        return None
    if not _has_analyze_intent(t):
        return None
    token = _extract_file_token(t)
    if not token:
        return None
    return resolve_project_file(token, project_root)

# FILE_MODIFICATION v1 ? yaln?zca a??k?a istenen dosya olu?turma/yazma.
_FILE_MOD_CREATE = (
    "olu?tur", "olustur", "yarat", "create"
)
_FILE_MOD_WRITE = (
    "i?ine", "icine", "yaz", "write"
)
_FILE_NAME_RE = re.compile(
    r"(?:[A-Za-z????????????0-9_\-]+(?:\.[A-Za-z0-9]{1,8}))"
)


def _extract_quoted_parts(text: str) -> tuple[str, str]:
    """Al?nt?l?/backtick ifadelerden dosya ad? ve i?erik ??kar?r.
    Bo?luksuz ilk par?a = ad, bo?luklu par?a = i?erik."""
    parts = []
    for match in re.finditer(r"[`'\"]([^`'\"]+)[`'\"]", text):
        value = match.group(1).strip()
        if value:
            parts.append(value)

    name = ""
    content = ""
    for value in parts:
        if " " in value and not content:
            content = value
        elif not name:
            name = value

    return name, content




def match_file_modification(text: str) -> dict | None:
    """Dosya olusturma/yazma komutlarini Turkce karakterlerden bagimsiz cozer."""

    if not text:
        return None

    t = text.strip()
    low = t.lower()

    # Silme/tasima gibi yikici islemleri burada ele alma.
    destructive = (
        "sil", "delete", "move",
        "rename", "tasi", "degistir", "duzelt",
        "kaldir", "duzenle", "düzenle",
    )
    if any(x in low for x in destructive):
        return None

    # Bozuk UTF-8/console durumlari icin:
    # olustur, olu?tur, olu?tur
    has_create = bool(re.search(
        r"olu.?tur|olustur|yarat|create|meydana\s+getir",
        low,
        re.IGNORECASE,
    ))

    # icine, i?ine, i?ine, yaz
    has_write = bool(re.search(
        r"i.?ine|icine|yaz|write",
        low,
        re.IGNORECASE,
    ))

    # Filename
    m = re.search(
        r"[A-Za-z0-9_\-\u00c7\u011e\u0130\u00d6\u015e\u00dc\u00e7\u011f\u0131\u00f6\u015f\u00fc]+\.[A-Za-z0-9]{1,8}",
        t,
        re.IGNORECASE,
    )
    if not m:
        return None

    name = m.group(0)

    # Location. Handles masa?st?, masaustu, masa?st? etc.
    if re.search(r"masa.?st", low) or "desktop" in low:
        path = "desktop"
    elif re.search(r"indirilen", low) or "download" in low:
        path = "downloads"
    elif re.search(r"belge", low) or "document" in low:
        path = "documents"
    else:
        path = "."

    content = ""

    # Quoted content
    quoted = re.findall(r"[`'\"]([^`'\"]+)[`'\"]", t)
    for q in quoted:
        q = q.strip()
        if q and q.lower() != name.lower():
            content = q
            break

    # Natural language:
    # i?ine MERHABA JARVIS yaz
    if not content:
        m2 = re.search(
            r"i.?ine\s+(.+?)\s+(?:yaz|write)\s*$",
            t,
            re.IGNORECASE,
        )
        if m2:
            content = m2.group(1).strip().strip("'\"")

    # GUVENLIK (2026-09-22, canli testte tespit edildi): bu fonksiyon SADECE
    # gercekten YENI bir dosya olusturma/yazma icin ONAYSIZ gorev
    # uretmelidir (bkz. yukaridaki "FILE_MODIFICATION v1" notu). Hedef dosya
    # DISKTE ZATEN VARSA burada sessizce/onaysiz ust yazilmasina IZIN VERME -
    # None don ki istek normal, onayli (code_helper/confirm_code korumali)
    # akisa dussun. Somut hata: "... duzenle, print('eski') yerine
    # print('yeni') yaz" cumlesi yanlis ayristirilip var olan dosyanin
    # tum icerigini "eski" gibi anlamsiz bir degerle ust yazmaya
    # calismisti - hicbir onay istemeden.
    try:
        from jarvis.actions.file_controller import _resolve_path as _fc_resolve_path
        from jarvis.actions.file_controller import _resolve_target_name as _fc_resolve_target_name
        _base_dir = _fc_resolve_path(path)
        _target, _err = _fc_resolve_target_name(_base_dir, name)
        target_exists = bool(_err) or (_target is not None and _target.exists())
    except Exception:
        # Cozumleme basarisiz olursa GUVENLI TARAFTA KAL: var kabul et,
        # oto-eslesmeyi iptal et.
        target_exists = True

    if target_exists:
        return None

    if has_create:
        result = {
            "action": "create_file",
            "path": path,
            "name": name,
        }
        if content:
            result["content"] = content
        return result

    if has_write and content:
        return {
            "action": "write",
            "path": path,
            "name": name,
            "content": content,
            "append": False,
        }

    return None

