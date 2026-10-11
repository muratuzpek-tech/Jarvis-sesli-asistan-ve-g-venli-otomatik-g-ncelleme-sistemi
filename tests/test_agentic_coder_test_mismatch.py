"""tests/test_agentic_coder_test_mismatch.py — canli hesap makinesi kosusunun uc hatasi

Canli ornek (~/jarvis_programs/tkinter_kullanarak_windows_3): tests/test_core.py
`pytest.raises(ZeroDivisionError)` bekliyor, calculator.py divide() ValueError
firlatiyordu; gorev "pytest FAILED: test_division" ile bitti.

1) pytest --tb=short ciktisi yalnizca `calculator.py:37 ... E ValueError`
   gosteriyordu; beklentinin test dosyasinda oldugu hic soylenmiyordu. Ustelik
   prompt her turda "tests/test_core.py TEKRAR YAZMA" diyordu. Model 10-18.
   turlarda hep calculator.py'yi "duzeltti".
2) Bir fix calculator.py'yi 1069 karakterden 291'e indirdi: Calculator sinifi
   kayboldu (parca, dosyanin tamami sanilip yazildi).
3) Modele giden hata bayattı: inspect/run ciktisi (last_run_output) hic
   temizlenmiyordu; taze bir ret mesaji bir sonraki turda degismemis pytest
   hatasiyla eziliyordu.

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


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


DESC = "calculator.py ve main.py ile dört işlem hesap makinesi yaz"
NARROW = "SADECE tests/test_core.py"   # dar test prompt'unun imzasi


def _decide(d: dict) -> str:
    return json.dumps({"thought": "t", **d})


def _write(name: str, content: str, action: str = "write") -> str:
    return _decide({"action": action, "args": {"filename": name, "content": content}})


def _solve(project, model, max_iterations=12):
    coder = ac.AgenticCoder(model_fn=model, max_iterations=max_iterations)
    return asyncio.run(coder.solve(description=DESC, project_path=str(project)))


# Canli calculator.py'nin ozu: safe_eval + Calculator, divide() ValueError.
CALC = (
    '"""Dort islem ve guvenli ifade hesaplama."""\n'
    "import ast\n"
    "import operator\n\n"
    "_OPS = {ast.Add: operator.add, ast.Sub: operator.sub,\n"
    "        ast.Mult: operator.mul, ast.Div: operator.truediv}\n\n\n"
    "def safe_eval(expr: str) -> float:\n"
    '    """Yalnizca sayi ve dort islem; gerisi ValueError."""\n'
    "    def walk(node):\n"
    "        if isinstance(node, ast.Constant) and type(node.value) in (int, float):\n"
    "            return node.value\n"
    "        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:\n"
    "            return _OPS[type(node.op)](walk(node.left), walk(node.right))\n"
    '        raise ValueError("izin verilmeyen ifade")\n'
    '    return walk(ast.parse(expr, mode="eval").body)\n\n\n'
    "class Calculator:\n"
    '    """Toplama, cikarma, carpma ve bolme."""\n\n'
    "    def add(self, a, b):\n"
    "        return a + b\n\n"
    "    def subtract(self, a, b):\n"
    "        return a - b\n\n"
    "    def multiply(self, a, b):\n"
    "        return a * b\n\n"
    "    def divide(self, a, b):\n"
    "        if b == 0:\n"
    '            raise ValueError("Cannot divide by zero")\n'
    "        return a / b\n"
)
# Kaynakta gercek hata: multiply() ZeroDivisionError ile coker.
CALC_BUG = CALC.replace("        return a * b\n", "        return a * b / (b - b)\n")

MAIN = (
    '"""Hesap makinesi giris noktasi."""\n'
    "from calculator import Calculator, safe_eval\n\n\n"
    "def main() -> None:\n"
    '    """Ornek hesaplari ekrana yazdirir."""\n'
    "    calc = Calculator()\n"
    '    print("2 + 3 =", calc.add(2, 3))\n'
    '    print("2*3+1 =", safe_eval("2*3+1"))\n\n\n'
    'if __name__ == "__main__":\n'
    "    main()\n"
)

README = (
    "# Hesap Makinesi\n\n"
    "Dort islem yapan basit bir hesap makinesi. Toplama, cikarma, carpma ve "
    "bolme desteklenir; sifira bolme anlasilir bir hata verir. Ifadeler eval "
    "kullanilmadan guvenli bicimde hesaplanir.\n\n"
    "## Kullanim\n\n    python3 main.py\n\n"
    "## Test\n\n    python3 -m pytest -q\n"
)

# Canli tests/test_core.py: ZeroDivisionError bekliyor (kod ValueError atiyor).
TEST_WRONG = (
    "import pytest\n"
    "from calculator import Calculator\n\n\n"
    "def test_addition():\n"
    "    assert Calculator().add(2, 3) == 5\n\n\n"
    "def test_subtraction():\n"
    "    assert Calculator().subtract(5, 3) == 2\n\n\n"
    "def test_multiplication():\n"
    "    assert Calculator().multiply(4, 3) == 12\n\n\n"
    "def test_division():\n"
    "    calc = Calculator()\n"
    "    assert calc.divide(10, 2) == 5\n"
    "    with pytest.raises(ZeroDivisionError):\n"
    "        calc.divide(10, 0)\n"
)
TEST_RIGHT = TEST_WRONG.replace("ZeroDivisionError", "ValueError")

# Canli kosudaki parca: yalnizca divide, Calculator ve safe_eval yok.
FRAGMENT = (
    '"""Bolme islemi duzeltmesi."""\n\n\n'
    "def divide(a: float, b: float) -> float:\n"
    '    """Iki sayiyi boler; sifira bolmede ZeroDivisionError firlatir."""\n'
    "    if b == 0:\n"
    '        raise ZeroDivisionError("sifira bolme yapilamaz")\n'
    "    return a / b\n"
)


def _project_writes(test_core: str, calc: str = CALC) -> list[str]:
    return [
        _write("calculator.py", calc),
        _write("main.py", MAIN),
        _write("README.md", README),
        _write("tests/__init__.py", ""),
        _write("tests/test_core.py", test_core),
    ]


# ── Hata 1: test beklentisi kodla uyumsuz ──────────────────────

def test_mismatched_test_is_regenerated_from_source_and_task_accepts(home):
    project = home / "jarvis_programs" / "hesap"
    narrow: list[str] = []
    seq = iter(_project_writes(TEST_WRONG))

    def model(prompt: str) -> str:
        if NARROW in prompt:
            narrow.append(prompt)
            return _write("tests/test_core.py", TEST_RIGHT)
        # Canli model gibi: hep calculator.py'yi "duzeltmeye" calisir.
        return next(seq, None) or _write("calculator.py", CALC, "fix")

    result = _solve(project, model)

    assert narrow, "uyumsuz test dar prompt ile yeniden uretilmedi"
    assert len(narrow) <= ac._TEST_CORE_ATTEMPTS
    # Dar prompt: kaynak modulun guncel icerigi + pytest ciktisi.
    assert 'raise ValueError("Cannot divide by zero")' in narrow[0]
    assert "class Calculator" in narrow[0]
    assert "ZeroDivisionError" in narrow[0] and "test_division" in narrow[0]
    assert "pytest.raises(ValueError)" in (project / "tests" / "test_core.py").read_text()
    assert "Durum: BAŞARILI" in result and "ACCEPTED ✅" in result, result


def test_failed_regeneration_names_test_file_and_model_may_fix_it(home):
    project = home / "jarvis_programs" / "hesap"
    narrow: list[str] = []
    main_prompts: list[str] = []
    seq = iter(_project_writes(TEST_WRONG))

    def model(prompt: str) -> str:
        if NARROW in prompt:
            narrow.append(prompt)
            return _write("tests/test_core.py", TEST_WRONG)   # hep ayni yanlis
        main_prompts.append(prompt)
        step = next(seq, None)
        if step:
            return step
        if "HATALI DOSYA: tests/test_core.py" in prompt:
            return _write("tests/test_core.py", TEST_RIGHT, "fix")
        return _write("calculator.py", CALC, "fix")

    result = _solve(project, model)

    assert len(narrow) == ac._TEST_CORE_ATTEMPTS, f"{len(narrow)} dar deneme"
    fed = [p for p in main_prompts if "HATALI DOSYA: tests/test_core.py" in p]
    assert fed, "modele hatali dosya soylenmedi"
    assert "ZeroDivisionError" in fed[0] and "ValueError" in fed[0]
    assert "tests/test_core.py TEKRAR YAZMA" not in fed[0]
    assert "Durum: BAŞARILI" in result and "ACCEPTED ✅" in result, result


def test_source_crash_names_source_file_and_does_not_rewrite_tests(home):
    project = home / "jarvis_programs" / "hesap"
    narrow: list[str] = []
    main_prompts: list[str] = []
    seq = iter(_project_writes(TEST_RIGHT, calc=CALC_BUG))

    def model(prompt: str) -> str:
        if NARROW in prompt:
            narrow.append(prompt)
            return _write("tests/test_core.py", TEST_RIGHT)
        main_prompts.append(prompt)
        step = next(seq, None)
        if step:
            return step
        if "HATALI DOSYA: calculator.py" in prompt:
            return _write("calculator.py", CALC, "fix")
        return _decide({"action": "inspect", "args": {"filename": "calculator.py"}})

    result = _solve(project, model)

    assert not narrow, "kaynak hatasinda test dosyasi yeniden uretilmemeli"
    fed = [p for p in main_prompts if "HATALI DOSYA: calculator.py" in p]
    assert fed and "test_multiplication" in fed[0]
    assert "HATALI DOSYA: tests/test_core.py" not in fed[0]
    assert "Durum: BAŞARILI" in result, result


# ── Hata 2: fix dosyayi parcayla eziyor ────────────────────────

def test_fragment_losing_public_names_is_rejected_full_rewrite_allowed(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []
    calc_plus = CALC + "\n    def power(self, a, b):\n        return a**b\n"
    helper = (
        '"""Kucuk yardimcilar: sayi bicimlendirme."""\n\n\n'
        "def fmt(x: float) -> str:\n"
        '    """Tam sayi olan sonucu ondaliksiz, digerlerini oldugu gibi yazar."""\n'
        "    return str(int(x)) if float(x).is_integer() else str(x)\n"
    )
    seq = iter([
        _write("calculator.py", CALC),
        _write("calculator.py", FRAGMENT, "fix"),           # 2: API kaybi -> RET
        _write("calculator.py", calc_plus, "fix"),          # 3: tum adlar korunuyor -> OK
        _write("yardimci.py", helper),                      # 4: yeni kisa dosya -> OK
        _write("main.py", MAIN),
        _write("README.md", README),
        _write("tests/__init__.py", ""),
        _write("tests/test_core.py", TEST_RIGHT),
    ])

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, _decide({"action": "accept", "args": {}, "response": "bitti"}))

    result = _solve(project, model)

    # Ret mesaji kaybolan adlari listeler ve dosyanin TAMAMINI ister.
    msg = prompts[2]
    assert "API_KAYBI (calculator.py)" in msg, msg[-1500:]
    assert "Calculator" in msg.split("API_KAYBI", 1)[1][:300]
    assert "safe_eval" in msg.split("API_KAYBI", 1)[1][:300]
    assert "TAMAMINI" in msg
    assert (project / "_reddedilen_calculator.py.txt").read_text() == FRAGMENT
    disk = (project / "calculator.py").read_text()
    assert "class Calculator" in disk and "def safe_eval" in disk and "def power" in disk
    assert (project / "yardimci.py").is_file()
    assert "Durum: BAŞARILI" in result, result


def test_first_write_of_short_new_file_is_not_an_api_loss(home):
    project = home / "jarvis_programs" / "hesap"
    seq = iter([_write("calculator.py", FRAGMENT)])
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, _decide({"action": "inspect", "args": {}}))

    _solve(project, model, max_iterations=2)

    assert (project / "calculator.py").is_file()
    assert "API_KAYBI" not in prompts[1]


# ── Hata 3: bayat hata / cikti ─────────────────────────────────

def test_inspect_output_does_not_outlive_a_rewrite_of_the_file(home):
    project = home / "jarvis_programs" / "hesap"
    old = '"""SURUM_BIR: safe_eval henuz yok."""\n\n\n' + "class Calculator" + CALC.split("class Calculator")[1]
    prompts: list[str] = []
    seq = iter([
        _write("calculator.py", old),
        _decide({"action": "inspect", "args": {"filename": "calculator.py"}}),
        _write("calculator.py", CALC, "fix"),
    ])

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, _decide({"action": "inspect", "args": {}}))

    _solve(project, model, max_iterations=4)

    assert "SURUM_BIR" in prompts[2]          # inspect ciktisi bir sonraki turda gorulur
    assert "SURUM_BIR" not in prompts[3], "dosya yeniden yazildi; eski inspect ciktisi bayat"


def test_fresh_rejection_is_not_overwritten_by_unchanged_pytest_error(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []
    with_eval = CALC_BUG.replace(
        '    return walk(ast.parse(expr, mode="eval").body)\n',
        "    return eval(expr)\n")
    seq = iter([*_project_writes(TEST_RIGHT, calc=CALC_BUG),
                _write("calculator.py", with_eval, "fix")])

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, _decide({"action": "inspect", "args": {}}))

    _solve(project, model, max_iterations=8)

    after = prompts[6]       # eval reddinden sonraki tur; pytest hatasi degismedi
    assert "PYTEST BAŞARISIZ" in after
    assert "VALIDATION (calculator.py)" in after and "KISA ÖRNEK" in after, after[-2000:]
