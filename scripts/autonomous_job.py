#!/usr/bin/env python3
"""Jarvis full-autonomy job supervisor.

The agent may freely edit only a temporary Git worktree. The protected source
branch is changed only after all verification commands pass in the same job.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Job:
    repo: Path
    base_branch: str
    branch: str
    worktree: Path
    job_dir: Path
    max_minutes: int
    max_attempts: int


def run(cmd: list[str], cwd: Path, *, timeout: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout, env=env)


def git(repo: Path, *args: str, timeout: int = 60) -> str:
    result = run(["git", *args], repo, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)} başarısız:\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def write_log(path: Path, title: str, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n=== {title} ===\n{text}\n")


def verification_commands() -> list[list[str]]:
    raw = os.environ.get("JARVIS_VERIFY_COMMANDS", "python -m pytest;ruff check .")
    commands: list[list[str]] = []
    for item in raw.split(";"):
        if item.strip():
            commands.append(shlex.split(item))
    return commands


def verify(job: Job, log: Path) -> tuple[bool, list[dict[str, object]]]:
    results: list[dict[str, object]] = []
    all_ok = True
    for command in verification_commands():
        started = time.monotonic()
        try:
            result = run(command, job.worktree, timeout=900)
            output = (result.stdout + "\n" + result.stderr).strip()
            ok = result.returncode == 0
        except subprocess.TimeoutExpired as exc:
            output = f"TIMEOUT: {exc}"
            ok = False
        except OSError as exc:
            output = f"COMMAND ERROR: {exc}"
            ok = False
        result_row = {"command": command, "ok": ok, "seconds": round(time.monotonic() - started, 2), "output": output[-12000:]}
        results.append(result_row)
        write_log(log, "OK" if ok else "FAIL" + " " + " ".join(command), output)
        if not ok:
            all_ok = False
            break
    return all_ok, results


def make_report(job: Job, result: str, checks: list[dict[str, object]], status: str) -> None:
    report = {
        "status": status,
        "job": job.job_dir.name,
        "base_branch": job.base_branch,
        "branch": job.branch,
        "worktree": str(job.worktree),
        "checks": checks,
        "agent_result": result,
        "diff_stat": git(job.worktree, "diff", "--stat", f"{job.base_branch}...HEAD") if job.worktree.exists() else "",
    }
    (job.job_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def create_job(repo: Path, base_branch: str, root: Path, max_minutes: int, max_attempts: int) -> Job:
    job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    job_dir = root / job_id
    worktree = job_dir / "workspace"
    job_dir.mkdir(parents=True)
    branch = f"jarvis/autonomous/{job_id}"
    if git(repo, "status", "--porcelain"):
        raise RuntimeError("Güvenlik: ana depo kirli; otomatik görevi başlatmadım.")
    git(repo, "fetch", "--quiet", "origin", base_branch, timeout=120)
    git(repo, "rev-parse", "--verify", base_branch)
    git(repo, "worktree", "add", "-b", branch, str(worktree), base_branch, timeout=120)
    (job_dir / "original_commit").write_text(git(repo, "rev-parse", base_branch) + "\n", encoding="utf-8")
    return Job(repo, base_branch, branch, worktree, job_dir, max_minutes, max_attempts)


async def execute(job: Job, description: str, language: str) -> int:
    # The existing coder is intentionally reused; all writes are confined to the worktree.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools.developer.agentic_coder import AgenticCoder

    deadline = time.monotonic() + job.max_minutes * 60
    attempts = 0
    result = ""
    checks: list[dict[str, object]] = []
    log = job.job_dir / "job.log"
    while attempts < job.max_attempts and time.monotonic() < deadline:
        attempts += 1
        write_log(log, f"AGENT ATTEMPT {attempts}", description)
        coder = AgenticCoder(max_iterations=15)
        try:
            result = await coder.solve(description=description, language=language, project_path=str(job.worktree))
        except Exception as exc:
            result = f"Agent exception: {type(exc).__name__}: {exc}"
            write_log(log, "AGENT EXCEPTION", result)
        ok, checks = verify(job, log)
        if ok:
            git(job.repo, "-C", str(job.worktree), "diff", "--check", timeout=60)
            diff = run(["git", "diff", "--binary", job.base_branch], job.worktree, timeout=60)
            if diff.returncode:
                raise RuntimeError(f"diff üretilemedi:\n{diff.stderr}")
            (job.job_dir / "final.patch").write_text(diff.stdout, encoding="utf-8")
            changed_result = run(["git", "diff", "--name-only", job.base_branch], job.worktree, timeout=60)
            changed = [line for line in changed_result.stdout.splitlines() if line.strip()]
            if any(name.startswith(".env") or name.startswith("id_rsa") for name in changed):
                raise RuntimeError("Güvenlik: gizli dosya değişikliği tespit edildi; patch reddedildi.")
            if not changed:
                raise RuntimeError("Doğrulama başarılı ancak değişiklik yok; inceleme patch’i üretilmedi.")
            # Deliberately do not commit, merge, push, or modify the protected branch.
            make_report(job, result, checks, "ready_for_review")
            write_log(log, "READY_FOR_REVIEW", f"{len(changed)} dosya; patch: {job.job_dir / 'final.patch'}")
            return 0
        write_log(log, f"RETRY {attempts}", "Verification failed; agent receives the next iteration context.")
    make_report(job, result, checks, "failed")
    # Worktree is removed, main remains at original commit.
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Jarvis autonomous coding job supervisor")
    parser.add_argument("description", help="Jarvis görev açıklaması")
    parser.add_argument("--repo", default=os.environ.get("JARVIS_REPO", os.getcwd()))
    parser.add_argument("--branch", default=os.environ.get("JARVIS_BASE_BRANCH", "main"))
    parser.add_argument("--language", default="python")
    parser.add_argument("--jobs-root", default=os.environ.get("JARVIS_JOBS_ROOT", "./.jarvis-jobs"))
    parser.add_argument("--max-attempts", type=int, default=int(os.environ.get("JARVIS_MAX_ATTEMPTS", "5")))
    parser.add_argument("--max-minutes", type=int, default=int(os.environ.get("JARVIS_MAX_MINUTES", "60")))
    args = parser.parse_args()
    repo = Path(args.repo).expanduser().resolve()
    jobs_root = Path(args.jobs_root).expanduser().resolve()
    jobs_root.mkdir(parents=True, exist_ok=True)
    job: Job | None = None
    try:
        job = create_job(repo, args.branch, jobs_root, args.max_minutes, args.max_attempts)
        print(f"JOB={job.job_dir.name}")
        print(f"WORKTREE={job.worktree}")
        code = asyncio.run(execute(job, args.description, args.language))
        return code
    except Exception as exc:
        if job:
            (job.job_dir / "fatal.txt").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        print(f"AUTONOMOUS JOB FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    finally:
        if job and job.worktree.exists():
            subprocess.run(["git", "worktree", "remove", "--force", str(job.worktree)], cwd=job.repo, capture_output=True, text=True)
        if job:
            subprocess.run(["git", "branch", "-D", job.branch], cwd=job.repo, capture_output=True, text=True)


if __name__ == "__main__":
    raise SystemExit(main())
