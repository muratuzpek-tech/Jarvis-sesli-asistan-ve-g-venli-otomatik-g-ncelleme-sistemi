"""tests/test_self_dev.py — sandbox'ta kendi kendini gelistirme dongusu

scripts/self_dev.py (init / run) ve scripts/self_dev_promote.sh GERCEK git ile
sinanir; "ana depo" tmp_path altinda kucuk bir git deposudur (gercek Jarvis
deposu degil). HOME tmp_path altindadir; AgenticCoder yerine senaryolu sahte
coder, Ollama/disk/saat/Jarvis-sureci kontrolleri yerine enjekte fonksiyonlar
kullanilir. Gercek ag, gercek Ollama, gercek MuratJARVIS verisi yok.
"""
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = ROOT / "scripts" / "self_dev.py"
_PROMOTE = ROOT / "scripts" / "self_dev_promote.sh"


@pytest.fixture(scope="module")
def sd():
    spec = importlib.util.spec_from_file_location("self_dev", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["self_dev"] = mod
    spec.loader.exec_module(mod)
    return mod


def git(cwd, *args, check=True) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and out.returncode != 0:
        raise AssertionError(f"git {args}: {out.stderr}")
    return out.stdout.strip()


CALC = "def add(a, b):\n    return a + b\n"
TEST_CALC = (
    "from calc import add\n\n\n"
    "def test_add():\n    assert add(2, 3) == 5\n\n\n"
    "def test_add_neg():\n    assert add(-1, 1) == 0\n"
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    for k in ("JARVIS_SANDBOX", "JARVIS_SELF_DIR", "JARVIS_REPO"):
        monkeypatch.delenv(k, raising=False)
    repo = tmp_path / "anadepo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "t@example.invalid")
    (repo / "calc.py").write_text(CALC)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(TEST_CALC)
    (repo / "README.md").write_text("# mini\n")
    (repo / ".gitignore").write_text(".env\n__pycache__/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "ilk")
    (repo / ".env").write_text("GEMINI_API_KEY=gizli\n")            # izlenmeyen sir
    (repo / "yarim.txt").write_text("commit edilmemis is\n")         # kirli calisma agaci
    return {"home": home, "repo": repo, "sandbox": home / "jarvis-sandbox",
            "self": home / ".jarvis-self", "tmp": tmp_path}


def repo_snapshot(repo: Path) -> dict:
    return {
        "refs": git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "head": git(repo, "rev-parse", "HEAD"),
        "count": git(repo, "rev-list", "--count", "--all"),
        "status": git(repo, "status", "--porcelain", "--ignored"),
        "config": (repo / ".git" / "config").read_text(),
    }


@pytest.fixture
def sandbox(sd, env):
    sd.init_sandbox(env["repo"], env["sandbox"], make_venv=False)
    return env["sandbox"]


VERIFY = f"{sys.executable} -m pytest -q -p no:cacheprovider"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make_cfg(sd, env, **kw):
    kw.setdefault("verify", VERIFY)
    kw.setdefault("max_attempts", 2)
    return sd.Config(repo=env["repo"], sandbox=env["sandbox"], self_dir=env["self"], **kw)


def make_hooks(sd, coder, **kw):
    kw.setdefault("clock", Clock())
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("disk_free", lambda p: 100 * 2**30)
    kw.setdefault("ollama_ok", lambda: True)
    kw.setdefault("jarvis_running", lambda: [])
    return sd.Hooks(coder=coder, **kw)


def write_backlog(env, *lines) -> Path:
    path = env["tmp"] / "backlog.md"
    path.write_text("# Görevler\n\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return path


def is_clean(sandbox: Path) -> bool:
    return git(sandbox, "status", "--porcelain") == ""


# ── 1) init ──

def test_init_clone_without_origin_and_main_repo_untouched(sd, env):
    before = repo_snapshot(env["repo"])
    sd.init_sandbox(env["repo"], env["sandbox"], make_venv=False)
    sb = env["sandbox"]
    assert git(sb, "remote") == ""
    push = subprocess.run(["git", "push"], cwd=sb, capture_output=True, text=True)
    assert push.returncode != 0
    push2 = subprocess.run(["git", "push", "origin", "jarvis/self-dev"], cwd=sb,
                           capture_output=True, text=True)
    assert push2.returncode != 0
    assert git(sb, "rev-parse", "--abbrev-ref", "HEAD") == "jarvis/self-dev"
    assert git(sb, "rev-parse", "HEAD") == before["head"]
    assert not (sb / ".env").exists() and not (sb / "yarim.txt").exists()
    # --no-hardlinks: nesne dosyalari ana depoyla paylasilmaz
    objs = [p for p in (sb / ".git" / "objects").rglob("*") if p.is_file()]
    assert objs and all(p.stat().st_nlink == 1 for p in objs)
    assert repo_snapshot(env["repo"]) == before
    with pytest.raises(sd.SelfDevError):
        sd.init_sandbox(env["repo"], env["sandbox"], make_venv=False)   # ikinci kez yok


def test_init_refuses_tracked_secret(sd, env):
    (env["repo"] / "id_rsa").write_text("-----BEGIN-----\n")
    git(env["repo"], "add", "id_rsa")
    git(env["repo"], "commit", "-q", "-m", "kaza")
    with pytest.raises(sd.SelfDevError, match="id_rsa"):
        sd.init_sandbox(env["repo"], env["sandbox"], make_venv=False)
    assert not env["sandbox"].exists()


# ── 2) backlog ──

def test_parse_backlog_marks_and_continuations(sd):
    tasks = sd.parse_backlog(
        "# Başlık\n"
        "- [ ] Tkinter hesap makinesi örneklerinde eval yerine güvenli matematik\n"
        "      (safe_math) kullanımı; test ekle\n"
        "- [x] bitti\n"
        "- [!] (neden) olmadı\n"
        "rastgele satır\n"
        "- [ ] ikinci görev\n"
    )
    assert [(t.mark, t.text) for t in tasks] == [
        (" ", "Tkinter hesap makinesi örneklerinde eval yerine güvenli matematik "
              "(safe_math) kullanımı; test ekle"),
        ("x", "bitti"),
        ("!", "(neden) olmadı"),
        (" ", "ikinci görev"),
    ]
    assert len({t.id for t in tasks}) == 4
    assert sd.parse_backlog("- [ ] ikinci görev\n")[0].id == tasks[3].id   # kararlı kimlik


# ── 3) run: biri gecer, biri dogrulamada kalir ──

MUL = CALC + "\n\ndef mul(a, b):\n    return a * b\n"
TEST_MUL = "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n"
BROKEN = "def add(a, b):\n    return a - b\n"


def test_run_commits_passing_task_and_reverts_failing_one(sd, env, sandbox):
    base = git(sandbox, "rev-parse", "HEAD")
    before = repo_snapshot(env["repo"])
    prompts: list[str] = []

    def coder(description, workdir, timeout):
        prompts.append(description)
        if "bozuk" in description:
            (workdir / "calc.py").write_text(BROKEN)
            return "bozuk yazdım"
        (workdir / "calc.py").write_text(MUL)
        (workdir / "tests" / "test_mul.py").write_text(TEST_MUL)
        return "çarpma eklendi"

    backlog = write_backlog(env, "- [ ] bozuk özellik ekle", "- [ ] çarpma fonksiyonu ekle",
                            "- [x] eskiden bitmiş")
    backlog_text = backlog.read_text()
    res = sd.run_loop(make_cfg(sd, env), backlog, make_hooks(sd, coder))
    assert res["exit_code"] == 0 and res["stop_reason"] == ""

    by = {r["text"]: r for r in res["results"]}
    bad, good = by["bozuk özellik ekle"], by["çarpma fonksiyonu ekle"]
    assert bad["status"] == "başarısız" and bad["attempts"] == 2
    assert good["status"] == "ok" and good["attempts"] == 1
    assert sorted(good["files"]) == ["calc.py", "tests/test_mul.py"]
    # ikinci deneme, ilk denemenin dogrulama ciktisini baglam olarak alir
    bad_prompts = [p for p in prompts if "bozuk" in p]
    assert len(bad_prompts) == 2 and "test_add" in bad_prompts[1] and "test_add" not in bad_prompts[0]

    log = git(sandbox, "log", "--format=%s", f"{base}..HEAD").splitlines()
    assert len(log) == 1 and "çarpma" in log[0]
    assert (sandbox / "calc.py").read_text() == MUL
    assert is_clean(sandbox)
    assert git(sandbox, "rev-parse", "--abbrev-ref", "HEAD") == "jarvis/self-dev"

    # state.json: [x] / [!]; backlog dosyasi ve ana depo degismez
    state = json.loads((sandbox / "state.json").read_text())
    marks = {t["text"]: t["mark"] for t in state["tasks"].values()}
    assert marks["çarpma fonksiyonu ekle"] == "[x]"
    assert marks["bozuk özellik ekle"].startswith("[!] (")
    assert "eskiden bitmiş" not in marks
    assert backlog.read_text() == backlog_text
    assert repo_snapshot(env["repo"]) == before

    # rapor + is klasoru sandbox DISINDA
    report = res["report"]
    assert report.parent == env["self"] / "reports"
    text = report.read_text()
    assert "çarpma fonksiyonu ekle" in text and "başarısız" in text and "ok" in text
    assert git(sandbox, "rev-parse", "HEAD") in text
    assert "calc.py" in text and "tests/test_mul.py" in text
    jobs = list((env["self"] / "jobs").iterdir())
    assert len(jobs) == 2
    assert any(any(p.suffix == ".diff" for p in j.iterdir()) for j in jobs)

    # ikinci calistirma: biten/basarisiz gorevler tekrar denenmez
    prompts.clear()
    res2 = sd.run_loop(make_cfg(sd, env), backlog, make_hooks(sd, coder))
    assert prompts == [] and res2["results"] == []


def test_task_exception_does_not_stop_loop(sd, env, sandbox):
    def coder(description, workdir, timeout):
        if "patla" in description:
            (workdir / "calc.py").write_text("yarım")
            raise RuntimeError("coder çöktü")
        (workdir / "calc.py").write_text(MUL)
        return "ok"

    backlog = write_backlog(env, "- [ ] patla", "- [ ] çarpma ekle")
    res = sd.run_loop(make_cfg(sd, env), backlog, make_hooks(sd, coder))
    by = {r["text"]: r for r in res["results"]}
    assert by["patla"]["status"] == "başarısız" and "coder çöktü" in by["patla"]["reason"]
    assert by["çarpma ekle"]["status"] == "ok"
    assert is_clean(sandbox)


def test_no_change_when_verify_passes_without_diff(sd, env, sandbox):
    base = git(sandbox, "rev-parse", "HEAD")
    backlog = write_backlog(env, "- [ ] hiçbir şey yapma")
    res = sd.run_loop(make_cfg(sd, env), backlog, make_hooks(sd, lambda d, w, t: "boş"))
    (r,) = res["results"]
    assert r["status"] == "no_change" and r["attempts"] == 1
    assert git(sandbox, "rev-parse", "HEAD") == base
    state = json.loads((sandbox / "state.json").read_text())
    assert next(iter(state["tasks"].values()))["mark"] == "[!] (no_change)"


# ── 4) koruma kurallari ──

def _env_file(w):
    (w / ".env").write_text("TOKEN=x\n")             # .gitignore'da: git gormez ama yine yakalanmali


def _env_local(w):
    (w / ".env.local").write_text("TOKEN=x\n")


def _github(w):
    (w / ".github" / "workflows").mkdir(parents=True)
    (w / ".github" / "workflows" / "x.yml").write_text("on: push\n")


def _pyproject(w):
    (w / "pyproject.toml").write_text("[project]\ndependencies = ['requests']\n")


def _self_dev(w):
    (w / "scripts").mkdir()
    (w / "scripts" / "self_dev.py").write_text("print('ben')\n")


def _git_hook(w):
    (w / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")


def _delete_test(w):
    (w / "tests" / "test_calc.py").unlink()


def _remove_test_fn(w):
    (w / "tests" / "test_calc.py").write_text(TEST_CALC.split("\n\n\ndef test_add_neg")[0] + "\n")


def _add_skip(w):
    (w / "tests" / "test_calc.py").write_text(
        "import pytest\n" + TEST_CALC.replace("def test_add():", "@pytest.mark.skip\ndef test_add():"))


def _add_xfail_call(w):
    (w / "tests" / "test_calc.py").write_text(
        TEST_CALC.replace("    assert add(2, 3) == 5", "    import pytest\n    pytest.xfail('x')\n    assert add(2, 3) == 5"))


def _fewer_asserts(w):
    (w / "tests" / "test_calc.py").write_text(TEST_CALC.replace("assert add(-1, 1) == 0", "add(-1, 1)"))


def _symlink_escape(w, outside):
    (w / "kacis").symlink_to(outside)
    (w / "kacis" / "yazildi.txt").write_text("dışarı\n")


def _many_files(w):
    for i in range(16):
        (w / f"m{i}.py").write_text(f"X = {i}\n")


def _many_lines(w):
    (w / "buyuk.py").write_text("".join(f"X{i} = {i}\n" for i in range(801)))


GUARD_CASES = [
    (_env_file, "korumalı"), (_env_local, "korumalı"), (_github, "korumalı"),
    (_pyproject, "korumalı"), (_self_dev, "korumalı"), (_git_hook, ".git"),
    (_delete_test, "test"), (_remove_test_fn, "test"), (_add_skip, "skip"),
    (_add_xfail_call, "skip"), (_fewer_asserts, "assert"), (_symlink_escape, "dışına"),
    (_many_files, "büyük"), (_many_lines, "büyük"),
]


@pytest.mark.parametrize("action, reason", GUARD_CASES, ids=[f.__name__ for f, _ in GUARD_CASES])
def test_guard_rejects_and_restores_sandbox(sd, env, sandbox, action, reason):
    base = git(sandbox, "rev-parse", "HEAD")
    outside = env["tmp"] / "disari"
    outside.mkdir()
    hooks_before = sorted(p.name for p in (sandbox / ".git" / "hooks").iterdir())

    def coder(description, workdir, timeout):
        if action is _symlink_escape:
            action(workdir, outside)
        else:
            action(workdir)
        return "yaptım"

    cfg = make_cfg(sd, env, verify=f"{sys.executable} -c \"open('dogrulandi','w')\"")
    backlog = write_backlog(env, "- [ ] kötü görev")
    res = sd.run_loop(cfg, backlog, make_hooks(sd, coder))
    (r,) = res["results"]
    assert r["status"] == "reddedildi", r
    assert reason in r["reason"], r["reason"]
    assert r["attempts"] == 1
    assert git(sandbox, "rev-parse", "HEAD") == base
    assert is_clean(sandbox)
    assert not (sandbox / ".env").exists() and not (sandbox / "kacis").exists()
    assert sorted(p.name for p in (sandbox / ".git" / "hooks").iterdir()) == hooks_before
    assert not (sandbox / "dogrulandi").exists()   # dogrulama hic calismadi
    assert reason in res["report"].read_text()
    state = json.loads((sandbox / "state.json").read_text())
    assert next(iter(state["tasks"].values()))["mark"].startswith("[!] (")


def test_guard_allows_normal_test_additions(sd, env, sandbox):
    def coder(description, workdir, timeout):
        (workdir / "tests" / "test_calc.py").write_text(
            TEST_CALC + "\n\ndef test_add_zero():\n    assert add(0, 0) == 0\n")
        return "ok"

    res = sd.run_loop(make_cfg(sd, env), write_backlog(env, "- [ ] test ekle"), make_hooks(sd, coder))
    assert res["results"][0]["status"] == "ok"


# ── 5) bekci ──

def test_low_disk_stops_loop(sd, env, sandbox):
    calls: list = []
    hooks = make_hooks(sd, lambda d, w, t: calls.append(d) or "x", disk_free=lambda p: 4 * 2**30)
    res = sd.run_loop(make_cfg(sd, env), write_backlog(env, "- [ ] a", "- [ ] b"), hooks)
    assert calls == [] and res["results"] == []
    assert "disk" in res["stop_reason"]
    assert "disk" in res["report"].read_text()
    assert not (sandbox / "state.json").exists() or json.loads(
        (sandbox / "state.json").read_text())["tasks"] == {}


def test_total_time_limit_stops_loop(sd, env, sandbox):
    clock = Clock()
    calls: list = []

    def coder(description, workdir, timeout):
        calls.append(description)
        assert timeout <= 30 * 60
        clock.t += 20 * 60                        # gorev 20 dk surdu (gorev siniri 30 dk)
        (workdir / "calc.py").write_text(MUL)
        return "ok"

    hooks = make_hooks(sd, coder, clock=clock)
    res = sd.run_loop(make_cfg(sd, env, max_hours=0.25), write_backlog(env, "- [ ] a", "- [ ] b"), hooks)
    assert len(calls) == 1
    assert [r["status"] for r in res["results"]] == ["ok"]
    assert "süre" in res["stop_reason"]


def test_task_time_limit_fails_task(sd, env, sandbox):
    clock = Clock()

    def coder(description, workdir, timeout):
        clock.t += 31 * 60
        (workdir / "calc.py").write_text(BROKEN)
        return "yavaş"

    hooks = make_hooks(sd, coder, clock=clock)
    res = sd.run_loop(make_cfg(sd, env, max_attempts=5), write_backlog(env, "- [ ] yavaş", "- [ ] b"), hooks)
    first = res["results"][0]
    assert first["status"] == "başarısız" and first["attempts"] == 1 and "süre" in first["reason"]
    assert len(res["results"]) == 2                 # dongu devam etti
    assert is_clean(sandbox)


def test_ollama_unreachable_retries_then_exits_cleanly(sd, env, sandbox):
    sleeps: list = []
    calls: list = []
    hooks = make_hooks(sd, lambda d, w, t: calls.append(d) or "x",
                       ollama_ok=lambda: False, sleep=sleeps.append)
    res = sd.run_loop(make_cfg(sd, env), write_backlog(env, "- [ ] a"), hooks)
    assert calls == [] and len(sleeps) == 2         # 3 deneme, aralarda 2 bekleme
    assert res["exit_code"] == 2 and "Ollama" in res["stop_reason"]
    assert "Ollama" in res["report"].read_text()


def test_running_jarvis_is_reported_not_killed(sd, env, sandbox):
    hooks = make_hooks(sd, lambda d, w, t: "x", jarvis_running=lambda: [4242])
    res = sd.run_loop(make_cfg(sd, env), write_backlog(env, "- [ ] a"), hooks)
    text = res["report"].read_text()
    assert "4242" in text and "GPU" in text
    assert res["results"][0]["status"] == "no_change"


# ── 6) kilit ──

def test_second_instance_does_not_start(sd, env, sandbox):
    lock = env["self"] / "lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    calls: list = []
    with open(lock, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        res = sd.run_loop(make_cfg(sd, env), write_backlog(env, "- [ ] a"),
                          make_hooks(sd, lambda d, w, t: calls.append(d) or "x"))
    assert res["exit_code"] == 3 and "kilit" in res["stop_reason"].lower()
    assert calls == []
    # kilit birakilinca calisir
    res2 = sd.run_loop(make_cfg(sd, env), write_backlog(env, "- [ ] a"),
                       make_hooks(sd, lambda d, w, t: calls.append(d) or "x"))
    assert res2["exit_code"] == 0 and len(calls) == 1


# ── 7) promote ──

def test_promote_exports_patches_without_touching_main_repo(sd, env, sandbox):
    def coder(description, workdir, timeout):
        (workdir / "calc.py").write_text(MUL)
        (workdir / "tests" / "test_mul.py").write_text(TEST_MUL)
        return "ok"

    sd.run_loop(make_cfg(sd, env), write_backlog(env, "- [ ] çarpma ekle"), make_hooks(sd, coder))
    before = repo_snapshot(env["repo"])
    run_env = {**os.environ, "JARVIS_SANDBOX": str(sandbox), "JARVIS_REPO": str(env["repo"]),
               "JARVIS_SELF_DIR": str(env["self"])}
    out = subprocess.run(["bash", str(_PROMOTE)], capture_output=True, text=True, env=run_env)
    assert out.returncode == 0, out.stderr
    patches = sorted((env["self"] / "out").rglob("*.patch"))
    assert len(patches) == 1 and "def mul" in patches[0].read_text()
    assert "git am --3way" in out.stdout and str(patches[0]) in out.stdout
    assert repo_snapshot(env["repo"]) == before
    assert git(sandbox, "remote") == ""
    # yama ana deponun bir kopyasina temiz uygulanir
    copy = env["tmp"] / "kopya"
    git(env["tmp"], "clone", "-q", str(env["repo"]), str(copy))
    git(copy, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "am", "--3way", str(patches[0]))
    assert (copy / "calc.py").read_text() == MUL


def test_promote_with_nothing_to_export(sd, env, sandbox):
    run_env = {**os.environ, "JARVIS_SANDBOX": str(sandbox), "JARVIS_REPO": str(env["repo"]),
               "JARVIS_SELF_DIR": str(env["self"])}
    out = subprocess.run(["bash", str(_PROMOTE)], capture_output=True, text=True, env=run_env)
    assert out.returncode == 0, out.stderr
    assert "yok" in out.stdout
    assert not list((env["self"] / "out").rglob("*.patch")) if (env["self"] / "out").exists() else True


# ── 8) HOME izolasyonu ──

def test_verify_and_coder_env_redirect_home(sd, env, sandbox, monkeypatch):
    fake_home = env["self"] / "home"
    writer = ("(__import__('pathlib').Path.home() / 'izle.txt')"   # ';' dogrulamada ayiricidir
              ".write_text(__import__('os').environ['XDG_CONFIG_HOME'])")
    cfg = make_cfg(sd, env, verify=f"{sys.executable} -c \"{writer}\"")
    before = sorted(p.name for p in env["home"].iterdir())
    ok, out = sd.verify(cfg, 60)
    assert ok, out
    assert (fake_home / "izle.txt").read_text().startswith(str(fake_home))
    assert not (env["home"] / "izle.txt").exists()
    after = sorted(p.name for p in env["home"].iterdir())
    assert after == sorted({*before, ".jarvis-self"})        # gercek HOME'a yeni dosya yok

    seen: dict = {}
    monkeypatch.setattr(sd, "_run_group",
                        lambda argv, cwd, env, timeout, stdin=None: (seen.update(env), (0, ""))[1])
    sd.make_subprocess_coder(cfg)("görev", sandbox, 10)
    # coder: HOME sandbox'i kapsar (AgenticCoder $HOME denetimi), gercek HOME degil
    assert seen["HOME"] == str(sandbox.resolve())
    assert all(seen[k].startswith(str(fake_home))
               for k in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"))


# ── 9) sabit gorev oneki ──

PREFIX = ("Önce başarısız test yaz, sonra düzelt. Mevcut testleri silme/zayıflatma, "
          "skip/xfail ekleme, bağımlılık ekleme.")


def test_task_prompt_starts_with_fixed_prefix(sd, env, sandbox):
    prompts: list[str] = []
    sd.run_loop(make_cfg(sd, env), write_backlog(env, "- [ ] bir şey yap"),
                make_hooks(sd, lambda d, w, t: prompts.append(d) or "x"))
    assert prompts and prompts[0].startswith(PREFIX)
    assert "bir şey yap" in prompts[0]


# ── 10) --forever: turlar ──

def test_forever_retries_failed_task_with_last_two_failures(sd, env, sandbox):
    backlog = write_backlog(env, "- [ ] çarpma ekle")
    calls: list[str] = []

    def coder(description, workdir, timeout):
        calls.append(description)
        if len(calls) == 1:                       # prompts.md tur sirasinda degisti
            with backlog.open("a", encoding="utf-8") as fh:
                fh.write("- [ ] yeni görev\n")
        if "yeni görev" in description:
            (workdir / "yeni.py").write_text("X = 1\n")
            return "ok"
        if len(calls) < 4:
            (workdir / "calc.py").write_text(BROKEN)
            return "bozuk"
        (workdir / "calc.py").write_text(MUL)
        (workdir / "tests" / "test_mul.py").write_text(TEST_MUL)
        return "ok"

    res = sd.run_loop(make_cfg(sd, env, max_attempts=2), backlog, make_hooks(sd, coder), forever=True)
    assert res["exit_code"] == 0, res
    assert "ÖNCEKİ" not in calls[0]
    assert "[tur 1, deneme 1]" in calls[1]
    assert "[tur 1, deneme 1]" in calls[2] and "[tur 1, deneme 2]" in calls[2]
    assert ("[tur 1, deneme 1]" not in calls[3] and "[tur 1, deneme 2]" in calls[3]
            and "[tur 2, deneme 1]" in calls[3])          # yalnizca son 2 deneme
    assert "yeni görev" in calls[4] and len(calls) == 5
    assert [(r["text"], r["status"], r["round"]) for r in res["results"]] == [
        ("çarpma ekle", "başarısız", 1), ("çarpma ekle", "ok", 2), ("yeni görev", "ok", 1)]
    state = json.loads((sandbox / "state.json").read_text())
    by = {t["text"]: t for t in state["tasks"].values()}
    assert by["çarpma ekle"]["rounds"] == 2 and by["çarpma ekle"]["mark"] == "[x]"
    assert by["yeni görev"]["rounds"] == 1 and by["yeni görev"]["mark"] == "[x]"
    assert "Tur" in res["report"].read_text()
    assert is_clean(sandbox)


def test_forever_stops_after_three_rounds_with_truncated_history(sd, env, sandbox):
    # yalnizca coder'in degisikliginden sonra kirmizi (onkontrol yesil gecsin)
    noisy = (f"{sys.executable} -c \"exit(print('Z' * 20000) or 'mul' in open('calc.py').read())\"")
    calls: list[str] = []

    def coder(description, workdir, timeout):
        calls.append(description)
        (workdir / "calc.py").write_text(MUL)
        return "x"

    cfg = make_cfg(sd, env, verify=noisy, max_attempts=1)
    backlog = write_backlog(env, "- [ ] imkansız")
    res = sd.run_loop(cfg, backlog, make_hooks(sd, coder), forever=True)
    assert len(calls) == 3
    assert all(len(c) < 5000 for c in calls)
    assert calls[2].count("[tur ") == 2
    assert [r["round"] for r in res["results"]] == [1, 2, 3]
    assert res["exit_code"] == 0 and res["report"].exists()
    (task,) = json.loads((sandbox / "state.json").read_text())["tasks"].values()
    assert task["rounds"] == 3 and task["mark"].startswith("[!]")
    res2 = sd.run_loop(cfg, backlog, make_hooks(sd, coder), forever=True)
    assert len(calls) == 3 and res2["results"] == []


class Crash(BaseException):
    """Surecin olmesini taklit eder (Exception yakalayicilarindan kacar)."""


def test_forever_resumes_after_process_death(sd, env, sandbox):
    crash = {"on": True}
    calls: list[str] = []

    def coder(description, workdir, timeout):
        calls.append(description)
        if "ikinci" in description:
            if crash["on"]:
                (workdir / "calc.py").write_text("yarım")
                raise Crash()
            (workdir / "yeni.py").write_text("X = 1\n")
            return "ok"
        (workdir / "calc.py").write_text(MUL)
        return "ok"

    backlog = write_backlog(env, "- [ ] birinci", "- [ ] ikinci")
    with pytest.raises(Crash):
        sd.run_loop(make_cfg(sd, env), backlog, make_hooks(sd, coder), forever=True)
    state = json.loads((sandbox / "state.json").read_text())
    by = {t["text"]: t for t in state["tasks"].values()}
    assert by["birinci"]["mark"] == "[x]"
    assert by["ikinci"]["rounds"] == 1 and by["ikinci"]["mark"].startswith("[~]")
    assert not is_clean(sandbox)

    crash["on"] = False
    calls.clear()
    res = sd.run_loop(make_cfg(sd, env), backlog, make_hooks(sd, coder), forever=True)
    assert len(calls) == 1 and "ikinci" in calls[0]
    assert [(r["text"], r["status"], r["round"]) for r in res["results"]] == [("ikinci", "ok", 2)]
    assert is_clean(sandbox)


# ── 11) STOP + YIELD ──

def test_stop_file_exits_cleanly_after_current_task(sd, env, sandbox):
    stop = env["self"] / "STOP"
    calls: list[str] = []

    def coder(description, workdir, timeout):
        calls.append(description)
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.touch()
        (workdir / "calc.py").write_text(MUL)
        return "ok"

    backlog = write_backlog(env, "- [ ] a", "- [ ] b")
    res = sd.run_loop(make_cfg(sd, env), backlog, make_hooks(sd, coder), forever=True)
    assert len(calls) == 1 and res["results"][0]["status"] == "ok"
    assert res["exit_code"] == 0 and "STOP" in res["stop_reason"]
    assert "STOP" in res["report"].read_text()
    assert is_clean(sandbox)
    res2 = sd.run_loop(make_cfg(sd, env), backlog, make_hooks(sd, coder), forever=True)
    assert len(calls) == 1 and res2["results"] == [] and "STOP" in res2["stop_reason"]


def test_yield_waits_while_jarvis_runs(sd, env, sandbox, monkeypatch):
    monkeypatch.setenv("JARVIS_SELF_DEV_YIELD", "1")
    running = [[4242], [4242], [4242], []]           # ilk cagri: baslangic uyarisi
    sleeps: list = []
    calls: list[str] = []

    def coder(description, workdir, timeout):
        assert running == []                          # Jarvis kapanmadan baslamadi
        calls.append(description)
        return "x"

    cfg = make_cfg(sd, env)
    assert cfg.yield_jarvis
    hooks = make_hooks(sd, coder, sleep=sleeps.append,
                       jarvis_running=lambda: running.pop(0) if running else [])
    res = sd.run_loop(cfg, write_backlog(env, "- [ ] a"), hooks)
    assert len(calls) == 1 and sleeps == [sd.YIELD_WAIT, sd.YIELD_WAIT]
    assert res["exit_code"] == 0

    # beklerken STOP gelirse gorev baslamadan cikar
    stop = env["self"] / "STOP"
    hooks = make_hooks(sd, coder, jarvis_running=lambda: [4242],
                       sleep=lambda s: stop.touch())
    res = sd.run_loop(cfg, write_backlog(env, "- [ ] b"), hooks)
    assert len(calls) == 1 and "STOP" in res["stop_reason"]


def test_yield_off_by_default(sd, env, monkeypatch):
    monkeypatch.delenv("JARVIS_SELF_DEV_YIELD", raising=False)
    assert not make_cfg(sd, env).yield_jarvis


# ── 12) systemd kullanici servisi + stdin kapali ──

def _closed_stdin_run(argv, run_env):
    return subprocess.run(argv, env=run_env, capture_output=True, text=True,
                          preexec_fn=lambda: os.close(0), timeout=300)


def _fake_systemctl(env) -> tuple[dict, Path]:
    bin_dir = env["tmp"] / "bin"
    bin_dir.mkdir(exist_ok=True)
    marker = env["tmp"] / "systemctl_cagrildi"
    tool = bin_dir / "systemctl"
    tool.write_text(f"#!/bin/sh\necho \"$@\" >> '{marker}'\n")
    tool.chmod(0o755)
    run_env = {**os.environ, "HOME": str(env["home"]),
               "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return run_env, marker


def test_install_and_uninstall_service_only_write_unit_file(sd, env):
    run_env, marker = _fake_systemctl(env)
    backlog = write_backlog(env, "- [ ] a")
    out = _closed_stdin_run([sys.executable, str(_SCRIPT), "install-service", "--backlog", str(backlog)],
                            run_env)
    assert out.returncode == 0, out.stderr
    unit = env["home"] / ".config" / "systemd" / "user" / "jarvis-self-dev.service"
    assert [p for p in env["home"].rglob("*") if p.is_file()] == [unit]
    text = unit.read_text()
    for needle in ("Restart=on-failure", "Nice=10", "--forever", str(backlog.resolve()),
                   "run", "WantedBy=default.target", "StandardInput=null"):
        assert needle in text, needle
    assert "User=" not in text and "sudo" not in text
    assert "systemctl --user daemon-reload" in out.stdout
    assert "systemctl --user enable --now jarvis-self-dev.service" in out.stdout
    assert not marker.exists()

    wants = unit.parent / "default.target.wants" / unit.name     # "enable" edilmis gibi
    wants.parent.mkdir()
    wants.symlink_to(unit)
    out = _closed_stdin_run([sys.executable, str(_SCRIPT), "uninstall-service"], run_env)
    assert out.returncode == 0, out.stderr
    assert not unit.exists() and not os.path.lexists(wants)
    assert "systemctl --user" in out.stdout
    assert not marker.exists()


def test_run_forever_with_stdin_closed(sd, env, sandbox):
    driver = env["tmp"] / "surucu.py"
    driver.write_text(
        "import importlib.util, sys\n"
        "from pathlib import Path\n"
        "assert sys.stdin is None\n"
        "spec = importlib.util.spec_from_file_location('self_dev', sys.argv[1])\n"
        "sd = importlib.util.module_from_spec(spec)\n"
        "sys.modules['self_dev'] = sd\n"
        "spec.loader.exec_module(sd)\n"
        "def coder(description, workdir, timeout):\n"
        f"    (Path(workdir) / 'calc.py').write_text({MUL!r})\n"
        "    return 'ok'\n"
        "sd.make_subprocess_coder = lambda cfg: coder\n"
        "sd.ollama_reachable = lambda: True\n"
        "sd.running_jarvis_pids = lambda: []\n"
        "sys.exit(sd.main(sys.argv[2:]))\n")
    backlog = write_backlog(env, "- [ ] çarpma ekle")
    run_env = {**os.environ, "HOME": str(env["home"]), "JARVIS_SANDBOX": str(sandbox),
               "JARVIS_SELF_DIR": str(env["self"]), "JARVIS_REPO": str(env["repo"])}
    out = _closed_stdin_run([sys.executable, str(driver), str(_SCRIPT), "run", "--forever",
                             "--backlog", str(backlog), "--verify", VERIFY, "--min-free-gb", "0"],
                            run_env)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "Rapor:" in out.stdout
    (task,) = json.loads((sandbox / "state.json").read_text())["tasks"].values()
    assert task["mark"] == "[x]"
    assert (sandbox / "calc.py").read_text() == MUL and is_clean(sandbox)


# ── 13) altyapi: PYTHONPATH, coder HOME, init -e ──

def _capture_coder_env(sd, cfg, monkeypatch) -> dict:
    seen: dict = {}
    monkeypatch.setattr(sd, "_run_group",
                        lambda argv, cwd, env, timeout, stdin=None: (seen.update(env), (0, ""))[1])
    sd.make_subprocess_coder(cfg)("görev", cfg.sandbox, 10)
    return seen


def test_run_env_pythonpath_points_to_sandbox_src(sd, env, sandbox, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/ana/depo/src")          # ana depo kodu sizmasin
    cfg = make_cfg(sd, env)
    assert sd._run_env(cfg)["PYTHONPATH"] == str(sandbox / "src")
    # sandbox'a .pyc yazilmaz: ayni saniyede ayni boyutta yeniden yazilan dosya bayat pyc'den okunmasin
    assert sd._run_env(cfg)["PYTHONDONTWRITEBYTECODE"] == "1"
    assert _capture_coder_env(sd, cfg, monkeypatch)["PYTHONPATH"] == str(sandbox / "src")

    # src/ duzeni: paket kurulmadan dogrulamada import edilebilir (mevcut sandbox, yeniden init yok)
    (sandbox / "src" / "paket").mkdir(parents=True)
    (sandbox / "src" / "paket" / "__init__.py").write_text("X = 1\n")
    git(sandbox, "add", "-A")
    git(sandbox, "commit", "-q", "-m", "src")
    ok, out = sd.verify(make_cfg(sd, env, verify=f"{sys.executable} -c \"import paket\""), 60)
    assert ok, out


def test_coder_home_contains_sandbox_outside_real_home(sd, env, monkeypatch):
    data = env["tmp"] / "data" / "jarvis-self"
    sb = data / "sandbox"
    sd.init_sandbox(env["repo"], sb, make_venv=False)
    cfg = sd.Config(repo=env["repo"], sandbox=sb, self_dir=env["self"], verify=VERIFY)
    seen = _capture_coder_env(sd, cfg, monkeypatch)
    home = Path(seen["HOME"])
    assert home == data.resolve()
    assert home != env["home"].resolve() and home not in env["home"].resolve().parents
    assert all(seen[k].startswith(str(env["self"] / "home"))
               for k in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"))
    # AgenticCoder'in denetimi (degistirilmeden) bu ortamda gecer; HOME'a yazma gercek HOME'a gitmez
    before = sorted(p.name for p in env["home"].iterdir())
    probe = ("from pathlib import Path\n"
             f"p = Path({str(sb)!r}).resolve()\n"
             "h = Path.home().resolve()\n"
             "assert h in p.parents or p == h, (p, h)\n"
             "(Path.home() / 'yazildi.txt').write_text('x')\n")
    out = subprocess.run([sys.executable, "-c", probe], env=seen, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert (data / "yazildi.txt").exists()
    assert sorted(p.name for p in env["home"].iterdir()) == before


def test_init_tries_editable_install_and_only_warns(sd, env, monkeypatch):
    real_run = subprocess.run
    calls: list[list[str]] = []

    def fake_run(argv, *a, **kw):
        if argv[0] == "git":
            return real_run(argv, *a, **kw)
        calls.append([str(x) for x in argv])
        return subprocess.CompletedProcess(argv, 1 if "-e" in argv else 0, "", "")

    (env["repo"] / "pyproject.toml").write_text("[project]\nname = 'mini'\nversion = '0'\n")
    git(env["repo"], "add", "pyproject.toml")
    git(env["repo"], "commit", "-q", "-m", "pyproject")
    monkeypatch.setattr(sd, "_venv_python", lambda: sys.executable)
    monkeypatch.setattr(subprocess, "run", fake_run)
    logs: list[str] = []
    sd.init_sandbox(env["repo"], env["sandbox"], make_venv=True, log=logs.append)
    editable = [c for c in calls if "-e" in c]
    assert len(editable) == 1 and "install" in editable[0] and editable[0][-3:] == ["-e", ".", "--no-deps"]
    assert any("⚠️" in m and "-e" in m for m in logs), logs
    assert (env["sandbox"] / ".git").is_dir()                     # hata degil, sandbox duruyor


# ── 14) on kontrol + coder cokmesi ──

def test_red_baseline_processes_no_tasks(sd, env, sandbox):
    red = (f"{sys.executable} -c \"exit(print(chr(10).join('satir%d' % i for i in range(100))) or 1)\"")
    calls: list = []
    res = sd.run_loop(make_cfg(sd, env, verify=red), write_backlog(env, "- [ ] a", "- [ ] b"),
                      make_hooks(sd, lambda d, w, t: calls.append(d) or "x"), forever=True)
    assert calls == [] and res["results"] == []
    assert res["exit_code"] == 4
    assert res["stop_reason"] == "altyapı hatası: sandbox baştan kırmızı"
    detail = res["detail"].splitlines()
    assert len(detail) <= 40 and "satir99" in detail[-1] and "satir0" not in res["detail"]
    text = res["report"].read_text()
    assert "sandbox baştan kırmızı" in text and "satir99" in text
    assert not (sandbox / "state.json").exists() or json.loads(
        (sandbox / "state.json").read_text())["tasks"] == {}
    assert is_clean(sandbox)


def test_preflight_leaves_sandbox_clean(sd, env, sandbox):
    cfg = make_cfg(sd, env, verify=f"{sys.executable} -c \"open('iz','w')\"")
    seen: list[bool] = []
    res = sd.run_loop(cfg, write_backlog(env, "- [ ] a"),
                      make_hooks(sd, lambda d, w, t: seen.append((w / "iz").exists()) or "x"))
    assert res["exit_code"] == 0 and seen == [False]   # on kontrolun izi coder'a kalmadi


CRASH_LOG = ("[coder çıkış 1]\nTraceback (most recent call last):\n  File \"x\", line 1\n"
             "ValueError: GÜVENLİK: proje yolu $HOME dışında: /data/jarvis-self/sandbox\n")


def test_coder_crash_twice_stops_loop_without_state(sd, env, sandbox):
    calls: list = []

    def coder(description, workdir, timeout):
        calls.append(description)
        raise sd.CoderCrash(CRASH_LOG)

    res = sd.run_loop(make_cfg(sd, env), write_backlog(env, "- [ ] a", "- [ ] b"),
                      make_hooks(sd, coder), forever=True)
    assert len(calls) == 2
    assert res["exit_code"] == 5 and "altyapı" in res["stop_reason"]
    assert "GÜVENLİK" in res["detail"] and "GÜVENLİK" in res["report"].read_text()
    assert res["results"] == []
    state = json.loads((sandbox / "state.json").read_text())
    assert state["tasks"] == {}
    assert is_clean(sandbox)


def test_single_coder_crash_does_not_burn_attempt(sd, env, sandbox):
    calls: list = []

    def coder(description, workdir, timeout):
        calls.append(description)
        if len(calls) == 1:
            raise sd.CoderCrash(CRASH_LOG)
        (workdir / "calc.py").write_text(MUL)
        return "ok"

    res = sd.run_loop(make_cfg(sd, env, max_attempts=1), write_backlog(env, "- [ ] a"),
                      make_hooks(sd, coder))
    assert res["exit_code"] == 0
    (r,) = res["results"]
    assert r["status"] == "ok" and r["attempts"] == 1 and r["round"] == 1
    assert "altyapı" in res["report"].read_text()                   # uyari olarak raporda


def test_coder_crash_with_changes_is_normal_failure(sd, env, sandbox):
    def coder(description, workdir, timeout):
        (workdir / "calc.py").write_text(BROKEN)
        raise sd.CoderCrash(CRASH_LOG)

    res = sd.run_loop(make_cfg(sd, env, max_attempts=1), write_backlog(env, "- [ ] a"),
                      make_hooks(sd, coder))
    assert res["exit_code"] == 0
    assert res["results"][0]["status"] == "başarısız"
    assert is_clean(sandbox)


def test_subprocess_coder_raises_crash_only_on_traceback(sd, env, sandbox, monkeypatch):
    cfg = make_cfg(sd, env)
    outputs = iter([(1, "Traceback (most recent call last):\nValueError: x\n"),
                    (1, "model vazgeçti\n"), (0, "Traceback (most recent call last): log\n"),
                    (None, "[ZAMAN AŞIMI]")])
    monkeypatch.setattr(sd, "_run_group", lambda *a, **kw: next(outputs))
    coder = sd.make_subprocess_coder(cfg)
    with pytest.raises(sd.CoderCrash, match="ValueError"):
        coder("g", sandbox, 10)
    assert "vazgeçti" in coder("g", sandbox, 10)
    assert "log" in coder("g", sandbox, 10)
    assert "ZAMAN" in coder("g", sandbox, 10)


# ── 15) reset-state ──

def test_reset_state_removes_only_state_json(sd, env, sandbox, monkeypatch):
    sd.run_loop(make_cfg(sd, env), write_backlog(env, "- [ ] a"), make_hooks(sd, lambda d, w, t: "x"))
    assert (sandbox / "state.json").exists()
    head = git(sandbox, "rev-parse", "HEAD")
    reports = sorted((env["self"] / "reports").iterdir())
    monkeypatch.setenv("JARVIS_SANDBOX", str(sandbox))
    monkeypatch.setenv("JARVIS_SELF_DIR", str(env["self"]))
    assert sd.main(["reset-state"]) == 0
    assert not (sandbox / "state.json").exists()
    assert git(sandbox, "rev-parse", "HEAD") == head and is_clean(sandbox)
    assert sorted((env["self"] / "reports").iterdir()) == reports
    assert sd.main(["reset-state"]) == 0                           # yoksa da hata degil
