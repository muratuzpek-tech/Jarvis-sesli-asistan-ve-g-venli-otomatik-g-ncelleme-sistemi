"""tests/test_agentic_coder_locked.py — "locked" atlamasi gorunur ve donguyu keser

Canli hata: model main.py'yi 2 kez basariyla yazdi, sonra turlar 5-25 boyunca
yine main.py yazmaya calisti. Kod rewrite_counts > 3 kosulunda sessizce
`continue` ediyordu (ekrana ve modele hicbir sey gitmiyordu). README.md ve
tests/ hic yazilmadi, 25 tur bosa gitti.

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


def _main(i: int) -> str:
    """Her turda biraz farkli, gecerli ve ruff-temiz main.py."""
    return (
        f'"""Hesap makinesi, surum {i}."""\n\n\n'
        "def topla(a: float, b: float) -> float:\n"
        '    """Iki sayiyi toplar."""\n'
        "    return a + b\n\n\n"
        "def main() -> None:\n"
        '    """Giris noktasi: ornek bir toplama yazdirir."""\n'
        f'    print("surum {i}, sonuc:", topla(2, 3))\n\n\n'
        'if __name__ == "__main__":\n'
        "    main()\n"
    )


CALC = (
    '"""Hesaplama cekirdegi."""\n\n\n'
    "def topla(a: float, b: float) -> float:\n"
    '    """Iki sayiyi toplar ve sonucu dondurur."""\n'
    "    return a + b\n\n\n"
    "def carp(a: float, b: float) -> float:\n"
    '    """Iki sayiyi carpar ve sonucu dondurur."""\n'
    "    return a * b\n"
)

TEST_CORE = (
    '"""calc modulu testleri."""\n'
    "from calc import carp, topla\n\n\n"
    "def test_topla() -> None:\n"
    "    assert topla(2, 3) == 5\n\n\n"
    "def test_carp() -> None:\n"
    "    assert carp(2, 3) == 6\n"
)

NARROW = "SADECE tests/test_core.py"   # dar test prompt'unun imzasi


# ── 1) locked atlamasi ekrana ve modele net talimat olarak gider ──

def test_locked_skip_is_shown_and_tells_model_next_file(home):
    project = home / "jarvis_programs" / "hesap"
    ui = _UI()
    prompts: list[str] = []
    seq = iter([_write("main.py", _main(n)) for n in range(1, 5)])

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return next(seq, _decide({"action": "inspect", "args": {}}))

    _solve(project, model, ui=ui, max_iterations=5)

    shown = [ln for ln in ui.lines if "⛔" in ln and "kilitli" in ln]
    assert shown, ui.lines
    assert "⛔ main.py kilitli (zaten yazıldı), şimdi README.md yaz" in shown[0]
    # 4. deneme kilitlendi -> 5. prompt talimati tasir
    assert "main.py TAMAM ve kilitli, TEKRAR YAZMA. Şimdi SADECE README.md yaz." in prompts[4]


# ── 2+4) model talimati 2 kez gormezden gelir -> README.md sablonu LLM'siz ──

def test_repeated_lock_writes_readme_template_and_accepts_fast(home):
    project = home / "jarvis_programs" / "hesap"
    ui = _UI()
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return _write("main.py", _main(len(prompts)))   # hep main.py

    result = _solve(project, model, ui=ui)

    readme = project / "README.md"
    assert readme.is_file(), ui.lines
    text = readme.read_text(encoding="utf-8")
    assert len(text) >= 250
    assert "hesap makinesi yaz" in text
    assert "main.py" in text and "python3 main.py" in text
    # README 5. denemede (2. kilit) yazildi; 25 tur yanmadi
    assert len(prompts) <= 10, f"{len(prompts)} LLM cagrisi"
    assert "Durum: BAŞARILI" in result, result


# ── 3) toplam kilit 4'u gecerse (LLM'siz uretilemeyen dosya) gorev biter ──

def test_too_many_locks_fail_the_task(home):
    project = home / "jarvis_programs" / "proje"
    prompts: list[str] = []

    def model(prompt: str) -> str:
        prompts.append(prompt)
        return _write("main.py", _main(len(prompts)))

    result = _solve(project, model, description="main.py ve calc.py yaz")

    # 3 basarili yazim + 5 kilit (5. kilit limiti asar)
    assert len(prompts) == 8, f"{len(prompts)} LLM cagrisi"
    assert "Durum: BAŞARISIZ" in result
    assert "döngü: main.py kilitli, model calc.py'ya geçmedi" in result


# ── 2) tests/__init__.py bos, tests/test_core.py dar prompt ile ──

def test_lock_fills_readme_tests_init_and_test_core(home):
    project = home / "jarvis_programs" / "proje"
    prompts: list[str] = []
    narrow: list[str] = []

    def model(prompt: str) -> str:
        if NARROW in prompt:
            narrow.append(prompt)
            return _write("tests/test_core.py", TEST_CORE)
        prompts.append(prompt)
        if len(prompts) == 1:
            return _write("calc.py", CALC)
        return _write("main.py", _main(len(prompts)))

    result = _solve(project, model, description="main.py ve calc.py yaz")

    assert (project / "README.md").is_file()
    assert (project / "tests" / "__init__.py").read_text() == ""
    assert (project / "tests" / "test_core.py").read_text().count("def test_") == 2
    assert len(narrow) == 1
    # dar prompt: sadece o dosya + yazilmis modullerin adlari
    assert "calc" in narrow[0] and "topla" in narrow[0]
    assert "## GÖREV" not in narrow[0] and len(narrow[0]) < len(prompts[0])
    assert len(prompts) <= 10, f"{len(prompts)} LLM cagrisi"
    assert "Durum: BAŞARILI" in result, result


def test_test_core_dropped_after_two_failed_attempts(home):
    project = home / "jarvis_programs" / "proje"
    prompts: list[str] = []
    narrow: list[str] = []

    def model(prompt: str) -> str:
        if NARROW in prompt:
            narrow.append(prompt)
            return "bunu yazamiyorum"
        prompts.append(prompt)
        if len(prompts) == 1:
            return _write("calc.py", CALC)
        return _write("main.py", _main(len(prompts)))

    result = _solve(project, model, description="main.py ve calc.py yaz")

    assert len(narrow) == 2
    assert "tests/test_core.py yazılamadı, atlandı" in result
    assert len(prompts) <= 10, f"{len(prompts)} LLM cagrisi"
    assert "Eksik dosya: tests/test_core.py" not in result
    assert "Durum: BAŞARILI" in result, result


# ── kilit, ruff hatasi olan dosyayi duzeltmeyi engellememeli ──

def test_file_with_ruff_errors_is_not_locked(home):
    project = home / "jarvis_programs" / "proje"
    ui = _UI()
    bad = _main(0).replace("return a + b", "return a + bilinmeyen")
    # araya calc.py: ust uste ayni dosya (ZORLA ROTATE) devreye girmesin
    seq = iter([_write("main.py", _main(1)), _write("main.py", _main(2)),
                _write("calc.py", CALC), _write("main.py", bad), _write("main.py", _main(4))])

    def model(prompt: str) -> str:
        return next(seq, _decide({"action": "inspect", "args": {}}))

    _solve(project, model, ui=ui, max_iterations=6, description="main.py ve calc.py yaz")

    assert not [ln for ln in ui.lines if "kilitli" in ln], ui.lines
    assert "surum 4" in (project / "main.py").read_text()
