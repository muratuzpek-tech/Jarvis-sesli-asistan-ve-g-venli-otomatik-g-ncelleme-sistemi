"""CANLI ENTEGRASYON: agent.py → Plugin Security Gate (FIX #13)

Bu dosya, core/agent.py'deki plugin çağrısını security_gate üzerinden 
geçirecek şekilde güncelleme talimatı içerir.

MEVCUT KOD (lines 326-330):
    if name in self.plugins.tools:
        try:
            result = self.plugins.call(name, args)
        except Exception as exc:
            result = {"error": str(exc)}

YENİ KOD:
    if name in self.plugins.tools:
        # FIX #13: Plugin Security Gate entegrasyon
        from jarvis.core.p0_critical_fixes import get_plugin_gate
        from jarvis.security_gate import Source
        
        gate = get_plugin_gate()
        ok, reason = gate.authorize_plugin_call(name, name, args)
        if not ok:
            result = {"error": f"Plugin '{name}' reddedildi: {reason}"}
        else:
            try:
                result = self.plugins.call(name, args)
            except Exception as exc:
                result = {"error": str(exc)}
"""

# Bu modül yalnızca entegrasyon talimatı içerir.
# core/agent.py'yi doğrudan güncellemek için aşağıdaki patch'i uygula:

AGENT_PY_PATCH = '''
--- a/src/jarvis/core/agent.py
+++ b/src/jarvis/core/agent.py
@@ -324,10 +324,18 @@ class JarvisAgent:
                 except (TypeError, ValueError, json.JSONDecodeError) as exc:
                     result = {"error": f"Geçersiz araç argümanları: {exc}"}
                 else:
                     if name in self.plugins.tools:
+                        # FIX #13: Plugin Security Gate entegrasyon
+                        from jarvis.core.p0_critical_fixes import get_plugin_gate
+                        from jarvis.security_gate import Source
+                        
+                        gate = get_plugin_gate()
+                        ok, reason = gate.authorize_plugin_call(name, name, args)
+                        if not ok:
+                            result = {"error": f"Plugin '{name}' reddedildi: {reason}"}
+                        else:
+                            try:
+                                result = self.plugins.call(name, args)
+                            except Exception as exc:
+                                result = {"error": str(exc)}
                         try:
                             result = self.plugins.call(name, args)
                         except Exception as exc:
'''
