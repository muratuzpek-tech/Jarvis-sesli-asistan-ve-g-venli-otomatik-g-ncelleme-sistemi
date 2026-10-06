"""tests/test_agentic_coder_loop.py — import denetimi ve dongu duzeltmeleri

Canli hata: main.py satir sonu olmadan bir prompt basiyor ("Enter an option: "),
import denetiminin isareti ayni satira yapisiyor, isaret bulunamiyor ve sahte
"import denemesi sonuç vermedi" hatasi SMART EXIT'i 25 tur engelliyordu.
Ayrica __main__ korumasiz `while True: input()` iceren main.py import
denemesinde calistiriliyordu.

Testler GERCEK AgenticCoder.solve dongusunu (dosya yazimi, ruff, gercek
import ve pytest alt surecleri) calistirir; yalnizca LLM yerine senaryolu bir
model_fn verilir. HOME ve proje klasoru tmp_path altindadir.
"""
import asyncio
import json
import sys

import pytest

sys.path.insert(0, '.')

from tools.developer import agentic_coder as ac

README = (
    "# Hesap Makinesi\n\n"
    "Komut satirindan dort islem yapan basit bir hesap makinesi. Toplama, cikarma, "
    "carpma ve bolme desteklenir; sifira bolme anlasilir bir hata verir.\n\n"
    "## Kullanim\n\n    python3 main.py\n\n"
    "## Test\n\n    python3 -m pytest -q\n"
)

# Canli logdaki main.py'nin aynisi: __main__ korumasi yok, prompt satir sonu
# olmadan basiliyor, sonsuz dongude input() bekleniyor.
MAIN_INTERACTIVE = (
    '"""Etkilesimli dort islem hesap makinesi."""\n'
    "OPS = {\n"
    '    "add": lambda a, b: a + b,\n'
    '    "subtract": lambda a, b: a - b,\n'
    '    "multiply": lambda a, b: a * b,\n'
    '    "divide": lambda a, b: a / b,\n'
    "}\n\n"
    "while True:\n"
    '    print("Options: add, subtract, multiply, divide, exit")\n'
    '    choice = input("Enter an option: ").strip()\n'
    '    if choice == "exit":\n'
    "        break\n"
    "    if choice in OPS:\n"
    '        a = float(input("a: "))\n'
    '        b = float(input("b: "))\n'
    '        print("Result:", OPS[choice](a, b))\n'
)

MAIN_CALC = (
    '"""Hesap makinesi giris noktasi."""\n'
    "from calculator import Calculator\n\n\n"
    "def main() -> None:\n"
    '    """Ornek hesaplari ekrana yazdirir."""\n'
    "    calc = Calculator()\n"
    '    print("2 + 3 =", calc.add(2, 3))\n'
    '    print("6 / 3 =", calc.divide(6, 3))\n\n\n'
    'if __name__ == "__main__":\n'
    "    main()\n"
)

CALCULATOR = (
    '"""Dort islem hesaplama mantigi."""\n\n\n'
    "class Calculator:\n"
    '    """Toplama, cikarma, carpma ve bolme."""\n\n'
    "    def add(self, a: float, b: float) -> float:\n"
    "        return a + b\n\n"
    "    def sub(self, a: float, b: float) -> float:\n"
    "        return a - b\n\n"
    "    def mul(self, a: float, b: float) -> float:\n"
    "        return a * b\n\n"
    "    def divide(self, a: float, b: float) -> float:\n"
    "        if b == 0:\n"
    '            raise {exc}("sifira bolme yapilamaz")\n'
    "        return a / b\n"
)

TEST_CALC = (
    '"""Hesap makinesi birim testleri."""\n'
    "import pytest\n\n"
    "from calculator import Calculator\n\n\n"
    "def test_add_and_divide():\n"
    "    calc = Calculator()\n"
    "    assert calc.add(2, 3) == 5\n"
    "    assert calc.divide(6, 3) == 2\n\n\n"
    "def test_divide_by_zero_raises():\n"
    "    with pytest.raises(ZeroDivisionError):\n"
    "        Calculator().divide(1, 0)\n"
)

INIT = '"""Test paketi."""\n'


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


ACCEPT = _decide({"action": "accept", "args": {}, "response": "bitti"})


def _solve(project, model, max_iterations=8, description="hesap makinesi yaz"):
    coder = ac.AgenticCoder(model_fn=model, max_iterations=max_iterations)
    return asyncio.run(coder.solve(description=description, project_path=str(project)))


# ── 1) isaret ayni satira yapisinca sahte import sorunu yok ──

def test_marker_glued_to_prompt_without_newline_is_found(tmp_path):
    (tmp_path / "helper.py").write_text(
        '"""Import edilince satir sonu olmadan prompt basan modul."""\n'
        'print("Enter an option: ", end="")\n'
        "VALUE = 1\n")
    problems, missing = ac._import_problems({"helper.py": ""}, tmp_path)
    assert problems == [] and missing == []


def test_glued_marker_still_reports_real_import_error(tmp_path):
    (tmp_path / "helper.py").write_text(
        'print("Enter an option: ", end="")\nimport yok_modul_glued_xyz\n')
    problems, missing = ac._import_problems({"helper.py": ""}, tmp_path)
    assert missing == ["yok_modul_glued_xyz"], problems


def test_missing_marker_is_not_a_fake_import_problem(tmp_path):
    (tmp_path / "helper.py").write_text('"""Import sirasinda sureci bitirir."""\nimport os\nos._exit(0)\n')
    problems, missing = ac._import_problems({"helper.py": ""}, tmp_path)
    assert problems == [] and missing == []


# ── 2) giris betikleri import edilmez; canli main.py ile gorev ACCEPT olur ──

def test_entry_scripts_are_not_imported(tmp_path):
    marker = tmp_path / "IMPORT_EDILDI.txt"
    body = f'from pathlib import Path\nPath({str(marker)!r}).write_text("x")\n'
    for name in ("main.py", "cli.py", "app.py"):
        (tmp_path / name).write_text(body)
    (tmp_path / "oyun.py").write_text(body + "while True:\n    input('> ')\n")
    files = {"main.py": "", "cli.py": "", "app.py": "", "oyun.py": (tmp_path / "oyun.py").read_text()}
    problems, missing = ac._import_problems(files, tmp_path)
    assert not marker.exists(), "giris betigi import edilip calistirildi"
    assert problems == [] and missing == []


def test_entry_script_imports_are_still_checked_statically(tmp_path):
    (tmp_path / "calculator.py").write_text(CALCULATOR.format(exc="ZeroDivisionError"))
    src = "from calculator import Yok\nimport kurulu_olmayan_giris_xyz\nwhile True:\n    input()\n"
    (tmp_path / "main.py").write_text(src)
    problems, missing = ac._import_problems({"main.py": src, "calculator.py": ""}, tmp_path)
    text = "\n".join(problems)
    assert "main.py" in text and "ImportError" in text and "Yok" in text
    assert missing == ["kurulu_olmayan_giris_xyz"]


def test_live_interactive_main_is_accepted(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []
    seq = iter([_write("main.py", MAIN_INTERACTIVE), _write("README.md", README)])

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, ACCEPT)

    result = _solve(project, model, max_iterations=6)

    assert not any("sonuç vermedi" in p for p in prompts)
    assert "Durum: BAŞARILI" in result and "ACCEPTED ✅" in result, result
    assert len(prompts) <= 4


# ── 3) _verify_project: stdin kapaliyken input() EOFError hata degildir ──

def _task(project, files):
    task = ac.CodingTask(description="hesap", project_path=project)
    task.files_written = dict(files)
    task.expected_files = list(files)
    return task


def test_verify_interactive_main_eof_is_not_an_error(tmp_path):
    task = _task(tmp_path, {"main.py": MAIN_INTERACTIVE, "README.md": README})
    ok, problems = ac._verify_project(task, run_pytest=False)
    assert ok, problems


def test_verify_still_reports_real_crash(tmp_path):
    crash = MAIN_INTERACTIVE.replace("while True:", 'raise RuntimeError("gercek hata")\nwhile True:')
    task = _task(tmp_path, {"main.py": crash, "README.md": README})
    ok, problems = ac._verify_project(task, run_pytest=False)
    assert not ok and any("RuntimeError" in p for p in problems), problems


# ── 4) dongude pytest geri bildirimi ──

def test_failing_pytest_is_fed_back_and_blocks_smart_exit(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []
    seq = iter([
        _write("main.py", MAIN_CALC),
        _write("calculator.py", CALCULATOR.format(exc="ValueError")),
        _write("tests/__init__.py", INIT),
        _write("tests/test_calc.py", TEST_CALC),
        _write("README.md", README),
    ])
    fixed = {"done": False}

    def model(prompt: str) -> str:
        prompts.append(prompt)
        step = next(seq, None)
        if step:
            return step
        if "1 failed" in prompt and not fixed["done"]:
            fixed["done"] = True
            return _write("calculator.py", CALCULATOR.format(exc="ZeroDivisionError"))
        return _decide({"action": "inspect", "args": {"filename": "calculator.py"}})

    result = _solve(project, model, max_iterations=10)

    fed = [p for p in prompts if "1 failed" in p and "ValueError" in p]
    assert fed, "pytest hatasi modele verilmedi"
    assert "PYTEST" in fed[0]
    assert fixed["done"] and "ACCEPTED ✅" in result, result


# ── 5) ayni dosya ayni icerikle 3 kez -> dongu, gorev biter ──

def test_same_file_same_content_three_times_stops(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return _write("main.py", MAIN_CALC)

    result = _solve(project, model, max_iterations=25)

    assert len(prompts) == 3, f"{len(prompts)} LLM cagrisi"
    assert "Durum: BAŞARISIZ" in result
    assert "döngü: main.py aynı içerikle tekrar yazılıyor, sebep:" in result
    assert "calculator" in result.split("sebep:", 1)[1]
