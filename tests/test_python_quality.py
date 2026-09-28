"""Python kalite kapısı testleri — iki canlı başarısızlığın birebir kodu ile."""
from __future__ import annotations

from jarvis.actions.devkit.python_quality import analyze_sources, header_only_outputs

# todo_app/utils/helpers.py (canlı testten, kısaltılmış)
HELPERS = '''
def create_task(description: str) -> dict:
    """Creates a task."""
    return {"id": 1, "description": description, "completed": False}

def save_task(task: dict) -> None:
    """
    Saves a task to a database.
    """
    # Placeholder for database save logic
    pass

def delete_task(task_id: int) -> None:
    """Deletes a task."""
    # Placeholder for database delete logic
    pass

def load_tasks() -> list:
    """Loads all tasks."""
    # Placeholder for database load logic
    return []
'''

# todo_app/main.py (canlı testten)
TODO_MAIN = '''
def main():
    print("gui")

if __name__ == "__main__":
    main()

def headless_test():
    print("SUCCESS")

if __name__ == "__main__":
    headless_test()
'''

# CodeReviewProgram/main.py save_report (canlı testten)
REPORT = '''
def save_report(issues, output_file="r.txt"):
    with open(output_file, "w") as file:
        file.write("Code Review Report")
        for file, func, line in issues["pass_functions"]:
            file.write(f"{file}:{line} - {func}")
'''

GOOD = '''
from abc import ABC, abstractmethod

class Base(ABC):
    @abstractmethod
    def run(self) -> None:
        """Alt sınıflar uygular."""

class MyError(Exception):
    pass

def real(x: int) -> int:
    """Gerçek iş."""
    return x * 2

def main() -> None:
    with open("out.txt", "w") as fh:
        for item in [1, 2]:
            fh.write(str(real(item)))

if __name__ == "__main__":
    main()
'''


def codes(result, path):
    return [i["code"] for i in result.get(path, [])]


def test_detects_todo_app_stubs():
    r = analyze_sources({"utils/helpers.py": HELPERS})
    stubs = [i for i in r["utils/helpers.py"] if i["code"] == "STUB-FUNCTION"]
    names = sorted(i["message"].split("'")[1] for i in stubs)
    assert names == ["delete_task", "load_tasks", "save_task"]
    assert all("yer tutucu yorumu var" in i["message"] for i in stubs)


def test_detects_duplicate_main_and_def_after_main():
    assert sorted(codes(analyze_sources({"main.py": TODO_MAIN}), "main.py")) == ["DEF-AFTER-MAIN", "DUPLICATE-MAIN"]


def test_detects_with_target_shadowing():
    r = analyze_sources({"main.py": REPORT})
    # save_report bu kısa örnekte hiç çağrılmadığı için UNUSED-DEFINITION da doğru bir bulgu
    assert sorted(codes(r, "main.py")) == ["UNUSED-DEFINITION", "WITH-TARGET-SHADOWED"]


def test_clean_code_has_no_findings():
    assert analyze_sources({"main.py": GOOD, "notes.txt": "pass", "broken.py": "def ("}) == {}


def test_header_only_report_is_flagged_but_explicit_empty_result_passes(tmp_path):
    (tmp_path / "r.txt").write_text("Code Review Report\n=================\n\n", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("Rapor\n=====\nTaranan dosya: 4\nSorun bulunamadı.\n", encoding="utf-8")
    (tmp_path / "data.db").write_bytes(b"x")
    outs = [{"path": "r.txt"}, {"path": "ok.txt"}, {"path": "data.db"}, {"path": "../dışarı.txt"}]
    problems = header_only_outputs(tmp_path, outs)
    assert len(problems) == 1 and problems[0].startswith("'r.txt'")


# code_reviewer/analyzer.py (canlı testten, kısaltılmış): iki özellik yazılmış ama çağrılmamış
ANALYZER = '''
class CodeAnalyzer:
    def analyze(self):
        self.analyze_ast(None)

    def analyze_ast(self, tree):
        return tree

    def check_unused_functions(self, tree):
        return [tree]

    def check_leftover_backup_files(self, path):
        return path.endswith(".bak")
'''

ANALYZER_MAIN = '''
from analyzer import CodeAnalyzer
import tkinter as tk


class App:
    def __init__(self, root):
        tk.Button(root, command=self.on_click)

    def on_click(self):
        return getattr(self, "helper")()

    def helper(self):
        return 1


def main():
    CodeAnalyzer().analyze()
    App(None)


if __name__ == "__main__":
    main()
'''


def test_detects_written_but_never_called_features():
    r = analyze_sources({"analyzer.py": ANALYZER, "main.py": ANALYZER_MAIN})
    unused = sorted(i["message"].split("'")[1] for i in r.get("analyzer.py", []) if i["code"] == "UNUSED-DEFINITION")
    assert unused == ["CodeAnalyzer.check_leftover_backup_files", "CodeAnalyzer.check_unused_functions"]
    # geri çağırma (command=self.on_click) ve getattr(\"helper\") kullanılmış sayılır
    assert "main.py" not in r


def test_placeholder_data_is_detected(tmp_path):
    from jarvis.actions.devkit.python_quality import placeholder_data_outputs
    (tmp_path / "quotes.json").write_text(
        '[{"id": 1, "text": "Sample quote 1", "author": "Author 1"}, {"id": 2, "text": "x", "author": "Author 2"}]'
    )
    out = [{"path": "quotes.json"}]
    problems = placeholder_data_outputs(tmp_path, out, "quotes.toscrape.com'u kazı")
    assert problems and "UYDURMA" in problems[0]
    # Görev açıkça örnek veri istiyorsa bayrak kalkmaz
    assert placeholder_data_outputs(tmp_path, out, "örnek veri üreten program") == []


def test_real_quotes_are_not_flagged(tmp_path):
    from jarvis.actions.devkit.python_quality import placeholder_data_outputs
    (tmp_path / "quotes.json").write_text(
        '[{"id": 1, "text": "The world as we have created it is a process of our thinking.", '
        '"author": "Albert Einstein"}, {"id": 2, "text": "It is our choices, Harry...", "author": "J.K. Rowling"}]'
    )
    assert placeholder_data_outputs(tmp_path, [{"path": "quotes.json"}], "kazı") == []
