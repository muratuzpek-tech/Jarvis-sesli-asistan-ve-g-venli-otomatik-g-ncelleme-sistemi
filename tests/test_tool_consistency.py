"""tests/test_tool_consistency.py — main.py TOOL_DECLARATIONS tutarlılığı.

Her beyan edilen araç için (denetim scripts/tool_inventory.py'de, tablo
olarak oradan da yazdırılır):

  a) _execute_tool dispatch kolu ya da tools/ registry işleyicisi var ve
     sonuç "Unknown tool" ile ezilmiyor
  b) beyandaki parametre adları/tipleri işleyicinin okuduğu anahtarlarla
     uyuşuyor
  c) security_gate.EFFECTS kaydı var
  d) MODEL_LIVE / AGENT_LOOP / BRAIN_TEAM / REACT için authorize() kararı
     tanımlı (istisna yok) ve READ olmayan etkide ALLOW yalnızca açık bir
     politika tablosundan geliyor
  e) tests/ altında araç adını anan en az bir test var

Her denetim iki testtir: *_no_new_* testleri bilinen listeye girmeyen her
yeni uyumsuzlukta DÜŞER; *_clean testleri bilinen uyumsuzlukta
xfail(strict=False) olur (uyumsuzluk giderilince XPASS -> KNOWN_* listesinden
çıkarılmalı). Kaynaklar yalnızca AST ile okunur; main.py import edilmez.
"""
import importlib.util
import sys
from functools import lru_cache
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "tool_inventory.py"
_spec = importlib.util.spec_from_file_location("tool_inventory", _SCRIPT)
inventory = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("tool_inventory", inventory)   # dataclass modülü sys.modules'ta arar
_spec.loader.exec_module(inventory)

TOOL_NAMES = [d["name"] for d in inventory.load_declarations()]


@lru_cache(maxsize=1)
def _inv() -> dict:
    return {t.name: t for t in inventory.build_inventory()}


# ── Bilinen uyumsuzluklar (2026-10-07 envanteri) ──────────────────────────

KNOWN_DISPATCH: dict[str, str] = {}

KNOWN_PARAMS: dict[str, tuple[set[str], str]] = {
    "terminal": ({"okunuyor ama beyan edilmemiş: stdin"},
                 "terminal_tool 'input' yoksa eski 'stdin' takma adına düşer; beyanda yalnızca input"),
    "browser_control": ({"okunuyor ama beyan edilmemiş: fields"},
                        "fill_form 'fields'i dict bekler; Gemini şeması özelliksiz OBJECT'i "
                        "kabul etmediği için beyan edilmedi"),
    "code_helper": ({"okunuyor ama beyan edilmemiş: instruction",
                     "okunuyor ama beyan edilmemiş: project_name",
                     "okunuyor ama beyan edilmemiş: query"},
                    "edit 'instruction'a, agentic_code yönlendirmesi query/project_name'e düşer; beyansız"),
    "dev_agent": ({"okunuyor ama beyan edilmemiş: code",
                   "okunuyor ama beyan edilmemiş: query"},
                  "agentic_code yönlendirmesi code/query'ye düşer; beyanda yok"),
    "discovered_topydo": ({"okunuyor ama beyan edilmemiş: content",
                           "okunuyor ama beyan edilmemiş: file_path",
                           "okunuyor ama beyan edilmemiş: id",
                           "okunuyor ama beyan edilmemiş: index",
                           "okunuyor ama beyan edilmemiş: search",
                           "okunuyor ama beyan edilmemiş: text"},
                          "takma ad anahtarları (id/index/search/text/content/file_path) beyansız"),
}

KNOWN_EFFECTS: dict[str, str] = {}

KNOWN_UNTESTED: dict[str, str] = {}

# READ olmayan etkide ALLOW veren (araç, argüman varyantı, kaynak) üçlüleri.
# Hepsi security_gate._LIVE_POLICY'de gerekçesiyle yazılı (aracın kendi
# önizleme/onay akışı ya da bugünkü bilinçli davranış). Yeni bir üçlü testi
# düşürür: bilinçliyse buraya ve politika tablosuna gerekçesiyle eklenmeli.
EXPECTED_NON_READ_ALLOW = {
    ("terminal", "{}", "MODEL_LIVE"),
    ("terminal", "risky", "MODEL_LIVE"),
    ("youtube_video", "{}", "MODEL_LIVE"),
    ("close_camera", "{}", "MODEL_LIVE"),
    ("computer_settings", "risky", "MODEL_LIVE"),
    ("task_manager", "{}", "MODEL_LIVE"),
    ("start_parallel_task", "{}", "MODEL_LIVE"),
    ("code_helper", "{}", "MODEL_LIVE"),
    ("dev_agent", "{}", "MODEL_LIVE"),
    ("self_improve", "{}", "MODEL_LIVE"),
    ("agent_loop", "{}", "MODEL_LIVE"),
    ("game_updater", "{}", "MODEL_LIVE"),
    ("game_updater", "risky", "MODEL_LIVE"),
    ("flight_finder", "risky", "MODEL_LIVE"),
    ("shutdown_jarvis", "{}", "MODEL_LIVE"),
    ("save_memory", "{}", "MODEL_LIVE"),
    ("reminder", "{}", "REACT"),
}


def _params(names, known: dict):
    out = []
    for name in names:
        reason = known.get(name)
        if isinstance(reason, tuple):
            reason = reason[1]
        marks = [pytest.mark.xfail(strict=False, reason=reason)] if reason else []
        out.append(pytest.param(name, id=name, marks=marks))
    return out


# ── Envanter bütünlüğü ────────────────────────────────────────────────────

def test_declarations_are_unique_and_inventoried():
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES)), "TOOL_DECLARATIONS'ta yinelenen ad"
    assert set(_inv()) == set(TOOL_NAMES)


def test_known_lists_reference_declared_tools():
    for known in (KNOWN_DISPATCH, KNOWN_PARAMS, KNOWN_EFFECTS, KNOWN_UNTESTED):
        assert set(known) <= set(TOOL_NAMES)
    assert {t for t, _, _ in EXPECTED_NON_READ_ALLOW} <= set(TOOL_NAMES)


def test_every_dispatch_branch_is_declared():
    assert inventory.branches_without_declaration() == []


# ── a) dispatch ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", TOOL_NAMES)
def test_dispatch_no_new_problem(name):
    tool = _inv()[name]
    if name not in KNOWN_DISPATCH:
        assert tool.dispatch_problem is None, f"{name}: {tool.dispatch_problem}"
    assert tool.branch or tool.registry


@pytest.mark.parametrize("name", _params(TOOL_NAMES, KNOWN_DISPATCH))
def test_dispatch_clean(name):
    assert _inv()[name].dispatch_problem is None


# ── b) parametreler ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name", TOOL_NAMES)
def test_params_no_new_mismatch(name):
    problems = set(_inv()[name].param_problems())
    known = KNOWN_PARAMS.get(name, (set(), ""))[0]
    assert problems <= known, f"{name}: yeni uyumsuzluk {sorted(problems - known)}"


@pytest.mark.parametrize("name", _params(TOOL_NAMES, KNOWN_PARAMS))
def test_params_clean(name):
    assert _inv()[name].param_problems() == []


def test_required_params_are_declared():
    for name in TOOL_NAMES:
        tool = _inv()[name]
        assert set(tool.required) <= set(tool.declared), name


# ── c) EFFECTS ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", _params(TOOL_NAMES, KNOWN_EFFECTS))
def test_effects_registered(name):
    assert _inv()[name].effect is not None


# ── d) authorize / kaynak politikaları ────────────────────────────────────

@pytest.mark.parametrize("name", TOOL_NAMES)
def test_authorize_defined_for_all_sources(name):
    tool = _inv()[name]
    for label, pol in tool.variant_policies.items():
        assert set(pol) == set(inventory.SOURCES)
        errors = {s: v for s, v in pol.items() if v.startswith("ERROR")}
        assert not errors, f"{name} args={label}: {errors}"


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_non_read_allow_is_expected(name):
    got = {(name, label, src) for label, src, _eff, _v in _inv()[name].non_read_allow}
    unexpected = got - EXPECTED_NON_READ_ALLOW
    assert not unexpected, f"beklenmedik ALLOW: {sorted(unexpected)}"


def test_non_read_allow_comes_from_explicit_policy():
    from jarvis import security_gate as gate
    tables = {"MODEL_LIVE": gate._LIVE_POLICY, "AGENT_LOOP": gate._AGENT_LOOP_POLICY}
    for tool in _inv().values():
        for label, src, eff, _v in tool.non_read_allow:
            explicit = tool.name in tables.get(src, {}) or (
                src in ("MODEL_LIVE", "REACT") and gate._registry_policy(tool.name) is not None)
            assert explicit, f"{tool.name} args={label} {src}: {eff} etkide politikasız ALLOW"


def test_expected_non_read_allow_still_observed():
    """Snapshot güncel kalsın: artık ALLOW vermeyen üçlü listeden çıkarılmalı."""
    got = {(t.name, label, src) for t in _inv().values() for label, src, _e, _v in t.non_read_allow}
    assert EXPECTED_NON_READ_ALLOW <= got, sorted(EXPECTED_NON_READ_ALLOW - got)


def test_model_live_never_denies_declared_tool():
    for tool in _inv().values():
        for label, pol in tool.variant_policies.items():
            assert pol["MODEL_LIVE"] != "deny", f"{tool.name} args={label}: beyanlı araç DENY"


# ── e) testler ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", _params(TOOL_NAMES, KNOWN_UNTESTED))
def test_tool_is_mentioned_in_tests(name):
    assert _inv()[name].test_files, f"{name}: tests/ altında anan test yok"
