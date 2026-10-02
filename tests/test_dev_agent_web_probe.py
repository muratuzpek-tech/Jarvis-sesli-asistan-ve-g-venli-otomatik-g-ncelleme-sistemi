"""Web sunucusu olan programlar gerçekten açılıp sayfadaki tablo kontrol edilir
(Murat@goxs 2026-09-30: Flask sunucusu '90 sn'de tamamlanamadı' diye raporlandı)."""
from __future__ import annotations

import textwrap

from jarvis.actions import dev_agent as da

SERVER = textwrap.dedent('''
    from http.server import BaseHTTPRequestHandler, HTTPServer
    ROWS = {rows}
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = "<table><tr><th>Ad</th></tr>" + "".join(f"<tr><td>k{{i}}</td></tr>" for i in range(ROWS)) + "</table>"
            self.send_response(200); self.end_headers(); self.wfile.write(body.encode())
        def log_message(self, *a):
            pass
    print("Serving on http://127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1", {port}), H).serve_forever()
''')


def _project(tmp_path, rows, port):
    code = SERVER.format(rows=rows, port=port)
    (tmp_path / "main.py").write_text(code, encoding="utf-8")
    return {"main.py": code}


def test_detects_web_apps():
    assert da._is_web_app({"main.py": "app = Flask(__name__)\napp.run(port=5000)"})
    assert da._is_web_app({"s.py": "from http.server import HTTPServer"})
    assert not da._is_web_app({"main.py": "print('merhaba')"})


def test_probe_finds_table_rows_and_kills_server(tmp_path):
    codes = _project(tmp_path, rows=5, port=18765)
    ok, note = da._probe_web_app("python main.py", tmp_path, codes, wait_s=15)
    assert ok and "5 veri satırı" in note, note
    import urllib.request
    try:
        urllib.request.urlopen("http://127.0.0.1:18765/", timeout=2)
        alive = True
    except Exception:
        alive = False
    assert not alive, "sunucu doğrulamadan sonra kapatılmalı"


def test_probe_reports_empty_table(tmp_path):
    codes = _project(tmp_path, rows=0, port=18766)
    ok, note = da._probe_web_app("python main.py", tmp_path, codes, wait_s=15)
    assert ok is False and "BOŞ" in note


def test_probe_reports_crashing_server(tmp_path):
    (tmp_path / "main.py").write_text("import flask_yok\n", encoding="utf-8")
    ok, note = da._probe_web_app("python main.py", tmp_path, {"main.py": "app.run()"}, wait_s=10)
    assert ok is False and "kapandı" in note


def test_data_outputs_are_cleared_between_attempts(tmp_path):
    (tmp_path / "database.db").write_text("x")
    (tmp_path / "rapor.html").write_text("x")
    da._clear_data_outputs(tmp_path, [{"path": "database.db"}, "rapor.html", "../disari.db"])
    assert not (tmp_path / "database.db").exists() and (tmp_path / "rapor.html").exists()


def test_sqlite_recipe_prevents_duplicates():
    from jarvis.actions.devkit import recipes
    assert "INSERT OR IGNORE" in recipes.SQLITE_STORE and "UNIQUE" in recipes.SQLITE_STORE
