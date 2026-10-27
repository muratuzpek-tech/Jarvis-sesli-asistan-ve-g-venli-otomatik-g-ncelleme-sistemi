from scripts.repo_doctor import (
    _stub_disposition,
    shell_review_issues,
    syntax_issues,
)


def test_reports_syntax_error_with_line():
    result = syntax_issues({"broken.py": "x = 1\ndef broken(:\n    pass\n"})
    assert len(result) == 1
    assert result[0]["priority"] == "P1"
    assert result[0]["location"].startswith("broken.py:")


def test_shell_true_is_review_candidate_not_claimed_vulnerability():
    result = shell_review_issues({
        "src/example.py": "subprocess.run(command, shell=True)\n",
        "src/comment.py": "# shell=True is discussed here\n",
    })
    assert len(result) == 1
    assert result[0]["kind"] == "review"
    assert result[0]["location"] == "src/example.py:1"


def test_clean_source_has_no_syntax_findings():
    assert syntax_issues({"ok.py": "def add(a, b):\n    return a + b\n"}) == []


def test_shell_true_inside_string_is_not_reported():
    result = shell_review_issues({
        "src/docs.py": 'message = "shell=True"\n',
    })
    assert result == []


def test_protocol_method_is_recognized_as_contract():
    source = (
        "from typing import Protocol\n"
        "class P(Protocol):\n"
        "    def available(self) -> bool: ...\n"
    )
    assert _stub_disposition(source, 3)[0] == "suppress"


def test_legacy_loader_noop_is_recognized():
    source = (
        "class _LegacyLoader:\n"
        "    def exec_module(self, module):  # modül zaten yüklendi\n"
        "        return None\n"
    )
    assert _stub_disposition(source, 2)[0] == "suppress"


def test_unexplained_empty_method_stays_visible():
    source = "class Worker:\n    def run(self):\n        pass\n"
    assert _stub_disposition(source, 2)[0] == "keep"


def test_documented_lock_method_is_lower_priority():
    source = (
        "class ProcessLockManager:\n"
        "    def force_release_stale(self):\n"
        "        return False\n"
    )
    assert _stub_disposition(source, 2)[0] == "downgrade"


def test_root_shim_protocol_stays_as_review_candidate():
    source = (
        "class _RootShim:\n"
        "    def protocol(self, *_):\n"
        "        pass\n"
    )
    assert _stub_disposition(source, 2)[0] == "downgrade"
