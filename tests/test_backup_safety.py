"""tests/test_backup_safety.py

Yedek / geri alma guvenligi (backup_tool.py, executor_ai, Brain Team):

1) rollback atomik: yedek once gecici klasore hazirlanir, mevcut proje yan
   klasore tasinir, yenisi yerine konur; herhangi bir adim basarisiz olursa
   eskisi geri konur ve False doner. Basarisizlik "tamamlandi" demez,
   hatalar yutulmaz.
2) rollback(backup_path) yalnizca backups_root altindaki, dizin olan ve bos
   olmayan bir yedegi kabul eder; aksi halde HICBIR SEYE dokunmadan reddeder.
3) Yedek kok dizini tek yerden (backup_tool.jarvis_project_root) gelir.
4) Brain Team modify adimi HEDEF dosyanin da yedegini alir; otomatik geri
   alma yalnizca o dosyayi geri yukler, Jarvis'in kendi klasorunu toptan
   geri almaz (eskiden executor backup_rollback = src/jarvis'in tamamini
   silip yeniden yaziyordu).
5) cleanup_old_backups(keep<0) ValueError.
6) Kullanilmayan restart_jarvis kaldirildi.

IZOLASYON: proje, yedekler ve hedef dosya tmp_path altindadir;
backup_tool.jarvis_project_root tmp projeye cevrilir. Brain Team testinde
LLM ajanlari (coder_ai, auditor_ai) mesaj yolunda sahte yanit doner ve
yedek eylemleri GERCEK executor'a hic gitmez (yalnizca kaydedilir) - eski kod
gercek src/jarvis'i geri almaya kalksa bile gercek projeye dokunulmaz.
Gercek proje klasorunun degismedigi oturum sonunda kontrol edilir.
"""
import ast
import hashlib
import os
import sys
import stat
from pathlib import Path

import pytest

import jarvis.backup_tool as bt
from jarvis.backup_tool import JarvisBackupTool

REPO = Path(__file__).resolve().parents[1]
REAL_JARVIS = REPO / "src" / "jarvis"
REAL_BACKUPS = REPO / "src" / "jarvis_yedekler"


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    if not root.exists():
        return "yok"
    for p in sorted(root.rglob("*")):
        if "__pycache__" in p.parts or p.suffix == ".pyc":
            continue
        rel = str(p.relative_to(root))
        h.update(rel.encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture(scope="module", autouse=True)
def real_project_untouched():
    before = (_tree_digest(REAL_JARVIS),
              sorted(p.name for p in REAL_BACKUPS.iterdir()) if REAL_BACKUPS.exists() else None)
    yield
    after = (_tree_digest(REAL_JARVIS),
             sorted(p.name for p in REAL_BACKUPS.iterdir()) if REAL_BACKUPS.exists() else None)
    assert after == before, "Testler GERCEK src/jarvis ya da src/jarvis_yedekler klasorune dokundu!"


def _make_project(root: Path, content: str) -> Path:
    proj = root / "jarvis"
    (proj / "sub").mkdir(parents=True)
    (proj / "a.txt").write_text(content)
    (proj / "sub" / "b.txt").write_text(content + "-b")
    return proj


@pytest.fixture
def proj(tmp_path):
    parent = tmp_path / "src"
    p = _make_project(parent, "ESKI")
    tool = JarvisBackupTool(p)
    backup = tool.create_backup()
    (p / "a.txt").write_text("YENI")
    (p / "yeni_dosya.txt").write_text("sonradan")
    yield tool, p, backup
    for d in tmp_path.rglob("*"):          # chmod'lu klasorleri temizlenebilir yap
        try:
            d.chmod(d.stat().st_mode | stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
        except OSError:
            pass


# ── (1) atomik rollback ──

_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0   # Windows'ta geteuid yok
_NO_POSIX_PERMS = sys.platform == "win32"
_POSIX_PERMS_REASON = ("Windows'ta chmod yalnizca salt-okunur bayragini degistirir; "
                       "klasor yazma/okuma izni kaldirilamadigi icin test ettigi hata olusturulamaz")


def test_rollback_restores_backup_and_keeps_previous_state(proj):
    tool, p, backup = proj
    assert tool.rollback(backup) is True
    assert (p / "a.txt").read_text() == "ESKI"
    assert not (p / "yeni_dosya.txt").exists()
    kept = [b for b in tool.list_backups() if (b / "yeni_dosya.txt").exists()]
    assert kept, "geri alma oncesi durum korunmadi"


@pytest.mark.skipif(_IS_ROOT, reason="root izinleri yok sayar")
@pytest.mark.skipif(_NO_POSIX_PERMS, reason=_POSIX_PERMS_REASON)
def test_failure_while_preparing_leaves_project_intact_and_returns_false(proj, capsys):
    tool, p, backup = proj
    secret = backup / "sub" / "b.txt"
    secret.chmod(0)                          # yedek okunamaz -> hazirlik basarisiz
    before = _tree_digest(p)
    assert tool.rollback(backup) is False
    assert _tree_digest(p) == before
    assert "tamamlandı" not in capsys.readouterr().out
    assert not [d for d in tool.backups_root.iterdir() if d.name.startswith(".")], "gecici klasor kaldi"


@pytest.mark.skipif(_IS_ROOT, reason="root izinleri yok sayar")
@pytest.mark.skipif(_NO_POSIX_PERMS, reason=_POSIX_PERMS_REASON)
def test_failure_while_swapping_puts_old_project_back(proj, capsys):
    tool, p, backup = proj
    before = _tree_digest(p)
    p.parent.chmod(0o555)                    # proje yan klasore tasinamaz
    try:
        assert tool.rollback(backup) is False
    finally:
        p.parent.chmod(0o755)
    assert _tree_digest(p) == before
    assert "tamamlandı" not in capsys.readouterr().out


# ── (2) yedek yolu dogrulamasi ──

def test_rollback_rejects_paths_outside_backups_root_before_touching_anything(proj, tmp_path):
    tool, p, _backup = proj
    outside = tmp_path / "baska"
    outside.mkdir()
    (outside / "x.txt").write_text("yabanci")
    empty = tool.backups_root / "20990101-000000"
    empty.mkdir()
    a_file = tool.backups_root / "20990101-000001"
    a_file.write_text("dosya")
    link = tool.backups_root / "20990101-000002"
    link.symlink_to(outside, target_is_directory=True)
    before = _tree_digest(p)
    for bad in (outside, tool.backups_root, empty, a_file, link, p, tmp_path / "yok"):
        assert tool.rollback(bad) is False, bad
        assert _tree_digest(p) == before, bad


# ── (3) tek kok dizini ──

def test_backup_root_comes_from_one_place(tmp_path, monkeypatch):
    fake = _make_project(tmp_path / "src", "X")
    monkeypatch.setattr(bt, "jarvis_project_root", lambda: fake)
    tool = JarvisBackupTool.for_jarvis()
    assert tool.project_path == fake.resolve()
    assert tool.backups_root == fake.parent.resolve() / "jarvis_yedekler"
    from jarvis.brains import executor_ai
    out = executor_ai._exec_backup_create({})
    assert str(tool.backups_root) in out
    assert len(tool.list_backups()) == 1


def test_no_module_builds_its_own_backup_root():
    users = {}
    for rel in ("src/jarvis/brains/executor_ai.py", "src/jarvis/actions/self_improve.py",
                "src/jarvis/actions/entegrasyon.py", "src/jarvis/core/brain_orchestrator.py"):
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        direct = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Name) and n.func.id == "JarvisBackupTool"]
        users[rel] = len(direct)
    assert all(v == 0 for v in users.values()), users


def test_scope_is_documented():
    doc = (JarvisBackupTool.__doc__ or "") + (JarvisBackupTool.for_jarvis.__doc__ or "")
    assert "src/jarvis" in doc and "kapsam" in doc.lower()


def test_executor_rollback_failure_is_an_error_not_completed(tmp_path, monkeypatch):
    fake = _make_project(tmp_path / "src", "X")
    monkeypatch.setattr(bt, "jarvis_project_root", lambda: fake)
    from jarvis.brains import executor_ai
    from jarvis.brains.base_brain import BrainError
    with pytest.raises(BrainError) as exc:
        executor_ai._exec_backup_rollback({})       # hic yedek yok
    assert "tamamlandı" not in str(exc.value)


# ── (5) / (6) ──

def test_cleanup_with_negative_keep_raises(proj):
    tool, _p, _b = proj
    with pytest.raises(ValueError):
        tool.cleanup_old_backups(-1)
    assert len(tool.list_backups()) == 1


def test_restart_jarvis_is_gone():
    assert not hasattr(JarvisBackupTool, "restart_jarvis")


# ── (4) Brain Team: hedef dosya yedegi, yalnizca o dosya geri yuklenir ──

class _FakeBus:
    """LLM ajanlarinin yerine gecer; yedek eylemlerini GERCEK executor'a
    iletmez, yalnizca kaydeder."""

    def __init__(self, target: Path):
        self.target = target
        self.sent: list[tuple[str, str]] = []

    def send(self, frm, to, task, payload=None, **kw):
        payload = payload or {}
        self.sent.append((to, payload.get("action", "")))
        if to == "coder_ai":
            if not payload.get("dry_run"):
                Path(payload["file_path"]).write_text("BOZUK")
            return {"status": "completed", "result": {"summary": "degistirildi"}}
        if to == "auditor_ai":
            return {"status": "completed", "result": {"passed": False, "reason": "test: bozuk"}}
        return {"status": "completed", "result": "kaydedildi"}


@pytest.fixture
def brain(tmp_path, monkeypatch):
    fake_jarvis = _make_project(tmp_path / "src", "JARVIS-KODU")
    # raising=False: eski kodda bu fonksiyon yok; sahte bus yedek eylemlerini
    # zaten gercek executor'a iletmedigi icin eski kod da guvenle calisir.
    monkeypatch.setattr(bt, "jarvis_project_root", lambda: fake_jarvis, raising=False)
    target = tmp_path / "home" / "Documents" / "rapor.py"
    target.parent.mkdir(parents=True)
    target.write_text("ORIJINAL")
    import jarvis.core.brain_orchestrator as bo
    from jarvis.core.task_manager import TaskManager
    orch = bo.BrainOrchestrator()
    orch.tasks = TaskManager(path=tmp_path / "brain_tasks.json")
    orch._last_player = None
    orch.bus = _FakeBus(target)
    step = {"order": 1, "description": "rapor.py'yi duzenle", "agent": "coder_ai",
            "operation": "modify", "file_path": str(target)}
    task = orch.tasks.create(name="duzenle", agent="planner_ai", priority="medium",
                             payload={"goal": "duzenle", "plan": [step], "step_index": 0,
                                      "history": [], "audit_retries": 0})
    return orch, task, step, target, fake_jarvis, bo


def test_modify_backs_up_target_and_auto_rollback_restores_only_it(brain):
    orch, task, step, target, fake_jarvis, bo = brain
    jarvis_before = _tree_digest(fake_jarvis)
    for _ in range(bo.MAX_AUDIT_ROUNDS + 1):
        result = orch._execute_step(task, step)
        assert target.read_text() == "BOZUK"
        orch._finish_step(task, step, result)
    assert target.read_text() == "ORIJINAL"
    assert ("executor_ai", "backup_rollback") not in orch.bus.sent
    assert _tree_digest(fake_jarvis) == jarvis_before


@pytest.mark.skipif(_IS_ROOT, reason="root izinleri yok sayar")
@pytest.mark.skipif(_NO_POSIX_PERMS, reason=_POSIX_PERMS_REASON)
def test_modify_does_not_run_when_target_backup_fails(brain):
    orch, task, step, target, fake_jarvis, bo = brain
    fake_jarvis.parent.chmod(0o555)          # yedek klasoru olusturulamaz
    try:
        with pytest.raises(Exception):
            orch._execute_step(task, step)
    finally:
        fake_jarvis.parent.chmod(0o755)
    assert target.read_text() == "ORIJINAL"
    assert not any(to == "coder_ai" for to, _ in orch.bus.sent)
