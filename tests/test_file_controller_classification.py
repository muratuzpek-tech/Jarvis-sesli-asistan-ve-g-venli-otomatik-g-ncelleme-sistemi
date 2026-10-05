"""tests/test_file_controller_classification.py

tools_kopru.is_destructive ve SecurityAI.classify_risk bilinen yikici
file_controller eylemlerini LISTELIYORDU; listede olmayan her ad guvenli
sayiliyordu. file_controller ise eylem adini kendi takma ad tablosundan
gecirdigi icin 'trash', 'sil', 'oluştur' gibi adlar onaysiz silme/olusturma
yapiyordu; 'write', 'find_replace', 'extract' Brain Team yolunda LOW riskti.

Artik tek kaynak file_controller.ACTION_ALIASES ve salt-okunur eylem listesi:
{list, read, find, largest, disk_usage, info} disindaki HER eylem (takma
adlar ve bilinmeyen adlar dahil) yikici / HIGH risktir.
"""
import pytest

from jarvis.actions import file_controller as fc
from jarvis.actions import tools_kopru
from jarvis.brains.security_ai import SecurityAI

READONLY = ["list", "read", "find", "largest", "disk_usage", "info"]

MUTATING = [
    "create_file", "create_folder", "delete", "delete_all_files", "move",
    "copy", "rename", "write", "find_replace", "organize_desktop", "extract",
]

# file_controller dispatcher'inin kabul ettigi takma adlar -> gercek eylem.
ALIASES = {
    "sil": "delete",
    "sil_file": "delete",
    "delete_file": "delete",
    "remove": "delete",
    "trash": "delete",
    "delete_all": "delete_all_files",
    "delete_all_files": "delete_all_files",
    "bulk_delete": "delete_all_files",
    "tümünü sil": "delete_all_files",
    "tumunu sil": "delete_all_files",
    "oluştur": "create_file",
    "olustur": "create_file",
}

UNKNOWN = ["frobnicate", "read_file", "write_file", "move_file", "chmod", "symlink", ""]


def _destructive(action):
    return tools_kopru.is_destructive("file_controller", {"action": action})


def _risk(action):
    return SecurityAI.classify_risk("file_controller", action)


# ── Salt-okunur eylemler ──

@pytest.mark.parametrize("action", READONLY + ["LIST", " Read ", "Info"])
def test_readonly_actions_are_safe(action):
    assert _destructive(action) is False
    assert _risk(action) == "low"


# ── Yikici eylemler ve HER takma ad ──

@pytest.mark.parametrize("action", MUTATING + list(ALIASES) + ["DELETE", " Trash ", "Sil"])
def test_mutating_actions_and_aliases_are_destructive(action):
    assert _destructive(action) is True
    assert _risk(action) == "high"


# ── Bilinmeyen eylem adlari guvenli sayilmaz ──

@pytest.mark.parametrize("action", UNKNOWN)
def test_unknown_actions_are_destructive(action):
    assert _destructive(action) is True
    assert _risk(action) == "high"


def test_missing_action_is_destructive():
    assert tools_kopru.is_destructive("file_controller", {}) is True
    assert SecurityAI.classify_risk("file_controller", None) == "high"


@pytest.mark.parametrize("action", ["oluştur", "trash", "sil", "bulk_delete", "write"])
def test_agent_loop_cannot_run_alias_without_approval(monkeypatch, action):
    # Gercek file_controller ASLA calismaz: eski kod bu testte gercek
    # Masaustune dosya olusturuyordu. Cagrilirsa kaydedilir ve test kirmizi olur.
    ran = []
    monkeypatch.setattr(fc, "file_controller", lambda parameters=None, **_k: ran.append(parameters) or "RAN")
    with pytest.raises(PermissionError):
        tools_kopru.call_tool("file_controller", {"action": action, "path": "desktop", "name": "x.txt"})
    assert ran == []


# ── Tek kaynak: ACTION_ALIASES ──

def test_action_aliases_is_the_dispatcher_table():
    assert fc.ACTION_ALIASES == ALIASES
    for alias, canonical in ALIASES.items():
        assert fc.normalize_action(alias) == canonical
    assert fc.normalize_action("  TRASH ") == "delete"
    assert set(fc.READONLY_ACTIONS) == set(READONLY)


def test_classifiers_and_dispatcher_share_action_aliases(monkeypatch):
    """Yeni bir takma ad TEK yere eklenince dispatcher, is_destructive ve
    classify_risk ayni sonucu verir - iki ayri tablonun kaymasi imkansiz."""
    monkeypatch.setitem(fc.ACTION_ALIASES, "goster", "list")

    assert _destructive("goster") is False
    assert _risk("goster") == "low"
    out = fc.file_controller(parameters={"action": "goster", "path": "/nonexistent-jarvis-dir"})
    assert not out.startswith("Unknown action")


def test_other_tools_are_unaffected():
    assert SecurityAI.classify_risk("computer_settings", "shutdown") == "high"
    assert SecurityAI.classify_risk("coder_ai", "write_new_file") == "medium"
    assert SecurityAI.classify_risk("research_ai", "search") == "low"
    assert tools_kopru.is_destructive("computer_settings", {"action": "volume"}) is False
    assert tools_kopru.is_destructive("send_message", {}) is True
