"""Ortak test ayarları."""
import os

# Uçtan uca dev_agent testleri gerçek programlar çalıştırır. Linux'ta varsayılan
# JARVIS_SANDBOX=required olduğundan, bubblewrap'in olmadığı ya da kullanıcı ad
# alanlarının kapalı olduğu makinelerde (ör. bazı CI koşucuları) bu testler
# REFUSED alırdı. Testlerde kafes varsa kullanılır, yoksa uyarıyla atlanır;
# kafesin kendisi tests/test_sandbox.py'de ayrıca ve zorunlu olarak sınanır.
os.environ.setdefault("JARVIS_SANDBOX", "auto")
# Testler gerçek Gemini çağrısı yapmaz (kabul testi/hakem yerel sahte modelle).
os.environ.setdefault("JARVIS_DEVAGENT_JUDGE", "local")
os.environ.setdefault("JARVIS_DEVAGENT_BACKEND", "local")  # testlerde gerçek Gemini CLI çağrılmaz
