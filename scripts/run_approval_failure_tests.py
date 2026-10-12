#!/usr/bin/env python3
"""Run the ten approval-regression scenarios as one deterministic test batch.

Usage:
    python scripts/run_approval_failure_tests.py
    python scripts/run_approval_failure_tests.py --verbose
    python scripts/run_approval_failure_tests.py --collect-only

The script intentionally exits with pytest's non-zero status when any selected
scenario fails. It does not hide failures with ``|| true``.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = (
    "tests/test_multi_approval.py::test_model_retry_and_background_agent_loop_approval_are_separate_requests",
    "tests/test_multi_approval.py::test_spoken_no_cancels_one_request_per_turn_and_keeps_brain",
    "tests/test_multi_approval.py::test_queued_request_is_asked_again_after_the_latest_is_answered",
    "tests/test_multi_approval.py::test_model_cannot_requeue_a_just_rejected_call_until_next_user_turn",
    "tests/test_multi_approval.py::test_unrelated_text_drops_every_voice_request_but_keeps_brain",
    "tests/test_brain_team_approval.py::test_changed_step_is_not_approved",
    "tests/test_brain_team_approval.py::test_expired_confirmation_is_not_approved",
    "tests/test_brain_team_approval.py::test_approval_runs_the_real_orchestrator_approve",
    "tests/test_agent_loop_approval.py::test_approval_request_shows_tool_args_and_target",
    "tests/test_agent_loop_approval.py::test_announcement_does_not_overwrite_other_pending_confirmation",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Jarvis onay entegrasyonundaki 10 regresyon senaryosunu çalıştırır."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="pytest çıktısını ayrıntılı göster (-vv).",
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Testleri çalıştırmadan seçimin doğru olduğunu doğrula.",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="İlk hatadan sonra diğer testleri de çalıştır (pytest -x kullanılmaz; varsayılan da budur).",
    )
    return parser.parse_args()


def python_executable() -> str:
    """Prefer the repository virtual environment on Linux and Windows."""
    candidates = (
        ROOT / ".venv" / "bin" / "python",
        ROOT / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def main() -> int:
    args = parse_args()
    missing = [str(ROOT / node) for node in TESTS if not (ROOT / node.split("::", 1)[0]).is_file()]
    if missing:
        print("Eksik test dosyaları:", file=sys.stderr)
        print("\n".join(f"  - {item}" for item in missing), file=sys.stderr)
        return 2

    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("JARVIS_SANDBOX", "off")
    env.setdefault("PYTHONPATH", str(ROOT / "src"))

    print("Jarvis onay regresyon paketi: 10 senaryo")
    for index, node in enumerate(TESTS, start=1):
        print(f"  {index:02d}. {node}")
    print()

    command = [
        python_executable(),
        "-m",
        "pytest",
        "-q" if not args.verbose else "-vv",
        "--tb=short",
        "--color=yes",
        *TESTS,
    ]
    if args.collect_only:
        command.insert(command.index("--tb=short"), "--collect-only")

    completed = subprocess.run(command, cwd=ROOT, env=env)
    if completed.returncode == 0:
        print("\nSONUÇ: Seçilen 10 senaryonun tamamı başarılı.")
    elif completed.returncode == 5:
        print("\nSONUÇ: Hiç test toplanamadı; test yolu veya bağımlılıkları kontrol edin.", file=sys.stderr)
    else:
        print(
            f"\nSONUÇ: En az bir senaryo başarısız oldu (pytest çıkış kodu: {completed.returncode}).",
            file=sys.stderr,
        )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
