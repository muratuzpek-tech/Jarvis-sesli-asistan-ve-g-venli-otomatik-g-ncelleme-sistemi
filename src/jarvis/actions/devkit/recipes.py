"""Usta şablonları: sık görülen görev türleri için DOĞRULANMIŞ kısa örnek kodlar.

NEDEN (canlı test, 2026-09-28/29): yerel model her seferinde yolu sıfırdan
tahmin ediyor ve bilinen tuzaklara tekrar tekrar düşüyordu: Selenium'a sapma,
çıktıyı Path(__file__).parent'a yazma, sonsuz kaydırma döngüsü, uydurma
'Sample quote' verisi. Modele "bu yoldan git" diye çalışan bir kalıp göstermek
bu tür modellerde tutarlılığı en çok artıran yöntemlerden biri.

Şablonlar yalnızca YOL GÖSTERİR; model görevin gerçek seçicilerini/alanlarını
kullanmalı. Görev metnine göre en fazla iki özel şablon + genel G/Ç kuralı
seçilir (istem kısa kalsın diye).
"""
from __future__ import annotations

import re

from jarvis.actions.devkit.task_intake import has_url, needs_browser, needs_browser_or_web

GENERAL_IO = '''
# GİRDİ/ÇIKTI KALIBI — girdi yolu komut satırından, çıktı ÇALIŞMA KLASÖRÜNE (göreli yol)
import sys
from pathlib import Path

def process(src: Path) -> str:
    text = src.read_text(encoding="utf-8")                    # gerçek işi burada yap
    return f"satir: {len(text.splitlines())}\\n"

def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Kullanım: python main.py <girdi_dosyası>")   # girdi yoksa açıkça çık
    result = process(Path(sys.argv[1]))
    Path("result.txt").write_text(result, encoding="utf-8")   # ASLA Path(__file__).parent / ...
    print(f"Wrote result.txt ({len(result)} chars)")

if __name__ == "__main__":
    main()
'''

PLAYWRIGHT_SCROLL = '''
# JAVASCRIPT / SONSUZ KAYDIRMA KALIBI — Playwright (Selenium DEĞİL), SERT DURDURMA ile
# (Örnek konu: bir ürün listesi. Seçicileri ve alanları GÖREVİN sayfasına göre değiştir.)
import json, sys
from playwright.sync_api import sync_playwright

ITEM, NAME, PRICE = "li.product", ".product-name", ".product-price"   # ← sayfaya göre değiştir

def collect(url: str, target: int, max_rounds: int = 40) -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=30_000)
        page.wait_for_selector(ITEM, timeout=10_000)
        for _ in range(max_rounds):                               # döngü HER ZAMAN sınırlı
            if page.locator(ITEM).count() >= target:              # yeterince öğe → dur
                break
            page.mouse.wheel(0, 20_000)                            # ya da: "daha fazla" düğmesine tıkla
            page.wait_for_timeout(800)
        rows = [{"name": el.locator(NAME).inner_text().strip(),
                 "price": el.locator(PRICE).inner_text().strip()}
                for el in page.locator(ITEM).all()[:target]]
        browser.close()
    return rows

if __name__ == "__main__":
    data = collect(sys.argv[1], target=int(sys.argv[2]) if len(sys.argv) > 2 else 50)
    with open("products.json", "w", encoding="utf-8") as fh:        # göreli yol, görevin istediği ad
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"Saved {len(data)} items")                               # GERÇEK veri; asla örnek/uydurma veri
'''

REQUESTS_BS4 = '''
# DÜZ SAYFA KAZIMA KALIBI — requests + BeautifulSoup (JavaScript gerekmiyorsa)
# (Örnek konu: kitap listesi, 3 sayfa. Seçicileri, sayfa adreslerini ve sütunları GÖREVE göre değiştir.)
import csv, re, sys
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

def fetch(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()                                       # hatayı yutma, görünür olsun
    # resp.content (bayt) ver: resp.text yanlış kodlamayla '£51.77' → 'Â£51.77' olur
    return BeautifulSoup(resp.content, "html.parser")


def parse_price(text: str) -> float:
    """'£51.77', 'Â£51.77', '1.299,90 TL' → sayı. ONDALIK NOKTASINI KORU:
    ''.join(filter(str.isdigit, ...)) '51.77'yi 5177 yapar — KULLANMA."""
    m = re.search(r"\d+(?:[.,]\d+)*", text)
    if not m:
        raise ValueError(f"fiyat yok: {text!r}")
    num = m.group(0)
    if "," in num and "." in num:                                  # 1.299,90 → 1299.90
        num = num.replace(".", "").replace(",", ".")
    elif "," in num:
        num = num.replace(",", ".")
    return float(num)

if __name__ == "__main__":
    # ÇOK SAYFA: sonraki sayfanın adresini ELLE KURMA (başlangıç adresi zaten bir alt klasör
    # içeriyorsa sabit bir göreli yol eklemek klasörü İKİ KEZ yazar → 404).
    # Sayfadaki "sonraki" bağlantısını izle ve O ANKİ sayfa adresine göre çöz.
    # Bir sayfa alınamazsa (404 vb.) SESSİZCE ATLAMA: fetch() hatası programı durdursun.
    url = sys.argv[1]
    rows = []
    for page in range(1, 4):                                      # görev kaç sayfa diyorsa
        soup = fetch(url)
        for card in soup.select("article.product_pod"):           # ← sayfaya göre değiştir
            link = card.select_one("h3 a")
            # Görünen metin kısaltılmış olabilir ("..."): tam değer title= özniteliğindedir
            title = link.get("title") or link.get_text(strip=True)
            price = parse_price(card.select_one(".price_color").get_text())
            rows.append((title, price))
        nxt = soup.select_one("li.next a")                        # ← sitenin "sonraki" bağlantısı
        if nxt is None:
            break                                                 # son sayfa
        url = urljoin(url, nxt["href"])                           # O ANKİ sayfaya göre çöz
    if not rows:
        sys.exit("No items found - selector may be wrong")        # sessizce boş dosya yazma
    with open("news.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["title", "price"])
        writer.writerows(rows)
'''

CSV_AGGREGATE = '''
# CSV GRUPLAMA KALIBI — DictReader + sayıya çevirme + gruplama
# (Örnek konu: şehir bazında ortalama sıcaklık. Sütun adlarını GÖREVİN dosyasına göre değiştir.)
import csv, json, sys
from collections import defaultdict

def group_average(path: str, key_col: str, value_col: str) -> dict[str, float]:
    sums: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):                            # sütun adlarıyla eriş
            sums[row[key_col]] += float(row[value_col])
            counts[row[key_col]] += 1
    return {k: round(sums[k] / counts[k], 2) for k in sums}

if __name__ == "__main__":
    result = group_average(sys.argv[1], "city", "temperature")
    with open("averages.json", "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
'''

DUPLICATE_FILES = '''
# KOPYA / BOŞ DOSYA KALIBI — içerik özetiyle (hash) karşılaştır, HİÇBİR ŞEYİ SİLME
import hashlib, sys
from collections import defaultdict
from pathlib import Path

def file_hash(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def main() -> None:
    root = Path(sys.argv[1])                                       # klasör komut satırından
    files = sorted(p for p in root.rglob("*") if p.is_file())
    empty = [p for p in files if p.stat().st_size == 0]
    groups: dict[str, list[Path]] = defaultdict(list)
    for p in files:
        if p.stat().st_size > 0:
            groups[file_hash(p)].append(p)
    dups = [g for g in groups.values() if len(g) > 1]
    lines = ["# Kopya dosyalar"]
    for g in dups:
        lines.append(" = ".join(str(p.relative_to(root)) for p in g))
    lines += ["", "# Boş dosyalar"] + [str(p.relative_to(root)) for p in empty]
    lines += ["", f"Toplam: {sum(len(g) - 1 for g in dups)} fazla kopya, {len(empty)} boş dosya"]
    Path("rapor.txt").write_text("\\n".join(lines) + "\\n", encoding="utf-8")   # yalnız RAPOR yaz
    print(f"Wrote rapor.txt ({len(dups)} kopya grubu, {len(empty)} boş)")

if __name__ == "__main__":
    main()
'''

FOLDER_WALK = '''
# KLASÖR TARAMA KALIBI — alt klasörler dahil, yalnızca dosyalar
# (Örnek konu: en büyük dosyaları bulmak. İşlemi GÖREVE göre değiştir.)
import sys
from pathlib import Path

def largest_files(root: Path, top: int = 10) -> list[tuple[str, int]]:
    files = [(str(p.relative_to(root)), p.stat().st_size) for p in root.rglob("*") if p.is_file()]
    return sorted(files, key=lambda x: x[1], reverse=True)[:top]

if __name__ == "__main__":
    rows = largest_files(Path(sys.argv[1]))                        # girdi klasörü komut satırından
    lines = [f"{name}: {size} bytes" for name, size in rows]       # gerçek değerler, şablon metin değil
    Path("sizes.txt").write_text("\\n".join(lines) + "\\n", encoding="utf-8")
'''

SQLITE_STORE = '''
# SQLITE KALIBI — tablo oluştur, parametreli ekle, TEKRAR ÇALIŞINCA ÇİFT KAYIT YOK
# Program birden çok kez çalışabilir: aynı kayıt ikinci kez EKLENMEMELİ (UNIQUE + INSERT OR IGNORE).
import sqlite3

def save(rows: list[tuple[str, float]], db_path: str = "database.db") -> int:
    """YENİ eklenen kayıt sayısını döndürür. cursor.rowcount KULLANMA (-1 olabilir →
    'veritabanına -1 kayıt eklendi' gibi saçma bir mesaj çıkar); total_changes farkı güvenilir."""
    con = sqlite3.connect(db_path)                             # göreli yol → çalışma klasörü
    try:
        con.execute("CREATE TABLE IF NOT EXISTS items (name TEXT NOT NULL UNIQUE, price REAL)")
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO items (name, price) VALUES (?, ?)", rows)   # ASLA f-string ile SQL
        con.commit()
        return con.total_changes - before
    finally:
        con.close()
'''

JSON_API = '''
# JSON API KALIBI — zaman aşımı, durum kontrolü, gerçek alanlar
import json, sys
import requests

def get_json(url: str) -> dict | list:
    resp = requests.get(url, headers={"Accept": "application/json"}, timeout=15)
    resp.raise_for_status()
    return resp.json()

if __name__ == "__main__":
    data = get_json(sys.argv[1])
    with open("sonuc.json", "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
'''

from jarvis.actions.devkit import recipes_extra as _x  # noqa: E402

_W = lambda pattern: re.compile(pattern, re.IGNORECASE).search  # noqa: E731


def _web_static(d: str) -> bool:
    return has_url(d) and needs_browser_or_web(d) and not needs_browser(d)


_WORKDIR_PHRASE = re.compile(r"(?:çalışma|calisma|working)\s+(?:klasör|klasor|folder|directory)\w*", re.IGNORECASE)
_FOLDER_SCAN = re.compile(r"klasör|klasor|folder|dizin|directory", re.IGNORECASE)


def _folder_scan(d: str) -> bool:
    """Klasör TARAMA isteği mi? "çalışma klasöründeki X'e yaz" hemen her görevde
    geçtiği için önce bu kalıp çıkarılır (PR #20 kod incelemesi bulgusu)."""
    return bool(_FOLDER_SCAN.search(_WORKDIR_PHRASE.sub(" ", d)))


# Öncelik sırasıyla (ad, eşleştirici, kod). Görev metnine uyan ilk `limit` tanesi seçilir.
_TABLE = [
    ("playwright_scroll", needs_browser, PLAYWRIGHT_SCROLL),
    ("requests_bs4", _web_static, REQUESTS_BS4),
    ("http_parallel", _W(r"bağlantı|baglanti|\blink|durum kod|status code|url listesi"), _x.HTTP_PARALLEL),
    ("tkinter_headless", _W(r"tkinter|\bgui\b|arayüz|arayuz|pencere|buton"), _x.TKINTER_HEADLESS),
    ("pillow_images", _W(r"resim|görsel|gorsel|fotoğraf|fotograf|\bimage|thumbnail|\.jpe?g\b|\.png\b"), _x.PILLOW_IMAGES),
    ("pptx_report", _W(r"sunum|slayt|\bpptx\b|powerpoint"), _x.PPTX_REPORT),
    ("psutil_sysinfo", _W(r"\bcpu\b|\bram\b|bellek|işlemci|islemci|disk kullan|sistem bilgi"), _x.PSUTIL_SYSINFO),
    ("zip_backup", _W(r"yedek|backup|\bzip\b|arşiv|arsiv"), _x.ZIP_BACKUP),
    ("regex_extract", _W(r"e-?posta|\bemail|telefon|\bregex|düzenli ifade|duzenli ifade"), _x.REGEX_EXTRACT),
    ("file_organize", _W(r"uzantı|uzanti|tarih\w*\s+g[öo]re|organize|sınıflandır|siniflandir|klasörlere ayır"
                          r"|alt klasörlere|kopyala"),
     _x.FILE_ORGANIZE),
    ("duplicate_files", _W(r"kopya|duplicate|aynı dosya|ayni dosya|yinelenen|boş dosya|bos dosya"), DUPLICATE_FILES),
    ("csv_aggregate", _W(r"\bcsv\b"), CSV_AGGREGATE),
    ("folder_walk", _folder_scan, FOLDER_WALK),
    ("sqlite_store", _W(r"sqlite|veritaban|database|\.db\b"), SQLITE_STORE),
    ("json_api", _W(r"\bapi\b|json endpoint|\brest\b"), JSON_API),
]


def select_recipes(description: str, language: str = "python", limit: int = 2) -> list[tuple[str, str]]:
    """(ad, kod) listesi: göreve uyan en fazla `limit` özel şablon + genel G/Ç."""
    if language.strip().lower() != "python":
        return []
    d = description or ""
    picked = [(name, code) for name, match, code in _TABLE if match(d)]
    return picked[:limit] + [("general_io", GENERAL_IO)]


def recipes_block(description: str, language: str = "python") -> str:
    recipes = select_recipes(description, language)
    if not recipes:
        return ""
    body = "\n".join(code.strip() for _, code in recipes)
    return (
        "PROVEN PATTERNS (verified working code for this kind of task). Follow the SAME approach and "
        "structure; adapt names, selectors, fields and paths to THIS task. Do not copy example values "
        "blindly, and never write sample/placeholder data instead of real results:\n"
        f"{body}\n"
    )
