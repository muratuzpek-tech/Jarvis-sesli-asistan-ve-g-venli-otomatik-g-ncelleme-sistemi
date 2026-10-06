"""tests/test_agentic_coder_filename.py — write/fix dosya adi dogrulamasi

Canli hata: model 255 bayttan uzun bir dosya adi verince solve()
"OSError: File name too long" ile coktu. Dosya adi diske dokunmadan once
dogrulanmali (en fazla 100 karakter, '..' / mutlak yol yok); uygunsuzsa
cokme yerine modele gorunur bir ret mesaji gider ve ret sayacina sayilir.

Testler GERCEK AgenticCoder.solve dongusunu calistirir; yalnizca LLM yerine
senaryolu bir model_fn verilir. HOME ve proje klasoru tmp_path altindadir.
"""
import asyncio
import json
import sys

import pytest

sys.path.insert(0, '.')

from tools.developer import agentic_coder as ac


class _UI:
    def __init__(self):
        self.lines: list[str] = []

    def write_log(self, msg: str) -> None:
        self.lines.append(msg)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _decide(d: dict) -> str:
    return json.dumps({"thought": "t", **d})


def _body(i: int) -> str:
    return (
        f'"""Hesap makinesi modulu, surum {i}."""\n\n\n'
        f"def topla_{i}(a: float, b: float) -> float:\n"
        '    """Iki sayiyi toplar ve sonucu dondurur."""\n'
        "    return a + b\n\n\n"
        "def main() -> None:\n"
        '    """Giris noktasi: ornek bir toplama yazdirir."""\n'
        f'    print("sonuc:", topla_{i}(2, 3))\n\n\n'
        'if __name__ == "__main__":\n'
        "    main()\n"
    )


def _solve(project, model, ui=None, max_iterations=4):
    coder = ac.AgenticCoder(model_fn=model, ui=ui, max_iterations=max_iterations)
    return asyncio.run(coder.solve(description="hesap makinesi yaz", project_path=str(project)))


def _scripted(first: list[str], prompts: list[str]):
    seq = iter(first)

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, _decide({"action": "inspect", "args": {}}))
    return model


LONG_300 = "a" * 297 + ".py"     # 255 bayt sinirini asar -> eskiden OSError
LONG_150 = "b" * 147 + ".py"     # OS kabul eder ama 100 karakter sinirini asar


@pytest.mark.parametrize("action", ["write", "fix"])
@pytest.mark.parametrize("name", [LONG_300, LONG_150, "pkg/" + "c" * 260 + ".py"],
                         ids=["300", "150", "nested-264"])
def test_too_long_filename_is_rejected_not_crash(home, action, name):
    project = home / "jarvis_programs" / "hesap"
    ui = _UI()
    prompts: list[str] = []
    model = _scripted([_decide({"action": action, "args": {"filename": name, "content": _body(1)}})],
                      prompts)

    result = _solve(project, model, ui=ui, max_iterations=2)   # cokmemeli

    assert isinstance(result, str)
    shown = [ln for ln in ui.lines if "⛔" in ln and "reddedildi:" in ln]
    assert shown, ui.lines
    assert "100" in shown[0]
    assert len(shown[0]) < 300, "ekrandaki ret satiri dev dosya adini tasimamali"
    # Ikinci turda model ret sebebini gormeli.
    assert len(prompts) == 2
    assert "FILENAME_REJECTED" in prompts[1] and "100" in prompts[1]
    written = [p for p in project.rglob("*") if p.is_file() and p.suffix == ".py"]
    assert written == [], written


def test_repeated_long_filename_counts_toward_reject_limit(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        # Icerik her turda farkli: "ayni icerik" sayacina takilmasin.
        return _decide({"action": "write",
                        "args": {"filename": LONG_300, "content": _body(len(prompts))}})

    result = _solve(project, model, max_iterations=10)

    assert len(prompts) == 3, f"{len(prompts)} LLM cagrisi (kota yakildi)"
    assert "Durum: BAŞARISIZ" in result
    assert "3 kez reddedildi" in result
    assert LONG_300 not in result


@pytest.mark.parametrize("name", ["../disari.py", "a/../../disari.py", "/tmp/abs.py", "a\x00b.py"],
                         ids=["dotdot", "nested-dotdot", "absolute", "nul"])
def test_escape_names_still_rejected(home, name):
    project = home / "jarvis_programs" / "hesap"
    ui = _UI()
    prompts: list[str] = []
    model = _scripted([_decide({"action": "write", "args": {"filename": name, "content": _body(1)}})],
                      prompts)

    _solve(project, model, ui=ui, max_iterations=2)

    assert any("⛔" in ln and "reddedildi:" in ln for ln in ui.lines), ui.lines
    assert any("REJECTED" in p for p in prompts[1:])
    assert not (home / "jarvis_programs" / "disari.py").exists()


def test_filename_at_limit_is_accepted(home):
    project = home / "jarvis_programs" / "hesap"
    name = "d" * 97 + ".py"
    assert len(name) == 100
    ui = _UI()
    model = _scripted([_decide({"action": "write", "args": {"filename": name, "content": _body(1)}})], [])

    _solve(project, model, ui=ui, max_iterations=2)

    assert (project / name).is_file()
    assert not any("⛔" in ln for ln in ui.lines), ui.lines


@pytest.mark.parametrize("name,ok", [
    ("main.py", True),
    ("pkg/mod.py", True),
    ("x" * 97 + ".py", True),
    ("x" * 98 + ".py", False),
    ("../x.py", False),
    ("/etc/passwd", False),
    ("", False),
    ("a\x00b", False),
    (123, False),
], ids=["main", "nested", "len100", "len101", "dotdot", "absolute", "empty", "nul", "non-str"])
def test_filename_problem_unit(name, ok):
    assert (ac._filename_problem(name) is None) is ok
