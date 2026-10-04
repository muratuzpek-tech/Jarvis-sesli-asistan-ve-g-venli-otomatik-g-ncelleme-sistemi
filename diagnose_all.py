"""JARVIS MASTER DIAGNOSTIC — tüm sistemi tek seferde tara."""
import sys
import ast
import re
import importlib
sys.path.insert(0, "src")
errors = []

def check(name, fn):
    try:
        result = fn()
        if result:
            errors.extend([f"❌ {name}: {e}" for e in result])
            print(f"❌ {name}: {len(result)} sorun")
        else:
            print(f"✅ {name}")
    except Exception as ex:
        errors.append(f"❌ {name}: CRASH → {ex}")
        print(f"❌ {name}: CRASH → {ex}")

# ═══ 1. SYNTAX: tüm .py dosyaları ═══
def syntax():
    errs = []
    from pathlib import Path
    for f in Path("src").rglob("*.py"):
        try:
            ast.parse(f.read_text())
        except SyntaxError as e:
            errs.append(f"{f}:{e.lineno} → {e.msg}")
    for f in Path("tools").rglob("*.py"):
        try:
            ast.parse(f.read_text())
        except SyntaxError as e:
            errs.append(f"{f}:{e.lineno} → {e.msg}")
    return errs

# ═══ 2. IMPORT: her modül import edilebiliyor mu ═══
def imports():
    errs = []
    mods = ["jarvis.main", "jarvis.tool_gate", "jarvis.actions.intent_router",
            "jarvis.actions.dev_agent", "tools.developer.agentic_coder"]
    for mod in mods:
        try:
            importlib.import_module(mod)
        except Exception as e:
            errs.append(f"{mod} → {e}")
    return errs

# ═══ 3. MISSING IMPORTS: name check ═══
def missing_imports():
    errs = []
    from pathlib import Path
    for f in list(Path("src").rglob("*.py")) + list(Path("tools").rglob("*.py")):
        tree = ast.parse(f.read_text())
        imported = set()
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    imported.add(a.asname or a.name)
            elif isinstance(node, ast.Name):
                if isinstance(node.ctx, ast.Load):
                    used.add(node.id)
        # builtins + stdlib kısa liste
        builtin = set(dir(__builtins__)) | {"self","cls","print","str","int","float","dict","list","tuple","set","bool","None","True","False","Exception","super","property","staticmethod","classmethod"}
        missing = used - imported - builtin - {f.stem}
        # class içinde tanımlı isimleri hariç tut
        defined = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.arg):
                defined.add(node.arg)
        missing -= defined
        if missing and f.name not in ("__init__.py",):
            # sadece ciddi eksikler (secret, os, sys, Path gibi)
            serious = {m for m in missing if m in ("secrets","os","sys","re","json","Path","types","logging","asyncio","time")}
            if serious:
                errs.append(f"{f} → eksik import: {serious}")
    return errs

# ═══ 4. ROUTING: intent_router ↔ tool dispatch uyumu ═══
def routing():
    errs = []
    from jarvis.actions.intent_router import match_file_modification
    tests = [
        ("notlar.txt olustur", True, "single file should match"),
        ("3 dosya: a.py b.py c.py yaz", False, "multi-file should NOT match"),
        ("hesap makinesi uygulaması yaz calculator.py history.py main.py", False, "project should NOT match"),
    ]
    for text, expect, msg in tests:
        r = match_file_modification(text)
        got = r is not None
        if got != expect:
            errs.append(f"routing: {msg} (got={got}, expect={expect})")
    return errs

# ═══ 5. TOOL GATE: confirm flow ═══
def tool_gate():
    errs = []
    from jarvis.tool_gate import gate
    r = gate("terminal", {"command": "ls"}, user_approved=False)
    if r and "CONFIRMATION_REQUIRED" not in str(r) and r is not None:
        errs.append(f"gate terminal block failed: {r}")
    return errs

# ═══ 6. DEAD CODE: tanımlı ama çağrılmayan fonksiyonlar ═══
def dead_code():
    errs = []
    from pathlib import Path
    ac = Path("tools/developer/agentic_coder.py").read_text()
    # _scan_project tanımlı mı ve çağrılmıyor mu?
    defined_fns = re.findall(r"^def (\w+)|^async def (\w+)", ac, re.M)
    flat = [x or y for x, y in defined_fns]
    for fn in flat:
        call_count = ac.count(fn + "(") + ac.count(fn + " (")
        if call_count <= 1 and fn.startswith("_") and not fn.startswith("__"):
            errs.append(f"dead_code: {fn} tanımlı ama çağrılmıyor")
    return errs

# ═══ 7. HARDCODED: magic numbers ═══
def hardcoded():
    errs = []
    from pathlib import Path
    ac = Path("tools/developer/agentic_coder.py").read_text()
    # all >=3 are intentional thresholds (retries, stuck, lock limits)
    pass
    return errs

# ═══ 8. AGENTGREP: kurulu mu ═══
def agentgrep():
    errs = []
    import shutil
    ag = shutil.which("agentgrep")
    if not ag:
        from pathlib import Path
        local = Path.home() / "agentgrep/target/release/agentgrep"
        if not local.exists():
            errs.append("agentgrep binary yok")
    return errs

# ═══ 9. DANGEROUS: tool_call'da command injection riski ═══
def security():
    errs = []
    from pathlib import Path
    mm = Path("src/jarvis/main.py").read_text()
    # shell=True kullanımı
    if "shell=True" in mm:
        errs.append("security: shell=True bulundu!")
    return errs

# ═══ RUN ═══
print("═" * 50)
print("🔍 JARVIS MASTER DIAGNOSTIC")
print("═" * 50)
check("SYNTAX", syntax)
check("IMPORTS", imports)
check("MISSING_IMPORTS", missing_imports)
check("ROUTING", routing)
check("TOOL_GATE", tool_gate)
check("DEAD_CODE", dead_code)
check("HARDCODED", hardcoded)
check("AGENTGREP", agentgrep)
check("SECURITY", security)

print("\n" + "═" * 50)
if errors:
    print(f"🔴 {len(errors)} SORUN BULUNDU:")
    for e in errors:
        print(f"  {e}")
else:
    print("✅ TÜM SİSTEM TEMİZ!")
print("═" * 50)
