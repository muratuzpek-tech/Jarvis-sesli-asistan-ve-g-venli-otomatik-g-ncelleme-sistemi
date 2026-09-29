"""guncelle-30 (Murat@goxs 2026-09-30): 'en ucuz 5 kitap' görevinde tablo 3 satır
gösterdi ve başarı sayıldı; 2. ve 3. sayfa 404 verdi (…/catalogue/catalogue/page-2.html)."""
from __future__ import annotations

import os
import sqlite3
import textwrap

from jarvis.actions import browser_control as bc
from jarvis.actions import dev_agent as da
from jarvis.actions.devkit import recipes


def test_requested_count_from_task():
    t = "books.toscrape.com ilk 3 sayfadan 20 sterlinden ucuz kitapları topla, en ucuz 5 kitabı tablo olarak göster"
    assert da._requested_count(t) == 5
    assert da._requested_count("top 10 haberi listele") == 10
    assert da._requested_count("en az 3 sayfa gez") is None
    assert da._requested_count("hava durumunu göster") is None


def _server(tmp_path, rows, port, extra=""):
    code = textwrap.dedent(f'''
        {extra}
        from http.server import BaseHTTPRequestHandler, HTTPServer
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = "<table><tr><th>Ad</th></tr>" + "".join("<tr><td>k</td></tr>" for _ in range({rows})) + "</table>"
                self.send_response(200); self.end_headers(); self.wfile.write(body.encode())
            def log_message(self, *a):
                pass
        print("Serving on http://127.0.0.1:{port}", flush=True)
        HTTPServer(("127.0.0.1", {port}), H).serve_forever()
    ''')
    (tmp_path / "main.py").write_text(code, encoding="utf-8")
    return {"main.py": code}


def test_probe_fails_when_fewer_rows_than_task_asks(tmp_path):
    codes = _server(tmp_path, rows=3, port=18771)
    ok, note = da._probe_web_app("python main.py", tmp_path, codes, wait_s=6, min_rows=5)
    assert ok is False and "3 veri satırı" in note and "5" in note, note


def test_probe_passes_when_enough_rows(tmp_path):
    codes = _server(tmp_path, rows=5, port=18772)
    ok, note = da._probe_web_app("python main.py", tmp_path, codes, wait_s=10, min_rows=5)
    assert ok, note


def test_probe_fails_on_page_fetch_error(tmp_path):
    codes = _server(tmp_path, rows=5, port=18773,
                    extra='import sys; print("Failed to fetch page http://x/catalogue/catalogue/page-2.html: '
                          '404 Client Error: Not Found", file=sys.stderr, flush=True)')
    ok, note = da._probe_web_app("python main.py", tmp_path, codes, wait_s=10)
    assert ok is False and "404" in note, note


def test_probe_fails_on_prior_output_error(tmp_path):
    ok, note = da._probe_web_app("python main.py", tmp_path, {"main.py": ""}, wait_s=3,
                                 prior_output="Failed to fetch page http://x/page-2.html: 404 Client Error")
    assert ok is False and "404" in note


def test_speakable_url():
    assert da._speakable("adres http://127.0.0.1:5000/ açık") == "adres yerel adres, 5000 numaralı port açık"


def test_scraper_recipe_follows_next_link():
    assert 'li.next a' in recipes.REQUESTS_BS4 and "urljoin(url, nxt" in recipes.REQUESTS_BS4
    assert 'catalogue/page-{page}' not in recipes.REQUESTS_BS4


def test_sqlite_recipe_returns_new_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ns: dict = {}
    exec(compile(recipes.SQLITE_STORE, "sqlite_store", "exec"), ns)  # noqa: S102 - kendi şablonumuz
    assert ns["save"]([("a", 1.0), ("b", 2.0)]) == 2
    assert ns["save"]([("a", 1.0), ("c", 3.0)]) == 1          # tekrar: yalnız yeni olan sayılır, asla -1
    assert sqlite3.connect("database.db").execute("SELECT COUNT(*) FROM items").fetchone()[0] == 3


def test_stale_browser_lock_is_cleared_only_for_dead_process(tmp_path):
    lock = tmp_path / "SingletonLock"
    os.symlink("goxs-999999", lock)                           # böyle bir süreç yok
    (tmp_path / "SingletonCookie").write_text("x")
    assert bc._clear_stale_chromium_lock(tmp_path)
    assert not lock.is_symlink() and not (tmp_path / "SingletonCookie").exists()
    os.symlink(f"goxs-{os.getpid()}", lock)                   # yaşayan süreç: dokunulmaz
    assert not bc._clear_stale_chromium_lock(tmp_path)
    assert lock.is_symlink()
