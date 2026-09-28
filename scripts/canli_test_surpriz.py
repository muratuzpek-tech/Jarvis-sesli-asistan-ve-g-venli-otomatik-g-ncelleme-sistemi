"""Canlı test — SÜRPRİZ görev havuzu.

Sabit 5 görev ilerlemeyi ölçer; ama hep aynı görevleri düzeltmek "teste göre
ders çalışmak" riskini taşır. Bu havuzdan her turda rastgele birkaç görev
seçilir; JARVIS'in HİÇ GÖRMEDİĞİ işlerde de başarılı olup olmadığı ayrıca
raporlanır. Her görevin çıktısı, JARVIS'in sözüne güvenilmeden bağımsız
olarak doğrulanır.

Yeni görev eklemek: aşağıdaki SURPRIZ listesine {no, ad, dil, hazirla,
dogrula, tarif} sözlüğü ekle. `tarif` içindeki {K}, görevin girdi klasörüyle
değiştirilir. `dogrula(proje)` sorun görürse AssertionError fırlatmalı.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from pathlib import Path


# ── yardımcılar ───────────────────────────────────────────────────────────
def _bul(proje: Path, ad: str) -> Path | None:
    for p in sorted(proje.rglob(ad)):
        if ".jarvis" not in p.parts:
            return p
    return None


def _oku(proje: Path, ad: str) -> str:
    p = _bul(proje, ad)
    assert p is not None, f"{ad} oluşturulmamış"
    metin = p.read_text(encoding="utf-8", errors="replace")
    assert metin.strip(), f"{ad} boş"
    return metin


def _yakin_sayi_var(metin: str, hedef: float, tolerans: float = 0.05) -> bool:
    return any(abs(float(s.replace(",", ".")) - hedef) <= tolerans
               for s in re.findall(r"-?\d+(?:[.,]\d+)?", metin))


# ── 101: dosyaları tarihe göre ayır ───────────────────────────────────────
def _h_tarih(k: Path) -> None:
    d = k / "fotograflar"
    d.mkdir(parents=True, exist_ok=True)
    for ad, (yil, ay) in {"a.jpg": (2024, 1), "b.jpg": (2024, 1), "c.jpg": (2024, 3), "d.png": (2023, 12)}.items():
        (d / ad).write_bytes(b"x")
        t = time.mktime((yil, ay, 15, 12, 0, 0, 0, 0, -1))
        os.utime(d / ad, (t, t))


def _d_tarih(p: Path) -> None:
    kok = _bul(p, "sirali")
    assert kok and kok.is_dir(), "'sirali' klasörü oluşturulmamış"
    for yol in ("2024-01/a.jpg", "2024-01/b.jpg", "2024-03/c.jpg", "2023-12/d.png"):
        assert (kok / yol).is_file(), f"sirali/{yol} yok"


# ── 102: JSON → HTML tablo ────────────────────────────────────────────────
_URUNLER = [{"ad": "Kalem", "fiyat": 12.5}, {"ad": "Defter", "fiyat": 30}, {"ad": "Silgi", "fiyat": 4.75}]


def _h_html(k: Path) -> None:
    (k / "urunler.json").write_text(json.dumps(_URUNLER, ensure_ascii=False), encoding="utf-8")


def _d_html(p: Path) -> None:
    html = _oku(p, "tablo.html")
    assert "<table" in html.lower(), "HTML tablo (<table>) yok"
    for u in _URUNLER:
        assert u["ad"] in html, f"{u['ad']} tabloda yok"
        assert _yakin_sayi_var(html, float(u["fiyat"]), 0.001), f"{u['ad']} fiyatı ({u['fiyat']}) tabloda yok"


# ── 103: web sunucu log analizi ───────────────────────────────────────────
_LOG = "\n".join([
    '1.1.1.1 - - [10/Oct/2025:13:55:36 +0000] "GET / HTTP/1.1" 200 512',
    '1.1.1.2 - - [10/Oct/2025:13:55:37 +0000] "GET /a HTTP/1.1" 404 128',
    '1.1.1.3 - - [10/Oct/2025:13:55:38 +0000] "GET /b HTTP/1.1" 200 256',
    '1.1.1.1 - - [10/Oct/2025:13:55:39 +0000] "POST /c HTTP/1.1" 500 64',
    '1.1.1.4 - - [10/Oct/2025:13:55:40 +0000] "GET /d HTTP/1.1" 200 1024',
    '1.1.1.2 - - [10/Oct/2025:13:55:41 +0000] "GET /e HTTP/1.1" 404 128',
]) + "\n"


def _h_log(k: Path) -> None:
    (k / "access.log").write_text(_LOG, encoding="utf-8")


def _d_log(p: Path) -> None:
    veri = json.loads(_oku(p, "durum.json"))
    duz = {str(a): int(b) for a, b in veri.items()} if isinstance(veri, dict) else {}
    assert duz == {"200": 3, "404": 2, "500": 1}, f"beklenen {{200:3, 404:2, 500:1}}, gelen {veri}"


# ── 104: iki CSV birleştirme ──────────────────────────────────────────────
def _h_birlestir(k: Path) -> None:
    (k / "musteriler.csv").write_text("id,ad\n1,Ayşe\n2,Mehmet\n3,Zeynep\n", encoding="utf-8")
    (k / "siparisler.csv").write_text("musteri_id,tutar\n1,100\n2,50\n1,25.5\n3,10\n2,40\n", encoding="utf-8")


def _d_birlestir(p: Path) -> None:
    satirlar = list(csv.reader(io.StringIO(_oku(p, "toplamlar.csv"))))
    metin = json.dumps(satirlar, ensure_ascii=False)
    for ad, toplam in (("Ayşe", 125.5), ("Mehmet", 90), ("Zeynep", 10)):
        satir = next((s for s in satirlar if ad in s), None)
        assert satir, f"{ad} yok: {metin[:200]}"
        assert _yakin_sayi_var(",".join(satir), toplam, 0.01), f"{ad} toplamı {toplam} olmalı: {satir}"


# ── 105: aynı içerikli dosyaları bul ──────────────────────────────────────
def _h_kopya(k: Path) -> None:
    d = k / "belgeler"
    (d / "alt").mkdir(parents=True, exist_ok=True)
    (d / "rapor.txt").write_text("aylık rapor içeriği", encoding="utf-8")
    (d / "alt" / "rapor_kopya.txt").write_text("aylık rapor içeriği", encoding="utf-8")
    (d / "notlar.txt").write_text("başka bir şey", encoding="utf-8")
    (d / "resim1.bin").write_bytes(b"\x00\x01\x02")
    (d / "resim2.bin").write_bytes(b"\x00\x01\x02")


def _d_kopya(p: Path) -> None:
    metin = _oku(p, "kopyalar.txt")
    for a in ("rapor.txt", "rapor_kopya.txt", "resim1.bin", "resim2.bin"):
        assert a in metin, f"{a} kopya listesinde yok"
    assert "notlar.txt" not in metin, "notlar.txt benzersiz ama kopya olarak listelenmiş"


# ── 106: sıcaklık özeti ───────────────────────────────────────────────────
def _h_sicaklik(k: Path) -> None:
    (k / "sicaklik.csv").write_text(
        "tarih,derece\n2025-07-01,31.5\n2025-07-02,28\n2025-07-03,35.25\n2025-07-04,26.75\n", encoding="utf-8")


def _d_sicaklik(p: Path) -> None:
    metin = _oku(p, "ozet.txt")
    for etiket, deger in (("en düşük", 26.75), ("en yüksek", 35.25), ("ortalama", 30.375)):
        assert _yakin_sayi_var(metin, deger, 0.03), f"{etiket} ({deger}) özette yok: {metin[:200]!r}"


# ── 107: kitap sitesi kazıma (gerçek site) ────────────────────────────────
def _d_kitap(p: Path) -> None:
    satirlar = list(csv.reader(io.StringIO(_oku(p, "kitaplar.csv"))))
    assert len(satirlar) >= 20, f"ilk sayfada 20 kitap var, {len(satirlar)} satır geldi"
    duz = json.dumps(satirlar, ensure_ascii=False)
    assert "A Light in the Attic" in duz, "ilk kitap (A Light in the Attic) yok"
    assert "51.77" in duz, "ilk kitabın fiyatı (51.77) yok"


# ── 108: Markdown içindekiler ─────────────────────────────────────────────
_BASLIKLAR = ["Giriş", "Kurulum", "Linux", "Windows", "Kullanım", "Sık Sorulanlar"]


def _h_md(k: Path) -> None:
    (k / "belge.md").write_text(
        "# Giriş\nMetin.\n## Kurulum\n### Linux\nx\n### Windows\ny\n## Kullanım\nz\n# Sık Sorulanlar\n"
        "```\n# bu bir başlık değil (kod bloğu)\n```\n", encoding="utf-8")


def _d_md(p: Path) -> None:
    metin = _oku(p, "icindekiler.md")
    for b in _BASLIKLAR:
        assert b in metin, f"'{b}' başlığı içindekilerde yok"
    assert "bu bir başlık değil" not in metin, "kod bloğundaki satır başlık sanılmış"
    assert metin.index("Kurulum") < metin.index("Linux") < metin.index("Kullanım"), "başlık sırası bozuk"


# ── 109: metin istatistiği ────────────────────────────────────────────────
def _h_ist(k: Path) -> None:
    (k / "hikaye.txt").write_text("Bir varmış bir yokmuş.\nEvvel zaman içinde\n\nkalbur saman içinde.\n",
                                  encoding="utf-8")


def _d_ist(p: Path) -> None:
    veri = json.loads(_oku(p, "istatistik.json"))
    duz = {str(a).lower(): b for a, b in veri.items()} if isinstance(veri, dict) else {}
    satir = next((v for a, v in duz.items() if "satir" in a or "satır" in a or "line" in a), None)
    kelime = next((v for a, v in duz.items() if "kelime" in a or "word" in a), None)
    assert satir in (3, 4), f"satır sayısı 4 (boş satır sayılmazsa 3) olmalı, gelen {satir} — {veri}"
    assert kelime == 10, f"kelime sayısı 10 olmalı, gelen {kelime} — {veri}"


# ── 110: bağlantı kontrolü (gerçek site) ──────────────────────────────────
def _d_link(p: Path) -> None:
    metin = _oku(p, "linkler.txt")
    satirlar = [s for s in metin.splitlines() if "http" in s]
    assert len(satirlar) >= 10, f"sayfada 10'dan fazla bağlantı var, {len(satirlar)} satır geldi"
    assert sum("200" in s for s in satirlar) >= 5, "çalışan bağlantılar için durum kodu (200) yazılmamış"


# ── 111: Go ile uzantı sayacı ─────────────────────────────────────────────
def _h_uzanti(k: Path) -> None:
    d = k / "karisik"
    (d / "alt").mkdir(parents=True, exist_ok=True)
    for ad in ("a.txt", "b.txt", "alt/c.txt", "d.go", "alt/e.go", "f.md"):
        (d / ad).write_text("x", encoding="utf-8")


def _d_uzanti(p: Path) -> None:
    metin = _oku(p, "uzantilar.txt")
    for uz, n in ((".txt", 3), (".go", 2), (".md", 1)):
        assert re.search(rf"{re.escape(uz)}\D{{0,10}}{n}\b", metin), f"{uz}: {n} satırı yok: {metin[:200]!r}"


SURPRIZ = [
    {"no": 101, "ad": "tarihe_gore_ayir", "dil": "python", "hazirla": _h_tarih, "dogrula": _d_tarih,
     "tarif": "{K}/fotograflar klasöründeki dosyaları değiştirilme tarihlerine göre, çalışma klasöründe "
              "'sirali/YYYY-AA/' alt klasörlerine KOPYALAYAN bir program yaz (ör. sirali/2024-01/a.jpg). "
              "Kaynak klasör ilk komut satırı argümanı olsun (run_command: python main.py {K}/fotograflar)."},
    {"no": 102, "ad": "json_html_tablo", "dil": "python", "hazirla": _h_html, "dogrula": _d_html,
     "tarif": "{K}/urunler.json dosyasındaki ürün listesini (alanlar: ad, fiyat) okuyup her ürünü bir satır "
              "olarak gösteren bir HTML tablo üreten program yaz. Sonuç çalışma klasöründeki tablo.html olsun. "
              "JSON yolu ilk argüman (run_command: python main.py {K}/urunler.json)."},
    {"no": 103, "ad": "log_analizi", "dil": "python", "hazirla": _h_log, "dogrula": _d_log,
     "tarif": "{K}/access.log (Apache/Nginx 'combined' biçimi) dosyasındaki isteklerin HTTP durum kodlarına "
              "göre kaç tane olduğunu sayan program yaz. Sonucu {\"200\": adet, ...} biçiminde çalışma "
              "klasöründeki durum.json dosyasına yaz. Log yolu ilk argüman (run_command: python main.py "
              "{K}/access.log)."},
    {"no": 104, "ad": "csv_birlestir", "dil": "python", "hazirla": _h_birlestir, "dogrula": _d_birlestir,
     "tarif": "{K}/musteriler.csv (id, ad) ve {K}/siparisler.csv (musteri_id, tutar) dosyalarını birleştirip "
              "her müşterinin toplam sipariş tutarını 'ad,toplam' sütunlarıyla çalışma klasöründeki "
              "toplamlar.csv dosyasına yazan program yaz. İki dosya yolu komut satırı argümanı olsun "
              "(run_command: python main.py {K}/musteriler.csv {K}/siparisler.csv)."},
    {"no": 105, "ad": "kopya_bulucu", "dil": "python", "hazirla": _h_kopya, "dogrula": _d_kopya,
     "tarif": "{K}/belgeler klasöründe (alt klasörler dahil) İÇERİĞİ AYNI olan dosyaları bulan program yaz. "
              "Her kopya grubunu bir satıra, dosya adlarıyla çalışma klasöründeki kopyalar.txt dosyasına yaz; "
              "benzersiz dosyaları yazma. Klasör ilk argüman (run_command: python main.py {K}/belgeler)."},
    {"no": 106, "ad": "sicaklik_ozeti", "dil": "python", "hazirla": _h_sicaklik, "dogrula": _d_sicaklik,
     "tarif": "{K}/sicaklik.csv (tarih, derece) dosyasından en düşük, en yüksek ve ortalama sıcaklığı "
              "hesaplayıp çalışma klasöründeki ozet.txt dosyasına yazan program yaz. CSV yolu ilk argüman "
              "(run_command: python main.py {K}/sicaklik.csv)."},
    {"no": 107, "ad": "kitap_kazima", "dil": "python", "hazirla": None, "dogrula": _d_kitap,
     "tarif": "https://books.toscrape.com/ sayfasındaki (yalnızca ilk sayfa, JavaScript gerekmez) kitapların "
              "adını ve fiyatını 'ad,fiyat' sütunlarıyla çalışma klasöründeki kitaplar.csv dosyasına yazan "
              "program yaz."},
    {"no": 108, "ad": "markdown_icindekiler", "dil": "python", "hazirla": _h_md, "dogrula": _d_md,
     "tarif": "{K}/belge.md Markdown dosyasındaki başlıklardan (#, ##, ###) girintili bir içindekiler listesi "
              "üreten program yaz; kod bloklarının (```) içindeki satırlar başlık değildir. Sonuç çalışma "
              "klasöründeki icindekiler.md olsun. Dosya yolu ilk argüman (run_command: python main.py "
              "{K}/belge.md)."},
    {"no": 109, "ad": "metin_istatistigi", "dil": "python", "hazirla": _h_ist, "dogrula": _d_ist,
     "tarif": "{K}/hikaye.txt dosyasının satır ve kelime sayısını {\"satir\": N, \"kelime\": N} biçiminde "
              "çalışma klasöründeki istatistik.json dosyasına yazan program yaz. Dosya yolu ilk argüman "
              "(run_command: python main.py {K}/hikaye.txt)."},
    {"no": 110, "ad": "link_kontrol", "dil": "python", "hazirla": None, "dogrula": _d_link,
     "tarif": "https://quotes.toscrape.com/ sayfasındaki bütün bağlantıları (a href) bulup her birinin HTTP "
              "durum kodunu kontrol eden program yaz (JavaScript gerekmez; göreli adresleri tam adrese çevir, "
              "her istekte 10 sn zaman aşımı). Her bağlantıyı 'adres durum_kodu' biçiminde bir satır olarak "
              "çalışma klasöründeki linkler.txt dosyasına yaz."},
    {"no": 111, "ad": "go_uzanti_sayaci", "dil": "go", "hazirla": _h_uzanti, "dogrula": _d_uzanti,
     "tarif": "Bir klasördeki (alt klasörler dahil) dosyaları uzantılarına göre sayan bir Go programı yaz; "
              "yalnızca standart kütüphane. Sonucu her satırda '.uzanti: adet' olarak çalışma klasöründeki "
              "uzantilar.txt dosyasına yaz. Klasör ilk argüman: {K}/karisik"},
]
