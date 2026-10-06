"""tests/test_agentic_coder_gui_expect.py — GUI beklentisi algisi

Canli hata: "Modern Windows tarzında bir hesap makinesi uygulaması yap" istegi
3 turda BASARILI bitti; sonuc input()/print() ile calisan, Ingilizce, penceresiz
bir komut satiri hesaplayicisiydi (main.py + README.md). _wants_gui yalnizca
"tkinter/arayuz/GUI/pencere/tus takimi" kelimelerini ariyordu; "Windows tarzi /
modern / hesap makinesi / uygulama" GUI beklentisi sayilmiyordu.

Testler GERCEK AgenticCoder.solve dongusunu senaryolu model_fn ile calistirir.
HOME ve proje klasoru tmp_path altindadir.
"""
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


def _scenario(*answers, prompts=None):
    seq = iter(answers)

    def model(prompt: str) -> str:
        if prompts is not None:
            prompts.append(prompt)
        return next(seq, ACCEPT)
    return model


class _Log:
    def __init__(self):
        self.lines: list[str] = []

    def write_log(self, msg: str) -> None:
        self.lines.append(msg)


def _solve(model, description, project, max_iterations=6, ui=None):
    coder = ac.AgenticCoder(model_fn=model, max_iterations=max_iterations, ui=ui)
    return asyncio.run(coder.solve(description=description, project_path=str(project)))


def _result_block(result: str) -> str:
    return result.split("\n\n", 1)[0]


LIVE_DESC = "Modern Windows tarzında bir hesap makinesi uygulaması yap"

# Canli sonucun ozu: Ingilizce, input()/print() ile calisan komut satiri.
CLI_MAIN = '''"""Simple command line calculator."""


def calculate(a: float, op: str, b: float) -> float:
    """Apply a basic arithmetic operator."""
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if op == "/":
        return a / b
    raise ValueError(f"unknown operator: {op}")


def main() -> None:
    """Read two numbers and an operator from the terminal."""
    a = float(input("First number: "))
    op = input("Operator (+ - * /): ")
    b = float(input("Second number: "))
    print("Result:", calculate(a, op, b))


if __name__ == "__main__":
    main()
'''

CLI_README = (
    "# Calculator\n\nA simple command line calculator. It asks for two numbers and an "
    "operator, then prints the result. Supported operators are plus, minus, times and "
    "divide.\n\n## Usage\n\n```bash\npython3 main.py\n```\n"
)

GUI_MAIN = '''"""Windows tarzi hesap makinesi: tkinter penceresi, tus takimi ve ekran."""
import tkinter as tk

KEYS = "789/456*123-0.=+"


def hesapla(ifade: str) -> float:
    """Yalnizca iki sayi ve tek islecli ifadeyi hesaplar (eval yok)."""
    for op in "+-*/":
        sol, _, sag = ifade.rpartition(op)
        if sol and sag:
            a, b = float(sol), float(sag)
            return {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else 0.0}[op]
    return float(ifade)


class App:
    """Ekran ve dugmelerden olusan hesap makinesi penceresi."""

    def __init__(self, root: tk.Tk) -> None:
        self.display = tk.Entry(root, width=24, justify="right")
        self.display.grid(row=0, column=0, columnspan=4)
        for i, key in enumerate(KEYS):
            tk.Button(root, text=key, width=5,
                      command=lambda k=key: self.press(k)).grid(row=1 + i // 4, column=i % 4)

    def press(self, key: str) -> None:
        """Dugmeye basilinca ekrani gunceller ya da hesaplar."""
        if key != "=":
            self.display.insert(tk.END, key)
            return
        try:
            result = str(hesapla(self.display.get()))
        except ValueError:
            result = "Hata"
        self.display.delete(0, tk.END)
        self.display.insert(0, result)


if __name__ == "__main__":
    window = tk.Tk()
    App(window)
    window.mainloop()
'''

GUI_README = (
    "# Modern hesap makinesi\n\nTkinter ile yazilmis, Windows tarzi dugmeli bir hesap "
    "makinesi. Pencerede bir ekran ve tus takimi vardir; hesaplama eval kullanmadan "
    "yapilir ve sonuc ekranda gosterilir.\n\n## Calistirma\n\n```bash\npython3 main.py\n```\n"
)


# ── _wants_gui: kelime algisi ─────────────────────────────────

@pytest.mark.parametrize("description", [
    LIVE_DESC,
    "Windows tarzı bir not defteri yap",
    "modern bir saat uygulaması yaz",
    "düğmeli bir zamanlayıcı yaz",
    "görsel bir yılan oyunu yap",
    "calculator uygulaması yap, butonları olsun",
    "Tkinter ile hesap makinesi yaz",            # eski kelimeler hala gecerli
])
def test_desktop_app_descriptions_expect_gui(description):
    assert ac._wants_gui(description) is True


@pytest.mark.parametrize("description", [
    "API yaz",
    "veri işleme betiği yaz",
    "CSV veri işleme uygulaması yaz",            # 'uygulama' tek basina GUI degil
    "hesap makinesi modülü yaz",                 # uygulama kelimesi + bicim yok
    "komut satırında çalışan hesap makinesi uygulaması yap",
    "terminalde çalışan modern bir zamanlayıcı uygulaması yaz",
    "CLI not defteri uygulaması yaz",
    "konsol tabanlı oyun uygulaması yap",
    "komut satırı arayüzü olan bir saat uygulaması yaz",
])
def test_non_gui_descriptions_do_not_expect_gui(description):
    assert ac._wants_gui(description) is False


# ── Canli hata: penceresiz komut satiri sonucu BASARILI sayilmaz ──

def test_live_cli_calculator_is_not_accepted_for_gui_request(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []
    # Model her seferinde (kucuk farkla) yine input() tabanli main.py yaziyor.
    model = _scenario(_write("main.py", CLI_MAIN), _write("README.md", CLI_README),
                      _write("main.py", CLI_MAIN + "\n# v2\n"), ACCEPT,
                      _write("main.py", CLI_MAIN + "\n# v3\n"), ACCEPT, prompts=prompts)
    log = _Log()
    result = _solve(model, LIVE_DESC, project, max_iterations=8, ui=log)

    block = _result_block(result)
    assert "Durum: BAŞARISIZ" in block, result
    assert "SMART_EXIT" not in result
    # Gorunur ret: ekrana (UI) ve modele acikca gider.
    shown = [m for m in log.lines if "main.py reddedildi" in m and "input() kullanma" in m]
    assert len(shown) == 3, log.lines
    assert any("pencereli GUI yaz" in p and "input() kullanma" in p for p in prompts[1:]), \
        prompts[-1][-1500:]
    # Sonuc blogunda net not.
    assert "GUI bekleniyordu" in block and "3 kez reddedildi" in block, block
    assert (project / "_reddedilen_main.py.txt").is_file()


def test_gui_rejections_stop_at_reject_limit(home):
    project = home / "jarvis_programs" / "hesap"
    model = _scenario(*[_write("main.py", CLI_MAIN + f"\n# v{n}\n") for n in range(6)])
    result = _solve(model, LIVE_DESC, project, max_iterations=8)

    assert "Durum: BAŞARISIZ" in _result_block(result), result
    assert f"main.py {ac._REJECT_LIMIT} kez reddedildi" in result, result
    assert not (project / "main.py").exists()


def test_gui_request_completes_after_window_code_is_written(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []
    model = _scenario(_write("main.py", CLI_MAIN), _write("main.py", GUI_MAIN),
                      _write("README.md", GUI_README), ACCEPT, prompts=prompts)
    result = _solve(model, LIVE_DESC, project, max_iterations=6)

    block = _result_block(result)
    assert "Durum: BAŞARILI" in block, result
    assert "GUI bekleniyordu" not in block
    assert "import tkinter" in (project / "main.py").read_text(encoding="utf-8")
    assert "pencereli GUI yaz" in prompts[1]


def test_explicit_cli_request_with_input_is_accepted(home):
    project = home / "jarvis_programs" / "hesap"
    model = _scenario(_write("main.py", CLI_MAIN), _write("README.md", CLI_README), ACCEPT)
    result = _solve(model, "Komut satırında çalışan hesap makinesi uygulaması yap", project)

    block = _result_block(result)
    assert "Durum: BAŞARILI" in block, result
    assert "GUI bekleniyordu" not in block
    assert "REDDEDİLDİ (GUI)" not in result


def test_non_gui_data_script_with_input_is_not_flagged(home):
    project = home / "jarvis_programs" / "veri"
    model = _scenario(_write("main.py", CLI_MAIN), _write("README.md", CLI_README), ACCEPT)
    result = _solve(model, "veri işleme betiği yaz", project)

    assert "Durum: BAŞARILI" in _result_block(result), result
    assert "REDDEDİLDİ (GUI)" not in result
