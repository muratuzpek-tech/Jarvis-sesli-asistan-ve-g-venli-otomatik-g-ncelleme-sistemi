#!/usr/bin/env python3
"""JARVIS canlı kod yazma testi — "gerçekten ilerliyor muyuz?" sorusunun ölçüsü.

Her değişiklikten sonra AYNI beş görevi gerçek modelle (Ollama/Gemini) baştan
yazdırır, çalıştırır ve sonucu JARVIS'in kendi "başarılı" demesine GÜVENMEDEN,
çıktı dosyalarını bağımsız olarak kontrol eder. Sonuç geçmişe eklenir ve bir
önceki çalıştırmayla karşılaştırılır.

Kullanım:
    .venv/bin/python scripts/canli_test.py            # beş görevin hepsi
    .venv/bin/python scripts/canli_test.py --sec 1,3  # yalnızca 1. ve 3. görev
    .venv/bin/python scripts/canli_test.py --liste    # görevleri göster

Not: Sesli arayüzü değil, kod yazma motorunu (dev_agent) ölçer. Her görev
birkaç dakika sürebilir; toplam 15-45 dk beklenebilir.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import jarvis.actions.dev_agent as da  # noqa: E402
from jarvis.paths import logs_dir  # noqa: E402

GECMIS = logs_dir() / "canli_test_gecmisi.jsonl"


# ── bağımsız doğrulama yardımcıları ──────────────────────────────────────
def _bul(proje: Path, ad: str) -> Path | None:
    """Program çıktıyı proje içinde nereye yazdıysa bul (.jarvis hariç)."""
    for p in sorted(proje.rglob(ad)):
        if ".jarvis" not in p.parts and ".arsiv" not in p.parts:
            return p
    return None


def _oku(proje: Path, ad: str) -> str:
    p = _bul(proje, ad)
    if p is None:
        raise AssertionError(f"{ad} oluşturulmamış")
    metin = p.read_text(encoding="utf-8", errors="replace")
    if not metin.strip():
        raise AssertionError(f"{ad} boş")
    return metin


def _kayitlar(metin: str) -> list:
    veri = json.loads(metin)
    if isinstance(veri, dict):  # {"quotes": [...]} gibi sarmalanmış olabilir
        listeler = [v for v in veri.values() if isinstance(v, list)]
        veri = listeler[0] if listeler else []
    return veri


# ── görevler ─────────────────────────────────────────────────────────────
def _hazirla_metinler(klasor: Path) -> None:
    (klasor / "metinler").mkdir(parents=True, exist_ok=True)
    (klasor / "metinler" / "a.txt").write_text("elma armut elma\nkiraz", encoding="utf-8")
    (klasor / "metinler" / "b.txt").write_text("elma armut", encoding="utf-8")


def _dogrula_metinler(proje: Path) -> None:
    metin = _oku(proje, "report.txt").lower()
    satir = next((s for s in metin.splitlines() if "elma" in s), "")
    assert "3" in satir, f"'elma' 3 kez geçiyor ama rapor satırı: {satir!r}"
    assert "kiraz" in metin, "'kiraz' raporda yok"


def _hazirla_csv(klasor: Path) -> None:
    (klasor / "satislar.csv").write_text(
        "urun,adet,birim_fiyat\nkalem,2,10\ndefter,1,25\nkalem,3,10\nsilgi,4,2.5\n", encoding="utf-8")


def _dogrula_csv(proje: Path) -> None:
    metin = _oku(proje, "ozet.json")
    veri = json.loads(metin)
    duz = json.dumps(veri, ensure_ascii=False)
    for urun, toplam in (("kalem", 50), ("defter", 25), ("silgi", 10)):
        assert urun in duz, f"{urun} özette yok"
        assert str(toplam) in duz, f"{urun} toplamı {toplam} olmalı; özet: {duz[:300]}"


def _dogrula_js_kazima(proje: Path) -> None:
    kayit = _kayitlar(_oku(proje, "quotes.json"))
    assert len(kayit) >= 20, f"en az 20 alıntı bekleniyordu, {len(kayit)} geldi (kaydırma çalışmadı mı?)"
    assert all(isinstance(k, dict) and len(k) >= 2 for k in kayit[:20]), "kayıtlar id/metin/yazar alanlı nesne değil"
    assert "einstein" in json.dumps(kayit, ensure_ascii=False).lower(), "ilk alıntı (Albert Einstein) yok"


def _dogrula_statik(proje: Path) -> None:
    satirlar = list(csv.reader(io.StringIO(_oku(proje, "quotes.csv"))))
    assert len(satirlar) >= 10, f"ilk sayfada 10 alıntı var, {len(satirlar)} satır geldi"
    assert "einstein" in str(satirlar).lower(), "Albert Einstein alıntısı yok"


def _dogrula_go(proje: Path) -> None:
    satirlar = [s for s in _oku(proje, "report.log").splitlines() if s.strip()]
    assert len(satirlar) >= 3, f"3 ölçüm bekleniyordu, {len(satirlar)} satır var"


GOREVLER = [
    {"no": 1, "ad": "kelime_sayici", "dil": "python", "hazirla": _hazirla_metinler, "dogrula": _dogrula_metinler,
     "tarif": "{K}/metinler klasöründeki bütün .txt dosyalarındaki kelimeleri sayan bir program yaz. Klasör yolu "
              "ilk komut satırı argümanı olsun (run_command: python main.py {K}/metinler). En sık geçen 10 kelimeyi "
              "'kelime: sayı' biçiminde, çalışma klasöründeki report.txt dosyasına yazsın."},
    {"no": 2, "ad": "satis_ozeti", "dil": "python", "hazirla": _hazirla_csv, "dogrula": _dogrula_csv,
     "tarif": "{K}/satislar.csv dosyasını (sütunlar: urun, adet, birim_fiyat) okuyup her ürün için toplam tutarı "
              "(adet × birim_fiyat) hesaplayan bir program yaz. CSV yolu ilk komut satırı argümanı olsun "
              "(run_command: python main.py {K}/satislar.csv). Sonucu {\"urun\": toplam} biçiminde çalışma "
              "klasöründeki ozet.json dosyasına yazsın."},
    {"no": 3, "ad": "js_kazima", "dil": "python", "hazirla": None, "dogrula": _dogrula_js_kazima,
     "tarif": "https://quotes.toscrape.com/scroll adresine git; bu sayfa içeriği JavaScript ile ve aşağı "
              "kaydırdıkça (infinite scroll) yüklüyor. Sayfayı kaydırarak ilk 20 alıntıyı topla ve her biri için "
              "id, metin ve yazar alanlarıyla çalışma klasöründeki quotes.json dosyasına JSON listesi olarak yaz. "
              "Bekleme süresi 10 saniye."},
    {"no": 4, "ad": "statik_kazima", "dil": "python", "hazirla": None, "dogrula": _dogrula_statik,
     "tarif": "https://quotes.toscrape.com/ sayfasındaki (yalnızca ilk sayfa, JavaScript gerekmez) alıntıları "
              "metin ve yazar sütunlarıyla çalışma klasöründeki quotes.csv dosyasına yazan bir program yaz."},
    {"no": 5, "ad": "go_ram_raporu", "dil": "go", "hazirla": None, "dogrula": _dogrula_go,
     "tarif": "Sistemin RAM kullanımını 1 saniye arayla 3 kez ölçüp her ölçümü zaman damgasıyla ayrı bir satır "
              "olarak çalışma klasöründeki report.log dosyasına yazan bir Go programı yaz. Yalnızca standart "
              "kütüphane kullan (Linux'ta /proc/meminfo, diğer sistemlerde uygun bir yöntem)."},
]


def _git_surum() -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip() or "?"
    except Exception:  # noqa: BLE001
        return "?"


def calistir(gorev: dict, kok: Path) -> dict:
    klasor = kok / f"_girdi_{gorev['ad']}"
    klasor.mkdir(parents=True, exist_ok=True)
    if gorev["hazirla"]:
        gorev["hazirla"](klasor)
    tarif = gorev["tarif"].replace("{K}", klasor.as_posix())
    if gorev["dil"] == "go" and not shutil.which("go"):
        return {"no": gorev["no"], "ad": gorev["ad"], "durum": "ATLANDI", "neden": "go kurulu değil", "sure_sn": 0}

    print(f"\n{'=' * 70}\n[{gorev['no']}] {gorev['ad']} başlıyor…\n{'=' * 70}")
    t0 = time.monotonic()
    try:
        sonuc = da._build_project(description=tarif, language=gorev["dil"], project_name=gorev["ad"], timeout=120)
    except Exception as e:  # noqa: BLE001
        sonuc = f"İSTİSNA: {type(e).__name__}: {e}"
    sure = round(time.monotonic() - t0)
    jarvis_dedi = "is working" in sonuc or "çalışıyor" in sonuc
    try:
        gorev["dogrula"](kok / gorev["ad"])
        dogru, neden = True, ""
    except (AssertionError, json.JSONDecodeError, OSError, StopIteration) as e:
        dogru, neden = False, str(e)[:300]

    if dogru and jarvis_dedi:
        durum = "GEÇTİ"
    elif jarvis_dedi and not dogru:
        durum = "YALANCI BAŞARI"   # en kötü durum: "çalışıyor" dedi ama çıktı yanlış
    elif dogru:
        durum = "DOĞRU AMA REDDETTİ"
    else:
        durum = "KALDI"
    return {"no": gorev["no"], "ad": gorev["ad"], "durum": durum, "neden": neden or sonuc[-300:],
            "sure_sn": sure, "kabul_testi": "Acceptance test" in sonuc}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sec", help="virgülle görev numaraları, ör. 1,3")
    ap.add_argument("--liste", action="store_true")
    a = ap.parse_args()
    if a.liste:
        for g in GOREVLER:
            print(f"{g['no']}. {g['ad']} ({g['dil']}): {g['tarif'][:110]}…")
        return 0
    secili = {int(x) for x in a.sec.split(",")} if a.sec else {g["no"] for g in GOREVLER}

    damga = datetime.now().strftime("%Y%m%d-%H%M%S")
    kok = Path.home() / "Desktop" / "JarvisProjects" / "_canli_test" / damga
    kok.mkdir(parents=True, exist_ok=True)
    da.PROJECTS_DIR = kok  # gerçek projelere dokunma

    sonuclar = [calistir(g, kok) for g in GOREVLER if g["no"] in secili]

    gecen = sum(s["durum"] == "GEÇTİ" for s in sonuclar)
    sayilan = sum(s["durum"] != "ATLANDI" for s in sonuclar)
    kayit = {"zaman": damga, "surum": _git_surum(), "gecen": gecen, "toplam": sayilan, "sonuclar": sonuclar}
    onceki = None
    if GECMIS.exists():
        satirlar = [s for s in GECMIS.read_text(encoding="utf-8").splitlines() if s.strip()]
        onceki = json.loads(satirlar[-1]) if satirlar else None
    GECMIS.parent.mkdir(parents=True, exist_ok=True)
    with GECMIS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(kayit, ensure_ascii=False) + "\n")
    (kok / "SONUC.json").write_text(json.dumps(kayit, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'=' * 70}\nCANLI TEST SONUCU — sürüm {kayit['surum']}\n{'=' * 70}")
    for s in sonuclar:
        ek = f" — {s['neden'][:140]}" if s["durum"] != "GEÇTİ" else ""
        print(f"  {s['no']}. {s['ad']:<15} {s['durum']:<20} {s['sure_sn']:>4} sn{ek}")
    print(f"\n  Başarı: {gecen}/{sayilan}")
    if onceki:
        print(f"  Önceki çalıştırma ({onceki['zaman']}, sürüm {onceki['surum']}): {onceki['gecen']}/{onceki['toplam']}")
    if any(s["durum"] == "YALANCI BAŞARI" for s in sonuclar):
        print("  ⚠️  YALANCI BAŞARI var: JARVIS 'çalışıyor' dedi ama çıktı yanlış — öncelikli hata!")
    print(f"\n  Projeler ve SONUC.json: {kok}\n  Geçmiş: {GECMIS}")
    return 0 if gecen == sayilan else 1


if __name__ == "__main__":
    raise SystemExit(main())
