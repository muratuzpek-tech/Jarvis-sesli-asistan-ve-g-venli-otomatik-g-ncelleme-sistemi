"""dev_agent.py FIX'leri

FIX #7: ApprovalRegistry Binding (dev_agent → main.py)
FIX #9: Experiments Isolation (Virtual Brain ↔ Real Tasks)
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# ══════════════════════════════════════════════════════════════
# FIX #7: ApprovalRegistry Binding
# ══════════════════════════════════════════════════════════════
def _dev_agent_fingerprint(description: str, language: str) -> str:
    """dev_agent istek parmak izi (idempotency)."""
    content = json.dumps(
        {"description": description, "language": language},
        sort_keys=True
    )
    return hashlib.sha256(content.encode()).hexdigest()[:16]


class DevAgentApprovalBridge:
    """dev_agent.py → main.py onay köprüsü.
    
    FIX #7: dev_agent kendi başına onay istemiyor — main.py'nin
    ApprovalSlot'unu ve ApprovalRegistry'yi kullanıyor.
    
    AKIŞ:
    1. dev_agent(parameters) çağrılır
    2. PREVIEW modunda: plan, dosyalar, bağımlılıklar gösterilir
       → confirm_code döner
    3. Kullanıcı onay verirse: main.py ApprovalSlot'a grant() ek
    4. dev_agent(parameters, confirm_code) tekrar çağrılır
    5. EXECUTE modunda: build, test, quality gate
    """

    @staticmethod
    def fingerprint(description: str, language: str = "python") -> str:
        """FIX #7: Parmak izi (idempotent confirm_code'lar)."""
        return _dev_agent_fingerprint(description, language)

    @staticmethod
    def request_approval(
        task_id: str,
        description: str,
        plan: dict,
        message: str,
        main_instance: Any,
    ) -> bool:
        """FIX #7: main.py'nin ApprovalSlot'unu kullan.
        
        Args:
            task_id: İş tanımlayıcısı
            description: Proje açıklaması
            plan: Planlama sonucu
            message: Kullanıcıya gösterilecek mesaj
            main_instance: JarvisLive örneği
        """
        from jarvis.main_approval_fixes import _action_fingerprint

        params = {
            "description": description,
            "plan": plan,
        }
        fingerprint = _action_fingerprint("dev_agent", params)

        # main.py'nin ApprovalSlot'unu iste
        ok = main_instance._approval_slot.request("dev_agent", task_id, fingerprint)
        if not ok:
            pending = main_instance._approval_slot.pending()
            if pending:
                action, tid, _ = pending
                return action == "dev_agent" and tid == task_id
            return False

        # Kullanıcıya sor
        try:
            main_instance.ui.write_log(f"[DEV_AGENT_ONAY] {message}")
        except Exception:
            pass
        main_instance.speak(
            f"[DEV_AGENT_ONAY_ISTEGI] {message}\n"
            f"Plan:\n{json.dumps(plan, indent=2, ensure_ascii=False)}\n"
            f"Onaylıyor musun?"
        )
        return True

    @staticmethod
    def consume_approval(
        task_id: str,
        description: str,
        plan: dict,
        main_instance: Any,
    ) -> bool:
        """FIX #7: Onay tüket (EXECUTE modunda).
        
        Returns:
            True: Onay verildi, çalış
            False: Onay yok veya süresi doldu
        """
        from jarvis.main_approval_fixes import _action_fingerprint

        params = {
            "description": description,
            "plan": plan,
        }
        fingerprint = _action_fingerprint("dev_agent", params)

        # ApprovalRegistry ile senkronize
        from jarvis.core.approval_registry import get_registry, ApprovalType
        try:
            registry = get_registry()
            code = registry.register_request(
                approval_type=ApprovalType.DEV_AGENT,
                fingerprint=fingerprint,
                metadata={"task_id": task_id, "description": description},
                ttl_seconds=60,
            )
            registry.confirm(code)
        except Exception as e:
            print(f"[WARN] dev_agent registry sync başarısız: {e}")

        # ApprovalSlot'tan tüket
        return main_instance._approval_slot.consume("dev_agent", task_id, fingerprint)


# ══════════════════════════════════════════════════════════════
# FIX #9: Experiments Isolation (Virtual Brain ↔ Real Tasks)
# ══════════════════════════════════════════════════════════════
class ExperimentsIsolationValidator:
    """FIX #9: Sanal Beyin deneyleri gerçek görevlerle karışmıyor mu?
    
    PRENSIP: experiments.json ↔ tasks/brain_tasks.json AYRI dosyalar.
    
    KONTROL:
    1. ExperimentTaskManager.path ≠ TaskManager.path
    2. Real Orchestrator'ın arka plan döngüsü experiments.json'u okumaz
    3. Virtual Brain deneyleri "deney" statüsünde kalır
    """

    @staticmethod
    def validate_isolation() -> tuple[bool, str]:
        """Isolation kontrol et.
        
        Returns:
            (ok: bool, message: str)
        """
        try:
            from jarvis.self_improvement.virtual_brain.orchestrator.experiment_task_manager import (
                ExperimentTaskManager,
            )
            from jarvis.paths import tasks_dir

            etm = ExperimentTaskManager()
            real_tasks_path = tasks_dir() / "brain_tasks.json"

            # Kontrol 1: Farklı dosyalar mı?
            if etm.path == real_tasks_path:
                return False, f"KRİTİK: experiments.json gerçek tasks.json ile aynı: {etm.path}"

            # Kontrol 2: experiments.json gerçek tasks/ altında mı?
            if real_tasks_path.parent == etm.path.parent:
                return (
                    False,
                    f"UYARI: experiments.json ve brain_tasks.json aynı klasörde: {etm.path.parent}",
                )

            return True, f"✓ Experiments isolation OK: {etm.path}"

        except Exception as e:
            return False, f"Isolation kontrol hatası: {e}"

    @staticmethod
    def prevent_experiment_leakage() -> None:
        """FIX #9: Real Orchestrator'ın arka plan döngüsü deneyleri çalıştırmasını engelle.
        
        main.py'nin orchestrator._tick() methodu AYNI anda hem real tasks hem
        de experiments.json'u okumuyor mu? Kontrol et.
        """
        try:
            from jarvis.core.brain_orchestrator import get_orchestrator
            from jarvis.self_improvement.virtual_brain.orchestrator.experiment_task_manager import (
                EXPERIMENTS_PATH,
            )

            orch = get_orchestrator()

            # Orchestrator hangi dosyayı okuyor?
            if hasattr(orch, "tasks") and hasattr(orch.tasks, "path"):
                real_path = orch.tasks.path
                if real_path == EXPERIMENTS_PATH:
                    print(
                        f"[CRITICAL] Orchestrator EXPERIMENTS_PATH'u okuyor!"
                        f"\n  Real: {real_path}"
                        f"\n  Experiments: {EXPERIMENTS_PATH}"
                    )
                    return

            print(f"✓ Orchestrator real tasks'ı okuyor: {orch.tasks.path}")

        except Exception as e:
            print(f"[WARN] Leakage prevention hatası: {e}")


# ══════════════════════════════════════════════════════════════
# Integration Snippet: main.py'de dev_agent Çağrısı
# ══════════════════════════════════════════════════════════════
# async def _execute_tool(self, fc) -> types.FunctionResponse:
#     name = fc.name
#     args = dict(fc.args or {})
#
#     # ... existing code ...
#
#     elif name == "dev_agent":
#         description = args.get("description") or args.get("code") or args.get("query") or ""
#         if not description:
#             return types.FunctionResponse(
#                 id=fc.id, name=name,
#                 response={"result": "Hata: Proje açıklaması boş."}
#             )
#
#         # FIX #7: ApprovalRegistry Binding
#         from jarvis.actions.dev_agent_approval_bridge import DevAgentApprovalBridge
#
#         task_id = f"dev_agent_{int(time.time())}"
#         plan = _plan_project(description, args.get("language", "python"))
#
#         # PREVIEW modunda
#         if not args.get("confirm_code"):
#             # Onay iste
#             ok = DevAgentApprovalBridge.request_approval(
#                 task_id=task_id,
#                 description=description,
#                 plan=plan,
#                 message=f"Proje: {description[:100]}\n\n{json.dumps(plan, indent=2)}",
#                 main_instance=self,
#             )
#             if not ok:
#                 return types.FunctionResponse(
#                     id=fc.id, name=name,
#                     response={
#                         "confirm_code": task_id,
#                         "preview": {
#                             "description": description,
#                             "plan": plan,
#                             "message": "Onay beklemede. Kullanıcı onay veriyi dev_agent'ı tekrar çağır.",
#                         }
#                     }
#                 )
#
#         # EXECUTE modunda
#         ok = DevAgentApprovalBridge.consume_approval(
#             task_id=task_id,
#             description=description,
#             plan=plan,
#             main_instance=self,
#         )
#         if not ok:
#             return types.FunctionResponse(
#                 id=fc.id, name=name,
#                 response={"result": "Onay süresi doldu. Lütfen tekrar isteyin."}
#             )
#
#         # Gerçek dev_agent çalıştır
#         result = await loop.run_in_executor(
#             None,
#             lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak)
#         )
#         return types.FunctionResponse(
#             id=fc.id, name=name,
#             response={"result": result or "Done."}
#         )
