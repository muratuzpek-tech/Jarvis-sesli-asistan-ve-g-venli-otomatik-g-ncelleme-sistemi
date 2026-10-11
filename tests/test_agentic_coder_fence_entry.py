"""tests/test_agentic_coder_fence_entry.py — markdown citi + eksik giris dosyasi

Canli hata 1: model dosya icerigini ```python ... ``` citleri icinde verdi;
citler dosyaya girdi, kod aslinda gecerli oldugu halde SyntaxError ile
reddedildi (_reddedilen_calculator.py.txt ```python ile basliyordu).

Canli hata 2: Jarvis gorevi yeniden cagirdiginda aciklama "Tkinter ile Windows
benzeri hesap makinesi ... calculator.py" oldu; plan [calculator.py, README.md]
cikti, main.py hic beklenmedi. Model GUI'siz bir calculator.py + README yazdi,
SMART EXIT "tum dosyalar temiz" deyip gorevi BASARILI bitirdi; calistirilacak
bir pencere yoktu.

Testler GERCEK AgenticCoder.solve dongusunu senaryolu model_fn ile calistirir.
HOME ve proje klasoru tmp_path altindadir.
"""
import ast
import asyncio
import json
import sys

import pytest

sys.path.insert(0, '.')

from tools.developer import agentic_coder as ac


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
INSPECT = _decide({"action": "inspect", "args": {}})


def _scenario(*answers, prompts=None):
    seq = iter(answers)

    def model(prompt: str) -> str:
        if prompts is not None:
            prompts.append(prompt)
        return next(seq, ACCEPT)
    return model


def _solve(model, description, project, max_iterations=6, target_filename=None):
    coder = ac.AgenticCoder(model_fn=model, max_iterations=max_iterations)
    return asyncio.run(coder.solve(description=description, project_path=str(project),
                                   target_filename=target_filename))


def _result_block(result: str) -> str:
    return result.split("\n\n", 1)[0]


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

# Canli calculator.py'nin ozu: guvenli degerlendirici + Calculator, GUI YOK.
CALCULATOR = ac._safe_eval_code(full=True)

GUI_MAIN = '''"""Tkinter arayuzu: tus takimi ve ekran; hesabi Calculator yapar."""
import tkinter as tk

from calculator import Calculator


class App:
    """Windows benzeri hesap makinesi penceresi."""

    def __init__(self, root: tk.Tk) -> None:
        self.calc = Calculator()
        self.display = tk.Entry(root, width=24, justify="right")
        self.display.grid(row=0, column=0, columnspan=4)
        for i, key in enumerate("789/456*123-0.=+"):
            tk.Button(root, text=key, width=5,
                      command=lambda k=key: self.press(k)).grid(row=1 + i // 4, column=i % 4)

    def press(self, key: str) -> None:
        """Tusa basildiginda ekrani gunceller ya da hesaplar."""
        if key != "=":
            self.display.insert(tk.END, key)
            return
        try:
            result = self.calc.calculate(self.display.get())
        except (ValueError, ZeroDivisionError):
            result = "Error"
        self.display.delete(0, tk.END)
        self.display.insert(0, str(result))


if __name__ == "__main__":
    window = tk.Tk()
    App(window)
    window.mainloop()
'''

GUI_README = (
    "# Windows benzeri hesap makinesi\n\nTkinter ile yazilmis, tus takimli bir hesap "
    "makinesi. calculator.py hesaplamayi eval kullanmadan yapar; arayuz giris dosyasi "
    "pencereyi acar ve tuslari Calculator sinifina baglar.\n\n## Calistirma\n\n"
    "```bash\npython3 main.py\n```\n"
)

GUI_DESC = ("Tkinter ile Windows benzeri hesap makinesi yaz; calculator.py dosyasında "
            "Calculator sınıfı olsun, tuş takımı olsun")


# ── Hata 1: markdown citi ──────────────────────────────────────

def test_fenced_valid_code_is_accepted_and_written_without_fence(home):
    project = home / "jarvis_programs" / "hesap"
    model = _scenario(_write("main.py", "```python\n" + MAIN + "```"),
                      _write("README.md", README), ACCEPT)
    result = _solve(model, "hesap makinesi yaz", project)

    assert "Durum: BAŞARILI" in result, result
    on_disk = (project / "main.py").read_text(encoding="utf-8")
    assert "```" not in on_disk
    assert on_disk.startswith('"""Hesap makinesi')
    ast.parse(on_disk)
    assert not (project / "_reddedilen_main.py.txt").exists()


def test_fence_with_trailing_whitespace_and_bare_language_is_stripped(home):
    project = home / "jarvis_programs" / "hesap"
    model = _scenario(_write("main.py", "\n```py\n" + MAIN + "```\n\n"),
                      _write("README.md", README), ACCEPT)
    result = _solve(model, "hesap makinesi yaz", project)

    assert "Durum: BAŞARILI" in result, result
    assert "```" not in (project / "main.py").read_text(encoding="utf-8")


def test_inner_fence_in_docstring_is_not_touched(home):
    project = home / "jarvis_programs" / "hesap"
    doc = '"""Hesap makinesi.\n\nKullanim:\n\n```bash\npython3 main.py\n```\n"""\n'
    body = MAIN.split("\n", 1)[1]           # MAIN'in kendi docstring'i yerine
    code = doc + body
    model = _scenario(_write("main.py", "```python\n" + code + "```"),
                      _write("README.md", README), ACCEPT)
    result = _solve(model, "hesap makinesi yaz", project)

    assert "Durum: BAŞARILI" in result, result
    on_disk = (project / "main.py").read_text(encoding="utf-8")
    assert ast.get_docstring(ast.parse(on_disk)) == ast.get_docstring(ast.parse(code))
    assert on_disk.count("```") == 2


def test_unfenced_code_with_inner_fence_is_written_unchanged(home):
    project = home / "jarvis_programs" / "hesap"
    # Basta/sonda cit yok; ortadaki ``` docstring'e ait, dokunulmamali.
    code = '"""Kullanim:\n\n```\npython3 main.py\n```\n"""\n' + MAIN.split("\n", 1)[1]
    model = _scenario(_write("main.py", code), _write("README.md", README), ACCEPT)
    result = _solve(model, "hesap makinesi yaz", project)

    assert "Durum: BAŞARILI" in result, result
    on_disk = (project / "main.py").read_text(encoding="utf-8")
    assert ast.get_docstring(ast.parse(on_disk)) == ast.get_docstring(ast.parse(code))


def test_fenced_but_really_broken_code_is_still_rejected(home):
    project = home / "jarvis_programs" / "hesap"
    broken = "```python\n" + MAIN.replace("def topla(a: float, b: float)", "def topla(a: float, b: float") + "```"
    model = _scenario(_write("main.py", broken), _write("main.py", broken), _write("main.py", broken))
    result = _solve(model, "hesap makinesi yaz", project, max_iterations=4)

    assert "Durum: BAŞARISIZ" in result
    assert not (project / "main.py").exists()
    rejected = project / "_reddedilen_main.py.txt"
    assert rejected.is_file()
    assert "SYNTAX_ERROR" in result, result
    # Ret, citten degil gercek hatadan: kopyada cit yok, bozuk satir var.
    saved = rejected.read_text(encoding="utf-8")
    assert "```" not in saved
    assert "def topla(a: float, b: float -> float:" in saved


# ── Hata 2: GUI isteginde giris dosyasi ────────────────────────

@pytest.mark.parametrize("description,target", [
    (GUI_DESC, None),
    ("Windows benzeri hesap makinesi arayüzü yaz, tuş takımı olsun", "calculator.py"),
])
def test_gui_task_without_entry_file_is_not_finished(home, description, target):
    project = home / "jarvis_programs" / "tk_hesap"
    prompts: list[str] = []
    model = _scenario(_write("calculator.py", CALCULATOR), _write("README.md", GUI_README),
                      ACCEPT, ACCEPT, ACCEPT, prompts=prompts)
    result = _solve(model, description, project, max_iterations=5, target_filename=target)

    block = _result_block(result)
    assert "Durum: BAŞARISIZ" in block, result
    assert "Eksik dosya: main.py" in block, result
    assert "çalıştırılabilir giriş dosyası yok" in result
    # Model eksik giris dosyasini gormeli (SMART EXIT ve accept yerine).
    assert any("→ main.py" in p for p in prompts[2:]), prompts[-1][-800:]
    assert "SMART_EXIT" not in result


def test_gui_task_completes_when_main_is_written(home):
    project = home / "jarvis_programs" / "tk_hesap"
    model = _scenario(_write("calculator.py", CALCULATOR), _write("main.py", GUI_MAIN),
                      _write("README.md", GUI_README), ACCEPT)
    result = _solve(model, GUI_DESC, project, max_iterations=6)

    block = _result_block(result)
    assert "Durum: BAŞARILI" in block, result
    assert "Eksik dosya" not in block
    assert "python3 main.py" in result
    assert "çalıştırılabilir giriş dosyası yok" not in result


def test_gui_task_accepts_other_entry_file_instead_of_main(home):
    project = home / "jarvis_programs" / "tk_hesap"
    model = _scenario(_write("calculator.py", CALCULATOR), _write("app.py", GUI_MAIN),
                      _write("README.md", GUI_README.replace("main.py", "app.py")), ACCEPT)
    result = _solve(model, GUI_DESC, project, max_iterations=6)

    block = _result_block(result)
    assert "Durum: BAŞARILI" in block, result
    assert "Eksik dosya" not in block
    assert not (project / "main.py").exists()
    assert "python3 app.py" in result


def test_library_only_result_says_no_runnable_entry(home):
    project = home / "jarvis_programs" / "hesap_modulu"
    model = _scenario(_write("calculator.py", CALCULATOR), _write("README.md", GUI_README), ACCEPT)
    result = _solve(model, "calculator.py dosyasında hesap makinesi modülü yaz", project)

    assert "Durum: BAŞARILI" in result, result
    assert "çalıştırılabilir giriş dosyası yok" in _result_block(result)
    assert "python3 calculator.py" not in result
