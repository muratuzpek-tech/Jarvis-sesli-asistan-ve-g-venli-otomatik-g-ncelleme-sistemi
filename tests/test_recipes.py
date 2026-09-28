"""Usta şablonları: doğru seçilmeli, derlenmeli, çevrimdışı olanlar gerçekten çalışmalı."""
from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys

import pytest

from jarvis.actions.devkit import recipes as r

from jarvis.actions.devkit import recipes_extra as x  # noqa: E402

ALL = ["GENERAL_IO", "PLAYWRIGHT_SCROLL", "REQUESTS_BS4", "CSV_AGGREGATE", "FOLDER_WALK", "SQLITE_STORE", "JSON_API"]
EXTRA = ["TKINTER_HEADLESS", "PILLOW_IMAGES", "PSUTIL_SYSINFO", "ZIP_BACKUP", "REGEX_EXTRACT", "HTTP_PARALLEL",
         "FILE_ORGANIZE", "PPTX_REPORT"]


@pytest.mark.parametrize("name", ALL)
def test_every_recipe_compiles(name):
    ast.parse(getattr(r, name))


@pytest.mark.parametrize("name", EXTRA)
def test_every_extra_recipe_compiles(name):
    ast.parse(getattr(x, name))


@pytest.mark.parametrize("desc,expected", [
    ("https://quotes.toscrape.com/scroll sayfasını aşağı kaydırarak topla", "playwright_scroll"),
    ("https://quotes.toscrape.com/ ilk sayfa, JavaScript gerekmez, kazı", "requests_bs4"),
    ("satislar.csv dosyasındaki toplamları hesapla", "csv_aggregate"),
    ("bir klasördeki txt dosyalarını tara", "folder_walk"),
    ("sonuçları sqlite veritabanına kaydet", "sqlite_store"),
    ("bir REST API'den JSON çek", "json_api"),
    ("tkinter arayüzlü not uygulaması", "tkinter_headless"),
    ("bir klasördeki resimleri küçült", "pillow_images"),
    ("cpu ve ram kullanımını raporla", "psutil_sysinfo"),
    ("belgeler klasörünü zip olarak yedekle", "zip_backup"),
    ("metindeki e-posta ve telefonları ayıkla", "regex_extract"),
    ("dosyaları uzantılarına göre klasörlere ayır", "file_organize"),
    ("verilerden powerpoint sunum yap", "pptx_report"),
    ("url listesindeki bağlantıların durum kodunu kontrol et", "http_parallel"),
])
def test_selection(desc, expected):
    names = [n for n, _ in r.select_recipes(desc)]
    assert expected in names and names[-1] == "general_io" and len(names) <= 3


def test_no_recipes_for_go():
    assert r.select_recipes("RAM ölç", "go") == [] and r.recipes_block("RAM ölç", "go") == ""


def _run(code: str, tmp_path, *args):
    (tmp_path / "prog.py").write_text(code, encoding="utf-8")
    import os
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, "prog.py", *args], cwd=tmp_path, capture_output=True, text=True,
                          encoding="utf-8", timeout=30, env=env)


def test_csv_and_folder_and_general_recipes_really_work(tmp_path):
    (tmp_path / "s.csv").write_text("city,temperature\nAnkara,10\nIzmir,20\nAnkara,14\n")
    assert _run(r.CSV_AGGREGATE, tmp_path, "s.csv").returncode == 0
    assert json.loads((tmp_path / "averages.json").read_text()) == {"Ankara": 12.0, "Izmir": 20.0}
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "a.bin").write_bytes(b"x" * 100)
    (tmp_path / "m" / "b.bin").write_bytes(b"x" * 10)
    assert _run(r.FOLDER_WALK, tmp_path, "m").returncode == 0
    assert (tmp_path / "sizes.txt").read_text().splitlines()[0] == "a.bin: 100 bytes"
    assert _run(r.GENERAL_IO, tmp_path, "s.csv").returncode == 0
    assert (tmp_path / "result.txt").read_text() == "satir: 4\n"
    assert _run(r.GENERAL_IO, tmp_path).returncode != 0  # girdi yoksa açıkça çıkmalı


def test_sqlite_recipe_really_works(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ns: dict = {}
    exec(compile(r.SQLITE_STORE, "sqlite_store", "exec"), ns)  # noqa: S102 - bizim kendi şablonumuz
    assert ns["save"]([("kalem", 10.0), ("defter", 25.0)]) == 2
    assert sqlite3.connect("database.db").execute("SELECT SUM(price) FROM items").fetchone()[0] == 35.0


def test_recipes_reach_writer_prompt(monkeypatch, tmp_path):
    import jarvis.actions.dev_agent as da
    seen = []

    class M:
        def generate_content(self, prompt):
            seen.append(prompt)
            return type("R", (), {"text": "print('x')\n"})()

    monkeypatch.setattr(da, "_get_model", lambda name: M())
    da._write_file(file_info={"path": "main.py", "description": "entry", "imports": []},
                   project_description="https://quotes.toscrape.com/scroll kaydırarak 20 alıntı topla",
                   all_files=[{"path": "main.py"}], language="python", project_dir=tmp_path, already_written={})
    assert "PROVEN PATTERNS" in seen[0] and "sync_playwright" in seen[0] and "max_rounds" in seen[0]


def test_extra_stdlib_recipes_really_work(tmp_path):
    assert _run(x.TKINTER_HEADLESS, tmp_path, "--headless-test").stdout.strip() == "SUCCESS"
    assert json.loads((tmp_path / "sonuc.json").read_text()) == {"adet": 2, "ilk": "a"}

    (tmp_path / "m.txt").write_text("ali@ornek.com, 0532 123 45 67, tekrar ali@ornek.com", encoding="utf-8")
    assert _run(x.REGEX_EXTRACT, tmp_path, "m.txt").returncode == 0
    assert (tmp_path / "bulunanlar.csv").read_text(encoding="utf-8").splitlines()[1:] == [
        "email,ali@ornek.com", "telefon,5321234567"]

    src = tmp_path / "k"
    (src / "alt").mkdir(parents=True)
    for n in ("a.txt", "alt/b.txt", "c.md"):
        (src / n).write_text("x")
    assert _run(x.FILE_ORGANIZE, tmp_path, "k").returncode == 0
    assert sorted(p.relative_to(tmp_path / "duzenli").as_posix() for p in (tmp_path / "duzenli").rglob("*.*")) == [
        "md/c.md", "txt/a.txt", "txt/b.txt"]
    assert (src / "a.txt").exists(), "kaynak dosyalar yerinde kalmalı (kopyalama)"

    out = _run(x.ZIP_BACKUP, tmp_path, "k")
    assert out.returncode == 0 and "3 dosya" in out.stdout


@pytest.mark.parametrize("desc,folder", [
    ("satislar.csv toplamlarını çalışma klasöründeki ozet.json dosyasına yaz", False),
    ("sonucu çalışma klasörüne kaydet", False),
    ("metinler klasöründeki txt dosyalarını say, çalışma klasöründeki report.txt'ye yaz", True),
    ("bir klasördeki dosyaları tara", True),
])
def test_working_folder_phrase_does_not_trigger_folder_scan(desc, folder):
    """PR #20 kod incelemesi: 'çalışma klasöründeki' hemen her görevde geçiyordu."""
    assert ("folder_walk" in [n for n, _ in r.select_recipes(desc)]) is folder


def test_templates_do_not_contain_test_answers():
    """Kopya kağıdı yasak: şablonlar YÖNTEM öğretir, canlı test görevlerinin
    cevabını (çıktı dosya adları, site seçicileri, sütun adları) içermez.
    (2026-09-29: ilk şablonlar sabit görevlerin cevabını içeriyordu; 5/5 kısmen şişikti.)"""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "scripts"
    tasks = "\n".join((root / f).read_text(encoding="utf-8")
                      for f in ("canli_test.py", "canli_test_surpriz.py", "canli_test_karmasik.py"))
    forbidden = set(re.findall(r"\b[\w-]+\.(?:json|csv|txt|log|html|md|db|png|zip)\b", tasks))
    forbidden |= {"div.quote", "span.text", "small.author", "birim_fiyat", "urun", "musteri_id", "quotes.toscrape",
                  "books.toscrape", "product_pod", "price_color"}
    forbidden -= {"main.py"}
    templates = "\n".join(getattr(r, n) for n in ALL) + "\n".join(getattr(x, n) for n in EXTRA)
    leaks = sorted(t for t in forbidden if re.search(rf"(?<![\w.-]){re.escape(t)}(?![\w-])", templates))
    assert not leaks, f"Şablonlarda test cevabı var: {leaks}"
