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
    Path("report.txt").write_text(result, encoding="utf-8")   # ASLA Path(__file__).parent / ...
    print(f"Wrote report.txt ({len(result)} chars)")

if __name__ == "__main__":
    main()
'''

PLAYWRIGHT_SCROLL = '''
# JAVASCRIPT / SONSUZ KAYDIRMA KALIBI — Playwright (Selenium DEĞİL), SERT DURDURMA ile
import json, sys
from playwright.sync_api import sync_playwright

def scrape(url: str, target: int = 20, max_scrolls: int = 40) -> list[dict]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=30_000)
        page.wait_for_selector("div.quote", timeout=10_000)       # seçiciyi gerçek sayfaya göre seç
        for _ in range(max_scrolls):                               # döngü HER ZAMAN sınırlı
            if page.locator("div.quote").count() >= target:        # yeterince öğe → dur
                break
            page.mouse.wheel(0, 20_000)
            page.wait_for_timeout(800)
        items = [{"id": i, "text": q.locator("span.text").inner_text(),
                  "author": q.locator("small.author").inner_text()}
                 for i, q in enumerate(page.locator("div.quote").all()[:target], 1)]
        browser.close()
    return items

if __name__ == "__main__":
    data = scrape(sys.argv[1])
    with open("quotes.json", "w", encoding="utf-8") as fh:     # göreli yol
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"Saved {len(data)} items")                           # GERÇEK veri; asla örnek/uydurma veri yazma
'''

REQUESTS_BS4 = '''
# DÜZ SAYFA KAZIMA KALIBI — requests + BeautifulSoup (JavaScript gerekmiyorsa)
import csv, sys
import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

def fetch(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()                                   # hatayı yutma, görünür olsun
    return BeautifulSoup(resp.text, "html.parser")

if __name__ == "__main__":
    soup = fetch(sys.argv[1])
    rows = [(q.select_one(".text").get_text(strip=True), q.select_one(".author").get_text(strip=True))
            for q in soup.select("div.quote")]                  # seçiciyi gerçek sayfaya göre seç
    if not rows:
        sys.exit("Sayfada öğe bulunamadı — seçici yanlış olabilir")   # sessizce boş dosya yazma
    with open("quotes.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["text", "author"])
        writer.writerows(rows)
'''

CSV_AGGREGATE = '''
# CSV TOPLAMA KALIBI — DictReader + sayıya çevirme + gruplama
import csv, json, sys
from collections import defaultdict

def totals(path: str) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):                        # sütun adlarıyla eriş (row["urun"])
            out[row["urun"]] += float(row["adet"]) * float(row["birim_fiyat"])
    return dict(out)

if __name__ == "__main__":
    with open("ozet.json", "w", encoding="utf-8") as fh:
        json.dump(totals(sys.argv[1]), fh, ensure_ascii=False, indent=2)
'''

FOLDER_WALK = '''
# KLASÖR TARAMA KALIBI — alt klasörler dahil, yalnızca dosyalar
import sys
from collections import Counter
from pathlib import Path

def scan(root: Path) -> Counter:
    counts: Counter = Counter()
    for p in root.rglob("*"):
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace")
            counts.update(w.lower() for w in text.split())
    return counts

if __name__ == "__main__":
    counts = scan(Path(sys.argv[1]))
    lines = [f"{word}: {n}" for word, n in counts.most_common(10)]   # gerçek değerler, şablon metin değil
    Path("report.txt").write_text("\\n".join(lines) + "\\n", encoding="utf-8")
'''

SQLITE_STORE = '''
# SQLITE KALIBI — tablo oluştur, parametreli ekle, kapatmayı unutma
import sqlite3

def save(rows: list[tuple[str, float]], db_path: str = "database.db") -> int:
    con = sqlite3.connect(db_path)                             # göreli yol → çalışma klasörü
    try:
        con.execute("CREATE TABLE IF NOT EXISTS items (name TEXT NOT NULL, price REAL)")
        con.executemany("INSERT INTO items (name, price) VALUES (?, ?)", rows)   # ASLA f-string ile SQL
        con.commit()
        return con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
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
