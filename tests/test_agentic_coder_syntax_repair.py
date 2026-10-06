"""tests/test_agentic_coder_syntax_repair.py — SYNTAX_ERROR geri bildirimi + tirnak onarimi

Canli hata (~/jarvis_programs/tkinter_pencereli_dugmeli/_reddedilen_calculator.py.txt):
dugme listesinde '*", '-", '+" gibi tek tirnakla acilip cift tirnakla kapanan
dizeler vardi. Ret mesaji yalnizca "SYNTAX_ERROR: line 14: unterminated string
literal" diyordu; model hatali satiri goremedi, 3 turda ayni hatayi tekrarladi.
Ayrica 1. tur YASAK(eval), 2.-3. tur SYNTAX_ERROR idi ve gorev "3 kez
reddedildi" diye durdu: sayac farkli ret turlerini ayni "VALIDATION"
kategorisinde sayiyordu.

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


ACCEPT = _decide({"action": "accept", "args": {}, "response": "bitti"})


def _scenario(*answers, prompts=None):
    seq = iter(answers)

    def model(prompt: str) -> str:
        if prompts is not None:
            prompts.append(prompt)
        return next(seq, ACCEPT)
    return model


def _solve(model, project, description="islemler.py dosyasında işlem tablosu yaz",
           ui=None, max_iterations=6):
    coder = ac.AgenticCoder(model_fn=model, ui=ui, max_iterations=max_iterations)
    return asyncio.run(coder.solve(description=description, project_path=str(project)))


def _block(result: str) -> str:
    return result.split("\n\n", 1)[0]


# Gecerli modul; satir 14-16 canli dosyadaki gibi karisik tirnakla bozulur.
VALID = '''"""Dort islem tablosu: tus etiketleri ve islemler (GIZLI_ILK_SATIR)."""
import operator

ISLEMLER = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": operator.truediv,
}

# Hesap makinesi tus duzeni (satir satir).
TUSLAR = [
    '7', '8', '9', '/',
    '4', '5', '6', '*',
    '1', '2', '3', '-',
    '0', '.', '=', '+',
]


def uygula(sol: float, isaret: str, sag: float) -> float:
    """Isarete karsilik gelen islemi uygular."""
    if isaret not in ISLEMLER:
        raise ValueError(f"bilinmeyen islem: {isaret}")
    return ISLEMLER[isaret](sol, sag)
'''

BROKEN = (VALID.replace("'6', '*',", "'6', '*\",")
               .replace("'3', '-',", "'3', '-\",")
               .replace("'=', '+',", "'=', '+\","))

README = (
    "# Islem tablosu\n\nislemler.py dort islemi ve hesap makinesi tus etiketlerini "
    "tanimlar. uygula fonksiyonu iki sayiya isarete karsilik gelen islemi uygular; "
    "bilinmeyen isaret ValueError verir.\n\n## Kullanim\n\n```python\nfrom islemler "
    "import uygula\n```\n"
)

EVAL_LINE = "\n\ndef hesapla(ifade: str) -> float:\n    \"\"\"Ifadeyi hesaplar.\"\"\"\n    return eval(ifade)\n"


def test_broken_fixture_matches_live_lines():
    lines = BROKEN.splitlines()
    assert lines[13].strip() == "'4', '5', '6', '*\","
    assert lines[14].strip() == "'1', '2', '3', '-\","
    assert lines[15].strip() == "'0', '.', '=', '+\","
    with pytest.raises(SyntaxError):
        ast.parse(BROKEN)


# ── 1) SYNTAX_ERROR mesaji hatali satiri, sutunu ve ipucunu gosterir ──

def test_syntax_error_message_shows_line_column_and_hint(home):
    project = home / "jarvis_programs" / "islem"
    # Onarilamaz: kapanis tirnagi hic yok.
    bad = VALID.replace("'6', '*',", "'6', '*,")
    prompts: list[str] = []
    ui = _UI()
    _solve(_scenario(_write("islemler.py", bad), prompts=prompts), project, ui=ui,
           max_iterations=2)

    p = prompts[1]
    assert "SYNTAX_ERROR: line 14" in p
    assert "satır 14: '4', '5', '6', '*," in p          # bastaki bosluk kirpildi
    line_row = next(ln for ln in p.splitlines() if ln.startswith("  satır 14: "))
    caret_row = p.splitlines()[p.splitlines().index(line_row) + 1]
    assert caret_row.rstrip().endswith("^") or "^" in caret_row
    assert caret_row.index("^") == line_row.index("'*,")   # sutun isareti acilis tirnaginda
    assert "tırnaklar eşleşmiyor" in p
    # Dosya icerigi DEGIL yalnizca o tek satir.
    assert "GIZLI_ILK_SATIR" not in p
    assert "def uygula" not in p
    shown = [m for m in ui.lines if "islemler.py reddedildi" in m]
    assert shown and "'4', '5', '6', '*," in shown[0] and "tırnaklar eşleşmiyor" in shown[0]
    assert "GIZLI_ILK_SATIR" not in "\n".join(shown)


@pytest.mark.parametrize("code,hint", [
    ("x = (1,\ny = 2\n", "parantez"),
    ("def f():\nreturn 1\n", "girinti"),
    ("x = 1\n    y = 2\n", "girinti"),
])
def test_syntax_error_hints(code, hint):
    ok, msg = ac._validate_code(code)
    assert not ok and msg.startswith("SYNTAX_ERROR")
    assert hint in msg
    assert "satır " in msg and "^" in msg


def test_long_error_line_is_capped_to_120_chars():
    code = "x = [" + ", ".join(f"'{i:03d}'" for i in range(60)) + ", 'son\n"
    ok, msg = ac._validate_code(code)
    assert not ok
    row = next(ln for ln in msg.splitlines() if ln.startswith("  satır 1: "))
    assert len(row[len("  satır 1: "):]) <= 120
    assert "^" in msg.splitlines()[msg.splitlines().index(row) + 1]


# ── 2) Guvenli tirnak onarimi ─────────────────────────────────

def test_live_mixed_quotes_are_repaired_and_written(home):
    project = home / "jarvis_programs" / "islem"
    ui = _UI()
    result = _solve(_scenario(_write("islemler.py", BROKEN), _write("README.md", README), ACCEPT),
                    project, ui=ui)

    assert "Durum: BAŞARILI" in _block(result), result
    on_disk = (project / "islemler.py").read_text(encoding="utf-8")
    # ruff bicimlendirir; icerik anlamca gecerli (onarilmis) halle ayni olmali.
    assert ast.dump(ast.parse(on_disk)) == ast.dump(ast.parse(VALID))
    assert any("🔧 tırnak onarıldı: satır 14" in m for m in ui.lines), ui.lines
    assert not (project / "_reddedilen_islemler.py.txt").exists()


def test_repaired_content_still_goes_through_eval_check(home):
    project = home / "jarvis_programs" / "islem"
    ui = _UI()
    prompts: list[str] = []
    _solve(_scenario(_write("islemler.py", BROKEN + EVAL_LINE), prompts=prompts), project,
           ui=ui, max_iterations=2)

    assert any("🔧 tırnak onarıldı" in m for m in ui.lines)
    assert "eval/exec kullanma" in prompts[1]
    assert "SYNTAX_ERROR" not in prompts[1]
    saved = (project / "_reddedilen_islemler.py.txt").read_text(encoding="utf-8")
    assert "'6', '*'," in saved            # reddedilen kopya onarilmis icerik


@pytest.mark.parametrize("code", [
    VALID,
    'MESAJ = "it\'s fine"\nDIGER = \'say "hi"\'\n',
    '"""Docstring \'tek\' ve "cift" tirnak."""\n\n\ndef f() -> str:\n    """It\'s."""\n    return f"{1!r}\'"\n',
    'METIN = """birinci satir \'\nikinci satir "\n"""\nB = \'\'\'x "\ny\'\'\'\n',
])
def test_valid_code_is_never_touched(code):
    ast.parse(code)
    assert ac._repair_mixed_quotes(code) is None


@pytest.mark.parametrize("code", [
    "x = 'abc\n",                                         # kapanis tirnagi yok
    "x = ['a\", 'b']\ny = (1,\n",                          # onarilsa da baska hata
    "x = '''acik kalan\n",                                # uc tirnakli
    "def f(:\n    return 'a\"\n",                         # once baska hata
])
def test_unrepairable_code_is_not_changed(code):
    assert ac._repair_mixed_quotes(code) is None


def test_repair_returns_repaired_lines():
    fixed, lines = ac._repair_mixed_quotes(BROKEN)
    assert fixed == VALID
    assert lines == [14, 15, 16]


def test_unrepairable_file_is_rejected_unchanged(home):
    project = home / "jarvis_programs" / "islem"
    bad = BROKEN.replace("def uygula(sol: float", "def uygula(sol: float,,")
    ui = _UI()
    _solve(_scenario(_write("islemler.py", bad)), project, ui=ui, max_iterations=2)

    assert not any("tırnak onarıldı" in m for m in ui.lines)
    assert (project / "_reddedilen_islemler.py.txt").read_text(encoding="utf-8") == bad
    assert not (project / "islemler.py").exists()


# ── 3) Ret sayaci: ayni dosya + AYNI kategori art arda ────────

def _syntax_bad(n: int) -> str:
    return VALID.replace("'6', '*',", f"'6', '*{n}") + f"\n# v{n}\n"


def test_mixed_rejection_kinds_do_not_stop_task(home):
    project = home / "jarvis_programs" / "islem"
    ui = _UI()
    model = _scenario(_write("islemler.py", VALID + EVAL_LINE),       # YASAK (eval)
                      _write("islemler.py", _syntax_bad(1)),          # SYNTAX_ERROR
                      _write("islemler.py", _syntax_bad(2)),          # SYNTAX_ERROR
                      _write("islemler.py", VALID), _write("README.md", README), ACCEPT)
    result = _solve(model, project, ui=ui, max_iterations=8)

    assert "kez reddedildi" not in result, result
    assert "Durum: BAŞARILI" in _block(result), result


def test_same_category_three_times_still_stops(home):
    project = home / "jarvis_programs" / "islem"
    model = _scenario(*[_write("islemler.py", _syntax_bad(n)) for n in range(5)])
    result = _solve(model, project, max_iterations=8)

    assert f"islemler.py {ac._REJECT_LIMIT} kez reddedildi" in result, result
    assert "Durum: BAŞARISIZ" in _block(result)
