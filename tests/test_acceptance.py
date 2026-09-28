"""Kabul testi modülü testleri (gerçek Python alt süreci ile)."""
from __future__ import annotations

import json

from jarvis.actions.devkit.acceptance import (
    contract_text,
    parse_spec,
    run_acceptance,
    validate_spec,
)

SPEC = {
    "applicable": True,
    "fixtures": [
        {"path": "sample/utils.py", "content": "def empty_func():\n    \"\"\"doc\"\"\"\n    return []\n"},
        {"path": "sample/old.bak.py", "content": "x = 1\n"},
    ],
    "args": ["{FIXTURE}/sample"],
    "input_contract": "First argument is the folder.",
    "expect": [{"output": "report.txt", "contains": ["empty_func", "old.bak.py"]}],
}

GOOD_PROGRAM = '''
import ast, sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "/home/murat/x")
lines = []
for p in sorted(root.rglob("*")):
    if ".bak" in p.name:
        lines.append(f"backup: {p.name}")
    elif p.suffix == ".py":
        for n in ast.walk(ast.parse(p.read_text())):
            if isinstance(n, ast.FunctionDef):
                lines.append(f"stub: {n.name}")
Path("report.txt").write_text("\\n".join(lines) + "\\n")
'''

HARDCODED_PROGRAM = '''
from pathlib import Path
Path("report.txt").write_text("stub: something_else\\n")
'''


def test_validate_rejects_escapes_and_accepts_good():
    spec, why = validate_spec(json.loads(json.dumps(SPEC)))
    assert spec and why == "ok"
    for bad in (
        {**SPEC, "fixtures": [{"path": "../evil.py", "content": ""}]},
        {**SPEC, "fixtures": [{"path": "/etc/x", "content": ""}]},
        {**SPEC, "expect": [{"output": "../../x", "contains": ["a"]}]},
        {**SPEC, "expect": [{"output": "r.txt", "contains": []}]},
        {**SPEC, "args": ["{FIXTURE}/../../etc"]},
        {**SPEC, "fixtures": [{"path": "big.py", "content": "x" * 9000}]},
        {"applicable": False, "reason": "GUI"},
        "not a dict",
    ):
        assert validate_spec(bad)[0] is None, bad


def test_parse_spec_handles_fences_and_prose():
    text = "Sure!\n```json\n" + json.dumps(SPEC) + "\n```\nDone."
    assert parse_spec(text)["applicable"] is True
    assert parse_spec("no json here") is None


def test_contract_mentions_args():
    spec, _ = validate_spec(SPEC)
    assert "<sample_folder>/sample" in contract_text(spec)


def test_good_program_passes(tmp_path):
    (tmp_path / "main.py").write_text(GOOD_PROGRAM)
    (tmp_path / "report.txt").write_text("REAL USER REPORT")
    spec, _ = validate_spec(SPEC)
    problems, _ = run_acceptance(tmp_path, "main.py", spec)
    assert problems == []
    # gerçek çıktı ezilmedi, test ayrı klasörde koştu
    assert (tmp_path / "report.txt").read_text() == "REAL USER REPORT"
    assert (tmp_path / ".jarvis/acceptance/run/report.txt").is_file()


def test_hardcoded_program_fails_with_clear_message(tmp_path):
    (tmp_path / "main.py").write_text(HARDCODED_PROGRAM)
    spec, _ = validate_spec(SPEC)
    problems, _ = run_acceptance(tmp_path, "main.py", spec)
    assert len(problems) == 1 and "empty_func" in problems[0] and "old.bak.py" in problems[0]


def test_crash_and_missing_output_reported(tmp_path):
    (tmp_path / "main.py").write_text("raise SystemExit(3)")
    spec, _ = validate_spec(SPEC)
    problems, _ = run_acceptance(tmp_path, "main.py", spec)
    assert any("hata koduyla" in p for p in problems) and any("oluşturulmadı" in p for p in problems)


def test_output_written_next_to_script_gets_precise_hint(tmp_path):
    (tmp_path / "main.py").write_text(
        "from pathlib import Path\n"
        "(Path(__file__).parent / 'report.txt').write_text('empty_func old.bak.py')\n"
    )
    spec, _ = validate_spec(SPEC)
    problems, _ = run_acceptance(tmp_path, "main.py", spec)
    assert len(problems) == 1 and "kendi klasörüne" in problems[0] and "report.txt" in problems[0]


def test_llm_miscount_in_spec_is_corrected():
    # Canlı testteki gerçek örnek: model 'hello: 3' dedi, doğrusu 4.
    spec = {"applicable": True,
            "fixtures": [{"path": "metinler/file1.txt", "content": "hello world hello"},
                         {"path": "metinler/file2.txt", "content": "hello universe"},
                         {"path": "metinler/file3.txt", "content": "world hello"}],
            "args": ["{FIXTURE}/metinler"],
            "expect": [{"output": "report.txt", "contains": ["hello: 3", "world: 2", "universe: 1"]}]}
    clean, _ = validate_spec(spec)
    assert clean["expect"][0]["contains"] == ["hello: 4", "world: 2", "universe: 1"]


def test_counts_in_csv_fixtures_are_not_touched():
    spec = {"applicable": True, "fixtures": [{"path": "s.csv", "content": "urun,adet\nkalem,2\nkalem,3\n"}],
            "args": ["{FIXTURE}/s.csv"], "expect": [{"output": "o.json", "contains": ["kalem: 5"]}]}
    assert validate_spec(spec)[0]["expect"][0]["contains"] == ["kalem: 5"]


def test_script_dir_paths_are_rewritten_to_cwd():
    from jarvis.actions.devkit.acceptance import rewrite_script_dir_paths
    codes = {
        "utils/helpers.py": "from pathlib import Path\np = Path(__file__).parent / filename\n",
        "a.py": "import os\nd = os.path.dirname(os.path.abspath(__file__))\n",
        "b.py": "x = os.path.dirname(__file__)\n",
        "c.py": "print('no file refs')\n",
    }
    out = rewrite_script_dir_paths(codes)
    assert out["utils/helpers.py"].endswith("p = Path.cwd() / filename\n")
    assert "os.getcwd()" in out["a.py"] and "os.getcwd()" in out["b.py"] and out["b.py"].startswith("import os\n")
    assert "c.py" not in out


def test_rewritten_program_passes_acceptance(tmp_path):
    from jarvis.actions.devkit.acceptance import rewrite_script_dir_paths
    code = "from pathlib import Path\n(Path(__file__).parent / 'report.txt').write_text('empty_func old.bak.py')\n"
    (tmp_path / "main.py").write_text(rewrite_script_dir_paths({"main.py": code})["main.py"])
    spec, _ = validate_spec(SPEC)
    assert run_acceptance(tmp_path, "main.py", spec)[0] == []


def test_file_written_just_before_acceptance_is_not_blamed(tmp_path):
    """Gerçek çalıştırma çıktıyı proje köküne biraz önce yazdıysa, kabul testinde
    çıktı üretilmemesi 'yanlış klasör' diye raporlanmamalı."""
    (tmp_path / "report.txt").write_text("from the real run")
    (tmp_path / "main.py").write_text("print('nothing written')\n")
    spec, _ = validate_spec(SPEC)
    problems, _ = run_acceptance(tmp_path, "main.py", spec)
    assert problems and "kendi klasörüne" not in problems[0] and "oluşturulmadı" in problems[0]


def test_requests_style_scraper_can_read_fixture_url(tmp_path):
    spec, why = validate_spec({
        "applicable": True,
        "fixtures": [{"path": "page.html", "content": "<p class='q'>Birinci</p><p class='q'>Ikinci</p>"}],
        "args": ["{FIXTURE_URL}/page.html"],
        "expect": [{"output": "quotes.csv", "contains": ["Birinci", "Ikinci"]}],
    })
    assert spec, why
    (tmp_path / "main.py").write_text(
        "import re, sys, urllib.request\n"
        "html = urllib.request.urlopen(sys.argv[1], timeout=5).read().decode()\n"
        "open('quotes.csv', 'w').write('\\n'.join(re.findall(r\"<p class='q'>(.*?)</p>\", html)))\n"
    )
    problems, output = run_acceptance(tmp_path, "main.py", spec)
    assert problems == [], (problems, output)


def test_bare_html_tag_token_accepts_attributes():
    from jarvis.actions.devkit.acceptance import _token_found
    assert _token_found("<table>", "<table border='1'><tr><td>elma</td></tr></table>")
    assert _token_found("<TABLE>", "<table\n class=x>")
    assert not _token_found("<table>", "<tablet>")
    assert not _token_found("<table>", "tablo yok")


def test_fixture_mtime_is_applied(tmp_path):
    spec, why = validate_spec({
        "applicable": True,
        "fixtures": [{"path": "foto/a.jpg", "content": "x", "mtime": "2024-01-15"}],
        "args": ["{FIXTURE}/foto"],
        "expect": [{"output": "STDOUT", "contains": ["2024-01"]}],
    })
    assert spec, why
    (tmp_path / "main.py").write_text(
        "import sys, datetime, pathlib\n"
        "for p in pathlib.Path(sys.argv[1]).iterdir():\n"
        "    print(datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime('%Y-%m'))\n")
    assert run_acceptance(tmp_path, "main.py", spec)[0] == []
    assert validate_spec({**spec, "applicable": True,
                          "fixtures": [{"path": "a", "content": "", "mtime": "dün"}]})[0] is None


# Dış analiz raporu (2026-09-29): klasör ağacı üreten doğru program
# "oluşturulmadı" diye reddediliyordu.
TREE_SPEC = {
    "applicable": True,
    "fixtures": [{"path": "foto/a.jpg", "content": "x", "mtime": "2024-01-15"},
                 {"path": "foto/c.jpg", "content": "y", "mtime": "2024-03-02"}],
    "args": ["{FIXTURE}/foto"],
    "expect": [{"output": "sorted", "contains": ["2024-01/a.jpg", "2024-03/c.jpg"]}],
}
TREE_PROGRAM = '''
import shutil, sys, datetime
from pathlib import Path
for p in Path(sys.argv[1]).iterdir():
    d = Path("{root}") / datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m")
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, d / p.name)
'''


def test_folder_tree_output_is_accepted(tmp_path):
    spec, why = validate_spec(TREE_SPEC)
    assert spec, why
    (tmp_path / "main.py").write_text(TREE_PROGRAM.replace("{root}", "sorted"))
    assert run_acceptance(tmp_path, "main.py", spec)[0] == []


def test_folder_tree_missing_file_is_rejected(tmp_path):
    spec, _ = validate_spec(TREE_SPEC)
    (tmp_path / "main.py").write_text(TREE_PROGRAM.replace("{root}", "sorted").replace(
        "for p in Path(sys.argv[1]).iterdir():", "for p in sorted(Path(sys.argv[1]).iterdir())[:1]:"))
    problems, _ = run_acceptance(tmp_path, "main.py", spec)
    assert len(problems) == 1 and "2024-03/c.jpg" in problems[0]


def test_empty_output_folder_is_rejected(tmp_path):
    spec, _ = validate_spec(TREE_SPEC)
    (tmp_path / "main.py").write_text("import os; os.makedirs('sorted')")
    problems, _ = run_acceptance(tmp_path, "main.py", spec)
    assert len(problems) == 1 and "hiç dosya yok" in problems[0]


def test_folder_tree_written_next_to_script_is_detected(tmp_path):
    """copy2 eski tarihi korusa bile (ctime) yanlış yere yazılan klasör bulunur."""
    spec, _ = validate_spec(TREE_SPEC)
    (tmp_path / "main.py").write_text(TREE_PROGRAM.replace('Path("{root}")', 'Path(__file__).parent / "sorted"'))
    problems, _ = run_acceptance(tmp_path, "main.py", spec)
    assert len(problems) == 1 and "kendi klasörüne" in problems[0]


def test_token_that_is_the_output_path_itself_is_satisfied(tmp_path):
    """Canlı test 2026-09-29: beklenti 'sirali/2024-01/a.jpg' içinde 'a.jpg' arıyordu;
    kopyalanan resmin içeriğinde kendi adı geçmez, doğru program 3 tur reddedildi."""
    spec, _ = validate_spec({**TREE_SPEC, "expect": [{"output": "sorted/2024-01/a.jpg", "contains": ["a.jpg"]}]})
    (tmp_path / "main.py").write_text(TREE_PROGRAM.replace("{root}", "sorted"))
    assert run_acceptance(tmp_path, "main.py", spec)[0] == []
