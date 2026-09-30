"""Düzeltme sırasında uydurma/yerel isimler PyPI'dan kurulmaz (2026-09-30 flask_todo)."""
from unittest import mock

from jarvis.actions import dev_agent as da


def _run(tmp_path, module, planned=()):
    with mock.patch.object(da.subprocess, "run") as run:
        run.return_value = mock.Mock(returncode=0)
        ok = da._try_auto_install(f"ModuleNotFoundError: No module named '{module}'", tmp_path, planned)
    return ok, run.called


def test_local_function_name_not_installed(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("def create_app():\n    pass\n")
    assert _run(tmp_path, "create_app") == (False, False)


def test_local_file_name_not_installed(tmp_path):
    (tmp_path / "routes.py").write_text("x = 1\n")
    assert _run(tmp_path, "routes") == (False, False)


def test_unknown_name_not_installed(tmp_path):
    assert _run(tmp_path, "totally_made_up_pkg") == (False, False)


def test_planned_and_well_known_installed(tmp_path):
    assert _run(tmp_path, "flask") == (True, True)
    assert _run(tmp_path, "tinydb", planned=["tinydb>=4"]) == (True, True)


def test_flask_layout_hint():
    out = "AttributeError: partially initialized module 'app' has no attribute 'route' (most likely due to a circular import)"
    assert "Blueprint" in da._known_error_hint(out)
    assert "create_app()" in da._known_error_hint("AttributeError: 'function' object has no attribute 'run'")
    assert da._known_error_hint("ValueError: x") == ""
