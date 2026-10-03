"""tests/conftest.py — Jarvis 2.0 test isolation

Sadece yeni regression testlerini çalıştır (test_registry, test_security, ...)
Eski test_brain_*.py dosyaları JarvisLive mock gerektirir → ayrı koleksiyon.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

collect_ignore = [
    "test_brain_health_route.py",
    "test_brain_intent_priority.py", 
    "test_brain_start_route.py",
    "test_brain_status_priority.py",
    "test_brain_*.py",
]
