"""tests/test_step3_4_audit_and_risk.py

Adim 3.4 - audit duzeltmeleri ve classify_risk varsayilani.

A) Terminal onayina "hayir" denince audit.log'a hicbir satir yazilmiyordu.
   Artik action="denied" satiri yazilir (Brain'deki gibi: tool, fingerprint,
   maskeli params, approved=false, executed=false). Hem sesli arac yolu (E1,
   depo kaydi) hem metin yonlendiricisi (E4, _pending_terminal_command).

B) security_gate'in terminal satiri, komut CALISMAYIP yalnizca onizleme
   (ONAY GEREKLI) dondugunde de executed=true yaziyordu. Artik executed=true
   yalnizca komut gercekten calistiginda.

C) security_ai.classify_risk tanimadigi her (tool, action)'a "low" diyordu.
   Artik varsayilan "medium"; tehlikeli anahtar kelimeler (rm, delete,
   format, shutdown, sudo ...) "high"a yukseltir. Bilinen salt-okunur
   islemler acik bir LOW tablosundan gelir.

STUB YOK: gercek authorize/finish/audit, gercek terminal_tool (onizleme;
komut calistirilmaz), gercek depo. IZOLASYON: HOME/JARVIS_HOME tmp_path.
"""
import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from jarvis.main import JarvisLive
from jarvis.brains.security_ai import SecurityAI
from jarvis.core.call_fingerprint import fingerprint as call_fingerprint
from jarvis.core.approval_service import approval_service


class _UI:
    muted = False

    def __init__(self):
        self.logs = []

    def write_log(self, text):
        self.logs.append(str(text))

    def __getattr__(self, _name):
        return lambda *a, **k: None


@pytest.fixture
def jl(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh"))
    monkeypatch.setattr(approval_service, "_pending", {})
    obj = JarvisLive.__new__(JarvisLive)
    obj.ui = _UI()
    obj.session = None
    obj._loop = None
    obj._bg_tasks = None
    obj.spoken = []
    obj.speak = lambda text: obj.spoken.append(str(text))
    obj.speak_error = lambda *a, **k: None
    obj.home = home
    return obj


def _rows():
    from jarvis.paths import memory_dir
    p = memory_dir() / "audit.log"
    assert str(p).startswith(os.environ["JARVIS_HOME"])
    if not p.is_file():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _hash(fp):
    import hashlib
    return hashlib.sha256(fp.encode("utf-8")).hexdigest()[:16]


def _terminal(jl, cmd):
    args = {"command": cmd, "cwd": str(jl.home)}
    resp = asyncio.run(jl._execute_tool(SimpleNamespace(name="terminal", args=args, id="1")))
    return args, str(resp.response.get("result"))


# ── B) onizleme executed=false ──

def test_terminal_preview_is_audited_as_not_executed(jl):
    args, out = _terminal(jl, "touch yeni.txt")
    assert out.startswith("ONAY GEREKL"), out
    assert not (jl.home / "yeni.txt").exists()
    rows = [r for r in _rows() if r.get("tool") == "terminal"]
    assert rows and rows[-1]["executed"] is False and rows[-1]["approved"] is False
    assert "Onay kodu" not in out and "confirm_code" not in out


def test_terminal_readonly_command_is_audited_as_executed(jl):
    _, out = _terminal(jl, "pwd")
    assert str(jl.home) in out
    rows = [r for r in _rows() if r.get("tool") == "terminal"]
    assert rows and rows[-1]["executed"] is True


# ── A) "hayir" -> denied ──

def test_voice_terminal_no_writes_denied_row(jl):
    args, _ = _terminal(jl, "touch yeni.txt")
    jl._on_text_command("hayır")
    denied = [r for r in _rows() if r.get("action") == "denied"]
    assert len(denied) == 1, _rows()
    row = denied[0]
    assert row["tool"] == "terminal"
    assert row["approved"] is False and row["executed"] is False
    assert row["fingerprint"] == _hash(call_fingerprint("terminal", args))
    assert row["params"]["command"] == "touch yeni.txt"
    assert "confirm_code" not in row["params"]
    assert jl._approvals.records() == []


def test_router_terminal_no_writes_denied_row(jl):
    params = {"command": "touch yeni.txt", "cwd": str(jl.home)}
    jl._pending_terminal_command = {**params, "confirm_code": "gizli-kod-123"}
    jl._on_text_command("hayır")
    denied = [r for r in _rows() if r.get("action") == "denied"]
    assert len(denied) == 1, _rows()
    row = denied[0]
    assert row["tool"] == "terminal" and row["source"] == "router"
    assert row["approved"] is False and row["executed"] is False
    assert row["fingerprint"] == _hash(call_fingerprint("terminal", params))
    assert "gizli-kod-123" not in json.dumps(_rows(), ensure_ascii=False)


def test_unrelated_text_does_not_write_denied_row(jl):
    _terminal(jl, "touch yeni.txt")
    jl._on_text_command("saat kaç")
    assert [r for r in _rows() if r.get("action") == "denied"] == []


# ── C) classify_risk ──

@pytest.mark.parametrize("tool,action", [
    ("bilinmeyen_arac", None), ("bilinmeyen_arac", "calistir"),
    ("executor_x", "information"),          # "format" alt-dizesi kelime degil
    ("backup_create", None),                # tablodaki medium korunur
])
def test_unknown_defaults_to_medium(tool, action):
    assert SecurityAI.classify_risk(tool, action) == "medium"


@pytest.mark.parametrize("tool,action", [
    ("bilinmeyen_arac", "rm"), ("shell", "sudo"), ("x", "delete_everything"),
    ("x", "format_disk"), ("x", "shutdown_now"), ("sudo", None), ("x", "rm -rf /"),
    ("x", "kill_process"), ("x", "wipe"),
])
def test_dangerous_keywords_escalate_to_high(tool, action):
    assert SecurityAI.classify_risk(tool, action) == "high"


@pytest.mark.parametrize("tool,action", [
    ("research_ai", "search"), ("github_search", None), ("windows_system", "process_list"),
    ("windows_system", "disk_info"), ("windows_system", "network_connections"),
    ("file_controller", "list"), ("coder_ai", "analyze"),
])
def test_known_readonly_stays_low(tool, action):
    assert SecurityAI.classify_risk(tool, action) == "low"
