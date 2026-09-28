"""experiment_manager.py — SANAL BEYİN DENEY YÖNETİCİSİ (AŞAMA C).

Belgenin §12 (Hipotez Sistemi) ve §39 (Deney Raporu) maddelerinin
uygulanışı. AŞAMA B'nin `VirtualOrchestrator`'ını (tek-adımlık, izinli-
beyin çağrıları: research_ai/security_ai/auditor_ai) kullanarak TAM bir
deney akışını, core/brain_orchestrator.py'nin KENDİ "her çağrıda sadece
bir adım ilerle" desenini (bkz. `_tick()`) tekrar ederek yönetir.

Uygulanan aşama zinciri:

    HYPOTHESIS -> RESEARCH -> AUDIT -> SECURITY -> AWAITING_SANDBOX

NEDEN "AWAITING_SANDBOX"DA DURUYOR (ve daha ileri gitmiyor):
§12'nin tam sırası HYPOTHESIS -> RESEARCH -> PROTOTYPE -> TEST ->
BENCHMARK -> AUDIT -> SECURITY -> PROPOSAL. PROTOTYPE/TEST/BENCHMARK,
izole bir çalışma alanı (AŞAMA D — Sandbox Controller) gerektirir; bu
HENÜZ kurulmadı. Var olmayan bir sandbox'ı taklit edip "test edildi" gibi
görünen SAHTE sonuçlar üretmek belgenin kendi §43 (mutlak güvenlik: hiçbir
deney otomatik olarak production'a dönüşmez, her aşama gerçek kontrolden
geçmeli) ilkesine doğrudan aykırı olurdu. Bu yüzden Experiment Manager,
sandbox kurulana kadar DÜRÜSTÇE "AWAITING_SANDBOX" durumunda durur ve
final_decision alanına bunu açıkça yazar - deneyin olmayan bir sonucunu
olmuş gibi göstermez.

Bu modül, belgenin §11'inde sayılan TAM alan setini deney kaydının
payload'ında tutar: experiment_id, timestamp, objective, hypothesis,
research_sources, current_method, proposed_method, changed_files,
dependencies, commands, network_requests, test_results,
benchmark_results, security_result, auditor_result, approval_status,
final_decision.
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from jarvis.self_improvement.virtual_brain.orchestrator.virtual_orchestrator import VirtualOrchestrator

_HERE = Path(__file__).resolve().parent
HYPOTHESES_PATH = _HERE.parent / "hypotheses" / "hypotheses.jsonl"
REPORTS_DIR = _HERE.parent / "reports"
_hyp_lock = threading.Lock()

STAGES = ("HYPOTHESIS", "RESEARCH", "AUDIT", "SECURITY", "AWAITING_SANDBOX")


def _log_hypothesis(experiment_id: str, objective: str, hypothesis: str) -> None:
    """§12: 'Sanal Beyin yeni geliştirmeleri doğrudan gerçek kabul etmez -
    bu bir HYPOTHESIS olarak kaydedilir.' Bu satır deneyin geri kalanı ne
    olursa olsun DEĞİŞTİRİLMEZ (append-only) - orijinal iddianın kalıcı
    kaydı, §26 ('başarısız denemeler değerlidir, silinmez') ruhuyla
    tutarlı."""
    with _hyp_lock:
        HYPOTHESES_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "experiment_id": experiment_id,
            "timestamp": datetime.now().isoformat(),
            "objective": objective,
            "hypothesis": hypothesis,
        }
        with open(HYPOTHESES_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class ExperimentManager:
    def __init__(self, vo: VirtualOrchestrator | None = None) -> None:
        self.vo = vo or VirtualOrchestrator()

    def start(self, objective: str, hypothesis: str) -> dict:
        if not objective.strip() or not hypothesis.strip():
            raise ValueError("start(): 'objective' ve 'hypothesis' boş olamaz (bkz. §12).")

        exp = self.vo.start_experiment(objective, hypothesis)
        _log_hypothesis(exp["id"], objective, hypothesis)

        payload = exp["payload"]
        payload.update({
            "timestamp": exp["created_at"],
            "stage": "HYPOTHESIS",
            "research_sources": [],
            "current_method": None,
            "proposed_method": hypothesis,
            "changed_files": [],
            "dependencies": [],
            "commands": [],
            "network_requests": [],
            "test_results": None,
            "benchmark_results": None,
            "security_result": None,
            "auditor_result": None,
            "approval_status": "not_yet_requested",
            "final_decision": None,
        })
        self.vo.experiments.update(exp["id"], payload=payload)
        return self.vo.experiments.get(exp["id"])

    def advance(self, experiment_id: str) -> dict:
        """Bir sonraki TEK aşamayı ilerletir (agent_loop/brain_orchestrator
        ile aynı 'her çağrıda sadece bir adım' deseni - hiçbir arka plan
        thread'i başlatmaz, sadece açıkça çağrıldığında ilerler)."""
        exp = self.vo.experiments.get(experiment_id)
        if exp is None:
            raise KeyError(f"'{experiment_id}' id'li deney bulunamadı.")

        stage = exp["payload"].get("stage", "HYPOTHESIS")
        objective = exp["payload"]["objective"]
        hypothesis = exp["payload"]["hypothesis"]

        if stage == "HYPOTHESIS":
            query = f"{objective} — hipotez: {hypothesis}"
            resp = self.vo.run_research_step(experiment_id, query)
            result = resp.get("result", {}) if resp.get("status") == "completed" else {}
            exp = self.vo.experiments.get(experiment_id)
            exp["payload"]["research_sources"] = result.get("sources", [])
            exp["payload"]["stage"] = "RESEARCH"
            self.vo.experiments.update(experiment_id, payload=exp["payload"], status="running")
            return self.vo.experiments.get(experiment_id)

        if stage == "RESEARCH":
            last_research = next(
                (h["response"] for h in reversed(exp["payload"].get("history", []))
                 if h.get("type") == "research"), {},
            )
            resp = self.vo.run_audit(experiment_id, f"Araştırma: {objective}", last_research)
            result = resp.get("result", {}) if resp.get("status") == "completed" else {}
            exp = self.vo.experiments.get(experiment_id)
            exp["payload"]["auditor_result"] = result
            exp["payload"]["stage"] = "AUDIT"
            self.vo.experiments.update(experiment_id, payload=exp["payload"])
            return self.vo.experiments.get(experiment_id)

        if stage == "AUDIT":
            resp = self.vo.run_security_check(
                experiment_id, tool="virtual_brain_hypothesis", action=None,
                target=f"{objective} — {hypothesis}",
            )
            result = resp.get("result", {}) if resp.get("status") == "completed" else {}
            exp = self.vo.experiments.get(experiment_id)
            exp["payload"]["security_result"] = result
            exp["payload"]["stage"] = "SECURITY"
            self.vo.experiments.update(experiment_id, payload=exp["payload"])
            return self.vo.experiments.get(experiment_id)

        if stage == "SECURITY":
            exp["payload"]["stage"] = "AWAITING_SANDBOX"
            exp["payload"]["final_decision"] = (
                "Araştırma + denetim + risk ön-değerlendirmesi tamamlandı. "
                "Prototip/test/benchmark için Sandbox Controller (AŞAMA D) gerekiyor - "
                "henüz kurulmadı, bu yüzden deney burada BEKLEMEDE (fiktif sonuç üretilmedi)."
            )
            self.vo.experiments.update(
                experiment_id, payload=exp["payload"], status="waiting_approval",
            )
            self._write_report(experiment_id)
            return self.vo.experiments.get(experiment_id)

        # AWAITING_SANDBOX (ya da bilinmeyen bir aşama) - ilerletilecek bir şey yok.
        return exp

    def run_to_awaiting_sandbox(self, experiment_id: str, max_steps: int = 10) -> dict:
        """advance()'i, deney AWAITING_SANDBOX'a ulaşana (ya da max_steps
        tükenene) kadar tekrar çağıran bir kolaylık fonksiyonu. Gerçek
        Orchestrator'ın arka plan döngüsünün YERİNE GEÇMEZ - bu paket
        kendi arka plan thread'ini başlatmaz, sadece açıkça çağrılırsa
        çalışır."""
        exp = self.vo.experiments.get(experiment_id)
        for _ in range(max_steps):
            if exp["payload"].get("stage") == "AWAITING_SANDBOX":
                break
            exp = self.advance(experiment_id)
        return exp

    def _write_report(self, experiment_id: str) -> Path:
        """§39 DENEY RAPORU şablonunu doldurup virtual_brain/reports/
        altına <experiment_id>.md olarak yazar."""
        exp = self.vo.experiments.get(experiment_id)
        p = exp["payload"]
        lines = [
            "EXPERIMENT REPORT", "",
            f"Experiment ID: {exp['id']}",
            f"Objective: {p.get('objective', '')}",
            f"Hypothesis: {p.get('hypothesis', '')}", "",
            f"Current Method: {p.get('current_method') or '(belirtilmedi)'}",
            f"Proposed Method: {p.get('proposed_method') or '(belirtilmedi)'}", "",
            f"Research Sources: {p.get('research_sources') or '(yok)'}", "",
            f"Files Changed: {p.get('changed_files') or '(henüz yok - sandbox bekleniyor)'}", "",
            f"Dependencies: {p.get('dependencies') or '(yok)'}", "",
            f"Tests: {p.get('test_results') or '(henüz yok - sandbox bekleniyor)'}", "",
            f"Benchmark: {p.get('benchmark_results') or '(henüz yok - sandbox bekleniyor)'}", "",
            f"Security Result: {p.get('security_result')}", "",
            f"Auditor Result: {p.get('auditor_result')}", "",
            f"Recommendation: {p.get('final_decision') or '(henüz yok)'}", "",
            f"User Approval: {p.get('approval_status')}", "",
        ]
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORTS_DIR / f"{exp['id']}.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path
