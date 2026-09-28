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
