# Jarvis — Claude Code kuralları

Türkçe sesli asistan (Gemini Live + PyQt6). Python 3.12, `.venv`. Kod `src/jarvis/`, ek araçlar `tools/`.

## Kesin kurallar
- COMMIT ve PUSH YAPMA. Commit'i kullanıcı yapar.
- Gerçek HOME'a, `MuratJARVIS` verisine, `~/.ssh`, `api_keys.json`, `.env` dosyalarına dokunma. Testler geçici HOME kullanır.
- Ağ/internet kullanma (testlerde). Gerçek ses, kamera, kapatma/yeniden başlatma çağrısı yapma.
- Dosya silme, taşıma, yeniden adlandırma yok (kullanıcı kendisi yapar). Force push yok.
- `ruff --fix` ve otomatik formatter çalıştırma.
- Mevcut testi silme, zayıflatma veya skip etme. Yeni `xfail` ekleme; sorunu raporla.
- Önce KIRMIZI test yaz, sonra düzelt. Kırmızı→yeşil kanıtını raporla.
- İstenmeyen dosyaya dokunma; kapsam dışı bulguyu ayrıca raporla.

## Güvenlik mimarisi (bozma)
- Her araç `security_gate` içinde EFFECTS kaydına ve 4 kaynak (MODEL_LIVE, AGENT_LOOP, BRAIN_TEAM, REACT) için politikaya sahip olmalı.
- Onay mantığı tek yerde: `core/user_confirmation.py`. Başka yerde kopyalama.
- Güvenlik kapısı hata verirse fail-closed (açık bırakma).
- Korumalı dosyalar: `security_gate.py`, `core/approval_service.py`, `core/user_confirmation.py`, `core/audit_log.py`.

## Doğrulama komutu (her görev sonunda)
```
PATH="$PWD/.venv/bin:$PATH" ruff check src tests scripts && PATH="$PWD/.venv/bin:$PATH" pytest -q --deselect tests/test_terminal_tool.py
```
`tests/test_tool_consistency.py` ve `scripts/tool_inventory.py` araç beyanı/işleyici/politika tutarlılığını denetler; araç eklerken/değiştirirken ikisini de çalıştır.

## Rapor biçimi
Değişen dosyalar, kırmızı→yeşil kanıtı, kalan bilinen sorunlar, tam pytest + ruff sonucu. Düz, kısa Türkçe.
