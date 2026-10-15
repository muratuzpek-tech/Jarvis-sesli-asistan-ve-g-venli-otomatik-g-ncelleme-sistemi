# Jarvis — 34 Test Onarımı Değişiklik Özeti

## Sonuç

- Tam pytest paketi: başarılı
- Önceki 34 başarısız test: 0 başarısız
- Ruff: başarılı
- Python compileall: başarılı
- Onay regresyonları: başarılı

## Ana düzeltmeler

### Dosya yazma güvenliği

- `_is_safe_write_path()` ve `_is_safe_write_file()` ortak fail-closed politika noktaları eklendi.
- Dosya oluşturma, yazma, değiştirme, silme, taşıma ve kopyalama işlemleri güvenli known-folder kökleriyle sınırlandırıldı.
- XDG `user-dirs.dirs` parser’ı `$HOME`, `${HOME}`, yorumlar ve mutlak yolları destekleyecek şekilde düzeltildi.
- `$HOME`, `/`, path traversal, `.ssh`, `.config`, `autostart` ve gizli path bileşenleri reddediliyor.
- Lokalize Desktop/Downloads/Documents/Pictures/Music/Videos yolları destekleniyor.
- `code_helper` ortak dosya yazma politikasına bağlandı.

### Dev Agent kalite kapısı

- Stub kod kaldığında sonuç `QUALITY GATE FAILED` olarak raporlanıyor.
- Kabul testi başarısızlığı `ACCEPTANCE TEST FAILED` olarak korunuyor.
- Tekrarlanan çıktıda erken çıkış, gerçek kalite/kabul hata türünü ezmiyor.
- Başarısız kalite kapısı başarı gibi raporlanmıyor.

### Onay ve DevKit uyumluluğu

- `_last_user_turn_text` state’i eklendi.
- `note_user_turn()` eski ve yeni çağrı biçimleriyle uyumlu hale getirildi.
- Go DevKit gerçek kullanıcı onayı sözleşmesine bağlandı.

## Doğrulama

Değişiklikler GitHub’a ayrı bir düzeltme branch’i üzerinden gönderilmiştir. Semgrep’in çalıştırılabilmesi için GitHub Actions ortamında Semgrep bağımlılığı ayrıca doğrulanmalıdır.
