"""Canlı test — KARMAŞIK görevler (birden çok parçayı birleştirmeyi gerektirir).

Tek adımlı görevler JARVIS'in parçaları bilip bilmediğini ölçer; bunlar
parçaları BİRLEŞTİREBİLİP BİRLEŞTİREMEDİĞİNİ ölçer. Her görevin çıktısı
JARVIS'e güvenilmeden bağımsız doğrulanır. Çalıştırma:

    .venv/bin/python scripts/canli_test.py --karmasik

Kural: bu dosyadaki çıktı dosya adları ve ayrıntılar usta şablonlarında
GEÇEMEZ (tests/test_recipes.py koruma testi bu dosyayı da tarar).
"""
from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import zipfile
from pathlib import Path


def _bul(proje: Path, ad: str) -> Path:
    for p in sorted(proje.rglob(ad)):
        if ".jarvis" not in p.parts:
            return p
    raise AssertionError(f"{ad} oluşturulmamış")


def _oku(proje: Path, ad: str) -> str:
    metin = _bul(proje, ad).read_text(encoding="utf-8", errors="replace")
    assert metin.strip(), f"{ad} boş"
    return metin


def _sayilar(metin: str) -> list[float]:
    return [float(s.replace(",", ".")) for s in re.findall(r"-?\d+(?:[.,]\d+)?", metin)]


# ── 201: web → filtre → SQLite → HTML ─────────────────────────────────────
def _d_kitap_raporu(p: Path) -> None:
    db = _bul(p, "ucuz_kitaplar.db")
    con = sqlite3.connect(db)
    try:
        tablolar = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        assert tablolar, "veritabanında tablo yok"
        tablo_adi = tablolar[0]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tablo_adi):
            raise AssertionError(f"Geçersiz tablo adı: {tablo_adi!r}")
        rows = con.execute(f'SELECT * FROM "{tablo_adi}"').fetchall()
    finally:
        con.close()
    assert len(rows) >= 3, f"ilk 3 sayfada 20£ altı birçok kitap var, veritabanında {len(rows)} kayıt"
    fiyatlar, adlar = [], []
    for r in rows:
        nums = [v for v in r if isinstance(v, (int, float))] or _sayilar(" ".join(map(str, r)))
        assert nums, f"kayıtta fiyat yok: {r}"
        fiyatlar.append(float(nums[-1]))
        adlar.extend(str(v) for v in r if isinstance(v, str))
    assert all(f < 20 for f in fiyatlar), f"20£ ve üstü kayıt var: {max(fiyatlar)}"
    assert not any(a.rstrip().endswith(("...", "…")) for a in adlar), "kitap adları kısaltılmış ('...')"
    html = _oku(p, "en_ucuz_5.html")
    assert re.search(r"<table[\s>]", html, re.I), "HTML tablo yok"
    satir = re.findall(r"<tr[\s>]", html, re.I)
    assert 5 <= len(satir) <= 6, f"en ucuz 5 kitap (+ başlık) bekleniyordu, {len(satir)} satır var"
    en_ucuz = sorted(fiyatlar)[:5]
    html_fiyat = [x for x in _sayilar(re.sub(r"<[^>]+>", " ", html)) if 0 < x < 20]
    for f in en_ucuz:
        assert any(abs(f - h) < 0.01 for h in html_fiyat), f"en ucuz fiyatlardan {f} HTML'de yok"


# ── 202: klasör → log ayrıştırma → saatlik gruplama → CSV + grafik ────────
_LOG1 = """2025-10-10 09:05:11 INFO servis başladı
2025-10-10 09:17:40 ERROR veritabanı zaman aşımı
2025-10-10 09:44:02 ERROR disk dolu
2025-10-10 10:01:13 WARNING yavaş yanıt
2025-10-10 10:30:55 ERROR bağlantı reddedildi
"""
_LOG2 = """2025-10-10 09:59:59 ERROR kimlik doğrulama
2025-10-10 11:12:00 INFO yedek alındı
2025-10-10 11:48:31 ERROR bellek yetersiz
2025-10-10 11:49:02 ERROR bellek yetersiz
"""


def _h_log_saatlik(k: Path) -> None:
    d = k / "loglar"
    (d / "eski").mkdir(parents=True, exist_ok=True)
    (d / "app.log").write_text(_LOG1, encoding="utf-8")
    (d / "eski" / "app.1.log").write_text(_LOG2, encoding="utf-8")
    (d / "notlar.txt").write_text("2025-10-10 09:00:00 ERROR bu bir log değil\n", encoding="utf-8")


def _d_log_saatlik(p: Path) -> None:
    satirlar = list(csv.reader(io.StringIO(_oku(p, "hata_saatleri.csv"))))
    veri = {}
    for s in satirlar:
        nums = _sayilar(" ".join(s))
        if len(nums) >= 2:
            veri[int(nums[0]) if nums[0] < 24 else int(str(int(nums[0]))[-2:])] = int(nums[-1])
    assert veri == {9: 3, 10: 1, 11: 2}, f"beklenen {{9:3, 10:1, 11:2}} (yalnız .log, alt klasörler dahil), gelen {veri}"
    png = _bul(p, "hata_grafigi.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 1000, "geçerli bir PNG grafiği yok"


# ── 203: iki CSV → temizlik (tekrar/eksik) → müşteri raporu → zip ─────────
def _h_temizlik(k: Path) -> None:
    (k / "musteriler.csv").write_text("id,ad\n1,Ayşe\n2,Mehmet\n3,Zeynep\n", encoding="utf-8")
    (k / "siparisler.csv").write_text(
        "siparis_no,musteri_id,tutar\n"
        "S1,1,100\nS2,2,50\nS1,1,100\nS3,1,25.5\nS4,3,\nS5,2,40\nS6,9,70\nS5,2,40\n", encoding="utf-8")


def _d_temizlik(p: Path) -> None:
    temiz = list(csv.DictReader(io.StringIO(_oku(p, "temiz_siparisler.csv"))))
    nolar = sorted(r.get("siparis_no", "") for r in temiz)
    assert nolar == ["S1", "S2", "S3", "S5"], (
        f"tekrarlar (S1, S5), tutarı eksik (S4) ve müşterisi olmayan (S6) atılmalı; kalan: {nolar}")
    rapor = json.loads(_oku(p, "musteri_raporu.json"))
    duz = json.dumps(rapor, ensure_ascii=False)
    for ad, adet, toplam in (("Ayşe", 2, 125.5), ("Mehmet", 2, 90.0)):
        assert ad in rapor, f"{ad} raporda yok: {duz[:200]}"
        nums = _sayilar(json.dumps(rapor[ad]))
        assert adet in nums and any(abs(n - toplam) < 0.01 for n in nums), f"{ad}: {adet} sipariş / {toplam} bekleniyordu"
    assert "Zeynep" not in rapor or not any(n > 0 for n in _sayilar(json.dumps(rapor["Zeynep"]))), \
        "Zeynep'in geçerli siparişi yok"
    with zipfile.ZipFile(_bul(p, "rapor_yedek.zip")) as zf:
        adlar = {Path(n).name for n in zf.namelist()}
    assert {"temiz_siparisler.csv", "musteri_raporu.json"} <= adlar, f"zip içeriği eksik: {adlar}"


# ── 204: kod klasörü → TODO/FIXME tarama → Markdown rapor ─────────────────
def _h_todo(k: Path) -> None:
    d = k / "proje"
    (d / "paket").mkdir(parents=True, exist_ok=True)
    (d / "ana.py").write_text("import os\n# TODO: ayarları dosyadan oku\nx = 1\n# FIXME: sıfıra bölme\n",
                              encoding="utf-8")
    (d / "paket" / "yardimci.py").write_text("def f():\n    return 1  # TODO: önbellek ekle\n", encoding="utf-8")
    (d / "paket" / "notlar.txt").write_text("TODO: bu python dosyası değil\n", encoding="utf-8")
    (d / "bos.py").write_text("print('temiz')\n", encoding="utf-8")


def _d_todo(p: Path) -> None:
    md = _oku(p, "yapilacaklar.md")
    for dosya, satir in (("ana.py", "2"), ("ana.py", "4"), ("yardimci.py", "2")):
        assert dosya in md and re.search(rf"\b{satir}\b", md), f"{dosya}:{satir} raporda yok"
    for metin in ("ayarları dosyadan oku", "sıfıra bölme", "önbellek ekle"):
        assert metin in md, f"'{metin}' raporda yok"
    assert "bu python dosyası değil" not in md, ".txt dosyası taranmamalı"
    assert "bos.py" not in md, "yapılacak işi olmayan dosya listelenmemeli"
    assert re.search(r"\b3\b", md) and re.search(r"(?i)toplam|total|özet", md), "toplam (3) özeti yok"


KARMASIK = [
    {"no": 201, "ad": "kitap_raporu", "dil": "python", "hazirla": None, "dogrula": _d_kitap_raporu,
     "tarif": "https://books.toscrape.com/ sitesinin ilk 3 sayfasındaki kitapları kazı (JavaScript gerekmez; "
              "sayfa adresleri catalogue/page-2.html, page-3.html). Fiyatı 20£'dan düşük olanları adı ve fiyatıyla "
              "(tam ad; bağlantı metni kısaltılmış olabilir) çalışma klasöründeki ucuz_kitaplar.db SQLite "
              "veritabanına kaydet. Sonra en ucuz 5 kitabı fiyata göre sıralı bir HTML tablo olarak "
              "en_ucuz_5.html dosyasına yaz."},
    {"no": 202, "ad": "hata_saatleri", "dil": "python", "hazirla": _h_log_saatlik, "dogrula": _d_log_saatlik,
     "tarif": "{K}/loglar klasöründeki (alt klasörler dahil) yalnızca .log uzantılı dosyaları oku. Satır biçimi "
              "'YYYY-AA-GG SS:DD:ss SEVİYE mesaj'. ERROR seviyesindeki satırları SAATE göre grupla ve 'saat,adet' "
              "sütunlarıyla çalışma klasöründeki hata_saatleri.csv dosyasına yaz; ayrıca aynı veriyi çubuk grafik "
              "olarak hata_grafigi.png dosyasına çiz (matplotlib, ekran açmadan). Klasör ilk argüman "
              "(run_command: python main.py {K}/loglar)."},
    {"no": 203, "ad": "siparis_temizligi", "dil": "python", "hazirla": _h_temizlik, "dogrula": _d_temizlik,
     "tarif": "{K}/musteriler.csv (id, ad) ve {K}/siparisler.csv (siparis_no, musteri_id, tutar) dosyalarını "
              "işle: aynı siparis_no ile tekrar eden satırların yalnız birini tut, tutarı boş olan ve müşterisi "
              "bulunmayan siparişleri at; sonucu temiz_siparisler.csv olarak yaz. Her müşteri için sipariş sayısı "
              "ve toplam tutarı {\"Ayşe\": {\"siparis\": 2, \"toplam\": 125.5}, ...} biçiminde musteri_raporu.json "
              "dosyasına yaz. Son olarak bu iki dosyayı rapor_yedek.zip içine koy. Hepsi çalışma klasöründe. "
              "(run_command: python main.py {K}/musteriler.csv {K}/siparisler.csv)"},
    {"no": 204, "ad": "yapilacaklar_raporu", "dil": "python", "hazirla": _h_todo, "dogrula": _d_todo,
     "tarif": "{K}/proje klasöründeki (alt klasörler dahil) yalnızca .py dosyalarında 'TODO' ve 'FIXME' "
              "notlarını bul. Çalışma klasöründeki yapilacaklar.md dosyasına dosya dosya gruplayarak, her not "
              "için satır numarası ve not metniyle yaz; en sona toplam not sayısını içeren bir özet satırı ekle. "
              "Notu olmayan dosyaları yazma. Klasör ilk argüman (run_command: python main.py {K}/proje)."},
]
