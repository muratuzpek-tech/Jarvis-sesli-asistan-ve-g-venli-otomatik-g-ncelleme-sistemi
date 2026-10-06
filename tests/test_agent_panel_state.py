"""tests/test_agent_panel_state.py — pano JSON'u: izin listesi + maskeleme

agent_panel_state.build_panel_state, agentic_coder.live_status() ve
agent_board.list_jobs() ciktisindan YALNIZCA izinli alanlarla JSON uretir.
Her metin sanitize + redact_text + anahtar maskesinden gecer, uzunlugu
sinirlidir; ev dizini yollari proje adina indirgenir.

Sizinti testi gercek bir solve() + gercek agent_board veritabani (tmp_path)
uzerinden yapilir: sahte GEMINI_API_KEY, onay kodu, ev yolu, ham model
ciktisi ve dosya icerigi cikti JSON'unda GECMEMELI.
"""
import asyncio
import json
import sys
import threading

import pytest

sys.path.insert(0, '.')
sys.path.insert(0, 'src')

from jarvis import agent_panel_state as ps
from jarvis.actions import agent_board as ab
from tools.developer import agentic_coder as ac

FAKE_KEY = "AIzaSyFAKE0123456789abcdefghijklmnopqrs"
ODD_KEY = "sahte-anahtar-ZXQ9-7781-plain"          # AIza onekisiz: ortam degeri taranmali
CONFIRM = "482913"
CONFIRM2 = "ZK7-991"
RAW = "HAM_MODEL_CIKTISI_91bd"
CONTENT = "DOSYA_ICERIGI_7f3a"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture(autouse=True)
def _clean_live():
    ac._LIVE.clear()
    yield
    ac._LIVE.clear()


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "DB_PATH", tmp_path / "agent_board.db")


def _run(**over) -> dict:
    run = {
        "task_id": "ab12cd34", "project": "hesap", "description": "hesap makinesi yaz",
        "status": "çalışıyor", "iteration": 3, "max_iterations": 25,
        "files": [{"name": "main.py", "size": 412}],
        "rejects": {"reject": 1, "eval": 0, "lock": 0, "method": 0, "ruff": 0},
        "model": {"provider": "gemini", "name": "gemini-flash-latest"},
        "events": ["#1 write: 📝 main.py (412 chars) (OK)"], "reason": "",
    }
    run.update(over)
    return run


def _job(**over) -> dict:
    job = {"id": "aaaa1111", "description": "not defteri yaz", "status": "completed",
           "result": "Durum: BAŞARILI\nProje klasörü: /p/notlar\n\ngovde",
           "started_at": "2026-10-06T10:00:00.123456", "finished_at": "2026-10-06T10:05:00"}
    job.update(over)
    return job


# ── izin listesi ve sekil ──

def test_output_has_only_allowlisted_fields_and_is_json():
    state = ps.build_panel_state([_run(secret="x", files=[{"name": "a.py", "size": 3,
                                                           "content": CONTENT}])],
                                 [_job(extra="y")])
    assert set(state) == {"schema", "runs", "jobs"}
    assert set(state["runs"][0]) == {"task_id", "project", "description", "status", "iteration",
                                     "max_iterations", "files", "rejects", "model", "events", "reason"}
    assert state["runs"][0]["files"] == [{"name": "a.py", "size": 3}]
    assert set(state["jobs"][0]) == {"id", "description", "status", "started_at",
                                     "finished_at", "summary"}
    assert state["jobs"][0]["summary"] == ["Durum: BAŞARILI", "Proje klasörü: notlar"]
    assert CONTENT not in json.dumps(state, ensure_ascii=False)


def test_bad_values_are_normalized_not_trusted():
    state = ps.build_panel_state(
        [_run(task_id="<script>", status="hacklendi", iteration="9; rm -rf", max_iterations=-4,
              model={"provider": "evil", "name": "m" * 500}, rejects={"reject": "çok", "eval": 2,
                                                                      "other": 7},
              files="degil-liste", events=None),
         "liste-degil", 42],
        [_job(id="'; DROP", status="??", started_at="dun", finished_at=None), None])
    run = state["runs"][0]
    assert len(state["runs"]) == 1 and len(state["jobs"]) == 1
    assert run["task_id"] == "?" and run["status"] == "bilinmiyor"
    assert run["iteration"] == 0 and run["max_iterations"] == 0
    assert run["model"]["provider"] == "bilinmiyor" and len(run["model"]["name"]) <= 60
    assert run["rejects"] == {"reject": 0, "eval": 2, "lock": 0, "method": 0, "ruff": 0}
    assert run["files"] == [] and run["events"] == []
    job = state["jobs"][0]
    assert job["id"] == "?" and job["status"] == "bilinmiyor"
    assert job["started_at"] == "" and job["finished_at"] == ""


def test_lengths_and_counts_are_bounded():
    state = ps.build_panel_state(
        [_run(description="d" * 1000, events=[f"olay {n} " + "e" * 500 for n in range(50)],
              files=[{"name": "n" * 400, "size": n} for n in range(200)])] * 30,
        [_job(description="j" * 1000)] * 30)
    run = state["runs"][0]
    assert len(state["runs"]) <= 10 and len(state["jobs"]) <= 10
    assert len(run["description"]) <= 80
    assert len(run["events"]) == 20 and all(len(e) <= 120 for e in run["events"])
    assert run["events"][-1].startswith("olay 49")       # en yeni 20
    assert len(run["files"]) <= 50 and all(len(f["name"]) <= 120 for f in run["files"])
    assert len(state["jobs"][0]["description"]) <= 80


# ── maskeleme ──

@pytest.mark.parametrize("raw,forbidden", [
    (f"anahtar {FAKE_KEY} burada", FAKE_KEY[:12]),
    (f"kesik {FAKE_KEY[:9]}", FAKE_KEY[:9]),
    (f"https://x.googleapis.com/v1?alt=json&key={ODD_KEY}", ODD_KEY),
    (f"Onay kodu: {CONFIRM}", CONFIRM),
    (f"confirm_code='{CONFIRM2}'", CONFIRM2),
    ("api_key=gizli123456", "gizli123456"),
    ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
    ("parolam: Kedi1234", "Kedi1234"),
])
def test_clean_text_masks_secrets(raw, forbidden):
    assert forbidden not in ps.clean_text(raw)


def test_env_secret_values_are_scrubbed_even_without_known_format(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", ODD_KEY)
    assert ODD_KEY not in ps.clean_text(f"hata metni {ODD_KEY} sonu")
    assert ODD_KEY[:12] not in ps.clean_text(f"kesik {ODD_KEY[:12]}")


def test_home_paths_reduce_to_last_component(home):
    out = ps.clean_text(f"Proje klasörü: {home}/jarvis_programs/gizli_proje ve /home/baska/x/y.py "
                        r"ve C:\Users\ali\Desktop\z.txt ve ~/belgeler/ozel.txt")
    assert str(home) not in out and "/home/" not in out and "Users" not in out
    assert "gizli_proje" in out and "y.py" in out and "z.txt" in out and "ozel.txt" in out
    assert "belgeler" not in out and "jarvis_programs" not in out


def test_clean_text_collapses_newlines_and_limits():
    out = ps.clean_text("satir1\nsatir2\r\n\tsatir3 " + "x" * 500, limit=40)
    assert "\n" not in out and "\t" not in out and len(out) <= 40
    assert out.startswith("satir1 satir2 satir3")


# ── sizinti testi: gercek solve + gercek pano veritabani ──

def test_no_secret_reaches_panel_json_end_to_end(home, db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", ODD_KEY)
    project = home / "jarvis_programs" / "gizli_proje"
    description = (f"Onay kodu: {CONFIRM} confirm_code='{CONFIRM2}' {FAKE_KEY} {ODD_KEY} "
                   f"{home}/belgeler/ozel.txt hesap makinesi yaz")
    main = ('"""Hesap makinesi."""\n\nAPI = "' + FAKE_KEY + '"\nNOT = "' + CONTENT + '"\n\n\n'
            "def topla(a: float, b: float) -> float:\n    \"\"\"Toplar.\"\"\"\n    return a + b\n\n\n"
            "def main() -> None:\n    \"\"\"Giris.\"\"\"\n    print(NOT, topla(2, 3))\n\n\n"
            'if __name__ == "__main__":\n    main()\n')
    todo = main.replace("    return a + b\n", f"    # TODO {CONTENT} {FAKE_KEY}\n    return a + b\n")
    seq = iter([
        json.dumps({"thought": RAW, "action": "write", "args": {"filename": "main.py", "content": todo}}),
        json.dumps({"thought": RAW, "action": "write", "args": {"filename": "main.py", "content": main}}),
        json.dumps({"thought": RAW, "action": "run", "args": {"filename": "main.py"}}),
        json.dumps({"thought": RAW, "action": f"{RAW}_{FAKE_KEY}", "args": {}}),
        json.dumps({"thought": RAW, "action": "accept", "args": {},
                    "response": f"{RAW} {FAKE_KEY} Onay kodu: {CONFIRM}"}),
    ])

    def model(prompt: str) -> str:
        return next(seq, json.dumps({"thought": RAW, "action": "inspect", "args": {}}))

    coder = ac.AgenticCoder(model_fn=model, max_iterations=7)
    asyncio.run(coder.solve(description=description, project_path=str(project)))

    conn = ab._connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO jobs (id, description, status, result, started_at, finished_at) "
                "VALUES (?, ?, 'failed', ?, '2026-10-06T10:00:00', '2026-10-06T10:01:00')",
                ("dddd4444", description,
                 f"Durum: BAŞARISIZ\nProje klasörü: {project}\nHata: {FAKE_KEY} Onay kodu: {CONFIRM}"
                 f" {ODD_KEY}\n\n{RAW} {CONTENT} {main}"))
    finally:
        conn.close()

    state = ps.collect_panel_state()
    text = json.dumps(state, ensure_ascii=False)
    assert state["runs"] and state["jobs"], state
    for secret in (FAKE_KEY[:10], ODD_KEY, ODD_KEY[:12], CONFIRM, CONFIRM2, str(home), "/home/",
                   "belgeler", RAW, CONTENT, "topla(2, 3)"):
        assert secret not in text, f"SIZINTI: {secret!r}\n{text}"
    assert "gizli_proje" in text                         # proje adi kalir
    assert state["runs"][0]["files"][0]["name"] == "main.py"


def test_collect_survives_broken_sources(monkeypatch, db):
    def boom(*a, **k):
        raise RuntimeError(f"bozuk {FAKE_KEY}")

    monkeypatch.setattr(ac, "live_status", boom)
    monkeypatch.setattr(ab, "list_jobs", boom)
    state = ps.collect_panel_state()
    assert state["runs"] == [] and state["jobs"] == []
    assert state["errors"] == ["agentic_coder okunamadı", "agent_board okunamadı"]
    assert FAKE_KEY not in json.dumps(state)


# ── eszamanli: yazar (gercek yayim) + okuyucu (collect) ──

def test_concurrent_collect_while_publishing(tmp_path, db):
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer(n):
        try:
            while not stop.is_set():
                task = ac.CodingTask(description=f"yazar {n} {FAKE_KEY}", project_path=tmp_path / f"p{n}")
                for j in range(20):
                    task.files_written[f"f{j}.py"] = CONTENT * j
                    ac._live_publish(ac._live_snapshot(task, [], "çalışıyor", None))
        except BaseException as e:   # pragma: no cover
            errors.append(e)

    def reader():
        try:
            while not stop.is_set():
                text = json.dumps(ps.collect_panel_state(), ensure_ascii=False)
                assert FAKE_KEY[:10] not in text and CONTENT not in text
        except BaseException as e:   # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    stop.wait(1.0)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert not errors, errors
