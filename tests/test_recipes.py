"""Usta şablonları: doğru seçilmeli, derlenmeli, çevrimdışı olanlar gerçekten çalışmalı."""
from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys

import pytest

from jarvis.actions.devkit import recipes as r

ALL = ["GENERAL_IO", "PLAYWRIGHT_SCROLL", "REQUESTS_BS4", "CSV_AGGREGATE", "FOLDER_WALK", "SQLITE_STORE", "JSON_API"]


@pytest.mark.parametrize("name", ALL)
def test_every_recipe_compiles(name):
    ast.parse(getattr(r, name))


@pytest.mark.parametrize("desc,expected", [
    ("https://quotes.toscrape.com/scroll sayfasını aşağı kaydırarak topla", "playwright_scroll"),
    ("https://quotes.toscrape.com/ ilk sayfa, JavaScript gerekmez, kazı", "requests_bs4"),
    ("satislar.csv dosyasındaki toplamları hesapla", "csv_aggregate"),
    ("bir klasördeki txt dosyalarını tara", "folder_walk"),
    ("sonuçları sqlite veritabanına kaydet", "sqlite_store"),
    ("bir REST API'den JSON çek", "json_api"),
])
def test_selection(desc, expected):
    names = [n for n, _ in r.select_recipes(desc)]
    assert expected in names and names[-1] == "general_io" and len(names) <= 3


def test_no_recipes_for_go():
    assert r.select_recipes("RAM ölç", "go") == [] and r.recipes_block("RAM ölç", "go") == ""


def _run(code: str, tmp_path, *args):
    (tmp_path / "prog.py").write_text(code, encoding="utf-8")
    return subprocess.run([sys.executable, "prog.py", *args], cwd=tmp_path, capture_output=True, text=True, timeout=30)


def test_csv_and_folder_and_general_recipes_really_work(tmp_path):
    (tmp_path / "s.csv").write_text("urun,adet,birim_fiyat\nkalem,2,10\ndefter,1,25\nkalem,3,10\n")
    assert _run(r.CSV_AGGREGATE, tmp_path, "s.csv").returncode == 0
    assert json.loads((tmp_path / "ozet.json").read_text()) == {"kalem": 50.0, "defter": 25.0}
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "a.txt").write_text("elma armut elma")
    (tmp_path / "m" / "b.txt").write_text("elma")
    assert _run(r.FOLDER_WALK, tmp_path, "m").returncode == 0
    assert (tmp_path / "report.txt").read_text().splitlines()[0] == "elma: 3"
    assert _run(r.GENERAL_IO, tmp_path, "s.csv").returncode == 0
    assert (tmp_path / "report.txt").read_text() == "satir: 4\n"
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
    assert "PROVEN PATTERNS" in seen[0] and "sync_playwright" in seen[0] and "max_scrolls" in seen[0]
