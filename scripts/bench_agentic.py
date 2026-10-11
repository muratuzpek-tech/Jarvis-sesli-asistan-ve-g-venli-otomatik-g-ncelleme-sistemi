#!/usr/bin/env python3
"""Ollama kod modeli olcumu — agentic_coder ayni gorevlerde ne kadar basarili?

Sabit uc gorevi (komut satiri hesaplayici, Tkinter pencereli hesap makinesi,
kucuk metin betigi) her biri --runs kez GERCEK AgenticCoder.solve ile, yalnizca
Ollama kullanarak (Gemini yok) calistirir. Her kosu ayri gecici HOME,
JARVIS_HOME ve proje klasorunde calisir; gercek ~/jarvis_programs ve gercek
Jarvis bellegi DEGISMEZ.

Kullanim:
    OLLAMA_CODER_MODEL=qwen3:14b .venv/bin/python scripts/bench_agentic.py
    OLLAMA_CODER_MODEL=qwen2.5-coder:7b JARVIS_OLLAMA_NUM_CTX=8192 \\
        .venv/bin/python scripts/bench_agentic.py --runs 5 --out sonuc.json

Cikti: model, gorev, basari orani (sonuc metninde "Durum: BAŞARILI"),
ortalama tur, ortalama sure, ret sayisi, en sik hata kategorisi. pytest'e
dahil degildir (tests/test_bench_agentic.py yalnizca mantigi sinar).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.developer import agentic_coder as ac  # noqa: E402

TASKS: tuple[tuple[str, str], ...] = (
    ("cli_hesap",
     "Komut satırından iki sayı ve bir işlem (+, -, *, /) alan basit bir hesaplayıcı yaz; "
     "sıfıra bölmede anlaşılır hata mesajı versin."),
    ("tk_hesap",
     "Tkinter ile pencereli bir hesap makinesi yaz: rakam ve işlem düğmeleri, sonuç ekranı, "
     "temizle (C) düğmesi olsun."),
    ("metin_betik",
     "Bir metin dosyasındaki satır, kelime ve karakter sayısını ve en sık geçen 5 kelimeyi "
     "yazdıran küçük bir Python betiği yaz; dosya yolu komut satırı argümanı olsun."),
)

ModelFactory = Callable[[], Callable[[str], str]]


def check_ollama(model: str, base: str | None = None, timeout: float = 3.0) -> str | None:
    """Ollama erisilebilir ve model kurulu mu? Sorun varsa kullaniciya
    gosterilecek mesaj, yoksa None."""
    import requests
    base = (base or ac._OLLAMA_BASE).rstrip("/")
    try:
        resp = requests.get(f"{base}/api/tags", timeout=timeout)
        resp.raise_for_status()
        installed = {m.get("name", "") for m in resp.json().get("models", [])}
    except Exception as e:
        return (f"Ollama'ya bağlanılamadı ({base}): {type(e).__name__}. "
                "Ollama çalışıyor mu? (ollama serve)")
    wanted = model if ":" in model else f"{model}:latest"
    if wanted not in installed and model not in installed:
        return f"Model kurulu değil: {model}. Önce: ollama pull {model}"
    return None


@contextlib.contextmanager
def _isolated_env(run_dir: Path) -> Iterator[Path]:
    """HOME ve JARVIS_HOME'u run_dir altina al; cikista eski degerleri geri koy."""
    home, jhome = run_dir / "home", run_dir / "jarvis_home"
    home.mkdir(parents=True)
    jhome.mkdir()
    saved = {k: os.environ.get(k) for k in ("HOME", "JARVIS_HOME")}
    os.environ["HOME"], os.environ["JARVIS_HOME"] = str(home), str(jhome)
    try:
        yield home
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _run_once(model: str, task_id: str, description: str, n: int, run_dir: Path,
              model_fn: Callable[[str], str], max_iterations: int) -> dict:
    record = {"model": model, "task": task_id, "run": n, "success": False, "iterations": 0,
              "seconds": 0.0, "rejects": 0, "error_categories": {}, "error": None}
    coder = ac.AgenticCoder(model_fn=model_fn, max_iterations=max_iterations)
    with _isolated_env(run_dir) as home:
        start = time.monotonic()
        try:
            result = asyncio.run(coder.solve(description=description,
                                             project_path=str(home / "jarvis_programs" / task_id)))
            record["success"] = "Durum: BAŞARILI" in result
        except Exception as e:
            record["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        record["seconds"] = round(time.monotonic() - start, 2)
    stats = coder.run_stats() or {}
    record["iterations"] = stats.get("iterations", 0)
    record["rejects"] = stats.get("rejects", 0)
    cats = dict(stats.get("error_categories", {}))
    if record["error"]:
        cats["İSTİSNA"] = cats.get("İSTİSNA", 0) + 1
    record["error_categories"] = cats
    return record


def run_benchmark(model: str, runs: int, model_fn_factory: ModelFactory,
                  tasks: Sequence[tuple[str, str]] = TASKS, workdir: Path | None = None,
                  max_iterations: int = ac.MAX_ITERATIONS,
                  progress: Callable[[str], None] = print) -> list[dict]:
    """Her gorevi runs kez calistirir; her kosu icin bir kayit dondurur.
    workdir verilmezse gecici bir dizin kullanilir ve sonda silinir."""
    own = workdir is None
    base = Path(tempfile.mkdtemp(prefix="bench_agentic_")) if own else Path(workdir)
    base.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    try:
        for task_id, description in tasks:
            for n in range(1, runs + 1):
                progress(f"▶ {model} · {task_id} · koşu {n}/{runs}")
                rec = _run_once(model, task_id, description, n, base / f"{task_id}_{n}",
                                model_fn_factory(), max_iterations)
                progress(f"  {'BAŞARILI' if rec['success'] else 'BAŞARISIZ'} · "
                         f"{rec['iterations']} tur · {rec['seconds']:.1f} sn · {rec['rejects']} ret"
                         + (f" · {rec['error']}" if rec["error"] else ""))
                records.append(rec)
    finally:
        if own:
            shutil.rmtree(base, ignore_errors=True)
    return records


def summarize(records: Sequence[dict]) -> list[dict]:
    """(model, gorev) basina ozet; gorevler ilk gorulme sirasinda."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for rec in records:
        groups.setdefault((rec["model"], rec["task"]), []).append(rec)
    rows = []
    for (model, task), recs in groups.items():
        n = len(recs)
        cats: dict[str, int] = {}
        for rec in recs:
            for cat, count in rec["error_categories"].items():
                cats[cat] = cats.get(cat, 0) + count
        successes = sum(1 for r in recs if r["success"])
        rows.append({
            "model": model,
            "task": task,
            "runs": n,
            "successes": successes,
            "success_rate": successes / n,
            "avg_iterations": sum(r["iterations"] for r in recs) / n,
            "avg_seconds": sum(r["seconds"] for r in recs) / n,
            "rejects": sum(r["rejects"] for r in recs),
            "error_categories": cats,
            # max() esitlikte ilk goruleni verir (dict ekleme sirasi)
            "top_error": max(cats, key=cats.__getitem__) if cats else "-",
        })
    return rows


def format_table(rows: Sequence[dict]) -> str:
    header = ("model", "görev", "başarı", "ort. tur", "ort. süre", "ret", "en sık hata")
    body = [(r["model"], r["task"],
             f"{r['successes']}/{r['runs']} ({r['success_rate']:.0%})",
             f"{r['avg_iterations']:.1f}", f"{r['avg_seconds']:.1f} sn",
             str(r["rejects"]), r["top_error"]) for r in rows]
    widths = [max(len(row[i]) for row in (header, *body)) for i in range(len(header))]

    def fmt(row):
        return "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()

    return "\n".join([fmt(header), "  ".join("-" * w for w in widths), *map(fmt, body)])


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"tam sayı değil: {text}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"en az 1 olmalı: {text}")
    return value


def main(argv: Sequence[str] | None = None, model_fn_factory: ModelFactory | None = None,
         checker: Callable[[str], str | None] = check_ollama) -> int:
    parser = argparse.ArgumentParser(description="agentic_coder Ollama model ölçümü")
    parser.add_argument("--runs", type=_positive_int, default=3, help="görev başına koşu (varsayılan 3)")
    parser.add_argument("--out", type=Path, help="sonuçların yazılacağı JSON dosyası")
    parser.add_argument("--max-iter", type=_positive_int, default=ac.MAX_ITERATIONS,
                        help=f"koşu başına en fazla tur (varsayılan {ac.MAX_ITERATIONS})")
    parser.add_argument("--workdir", type=Path,
                        help="koşu klasörlerinin kökü (verilmezse geçici, sonda silinir)")
    args = parser.parse_args(argv)

    model = ac._pick_ollama_coder_model()
    num_ctx = ac._ollama_num_ctx()
    if model_fn_factory is None:
        problem = checker(model)
        if problem:
            print(f"HATA: {problem}", file=sys.stderr)
            return 2
        model_fn_factory = lambda: ac.ollama_generate  # noqa: E731
    print(f"🧠 ollama {model} ctx={num_ctx} · {len(TASKS)} görev × {args.runs} koşu")

    records = run_benchmark(model, args.runs, model_fn_factory, workdir=args.workdir,
                            max_iterations=args.max_iter)
    rows = summarize(records)
    print()
    print(format_table(rows))
    if args.out:
        payload = {"model": model, "num_ctx": num_ctx, "runs": args.runs,
                   "max_iterations": args.max_iter, "summary": rows, "records": records}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
