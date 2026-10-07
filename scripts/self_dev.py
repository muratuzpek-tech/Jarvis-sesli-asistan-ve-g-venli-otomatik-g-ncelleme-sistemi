#!/usr/bin/env python3
"""Jarvis'in sandbox klonunda tam otomatik kendi kendini gelistirme dongusu.

    python scripts/self_dev.py init                        # ~/jarvis-sandbox klonu
    python scripts/self_dev.py run --backlog prompts.md    # tek tur
    python scripts/self_dev.py run --forever --backlog prompts.md
    python scripts/self_dev.py install-service --backlog prompts.md

Ana depo yalnizca `init` aninda BIR KEZ okunur (git clone --no-hardlinks);
sonra hic dokunulmaz. Klonda `origin` kaldirilir (push imkansiz), calisma
jarvis/self-dev dalinda yapilir. Her gorev icin AgenticCoder (Ollama) klonda
calisir; koruma kurallari + dogrulama (varsayilan pytest + ruff) gecerse
sandbox'ta commit atilir, aksi halde sandbox gorev oncesi commit'e doner.

--forever: basarisiz gorev sonraki turda son 2 denemenin hata ciktisiyla
yeniden denenir (gorev basi en cok 3 tur); ~/.jarvis-self/STOP varsa mevcut
gorev bitince cikar; JARVIS_SELF_DEV_YIELD=1 iken Jarvis calisirken bekler.

Durum sandbox'taki state.json'da (git'e girmez); rapor, is gunlukleri ve
kilit ~/.jarvis-self altindadir. Dogrulama/coder HOME'u ~/.jarvis-self/home. Terfi: scripts/self_dev_promote.sh (yalnizca
patch disa aktarir). Ayrinti: docs/SELF_DEV.md
"""
from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: dongu yalnizca Linux'ta desteklenir
    fcntl = None

BRANCH = "jarvis/self-dev"
GIB = 2**30
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VERIFY = "python -m pytest -q;ruff check ."
# Sandbox'ta git'e girmeyen yerel dosyalar (.git/info/exclude).
_EXCLUDES = ("state.json", ".venv/", "__pycache__/", "*.pyc", ".pytest_cache/",
             ".ruff_cache/", "_reddedilen_*")
_COMMIT_GIT = ("-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false")
# Ortamdan alt sureclere gecmeyen degiskenler (anahtar, token, parola).
_SECRET_ENV_RE = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL", re.IGNORECASE)

MAX_FILES = 15
MAX_LINES = 800
OLLAMA_TRIES = 3
OLLAMA_WAIT = 30.0
MAX_ROUNDS = 3              # --forever: gorev basina en cok tur
HISTORY_KEEP = 2            # coder'a verilen son basarisiz deneme sayisi
HISTORY_CHARS = 1500        # deneme basina hata ciktisi (sondan) siniri
YIELD_WAIT = 60.0
SERVICE_NAME = "jarvis-self-dev.service"
TASK_PREFIX = ("Önce başarısız test yaz, sonra düzelt. Mevcut testleri silme/zayıflatma, "
               "skip/xfail ekleme, bağımlılık ekleme.")

# ── Koruma kurallari ──────────────────────────────────────────
_PROTECTED_NAMES = (".env*", "id_rsa*")
_PROTECTED_PATHS = (".git/*", ".github/*", "pyproject.toml", "scripts/self_dev*")
_REQUIREMENTS = "requirements*.txt"
# init: ana depoda izlenen bu dosyalar varsa klonlanmaz (ornekler haric)
_SECRET_TRACKED = (".env*", "id_rsa*", "*.pem", "*.key", "api_keys.json")

_TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.MULTILINE)
_SKIP_RE = re.compile(
    r"pytest\.mark\.(?:skip|skipif|xfail)\b|pytest\.(?:skip|xfail|importorskip)\s*\("
    r"|unittest\.skip|@skip\w*\b|\.skipTest\s*\(")
_ASSERT_RE = re.compile(r"\bassert\b|\bself\.assert\w+\s*\(|\bpytest\.raises\s*\(")


class SelfDevError(Exception):
    pass


class _Stop(Exception):
    """Bekci: dongu durur, mevcut gorev isaretlenmez."""


# ── Yardimcilar ───────────────────────────────────────────────

def _git(cwd: Path, *args: str, check: bool = True, text: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=text,
                          stdin=subprocess.DEVNULL)
    if check and proc.returncode != 0:
        err = proc.stderr if text else proc.stderr.decode("utf-8", "replace")
        raise SelfDevError(f"git {' '.join(args)}: {err.strip()[:500]}")
    return proc.stdout


def _safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}
    env.update(extra or {})
    return env


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "…" + text[-limit:]


def _task_id(text: str) -> str:
    return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()[:10]


def _fmt_secs(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60} dk {sec % 60} sn" if sec >= 60 else f"{sec} sn"


# ── Backlog ───────────────────────────────────────────────────

@dataclass
class Task:
    text: str
    mark: str           # " " bekliyor, "x" bitti, "!" basarisiz

    @property
    def id(self) -> str:
        return _task_id(self.text)


_ITEM_RE = re.compile(r"^\s*[-*]\s+\[([ xX!])\]\s+(.*\S)\s*$")


def parse_backlog(text: str) -> list[Task]:
    """'- [ ] gorev' satirlari; girintili devam satirlari goreve eklenir."""
    tasks: list[Task] = []
    current: Task | None = None
    for line in text.splitlines():
        m = _ITEM_RE.match(line)
        if m:
            current = Task(text=m.group(2), mark=m.group(1).lower())
            tasks.append(current)
        elif current is not None and line.strip() and line[:1] in (" ", "\t"):
            current.text = f"{current.text} {line.strip()}"
        else:
            current = None
    return tasks


# ── init ──────────────────────────────────────────────────────

def _is_secret(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (not name.endswith(".example")
            and any(fnmatch.fnmatch(name, pat) for pat in _SECRET_TRACKED))


def ensure_excludes(sandbox: Path) -> None:
    exclude = sandbox / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    have = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    missing = [e for e in _EXCLUDES if e not in have]
    if missing:
        with exclude.open("a", encoding="utf-8") as fh:
            fh.write("\n# self_dev\n" + "\n".join(missing) + "\n")


def _venv_python() -> str | None:
    found = shutil.which("python3.12")
    if found:
        return found
    return sys.executable if sys.version_info[:2] == (3, 12) else None


def init_sandbox(repo: Path, sandbox: Path, *, make_venv: bool = True,
                 install_deps: bool = True, log: Callable[[str], None] = print) -> None:
    """Ana depodan (yalnizca okuyarak) sandbox klonu kurar."""
    repo, sandbox = Path(repo).resolve(), Path(sandbox).expanduser()
    if sandbox.exists():
        raise SelfDevError(f"sandbox zaten var: {sandbox} (silip yeniden kurun ya da olduğu gibi kullanın)")
    if not (repo / ".git").exists():
        raise SelfDevError(f"git deposu değil: {repo}")
    tracked = _git(repo, "ls-tree", "-r", "--name-only", "main").splitlines()
    secrets = [p for p in tracked if _is_secret(p)]
    if secrets:
        raise SelfDevError(f"ana depoda izlenen gizli dosya var, klonlanmadı: {', '.join(secrets)}")
    try:
        _git(repo.parent, "clone", "-q", "--no-hardlinks", "--single-branch", "--branch", "main",
             str(repo), str(sandbox))
        _git(sandbox, "remote", "remove", "origin")
        _git(sandbox, "checkout", "-q", "-b", BRANCH)
        _git(sandbox, "config", "user.name", "Jarvis Self-Dev")
        _git(sandbox, "config", "user.email", "self-dev@jarvis.invalid")
        _git(sandbox, "config", "commit.gpgsign", "false")
        ensure_excludes(sandbox)
    except Exception:
        shutil.rmtree(sandbox, ignore_errors=True)
        raise
    log(f"✅ sandbox: {sandbox} (dal {BRANCH}, uzak yok)")
    if not make_venv:
        return
    py = _venv_python()
    if py is None:
        log("⚠️ python3.12 bulunamadı; .venv kurulmadı")
        return
    subprocess.run([py, "-m", "venv", str(sandbox / ".venv")], check=True, stdin=subprocess.DEVNULL)
    log(f"✅ venv: {sandbox / '.venv'}")
    if install_deps:
        reqs = [a for r in ("requirements.txt", "requirements-dev.txt") if (sandbox / r).exists()
                for a in ("-r", r)]
        if reqs:
            proc = subprocess.run([str(sandbox / ".venv" / "bin" / "pip"), "install", "-q", *reqs],
                                  cwd=sandbox, env=_safe_env(), stdin=subprocess.DEVNULL)
            if proc.returncode != 0:
                log("⚠️ bağımlılık kurulumu başarısız; .venv eksik olabilir")


# ── Yapilandirma ──────────────────────────────────────────────

@dataclass
class Config:
    repo: Path
    sandbox: Path
    self_dir: Path
    verify: str = DEFAULT_VERIFY
    max_hours: float = 4.0
    max_attempts: int = 5
    task_minutes: float = 30.0
    min_free_gb: float = 5.0
    yield_jarvis: bool = field(
        default_factory=lambda: os.environ.get("JARVIS_SELF_DEV_YIELD", "").strip() == "1")


Coder = Callable[[str, Path, float], str]


@dataclass
class Hooks:
    coder: Coder
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    disk_free: Callable[[Path], int] = field(default=lambda p: shutil.disk_usage(p).free)
    ollama_ok: Callable[[], bool] = field(default=lambda: ollama_reachable())
    jarvis_running: Callable[[], list[int]] = field(default=lambda: running_jarvis_pids())


def ollama_reachable() -> bool:
    import urllib.request
    base = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    if not base.startswith("http"):
        base = f"http://{base}"
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=3) as resp:  # noqa: S310 - yerel Ollama
            return resp.status == 200
    except Exception:
        return False


def running_jarvis_pids() -> list[int]:
    """Calisan Jarvis surecleri (/proc): uyari icin, durdurmak icin DEGIL."""
    pids = []
    me = os.getpid()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if int(proc.name) == me or "self_dev" in cmd:
            continue
        if re.search(r"-m jarvis\b|jarvis/__main__\.py|/bin/jarvis(?:-cli)?\b|START_JARVIS", cmd):
            pids.append(int(proc.name))
    return pids


@contextlib.contextmanager
def acquire_lock(path: Path) -> Iterator[None]:
    """Tek ornek: flock (surec olurse cekirdek birakir). Doluysa BlockingIOError."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")  # noqa: SIM115 - kilit omru boyunca acik
    try:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        yield
    finally:
        fh.close()


# ── Degisiklik toplama + koruma ───────────────────────────────

def _git_fingerprint(sb: Path) -> str:
    """.git'in coder'in dokunmamasi gereken kisimlari: config, hooks, info,
    HEAD, dal ucu. Degisirse '.git' ihlali."""
    h = hashlib.sha256()
    g = sb / ".git"
    for rel in ("config", "HEAD"):
        p = g / rel
        h.update(rel.encode() + (p.read_bytes() if p.exists() else b"-"))
    for sub in ("hooks", "info"):
        for p in sorted((g / sub).rglob("*")) if (g / sub).exists() else []:
            h.update(str(p.relative_to(g)).encode())
            if p.is_file():
                h.update(p.read_bytes())
    h.update(_git(sb, "rev-parse", "HEAD").encode())
    return h.hexdigest()


def _ignored_entries(sb: Path) -> set[str]:
    out = _git(sb, "ls-files", "-z", "-o", "-i", "--exclude-standard", "--directory")
    return {p for p in out.split("\0") if p}


@dataclass
class Snapshot:
    head: str
    fingerprint: str
    ignored: set[str]


def snapshot(sb: Path) -> Snapshot:
    return Snapshot(_git(sb, "rev-parse", "HEAD").strip(), _git_fingerprint(sb), _ignored_entries(sb))


@dataclass
class Changes:
    files: dict[str, str]                      # yol -> durum (A/M/D/T)
    lines: int
    new_ignored: list[str]
    git_touched: bool
    stat: str = ""

    @property
    def empty(self) -> bool:
        # yok sayilan yeni dosyalar (__pycache__, .pytest_cache) degisiklik sayilmaz;
        # yalnizca koruma (.env gibi) icin bakilir
        return not self.files and not self.git_touched


def collect_changes(sb: Path, snap: Snapshot) -> Changes:
    touched = _git_fingerprint(sb) != snap.fingerprint
    _git(sb, "add", "-A")
    status = _git(sb, "diff", "--cached", "--no-renames", "--name-status", "-z", snap.head).split("\0")
    files = {status[i + 1]: status[i] for i in range(0, len(status) - 1, 2) if status[i]}
    lines = 0
    for row in _git(sb, "diff", "--cached", "--no-renames", "--numstat", "-z", snap.head).split("\0"):
        parts = row.split("\t")
        if len(parts) >= 2:
            lines += sum(int(x) for x in parts[:2] if x.isdigit())
    stat = _git(sb, "diff", "--cached", "--stat", snap.head)
    new_ignored = sorted(_ignored_entries(sb) - snap.ignored)
    return Changes(files, lines, new_ignored, touched, stat)


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")
                                     or path.startswith("tests/") or "/tests/" in path)


def _protected(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (any(fnmatch.fnmatch(name, p) for p in _PROTECTED_NAMES)
            or fnmatch.fnmatch(name, _REQUIREMENTS)
            or any(fnmatch.fnmatch(path, p) for p in _PROTECTED_PATHS))


def guard_violation(sb: Path, snap: Snapshot, ch: Changes) -> str | None:
    """Ilk koruma ihlalinin nedeni; yoksa None."""
    if ch.git_touched:
        return ".git değiştirildi (config/hooks/HEAD/dal)"
    for path in [*ch.files, *ch.new_ignored]:
        if _protected(path.rstrip("/")):
            return f"korumalı dosya: {path}"
    root = sb.resolve()
    for path, st in ch.files.items():
        if path.startswith("/") or ".." in Path(path).parts:
            return f"çalışma dizini dışına yazma: {path}"
        full = sb / path
        if st != "D" and (full.is_symlink() or full.exists()):
            target = full.resolve()
            if target != root and root not in target.parents:
                return f"çalışma dizini dışına yazma (symlink kaçışı): {path} -> {target}"
    if len(ch.files) > MAX_FILES or ch.lines > MAX_LINES:
        return (f"çok büyük değişiklik: {len(ch.files)} dosya, {ch.lines} satır "
                f"(sınır {MAX_FILES} dosya / {MAX_LINES} satır)")
    for path, st in ch.files.items():
        if not _is_test_path(path):
            continue
        old = _git(sb, "show", f"{snap.head}:{path}", check=False) if st != "A" else ""
        new = (sb / path).read_text(encoding="utf-8", errors="replace") if st != "D" else ""
        gone = set(_TEST_DEF_RE.findall(old)) - set(_TEST_DEF_RE.findall(new))
        if gone:
            return f"test zayıflatma: silinen test {path}::{', '.join(sorted(gone))}"
        if len(_SKIP_RE.findall(new)) > len(_SKIP_RE.findall(old)):
            return f"test zayıflatma: yeni skip/xfail {path}"
        if len(_ASSERT_RE.findall(new)) < len(_ASSERT_RE.findall(old)):
            return f"test zayıflatma: assert sayısı azaldı {path}"
    return None


def restore(sb: Path, snap: Snapshot, ch: Changes | None = None) -> None:
    """Sandbox'i gorev oncesi commit'e dondur. .git ihlali varsa hooks/config
    da geri alinamaz; o yuzden hooks klasorundeki yeni dosyalar silinir."""
    _git(sb, "checkout", "-q", "-f", BRANCH, check=False)
    _git(sb, "reset", "-q", "--hard", snap.head)
    _git(sb, "clean", "-q", "-fd")
    for entry in (ch.new_ignored if ch else []):
        if _protected(entry.rstrip("/")):
            target = sb / entry
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
    if _git_fingerprint(sb) != snap.fingerprint:
        hooks = sb / ".git" / "hooks"
        for p in hooks.iterdir() if hooks.exists() else []:
            if not p.name.endswith(".sample"):
                p.unlink() if not p.is_dir() else shutil.rmtree(p, ignore_errors=True)


# ── Dogrulama + coder ─────────────────────────────────────────

def _run_env(cfg: Config) -> dict[str, str]:
    venv_bin = cfg.sandbox / ".venv" / "bin"
    path = os.environ.get("PATH", "")
    if venv_bin.is_dir():
        path = f"{venv_bin}{os.pathsep}{path}"
    jh = cfg.self_dir / "jarvis_home"
    home = cfg.self_dir / "home"          # gercek HOME'a (~/.config, ~/MuratJARVIS) yazilmasin
    for d in (jh, home):
        d.mkdir(parents=True, exist_ok=True)
    return _safe_env({"PATH": path, "JARVIS_HOME": str(jh), "HOME": str(home),
                      "XDG_CONFIG_HOME": str(home / ".config"),
                      "XDG_CACHE_HOME": str(home / ".cache"),
                      "XDG_DATA_HOME": str(home / ".local" / "share"),
                      "XDG_STATE_HOME": str(home / ".local" / "state")})


def _run_group(argv: list[str], cwd: Path, env: dict, timeout: float, stdin: str | None = None
               ) -> tuple[int | None, str]:
    """Ayri surec grubunda calistir; zaman asiminda tum grubu oldur."""
    proc = subprocess.Popen(argv, cwd=cwd, env=env, text=True, start_new_session=True,
                            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        out, _ = proc.communicate(stdin, timeout=max(1.0, timeout))
        return proc.returncode, out
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return None, (out or "") + f"\n[ZAMAN AŞIMI] {timeout:.0f} sn"


def verify(cfg: Config, timeout: float) -> tuple[bool, str]:
    env = _run_env(cfg)
    outputs = []
    ok = True
    for cmd in (c.strip() for c in cfg.verify.split(";")):
        if not cmd:
            continue
        argv = shlex.split(cmd)
        if argv[0] == "python" and not (cfg.sandbox / ".venv" / "bin").is_dir():
            argv[0] = sys.executable
        else:
            argv[0] = shutil.which(argv[0], path=env["PATH"]) or argv[0]
        try:
            code, out = _run_group(argv, cfg.sandbox, env, timeout)
        except OSError as e:
            code, out = 127, f"{type(e).__name__}: {e}"
        outputs.append(f"$ {cmd}  → çıkış {code}\n{out}")
        ok = ok and code == 0
    return ok, "\n".join(outputs)


def make_subprocess_coder(cfg: Config) -> Coder:
    """Gercek coder: AgenticCoder ayri surecte (zaman asiminda oldurulebilir)."""
    def coder(description: str, workdir: Path, timeout: float) -> str:
        code, out = _run_group([sys.executable, str(Path(__file__).resolve()), "_code",
                                "--workdir", str(workdir)],
                               workdir, _run_env(cfg), timeout, stdin=description)
        return f"[coder çıkış {code}]\n{out}"
    return coder


def _code_main(workdir: str) -> int:
    """Alt surec: AgenticCoder'i yalnizca Ollama ile calistir."""
    import asyncio
    import logging
    sys.path.insert(0, str(_REPO_ROOT))
    from tools.developer import agentic_coder as ac
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    description = sys.stdin.read()
    coder = ac.AgenticCoder(model_fn=ac.ollama_generate)
    print(asyncio.run(coder.solve(description=description, project_path=workdir)))
    return 0


# ── Dongu ─────────────────────────────────────────────────────

def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tasks"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"tasks": {}}


def _save_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _description(task: Task, history: list[str]) -> str:
    text = f"{TASK_PREFIX}\n\n{task.text}"
    recent = history[-HISTORY_KEEP:]
    if recent:
        text += (f"\n\nÖNCEKİ DENEMELER BAŞARISIZ (son {len(recent)}, kısaltılmış) — bunları düzelt:\n\n"
                 + "\n\n".join(recent))
    return text


def _failure_entry(round_no: int, attempt: int, reason: str, output: str = "") -> str:
    return f"[tur {round_no}, deneme {attempt}] {reason}\n{_tail(output.strip(), HISTORY_CHARS)}".strip()


class _Runner:
    def __init__(self, cfg: Config, hooks: Hooks):
        self.cfg, self.hooks = cfg, hooks
        self.sb = cfg.sandbox
        self.start = hooks.clock()
        self.stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    def total_left(self) -> float:
        if self.cfg.max_hours <= 0:
            return float("inf")
        return self.cfg.max_hours * 3600 - (self.hooks.clock() - self.start)

    def stop_requested(self) -> str:
        stop = self.cfg.self_dir / "STOP"
        return f"STOP dosyası var ({stop})" if stop.exists() else ""

    def before_task(self) -> tuple[str, int]:
        """Yeni gorevden once: STOP, YIELD beklemesi, bekci. (neden, cikis kodu)"""
        why = self.stop_requested()
        if why:
            return why, 0
        if self.cfg.yield_jarvis:
            while self.hooks.jarvis_running():
                why = self.stop_requested()
                if why:
                    return why, 0
                if self.total_left() <= 0:
                    return f"süre sınırı doldu ({self.cfg.max_hours:g} saat)", 0
                self.hooks.sleep(YIELD_WAIT)
        why = self.watchdog()
        return why, 2 if why.startswith("Ollama") else 0

    def watchdog(self) -> str:
        if self.total_left() <= 0:
            return f"süre sınırı doldu ({self.cfg.max_hours:g} saat)"
        free = self.hooks.disk_free(self.sb)
        if free < self.cfg.min_free_gb * GIB:
            return f"boş disk az: {free / GIB:.1f} GB < {self.cfg.min_free_gb:g} GB"
        for i in range(OLLAMA_TRIES):
            if self.hooks.ollama_ok():
                return ""
            if i < OLLAMA_TRIES - 1:
                self.hooks.sleep(OLLAMA_WAIT)
        return f"Ollama erişilemedi ({OLLAMA_TRIES} deneme)"

    def process(self, task: Task, history: list[str] | None = None, round_no: int = 1) -> dict:
        """Gorevi bir tur isler. `history` (son hata ciktilari) yerinde guncellenir."""
        history = [] if history is None else history
        job = self.cfg.self_dir / "jobs" / f"{self.stamp}_{task.id}"
        job.mkdir(parents=True, exist_ok=True)
        res = {"id": task.id, "text": task.text, "status": "başarısız", "reason": "",
               "attempts": 0, "files": [], "stat": "", "seconds": 0.0, "commit": "",
               "job": str(job), "round": round_no}
        t0 = self.hooks.clock()
        task_limit = self.cfg.task_minutes * 60
        snap = snapshot(self.sb)
        ch: Changes | None = None
        attempt = 0
        try:
            for attempt in range(1, self.cfg.max_attempts + 1):
                task_left = task_limit - (self.hooks.clock() - t0)
                if task_left <= 0:
                    res["reason"] = f"görev süresi aşıldı ({self.cfg.task_minutes:g} dk)"
                    history.append(_failure_entry(round_no, attempt, res["reason"]))
                    break
                if attempt > 1 and self.total_left() <= 0:
                    raise _Stop(f"süre sınırı doldu ({self.cfg.max_hours:g} saat)")
                res["attempts"] = attempt
                log = self.hooks.coder(_description(task, history), self.sb,
                                       min(task_left, self.total_left()))
                (job / f"deneme{attempt}_coder.log").write_text(str(log), encoding="utf-8")
                ch = collect_changes(self.sb, snap)
                self._save_diff(job, attempt, snap, ch)
                res["files"], res["stat"] = sorted(ch.files), ch.stat
                why = guard_violation(self.sb, snap, ch)
                if why:
                    res["status"], res["reason"] = "reddedildi", why
                    history.append(_failure_entry(round_no, attempt, f"reddedildi: {why}"))
                    restore(self.sb, snap, ch)
                    break
                if self.hooks.clock() - t0 >= task_limit:
                    res["reason"] = f"görev süresi aşıldı ({self.cfg.task_minutes:g} dk)"
                    history.append(_failure_entry(round_no, attempt, res["reason"]))
                    restore(self.sb, snap, ch)
                    break
                ok, out = verify(self.cfg, max(60.0, task_limit - (self.hooks.clock() - t0)))
                (job / f"deneme{attempt}_dogrulama.log").write_text(out, encoding="utf-8")
                if not ok:
                    history.append(_failure_entry(round_no, attempt, "doğrulama geçmedi", out))
                    res["reason"] = f"doğrulama {attempt} denemede geçmedi"
                    restore(self.sb, snap, ch)
                    continue
                ch = collect_changes(self.sb, snap)      # dogrulama dosya degistirmis olabilir
                res["files"], res["stat"] = sorted(ch.files), ch.stat
                why = guard_violation(self.sb, snap, ch)
                if why:
                    res["status"], res["reason"] = "reddedildi", f"doğrulamadan sonra: {why}"
                    history.append(_failure_entry(round_no, attempt, res["reason"]))
                    restore(self.sb, snap, ch)
                    break
                if ch.empty:
                    res["status"], res["reason"] = "no_change", "no_change"
                    history.append(_failure_entry(round_no, attempt, "hiçbir dosya değiştirilmedi"))
                    restore(self.sb, snap, ch)
                    break
                msg = (f"self-dev: {' '.join(task.text.split())[:60]}\n\nGörev: {task.text}\n"
                       f"Tur: {round_no}\nDeneme: {attempt}\n")
                _git(self.sb, *_COMMIT_GIT, "commit", "-q", "--no-verify", "-m", msg)
                res["status"], res["reason"] = "ok", ""
                res["commit"] = _git(self.sb, "rev-parse", "HEAD").strip()
                break
        except _Stop:
            restore(self.sb, snap, ch)
            raise
        except Exception as e:
            res["status"], res["reason"] = "başarısız", f"hata: {type(e).__name__}: {str(e)[:300]}"
            history.append(_failure_entry(round_no, attempt, res["reason"]))
            with contextlib.suppress(Exception):
                restore(self.sb, snap, ch)
        res["seconds"] = round(self.hooks.clock() - t0, 1)
        if res["status"] != "ok" and not res["reason"]:
            res["reason"] = "bilinmeyen"
        return res

    def _save_diff(self, job: Path, attempt: int, snap: Snapshot, ch: Changes) -> None:
        diff = _git(self.sb, "diff", "--cached", "--binary", snap.head, check=False)
        extra = "".join(f"# yeni yok sayılan dosya: {p}\n" for p in ch.new_ignored)
        if ch.git_touched:
            extra += "# .git değişti\n"
        (job / f"deneme{attempt}.diff").write_text(extra + diff, encoding="utf-8")


def _mark(res: dict) -> str:
    return "[x]" if res["status"] == "ok" else f"[!] ({res['reason']})"


def _write_report(cfg: Config, runner: _Runner, results: list[dict], stop: str,
                  warnings: list[str]) -> Path:
    reports = cfg.self_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{runner.stamp}.md"
    head = _git(cfg.sandbox, "rev-parse", "HEAD", check=False).strip() or "?"
    lines = [f"# Jarvis self-dev raporu — {runner.stamp}", "",
             f"- Sandbox: `{cfg.sandbox}` (dal `{BRANCH}`)",
             f"- Doğrulama: `{cfg.verify}`",
             f"- Durma nedeni: {stop or 'backlog bitti (tüm görevler [x] ya da tur sınırında)'}", ""]
    if warnings:
        lines += ["## Uyarılar", "", *[f"- ⚠️ {w}" for w in warnings], ""]
    lines += ["## Görevler", ""]
    for i, r in enumerate(results, 1):
        lines += [f"### {i}. {r['text']}", "",
                  f"- Durum: **{r['status']}**" + (f" — {r['reason']}" if r["reason"] and r["status"] != "ok" else ""),
                  f"- Tur: {r.get('round', 1)}, deneme: {r['attempts']}",
                  f"- Süre: {_fmt_secs(r['seconds'])}",
                  f"- Değişen dosyalar: {', '.join(r['files']) or '-'}"]
        if r["commit"]:
            lines.append(f"- Commit: `{r['commit']}`")
        lines.append(f"- İş klasörü: `{r['job']}`")
        if r["stat"].strip():
            lines += ["", "```", r["stat"].rstrip(), "```"]
        lines.append("")
    lines += ["## Özet", "", "| # | Görev | Durum | Tur | Deneme | Süre |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(results, 1):
        text = r["text"].replace("|", "\\|")
        lines.append(f"| {i} | {text[:70]} | {r['status']} | {r.get('round', 1)} | {r['attempts']} "
                     f"| {_fmt_secs(r['seconds'])} |")
    if not results:
        lines.append("| - | (işlenen görev yok) | - | - | - | - |")
    lines += ["", f"Sandbox HEAD: `{head}`", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _check_sandbox(sb: Path) -> None:
    if not (sb / ".git").is_dir():
        raise SelfDevError(f"sandbox yok: {sb} (önce: self_dev.py init)")
    if _git(sb, "remote").strip():
        raise SelfDevError("sandbox'ta git uzağı var; güvenlik için çalışmıyor (git remote remove ...)")
    branch = _git(sb, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch != BRANCH:
        raise SelfDevError(f"sandbox dalı {branch}, beklenen {BRANCH}")


def _rounds(entry: dict | None) -> int:
    # eski state.json kayitlarinda "rounds" yok: islenmis = 1 tur
    return int(entry.get("rounds", 1)) if entry else 0


def _eligible(task: Task, state: dict, limit: int) -> bool:
    entry = state["tasks"].get(task.id)
    return task.mark == " " and not (entry and entry.get("mark") == "[x]") and _rounds(entry) < limit


def run_loop(cfg: Config, backlog: Path, hooks: Hooks, *, forever: bool = False) -> dict:
    """Backlog'daki bekleyen gorevleri isler. forever=False: tek tur, state'te
    olan gorevler tekrar denenmez. forever=True: basarisizlar sonraki turlarda
    yeniden denenir (gorev basi MAX_ROUNDS); backlog her tur basinda yeniden
    okunur. Donus: exit_code, stop_reason, report (Path | None), results."""
    out = {"exit_code": 0, "stop_reason": "", "report": None, "results": []}
    try:
        lock = acquire_lock(cfg.self_dir / "lock")
        lock.__enter__()
    except BlockingIOError:
        out.update(exit_code=3, stop_reason="kilit: başka bir self_dev örneği çalışıyor")
        return out
    runner = _Runner(cfg, hooks)
    warnings: list[str] = []
    results: list[dict] = out["results"]
    limit = MAX_ROUNDS if forever else 1
    try:
        _check_sandbox(cfg.sandbox)
        ensure_excludes(cfg.sandbox)
        if _git(cfg.sandbox, "status", "--porcelain").strip():
            warnings.append("sandbox kirliydi; son commit'e döndürüldü")
            restore(cfg.sandbox, snapshot(cfg.sandbox))
        pids = hooks.jarvis_running()
        if pids:
            warnings.append(f"Jarvis çalışıyor (PID {', '.join(map(str, pids))}): Ollama/GPU çakışması "
                            "olabilir; süreç durdurulmadı")
        state_path = cfg.sandbox / "state.json"
        state = _load_state(state_path)
        stop = ""
        while not stop:
            tasks = parse_backlog(backlog.read_text(encoding="utf-8"))
            pending = [t for t in tasks if _eligible(t, state, limit)]
            if not pending:
                break
            state["round"] = int(state.get("round", 0)) + 1
            for task in pending:
                stop, code = runner.before_task()
                if stop:
                    out["exit_code"] = code
                    break
                before = state["tasks"].get(task.id)
                entry = {**(before or {"text": task.text}), "rounds": _rounds(before) + 1,
                         "mark": "[~] (sürüyor)"}
                entry["history"] = list(entry.get("history", []))
                state["tasks"][task.id] = entry
                _save_state(state_path, state)       # surec olurse bu tur sayilir
                try:
                    res = runner.process(task, entry["history"], entry["rounds"])
                except _Stop as s:
                    # gorevin kusuru degil: turu iade et
                    if before is None:
                        del state["tasks"][task.id]
                    else:
                        state["tasks"][task.id] = before
                    _save_state(state_path, state)
                    stop = str(s)
                    break
                results.append(res)
                entry.update({k: res[k] for k in ("text", "status", "reason", "attempts",
                                                  "files", "commit", "seconds")})
                entry["mark"] = _mark(res)
                entry["history"] = entry["history"][-HISTORY_KEEP:]
                state["head"] = _git(cfg.sandbox, "rev-parse", "HEAD").strip()
                _save_state(state_path, state)
            if not forever:
                break
        out["stop_reason"] = stop
    except SelfDevError as e:
        out.update(exit_code=1, stop_reason=f"hata: {e}")
    except Exception as e:  # rapor her durumda yazilsin
        out.update(exit_code=1, stop_reason=f"beklenmeyen hata: {type(e).__name__}: {e}")
    finally:
        with contextlib.suppress(Exception):
            out["report"] = _write_report(cfg, runner, results, out["stop_reason"], warnings)
        lock.__exit__(None, None, None)
    return out


# ── systemd kullanici servisi ─────────────────────────────────

def _unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _sd_quote(value: str) -> str:
    """systemd birim dosyasi icin tirnaklama (% belirtec kacisi dahil)."""
    value = value.replace("%", "%%")
    if re.fullmatch(r"[\w@%+=:,./-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def service_unit(backlog: Path, repo: Path, sandbox: Path, self_dir: Path,
                 python: str | None = None) -> str:
    exec_start = " ".join(_sd_quote(a) for a in (
        python or sys.executable, str(Path(__file__).resolve()), "run", "--forever",
        "--backlog", str(Path(backlog).resolve())))
    env = {"JARVIS_REPO": str(repo), "JARVIS_SANDBOX": str(sandbox), "JARVIS_SELF_DIR": str(self_dir),
           "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    for key in ("OLLAMA_HOST", "JARVIS_SELF_DEV_YIELD", "JARVIS_OLLAMA_NUM_CTX"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    env_lines = [f"Environment={_sd_quote(f'{k}={v}')}" for k, v in env.items()]
    return "\n".join([
        "[Unit]",
        "Description=Jarvis self-dev (sandbox) döngüsü",
        "After=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={_sd_quote(str(sandbox))}",
        f"ExecStart={exec_start}",
        *env_lines,
        "StandardInput=null",
        "Restart=on-failure",
        "RestartSec=300",
        "Nice=10",
        "TimeoutStopSec=30",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def install_service(backlog: Path, repo: Path, sandbox: Path, self_dir: Path) -> Path:
    """Yalnizca birim dosyasini yazar; systemctl CAGIRMAZ."""
    if not Path(backlog).is_file():
        raise SelfDevError(f"backlog yok: {backlog}")
    unit = _unit_dir() / SERVICE_NAME
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(service_unit(backlog, repo, sandbox, self_dir), encoding="utf-8")
    return unit


def uninstall_service() -> list[Path]:
    """Birim dosyasini ve (enable edilmisse) wants baglantisini siler; systemctl CAGIRMAZ."""
    removed = []
    for path in (_unit_dir() / "default.target.wants" / SERVICE_NAME, _unit_dir() / SERVICE_NAME):
        if path.is_symlink() or path.exists():
            path.unlink()
            removed.append(path)
    return removed


# ── CLI ───────────────────────────────────────────────────────

def _default_paths() -> tuple[Path, Path, Path]:
    sandbox = Path(os.environ.get("JARVIS_SANDBOX", "~/jarvis-sandbox")).expanduser()
    self_dir = Path(os.environ.get("JARVIS_SELF_DIR", "~/.jarvis-self")).expanduser()
    repo = Path(os.environ.get("JARVIS_REPO", str(_REPO_ROOT))).expanduser()
    return repo, sandbox, self_dir


def main(argv: list[str] | None = None) -> int:
    repo, sandbox, self_dir = _default_paths()
    p = argparse.ArgumentParser(description="Jarvis sandbox self-dev döngüsü")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init", help="sandbox klonunu kur")
    pi.add_argument("--no-venv", action="store_true", help=".venv kurma")
    pi.add_argument("--no-deps", action="store_true", help=".venv'e bağımlılık kurma")
    pr = sub.add_parser("run", help="backlog görevlerini işle")
    pr.add_argument("--backlog", type=Path, required=True)
    pr.add_argument("--forever", action="store_true",
                    help=f"tur tur çalış: başarısızları yeniden dene (görev başı {MAX_ROUNDS} tur)")
    pr.add_argument("--max-hours", type=float, default=None,
                    help="toplam süre sınırı; varsayılan 4, --forever ile sınırsız (0)")
    pr.add_argument("--max-attempts", type=int, default=5)
    pr.add_argument("--task-minutes", type=float, default=30.0)
    pr.add_argument("--min-free-gb", type=float, default=5.0)
    pr.add_argument("--verify", default=DEFAULT_VERIFY, help="';' ile ayrılmış komutlar")
    ps = sub.add_parser("install-service", help=f"~/.config/systemd/user/{SERVICE_NAME} yaz")
    ps.add_argument("--backlog", type=Path, required=True)
    sub.add_parser("uninstall-service", help="servis dosyasını sil")
    pc = sub.add_parser("_code", help=argparse.SUPPRESS)
    pc.add_argument("--workdir", required=True)
    args = p.parse_args(argv)

    if args.cmd == "_code":
        return _code_main(args.workdir)
    if args.cmd == "init":
        try:
            init_sandbox(repo, sandbox, make_venv=not args.no_venv, install_deps=not args.no_deps)
        except (SelfDevError, subprocess.CalledProcessError) as e:
            print(f"HATA: {e}", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "install-service":
        try:
            unit = install_service(args.backlog, repo, sandbox, self_dir)
        except SelfDevError as e:
            print(f"HATA: {e}", file=sys.stderr)
            return 1
        print(f"Servis dosyası yazıldı: {unit}")
        print("Hiçbir systemctl komutu çalıştırılmadı. Etkinleştirmek için:")
        print("  systemctl --user daemon-reload")
        print(f"  systemctl --user enable --now {SERVICE_NAME}")
        print(f"Oturum kapalıyken de çalışsın isterseniz: loginctl enable-linger {os.environ.get('USER', '$USER')}")
        print(f"Durdurmak: touch {self_dir / 'STOP'}  (görev bitince çıkar)")
        print(f"Günlük: journalctl --user -u {SERVICE_NAME} -f")
        return 0
    if args.cmd == "uninstall-service":
        removed = uninstall_service()
        for path in removed:
            print(f"Silindi: {path}")
        if not removed:
            print("Servis dosyası yoktu.")
        print("Hiçbir systemctl komutu çalıştırılmadı. Çalışan servisi durdurmak için:")
        print(f"  systemctl --user stop {SERVICE_NAME}")
        print("  systemctl --user daemon-reload")
        return 0
    max_hours = args.max_hours if args.max_hours is not None else (0.0 if args.forever else 4.0)
    cfg = Config(repo=repo, sandbox=sandbox, self_dir=self_dir, verify=args.verify,
                 max_hours=max_hours, max_attempts=max(1, args.max_attempts),
                 task_minutes=args.task_minutes, min_free_gb=args.min_free_gb)
    res = run_loop(cfg, args.backlog, Hooks(coder=make_subprocess_coder(cfg)), forever=args.forever)
    for r in res["results"]:
        print(f"{_mark(r):40.40}  tur {r['round']}  {r['text'][:70]}")
    if res["stop_reason"]:
        print(f"Durdu: {res['stop_reason']}", file=sys.stderr if res["exit_code"] else sys.stdout)
    if res["report"]:
        print(f"Rapor: {res['report']}")
    return res["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
