"""Python projeleri için modelden BAĞIMSIZ kalite kapısı (dev_agent).

NEDEN (2026-09-28, iki canlı test): dev_agent iki kez "başarılı" dedi ama
  * todo_app: GUI'nin kullandığı save_task/load_tasks/delete_task İÇİ BOŞTU
    (yalnızca docstring + pass / return []), görevler hiç kaydedilmiyordu;
    main.py'de iki ayrı `if __name__ == "__main__"` bloğu vardı.
  * CodeReviewProgram: rapor dosyası yalnızca başlıktan ibaretti; ayrıca
    `with open(...) as file:` bloğu içinde `for file, ... in` döngüsü dosya
    nesnesinin üstüne yazıyordu (ilk bulguda çökerdi).
Program çökmediği ve çıktı dosyası boş olmadığı için eski kontroller bunları
göremiyordu. Buradaki kontroller AST ile, her seferinde aynı sonucu verir ve
dev_agent'ın mevcut {dosya: [bulgu, ...]} biçiminde döner; bulgu varsa proje
"başarılı" SAYILMAZ, _fix_files ile düzeltme turuna girer.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

# Bir çıktı dosyası bundan az "anlamlı" satır içeriyorsa yalnızca başlık sayılır
# (başlık + en az bir gerçek sonuç satırı = 2).
MIN_CONTENT_LINES = 2

_PLACEHOLDER_RE = re.compile(r"\b(placeholder|todo|fixme|not implemented|implement (this|me|later))\b", re.I)
_STUB_DECORATORS = {"abstractmethod", "overload", "abc.abstractmethod", "typing.overload"}


def _issue(code: str, message: str, line: int = 0, col: int = 0) -> dict:
    return {"code": code, "message": message, "line": line, "col": col}


def _decorator_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    names = set()
    for d in fn.decorator_list:
        target = d.func if isinstance(d, ast.Call) else d
        try:
            names.add(ast.unparse(target))
        except Exception:  # noqa: BLE001
            pass
    return names


def _is_trivial_stmt(stmt: ast.stmt) -> bool:
    """pass / ... / return / return None / return [] {} () "" 0 False / raise NotImplementedError."""
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and stmt.value.value is Ellipsis:
        return True
    if isinstance(stmt, ast.Return):
        v = stmt.value
        if v is None:
            return True
        if isinstance(v, ast.Constant) and v.value in (None, "", 0, False):
            return True
        if isinstance(v, (ast.List, ast.Dict, ast.Tuple, ast.Set)) and not getattr(v, "elts", None) and not getattr(v, "keys", None):
            return True
    if isinstance(stmt, ast.Raise) and stmt.exc is not None:
        exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
        if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
            return True
    return False


def _function_body_lines(source_lines: list[str], fn: ast.AST) -> str:
    start, end = fn.lineno - 1, getattr(fn, "end_lineno", fn.lineno)
    return "\n".join(source_lines[start:end])


def find_stub_functions(tree: ast.Module, source: str) -> list[dict]:
    """Gövdesi (docstring hariç) yalnızca önemsiz ifadelerden oluşan fonksiyonlar."""
    lines = source.splitlines()
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _decorator_names(node) & _STUB_DECORATORS:
            continue
        body = list(node.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        text = _function_body_lines(lines, node)
        if body and all(_is_trivial_stmt(s) for s in body):
            hint = " (yer tutucu yorumu var)" if _PLACEHOLDER_RE.search(text) else ""
            found.append(_issue(
                "STUB-FUNCTION",
                f"'{node.name}' fonksiyonunun gövdesi boş/yer tutucu{hint}: gerçek işi yapmıyor. "
                f"Fonksiyonu gerçekten uygula ya da çağıranları gerçek uygulamaya yönlendir.",
                node.lineno, node.col_offset,
            ))
        elif _PLACEHOLDER_RE.search(text) and re.search(r"#.*\b(placeholder|todo|fixme)\b", text, re.I):
            found.append(_issue(
                "PLACEHOLDER-COMMENT",
                f"'{node.name}' içinde yer tutucu/TODO yorumu var; eksik kalan mantığı tamamla.",
                node.lineno, node.col_offset,
            ))
    return found


def _is_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    parts = [node.test.left, *node.test.comparators]
    names = {p.id for p in parts if isinstance(p, ast.Name)}
    consts = {p.value for p in parts if isinstance(p, ast.Constant)}
    return "__name__" in names and "__main__" in consts


def find_duplicate_main_guards(tree: ast.Module) -> list[dict]:
    guards = [n for n in tree.body if _is_main_guard(n)]
    issues = []
    if len(guards) > 1:
        issues.append(_issue(
            "DUPLICATE-MAIN",
            f"Dosyada {len(guards)} ayrı `if __name__ == \"__main__\"` bloğu var (satırlar: "
            f"{', '.join(str(g.lineno) for g in guards)}). İlki çalışınca sonrakiler de çalışır; "
            f"tek bir blokta birleştir.",
            guards[1].lineno,
        ))
    if guards:
        first = guards[0]
        later_defs = [n for n in tree.body if n.lineno > first.lineno
                      and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        if later_defs:
            issues.append(_issue(
                "DEF-AFTER-MAIN",
                f"`if __name__ == \"__main__\"` bloğundan SONRA tanım var ('{later_defs[0].name}'); "
                f"blok çalışırken bu tanım henüz yok. Tanımları bloğun üstüne taşı.",
                later_defs[0].lineno,
            ))
    return issues


def find_with_target_shadowing(tree: ast.Module) -> list[dict]:
    """`with ... as f:` gövdesinde `f`'nin başka bir değerle ezilmesi (ör. for f in ...)."""
    issues = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        names = {item.optional_vars.id for item in node.items if isinstance(item.optional_vars, ast.Name)}
        if not names:
            continue
        for inner in (n for stmt in node.body for n in ast.walk(stmt)):
            targets = []
            if isinstance(inner, (ast.For, ast.AsyncFor, ast.comprehension)):
                targets = [inner.target]
            elif isinstance(inner, ast.Assign):
                targets = inner.targets
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name) and n.id in names and isinstance(n.ctx, ast.Store):
                        issues.append(_issue(
                            "WITH-TARGET-SHADOWED",
                            f"`with ... as {n.id}` ile açılan nesne, blok içinde yeniden atanıyor; "
                            f"sonraki `{n.id}.write(...)` gibi çağrılar yanlış nesneye gider. Değişken adını değiştir.",
                            getattr(n, "lineno", node.lineno),
                        ))
                        names.discard(n.id)
    return issues


def _referenced_names(tree: ast.AST) -> set[str]:
    """Kodda ADIYLA geçen her şey: çağrılar, `self.x` / `obj.x` erişimleri,
    geri çağırma olarak verilmiş adlar (command=self.add_task) ve getattr
    gibi kullanımlar için düz metin sabitleri."""
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            names.add(n.id)
        elif isinstance(n, ast.Attribute):
            names.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            names.add(n.value)
        elif isinstance(n, ast.ImportFrom):
            names.update(a.name for a in n.names)
    return names


def find_unused_definitions(trees: dict[str, ast.Module]) -> dict[str, list[dict]]:
    """Projede tanımlanıp HİÇBİR dosyada adı geçmeyen fonksiyon/metotlar.

    NEDEN (2026-09-28, code_reviewer testi): model istenen iki özelliği
    (check_unused_functions, check_leftover_backup_files) yazmış ama hiçbir
    yerden çağırmamıştı; program çökmediği ve rapor dolu olduğu için
    "başarılı" sayıldı. Yanlış alarmı azaltmak için atlananlar: dunder
    metotlar, main/test_* fonksiyonları, dekoratörlü fonksiyonlar (çerçeve
    tarafından kaydedilir: @app.route, @property ...) ve taban sınıfı olan
    sınıfların metotları (üst sınıf metodunu ezip çerçeve tarafından
    çağrılıyor olabilir: Thread.run, Handler.do_GET ...)."""
    referenced: set[str] = set()
    for tree in trees.values():
        referenced |= _referenced_names(tree)

    result: dict[str, list[dict]] = {}

    def consider(path: str, fn: ast.FunctionDef | ast.AsyncFunctionDef, owner: str | None) -> None:
        name = fn.name
        if (name.startswith("__") and name.endswith("__")) or name == "main" or name.startswith("test"):
            return
        if fn.decorator_list or name in referenced:
            return
        where = f"{owner}.{name}" if owner else name
        result.setdefault(path, []).append(_issue(
            "UNUSED-DEFINITION",
            f"'{where}' tanımlanmış ama projede HİÇBİR yerde çağrılmıyor. İstenen bir özelliği "
            f"uyguluyorsa programın akışına bağla (tercih edilen); gerçekten gereksizse kaldır.",
            fn.lineno, fn.col_offset,
        ))

    for path, tree in trees.items():
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                consider(path, node, None)
            elif isinstance(node, ast.ClassDef):
                has_base = any(not (isinstance(b, ast.Name) and b.id == "object") for b in node.bases)
                if has_base:
                    continue
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        consider(path, item, node.name)
    return result


def analyze_sources(file_codes: dict[str, str]) -> dict[str, list[dict]]:
    """{göreli_yol: kaynak} → {göreli_yol: [bulgu, ...]} (yalnızca .py dosyaları)."""
    result: dict[str, list[dict]] = {}
    trees: dict[str, ast.Module] = {}
    for path, code in file_codes.items():
        if not path.endswith(".py") or not code:
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue  # sözdizimi hatası ruff/çalıştırma adımında zaten yakalanıyor
        trees[path] = tree
        issues = find_stub_functions(tree, code) + find_duplicate_main_guards(tree) + find_with_target_shadowing(tree)
        if issues:
            result[path] = issues
    for path, issues in find_unused_definitions(trees).items():
        result.setdefault(path, []).extend(issues)
    return result


def header_only_outputs(project_dir: Path, expected_outputs: list) -> list[str]:
    """Metin çıktısı yalnızca başlık/ayraçtan oluşuyorsa sorun döndürür.

    Gerçekten "bulunacak bir şey yok" durumu da geçerli bir sonuçtur; ama o
    zaman program bunu AÇIKÇA yazmalı (ör. "Sorun bulunamadı"). Böylece
    boş bir rapor ile "hiç çalışmamış" bir rapor birbirinden ayrılır.
    """
    problems = []
    for item in expected_outputs or []:
        rel = item.get("path") if isinstance(item, dict) else str(item)
        if not rel or Path(rel).suffix.lower() not in {".txt", ".md", ".log", ".csv", ".rst", ""}:
            continue
        target = (project_dir / rel)
        try:
            target.resolve().relative_to(project_dir.resolve())
            text = target.read_text(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue
        meaningful = [ln for ln in text.splitlines() if ln.strip() and not re.fullmatch(r"[\s=\-_*#~.]+", ln)]
        if len(meaningful) < MIN_CONTENT_LINES:
            problems.append(
                f"'{rel}' yalnızca {len(meaningful)} anlamlı satır içeriyor (başlıktan ibaret). "
                f"Program gerçek sonuçları yazmalı; hiç bulgu yoksa bunu açıkça belirten bir satır yazmalı."
            )
    return problems
