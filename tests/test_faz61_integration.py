"""
tests/test_faz61_integration.py — FAZ 6.1 Registry entegrasyon testleri

Test A-C: Registry tool kayıtlı
Test D:   generate_declarations() → 3 tool var
Test E:   _build_config() → 3 yeni declaration config'e giriyor
Test F:   _execute_tool() → registry üzerinden çalışıyor
Test G:   Unknown tool → kontrollü hata
Test H:   Registry exception → Jarvis runtime öldürmüyor
"""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Setup: Trigger registry registration ────────────────────
import tools.developer  # noqa: F401
import tools.media     # noqa: F401
import tools.agent     # noqa: F401

from tools.registry import registry, ToolContext
from tools.schemas import generate_declarations


# ── TEST A: react_agent kayıtlı ────────────────────────────
def test_a_react_agent_registered():
    entry = registry.get("react_agent")
    assert entry is not None, "react_agent registry'de YOK"
    assert entry.enabled, "react_agent DISABLED"


# ── TEST B: agentic_code kayıtlı ───────────────────────────
def test_b_agentic_code_registered():
    entry = registry.get("agentic_code")
    assert entry is not None, "agentic_code registry'de YOK"
    assert entry.enabled, "agentic_code DISABLED"


# ── TEST C: spotify_control kayıtlı ────────────────────────
def test_c_spotify_control_registered():
    entry = registry.get("spotify_control")
    assert entry is not None, "spotify_control registry'de YOK"
    assert entry.enabled, "spotify_control DISABLED"


# ── TEST D: generate_declarations() → 3 tool ───────────────
def test_d_generate_declarations_contains_new_tools():
    decls = generate_declarations()
    names = {d.get("name") for d in decls}
    assert "react_agent" in names, f"react_agent yok: {names}"
    assert "agentic_code" in names, f"agentic_code yok: {names}"
    assert "spotify_control" in names, f"spotify_control yok: {names}"


# ── TEST E: _build_config() → merge doğru mu ───────────────
def test_e_build_config_merges_declarations():
    """Kontrol: main.py'de _build_config regex ile merge eklendi mi?"""
    code = Path("src/jarvis/main.py").read_text()
    # Merge mevcut olmalı
    assert "_gen_decls()" in code, "_build_config merge edilmemis"
    assert "_JARVIS2_REGISTRY_AVAILABLE" in code, "Registry flag yok"
    # TOOL_DECLARATIONS hala mevcut olmalı (eski declarations silinmemis)
    assert "TOOL_DECLARATIONS" in code, "TOOL_DECLARATIONS silinmis!"


# ── TEST F: _execute_tool() → registry routing ─────────────
def test_f_execute_tool_routes_to_registry():
    """Kontrol: main.py'de registry routing var mı?"""
    code = Path("src/jarvis/main.py").read_text()
    assert "_JARVIS2_REGISTRY_TOOL_NAMES" in code, "Registry routing yok"
    assert "_jarvis2_registry.execute" in code, "registry.execute() çağrılmıyor"
    # Eski dispatch korunmalı
    assert 'name == "save_memory"' in code, "Eski save_memory silinmis!"


# ── TEST G: Unknown tool → kontrollü hata ──────────────────
def test_g_unknown_tool_no_crash():
    r = asyncio.run(registry.execute("nonexistent_tool_xyz", {}))
    assert isinstance(r, str), f"Dönüş tipi: {type(r)}"
    assert "Unknown" in r or "error" in r.lower(), f"Kontrollü hata degil: {r}"


# ── TEST H: Registry exception → runtime ayakta kalır ──────
def test_h_registry_exception_survives():
    """Handler exception → temiz hata mesajı, çökme yok."""
    from tools.registry import register

    @register(name="_faz61_crash", description="test crash",
              parameters={}, security="normal", category="test")
    def _crash_handler(args, ctx):
        raise RuntimeError("Intentional crash test")

    r = asyncio.run(registry.execute("_faz61_crash", {}, ctx=ToolContext()))
    assert isinstance(r, str), f"Dönüş tipi: {type(r)}"
    assert "Tool error" in r or "RuntimeError" in r, f"Hata mesajı: {r}"
    # Jarvis canlı kalır → bu test'in ulaşması zaten bunu kanıtlar


# ── BRAIN TEST FIX: Class-level defaults çalışıyor ─────────
def test_i_brain_class_defaults():
    """JarvisLive.__new__() ile oluşturulan nesne _pending_dangerous_action'a sahip."""
    from jarvis.main import JarvisLive

    obj = JarvisLive.__new__(JarvisLive)
    # __init__() ÇALIŞMADI — ama class-level defaults OLmalı
    assert hasattr(obj, "_pending_dangerous_action"), "_pending_dangerous_action YOK"
    assert obj._pending_dangerous_action is None
    assert hasattr(obj, "_dangerous_confirmation_granted")
    assert obj._dangerous_confirmation_granted is False
    assert hasattr(obj, "_pending_terminal_command")


# ── SECURITY: Mevcut davranış korunmuş ─────────────────────
def test_j_security_behavior_preserved():
    """Code review: güvenlik satırları hala orijinal değerlerle mevcut."""
    code = Path("src/jarvis/main.py").read_text()

    # __init__ içindeki security satırları SİLİNMEMİş OLMALI
    assert "self._pending_dangerous_action = None" in code
    assert "self._dangerous_confirmation_granted = False" in code

    # Onay kontrolü hala çalışıyor olmalı
    assert "onaylıyorum" in code
    assert "_dangerous_confirmation_granted = True" in code

    # Registry routing → CONFIRMATION_REQUIRED protokolü
    assert "CONFIRMATION_REQUIRED:" in code

    # Terminal confirmation korunmuş
    assert "_pending_terminal_command" in code
