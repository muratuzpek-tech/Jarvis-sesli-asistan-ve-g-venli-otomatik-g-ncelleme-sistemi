"""Dev Agent syntax ve kalite kapısı fail-closed regresyon testleri."""
from __future__ import annotations


import pytest

import jarvis.actions.dev_agent as da


class _Response:
    def __init__(self, text: str):
        self.text = text


class _AlwaysInvalidModel:
    def generate_content(self, prompt: str):  # noqa: ARG002
        return _Response("def broken(:\n    pass\n")


def test_second_syntax_failure_is_not_written(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "_get_model", lambda _name: _AlwaysInvalidModel())
    target = tmp_path / "main.py"

    with pytest.raises(SyntaxError):
        da._write_file(
            file_info={"path": "main.py", "description": "entry point", "imports": []},
            project_description="small Python app",
            all_files=[{"path": "main.py", "description": "entry point", "imports": []}],
            language="python",
            project_dir=tmp_path,
            already_written={},
        )

    assert not target.exists(), "İkinci syntax hatasından sonra bozuk kod diske yazılmamalı"


def test_quality_analyzer_failure_is_a_blocking_finding(monkeypatch):
    import jarvis.actions.devkit.python_quality as quality

    def _fail(_sources):
        raise RuntimeError("simulated analyzer failure")

    monkeypatch.setattr(quality, "analyze_sources", _fail)
    findings = da._python_quality_issues({"main.py": "print('ok')\n"})

    assert findings["__quality_gate__"][0]["code"] == "QUALITY-GATE-UNAVAILABLE"
    assert findings["__quality_gate__"][0]["line"] == 0
