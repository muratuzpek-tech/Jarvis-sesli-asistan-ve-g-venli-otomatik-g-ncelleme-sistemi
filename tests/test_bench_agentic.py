"""tests/test_bench_agentic.py — scripts/bench_agentic.py olcum betigi mantigi

Betik pytest'e dahil degildir (elle calistirilir); burada mantigi senaryolu
model_fn ile, GERCEK AgenticCoder.solve uzerinden dogrulanir:
- her kosu ayri gecici HOME + JARVIS_HOME + proje klasorunde; gercek ev
  dizini ve ortam degiskenleri degismez
- ozet: basari orani (Durum: BAŞARILI), ortalama tur/sure, ret sayisi,
  en sik hata kategorisi; tablo ve --out JSON
- model hata firlatirsa kosu basarisiz sayilir, betik cokmez
- Ollama yoksa temiz hata mesaji (traceback yok), cikis kodu 2

Gercek Ollama'ya baglanilmaz: model_fn senaryoludur; check_ollama yalnizca
kapali bir porta ve yerel sahte bir HTTP sunucusuna karsi denenir.
"""
import importlib.util
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, '.')

from tests.test_agentic_coder_live import MAIN, README, _decide, _write

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "bench_agentic.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("bench_agentic", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def real_env(tmp_path, monkeypatch):
    """'Gercek' ev ve JARVIS_HOME (tmp altinda): betik bunlara DOKUNMAMALI."""
    home, jh = tmp_path / "realhome", tmp_path / "realjh"
    home.mkdir()
    jh.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("JARVIS_HOME", str(jh))
    return home, jh


def _success_factory(seen: list):
    """Her kosu icin taze senaryo: main.py, README.md, accept."""
    def factory():
        seq = iter([_write("main.py", MAIN), _write("README.md", README),
                    _decide({"action": "accept", "args": {}, "response": "bitti"})])

        def model(prompt: str) -> str:
            seen.append((os.environ["HOME"], os.environ["JARVIS_HOME"]))
            return next(seq, _decide({"action": "inspect", "args": {}}))
        return model
    return factory


# ── 1) gercek solve, izolasyon ──

def test_runs_are_isolated_and_successful(bench, real_env, tmp_path):
    home, jh = real_env
    seen: list = []
    work = tmp_path / "work"
    records = bench.run_benchmark(
        model="sahte:1b", runs=2, model_fn_factory=_success_factory(seen),
        tasks=(("cli", "hesap makinesi yaz"),), workdir=work, progress=lambda m: None,
    )
    assert [r["success"] for r in records] == [True, True]
    assert all(r["iterations"] == 3 and r["rejects"] == 0 for r in records)
    assert all(r["seconds"] >= 0 and r["model"] == "sahte:1b" and r["task"] == "cli" for r in records)
    homes = {h for h, _ in seen}
    jhomes = {j for _, j in seen}
    assert len(homes) == 2 and len(jhomes) == 2                 # her kosu ayri
    assert str(home) not in homes and str(jh) not in jhomes
    assert all(Path(h).is_relative_to(work) for h in homes)
    # gercek ortam geri geldi, gercek eve hicbir sey yazilmadi
    assert os.environ["HOME"] == str(home) and os.environ["JARVIS_HOME"] == str(jh)
    assert list(home.iterdir()) == [] and list(jh.iterdir()) == []


def test_default_workdir_is_removed(bench, real_env):
    created: list = []

    def factory():
        def model(prompt):
            created.append(Path(os.environ["HOME"]))
            return _decide({"action": "inspect", "args": {}})
        return model

    bench.run_benchmark(model="m", runs=1, model_fn_factory=factory,
                        tasks=(("cli", "hesap makinesi yaz"),), max_iterations=1,
                        progress=lambda m: None)
    assert created and not created[0].exists()


def test_model_exception_counts_as_failure(bench, real_env, tmp_path):
    def factory():
        def model(prompt):
            raise ConnectionError("bağlantı reddedildi")
        return model

    records = bench.run_benchmark(model="m", runs=1, model_fn_factory=factory,
                                  tasks=(("cli", "hesap makinesi yaz"),), workdir=tmp_path / "w",
                                  progress=lambda m: None)
    assert records[0]["success"] is False
    assert records[0]["error"].startswith("ConnectionError")
    assert records[0]["error_categories"] == {"İSTİSNA": 1}
    assert os.environ["HOME"] == str(real_env[0])


# ── 2) ozet + tablo ──

def _rec(task, ok, it, sec, rej, cats, model="qwen3:14b"):
    return {"model": model, "task": task, "run": 1, "success": ok, "iterations": it,
            "seconds": sec, "rejects": rej, "error_categories": cats, "error": None}


def test_summarize_computes_rates_averages_and_top_error(bench):
    rows = bench.summarize([
        _rec("cli", True, 3, 10.0, 0, {}),
        _rec("cli", False, 7, 20.0, 3, {"SYNTAX": 2, "EVAL": 1}),
        _rec("cli", True, 5, 30.0, 1, {"EVAL": 1}),
        _rec("tk", False, 25, 100.0, 4, {"LLM": 1, "KİLİT": 3}),
    ])
    assert [r["task"] for r in rows] == ["cli", "tk"]
    cli, tk = rows
    assert cli["runs"] == 3 and cli["successes"] == 2
    assert cli["success_rate"] == pytest.approx(2 / 3)
    assert cli["avg_iterations"] == pytest.approx(5.0)
    assert cli["avg_seconds"] == pytest.approx(20.0)
    assert cli["rejects"] == 4
    assert cli["error_categories"] == {"SYNTAX": 2, "EVAL": 2}
    assert cli["top_error"] == "SYNTAX"                       # esitlikte ilk gorulen
    assert tk["top_error"] == "KİLİT" and tk["success_rate"] == 0.0


def test_summarize_no_errors_and_empty(bench):
    assert bench.summarize([]) == []
    (row,) = bench.summarize([_rec("cli", True, 3, 1.0, 0, {})])
    assert row["top_error"] == "-"


def test_format_table_has_header_and_values(bench):
    rows = bench.summarize([_rec("cli", True, 3, 12.34, 0, {}),
                            _rec("cli", False, 6, 20.0, 2, {"SYNTAX": 2})])
    table = bench.format_table(rows)
    lines = table.splitlines()
    for col in ("model", "görev", "başarı", "ort. tur", "ort. süre", "ret", "en sık hata"):
        assert col in lines[0]
    assert "qwen3:14b" in table and "cli" in table
    assert "1/2 (50%)" in table and "4.5" in table and "16.2" in table and "SYNTAX" in table


# ── 3) main: --out JSON, tum gorevler, Ollama yok ──

def test_main_writes_json_and_prints_table(bench, real_env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OLLAMA_CODER_MODEL", "sahte:1b")
    out = tmp_path / "sonuc.json"

    def factory():
        return lambda prompt: _decide({"action": "inspect", "args": {}})

    code = bench.main(["--runs", "2", "--max-iter", "1", "--out", str(out),
                       "--workdir", str(tmp_path / "w")],
                      model_fn_factory=factory, checker=lambda m: pytest.fail("checker cagrilmamali"))
    assert code == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["model"] == "sahte:1b" and data["runs"] == 2
    assert [r["task"] for r in data["summary"]] == [t for t, _ in bench.TASKS]
    assert len(bench.TASKS) == 3 and len(data["records"]) == 6
    assert all(r["success_rate"] == 0.0 for r in data["summary"])
    printed = capsys.readouterr().out
    assert "en sık hata" in printed and "sahte:1b" in printed


def test_main_without_ollama_prints_clean_error(bench, real_env, monkeypatch, capsys):
    monkeypatch.setenv("OLLAMA_CODER_MODEL", "qwen3:14b")
    code = bench.main(["--runs", "1"], checker=lambda m: "Ollama'ya bağlanılamadı: bağlantı reddedildi")
    assert code == 2
    err = capsys.readouterr().err
    assert "Ollama'ya bağlanılamadı" in err and "Traceback" not in err


def test_main_rejects_bad_runs(bench, real_env, capsys):
    with pytest.raises(SystemExit) as exc:
        bench.main(["--runs", "0"], checker=lambda m: None)
    assert exc.value.code == 2


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_check_ollama_unreachable(bench):
    msg = bench.check_ollama("qwen3:14b", base=f"http://127.0.0.1:{_closed_port()}")
    assert msg and "Ollama" in msg


def test_check_ollama_model_missing_and_present(bench):
    class Tags(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"models": [{"name": "qwen3:14b"}, {"name": "gemma3:latest"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Tags)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        assert bench.check_ollama("qwen3:14b", base=base) is None
        assert bench.check_ollama("gemma3", base=base) is None         # ":latest" yazmadan da
        assert bench.check_ollama("qwen3", base=base)                   # qwen3:latest kurulu degil
        missing = bench.check_ollama("llama9:70b", base=base)
        assert missing and "llama9:70b" in missing and "ollama pull" in missing
    finally:
        srv.shutdown()
        srv.server_close()
