"""tests/test_agentic_coder_methods.py — dosyalar arasi metot varlik kapisi

Canli hata: main.py `self.calculator.calculate(...)` cagiriyordu ama
calculator.py'deki Calculator sinifinda `calculate` yoktu. Cagri yalnizca bir
GUI geri cagrisinda calistigi icin --help / import / pytest kapilari yakalamadi.

Birim testleri _missing_methods'u (LLM'siz, ast) dogrudan; dongu testleri GERCEK
AgenticCoder.solve'u senaryolu model_fn ile calistirir. HOME tmp_path altinda.
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


def _solve(project, model, ui=None, max_iterations=25, description="calculator.py ve main.py yaz"):
    coder = ac.AgenticCoder(model_fn=model, ui=ui, max_iterations=max_iterations)
    return asyncio.run(coder.solve(description=description, project_path=str(project)))


CALC = '''"""Hesap cekirdegi: dort islem ve karekok."""
import math


class Calculator:
    """Temel islemler."""

    def add(self, a: float, b: float) -> float:
        """Iki sayiyi toplar."""
        return a + b

    def sqrt(self, x: float) -> float:
        """Karekok; negatifte ValueError."""
        return math.sqrt(x)
'''

CALC_FIXED = CALC + '''
    def calculate(self, expr: str) -> float:
        """'a+b' bicimindeki ifadeyi hesaplar."""
        left, right = expr.split("+")
        return self.add(float(left), float(right))
'''

MAIN = '''"""Uygulama: ifadeyi hesap cekirdegine iletir."""
from calculator import Calculator


class App:
    """Ekran yerine basit metin arayuzu."""

    def __init__(self) -> None:
        self.calculator = Calculator()

    def on_equals(self, expr: str) -> float:
        """'=' tusu: ifadeyi hesaplar."""
        return self.calculator.calculate(expr)


def main() -> None:
    """Giris noktasi."""
    App()
    print("hazir")


if __name__ == "__main__":
    main()
'''

README = (
    "# Hesap makinesi\n\nBu proje bir hesap makinesidir. Calculator sinifi dort islem ve "
    "karekok sunar; App sinifi ifadeyi hesap cekirdegine iletir.\n\n## Calistirma\n\n"
    "```bash\npython3 main.py\n```\n\n## Testler\n\n```bash\npython3 -m pytest -q\n```\n"
)

TEST_CORE = '''"""Calculator testleri."""
from calculator import Calculator


def test_add() -> None:
    assert Calculator().add(2, 3) == 5


def test_sqrt() -> None:
    assert Calculator().sqrt(9) == 3


def test_add_negative() -> None:
    assert Calculator().add(-2, 2) == 0
'''


def _project(main: str = MAIN, calc: str = CALC) -> dict:
    return {"calculator.py": calc, "main.py": main}


# ── B1) birim: eksik metot yakalanir, mesaj dosya + satir soyler ──

def test_missing_method_via_self_attribute_is_reported():
    problems = ac._missing_methods(_project())
    line = MAIN.splitlines().index("        return self.calculator.calculate(expr)") + 1
    assert problems == [
        f"calculator.py: Calculator sınıfında 'calculate' metodu yok (main.py satır {line} kullanıyor)"
    ]


@pytest.mark.parametrize("main", [
    # yerel degisken
    "from calculator import Calculator\n\n\ndef f():\n    c = Calculator()\n    return c.calculate('1+2')\n",
    # modul uzerinden
    "import calculator\n\n\ndef f():\n    c = calculator.Calculator()\n    return c.calculate('1+2')\n",
    # takma ad
    "from calculator import Calculator as C\n\nc = C()\nc.calculate('1+2')\n",
    # tip notlu self atamasi
    "from calculator import Calculator\n\n\nclass A:\n    def __init__(self):\n"
    "        self.c: Calculator = Calculator()\n\n    def f(self):\n        return self.c.calculate('1')\n",
])
def test_missing_method_detected_in_common_forms(main):
    problems = ac._missing_methods(_project(main=main))
    assert len(problems) == 1 and "'calculate'" in problems[0], problems


# ── B2) yanlis alarm yok ──

@pytest.mark.parametrize("name,calc,main", [
    ("var_olan_metot", CALC, MAIN.replace(".calculate(expr)", ".add(1, 2)")),
    ("duzeltilmis_sinif", CALC_FIXED, MAIN),
    ("getattr", CALC + "\n    def __getattr__(self, name):\n        return lambda *a: 0\n", MAIN),
    ("yerel_olmayan_taban", CALC.replace("class Calculator:", "class Calculator(dict):"), MAIN),
    ("yerel_taban_metodu",
     CALC.replace("class Calculator:", "class Base:\n    def calculate(self, e):\n"
                  "        return 0\n\n\nclass Calculator(Base):"), MAIN),
    ("sinif_duzeyi_atama", CALC + "\n    calculate = add\n", MAIN),
    ("ornek_ozniteligi", CALC + "\n    def __init__(self):\n        self.calculate = self.add\n", MAIN),
    ("setattr", CALC + "\n    def __init__(self):\n        setattr(self, 'calculate', self.add)\n", MAIN),
    ("kosullu_atama", CALC,
     MAIN.replace("self.calculator = Calculator()",
                  "self.calculator = Calculator() if True else object()")),
    ("yeniden_atama", CALC,
     MAIN.replace("        self.calculator = Calculator()\n",
                  "        self.calculator = Calculator()\n        self.calculator = make()\n")),
    ("parametre_golgeler", CALC,
     "from calculator import Calculator\n\nc = Calculator()\n\n\ndef f(c):\n    return c.calculate('1')\n"),
    ("yerel_olmayan_import", CALC, MAIN.replace("from calculator import", "from decimal import")),
])
def test_no_false_alarm(name, calc, main):
    assert ac._missing_methods(_project(main=main, calc=calc)) == [], name


# ── B1+B3) dongu: SMART EXIT / accept engellenir, model yonlendirilir ──

def _base_seq() -> list[str]:
    return [_write("calculator.py", CALC), _write("main.py", MAIN), _write("README.md", README),
            _write("tests/__init__.py", ""), _write("tests/test_core.py", TEST_CORE)]


def test_missing_method_blocks_accept_and_fix_accepts(home):
    project = home / "jarvis_programs" / "hesap"
    ui = _UI()
    prompts: list[str] = []
    seq = iter(_base_seq() + [
        _decide({"action": "accept", "args": {}, "response": "bitti"}),
        _write("calculator.py", CALC_FIXED),
        _decide({"action": "accept", "args": {}, "response": "bitti"}),
    ])

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, _decide({"action": "inspect", "args": {}}))

    result = _solve(project, model, ui=ui)

    assert any("ACCEPT REDDEDILDI" in ln and "calculate" in ln for ln in ui.lines), ui.lines
    # Modele hangi metodun hangi dosyaya eklenecegi soylendi
    assert any("calculator.py" in p and "Calculator.calculate" in p and "ekle" in p
               for p in prompts[5:7]), prompts[5][-1500:]
    assert "Durum: BAŞARILI" in result, result
    assert len(prompts) <= 9


def test_missing_method_never_smart_exits_and_stops_after_three_turns(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []
    seq = iter(_base_seq())

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, _decide({"action": "inspect", "args": {}}))

    result = _solve(project, model)

    assert "Durum: BAŞARISIZ" in result
    assert "Calculator sınıfında 'calculate' metodu yok" in result
    assert "EKSİK METOT" in result
    assert len(prompts) <= 9, f"{len(prompts)} LLM cagrisi"
