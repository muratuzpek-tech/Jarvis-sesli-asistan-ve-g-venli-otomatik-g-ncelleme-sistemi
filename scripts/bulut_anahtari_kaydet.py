"""Ücretsiz bulut modeli (Groq) anahtarını GÜVENLİ biçimde kaydeder.

Kullanım:  .venv/bin/python scripts/bulut_anahtari_kaydet.py
- Anahtar ekrana YAZILMADAN (gizli) sorulur, sohbete/terminal geçmişine düşmez.
- Önce Groq'a küçük bir deneme isteği atılır; anahtar çalışıyorsa JARVIS'in
  kullanıcı ayar dosyasına (yalnız sizin okuyabileceğiniz) kaydedilir.
- Silmek için:  .venv/bin/python scripts/bulut_anahtari_kaydet.py --sil
"""
from __future__ import annotations

import getpass
import os
import sys

import requests

from jarvis.actions.dev_agent import CLOUD_LLM_DEFAULT_MODEL, CLOUD_LLM_DEFAULT_URL
from jarvis.core.secure_config import api_keys_path, load_config, save_config


def main() -> int:
    if "--sil" in sys.argv:
        cfg = load_config()
        cfg.pop("cloud_llm_api_key", None)
        path = api_keys_path(for_write=True)
        path.write_text(__import__("json").dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print("Bulut modeli anahtarı silindi; JARVIS yerel modele döner.")
        return 0
    key = getpass.getpass("Groq API anahtarını yapıştırın (ekranda görünmez) ve Enter'a basın: ").strip()
    if not key.startswith("gsk_") or len(key) < 20:
        print("Bu bir Groq anahtarına benzemiyor (gsk_ ile başlamalı). Kaydedilmedi.")
        return 1
    print("Deneme isteği gönderiliyor…")
    try:
        r = requests.post(
            f"{CLOUD_LLM_DEFAULT_URL}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": CLOUD_LLM_DEFAULT_MODEL, "max_tokens": 20,
                  "messages": [{"role": "user", "content": "Sadece 'tamam' yaz."}]},
            timeout=60,
        )
    except requests.RequestException as e:
        print(f"Groq'a ulaşılamadı: {type(e).__name__}. Kaydedilmedi.")
        return 1
    if r.status_code != 200:
        print(f"Groq anahtarı kabul etmedi (kod {r.status_code}). Kaydedilmedi.")
        return 1
    path = save_config({"cloud_llm_api_key": key})
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f"✅ Anahtar çalışıyor ve kaydedildi ({path}, yalnız siz okuyabilirsiniz).")
    print(f"   JARVIS artık kod yazımı için {CLOUD_LLM_DEFAULT_MODEL} modelini kullanacak.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
