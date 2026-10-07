"""code_search: sesli araç yolu (main.JarvisLive._execute_tool) agentgrep
çıktısını modele döndürmeli; sonuç "Unknown tool: code_search" ile
ezilmemeli (tests/test_tool_consistency.py KNOWN_DISPATCH bulgusu).

agentgrep ikilisi ve subprocess.run sahte; ağ/gerçek arama yok."""
import asyncio
import shutil
import subprocess
from types import SimpleNamespace

import jarvis.main as main_mod
from jarvis.main import JarvisLive


class _UI:
    muted = False
    current_file = None

    def __getattr__(self, _name):
        return lambda *a, **k: None


def _jarvis() -> JarvisLive:
    jl = JarvisLive.__new__(JarvisLive)
    jl.ui = _UI()
    jl.speak = lambda *a, **k: None
    jl.speak_error = lambda *a, **k: None
    return jl


def _call(jl, name, **args):
    resp = asyncio.run(jl._execute_tool(SimpleNamespace(name=name, args=args, id="1")))
    return resp.response


def test_code_search_returns_agentgrep_output(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout="src/jarvis/main.py:12: def foo()\n", stderr="")

    monkeypatch.setattr(shutil, "which", lambda name: "/fake/agentgrep" if name == "agentgrep" else None)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(main_mod, "_JARVIS2_REGISTRY_AVAILABLE", False)

    resp = _call(_jarvis(), "code_search", mode="find", query="def foo", path="src")

    assert calls == [["/fake/agentgrep", "find", "def", "foo", "--path", "src"]]
    assert resp == {"result": "src/jarvis/main.py:12: def foo()"}
    assert "Unknown tool" not in str(resp)


def test_code_search_reports_runner_error(monkeypatch):
    def boom(cmd, **kwargs):
        raise FileNotFoundError("agentgrep yok")

    monkeypatch.setattr(shutil, "which", lambda name: "/fake/agentgrep")
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(main_mod, "_JARVIS2_REGISTRY_AVAILABLE", False)

    result = str(_call(_jarvis(), "code_search", mode="find", query="x")["result"])

    assert result.startswith("code_search error:")
    assert "Unknown tool" not in result
