"""dev_agent Python akışı + kalite kapısı uçtan uca (sahte model, gerçek çalıştırma)."""
from __future__ import annotations

import pytest

import jarvis.actions.dev_agent as da

PLAN = {
    "project_name": "notes_app",
    "entry_point": "main.py",
    "files": [
        {"path": "store.py", "description": "save_note/load_notes", "imports": []},
        {"path": "main.py", "description": "entry point", "imports": ["store"]},
    ],
    "run_command": "python main.py",
    "dependencies": [],
    "expected_outputs": [{"path": "report.txt", "description": "lists saved notes"}],
}

STUB_STORE = '''
def save_note(text: str) -> None:
    """Save a note."""
    # Placeholder for save logic
    pass


def load_notes() -> list:
    """Load notes."""
    return []
'''

REAL_STORE = '''
import json
from pathlib import Path

DB = Path("notes.json")


def save_note(text: str) -> None:
    """Save a note."""
    notes = load_notes()
    notes.append(text)
    DB.write_text(json.dumps(notes), encoding="utf-8")


def load_notes() -> list:
    """Load notes."""
    return json.loads(DB.read_text(encoding="utf-8")) if DB.exists() else []
'''

MAIN = '''
from store import save_note, load_notes


def main() -> None:
    save_note("ilk not")
    save_note("ikinci not")
    with open("report.txt", "w", encoding="utf-8") as fh:
        fh.write("Notlar\\n======\\n")
        for n in load_notes():
            fh.write(f"- {n}\\n")


if __name__ == "__main__":
    main()
'''


class FakeModel:
    def __init__(self, store_versions):
        self.store_versions = list(store_versions)
        self.prompts = []

    def generate_content(self, prompt):
        self.prompts.append(prompt)

        class R:
            pass

        r = R()
        if "Code for main.py" in prompt or "Fixed code for main.py" in prompt:
            r.text = MAIN
        elif "store.py" in prompt:
            r.text = self.store_versions.pop(0) if len(self.store_versions) > 1 else self.store_versions[0]
        else:
            r.text = MAIN
        return r


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(da, "PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(da, "_plan_project", lambda d, lang: dict(PLAN))
    monkeypatch.setattr(da, "_open_vscode", lambda p: False)
    monkeypatch.setattr(da, "_search_error_context", lambda out: "")
    monkeypatch.setattr(da, "_proactively_lint_generated_files", lambda d, c: None)  # ruff'a bağımlı olmasın
    monkeypatch.setattr(da.time, "sleep", lambda s: None)
    return tmp_path


def test_stub_code_is_rejected_then_fixed(env, monkeypatch):
    model = FakeModel([STUB_STORE, STUB_STORE, REAL_STORE])
    monkeypatch.setattr(da, "_get_model", lambda name: model)
    result = da._build_project("not uygulaması", "python", "", 10, player=None, speak=None)
    assert "is working" in result, result
    fix_prompts = [p for p in model.prompts if p.startswith("You are an expert python debugger")]
    assert fix_prompts and "STUB-FUNCTION" in fix_prompts[0]
    proj = env / "notes_app"
    assert "ikinci not" in (proj / "report.txt").read_text(encoding="utf-8")
    # yedekler proje köküne değil .jarvis/backups altına
    assert not list(proj.glob("*.bak*"))
    assert list((proj / ".jarvis" / "backups").iterdir())


def test_never_reports_success_while_stubs_remain(env, monkeypatch):
    model = FakeModel([STUB_STORE])
    monkeypatch.setattr(da, "_get_model", lambda name: model)
    result = da._build_project("not uygulaması", "python", "", 10, player=None, speak=None)
    assert "is working" not in result
    assert "QUALITY GATE FAILED" in result
