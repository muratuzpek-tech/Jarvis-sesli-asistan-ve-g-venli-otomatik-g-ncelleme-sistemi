"""tests/test_agentic_coder_safe_eval.py — eval/exec reddine hazir guvenli alternatif

Canli hata: kucuk model (Ollama) "=" tusu icin eval(self.display.get()) yazdi;
eval reddi 3 kez tekrarlandi, model alternatifi bilmedigi icin gorev basarisiz
bitti. Kapi dogru calisti; eksik olan, modelin kopyalayabilecegi guvenli bir
ornekti.

Testler GERCEK AgenticCoder.solve dongusunu calistirir; yalnizca LLM yerine
senaryolu bir model_fn verilir. HOME ve proje klasoru tmp_path altindadir.
"""
import asyncio
import importlib.util
import json
import re
import subprocess
import sys

import pytest

sys.path.insert(0, '.')

from tools.developer import agentic_coder as ac

NEW_MSG = ("eval kullanma. Şu güvenli değerlendiriciyi Calculator sınıfına calculate(expr) "
           "metodu olarak ekle ve main.py'de ekran metnini ona ver.")
_FENCE = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── 1-2) ornek kod kendi dogrulamasindan gecer ve dogru calisir ──

@pytest.mark.parametrize("full", [False, True])
def test_snippet_passes_validation_and_ruff(tmp_path, full):
    code = ac._safe_eval_code(full=full)
    path = tmp_path / "guvenli_hesap.py"
    path.write_text(code, encoding="utf-8")
    ok, msg = ac._validate_file(path.name, code)
    assert ok, msg
    assert ac._eval_exec_call(__import__("ast").parse(code)) is None
    if ac._ruff_cmd() is not None:
        r = subprocess.run([*ac._ruff_cmd(), "check", "--isolated", "--no-cache", str(path)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout


@pytest.mark.parametrize("full", [False, True])
def test_snippet_evaluates_arithmetic_and_rejects_everything_else(tmp_path, full):
    path = tmp_path / "guvenli_hesap.py"
    path.write_text(ac._safe_eval_code(full=full), encoding="utf-8")
    mod = _load(path)
    calc = mod.Calculator().calculate if full else mod.safe_eval
    assert calc("2+3*4") == 14
    assert calc("-(2+3) * 2") == -10
    assert calc("2 ** 3 % 5") == 3
    assert calc("7 / 2") == 3.5
    for bad in ("__import__('os')", "open('x')", "a + 1", "True + 1", "[1, 2]",
                "(lambda: 1)()", "2 +", "", "9 ** 9 ** 9"):
        with pytest.raises(ValueError):
            calc(bad)


def test_short_and_full_versions_differ():
    short, full = ac._safe_eval_code(full=False), ac._safe_eval_code(full=True)
    assert "def safe_eval" in short and "def calculate" not in short
    assert "def safe_eval" in full and "def calculate(self" in full
    assert len(full) > len(short)


def test_rejection_message_names_the_alternative():
    ok, msg = ac._validate_code("x = eval(input())\n")
    assert not ok and NEW_MSG in msg


# ── 3-4) gercek dongu: eval reddi -> ornek -> calculate() ile ACCEPT ──

README = (
    "# Hesap Makinesi\n\n"
    "Ekrandaki ifadeyi eval() KULLANMADAN hesaplayan hesap makinesi. Toplama, "
    "cikarma, carpma, bolme, us ve mod desteklenir; gecersiz ifadede 'Error' yazar.\n\n"
    "## Kullanim\n\n    python3 main.py\n\n"
    "## Mimari\n\ncalculator.py hesaplama mantigini, main.py ekran akisini icerir.\n"
)

CALC_NO_METHOD = (
    '"""Hesap makinesi cekirdegi."""\n\n\n'
    "class Calculator:\n"
    '    """Ekran metnini tutar."""\n\n'
    "    def __init__(self) -> None:\n"
    '        self.text = ""\n\n'
    "    def press(self, key: str) -> str:\n"
    '        """Tusa basinca ekran metnini gunceller."""\n'
    "        self.text += key\n"
    "        return self.text\n"
)

MAIN_EVAL = (
    '"""Hesap makinesi giris noktasi."""\n'
    "from calculator import Calculator\n\n\n"
    "def on_equals(display_text: str) -> str:\n"
    '    """"=" tusu: ekrandaki ifadeyi hesaplar."""\n'
    "    try:\n"
    "        return str(eval(display_text))\n"
    "    except Exception:\n"
    '        return "Error"\n\n\n'
    'if __name__ == "__main__":\n'
    "    calc = Calculator()\n"
    '    for k in "2+3*4":\n'
    "        calc.press(k)\n"
    '    print(on_equals(calc.text))\n'
)

MAIN_CALCULATE = MAIN_EVAL.replace(
    "        return str(eval(display_text))\n    except Exception:\n",
    "        return str(Calculator().calculate(display_text))\n    except ValueError:\n")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


def _decide(d: dict) -> str:
    return json.dumps({"thought": "t", **d})


def _write(name: str, content: str) -> str:
    return _decide({"action": "write", "args": {"filename": name, "content": content}})


def _solve(project, model, max_iterations=10):
    coder = ac.AgenticCoder(model_fn=model, max_iterations=max_iterations)
    return asyncio.run(coder.solve(description="hesap makinesi yaz", project_path=str(project)))


def test_model_copies_snippet_after_eval_rejection_and_is_accepted(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []
    state = {"calc": False, "main": False, "readme": False}
    seq = iter([_write("calculator.py", CALC_NO_METHOD), _write("main.py", MAIN_EVAL)])

    def model(prompt: str) -> str:
        prompts.append(prompt)
        step = next(seq, None)
        if step:
            return step
        blocks = _FENCE.findall(prompt)
        if NEW_MSG in prompt and blocks and not state["calc"]:
            state["calc"] = True
            # Senaryolu model ret mesajindaki ornegi Calculator'a calculate() olarak ekler.
            snippet = blocks[0]
            method = ("\n    def calculate(self, expr: str) -> float:\n"
                      '        """Ekran metnini eval() olmadan hesaplar."""\n'
                      "        return safe_eval(expr)\n")
            return _write("calculator.py", snippet + "\n\n" + CALC_NO_METHOD.split('\n\n\n', 1)[1] + method)
        if state["calc"] and not state["main"]:
            state["main"] = True
            return _write("main.py", MAIN_CALCULATE)
        if not state["readme"]:
            state["readme"] = True
            return _write("README.md", README)
        return _decide({"action": "accept", "args": {}, "response": "bitti"})

    result = _solve(project, model)

    assert any(NEW_MSG in p and "def safe_eval" in p for p in prompts), "ornek modele verilmedi"
    assert state["calc"] and state["main"]
    assert "ACCEPTED ✅" in result and "Durum: BAŞARILI" in result, result
    for name in ("main.py", "calculator.py"):
        text = (project / name).read_text()
        assert ac._eval_exec_call(__import__("ast").parse(text)) is None, name
    out = subprocess.run([sys.executable, "main.py"], cwd=project, capture_output=True, text=True)
    assert out.stdout.strip() == "14", out


def test_second_eval_rejection_shows_full_example(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        # her turda biraz farkli ama hep eval
        return _write("main.py", MAIN_EVAL.replace("Hesap makinesi", f"Hesap makinesi {len(prompts)}"))

    _solve(project, model, max_iterations=4)

    assert len(prompts) == 3      # 3. ret ayni dosya/kategori -> dongu, gorev durur
    first, second = prompts[1], prompts[2]
    assert NEW_MSG in first and "def safe_eval" in first and "def calculate(self" not in first
    assert NEW_MSG in second and "def safe_eval" in second and "def calculate(self" in second
