"""tests/test_security_gate.py

docs/GUVENLIK_KAPISI_PLAN.md Adim 0 ve Adim 1 - DAVRANIS DEGISMEZ.

Adim 0: src/jarvis/security_gate.py iskeleti (ResolvedCall, Decision,
ApprovalStore) ve bugunku tek onay yuvasini (main.JarvisLive.
_set_pending_dangerous / _grant_dangerous_confirmation /
_consume_dangerous_confirmation) saran adaptor. Adaptor uzerinden verilen
onay, main.py'nin kendi yoluyla birebir ayni sonucu verir; parmak izi tek
kaynaktan (security_gate.fingerprint) gelir.

Adim 1: herhangi bir giris yolundan erisilebilen HER arac adinin bir
ToolSpec'i vardir. Erisim kumesi mevcut tablolardan toplanir (Gemini
TOOL_DECLARATIONS + _execute_tool dallari, tools/ registry kayitlari,
tools_kopru.ALLOWED_TOOLS, executor_ai._ALLOWED_ACTIONS, Brain ajanlari,
CLI core/agent.TOOLS, eklentiler, deterministik yonlendiriciler). Yeni bir
arac spec'siz eklenirse tutarlilik testi kirmizi olur. Eyleme bagli etki,
mevcut siniflandiricilardan (file_controller.READONLY_ACTIONS,
terminal_tool._is_readonly, computer_settings._DANGEROUS_ACTIONS) uretilir.

Hicbir karar fonksiyonu stub'lanmaz. HOME/JARVIS_HOME tmp_path; toplayici
kaynak dosyalari yalnizca OKUR (AST), modulleri calistirmaz/eklenti
yuklemez.
"""
import hashlib
import os
from pathlib import Path

import pytest


def _real_home() -> Path:
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:
        return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


_REAL_DATA = _real_home() / ".local" / "share" / "MuratJARVIS"
_GUARDED = [_REAL_DATA / "memory" / n for n in ("audit.log", "conversation_log.jsonl", "agent_tasks.json")]
_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "src" / "jarvis" / "plugins"


def _snapshot():
    snap = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
            for p in _GUARDED}
    snap["plugins_dir_exists"] = _PLUGIN_DIR.exists()
    return snap


@pytest.fixture(scope="module", autouse=True)
def real_data_untouched():
    before = _snapshot()
    yield
    assert _snapshot() == before, "Testler gercek veriye / proje dizinine dokundu!"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "jh"))
    (tmp_path / "home" / "Desktop").mkdir(parents=True)
    from jarvis.paths import memory_dir
    assert memory_dir().resolve().is_relative_to(tmp_path.resolve())
    yield


# ── Adim 0: iskelet ──

def test_skeleton_types_exist():
    from jarvis import security_gate as sg
    assert {s.name for s in sg.Source} >= {
        "MODEL_LIVE", "REACT", "ROUTER", "AGENT_LOOP", "BRAIN_TEAM",
        "SCHEDULER", "PROACTIVE", "CLI_AGENT", "PLUGIN"}
    assert [e.name for e in sorted(sg.Effect)] == ["READ", "MUTATE", "EXTERNAL", "EXECUTE", "SYSTEM"]
    assert {v.name for v in sg.Verdict} == {"ALLOW", "NEEDS_APPROVAL", "DENY"}
    call = sg.resolve("send_message", {"receiver": "Ali"}, sg.Source.MODEL_LIVE)
    assert isinstance(call, sg.ResolvedCall)
    with pytest.raises(Exception):
        call.tool = "baska"            # frozen
    d = sg.Decision(verdict=sg.Verdict.DENY, call=call, reason="r", model_message="m")
    assert d.request_id is None and d.user_prompt is None


@pytest.mark.parametrize("args", [
    {}, {"a": 1, "b": "x"}, {"n": 3.5, "nested": {"k": [1, 2]}}, {"ü": "çğş"},
])
def test_fingerprint_is_the_same_as_main(args):
    from jarvis import security_gate as sg
    from jarvis.main import JarvisLive
    assert sg.fingerprint("send_message", args) == JarvisLive._action_fingerprint("send_message", args)
    # 3 ve "3" ayni islem (registry.execute ile ayni kural)
    assert sg.fingerprint("t", {"x": 3}) == sg.fingerprint("t", {"x": "3"})


def test_resolve_uses_one_fingerprint_for_tool_and_args():
    from jarvis import security_gate as sg
    a = sg.resolve("file_controller", {"action": "delete", "path": "desktop", "name": "x"}, sg.Source.MODEL_LIVE)
    b = sg.resolve("file_controller", {"action": "delete", "path": "desktop", "name": "y"}, sg.Source.MODEL_LIVE)
    assert a.fingerprint != b.fingerprint
    assert a.effect is sg.Effect.MUTATE and a.action == "delete"


@pytest.fixture
def jl():
    from jarvis.main import JarvisLive
    obj = JarvisLive.__new__(JarvisLive)
    obj._set_pending_dangerous(None)
    yield obj
    obj._set_pending_dangerous(None)


def test_adapter_wraps_todays_single_slot(jl):
    from jarvis import security_gate as sg
    store = sg.PendingSlotAdapter(jl)
    assert isinstance(store, sg.ApprovalStore)
    args = {"receiver": "Ali", "message_text": "selam"}
    call = sg.resolve("send_message", args, sg.Source.MODEL_LIVE)

    rid = store.request(call)
    assert rid == call.fingerprint
    assert jl._pending_dangerous_action == "send_message"
    assert jl._pending_dangerous_fingerprint == call.fingerprint
    assert store.pending() == ("send_message", call.fingerprint)

    assert store.consume(call) is False            # kullanici turu yok
    store.grant_from_user_turn()
    other = sg.resolve("send_message", dict(args, receiver="Başkası"), sg.Source.MODEL_LIVE)
    assert store.consume(other) is False           # farkli arguman
    assert store.consume(call) is True
    assert store.consume(call) is False            # tek kullanimlik
    assert store.pending() is None


def test_adapter_and_main_paths_are_interchangeable(jl):
    from jarvis import security_gate as sg
    store = sg.PendingSlotAdapter(jl)
    args = {"app_name": "terminal"}
    store.request(sg.resolve("open_app", args, sg.Source.MODEL_LIVE))
    jl._grant_dangerous_confirmation()               # main'in yolu
    assert jl._consume_dangerous_confirmation("open_app", args) is True

    jl._set_pending_dangerous("open_app", jl._action_fingerprint("open_app", args))
    store.grant_from_user_turn()                     # adaptorun yolu
    assert store.consume(sg.resolve("open_app", args, sg.Source.MODEL_LIVE)) is True


def test_adapter_cancel_and_ttl(jl, monkeypatch):
    from jarvis import security_gate as sg
    store = sg.PendingSlotAdapter(jl)
    call = sg.resolve("send_message", {"receiver": "Ali"}, sg.Source.MODEL_LIVE)
    store.request(call)
    store.cancel()
    store.grant_from_user_turn()                     # bekleyen yok: onay verilmez
    assert store.consume(call) is False

    store.request(call)
    store.grant_from_user_turn()
    import jarvis.main as main_mod
    real = main_mod.time.monotonic
    monkeypatch.setattr(main_mod.time, "monotonic", lambda: real() + jl._CONFIRMATION_TTL_S + 1)
    assert store.consume(call) is False


def test_pseudo_actions_brain_and_agent_loop_round_trip(jl):
    # Adim 3.4: agent_loop istegi depoda GERCEK cagri olarak (arac + argumanlar,
    # kaynak AGENT_LOOP) durur; ayni gorevin ayni adimi yeniden duyurulursa
    # "zaten soruluyor" sayilir ve ikinci kayit acilmaz.
    from jarvis import security_gate as sg
    jl.speak = lambda *a, **k: None          # duyuru canli oturuma konusur
    pending = {"tool": "send_message", "parameters": {"x": "1"}}
    assert jl.request_agent_loop_approval("abcd1234", pending, "m") is True
    assert jl.request_agent_loop_approval("abcd1234", pending, "m") is True   # ayni adim zaten soruluyor
    recs = jl._approvals.records(sg.KIND_AGENT_LOOP)
    assert len(recs) == 1 and recs[0].call.tool == "send_message" and recs[0].task_id == "abcd1234"
    # Eski bicimli sozde kayit (for_pending) gercek cagri yerine gecmez.
    jl._approvals.cancel()
    sg.PendingSlotAdapter(jl).request(sg.ResolvedCall.for_pending(
        "agent_loop", jl._agent_loop_fingerprint_args("abcd1234", pending)))
    assert jl.request_agent_loop_approval("abcd1234", pending, "m") is False


# ── Adim 1: ToolSpec + tutarlilik ──

def test_every_reachable_tool_has_a_spec():
    from jarvis import security_gate as sg
    reachable = sg.collect_reachable_tools()
    missing = sg.missing_specs(reachable)
    assert missing == {}, f"Spec'i olmayan erisilebilir araclar: {missing}"


def test_collector_sees_every_entry_path():
    from jarvis import security_gate as sg
    r = sg.collect_reachable_tools()
    S = sg.Source
    expect = {
        "send_message": S.MODEL_LIVE, "save_memory": S.MODEL_LIVE, "code_search": S.MODEL_LIVE,
        "agentic_code": S.MODEL_LIVE, "react_agent": S.MODEL_LIVE, "spotify_control": S.REACT,
        "entegrasyon_uygula": S.AGENT_LOOP, "github_arac_bul_ve_degerlendir": S.AGENT_LOOP,
        "backup_rollback": S.BRAIN_TEAM, "vault_decrypt": S.BRAIN_TEAM,
        "coder_ai": S.BRAIN_TEAM, "research_ai": S.BRAIN_TEAM,
        "run_python": S.CLI_AGENT, "file_delete": S.CLI_AGENT,
        "terminal": S.ROUTER, "brain_team": S.ROUTER, "windows_system": S.ROUTER,
    }
    for name, source in expect.items():
        assert source in r.get(name, set()), (name, source, r.get(name))


def test_new_agent_loop_tool_without_spec_is_red(monkeypatch):
    from jarvis import security_gate as sg
    from jarvis.actions import tools_kopru
    monkeypatch.setitem(tools_kopru.ALLOWED_TOOLS, "yeni_arac", lambda p: "x")
    missing = sg.missing_specs(sg.collect_reachable_tools())
    assert missing == {"yeni_arac": {sg.Source.AGENT_LOOP}}


def test_new_gemini_declaration_without_spec_is_red(monkeypatch, tmp_path):
    from jarvis import security_gate as sg
    src = Path(sg.MAIN_PY).read_text(encoding="utf-8")
    patched = src.replace('TOOL_DECLARATIONS = [', 'TOOL_DECLARATIONS = [\n    {"name": "gizli_arac", "parameters": {}},', 1)
    fake = tmp_path / "main.py"
    fake.write_text(patched, encoding="utf-8")
    monkeypatch.setattr(sg, "MAIN_PY", fake)
    assert sg.missing_specs(sg.collect_reachable_tools()) == {"gizli_arac": {sg.Source.MODEL_LIVE}}


def test_new_plugin_tool_without_spec_is_red(monkeypatch, tmp_path):
    from jarvis import security_gate as sg
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "p.py").write_text(
        'def register():\n    return [{"name": "plugin_sil", "handler": None}]\n', encoding="utf-8")
    monkeypatch.setattr(sg, "PLUGIN_DIR", plugins)
    assert sg.missing_specs(sg.collect_reachable_tools()) == {"plugin_sil": {sg.Source.PLUGIN}}


def test_discovered_tools_get_a_fail_closed_spec():
    from jarvis import security_gate as sg
    spec = sg.spec_for("discovered_yeni_bir_arac")
    assert spec is not None and spec.effect is sg.Effect.EXECUTE


def test_effects_table_has_no_stale_entries():
    from jarvis import security_gate as sg
    reachable = sg.collect_reachable_tools()
    stale = set(sg.EFFECTS) - set(reachable)
    assert stale == set(), f"Hicbir yoldan erisilemeyen spec'ler: {stale}"


def test_specs_record_the_paths_they_are_reachable_from():
    from jarvis import security_gate as sg
    specs = sg.build_specs()
    S = sg.Source
    assert specs["file_controller"].sources >= {S.MODEL_LIVE, S.AGENT_LOOP, S.BRAIN_TEAM, S.ROUTER}
    assert specs["web_search"].sources >= {S.MODEL_LIVE, S.AGENT_LOOP, S.CLI_AGENT}


# ── Eyleme bagli etki MEVCUT siniflandiricilardan gelir ──

def test_file_controller_effect_comes_from_readonly_actions():
    from jarvis import security_gate as sg
    from jarvis.actions import file_controller as fc
    spec = sg.spec_for("file_controller")
    for action in list(fc.READONLY_ACTIONS) + list(fc.ACTION_ALIASES) + ["write", "bilinmeyen", ""]:
        expected = sg.Effect.READ if fc.is_readonly_action(action) else sg.Effect.MUTATE
        assert spec.effect_of({"action": action}) is expected, action


@pytest.mark.parametrize("command", ["ls", "git status", "cat x", "python3 -c 'print(1)'",
                                     "find . -delete", "rm -rf x", "git branch yeni"])
def test_terminal_effect_comes_from_is_readonly(command):
    import shlex
    from jarvis import security_gate as sg
    from jarvis.actions.terminal_tool import _is_readonly
    expected = sg.Effect.READ if _is_readonly(shlex.split(command)) else sg.Effect.EXECUTE
    assert sg.spec_for("terminal").effect_of({"command": command}) is expected


def test_computer_settings_effect_comes_from_dangerous_actions():
    from jarvis import security_gate as sg
    from jarvis.actions.computer_settings import _DANGEROUS_ACTIONS
    spec = sg.spec_for("computer_settings")
    for action in _DANGEROUS_ACTIONS:
        assert spec.effect_of({"action": action}) is sg.Effect.SYSTEM
    assert spec.effect_of({"description": "bilgisayarı kapat"}) is sg.Effect.SYSTEM  # LLM tahmin eder
    assert spec.effect_of({"action": "volume_up"}) is sg.Effect.MUTATE


def test_readonly_tools_of_agent_loop_are_read_or_documented():
    """tools_kopru'nun onaysiz calistirdigi her arac spec'te READ'dir ya da
    bilinen/belgelenmis bir istisnadir (plan §3.3)."""
    from jarvis import security_gate as sg
    from jarvis.actions import tools_kopru
    for name in tools_kopru._READONLY_TOOLS:
        spec = sg.spec_for(name)
        assert spec.effect is sg.Effect.READ or name in sg.KNOWN_UNGATED_NON_READ, name


# ── Kapiyi yalnizca kapidan gecirilmis yollar kullaniyor (Adim 2) ──
# E1/E2: main.py; E3: tools/agent/react_runtime.py. Yeni bir kullanici
# eklenirse bu liste bilincli olarak guncellenmeli.

def test_only_the_gated_paths_import_the_gate():
    import ast
    root = Path(__file__).resolve().parents[1]
    users = []
    for path in (
        [
            p for p in (root / "src").rglob("*.py")
            if "jarvis_yedekler" not in p.relative_to(root).parts
        ]
        + list((root / "tools").rglob("*.py"))
    ):
        if path.name == "security_gate.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom):
                mods = [node.module or ""] + [f"{node.module or ''}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            if any(m.split(".")[-1] == "security_gate" for m in mods):
                users.append(str(path.relative_to(root)))
                break
    # Adim 3.4: agent_loop (E5) ve Brain Team (E6) de kapidan gecer.
    assert sorted(users) == ["src/jarvis/actions/agent_loop.py", "src/jarvis/core/brain_orchestrator.py",
                             "src/jarvis/main.py", "tools/agent/react_runtime.py"], users
