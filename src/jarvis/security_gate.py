"""security_gate.py — tek güvenlik kapısı (docs/GUVENLIK_KAPISI_PLAN.md).

ADIM 0 + ADIM 1 (2026-10-05): DAVRANIŞ DEĞİŞMEZ. Bu modül henüz hiçbir
dağıtıcı tarafından karar vermek için kullanılmıyor; yalnızca:

  * kapının veri tipleri (Source, Effect, Verdict, ResolvedCall, Decision),
  * onay deposu arayüzü (ApprovalStore) ve bugünkü TEK onay yuvasını
    (main.JarvisLive._set_pending_dangerous / _grant_dangerous_confirmation /
    _consume_dangerous_confirmation) saran adaptör (PendingSlotAdapter),
  * onay parmak izinin TEK kaynağı (fingerprint; main._action_fingerprint
    buraya devreder),
  * her araç için bir ToolSpec ve araçların HANGİ giriş yollarından
    erişilebildiğini mevcut tablolardan toplayan tarayıcı
    (collect_reachable_tools).

Tarayıcı kaynak dosyaları yalnızca AST ile OKUR: main.py, tools/ registry
kayıtları, executor_ai, Brain orkestratörü, CLI ajanı ve eklentiler
çalıştırılmaz/yüklenmez. Yalnızca tools_kopru.ALLOWED_TOOLS import edilerek
okunur (çalışma anında entegrasyonla eklenen araçlar da görünsün diye).

Yeni bir araç herhangi bir yola eklenip EFFECTS tablosuna girmezse
missing_specs() onu raporlar ve tests/test_security_gate.py kırmızı olur.
"""
from __future__ import annotations

import abc
import ast
import enum
import json
import os
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PKG_DIR = Path(__file__).resolve().parent            # src/jarvis
_PROJECT_ROOT = _PKG_DIR.parent.parent

MAIN_PY = _PKG_DIR / "main.py"
TOOLS_DIR = _PROJECT_ROOT / "tools"
EXECUTOR_PY = _PKG_DIR / "brains" / "executor_ai.py"
ORCHESTRATOR_PY = _PKG_DIR / "core" / "brain_orchestrator.py"
CLI_AGENT_PY = _PKG_DIR / "core" / "agent.py"
PLUGIN_DIR = _PKG_DIR / "plugins"                      # core/plugin_manager.PLUGIN_DIR


# ── Tipler ────────────────────────────────────────────────────────────────

class Source(enum.Enum):
    """Bir araç çağrısının geldiği giriş yolu (plan §1)."""
    MODEL_LIVE = "model_live"      # E1 Gemini Live araç çağrısı, E2 registry
    REACT = "react"                # E3 ReAct alt ajanı (registry.execute)
    ROUTER = "router"              # E4 deterministik metin yönlendiricileri
    AGENT_LOOP = "agent_loop"      # E5 arka plan görev döngüsü (tools_kopru)
    BRAIN_TEAM = "brain_team"      # E6 Brain Team orkestratörü
    SCHEDULER = "scheduler"        # E13 zamanlanmış istem
    PROACTIVE = "proactive"        # proaktif mod / brifing
    CLI_AGENT = "cli_agent"        # E15 CLI metin ajanı (core/agent.py)
    PLUGIN = "plugin"              # E16 CLI eklentileri


class Effect(enum.IntEnum):
    """Aracın en kötü etkisi; büyük = tehlikeli."""
    READ = 0
    MUTATE = 1
    EXTERNAL = 2
    EXECUTE = 3
    SYSTEM = 4


class Verdict(enum.Enum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


def fingerprint(action: str, args: Any) -> str:
    """Onaylanan işlemin kimliği: araç adı + argümanlar. Sayılar
    registry.execute'taki gibi str'ye çevrilir (3 ve "3" aynı işlemdir).
    main.JarvisLive._action_fingerprint bunu kullanır - TEK kaynak."""
    if isinstance(args, dict):
        args = {k: str(v) if isinstance(v, (int, float)) else v for k, v in args.items()}
    return action + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)


@dataclass(frozen=True)
class ResolvedCall:
    """Bir kez çözülmüş çağrı. Kapı devreye girdiğinde yürütme bu nesneyle
    yapılacak, yeniden çözümleme olmayacak (plan §4.1-1)."""
    tool: str
    action: str | None
    params: dict
    targets: tuple[str, ...]
    effect: Effect
    fingerprint: str

    @classmethod
    def for_pending(cls, action: str, args: dict) -> "ResolvedCall":
        """Brain Team / agent_loop gibi sözde eylemler için (onay yuvası
        bunları 'brain_team' / 'agent_loop' adıyla tutar)."""
        return cls(tool=action, action=None, params=dict(args), targets=(),
                   effect=Effect.EXECUTE, fingerprint=fingerprint(action, args))


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    call: ResolvedCall | None
    reason: str
    model_message: str
    user_prompt: str | None = None
    request_id: str | None = None   # iç kimlik; modele verilmez


# ── Onay deposu ───────────────────────────────────────────────────────────

class ApprovalStore(abc.ABC):
    """Onay deposu arayüzü. Onay yalnızca girdi katmanının bildirdiği gerçek
    kullanıcı turundan gelir; model argümanı hiçbir zaman onay değildir."""

    @abc.abstractmethod
    def request(self, call: ResolvedCall) -> str:
        """Çağrıyı onay bekleyen olarak kaydeder; iç istek kimliğini döner."""

    @abc.abstractmethod
    def grant_from_user_turn(self) -> None:
        """Kullanıcının gerçek turundaki "evet"i bekleyen isteğe uygular."""

    @abc.abstractmethod
    def consume(self, call: ResolvedCall) -> bool:
        """Aynı çağrı için verilmiş, süresi dolmamış onay varsa tüketir."""

    @abc.abstractmethod
    def cancel(self, request_id: str | None = None) -> None:
        """Bekleyen isteği iptal eder (request_id verilirse yalnızca o)."""

    @abc.abstractmethod
    def pending(self) -> tuple[str, str] | None:
        """(araç, parmak izi) ya da None."""


class PendingSlotAdapter(ApprovalStore):
    """Bugünkü tek onay yuvasını (main.JarvisLive) ApprovalStore olarak
    sunar. Hiçbir kural eklemez/değiştirmez: TTL (_CONFIRMATION_TTL_S), tek
    kullanım, parmak izi eşleşmesi ve "yeni istek eski onayı taşımaz"
    davranışları olduğu gibi main.py'den gelir."""

    def __init__(self, owner: Any) -> None:
        self._owner = owner

    def request(self, call: ResolvedCall) -> str:
        self._owner._set_pending_dangerous(call.tool, call.fingerprint)
        return call.fingerprint

    def grant_from_user_turn(self) -> None:
        self._owner._grant_dangerous_confirmation()

    def consume(self, call: ResolvedCall) -> bool:
        return self._owner._consume_dangerous_confirmation(call.tool, call.params)

    def cancel(self, request_id: str | None = None) -> None:
        with self._owner._confirmation_lock:
            if request_id is None or self._owner._pending_dangerous_fingerprint == request_id:
                self._owner._set_pending_dangerous(None)

    def pending(self) -> tuple[str, str] | None:
        with self._owner._confirmation_lock:
            action = self._owner._pending_dangerous_action
            if action is None:
                return None
            return action, self._owner._pending_dangerous_fingerprint


# ── Araç tanımları (plan §3) ──────────────────────────────────────────────

@dataclass(frozen=True)
class ToolSpec:
    name: str
    effect: Effect                                   # en kötü durum
    sources: frozenset[Source] = frozenset()         # erişilebildiği yollar
    effect_of: Callable[[Mapping], Effect] | None = field(default=None, compare=False)

    def effect_for(self, params: Mapping | None) -> Effect:
        return self.effect_of(params or {}) if self.effect_of else self.effect


# Eyleme bağlı etkiler - MEVCUT sınıflandırıcıları kullanır, kopyalamaz.

def _file_controller_effect(params: Mapping) -> Effect:
    from jarvis.actions.file_controller import is_readonly_action
    return Effect.READ if is_readonly_action(params.get("action")) else Effect.MUTATE


def _terminal_effect(params: Mapping) -> Effect:
    # Fail-closed sapma: terminal_tool "cd X && cmd" önekini ayıklayıp cmd'yi
    # değerlendirir; burada önek ayıklanmaz, böyle komutlar EXECUTE sayılır.
    from jarvis.actions.terminal_tool import _is_readonly
    try:
        argv = shlex.split(str(params.get("command", "")), posix=os.name != "nt")
    except ValueError:
        return Effect.EXECUTE
    return Effect.READ if _is_readonly(argv) else Effect.EXECUTE


def _computer_settings_effect(params: Mapping) -> Effect:
    from jarvis.actions.computer_settings import _DANGEROUS_ACTIONS
    action = str(params.get("action", "")).lower().strip().replace(" ", "_").replace("-", "_")
    if not action or action in _DANGEROUS_ACTIONS:
        # Boş eylemde computer_settings eylemi LLM ile tahmin eder (kapatma
        # dahil) - en kötü durum.
        return Effect.SYSTEM
    return Effect.MUTATE


def _game_updater_effect(params: Mapping) -> Effect:
    shutdown = str(params.get("shutdown_when_done", "false")).lower() == "true"
    return Effect.SYSTEM if shutdown else Effect.EXECUTE


def _save_flag(base: Effect) -> Callable[[Mapping], Effect]:
    def _effect(params: Mapping) -> Effect:
        saving = str(params.get("save", "false")).lower() in ("true", "1", "yes")
        return max(base, Effect.MUTATE) if saving else base
    return _effect


def _coder_ai_effect(params: Mapping) -> Effect:
    return Effect.READ if params.get("operation") == "analyze" else Effect.MUTATE


def _const(effect: Effect) -> Callable[[Mapping], Effect]:
    return lambda _params: effect


# (en kötü etki, eyleme bağlı etki). Plan §3 tablosu.
EFFECTS: dict[str, tuple[Effect, Callable[[Mapping], Effect] | None]] = {
    # 3.1 Gemini araçları (main.py TOOL_DECLARATIONS / _execute_tool)
    "open_app": (Effect.EXECUTE, None),
    "web_search": (Effect.READ, None),
    "terminal": (Effect.EXECUTE, _terminal_effect),
    "system_status": (Effect.READ, None),
    "system_scan_and_repair": (Effect.EXECUTE, None),
    "weather_report": (Effect.READ, None),
    "send_message": (Effect.EXTERNAL, None),
    "reminder": (Effect.MUTATE, None),
    "youtube_video": (Effect.EXTERNAL, None),
    "screen_process": (Effect.READ, None),
    "close_camera": (Effect.MUTATE, None),
    "computer_settings": (Effect.SYSTEM, _computer_settings_effect),
    "browser_control": (Effect.EXTERNAL, None),
    "file_controller": (Effect.MUTATE, _file_controller_effect),
    "task_manager": (Effect.MUTATE, None),
    "health_check": (Effect.READ, None),
    "start_parallel_task": (Effect.EXECUTE, None),
    "check_agent_board": (Effect.READ, None),
    "desktop_control": (Effect.MUTATE, None),
    "code_helper": (Effect.EXECUTE, None),
    "code_search": (Effect.READ, None),
    "dev_agent": (Effect.EXECUTE, None),
    "self_improve": (Effect.MUTATE, None),
    "agent_loop": (Effect.MUTATE, None),
    "recall_conversation": (Effect.READ, None),
    "github_arama": (Effect.READ, None),
    "discovered_topydo": (Effect.EXECUTE, None),
    "discovered_jc": (Effect.EXECUTE, None),
    "computer_control": (Effect.EXECUTE, None),
    "game_updater": (Effect.SYSTEM, _game_updater_effect),
    "flight_finder": (Effect.MUTATE, _save_flag(Effect.READ)),
    "shutdown_jarvis": (Effect.SYSTEM, None),
    "file_processor": (Effect.MUTATE, None),
    "save_memory": (Effect.MUTATE, None),
    # 3.2 Registry (tools/)
    "agentic_code": (Effect.EXECUTE, None),
    "react_agent": (Effect.EXECUTE, None),     # kayıtlı her registry aracını çağırabilir
    "spotify_control": (Effect.EXTERNAL, None),
    # 3.3 agent_loop köprüsü (tools_kopru.ALLOWED_TOOLS) - ek olanlar
    "windows_system": (Effect.READ, None),
    "github_arac_bul_ve_degerlendir": (Effect.MUTATE, None),
    "entegrasyon_uygula": (Effect.EXECUTE, None),
    "discovery_register": (Effect.MUTATE, None),
    # 3.4 Brain Team uygulayıcıları
    "backup_create": (Effect.MUTATE, None),
    "backup_rollback": (Effect.MUTATE, None),
    "vault_encrypt": (Effect.MUTATE, None),
    "vault_decrypt": (Effect.MUTATE, None),
    "github_search": (Effect.READ, None),
    "coder_ai": (Effect.MUTATE, _coder_ai_effect),
    "research_ai": (Effect.READ, None),
    # 3.5 CLI ajanı (core/agent.py TOOLS)
    "file_list": (Effect.READ, None),
    "file_read": (Effect.READ, None),
    "file_write": (Effect.MUTATE, None),
    "run_python": (Effect.EXECUTE, None),
    "remember": (Effect.MUTATE, None),
    "file_delete": (Effect.MUTATE, None),
    # Deterministik yönlendirici sözde aracı (brain_team_tool / Brain görevi)
    "brain_team": (Effect.MUTATE, None),
}

# Entegrasyonla eklenen dış kod: her ad için fail-closed genel spec.
DISCOVERED_PREFIX = "discovered_"

# Bir yolun bugün onaysız çalıştırdığı ama READ olmayan, BİLİNÇLİ istisnalar.
KNOWN_UNGATED_NON_READ: dict[str, str] = {
    "github_arac_bul_ve_degerlendir": (
        "karantinaya indirir ve LLM'e analiz ettirir; kodu çalıştırmaz. "
        "tools_kopru._READONLY_TOOLS'ta bilinçli olarak onaysız (plan §3.3)."
    ),
}


# ── Erişim yollarını toplayan tarayıcı (yalnızca okur) ────────────────────

def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None


def _dict_name_values(node: ast.AST) -> list[str]:
    """Bir liste/ifade içindeki {"name": "..."} sözlüklerinin adları."""
    names = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Dict):
            for k, v in zip(sub.keys, sub.values):
                if (isinstance(k, ast.Constant) and k.value == "name"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    names.append(v.value)
    return names


def _dict_keys(node: ast.AST | None) -> list[str]:
    if not isinstance(node, ast.Dict):
        return []
    return [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]


def _module_assign(tree: ast.Module, name: str) -> ast.AST | None:
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in stmt.targets):
            return stmt.value
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) \
                and stmt.target.id == name:
            return stmt.value
    return None


def _find_function(tree: ast.AST, name: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _compared_constants(func: ast.AST | None, var: str) -> list[str]:
    """func içinde `var == "x"` karşılaştırmalarındaki "x" değerleri."""
    out = []
    if func is None:
        return out
    for node in ast.walk(func):
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)
                and node.left.id == var and len(node.ops) == 1
                and isinstance(node.ops[0], ast.Eq)
                and isinstance(node.comparators[0], ast.Constant)
                and isinstance(node.comparators[0].value, str)):
            out.append(node.comparators[0].value)
    return out


# _on_text_command'daki deterministik yönlendiricilerin çağırdığı fonksiyon
# -> araç eşlemesi (main.py, plan §1.2 E4).
_ROUTER_CALLS = {
    "terminal_tool": "terminal",
    "brain_team_tool": "brain_team",
    "agent_loop_tool": "agent_loop",
    "get_file_info": "file_controller",
}


def _router_tools(func: ast.AST | None) -> list[str]:
    out = []
    if func is None:
        return out
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            fn = node.func
            fname = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
            if fname in _ROUTER_CALLS:
                out.append(_ROUTER_CALLS[fname])
            # FILE_MODIFICATION / FILE_ANALYSIS: orch.tasks.create(...) -> Brain görevi
            if (fname == "create" and isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Attribute) and fn.value.attr == "tasks"):
                out.append("brain_team")
        # ALLOWED_TOOLS["windows_system"](...)
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                and node.value.id == "ALLOWED_TOOLS" and isinstance(node.slice, ast.Constant)):
            out.append(node.slice.value)
    return out


def _registry_names() -> list[str]:
    names = []
    for path in sorted(TOOLS_DIR.rglob("*.py")) if TOOLS_DIR.is_dir() else []:
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            fname = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            if fname != "register":
                continue
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    names.append(kw.value.value)
    return names


def collect_reachable_tools() -> dict[str, set[Source]]:
    """Araç adı -> erişilebildiği giriş yolları. Kaynaklar (plan §1-§3):
    main.py TOOL_DECLARATIONS ve _execute_tool dalları (MODEL_LIVE),
    _on_text_command yönlendiricileri (ROUTER), tools/ registry kayıtları
    (MODEL_LIVE + REACT), tools_kopru.ALLOWED_TOOLS (AGENT_LOOP),
    executor_ai._ALLOWED_ACTIONS ve _execute_step ajanları (BRAIN_TEAM),
    core/agent.py TOOLS (CLI_AGENT), eklenti dosyaları (PLUGIN)."""
    reach: dict[str, set[Source]] = {}

    def add(names, source: Source) -> None:
        for n in names:
            reach.setdefault(n, set()).add(source)

    main_tree = _parse(MAIN_PY)
    if main_tree is not None:
        decls = _module_assign(main_tree, "TOOL_DECLARATIONS")
        add(_dict_name_values(decls) if decls is not None else [], Source.MODEL_LIVE)
        add(_compared_constants(_find_function(main_tree, "_execute_tool"), "name"), Source.MODEL_LIVE)
        add(_router_tools(_find_function(main_tree, "_on_text_command")), Source.ROUTER)

    registry = _registry_names()
    add(registry, Source.MODEL_LIVE)
    add(registry, Source.REACT)

    from jarvis.actions.tools_kopru import ALLOWED_TOOLS
    add(list(ALLOWED_TOOLS), Source.AGENT_LOOP)

    exec_tree = _parse(EXECUTOR_PY)
    if exec_tree is not None:
        add(_dict_keys(_module_assign(exec_tree, "_ALLOWED_ACTIONS")), Source.BRAIN_TEAM)
    orch_tree = _parse(ORCHESTRATOR_PY)
    if orch_tree is not None:
        agents = _compared_constants(_find_function(orch_tree, "_execute_step"), "agent")
        add([a for a in agents if a != "executor_ai"], Source.BRAIN_TEAM)

    cli_tree = _parse(CLI_AGENT_PY)
    if cli_tree is not None:
        add(_dict_keys(_module_assign(cli_tree, "TOOLS")), Source.CLI_AGENT)

    if PLUGIN_DIR.is_dir():
        for path in sorted(PLUGIN_DIR.glob("*.py")):
            tree = _parse(path)
            if tree is not None:
                add(_dict_name_values(tree), Source.PLUGIN)

    return reach


def _known(name: str) -> bool:
    return name in EFFECTS or name.startswith(DISCOVERED_PREFIX)


def missing_specs(reachable: dict[str, set[Source]] | None = None) -> dict[str, set[Source]]:
    """Erişilebilir ama spec'i olmayan araçlar (boş olmalı)."""
    reachable = collect_reachable_tools() if reachable is None else reachable
    return {name: srcs for name, srcs in reachable.items() if not _known(name)}


def _make_spec(name: str, sources) -> ToolSpec | None:
    if name in EFFECTS:
        effect, effect_of = EFFECTS[name]
    elif name.startswith(DISCOVERED_PREFIX):
        effect, effect_of = Effect.EXECUTE, None
    else:
        return None
    return ToolSpec(name=name, effect=effect, sources=frozenset(sources), effect_of=effect_of)


def build_specs(reachable: dict[str, set[Source]] | None = None) -> dict[str, ToolSpec]:
    reachable = collect_reachable_tools() if reachable is None else reachable
    specs = {}
    for name, srcs in reachable.items():
        spec = _make_spec(name, srcs)
        if spec is not None:
            specs[name] = spec
    return specs


def spec_for(name: str) -> ToolSpec | None:
    """Tek aracın spec'i (erişim yolları hesaplanmadan). Bilinmeyen -> None."""
    return _make_spec(name, ())


def resolve(tool: str, args: Mapping | None, source: Source) -> ResolvedCall:
    """Çağrıyı bir kez çözer. Modelin gönderdiği confirm_code hiçbir zaman
    çağrının parçası değildir. Bilinmeyen araç en kötü etkiyle (SYSTEM)
    döner - fail-closed."""
    params = {k: v for k, v in dict(args or {}).items() if k != "confirm_code"}
    spec = spec_for(tool)
    effect = spec.effect_for(params) if spec else Effect.SYSTEM
    action = params.get("action")
    if tool == "file_controller":
        from jarvis.actions.file_controller import normalize_action
        action = normalize_action(action)
    return ResolvedCall(tool=tool, action=str(action) if action not in (None, "") else None,
                        params=params, targets=(), effect=effect,
                        fingerprint=fingerprint(tool, params))
