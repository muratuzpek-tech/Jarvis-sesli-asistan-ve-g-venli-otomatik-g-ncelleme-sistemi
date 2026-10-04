"""
tools/developer/agentic_coder.py — Jarvis 2.0 Agentic Coding Engine

BORAN-SERT'TEN ALINAN (Brain/coding_engine.py):
  - Iteratif yaz→çalıştır→test et→düzelt döngüsü (max 15)
  - "TODO/pass" YASAK → her fonksiyon ÇALIŞIR kod içermeli
  - Multi-file proje desteği (dosya dosya yaz)
  - Structured output (JSON thought/tool/args/response)

SENİN SİSTEME ADAPTE EDİLEN:
  - Gemini API (Ollama yerine → sizin mevcut altyapınız)
  - tools/registry entegrasyonu
  - stdin=DEVNULL güvenlik (input() bloğu)
  - mevcut code_helper._clean_code() markdown extractor

AKIŞ:
    USER: "Hesap makinesi yaz"
      ↓
    1. PLAN     → Ne yapılacak? Hangi dosyalar?
    2. INSPECT  → Hedef dizin mevcut mu?
    3. WRITE    → Dosya 1 yaz (TAM ÇALIŞIR)
    4. RUN      → python3 dosya.py
    5. TEST     → Çıktı doğru mu?
    6. ERROR?   → Hata var mı?
    7. FIX      → Hata düzelt + yeniden yaz
    8. ACCEPT   → Çalışıyor → BİTİR (veya 15 iterasyon → zorla bitir)
"""
from __future__ import annotations

import ast
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("tools.developer.agentic")

# ── Sabitler ──────────────────────────────────────────────────
MAX_ITERATIONS = 25
_MAX_OUTPUT_CHARS = 2000
_RUN_TIMEOUT = 30

_FORBIDDEN_PATTERNS = [
    (re.compile(r"\bpass\b(?!\w)", re.MULTILINE), "pass statement"),
    (re.compile(r"#\s*(TODO|FIXME|XXX|buraya|burayı|doldur)", re.IGNORECASE), "placeholder comment"),
    (re.compile(r"^\s*\.\.\.\s*$", re.MULTILINE), "ellipsis stub"),
    (re.compile(r"NotImplementedError"), "NotImplementedError stub"),
]


@dataclass
class CodingTask:
    """Bir kodlama görevinin durumu."""
    description: str
    language: str = "python"
    project_path: Path = field(default_factory=lambda: Path.cwd())
    files_written: dict[str, str] = field(default_factory=dict)  # rel_path → content
    last_written_file: str = ""
    same_file_writes: int = 0
    expected_files: list[str] = field(default_factory=list)
    last_content_hash: str = ""
    stuck_count: int = 0
    last_content_hash: str = ""
    stuck_count: int = 0
    iterations: int = 0
    errors: list[str] = field(default_factory=list)
    rewrite_counts: dict = field(default_factory=dict)
    status: str = "running"
    accepted: bool = False
    final_response: str = ""


@dataclass
class CodingStep:
    """Tek bir iterasyon adımı."""
    step_num: int
    thought: str = ""
    action: str = ""      # write | run | inspect | fix | accept
    detail: str = ""
    success: bool = False


# ── System Prompt ─────────────────────────────────────────────

_SYSTEM_PROMPT = """Sen Jarvis'in Kıdemli Baş Yazılım Mühendisisisin.

GÖREVİN: Kullanıcının kodlama isteklerini çöz, TAM ÇALIŞAN KOD yaz.

ASLA YAPMA:
- `pass` yazma
- `# TODO`, `# buraya yazın` gibi placeholder yorum yazma
- İskellet/stub/boş fonksiyon yazma
- `...` ile kod kısaltma
- Fonksiyonları "geri kalanı aynı" diye geçiştirme

HER FONKSİYON ÇALIŞIR KOD İÇERMELİ. `python3 dosya.py` ile HATASIZ çalışmalı.

ÇOK DOSYALI PROJELER:
- Her adımda tek dosyanın TAM ve ÇALIŞIR kodunu yaz
- Dosyalar arası import'ları doğru yaz
- TÜM dosyaları YAZDIĞINDAN EMİN OL!
- Her dosyayı ayrı ayrı yaz: ana dosya, yardımcı modül, config vb.
- ANA DOSYAYI (main.py / target_filename) ATLA!
- Üretilen dosya sayısı = istenen dosya sayısı eşleşmeden ACCEPT yapma.

ÇIKTI FORMATI (SADECE JSON):
{
  "thought": "Ne yapıyorum ve neden",
  "action": "write | run | inspect | fix | accept",
  "args": {
    "filename": "dosya_adı.py",
    "content": "tam kod (sadece write/fix için)",
    "command": "komut (sadece run için)",
    "fix_note": "düzeltme açıklaması (sadece fix için)"
  },
  "response": "Sadece accept ise final özet"
}
"""


# ── Yardımcı Fonksiyonlar ────────────────────────────────────

def _validate_code(code: str, language: str = "python") -> tuple[bool, str]:
    """Kodda yasaklı kalıp var mı kontrol et."""
    for pattern, label in _FORBIDDEN_PATTERNS:
        if pattern.search(code):
            return False, f"YASAK: {label} bulundu"

    # Python syntax kontrolü
    if language == "python":
        try:
            ast.parse(code)
        except SyntaxError as e:
            return False, f"SYNTAX_ERROR: line {e.lineno}: {e.msg}"

    return True, "OK"


def _run_file(path: Path, timeout: int = _RUN_TIMEOUT) -> str:
    """Dosyayı çalıştır ve çıktıyı döndür (stdin=DEVNULL güvenli)."""
    interpreters = {
        ".py":  [sys.executable],
        ".js":  ["node"],
        ".ts":  ["ts-node"],
        ".sh":  ["bash"],
        ".rb":  ["ruby"],
        ".php": ["php"],
    }
    interp = interpreters.get(path.suffix.lower())
    if not interp:
        return f"No interpreter for {path.suffix}"

    try:
        result = subprocess.run(
            interp + [str(path)],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
            encoding="utf-8", errors="replace",
            timeout=timeout,
            cwd=str(path.parent),
        )
        parts = []
        if result.stdout.strip():
            parts.append(f"STDOUT:\n{result.stdout.strip()[:_MAX_OUTPUT_CHARS]}")
        if result.stderr.strip():
            parts.append(f"STDERR:\n{result.stderr.strip()[:_MAX_OUTPUT_CHARS]}")
        status = "SUCCESS" if result.returncode == 0 else f"FAIL(rc={result.returncode})"
        return f"[{status}] " + ("\n".join(parts) if parts else "(no output)")
    except subprocess.TimeoutExpired:
        return f"[TIMEOUT] {timeout}sn aşıldı"
    except Exception as e:
        return f"[ERROR] {type(e).__name__}: {e}"


def _parse_model_response(raw: str) -> dict:
    """Model çıktısından JSON çıkar (farklı formatlara dayanıklı)."""
    raw = raw.strip()

    # 1. Direkt JSON
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2. Markdown code block içinde
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", raw, re.DOTALL)
    for block in blocks:
        try:
            return json.loads(block.strip())
        except json.JSONDecodeError:
            continue

    # 3. İlk { ... } bloğunu yakala
    brace_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group())
        except json.JSONDecodeError:
            pass

    return {}


# ── Agentic Coding Engine ─────────────────────────────────────

class AgenticCoder:
    """
    Iteratif kod yazma motoru.

    Döngü:
        think → action(write/run/inspect/fix/accept) → observe → repeat

    Güvenlik:
        - MAX_ITERATIONS = 15 → sonsuz döngü koruması
        - _validate_code → TODO/pass yasak
        - stdin=DEVNULL → input() bloğu yok
    """

    def __init__(
        self,
        model_fn: Callable[[str], str] | None = None,
        ui: Any = None,
        max_iterations: int = MAX_ITERATIONS,
    ):
        """
        Args:
            model_fn: LLM prompt → raw text fonksiyonu.
                      Varsayılan: Gemini (eğer kullanılabilirse)
            ui:        JarvisUI (opsiyonel, progress göstermek için)
            max_iterations: Maksimum döngü sayısı
        """
        self._model_fn = model_fn or self._default_model
        self._ui = ui
        self._max = max_iterations

    @staticmethod
    def _default_model(prompt: str) -> str:
        """
        Varsayılan LLM: Gemini API.
        Kullanılamazsa Ollama fallback.
        """
        # 1. Gemini dene (CRITICAL FIX: API key kontrolü ÖNCE, client leak önleme)
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.info("[Coder] GEMINI_API_KEY yok → Ollama")
        else:
            try:
                from google import genai
                client = genai.Client(api_key=api_key)
                try:
                    resp = client.models.generate_content(
                        model="gemini-2.0-flash",
                        contents=prompt,
                    )
                    return resp.text or ""
                finally:
                    # CRITICAL FIX: Client async resource leak prevention
                    try:
                        if hasattr(client, '_async_httpx_client'):
                            import asyncio as _aio
                            _aio.get_event_loop().run_until_complete(
                                client._async_httpx_client.aclose()
                            )
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"[Coder] Gemini hatası ({type(e).__name__}) → Ollama")

        # 2. Ollama fallback
        try:
            import ollama
            resp = ollama.chat(
                model=os.environ.get("OLLAMA_CODER_MODEL", "qwen2.5:7b"),
                messages=[{"role": "user", "content": prompt}],
                format="json",
                options={"temperature": 0.2},
            )
            return resp.get("message", {}).get("content", "")
        except Exception as e:
            logger.warning(f"[Coder] Ollama da yok ({type(e).__name__})")
            return json.dumps({
                "thought": "LLM bulunamadı",
                "action": "accept",
                "args": {},
                "response": "HATA: Kullanılabilir LLM yok (Gemini/Ollama)",
            })

    async def solve(
        self,
        description: str,
        language: str = "python",
        project_path: str | None = None,
        target_filename: str | None = None,
    ) -> str:
        """
        Kodlama görevini çöz (iteratif döngü).

        Args:
            description:     Ne yapılacağı ("Hesap makinesi yaz")
            language:        Programlama dili
            project_path:    Hedef dizin (varsayılan: ~/Desktop/jarvis_code)
            target_filename: Ana dosya adı (otomatik belirlenebilir)

        Returns:
            str: Final özet + yapılan işlerin listesi
        """
        task = CodingTask(
            description=description,
            language=language,
            project_path=Path(project_path) if project_path
                else _auto_project_dir(description),
        )
        task.project_path.mkdir(parents=True, exist_ok=True)

        # Auto-parse expected files from description
        import re as _re
        _fn_pattern = _re.findall(r"(?:[\w]+\.py|[\w]+\.js|[\w]+\.ts|[\w]+\.html|[\w]+\.css|[\w]+\.json|[\w]+\.txt)", description)
        task.expected_files = list(dict.fromkeys(_fn_pattern))  # unique, preserve order
        if target_filename and target_filename not in task.expected_files:
            task.expected_files.append(target_filename)
        if not task.expected_files:
            task.expected_files = [f"main.{language}"]
        self._ui_progress(f"  [PLAN] Beklenen dosyalar: {task.expected_files}")

        steps: list[CodingStep] = []
        last_run_output = ""
        last_error = ""

        description = str(description)  # HIGH FIX: type coercion
        self._ui_progress(f"🔨 Agentic coding başlıyor: {description[:60]}")

        for i in range(self._max):
            task.iterations = i + 1
            self._ui_progress(f"  ⚙️ Iterasyon {task.iterations}/{self._max}")

            # ── LLM'e sorma ────────────────────────────────────
            prompt = self._build_prompt(task, steps, last_run_output, last_error, target_filename)
            raw = self._model_fn(prompt)
            decision = _parse_model_response(raw)

            if not decision:
                self._ui_progress(f"  ⚠️ JSON parse başarısız → accept")
                task.final_response = f"Kod oluşturuldu ({task.iterations} iterasyon). JSON parse hatası."
                break

            thought = decision.get("thought", "")
            action = decision.get("action", "accept").lower()
            args = decision.get("args", {})
            response = decision.get("response", "")

            step = CodingStep(step_num=i + 1, thought=thought, action=action)

            # ═══ SMART EXIT: tum dosyalar yazildi + compile temiz → accept ═══
            _all_clean = len(task.files_written) >= 3
            if _all_clean:
                for _fn, _fc in task.files_written.items():
                    try:
                        compile(_fc, _fn, "exec")
                    except SyntaxError:
                        _all_clean = False
                        break
            if _all_clean and task.iterations >= 3:
                task.accepted = True
                task.final_response = f"Tum dosyalar yazildi ve temiz: {sorted(task.files_written.keys())}"
                steps.append(CodingStep(step_num=i+1, thought="auto-accept: all files valid", action="accept", detail="SMART_EXIT", success=True))
                break


            # ── ACTION: write / fix ────────────────────────────
            if action in ("write", "fix"):
                _fn_target = args.get("filename", "")
                if _fn_target:
                    task.rewrite_counts[_fn_target] = task.rewrite_counts.get(_fn_target, 0) + 1
                    if task.rewrite_counts[_fn_target] > 3 and _fn_target in task.files_written:
                        step.detail = f"SKIP: {_fn_target} zaten 3+ kez yazildi (locked)"
                        step.success = True
                        steps.append(step)
                        continue
            if action in ("write", "fix"):
                filename = args.get("filename") or target_filename or f"main.{task.language}"
                content = args.get("content", "")

                # Same file write tracking + force rotate
                if filename == task.last_written_file:
                    task.same_file_writes += 1
                    if task.same_file_writes >= 3:
                        task.same_file_writes = 0
                        # Force next unwritten file
                        for exp_f in task.expected_files:
                            if exp_f not in task.files_written:
                                filename = exp_f
                                last_error = f"ZORLA ROTATE: {task.last_written_file} dosyasina 3 kez yazdin. Simdi MUTLAKA {filename} yaz."
                                step.detail = f"ROTATE -> {filename}"
                                break
                else:
                    task.same_file_writes = 0
                task.last_written_file = filename

                if not content or len(content.strip()) < 100:
                    step.detail = f"COK KISA kod ({len(content)} char) — en az 50 gerekli, REDDEDILDI"
                    step.success = False
                    steps.append(step)
                    last_error = f"CONTENT_TOO_SHORT: {len(content)} char kod yazdin. En az 200 karakter dolu, calisan kod yaz."
                    continue

                valid, val_msg = _validate_code(content, task.language)
                if not valid:
                    task.errors.append(f"Iteration {i+1}: {val_msg}")
                    last_error = f"VALIDATION: {val_msg}"
                    step.detail = f"REDDEDİLDİ: {val_msg}"
                    step.success = False
                    steps.append(step)
                    continue

                fpath = task.project_path / filename
                fpath.parent.mkdir(parents=True, exist_ok=True)
                fpath.write_text(content, encoding="utf-8")
                # Stuck detection: same content repeatedly
                _ch = str(hash(content))[:8]
                if _ch == task.last_content_hash:
                    task.stuck_count += 1
                    if task.stuck_count >= 3:
                        last_error = f"STUCK: Ayni kodu 3 kez urettin! DAHA FAZLA KOD, daha detayli yaz. {filename} icin en az 300 karakter dolu fonksiyon yaz."
                        step.detail = f"🔄 STUCK ({task.stuck_count}x ayni icerik) — DETAYLI yazmalisin"
                        step.success = False
                        steps.append(step)
                        continue
                else:
                    task.stuck_count = 0
                task.last_content_hash = _ch

                _ch = str(hash(content))[:8]
                if _ch == task.last_content_hash:
                    task.stuck_count += 1
                    if task.stuck_count >= 3:
                        last_error = f"STUCK: Ayni kodu {task.stuck_count}x urettin! DETAYLI yaz — en az 300 karakter, fonksiyonlar dolu olsun."
                        step.detail = f"STUCK ({task.stuck_count}x) — detayli yaz"
                        step.success = False
                        steps.append(step)
                        continue
                else:
                    task.stuck_count = 0
                task.last_content_hash = _ch

                task.files_written[filename] = content
                step.detail = f"📝 {filename} ({len(content)} chars)"
                step.success = True
                steps.append(step)
                self._ui_progress(f"    📝 {filename} yazıldı ({len(content):,} chars)")

            # ── ACTION: run ────────────────────────────────────
            elif action == "run":
                cmd_target = args.get("command", "") or args.get("filename", "")
                if cmd_target:
                    fpath = task.project_path / cmd_target.split()[-1]
                    if fpath.exists():
                        _fsz = fpath.stat().st_size
                        if _fsz < 200:
                            last_error = f"KUCUK DOSYA: {fpath.name} sadece {_fsz} bytes. Daha fazla kod yaz — fonksiyonlar, class, mantik ekle!"
                        last_run_output = _run_file(fpath)
                        last_error = ""
                        step.detail = last_run_output[:200]
                        step.success = "[SUCCESS]" in last_run_output
                        steps.append(step)
                        self._ui_progress(f"    ▶️  {fpath.name}: {'✅' if step.success else '❌'}")
                    else:
                        step.detail = f"Dosya yok: {cmd_target}"
                        step.success = False
                        steps.append(step)
                else:
                    step.detail = "Çalıştırılacak dosya belirtilmedi"
                    step.success = False
                    steps.append(step)

            # ── ACTION: inspect ────────────────────────────────
            elif action == "inspect":
                target = args.get("filename", "")
                fpath = task.project_path / target
                if fpath.exists():
                    content = fpath.read_text(encoding="utf-8")
                    last_run_output = f"FILE: {target}\n{content[:_MAX_OUTPUT_CHARS]}"
                    step.detail = f"👁️ {target} okundu"
                else:
                    listing = [p.name for p in task.project_path.iterdir()]
                    last_run_output = f"DIR: {task.project_path}\nFiles: {listing}"
                    step.detail = f"👁️ Dizin listelendi"
                step.success = True
                steps.append(step)

            # ── ACTION: accept ─────────────────────────────────
            elif action == "accept":
                step.detail = f"✅ ACCEPT: {response[:100]}"
                step.success = True
                steps.append(step)
                task.accepted = True
                task.final_response = response or (
                    f"Tamamlandı. {len(task.files_written)} dosya yazıldı, "
                    f"{task.iterations} iterasyon."
                )
                break

            else:
                step.detail = f"Bilinmeyen action: {action}"
                step.success = False
                steps.append(step)

        # ── Final (accept olmasa bile) ─────────────────────────
        if not task.accepted:
            task.final_response = (
                f"Kod tamamlandı (max {self._max} iterasyon). "
                f"{len(task.files_written)} dosya yazıldı. "
                f"⚠️ Otomatik accept yapılmadı — Manuel kontrol önerilir."
            )

        summary = self._build_summary(task, steps)
        self._ui_progress(f"  🏁 Tamamlandı: {len(task.files_written)} dosya, {task.iterations} iterasyon")
        _acc_err = []
        for _fn, _fc in task.files_written.items():
            try:
                compile(_fc, _fn, "exec")
            except SyntaxError as _se:
                _acc_err.append(f"{_fn}: line {_se.lineno}: {_se.msg}")
            if len(_fc) < 150:
                _acc_err.append(f"{_fn}: too short ({len(_fc)} chars)")
        if _acc_err:
            task.accepted = False
            task.errors.extend(_acc_err[:3])
            task.final_response = f"HATA: {'; '.join(_acc_err[:3])}"
        try:
            import sys as _s
            _s.path.insert(0, str(Path(__file__).parent.parent / "src"))
            from jarvis.path_utils import register_project
            register_project(name=task.project_path.name, root=task.project_path, entry="main.py", status="accepted" if task.accepted else "needs_fix")
        except Exception:
            pass
        return summary

    # ── Prompt Builder ─────────────────────────────────────────

    def _build_prompt(
        self,
        task: CodingTask,
        steps: list[CodingStep],
        last_run_output: str,
        last_error: str,
        target_filename: str | None = None,
    ) -> str:
        """Durum → LLM prompt."""
        parts = [_SYSTEM_PROMPT]

        parts.append(f"\n## GÖREV\n{task.description}")
        parts.append(f"\n## DİL: {task.language}")
        parts.append(f"## PROJE YOLU: {task.project_path}")
        if target_filename:
            parts.append(f"## ANA DOSYA: {target_filename}")

        if task.files_written:
            parts.append("\n## YAZILAN DOSYALAR:")
            for fname in list(task.files_written.keys()):
                parts.append(f"- {fname}")

        if steps:
            recent = steps[-5:]  # Son 5 adım
            parts.append("\n## SON ADIMLAR:")
            for s in recent:
                parts.append(f"  {s.step_num}. [{s.action}] {s.thought[:80]} → {s.detail[:80]} ({'OK' if s.success else 'FAIL'})")

        if last_run_output:
            parts.append(f"\n## SON ÇALIŞTIRMA ÇIKTISI:\n{last_run_output[:1500]}")

        if last_error:
            parts.append(f"\n## HATA:\n{last_error}")

        _missing = [f for f in task.expected_files if f not in task.files_written]
        _written_names = list(task.files_written.keys())
        parts.append("\n## DOSYA PLANI (TUMU yazilmali!)")
        parts.append(f"  Yazilan: {_written_names if _written_names else 'hicbir sey yok'}")
        parts.append(f"  EKSIK: {_missing if _missing else 'tumu yazildi!'}")
        if _missing:
            parts.append(f"  -> SIMDI MUTLAKA {_missing[0]} dosyasini yaz. AYNI dosyaya tekrar yazma!")
            parts.append("  -> TUM dosyalar bitmeden ACCEPT yapma!")
        else:
            parts.append("  -> Tum dosyalar yazildi. Run et, test et, sonra ACCEPT.")
        _wr = list(task.files_written.keys())
        if _wr:
            _lc = task.files_written[_wr[-1]]
            parts.append(_lc[:300])
            parts.append('ONCEKI DOSYA YUKARIDA. AYNISINI TEKRAR YAZMA!')
            parts.append('models.py=sadece sinif/dataclass. storage.py=dosya I/O JSON. cli.py=input/print/menu. analyzer.py=hesaplama/rapor. main.py=import+baglama')
            parts.append('SIMDI MUTLAKA FARKLI dosya yaz.')
        parts.append("\n## SIMDI NE YAPMALISIN?")
        if not task.files_written:
            parts.append("→ İlk dosyayı yaz (action: write)")
        elif last_error:
            parts.append(f"→ Hatayı düzelt (action: fix) veya yeniden yaz (action: write)")
        elif not last_run_output:
            parts.append("→ Çalıştır (action: run)")
        elif "[SUCCESS]" in last_run_output:
            parts.append("→ Çalışıyor! Accept yap (action: accept) veya başka dosya gerekiyorsa yaz")
        else:
            parts.append("→ Hata var → düzelt (action: fix)")

        parts.append('\nSADECE JSON çıktısı ver.')

        return "\n".join(parts)

    # ── Summary ────────────────────────────────────────────────

    def _build_summary(self, task: CodingTask, steps: list[CodingStep]) -> str:
        lines = [
            f"🔧 AGENTIC CODING — {task.description}",
            f"📂 Konum: {task.project_path}",
            "Dosyalar:\n" + "\n".join(
                f"  ✅ {task.project_path / f} ({len(c)} karakter)"
                for f, c in sorted(task.files_written.items())
            ),
            f"▶️ Çalıştırma: cd {task.project_path} && python3 main.py",
            f"📊 Durum: {'tamamlandi' if task.accepted else 'hatali'}",
            "\n".join(
                f"  - {task.project_path / fname} ({len(c)} karakter)"
                for fname, c in sorted(task.files_written.items())
            ),
            f"▶️ Çalıştır: python3 {task.project_path / 'main.py'}",
            f"📊 {task.iterations} iterasyon, {len(task.files_written)} dosya, "
            f"{len(task.errors)} hata, {'ACCEPTED ✅' if task.accepted else 'NOT ACCEPTED ⚠️'}",
            "",
        ]

        if task.files_written:
            lines.append("📝 Yazılan dosyalar:")
            for fname in task.files_written:
                fpath = task.project_path / fname
                size = fpath.stat().st_size if fpath.exists() else 0
                lines.append(f"  • {fname} ({size:,} bytes)")

        lines.append("")
        lines.append(f"💬 {task.final_response}")
        return "\n".join(lines)

    def _ui_progress(self, msg: str) -> None:
        """UI'a ilerleme yaz (opsiyonel)."""
        logger.info(msg)
        if self._ui:
            try:
                self._ui.write_log(msg)
            except Exception:
                pass


# ── Kolay Kullanım Fonksiyonu ────────────────────────────────

def _auto_project_dir(description: str) -> Path:
    """Her proje icin ayri alt klasor: ~/jarvis_programs/<slug>/"""
    import re
    from datetime import datetime
    base = Path.home() / "jarvis_programs"
    tr = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    text = (description or "").translate(tr).lower()
    stop = {"bir","ile","yaz","olsun","icin","ve","veya","dosya","dosyalardan",
            "dosyalari","kaliteli","calisan","kod","yazilsin","gelistirme",
            "the","for","and","with","app","application","create","build",
            "python","projesi","proje","uygulama","olustur","yap","tam"}
    words = [w for w in re.findall(r"[a-z0-9]+", text) if len(w) >= 3 and w not in stop]
    name = "_".join(words[:3]) if words else f"proje_{datetime.now().strftime('%Y%m%d_%H%M')}"
    target = base / name
    n = 2
    while target.exists() and any(target.iterdir()):
        target = base / f"{name}_{n}"
        n += 1
    return target


async def agentic_solve(
    description: str,
    language: str = "python",
    project_path: str | None = None,
    target_filename: str | None = None,
    ui: Any = None,
    model_fn: Callable[[str], str] | None = None,
) -> str:
    """
    Tek satırda agentic kodlama.

    Usage:
        result = await agentic_solve("Basit hesap makinesi yaz")
    """
    coder = AgenticCoder(model_fn=model_fn, ui=ui)
    return await coder.solve(
        description=description,
        language=language,
        project_path=project_path,
        target_filename=target_filename,
    )
