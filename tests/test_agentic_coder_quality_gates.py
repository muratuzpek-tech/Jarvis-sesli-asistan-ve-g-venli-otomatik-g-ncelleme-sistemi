"""tests/test_agentic_coder_quality_gates.py — ruff ve eval/exec kalite kapilari

Canli hata: model main.py'de sqrt() kullandi ama import etmedi (ruff F821).
Log "ruff: main.py içinde 1 hata kaldı" dedi; README yazilinca bu hata
last_error'dan silindi, accept/SMART EXIT ruff'a hic bakmadi ve dosya oldugu
gibi kaldi ("√" tusu hep "Error"). Ayrica "=" tusu eval() ile yazilmisti.

Testler GERCEK AgenticCoder.solve dongusunu (dosya yazimi, gercek ruff,
import ve --help alt surecleri) calistirir; yalnizca LLM yerine senaryolu bir
model_fn verilir. HOME ve proje klasoru tmp_path altindadir.
"""
import asyncio
import json
import sys

import pytest

sys.path.insert(0, '.')

from tools.developer import agentic_coder as ac

pytestmark = pytest.mark.skipif(ac._ruff_cmd() is None, reason="ruff kurulu degil")

README = (
    "# Karekok Araci\n\n"
    "Bir sayinin karekokunu ve basit bir ifadenin degerini hesaplayan kucuk bir "
    "komut satiri araci. Negatif sayilar icin anlasilir bir hata verir.\n\n"
    "## Kullanim\n\n    python3 main.py\n\n"
    "## Mimari\n\nTum mantik main.py icindedir; ek bagimlilik yoktur.\n"
)

MAIN_SQRT_BROKEN = (
    '"""Karekok hesaplayan kucuk arac."""\n\n\n'
    "def karekok(sayi: float) -> float:\n"
    '    """Negatif olmayan sayinin karekokunu dondurur."""\n'
    "    if sayi < 0:\n"
    '        raise ValueError("negatif sayinin karekoku yok")\n'
    "    return sqrt(sayi)\n\n\n"
    'if __name__ == "__main__":\n'
    '    print("karekok(16) =", karekok(16))\n'
)
MAIN_SQRT_FIXED = MAIN_SQRT_BROKEN.replace(
    '"""Karekok hesaplayan kucuk arac."""\n',
    '"""Karekok hesaplayan kucuk arac."""\nfrom math import sqrt\n')

MAIN_EVAL = (
    '"""Basit ifade hesaplayici."""\n\n\n'
    "def hesapla(ifade: str) -> float:\n"
    '    """Kullanicinin yazdigi ifadeyi hesaplar ("=" tusu)."""\n'
    "    return float(eval(ifade))\n\n\n"
    'if __name__ == "__main__":\n'
    '    print("2+3 =", hesapla("2+3"))\n'
)
MAIN_LITERAL = (
    '"""Basit sayi okuyucu."""\n'
    "import ast\n\n\n"
    "def oku(metin: str) -> float:\n"
    '    """Metindeki sayiyi guvenli bicimde okur ("=" tusu icin)."""\n'
    "    return float(ast.literal_eval(metin.strip()))\n\n\n"
    'if __name__ == "__main__":\n'
    '    print("oku(\'3.5\') =", oku("3.5"))\n'
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


ACCEPT = _decide({"action": "accept", "args": {}, "response": "bitti"})


def _solve(project, model, max_iterations=8, description="karekok araci yaz"):
    coder = ac.AgenticCoder(model_fn=model, max_iterations=max_iterations)
    return asyncio.run(coder.solve(description=description, project_path=str(project)))


# ── 7) ruff'in kalan GERCEK hatalari accept / SMART EXIT'i engeller ──

def test_undefined_name_blocks_accept_until_import_is_added(home):
    project = home / "jarvis_programs" / "karekok"
    prompts: list[str] = []
    seq = iter([_write("main.py", MAIN_SQRT_BROKEN), _write("README.md", README)])
    fixed = {"done": False}

    def model(prompt: str) -> str:
        prompts.append(prompt)
        step = next(seq, None)
        if step:
            return step
        if "F821" in prompt and not fixed["done"]:
            fixed["done"] = True
            return _write("main.py", MAIN_SQRT_FIXED)
        return ACCEPT

    result = _solve(project, model)

    # README yazildiktan SONRA da hata modele gosterilmeli (eskiden siliniyordu).
    assert any("F821" in p and "sqrt" in p for p in prompts[2:]), "ruff hatasi modele geri verilmedi"
    assert fixed["done"]
    assert "Durum: BAŞARILI" in result and "ACCEPTED ✅" in result, result
    assert "from math import sqrt" in (project / "main.py").read_text()


def test_undefined_name_never_accepted_and_stops_after_three_turns(home):
    project = home / "jarvis_programs" / "karekok"
    prompts: list[str] = []
    seq = iter([_write("main.py", MAIN_SQRT_BROKEN), _write("README.md", README)])

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, ACCEPT)

    result = _solve(project, model, max_iterations=25)

    assert "NOT ACCEPTED" in result and "Durum: BAŞARISIZ" in result
    assert "F821" in result
    assert len(prompts) <= 6, f"{len(prompts)} LLM cagrisi (kota yakildi)"


def test_style_only_findings_do_not_block(home):
    project = home / "jarvis_programs" / "karekok"
    styled = MAIN_SQRT_FIXED.replace(
        "from math import sqrt\n",
        "from math import sqrt\n"
        "UZUN_ACIKLAMA = '" + "x" * 140 + "'  # E501: satir uzunlugu yalnizca stil\n")
    seq = iter([_write("main.py", styled), _write("README.md", README)])

    def model(prompt: str) -> str:
        return next(seq, ACCEPT)

    result = _solve(project, model, max_iterations=5)
    assert "ACCEPTED ✅" in result, result


def test_verify_project_reports_real_ruff_errors_only(tmp_path):
    task = ac.CodingTask(description="k", project_path=tmp_path)
    task.files_written = {"main.py": MAIN_SQRT_BROKEN, "README.md": README}
    task.expected_files = list(task.files_written)
    ok, problems = ac._verify_project(task, run_pytest=False)
    assert not ok and any("F821" in p for p in problems), problems

    task.files_written["main.py"] = "import os\n" + MAIN_SQRT_FIXED   # F401: engellememeli
    ok, problems = ac._verify_project(task, run_pytest=False)
    assert ok, problems


# ── 8) eval / exec reddi ──

EVAL_MSG = "eval/exec kullanma; ast.literal_eval ya da kendi ayrıştırıcını yaz"


@pytest.mark.parametrize("code", [
    "x = eval('1 + 2')\n",
    "exec('print(1)')\n",
    "kod = compile('print(1)', '<s>', 'exec')\nexec(kod)\n",
    "__import__('builtins').eval('1')\n",
    "import builtins\nbuiltins.exec('print(1)')\n",
    "f = getattr(__import__('builtins'), 'eval')\n",
])
def test_validate_code_rejects_eval_exec(code):
    ok, msg = ac._validate_code(code)
    assert not ok and EVAL_MSG in msg


@pytest.mark.parametrize("code", [
    "import ast\nx = ast.literal_eval('[1, 2]')\n",
    "s = 'eval(1) yalnizca bir metin'\n# exec( yorumda\n",
    "import re\np = re.compile(r'\\d+')\n",
    "class Model:\n    def eval(self):\n        return 1\n\nModel().eval()\n",
])
def test_validate_code_accepts_safe_code(code):
    ok, msg = ac._validate_code(code)
    assert ok, msg


def test_eval_file_rejected_in_loop_and_safe_rewrite_accepted(home):
    project = home / "jarvis_programs" / "hesap"
    prompts: list[str] = []
    seq = iter([_write("main.py", MAIN_EVAL)])
    state = {"rewritten": False, "readme": False}

    def model(prompt: str) -> str:
        prompts.append(prompt)
        step = next(seq, None)
        if step:
            return step
        if EVAL_MSG in prompt and not state["rewritten"]:
            state["rewritten"] = True
            return _write("main.py", MAIN_LITERAL)
        if not state["readme"]:
            state["readme"] = True
            return _write("README.md", README)
        return ACCEPT

    result = _solve(project, model)

    assert any(EVAL_MSG in p for p in prompts), "eval reddi modele gosterilmedi"
    assert "eval(" not in (project / "main.py").read_text().replace("literal_eval(", "")
    assert "ACCEPTED ✅" in result, result
