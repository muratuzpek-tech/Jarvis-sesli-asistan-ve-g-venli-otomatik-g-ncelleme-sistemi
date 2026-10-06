"""tests/test_agentic_coder_live.py — dev agent panosu icin canli durum kaydi

agentic_coder'in durumu solve() icinde yerel bir CodingTask'ta tutuluyordu;
dis thread'den okunamiyordu ve kilitsiz degisiyordu. Bu testler modul
duzeyindeki kilitli _LIVE kaydini ve live_status()'u dogrular:

- tur basinda / eylem sonunda DEGISMEZ ozet yayimlanir, live_status() kopya verir
- ozet yalnizca dosya ADI + boyutu tasir; icerik, ham model ciktisi yok
- son kullanilan model (gemini/ollama + ad) saklanir
- pano hatasi solve()'u ASLA bozmaz
- eszamanli yazma/okuma "dictionary changed size" uretmez

Testler GERCEK AgenticCoder.solve dongusunu senaryolu model_fn ile calistirir.
HOME tmp_path altindadir (conftest izolasyonu + home fixture).
"""
import asyncio
import json
import sys
import threading
import types

import pytest

sys.path.insert(0, '.')

from tools.developer import agentic_coder as ac


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


def _decide(d: dict) -> str:
    return json.dumps({"thought": "t", **d})


def _write(name: str, content: str) -> str:
    return _decide({"action": "write", "args": {"filename": name, "content": content}})


MAIN = '''"""Hesap makinesi: iki sayiyi toplar ve sonucu yazdirir."""


def topla(a: float, b: float) -> float:
    """Iki sayiyi toplar."""
    return a + b


def main() -> None:
    """Giris noktasi: ornek bir toplama yazdirir."""
    print("sonuc:", topla(2, 3))


if __name__ == "__main__":
    main()
'''

README = (
    "# Hesap makinesi\n\nBu proje iki sayiyi toplayan kucuk bir hesap makinesidir. "
    "topla fonksiyonu iki sayiyi alir ve toplamini dondurur; main ornek bir "
    "toplama yazdirir.\n\n## Calistirma\n\n```bash\npython3 main.py\n```\n"
)

_PRIMITIVES = (str, int, float, bool, type(None))


def _only_plain_data(obj) -> bool:
    if isinstance(obj, _PRIMITIVES):
        return True
    if isinstance(obj, (list, tuple)):
        return all(_only_plain_data(x) for x in obj)
    if isinstance(obj, dict):
        return all(isinstance(k, str) and _only_plain_data(v) for k, v in obj.items())
    return False


# ── 1) gercek solve: tur basinda ozet gorunur, sonda durum "bitti" ──

def test_live_status_tracks_real_solve(home):
    project = home / "jarvis_programs" / "hesap"
    seen: list[list[dict]] = []
    seq = iter([_write("main.py", MAIN), _write("README.md", README),
                _decide({"action": "accept", "args": {}, "response": "bitti"})])

    def model(prompt: str) -> str:
        seen.append(ac.live_status())          # model cagrisi = tur basi yayimlandi
        return next(seq, _decide({"action": "inspect", "args": {}}))

    coder = ac.AgenticCoder(model_fn=model, max_iterations=6)
    result = asyncio.run(coder.solve(description="hesap makinesi yaz", project_path=str(project)))
    assert "Durum: BAŞARILI" in result, result

    first = seen[0]
    assert len(first) == 1
    run = first[0]
    assert run["status"] == "çalışıyor"
    assert run["iteration"] == 1 and run["max_iterations"] == 6
    assert run["description"] == "hesap makinesi yaz"
    assert run["project"] == "hesap"
    assert run["files"] == []
    assert run["model"] == {"provider": "özel", "name": ""}

    second = seen[1][0]
    assert second["iteration"] == 2
    assert second["task_id"] == run["task_id"]
    assert [f["name"] for f in second["files"]] == ["main.py"]
    assert second["files"][0]["size"] > 100
    assert any("📝 main.py" in e for e in second["events"])

    final = ac.live_status()
    assert len(final) == 1 and final[0]["status"] == "bitti"
    assert [f["name"] for f in final[0]["files"]] == ["main.py", "README.md"]
    assert set(final[0]["rejects"]) == {"reject", "eval", "lock", "method", "ruff"}


def test_failed_solve_is_marked_failed_with_reject_counts(home):
    project = home / "jarvis_programs" / "hesap"

    calls: list[str] = []

    def model(prompt: str) -> str:
        calls.append(prompt)                        # her turda farkli ama hep cok kisa
        return _write("main.py", f"print({len(calls)})\n")

    coder = ac.AgenticCoder(model_fn=model, max_iterations=10)
    result = asyncio.run(coder.solve(description="hesap makinesi yaz", project_path=str(project)))
    assert "Durum: BAŞARISIZ" in result

    run = ac.live_status()[0]
    assert run["status"] == "başarısız"
    assert run["rejects"]["reject"] == 3
    assert run["reason"] == "döngü"
    assert any("REDDEDİLDİ (COK_KISA)" in e for e in run["events"])


def test_exception_inside_solve_marks_run_failed(home, monkeypatch):
    project = home / "jarvis_programs" / "hesap"

    def model(prompt: str) -> str:
        raise RuntimeError("model coktu")

    coder = ac.AgenticCoder(model_fn=model, max_iterations=3)
    with pytest.raises(RuntimeError):
        asyncio.run(coder.solve(description="hesap makinesi yaz", project_path=str(project)))
    assert ac.live_status()[0]["status"] == "başarısız"


# ── 2) ozet: yalnizca ad + boyut; icerik / ham model ciktisi / ret alintisi yok ──

def test_snapshot_never_contains_content_or_raw_model_output(home):
    project = home / "jarvis_programs" / "hesap"
    content_marker = "DOSYA_ICERIGI_7f3a"
    raw_marker = "HAM_MODEL_CIKTISI_91bd"
    todo_main = MAIN.replace("    return a + b\n", f"    # TODO {content_marker}\n    return a + b\n")
    seq = iter([
        json.dumps({"thought": raw_marker, "action": "write",
                    "args": {"filename": "main.py", "content": todo_main}}),   # ret: TODO alintisi
        json.dumps({"thought": raw_marker, "action": "write",
                    "args": {"filename": "main.py", "content": MAIN + f"# {content_marker}\n"}}),
        json.dumps({"thought": raw_marker, "action": "run", "args": {"filename": "main.py"}}),
        json.dumps({"thought": raw_marker, "action": "accept", "args": {},
                    "response": f"{raw_marker} tamam"}),
    ])

    def model(prompt: str) -> str:
        return next(seq, _decide({"action": "inspect", "args": {}}))

    coder = ac.AgenticCoder(model_fn=model, max_iterations=6)
    asyncio.run(coder.solve(description="hesap makinesi yaz", project_path=str(project)))

    status = ac.live_status()
    dumped = json.dumps(status, ensure_ascii=False)
    assert content_marker not in dumped
    assert raw_marker not in dumped
    assert "sonuc:" not in dumped                     # program (run) ciktisi
    assert _only_plain_data(status)
    assert not any(isinstance(v, ac.CodingTask) for run in status for v in run.values())


def test_description_and_events_are_bounded(home):
    project = home / "jarvis_programs" / "hesap"
    long_name = "x" * 200 + ".py"     # 255 bayt ustu ad solve()'u OSError ile cokertir (ayri hata)
    seq = iter([_decide({"action": "nonsense_" + "y" * 400, "args": {}}),
                _write(long_name, MAIN)])

    def model(prompt: str) -> str:
        return next(seq, _decide({"action": "inspect", "args": {}}))

    coder = ac.AgenticCoder(model_fn=model, max_iterations=30)
    asyncio.run(coder.solve(description="hesap makinesi yaz " + "z" * 500,
                            project_path=str(project)))
    run = ac.live_status()[0]
    assert len(run["description"]) <= 80
    assert 0 < len(run["events"]) <= 20
    assert all(len(e) <= 120 for e in run["events"])
    assert all(len(f["name"]) <= 120 for f in run["files"])


# ── 3) live_status kopya verir; ozet degismez ──

def test_live_status_returns_copies_and_snapshot_is_immutable(home):
    task = ac.CodingTask(description="kopya testi", project_path=home / "p")
    task.files_written["a.py"] = "print('a')\n"
    ac._live_publish(ac._live_snapshot(task, [], "çalışıyor", None))

    first = ac.live_status()
    first[0]["files"].append({"name": "sahte.py", "size": 1})
    first[0]["rejects"]["reject"] = 99
    first[0]["status"] = "bitti"
    task.files_written["b.py"] = "print('b')\n"       # canli nesne degisir, ozet degismez

    again = ac.live_status()[0]
    assert [f["name"] for f in again["files"]] == ["a.py"]
    assert again["rejects"]["reject"] == 0
    assert again["status"] == "çalışıyor"
    stored = ac._LIVE[task.run_id]
    with pytest.raises(TypeError):
        stored["status"] = "bitti"                      # type: ignore[index]


def test_live_registry_is_bounded():
    for n in range(ac._LIVE_MAX_RUNS + 5):
        t = ac.CodingTask(description=f"gorev {n}")
        ac._live_publish(ac._live_snapshot(t, [], "bitti", None))
    assert len(ac.live_status()) == ac._LIVE_MAX_RUNS


# ── 4) pano hatasi solve()'u bozmaz ──

def test_publish_failure_never_breaks_solve(home, monkeypatch):
    project = home / "jarvis_programs" / "hesap"

    def boom(*a, **k):
        raise RuntimeError("pano bozuk")

    monkeypatch.setattr(ac, "_live_snapshot", boom)
    seq = iter([_write("main.py", MAIN), _write("README.md", README),
                _decide({"action": "accept", "args": {}, "response": "bitti"})])

    def model(prompt: str) -> str:
        return next(seq, _decide({"action": "inspect", "args": {}}))

    coder = ac.AgenticCoder(model_fn=model, max_iterations=6)
    result = asyncio.run(coder.solve(description="hesap makinesi yaz", project_path=str(project)))
    assert "Durum: BAŞARILI" in result
    assert ac.live_status() == []


# ── 5) son kullanilan model ──

def test_last_model_records_ollama(monkeypatch):
    monkeypatch.setattr(ac, "_gemini_api_key", lambda: "")
    monkeypatch.setenv("OLLAMA_CODER_MODEL", "qwen-test:1b")

    class _RespErr(Exception):
        pass

    fake = types.SimpleNamespace(
        ResponseError=_RespErr,
        chat=lambda **kw: {"message": {"content": '{"action": "inspect"}'}},
    )
    monkeypatch.setitem(sys.modules, "ollama", fake)
    out = ac.AgenticCoder._default_model("merhaba")
    assert out == '{"action": "inspect"}'
    assert ac.last_model() == {"provider": "ollama", "name": "qwen-test:1b"}


def test_last_model_records_gemini(monkeypatch):
    monkeypatch.setattr(ac, "_gemini_api_key", lambda: "sahte-anahtar")
    monkeypatch.setattr(ac, "_gemini_disabled_until", 0.0)

    class _Models:
        @staticmethod
        def generate_content(**kw):
            return types.SimpleNamespace(text='{"action": "accept"}')

    class _Client:
        def __init__(self, api_key):
            self.models = _Models()

    fake_types = types.SimpleNamespace(GenerateContentConfig=lambda **kw: kw,
                                       AutomaticFunctionCallingConfig=lambda **kw: kw)
    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = _Client
    fake_genai.types = fake_types
    try:
        import google
    except ImportError:
        google = types.ModuleType("google")
        monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setattr(google, "genai", fake_genai, raising=False)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

    assert ac.AgenticCoder._default_model("merhaba") == '{"action": "accept"}'
    assert ac.last_model() == {"provider": "gemini", "name": ac._GEMINI_CODER_MODEL}


def test_default_model_run_reports_last_model(home, monkeypatch):
    project = home / "jarvis_programs" / "hesap"
    monkeypatch.setattr(ac, "_last_model", ("ollama", "qwen-test:1b"))
    seen: list[dict] = []
    seq = iter([_decide({"action": "inspect", "args": {}})])

    def fake_default(prompt: str) -> str:
        seen.append(ac.live_status()[0]["model"])
        return next(seq, _decide({"action": "inspect", "args": {}}))

    monkeypatch.setattr(ac.AgenticCoder, "_default_model", staticmethod(fake_default))
    coder = ac.AgenticCoder(max_iterations=1)
    asyncio.run(coder.solve(description="hesap makinesi yaz", project_path=str(project)))
    assert seen == [{"provider": "ollama", "name": "qwen-test:1b"}]


# ── 6) eszamanli yazma / okuma ──

def test_concurrent_publish_and_read_is_safe(tmp_path):
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer(n: int) -> None:
        try:
            k = 0
            while not stop.is_set():
                task = ac.CodingTask(description=f"yazar {n}", project_path=tmp_path)
                steps = []
                for j in range(30):
                    task.files_written[f"f{j}.py"] = "x" * j
                    task.iterations = j
                    steps.append(ac.CodingStep(step_num=j, action="write", detail=f"📝 f{j}.py", success=True))
                    ac._live_publish(ac._live_snapshot(task, steps, "çalışıyor", None))
                ac._live_publish(ac._live_snapshot(task, steps, "bitti", None))
                k += 1
        except BaseException as e:   # pragma: no cover - test basarisizliginda raporlanir
            errors.append(e)

    def reader() -> None:
        try:
            while not stop.is_set():
                for run in ac.live_status():
                    json.dumps(run, ensure_ascii=False)
                    run["files"].clear()            # kopya: kayit etkilenmez
        except BaseException as e:   # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    stop.wait(1.0)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert not errors, errors
    assert len(ac.live_status()) <= ac._LIVE_MAX_RUNS
