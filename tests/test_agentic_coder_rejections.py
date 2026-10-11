"""tests/test_agentic_coder_rejections.py — reddedilen yazimlar gorunur ve donguyu keser

Canli hata: "main.py yaz" 22 tur ust uste reddedildi; log'da ne "yazıldı" ne
"ruff" satiri vardi, ret sebebi hicbir yerde gorunmuyordu. Gorev 25 tur yanip
yalnizca "Eksik dosya: main.py" dedi. Icerik her turda biraz farkli oldugu icin
"ayni icerik" sayacina takilmadi.

Ayrica yanlis alarm incelemesi: gercekci tkinter hesap makinesi main.py
ornekleri _validate_code / _validate_file'dan gecmeli; gercek stub'lar
reddedilmeye devam etmeli ve mesaj modele NE degistirecegini soylemeli.

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


def _write(name: str, content: str) -> str:
    return _decide({"action": "write", "args": {"filename": name, "content": content}})


def _solve(project, model, ui=None, max_iterations=25, description="hesap makinesi yaz"):
    coder = ac.AgenticCoder(model_fn=model, ui=ui, max_iterations=max_iterations)
    return asyncio.run(coder.solve(description=description, project_path=str(project)))


def _stub_main(i: int) -> str:
    """Her turda biraz farkli ama hep bos (stub) fonksiyon iceren main.py."""
    return (
        f'"""Hesap makinesi taslagi, deneme {i}."""\n\n\n'
        f"def topla_{i}(a: float, b: float) -> float:\n"
        '    """Iki sayiyi toplar."""\n'
        "    pass\n\n\n"
        "def main() -> None:\n"
        '    """Giris noktasi: ornek bir toplama yazdirir."""\n'
        f'    print("sonuc:", topla_{i}(2, 3))\n\n\n'
        'if __name__ == "__main__":\n'
        "    main()\n"
    )


# ── 1) her ret ekranda "⛔ <dosya> reddedildi: <sebep>" ──

@pytest.mark.parametrize("filename,content,expect", [
    ("main.py", _stub_main(1), "boş"),
    ("main.py", "print(1)\n", "kısa"),
    ("../disari.py", _stub_main(1).replace("    pass\n", "    return a + b\n"), "proje"),
    ("main.py", _stub_main(1).replace("    pass\n", "    return eval('a + b')\n"), "eval/exec"),
    ("main.py", _stub_main(1).replace("    pass\n", "    return sqrt(a + b)\n"), "F821"),
])
def test_every_rejection_is_shown_on_screen(home, filename, content, expect):
    project = home / "jarvis_programs" / "hesap"
    ui = _UI()
    seq = iter([_write(filename, content)])

    def model(prompt: str) -> str:
        return next(seq, _decide({"action": "inspect", "args": {}}))

    _solve(project, model, ui=ui, max_iterations=2)
    shown = [ln for ln in ui.lines if "⛔" in ln and "reddedildi:" in ln]
    assert shown, ui.lines
    assert expect in shown[0], shown[0]
    assert len(shown[0].split("reddedildi:", 1)[1].strip()) <= 150


# ── 2) ayni dosya 3 kez ust uste reddedilirse (icerik farkli) gorev durur ──

def test_three_rejections_of_same_file_stop_the_task(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return _write("main.py", _stub_main(len(prompts)))

    result = _solve(project, model)

    assert len(prompts) == 3, f"{len(prompts)} LLM cagrisi (kota yakildi)"
    assert "Durum: BAŞARISIZ" in result
    assert "döngü: main.py 3 kez reddedildi, son sebep:" in result
    assert "topla_3" in result.split("son sebep:", 1)[1]


def test_successful_write_resets_rejection_streak(home):
    project = home / "jarvis_programs" / "hesap"
    good = _stub_main(9).replace("    pass\n", "    return a + b\n")
    seq = iter([_write("main.py", _stub_main(1)), _write("main.py", _stub_main(2)),
                _write("main.py", good), _write("main.py", _stub_main(3))])
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, _decide({"action": "inspect", "args": {}}))

    result = _solve(project, model, max_iterations=5)
    assert "3 kez reddedildi" not in result


# ── 3) reddedilen son icerik _reddedilen_<dosya>.txt; model goremez ──

def test_rejected_content_is_saved_but_hidden_from_model(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []
    seq = iter([
        _write("main.py", _stub_main(1)),
        _write("main.py", _stub_main(2)),
        _decide({"action": "inspect", "args": {"filename": "_reddedilen_main.py.txt"}}),
        _decide({"action": "inspect", "args": {}}),
    ])

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, _decide({"action": "inspect", "args": {}}))

    _solve(project, model, max_iterations=5)

    saved = project / "_reddedilen_main.py.txt"
    assert saved.is_file() and "topla_2" in saved.read_text()   # SON reddedilen icerik
    assert not (project / "main.py").exists()
    # Ret SEBEBI ("'topla_2' fonksiyonu bos") modele gider; icerigin kendisi
    # (yalnizca dosyadaki docstring "deneme 2") gitmez.
    assert not any("deneme 2" in p for p in prompts), "model reddedilen icerigi gordu"
    assert not any("_reddedilen_" in p for p in prompts), "model dosya adini gordu"


def test_path_rejected_content_is_saved_inside_project(home):
    project = home / "jarvis_programs" / "hesap"
    body = _stub_main(1).replace("    pass\n", "    return a + b\n")
    seq = iter([_write("../../disari.py", body)])

    def model(prompt: str) -> str:
        return next(seq, _decide({"action": "inspect", "args": {}}))

    _solve(project, model, max_iterations=2)
    assert (project / "_reddedilen_disari.py.txt").is_file()
    assert not (home / "jarvis_programs" / "disari.py").exists()


# ── 4) yanlis alarm incelemesi: gercekci tkinter hesap makinesi main.py ──

TK_HEADER = '''"""Tkinter hesap makinesi.

Tuslara basildikca ifade ekranda birikir; "=" sonucu hesaplar. Gecersiz
ifadede ekranda "Error" gorunur. (Bu modulde stub yok; testler pass etmeli.)
"""
import ast
import operator
import tkinter as tk

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub,
        ast.Mult: operator.mul, ast.Div: operator.truediv}


def safe_eval(expr: str) -> float:
    """Yalnizca + - * / iceren ifadeyi eval() KULLANMADAN hesaplar."""
    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        raise ValueError("desteklenmeyen ifade")
    return walk(ast.parse(expr, mode="eval"))
'''

TK_APP = '''

class Calculator:
    """Ekran + tus takimi."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.display = tk.Entry(root, font=("Arial", 18), justify="right")
        self.display.grid(row=0, column=0, columnspan=4, sticky="nsew")
        # Tus takimi: 4x4 izgara
        keys = ["7", "8", "9", "/", "4", "5", "6", "*", "1", "2", "3", "-", "0", "C", "=", "+"]
        for idx, key in enumerate(keys):
            btn = tk.Button(root, text=key, width=4,
                            command=lambda k=key: self.press(k))
            btn.grid(row=1 + idx // 4, column=idx % 4)

    def press(self, key: str) -> None:
        """Tusa basildiginda ekrani gunceller."""
        if key == "C":
            self.display.delete(0, tk.END)
        elif key == "=":
            self.calculate()
        else:
            self.display.insert(tk.END, key)

    def calculate(self) -> None:
        """Ekrandaki ifadeyi hesaplar; hatada "Error" gosterir."""
        expr = self.display.get()
        try:
            result = safe_eval(expr)
        except ZeroDivisionError:
            result = "Error: sifira bolme"
        except (ValueError, SyntaxError):
            result = "Error"
        self.display.delete(0, tk.END)
        self.display.insert(tk.END, str(result))


def main() -> None:
    root = tk.Tk()
    root.title("Hesap Makinesi")
    Calculator(root)
    root.mainloop()


if __name__ == "__main__":
    main()
'''

TK_EXAMPLES = {
    "tam_uygulama": TK_HEADER + TK_APP,
    "except_icinde_pass": TK_HEADER + TK_APP.replace(
        '        except (ValueError, SyntaxError):\n            result = "Error"\n',
        '        except (ValueError, SyntaxError):\n            result = "Error"\n'
        "        except OverflowError:\n            pass\n"),
    "bos_hata_sinifi": TK_HEADER + "\n\nclass CalcError(Exception):\n    pass\n" + TK_APP,
    "metin_icinde_pass": TK_HEADER + TK_APP.replace(
        'root.title("Hesap Makinesi")', 'root.title("Hesap Makinesi - pass the numbers")'),
    "yorumda_doldur": TK_HEADER + TK_APP.replace(
        "        # Tus takimi: 4x4 izgara\n",
        "        # Ekrani doldur: 4x4 izgara, son satirda = ve +\n"),
    "self_eval_metodu": TK_HEADER + TK_APP.replace(
        "            self.calculate()\n", "            self.eval()\n").replace(
        "    def calculate(self) -> None:", "    def eval(self) -> None:"),
    "notimplemented_except": TK_HEADER + TK_APP.replace(
        "        except ZeroDivisionError:\n",
        "        except NotImplementedError:\n            result = \"Error\"\n"
        "        except ZeroDivisionError:\n"),
    "protocol_ellipsis": TK_HEADER + (
        "\n\nfrom typing import Protocol\n\n\n"
        "class Ekran(Protocol):\n"
        "    def insert(self, index: str, text: str) -> None:\n"
        "        ...\n") + TK_APP,
}


@pytest.mark.parametrize("name", sorted(TK_EXAMPLES))
def test_realistic_tkinter_calculator_is_accepted(name):
    code = TK_EXAMPLES[name]
    ok, msg = ac._validate_file("main.py", code)
    assert ok, f"{name}: {msg}"


@pytest.mark.parametrize("code,needle", [
    ("def topla(a, b):\n    pass\n", "topla"),
    ("def topla(a, b):\n    '''Toplar.'''\n    pass\n", "topla"),
    ("def topla(a, b):\n    ...\n", "topla"),
    ("class C:\n    def topla(self, a, b):\n        raise NotImplementedError\n", "topla"),
    ("def topla(a, b):\n    # TODO: implement\n    return a + b\n", "TODO"),
    ("def topla(a, b):\n    # buraya kodu yazin\n    return a + b\n", "yer tutucu"),
])
def test_real_stubs_still_rejected_with_actionable_message(code, needle):
    ok, msg = ac._validate_code(code)
    assert not ok
    assert needle in msg, msg
    assert "yaz" in msg or "sil" in msg, f"mesaj ne yapilacagini soylemiyor: {msg}"
