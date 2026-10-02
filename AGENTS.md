# JARVIS — Kod ajanları için kurallar

Bu depoda çalışan her yapay zekâ aracı (Aider, Copilot, Claude, Gemini …) ve
insan katkıcı için ortak kurallar. Araca özel kopya gerekirse bu dosyadan türetin.

## Zorunlu doğrulama

- Belirleyici (deterministik) doğrulama geçmeden **başarı bildirme**.
- Değişikliğin etkilediği testleri VE ilgili test paketinin tamamını çalıştır.
- Linux'ta `JARVIS_SANDBOX=required` davranışını koru; bir testi geçirmek için
  bubblewrap yalıtımını (`src/jarvis/actions/devkit/sandbox.py`) **asla gevşetme**.
- Üretilen programlara API anahtarı ya da ev klasörü **açma**.
- `main` dalına doğrudan değişiklik yapma; ayrı bir dal aç (ör. `feat/…`).
- Uzun canlı testi (`scripts/canli_test.py`) normal `pytest` içine ekleme.
- "Program çalıştı" ile "görev doğru yapıldı"yı ayırt et.
- Her hata düzeltmesine bir regresyon testi ekle.

## Komutlar

```bash
.venv/bin/ruff check .
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_sandbox.py -q -rs
# isteğe bağlı: .venv/bin/pyright src scripts
```

## Başarısızlık sayılan durumlar

- Beklenen çıktı yok, boş ya da yanlış adla yazılmış.
- Kayıt beklenirken veritabanında sıfır satır var.
- Aynı düzeltmeyi strateji değiştirmeden tekrarlamak.

## Gizli bilgiler

- API anahtarları yalnız ortam değişkeni ya da kullanıcı ayar dosyasından
  (`jarvis.core.secure_config`) okunur; koda, teste, loga ya da sohbete yazılmaz.
- Testler gerçek bulut modellerini çağırmaz (`tests/conftest.py` yerel modu zorlar).
