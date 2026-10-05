"""tests/test_registry_react_gate.py

docs/GUVENLIK_KAPISI_PLAN.md Adim 2 (ikinci yari): registry (E2,
main.JarvisLive._execute_registry_tool) ve ReAct (E3,
tools/agent/react_runtime) yollari security_gate.authorize/prepare/finish
uzerinden gecer.

* Registry araclari icin spec, registry kaydinin kendi guvenlik seviyesinden
  uretilir (plan §4.3); kayitsiz arac DENY.
* E2: modelin confirm_code'u araca gitmez; onay yalnizca kullanicinin gercek
  turundan (arguman-bagli, 60 sn, tek kullanim) gelir.
* E3: ReAct ic dongusu dis cagrinin onayini MIRAS ALAMAZ (eskiden
  ctx.dangerous_confirmed ic cagrilara aynen geciyordu); onay gerektiren ic
  cagri calismaz, kayitsiz arac reddedilir.
* ALLOW ve onayli her yurutme de audit'e yazilir (tool_gate.audit_entry,
  ~/.jarvis/audit.log - conftest HOME'u tmp'ye yonlendirir). E1 dahil.

Karar kodu stub'lanmaz. Araclar registry'ye kaydedilen, cagri kaydeden test
araclaridir. ReAct'in LLM'i sabit JSON donen bir model_fn'dir; memory.recall
(yerel Ollama) bos bir modulle devre disidir. Gercek ag/LLM yok.
"""
import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import jarvis.main as main_mod
from jarvis.main import JarvisLive
from jarvis import security_gate as sg
from tools.registry import registry, ToolContext

_calls: dict[str, list] = {"destructive": [], "normal": [], "dangerous": []}


@registry.register(name="_gate_destructive", description="t", parameters={},
                   security="destructive", category="test")
def _destructive(args, ctx):
    _calls["destructive"].append(dict(args))
    return "RAN-D"


@registry.register(name="_gate_dangerous", description="t", parameters={},
                   security="dangerous", category="test")
def _dangerous(args, ctx):
    _calls["dangerous"].append(dict(args))
    return "RAN-X"


@registry.register(name="_gate_normal", description="t", parameters={},
                   security="normal", category="test")
def _normal(args, ctx):
    _calls["normal"].append(dict(args))
    return "RAN-N"


class _UI:
    muted = False

    def __getattr__(self, _name):
        return lambda *a, **k: None


@pytest.fixture
def jl():
    for v in _calls.values():
        v.clear()
    obj = JarvisLive.__new__(JarvisLive)
    obj.ui = _UI()
    obj.session = None
    obj._loop = None
    obj._bg_tasks = None
    obj.speak = lambda *a, **k: None
    obj.speak_error = lambda *a, **k: None
    obj._set_pending_dangerous(None)
    obj._tool_confirm_code = None
    yield obj
    obj._set_pending_dangerous(None)


def _audit() -> list[dict]:
    p = Path.home() / ".jarvis" / "audit.log"
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_e2(obj, name, args):
    return asyncio.run(obj._execute_registry_tool(name, args))


# ── Spec: registry seviyesinden, kayitsiz -> DENY ──

def test_registry_tools_get_spec_from_their_security_level():
    assert sg.spec_for("_gate_normal") is not None
    S, V = sg.Source, sg.Verdict
    assert sg.authorize("_gate_normal", {}, S.MODEL_LIVE).verdict is V.ALLOW
    assert sg.authorize("_gate_destructive", {}, S.MODEL_LIVE).verdict is V.NEEDS_APPROVAL
    assert sg.authorize("_gate_dangerous", {}, S.MODEL_LIVE).verdict is V.NEEDS_APPROVAL
    assert sg.authorize("agentic_code", {"description": "x"}, S.MODEL_LIVE).verdict is V.NEEDS_APPROVAL
    assert sg.authorize("spotify_control", {"action": "play"}, S.REACT).verdict is V.ALLOW
    assert sg.authorize("_gate_hic_kayitli_degil", {}, S.REACT).verdict is V.DENY


# ── E2: registry yolu ──

def test_e2_unregistered_tool_is_blocked(jl):
    r = run_e2(jl, "_gate_hic_kayitli_degil", {})
    assert r.startswith("BLOCKED"), r


def test_e2_model_confirm_code_never_reaches_the_tool(jl):
    run_e2(jl, "_gate_normal", {"q": "a", "confirm_code": "X"})
    assert _calls["normal"] == [{"q": "a"}]


def test_e2_approval_text_comes_from_gate_and_has_no_code(jl):
    r = run_e2(jl, "_gate_destructive", {"path": "proje"})
    assert r.startswith("CONFIRMATION_REQUIRED:_gate_destructive:")
    assert "path=proje" in r                      # kapinin mesaji: arguman gosterilir
    assert "confirm_code" not in r and "ONAY_KODU" not in r
    assert _calls["destructive"] == []


def test_e2_real_user_turn_runs_once_bound_to_args(jl):
    run_e2(jl, "_gate_destructive", {"path": "proje"})
    run_e2(jl, "_gate_destructive", {"path": "proje", "confirm_code": "X"})   # model kodu onay degil
    assert _calls["destructive"] == []
    jl._grant_dangerous_confirmation()                                    # kullanici "evet"
    assert run_e2(jl, "_gate_destructive", {"path": "baska"}).startswith("CONFIRMATION_REQUIRED")
    assert _calls["destructive"] == []
    run_e2(jl, "_gate_destructive", {"path": "baska"})
    jl._grant_dangerous_confirmation()
    assert run_e2(jl, "_gate_destructive", {"path": "baska"}).startswith("RAN-D")
    assert run_e2(jl, "_gate_destructive", {"path": "baska"}).startswith("CONFIRMATION_REQUIRED")
    assert _calls["destructive"] == [{"path": "baska"}]


def test_e2_approval_expires_after_60s(jl, monkeypatch):
    run_e2(jl, "_gate_destructive", {"path": "p"})
    jl._grant_dangerous_confirmation()
    real = main_mod.time.monotonic
    monkeypatch.setattr(main_mod.time, "monotonic", lambda: real() + jl._CONFIRMATION_TTL_S + 1)
    assert run_e2(jl, "_gate_destructive", {"path": "p"}).startswith("CONFIRMATION_REQUIRED")
    assert _calls["destructive"] == []


def test_e2_allowed_and_approved_runs_are_audited(jl):
    run_e2(jl, "_gate_normal", {"q": "a"})
    run_e2(jl, "_gate_destructive", {"path": "p"})
    jl._grant_dangerous_confirmation()
    run_e2(jl, "_gate_destructive", {"path": "p"})
    rows = [r for r in _audit() if r["tool"].startswith("_gate_")]
    executed = [(r["tool"], r["approved"]) for r in rows if r["approved"]]
    assert ("_gate_normal", True) in executed
    assert ("_gate_destructive", True) in executed
    assert any(r["tool"] == "_gate_destructive" and not r["approved"] for r in rows)   # bekleyen istek


def test_e1_allowed_runs_are_audited_too(jl, monkeypatch):
    rec = []
    monkeypatch.setattr(main_mod, "weather_action", lambda **k: rec.append(k) or "güneşli")
    asyncio.run(jl._execute_tool(SimpleNamespace(name="weather_report", args={"city": "Ankara"}, id="1")))
    assert rec
    assert any(r["tool"] == "weather_report" and r["approved"] for r in _audit())


# ── E3: ReAct ──

@pytest.fixture
def no_memory(monkeypatch):
    # memory.recall yerel Ollama'ya baglanir; bos modul -> solve() atlar.
    monkeypatch.setitem(sys.modules, "memory", types.ModuleType("memory"))
    from tools.agent import react_runtime
    monkeypatch.setattr(react_runtime, "_react_cache", {})
    yield react_runtime


def _script(*steps):
    seq = list(steps) + [{"thought": "bitti", "tool": "finish_task", "args": {}, "response": "ok"}]
    it = iter(seq)
    return lambda prompt: json.dumps(next(it))


def _react(rt, ctx, *steps):
    agent = rt.ReactAgent(model_fn=_script(*steps), ctx=ctx, max_turns=len(steps) + 1)
    return asyncio.run(agent.solve("test hedefi"))


def test_e3_inner_call_does_not_inherit_outer_confirmation(no_memory):
    for v in _calls.values():
        v.clear()
    outer = ToolContext(dangerous_confirmed=True)        # dis react_agent cagrisi onayliymis gibi
    _react(no_memory, outer, {"thought": "t", "tool": "_gate_destructive", "args": {"path": "x"}})
    assert _calls["destructive"] == []


def test_e3_unregistered_tool_is_blocked(no_memory):
    out = _react(no_memory, ToolContext(),
                 {"thought": "t", "tool": "_gate_hic_kayitli_degil", "args": {}})
    assert "BLOCKED" in out, out


def test_e3_allowed_tool_runs_without_model_code_and_is_audited(no_memory):
    for v in _calls.values():
        v.clear()
    _react(no_memory, ToolContext(),
           {"thought": "t", "tool": "_gate_normal", "args": {"q": "b", "confirm_code": "X"}})
    assert _calls["normal"] == [{"q": "b"}]
    assert any(r["tool"] == "_gate_normal" and r["approved"] for r in _audit())


def test_e3_approval_needed_tool_does_not_run_and_shows_no_code(no_memory):
    for v in _calls.values():
        v.clear()
    out = _react(no_memory, ToolContext(),
                 {"thought": "t", "tool": "_gate_dangerous", "args": {"cmd": "x"}})
    assert _calls["dangerous"] == []
    assert "confirm_code" not in out and "ONAY_KODU" not in out
