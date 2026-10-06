"""tests/test_agentic_coder_imports.py — agentic_code hesap makinesi hatasi

Canli hata: "hesap makinesi yaz" isteginde dosya adi yok -> plan yalnizca
[main.py, README.md]. Model main.py'de "from calculator import Calculator"
yaziyor ama calculator.py planda olmadigi icin hic yazilmiyor; SMART EXIT
yalnizca compile() yapip "temiz" diyor; _verify_project GUI projesinde
"main.py --help" ile programi (pencereyi) aciyor; ModuleNotFoundError'da
25 turun hepsi (kota) harcaniyor.

Testler GERCEK AgenticCoder.solve dongusunu, gercek dosya yazimini, ruff'i
ve gercek import denemesini (subprocess) calistirir. Yalnizca LLM yerine
senaryolu bir model_fn verilir. HOME ve proje klasoru tmp_path altindadir.
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
    "## Mimari\n\nmain.py giris noktasidir, hesaplama mantigi calculator.py icindedir.\n"
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
    '            raise ZeroDivisionError("sifira bolme yapilamaz")\n'
    "        return a / b\n"
)


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


def _solve(project, model, max_iterations=12, description="hesap makinesi yaz"):
    coder = ac.AgenticCoder(model_fn=model, max_iterations=max_iterations)
    return asyncio.run(coder.solve(description=description, project_path=str(project)))


# ── 1) yerel import -> eksik dosya planda, yazilana kadar SMART EXIT/accept yok ──

def test_missing_local_modules_from_ast(tmp_path):
    (tmp_path / "helpers.py").write_text("X = 1\n")
    files = {
        "main.py": (
            "import os\nimport json.decoder\nimport helpers\nimport pytest\n"
            "from calculator import Calculator\nfrom storage.db import save\n"
            "from . import sibling\n"
            "try:\n    import yaml_opsiyonel\nexcept ImportError:\n    yaml_opsiyonel = None\n"
        ),
    }
    assert ac._missing_local_modules(files, tmp_path) == ["calculator.py", "storage/db.py", "sibling.py"]


def test_calculator_module_is_planned_and_written_before_exit(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []
    done: set[str] = set()

    def model(prompt: str) -> str:
        prompts.append(prompt)
        for name, content in (("main.py", MAIN_CALC), ("README.md", README)):
            if name not in done:
                done.add(name)
                return _write(name, content)
        if "calculator.py" in prompt and "calculator.py" not in done:
            done.add("calculator.py")
            return _write("calculator.py", CALCULATOR)
        return _decide({"action": "accept", "args": {}, "response": "bitti"})

    result = _solve(project, model)

    assert (project / "calculator.py").is_file(), result
    assert any("calculator.py eksik" in p for p in prompts)
    assert "ACCEPTED ✅" in result and "NOT ACCEPTED" not in result


def test_accept_is_rejected_while_local_module_missing(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []
    seq = iter([_write("main.py", MAIN_CALC), _write("README.md", README)])

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, _decide({"action": "accept", "args": {}, "response": "bitti"}))

    result = _solve(project, model, max_iterations=4)

    assert "NOT ACCEPTED" in result
    assert any("ACCEPT_REDDEDILDI" in p and "calculator.py" in p for p in prompts)


# ── 2) SMART EXIT oncesi gercek import denemesi; hata modele geri verilir ──

MAIN_BAD_SUBMODULE = (
    '"""Var olmayan bir alt modulu kullanan giris noktasi."""\n'
    "import os.yok_alt_modul\n\n\n"
    "def main() -> None:\n"
    '    """Alt moduldeki degeri yazdirir ve calisma dizinini gosterir."""\n'
    "    print(os.yok_alt_modul.DEGER, os.getcwd())\n\n\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
)


def test_smart_exit_runs_real_import_and_feeds_error_back(home):
    project = home / "jarvis_programs" / "alt"
    prompts: list[str] = []
    seq = iter([_write("main.py", MAIN_BAD_SUBMODULE), _write("README.md", README)])

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, _decide({"action": "inspect", "args": {"filename": "main.py"}}))

    result = _solve(project, model, max_iterations=4)

    assert "NOT ACCEPTED" in result
    fed_back = [p for p in prompts[2:] if "ModuleNotFoundError" in p and "os.yok_alt_modul" in p]
    assert fed_back, "import hatasi modele geri verilmedi"


def test_import_check_reports_import_error_and_opens_no_window(tmp_path):
    (tmp_path / "calculator.py").write_text(CALCULATOR)
    (tmp_path / "main.py").write_text("from calculator import Yok\n")
    (tmp_path / "gui.py").write_text(
        "import tkinter as tk\nroot = tk.Tk()\nroot.mainloop()\n")
    problems, missing = ac._import_problems(
        {"main.py": "", "calculator.py": "", "gui.py": ""}, tmp_path, timeout=20)
    text = "\n".join(problems)
    assert "main.py" in text and "ImportError" in text and "Yok" in text
    assert "gui.py" not in text          # DISPLAY yok -> pencere yok, hata da sayilmaz
    assert missing == []


# ── 3) GUI projesinde --help ile program calistirilmaz ──

GUI_MAIN = (
    '"""Tkinter hesap makinesi arayuzu."""\n'
    "import sys\n"
    "import tkinter as tk\n"
    "from pathlib import Path\n\n\n"
    "def build(root: tk.Tk) -> tk.Entry:\n"
    '    """Giris kutusunu olusturur ve dondurur."""\n'
    "    entry = tk.Entry(root)\n"
    "    entry.pack()\n"
    "    return entry\n\n\n"
    'if __name__ == "__main__":\n'
    '    Path("PROGRAM_CALISTI.txt").write_text("pencere acilacakti")\n'
    "    sys.exit(0)\n"
)


def _task(project, files):
    task = ac.CodingTask(description="gui", project_path=project)
    task.files_written = dict(files)
    task.expected_files = list(files)
    return task


def test_verify_gui_project_does_not_run_program(tmp_path):
    task = _task(tmp_path, {"main.py": GUI_MAIN, "README.md": README})
    ok, problems = ac._verify_project(task, run_pytest=False)
    assert not (tmp_path / "PROGRAM_CALISTI.txt").exists()
    assert ok, problems


def test_verify_gui_project_still_catches_missing_module(tmp_path):
    gui = GUI_MAIN.replace("from pathlib import Path\n", "from pathlib import Path\nfrom widgets import Panel\n")
    task = _task(tmp_path, {"main.py": gui, "README.md": README})
    ok, problems = ac._verify_project(task, run_pytest=False)
    assert not ok
    assert any("widgets" in p for p in problems), problems
    assert not (tmp_path / "PROGRAM_CALISTI.txt").exists()


# ── 4) ayni ModuleNotFoundError 3 tur -> basarisiz, kota yakilmaz ──

MAIN_EXTERNAL = (
    '"""Kurulu olmayan bir pakete dayanan giris noktasi."""\n'
    "import kurulu_olmayan_paket_xyz\n\n\n"
    "def main() -> None:\n"
    '    """Paketten gelen degeri ekrana yazdirir ve cikar."""\n'
    "    print(kurulu_olmayan_paket_xyz.surum())\n\n\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
)


def test_same_module_not_found_three_turns_stops_task(home):
    project = home / "jarvis_programs" / "dis"
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        if len(prompts) == 1:
            return _write("main.py", MAIN_EXTERNAL)
        return _decide({"action": "run", "args": {"filename": "main.py"}})

    result = _solve(project, model, max_iterations=25)

    assert len(prompts) <= 5, f"{len(prompts)} LLM cagrisi (kota yakildi)"
    assert "Durum: BAŞARISIZ" in result
    assert "Eksik modül: kurulu_olmayan_paket_xyz" in result


# ── 5) sonuc metni: proje klasoru + durum (+ eksik modul) ──

def test_success_result_names_folder_and_status(home):
    project = home / "jarvis_programs" / "hesap"
    seq = iter([_write("main.py", MAIN_CALC), _write("calculator.py", CALCULATOR),
                _write("README.md", README)])

    def model(prompt: str) -> str:
        return next(seq, _decide({"action": "accept", "args": {}, "response": "bitti"}))

    result = _solve(project, model)
    head = result[:600]
    assert "Durum: BAŞARILI" in head
    assert f"Proje klasörü: {project.resolve()}" in head
    assert "Eksik modül" not in result


def test_failure_result_names_folder_status_and_module(home):
    project = home / "jarvis_programs" / "hesap"
    seq = iter([_write("main.py", MAIN_CALC), _write("README.md", README)])

    def model(prompt: str) -> str:
        return next(seq, _decide({"action": "inspect", "args": {}}))

    result = _solve(project, model, max_iterations=4)
    head = result[:600]
    assert "Durum: BAŞARISIZ" in head
    assert f"Proje klasörü: {project.resolve()}" in head
    assert "Eksik modül: calculator" in head
    assert "NOT ACCEPTED" in result      # main.py'deki tekrar-deneme engeli bunu okur


def test_llm_unavailable_result_still_names_folder(home):
    project = home / "jarvis_programs" / "llmsiz"

    def model(prompt: str) -> str:
        return _decide({"action": "error", "args": {}, "response": "HATA: LLM yok"})

    orig_sleep = asyncio.sleep

    async def no_sleep(_s):
        await orig_sleep(0)

    ac.asyncio.sleep = no_sleep
    try:
        result = _solve(project, model, max_iterations=5)
    finally:
        ac.asyncio.sleep = orig_sleep
    assert result.startswith("❌")
    assert "Durum: BAŞARISIZ" in result and f"Proje klasörü: {project.resolve()}" in result
