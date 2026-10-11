#!/usr/bin/env python3
"""scripts/tool_inventory.py — main.py TOOL_DECLARATIONS araç envanteri.

Her beyan edilen araç için (tests/test_tool_consistency.py ile AYNI denetim):

  a) main.py'de dispatch kolu (_execute_tool `name == "x"`) ya da tools/
     registry işleyicisi var mı; kol "Unknown tool"a düşmeden sonuç dönüyor mu
  b) beyandaki parametre adları/tipleri işleyicinin okuduğu anahtarlarla ve
     varsayılan değerlerin/kullanımın tipleriyle uyuşuyor mu
  c) security_gate.EFFECTS kaydı var mı
  d) MODEL_LIVE, AGENT_LOOP, BRAIN_TEAM, REACT için authorize() kararı
     (boş argümanla) ve kaynaklar arası politika farkları
  e) tests/ altında araç adını anan test dosyası sayısı

Kaynaklar yalnızca AST ile okunur (main.py ve araç modülleri import
edilmez/çalıştırılmaz); d) için security_gate.authorize çağrılır. Betik
doğrudan çalıştırıldığında HOME/JARVIS_HOME geçici bir klasöre çevrilir.

Kullanım:  python scripts/tool_inventory.py
"""
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
MAIN_PY = SRC / "jarvis" / "main.py"
TESTS_DIR = ROOT / "tests"
SELF_TEST = "test_tool_consistency.py"

SOURCES = ("MODEL_LIVE", "AGENT_LOOP", "BRAIN_TEAM", "REACT")

# İşleyiciye giden ama beyanda olmaması beklenen anahtarlar: main.py'nin
# kendi eklediği iç bayraklar ve kapının (prepare) eklediği önizleme kodu.
INTERNAL_KEYS = frozenset({"confirm_code", "_user_confirmation_granted"})

_STR_METHODS = frozenset({
    "strip", "lower", "upper", "casefold", "split", "startswith", "endswith",
    "replace", "lstrip", "rstrip", "splitlines", "title", "capitalize",
})


@dataclass
class ToolInfo:
    name: str
    declared: dict[str, str]                       # parametre -> beyan tipi
    required: list[str]
    branch: bool = False                           # _execute_tool kolu
    registry: bool = False                         # tools/ registry kaydı
    dispatch_problem: str | None = None
    handlers: list[str] = field(default_factory=list)       # "modül.fonksiyon"
    used_keys: set[str] = field(default_factory=set)
    loose_keys: set[str] = field(default_factory=set)       # dinamik dispatch varsa modül geneli
    key_types: dict[str, set[str]] = field(default_factory=dict)   # anahtar -> gözlenen tip
    effect: str | None = None
    policies: dict[str, str] = field(default_factory=dict)  # kaynak -> karar (boş argüman)
    variant_policies: dict[str, dict[str, str]] = field(default_factory=dict)
    non_read_allow: list[tuple] = field(default_factory=list)  # (varyant, kaynak, etki, karar)
    reachable_from: set[str] = field(default_factory=set)   # collect_reachable_tools kaynakları
    test_files: list[str] = field(default_factory=list)

    @property
    def declared_unused(self) -> list[str]:
        return sorted(k for k in self.declared
                      if k not in self.used_keys and k not in self.loose_keys)

    @property
    def undeclared_used(self) -> list[str]:
        return sorted(k for k in self.used_keys
                      if k not in self.declared and k not in INTERNAL_KEYS)

    @property
    def type_mismatches(self) -> list[str]:
        out = []
        for key, decl in sorted(self.declared.items()):
            seen = self.key_types.get(key, set())
            want = {"STRING": "str", "INTEGER": "num", "NUMBER": "num",
                    "BOOLEAN": "bool", "ARRAY": "list", "OBJECT": "dict"}.get(decl)
            bad = sorted(t for t in seen if t != want
                         and not (want == "num" and t == "num")
                         # bool 'true'/'false' metni olarak da gelir; str karşılaştırması tolere
                         and not (want == "bool" and t == "str"))
            if bad:
                out.append(f"{key}: beyan {decl}, işleyici {'/'.join(bad)} bekliyor")
        return out

    def param_problems(self) -> list[str]:
        out = [f"beyan edilmiş ama okunmuyor: {k}" for k in self.declared_unused]
        out += [f"okunuyor ama beyan edilmemiş: {k}" for k in self.undeclared_used]
        out += self.type_mismatches
        return out


# ── AST yardımcıları ───────────────────────────────────────────────────────

def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _module_assign(tree: ast.Module, name: str) -> ast.AST | None:
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in stmt.targets):
            return stmt.value
    return None


def _find_function(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def load_declarations() -> list[dict]:
    node = _module_assign(_parse(MAIN_PY), "TOOL_DECLARATIONS")
    if node is None:
        raise RuntimeError("main.py TOOL_DECLARATIONS bulunamadı")
    return ast.literal_eval(node)


def _imported_names(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """main.py'deki `from jarvis.x import f as g` -> {g: ("jarvis.x", "f")}
    (fonksiyon içindeki import'lar dahil)."""
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("jarvis."):
            for alias in node.names:
                out[alias.asname or alias.name] = (node.module, alias.name)
    return out


def _module_path(module: str) -> Path:
    base = SRC.joinpath(*module.split("."))
    return base / "__init__.py" if base.is_dir() else base.with_suffix(".py")


def _is_name_eq(test: ast.AST, var: str) -> str | None:
    if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
            and test.left.id == var and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.comparators[0], ast.Constant)
            and isinstance(test.comparators[0].value, str)):
        return test.comparators[0].value
    return None


def _dispatch_branches(func: ast.AST) -> dict[str, tuple[list[ast.stmt], bool]]:
    """_execute_tool içindeki `name == "x"` kolları -> (gövde, sonuç_dönüyor).

    if/elif zincirindeki (son `else` "Unknown tool") kollar sonucu zincirin
    ardından döner. Zincirin DIŞINDAKİ tek başına bir `if name == "x":` kolu
    kendi içinde return etmezse akış zincire düşer ve "Unknown tool" olur."""
    out: dict[str, tuple[list[ast.stmt], bool]] = {}

    def chain(node: ast.If) -> None:
        cur: ast.AST = node
        while isinstance(cur, ast.If):
            tool = _is_name_eq(cur.test, "name")
            if tool:
                out.setdefault(tool, (cur.body, True))
            cur = cur.orelse[0] if len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If) else None

    def visit(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, ast.If) and _is_name_eq(stmt.test, "name"):
                if stmt.orelse:
                    chain(stmt)
                else:
                    tool = _is_name_eq(stmt.test, "name")
                    returns = any(isinstance(n, ast.Return) for n in ast.walk(stmt))
                    out.setdefault(tool, (stmt.body, returns))
            elif isinstance(stmt, ast.Try):
                visit(stmt.body)

    visit(func.body)
    return out


def _value_type(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, (int, float)):
            return "num"
        if isinstance(v, str):
            return "str"
        return None
    if isinstance(node, (ast.List, ast.ListComp)):
        return "list"
    if isinstance(node, ast.Dict):
        return "dict"
    return None


class _KeyCollector(ast.NodeVisitor):
    """Bir gövdede `alias.get("k", d)`, `alias["k"]`, `"k" in alias`
    kullanımlarını ve (varsayılan değer / doğrudan str metodu çağrısından)
    beklenen tipleri toplar. alias'lar: parametre adı ve ondan türetilen
    `p = parameters or {}` / `dict(parameters)` atamaları."""

    def __init__(self, aliases: set[str]):
        self.aliases = set(aliases)
        self.keys: set[str] = set()
        self.types: dict[str, set[str]] = {}
        self.passed_to: list[tuple[str, int]] = []      # (fonksiyon, argüman sırası)
        self.escaped = False

    def _derived(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.aliases
        if isinstance(node, ast.BoolOp):
            return any(self._derived(v) for v in node.values)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("dict",) and node.args:
            return self._derived(node.args[0])
        if isinstance(node, ast.Dict):              # {**parameters, "k": v}
            return any(k is None and self._derived(v)
                       for k, v in zip(node.keys, node.values, strict=True))
        return False

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._derived(node.value):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.aliases.add(t.id)
        self.generic_visit(node)

    def _key_of(self, node: ast.AST) -> str | None:
        """alias.get("k", ...) ya da alias["k"] ise "k"."""
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and self._derived(node.func.value)
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            return node.args[0].value
        if (isinstance(node, ast.Subscript) and self._derived(node.value)
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
            return node.slice.value
        return None

    def _type(self, key: str, t: str | None) -> None:
        if t:
            self.types.setdefault(key, set()).add(t)

    def visit_Call(self, node: ast.Call) -> None:
        key = self._key_of(node)
        if key is not None:
            self.keys.add(key)
            if len(node.args) > 1:
                self._type(key, _value_type(node.args[1]))
        # alias.get("k", "").strip() -> str bekleniyor
        if isinstance(node.func, ast.Attribute) and node.func.attr in _STR_METHODS:
            inner = self._key_of(node.func.value)
            if inner is not None:
                self._type(inner, "str")
        # Parametre sözlüğü başka bir fonksiyona aynen veriliyorsa izlenir;
        # adı çözülemeyen çağrıya (handler(params) tablosu) giderse "kaçtı".
        fname = node.func.id if isinstance(node.func, ast.Name) else None
        for i, a in enumerate(node.args):
            if self._derived(a):
                if fname:
                    self.passed_to.append((fname, i))
                else:
                    self.escaped = True
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        key = self._key_of(node)
        if key is not None and isinstance(node.ctx, ast.Load):
            self.keys.add(key)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if (isinstance(node.left, ast.Constant) and isinstance(node.left.value, str)
                and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn))
                and self._derived(node.comparators[0])):
            self.keys.add(node.left.value)
        self.generic_visit(node)


def _handler_keys(module_tree: ast.Module, func_name: str, depth: int = 0,
                  arg_index: int = 0, seen: set | None = None) -> _KeyCollector | None:
    seen = set() if seen is None else seen
    if (func_name, arg_index) in seen or depth > 3:
        return None
    seen.add((func_name, arg_index))
    func = _find_function(module_tree, func_name)
    if func is None:
        return None
    params = [a.arg for a in func.args.args]
    if params and params[0] in ("self", "cls"):
        params = params[1:]
    if arg_index >= len(params):
        return None
    collector = _KeyCollector({params[arg_index]})
    collector.visit(func)
    # Aynı modülde parametre sözlüğünü alan yardımcılar da izlenir.
    for callee, idx in list(collector.passed_to):
        if (callee, idx) in seen:
            continue
        sub = _handler_keys(module_tree, callee, depth + 1, idx, seen)
        if sub is None:
            # handler(params) tablosu ya da modül dışı fonksiyon: izlenemez.
            if callee not in ("dict", "len", "str", "print", "repr", "bool", "list"):
                collector.escaped = True
            continue
        collector.keys |= sub.keys
        collector.escaped |= sub.escaped
        for k, ts in sub.types.items():
            collector.types.setdefault(k, set()).update(ts)
    return collector


def _module_loose_keys(tree: ast.Module) -> set[str]:
    """Modüldeki HER fonksiyonun parametreleri (ve lambda parametreleri)
    üzerinde okunan sabit anahtarlar. Sözlük dinamik bir tabloya kaçtığında
    'beyan edilmiş ama okunmuyor' denetimi bu gevşek kümeyle yapılır."""
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            names = {a.arg for a in node.args.args + node.args.kwonlyargs}
            coll = _KeyCollector(names)
            for stmt in (node.body if isinstance(node.body, list) else [node.body]):
                coll.visit(stmt)
            keys |= coll.keys
    return keys


def _branch_handlers(body: list[ast.stmt], imports: dict[str, tuple[str, str]]):
    """Kolun gövdesinde argüman sözlüğüyle çağrılan içe aktarılmış işleyiciler
    ve kolun kendisinin okuduğu anahtarlar."""
    handlers: list[tuple[str, str, int]] = []
    branch = _KeyCollector({"args"})
    for stmt in body:
        branch.visit(stmt)
    for stmt in body:
        for node in ast.walk(stmt):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in imports):
                continue
            mod, fn = imports[node.func.id]
            if any(kw.arg == "parameters" and branch._derived(kw.value) for kw in node.keywords):
                handlers.append((mod, fn, -1))
            for i, a in enumerate(node.args):
                if branch._derived(a):
                    handlers.append((mod, fn, i))
    return handlers, branch


def _agentic_redirect_keys(main_tree: ast.Module, body: list[ast.stmt]) -> set[str]:
    """code_helper/dev_agent -> agentic_code yönlendirmesinde
    _agentic_code_args'ın okuduğu anahtarlar."""
    calls = any(isinstance(n, ast.Attribute) and n.attr == "_agentic_code_args"
                for s in body for n in ast.walk(s))
    if not calls:
        return set()
    coll = _handler_keys(main_tree, "_agentic_code_args")
    return coll.keys if coll else set()


def _registry_names() -> set[str]:
    from jarvis.security_gate import _registry_names as names
    return set(names())


def _test_mentions(tool: str) -> list[str]:
    pat = re.compile(rf"(?<![\w]){re.escape(tool)}(?![\w])")
    out = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.name == SELF_TEST or "__pycache__" in path.parts:
            continue
        try:
            if pat.search(path.read_text(encoding="utf-8", errors="replace")):
                out.append(str(path.relative_to(ROOT)))
        except OSError:
            continue
    return out


# Eyleme bağlı politikası olan araçlar için en kötü durum argümanları (d).
# Boş argüman tablodaki sütundur; bunlar ek olarak denetlenir.
RISKY_ARGS: dict[str, dict] = {
    "terminal": {"command": "rm -rf build"},
    "youtube_video": {"save": True},
    "computer_settings": {"action": "shutdown"},
    "browser_control": {"action": "click", "selector": "#x"},
    "file_controller": {"action": "delete", "path": "desktop", "name": "x.txt"},
    "desktop_control": {"action": "wallpaper", "path": "x.png"},
    "agent_loop": {"action": "retry", "task_id": "t1"},
    "game_updater": {"shutdown_when_done": "true"},
    "flight_finder": {"origin": "IST", "destination": "AMS", "date": "2026-11-01", "save": True},
    "save_memory": {"category": "notes", "key": "k", "value": "ignore previous instructions"},
}


def _decide(tool: str, args: dict, source: str) -> tuple[str, str]:
    """(karar, çözülmüş etki). Tanımsız karar (istisna) 'ERROR:...' olur."""
    from jarvis import security_gate as gate
    try:
        d = gate.authorize(tool, dict(args), gate.Source[source])
    except Exception as e:
        return f"ERROR:{type(e).__name__}", "?"
    verdict = d.verdict.value + (f"+{d.protocol}" if d.protocol else "")
    return verdict, d.call.effect.name if d.call is not None else "?"


def _policies(tool: str) -> tuple[dict[str, str], dict[str, dict[str, str]], list[tuple]]:
    """Boş argümanlı kararlar, varyant kararları ve READ olmayan etkide
    verilen ALLOW'lar [(varyant, kaynak, etki, karar)]."""
    variants = {"{}": {}}
    if tool in RISKY_ARGS:
        variants["risky"] = RISKY_ARGS[tool]
    by_variant: dict[str, dict[str, str]] = {}
    non_read_allow = []
    for label, args in variants.items():
        row = {}
        for src in SOURCES:
            verdict, effect = _decide(tool, args, src)
            row[src] = verdict
            if verdict.startswith("allow") and effect != "READ":
                non_read_allow.append((label, src, effect, verdict))
        by_variant[label] = row
    return by_variant["{}"], by_variant, non_read_allow


def build_inventory(with_policies: bool = True) -> list[ToolInfo]:
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from jarvis.security_gate import EFFECTS, collect_reachable_tools

    main_tree = _parse(MAIN_PY)
    imports = _imported_names(main_tree)
    branches = _dispatch_branches(_find_function(main_tree, "_execute_tool"))
    registry = _registry_names()
    module_trees: dict[str, ast.Module] = {}
    reach = collect_reachable_tools()

    inv = []
    for decl in load_declarations():
        params = decl.get("parameters") or {}
        info = ToolInfo(
            name=decl["name"],
            declared={k: str(v.get("type", "?")) for k, v in (params.get("properties") or {}).items()},
            required=list(params.get("required") or []),
        )
        info.registry = info.name in registry
        info.reachable_from = {s.name for s in reach.get(info.name, ())}
        if info.name in branches:
            body, returns = branches[info.name]
            info.branch = True
            if not returns:
                info.dispatch_problem = ("kol zincir dışında ve return etmiyor: sonuç "
                                         "'Unknown tool' ile eziliyor")
            handlers, branch_keys = _branch_handlers(body, imports)
            info.used_keys |= branch_keys.keys
            for k, ts in branch_keys.types.items():
                info.key_types.setdefault(k, set()).update(ts)
            info.used_keys |= _agentic_redirect_keys(main_tree, body)
            for mod, fn, idx in handlers:
                handler_name = f"{mod.rsplit('.', 1)[-1]}.{fn}"
                info.handlers.append(handler_name)

                # computer_settings delegates only typing actions to
                # computer_control. Do not merge the latter's complete
                # parameter surface into the computer_settings schema:
                # the delegated action is conditional and separately declared.
                if info.name == "computer_settings" and handler_name == "computer_control.computer_control":
                    continue

                tree = module_trees.get(mod)
                if tree is None:
                    tree = module_trees[mod] = _parse(_module_path(mod))
                func = _find_function(tree, fn)
                if func is None:
                    continue
                if idx == -1:
                    names = [a.arg for a in func.args.args + func.args.kwonlyargs]
                    idx = names.index("parameters") if "parameters" in names else 0
                coll = _handler_keys(tree, fn, arg_index=idx)
                if coll is not None:
                    info.used_keys |= coll.keys
                    if coll.escaped:
                        info.loose_keys |= _module_loose_keys(tree)
                    for k, ts in coll.types.items():
                        info.key_types.setdefault(k, set()).update(ts)
        elif not info.registry:
            info.dispatch_problem = "dispatch kolu da registry işleyicisi de yok"
        if info.name in EFFECTS:
            info.effect = EFFECTS[info.name][0].name
        if with_policies:
            info.policies, info.variant_policies, info.non_read_allow = _policies(info.name)
        info.test_files = _test_mentions(info.name)
        inv.append(info)
    return inv


def branches_without_declaration() -> list[str]:
    """_execute_tool'da kolu olan ama TOOL_DECLARATIONS'ta olmayan adlar."""
    decl = {d["name"] for d in load_declarations()}
    branches = _dispatch_branches(_find_function(_parse(MAIN_PY), "_execute_tool"))
    return sorted(set(branches) - decl)


def policy_differences(inv: list[ToolInfo]) -> list[tuple[str, str, dict[str, str]]]:
    """Kaynaklara göre farklı karar verilen (araç, varyant, kararlar)."""
    return [(t.name, label, pol) for t in inv for label, pol in t.variant_policies.items()
            if len(set(pol.values())) > 1]


# ── Çıktı ──────────────────────────────────────────────────────────────────

_SHORT = {"allow": "ALLOW", "needs_approval": "APPROVE", "deny": "DENY"}


def _short(verdict: str) -> str:
    base, _, proto = verdict.partition("+")
    return _SHORT.get(base, base) + (f"({proto})" if proto else "")


def render(inv: list[ToolInfo]) -> str:
    rows = [("araç", "beyan", "işleyici", "etki", *SOURCES, "test")]
    for t in inv:
        handler = ("registry" if t.registry else "") or (
            ", ".join(dict.fromkeys(t.handlers)) or ("inline" if t.branch else "YOK"))
        if t.dispatch_problem:
            handler += " [!]"
        rows.append((t.name, f"{len(t.declared)} param", handler, t.effect or "YOK",
                     *(_short(t.policies.get(s, "-")) for s in SOURCES), str(len(t.test_files))))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = [" | ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows]
    lines.insert(1, "-+-".join("-" * w for w in widths))

    problems = []
    for t in inv:
        if t.dispatch_problem:
            problems.append(f"[a] {t.name}: {t.dispatch_problem}")
        for p in t.param_problems():
            problems.append(f"[b] {t.name}: {p}")
        if t.effect is None:
            problems.append(f"[c] {t.name}: security_gate.EFFECTS kaydı yok")
        for label, pol in t.variant_policies.items():
            for s, v in pol.items():
                if v.startswith("ERROR"):
                    problems.append(f"[d] {t.name}: {s} args={label} authorize hatası {v}")
        if not t.test_files:
            problems.append(f"[e] {t.name}: tests/ altında anan test yok")
    for name in branches_without_declaration():
        problems.append(f"[a] {name}: _execute_tool kolu var ama TOOL_DECLARATIONS'ta yok")

    diffs = [f"  {name} args={label}: " + ", ".join(f"{s}={_short(v)}" for s, v in pol.items())
             for name, label, pol in policy_differences(inv)]
    unreachable = [f"  {t.name}: " + ", ".join(f"{s}={_short(t.policies[s])}" for s in SOURCES
                                               if s not in t.reachable_from and s in t.policies)
                   for t in inv if any(s not in t.reachable_from for s in t.policies)]
    allows = [f"  {t.name} args={label} {src}: etki {eff} -> {_short(v)}"
              for t in inv for label, src, eff, v in t.non_read_allow]
    return "\n".join(lines + ["", f"Uyumsuzluklar ({len(problems)}):"]
                     + [f"  {p}" for p in problems]
                     + ["", f"Kaynaklar arası politika farkları ({len(diffs)}):"] + diffs
                     + ["", f"READ olmayan etkide ALLOW ({len(allows)}):"] + allows
                     + ["", "Aracın erişilemediği kaynakta authorize kararı (boş argüman; "
                            f"kapı erişim denetimini yalnızca MODEL_LIVE'da yapar) ({len(unreachable)}):"]
                     + unreachable)


def main() -> int:
    import os
    import tempfile
    tmp = tempfile.mkdtemp(prefix="tool_inventory_")
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(Path(tmp) / "home")
    os.environ["JARVIS_HOME"] = str(Path(tmp) / "jarvis_home")
    print(render(build_inventory()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
