"""Run only the historical script-style regression checks.

Pytest modules are deliberately not executed as plain scripts: doing so would
skip their test functions and could make a broken module look successful.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# TODO(pr29-integration): Re-enable when PR29 modules are ported
pytestmark = pytest.mark.skip(reason="PR29 integration pending")

TESTS_DIR = Path(__file__).resolve().parent
# Explicit allowlist of the pre-existing assertion scripts. New pytest modules,
# including test_repair_*.py, must be collected by pytest instead.
LEGACY_SCRIPTS = (
    "test_agent_loop_retry_guard.py",
    "test_brain_health_route.py",
    "test_brain_intent_priority.py",
    "test_brain_start_route.py",
    "test_brain_status_priority.py",
    "test_hardening_suite.py",
    "test_local_llm_selection.py",
    "test_security_hardening.py",
    "test_windows_shell.py",
)


@pytest.mark.parametrize("script", LEGACY_SCRIPTS)
def test_legacy_script(script: str, tmp_path: Path) -> None:
    assert (TESTS_DIR / script).is_file(), script
    env = os.environ.copy()
    env.update({"JARVIS_HOME": str(tmp_path), "QT_QPA_PLATFORM": "offscreen"})
    result = subprocess.run(
        [sys.executable, str(TESTS_DIR / script)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        text=True,
        timeout=300,
        env=env,
        cwd=TESTS_DIR.parent,
    )
    assert result.returncode == 0, (
        f"{script} baÅŸarÄ±sÄ±z:\n{result.stdout}\n{result.stderr}"
    )

