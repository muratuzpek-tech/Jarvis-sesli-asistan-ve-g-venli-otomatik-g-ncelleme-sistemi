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


# ── kabul testi + arşivleme ──────────────────────────────────────────────
import json  # noqa: E402

WC_PLAN = {
    "project_name": "word_counter",
    "entry_point": "main.py",
    "files": [{"path": "main.py", "description": "counts words", "imports": []}],
    "run_command": "python main.py /home/murat/notlar.txt",
    "dependencies": [],
    "expected_outputs": [{"path": "report.txt", "description": "word count"}],
}

WC_SPEC = {
    "applicable": True,
    "fixtures": [{"path": "in.txt", "content": "elma armut elma kiraz elma"}],
    "args": ["{FIXTURE}/in.txt"],
    "input_contract": "First argument is the text file.",
    "expect": [{"output": "report.txt", "contains": ["elma: 3", "kiraz: 1"]}],
}

WC_HARDCODED = '''
import collections
from pathlib import Path


def main() -> None:
    text = "tek kelime"  # girdi dosyasini hic okumuyor
    counts = collections.Counter(text.split())
    lines = ["Kelime sayimi", "============"] + [f"{w}: {c}" for w, c in counts.most_common()]
    Path("report.txt").write_text("\\n".join(lines) + "\\n", encoding="utf-8")


if __name__ == "__main__":
    main()
'''

WC_REAL = '''
import collections
import sys
from pathlib import Path


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("notlar.txt")
    text = src.read_text(encoding="utf-8") if src.exists() else "bos"
    counts = collections.Counter(text.split())
    lines = ["Kelime sayimi", "============"] + [f"{w}: {c}" for w, c in counts.most_common()]
    Path("report.txt").write_text("\\n".join(lines) + "\\n", encoding="utf-8")


if __name__ == "__main__":
    main()
'''


class AcceptanceModel:
    def __init__(self, versions):
        self.versions = list(versions)
        self.prompts = []

    def generate_content(self, prompt):
        self.prompts.append(prompt)

        class R:
            pass

        r = R()
        if prompt.startswith("You design an ACCEPTANCE TEST"):
            r.text = "```json\n" + json.dumps(WC_SPEC) + "\n```"
        else:
            r.text = self.versions.pop(0) if len(self.versions) > 1 else self.versions[0]
        return r


def test_acceptance_catches_wrong_result_and_old_project_is_archived(env, monkeypatch):
    monkeypatch.setattr(da, "_plan_project", lambda d, lang: dict(WC_PLAN))
    old = env / "word_counter"
    old.mkdir()
    (old / "eski.py").write_text("# önceki denemeden kalan dosya")
    model = AcceptanceModel([WC_HARDCODED, WC_REAL])
    monkeypatch.setattr(da, "_get_model", lambda name: model)

    result = da._build_project("kelime sayici", "python", "", 10, player=None, speak=None)

    assert "is working" in result and "Acceptance test" in result, result
    fix = [p for p in model.prompts if p.startswith("You are an expert python debugger")]
    assert fix and "ACCEPTANCE-FAILED" in fix[0] and "elma: 3" in fix[0]
    proj = env / "word_counter"
    assert not (proj / "eski.py").exists()
    archived = list((env / ".arsiv").iterdir())
    assert len(archived) == 1 and (archived[0] / "eski.py").exists()
    assert "elma: 3" in (proj / ".jarvis/acceptance/run/report.txt").read_text(encoding="utf-8")


def test_acceptance_failure_is_reported_honestly(env, monkeypatch):
    monkeypatch.setattr(da, "_plan_project", lambda d, lang: dict(WC_PLAN))
    model = AcceptanceModel([WC_HARDCODED])
    monkeypatch.setattr(da, "_get_model", lambda name: model)
    result = da._build_project("kelime sayici", "python", "", 10, player=None, speak=None)
    assert "is working" not in result and "ACCEPTANCE TEST FAILED" in result


WC_SCRIPT_DIR = WC_REAL.replace(
    'Path("report.txt").write_text', '(Path(__file__).parent / "report.txt").write_text'
)


def test_output_next_to_script_is_fixed_without_model(env, monkeypatch):
    """Canlı test 2026-09-28: model Path(__file__).parent'ı 5 denemede de bırakmadı."""
    monkeypatch.setattr(da, "_plan_project", lambda d, lang: dict(WC_PLAN))
    model = AcceptanceModel([WC_SCRIPT_DIR])
    monkeypatch.setattr(da, "_get_model", lambda name: model)
    result = da._build_project("kelime sayici", "python", "", 10, player=None, speak=None)
    assert "is working" in result, result
    assert not [p for p in model.prompts if p.startswith("You are an expert python debugger")]
    assert "Path.cwd()" in (env / "word_counter" / "main.py").read_text(encoding="utf-8")


HANG = '''
import time


def main() -> None:
    while True:  # bitmeyen kaydırma döngüsü
        time.sleep(0.2)


if __name__ == "__main__":
    main()
'''

DONE = '''
from pathlib import Path


def main() -> None:
    Path("report.txt").write_text("Notlar\\n======\\n- elma\\n- armut\\n", encoding="utf-8")


if __name__ == "__main__":
    main()
'''


def test_hanging_batch_program_gets_hard_stop_hint(env, monkeypatch):
    """Canlı test 2026-09-29: sonsuz döngüdeki kazıyıcı 4 denemede de 'çıktı yok'
    diye düzeltildi; modele programın BİTMEDİĞİ söylenmedi."""
    plan = {"project_name": "hang_app", "entry_point": "main.py",
            "files": [{"path": "main.py", "description": "entry", "imports": []}],
            "run_command": "python main.py", "dependencies": [],
            "expected_outputs": [{"path": "report.txt", "description": "notes"}]}
    monkeypatch.setattr(da, "_plan_project", lambda d, lang: dict(plan))
    prompts = []

    class M:
        def generate_content(self, prompt):
            prompts.append(prompt)
            r = type("R", (), {})()
            if prompt.startswith("You design an ACCEPTANCE TEST"):
                r.text = '{"applicable": false, "reason": "x"}'
            else:
                r.text = DONE if any("NEVER FINISHED" in p for p in prompts) else HANG
            return r

    monkeypatch.setattr(da, "_get_model", lambda name: M())
    result = da._build_project("not raporu", "python", "", 1, player=None, speak=None)
    assert "is working" in result, result
    assert any("NEVER FINISHED" in p for p in prompts)


class DisputeModel(AcceptanceModel):
    """Beklentisi yanlış ('TOPLAM 5' istenmemişti) bir kabul testi + hakem."""

    def __init__(self, versions, verdict, values=("elma: 3", "kiraz: 1")):
        super().__init__(versions)
        self.verdict = verdict
        self.values = list(values)

    def generate_content(self, prompt):
        class R:
            pass

        if prompt.startswith("You design an ACCEPTANCE TEST"):
            self.prompts.append(prompt)
            r = R()
            r.text = json.dumps({**WC_SPEC, "expect": [{"output": "report.txt", "contains": ["elma: 3", "TOPLAM 5"]}]})
            return r
        if prompt.startswith("A program was tested on a small sample input"):
            self.prompts.append(prompt)
            r = R()
            r.text = json.dumps({"program_output_is_correct": self.verdict == "program",
                                 "expectation_is_correct": self.verdict != "program",
                                 "reason": "elma 3 kez geçiyor; TOPLAM görevde yok",
                                 "correct_values": self.values})
            return r
        return super().generate_content(prompt)


@pytest.mark.parametrize("verdict,works", [("program", True), ("expectation", False)])
def test_wrong_expectation_is_dropped_only_when_different_code_agrees(env, monkeypatch, verdict, works):
    """Canlı test 2026-09-29 (hata_saatleri): beklenti yanlıştı, doğru program 5 tur reddedildi."""
    monkeypatch.setattr(da, "_plan_project", lambda d, lang: dict(WC_PLAN))
    model = DisputeModel([WC_REAL, WC_REAL + "\n# ikinci yazim\n", WC_REAL + "\n# ucuncu\n"], verdict)
    monkeypatch.setattr(da, "_get_model", lambda name: model)
    result = da._build_project("kelime sayici", "python", "", 10, player=None, speak=None)
    judged = [p for p in model.prompts if p.startswith("A program was tested")]
    assert len(judged) == 1 and "elma armut elma" in judged[0]
    assert ("is working" in result) is works, result
    if works:
        assert "beklenti hatalı" in result


def test_judge_is_asked_even_when_model_keeps_the_same_code(env, monkeypatch):
    """Canlı test 2026-09-29 (2. tur): model kodu değiştirmeyince hakem hiç sorulmuyordu."""
    monkeypatch.setattr(da, "_plan_project", lambda d, lang: dict(WC_PLAN))
    model = DisputeModel([WC_REAL], "program")
    monkeypatch.setattr(da, "_get_model", lambda name: model)
    result = da._build_project("kelime sayici", "python", "", 10, player=None, speak=None)
    assert "is working" in result and "beklenti hatalı" in result, result
    assert len([p for p in model.prompts if p.startswith("A program was tested")]) == 1


UNUSED_STORE = REAL_STORE + '''

def export_all() -> str:
    """Hiç çağrılmayan yardımcı."""
    return "\\n".join(load_notes())
'''


def test_unused_definition_alone_blocks_only_one_round(env, monkeypatch):
    """Canlı test 2026-09-29 (kitap_raporu): doğru program yalnız kullanılmayan fonksiyon yüzünden reddedildi."""
    model = FakeModel([UNUSED_STORE])
    monkeypatch.setattr(da, "_get_model", lambda name: model)
    result = da._build_project("not uygulamasi", "python", "", 10, player=None, speak=None)
    assert "is working" in result and "UNUSED-DEFINITION" in result, result
    assert "Built in 2 attempts" in result, result


def test_judge_that_contradicts_the_program_output_is_ignored(env, monkeypatch):
    """Canlı test 2026-09-29 (3. tur): hakem 'hello: 6 doğru' deyip 'hello: 3' yazan
    hatalı programı onayladı → YALANCI BAŞARI. Hakemin değerleri çıktıda yoksa karar geçersiz."""
    monkeypatch.setattr(da, "_plan_project", lambda d, lang: dict(WC_PLAN))
    model = DisputeModel([WC_REAL], "program", values=("elma: 4", "kiraz: 1"))
    monkeypatch.setattr(da, "_get_model", lambda name: model)
    result = da._build_project("kelime sayici", "python", "", 10, player=None, speak=None)
    assert "is working" not in result, result
