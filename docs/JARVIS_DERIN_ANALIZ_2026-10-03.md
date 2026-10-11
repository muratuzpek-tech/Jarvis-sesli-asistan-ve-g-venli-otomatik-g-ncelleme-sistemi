# MuratJARVIS Derin Analiz Raporu

**Tarih:** 2026-10-03  
**İncelenen sürüm:** 25.1.0 deposu  
**Kapsam:** Mimari, komut yürütme, yapay zekâ araç zinciri, dosya güvenliği, uzaktan dashboard, otomasyon ve test kapsamı.

## 1. Yönetici özeti

JARVIS basit bir sesli asistan değil; yaklaşık **37.000 satırlık Python kodundan** oluşan, LLM araç çağrıları, dosya işlemleri, masaüstü kontrolü, kod üretimi, arka plan görevleri, yerel dashboard, ses ve çoklu beyin/orchestrator katmanlarını birleştiren bir masaüstü ajanıdır.

Mimari yönden güçlü tarafı, keyfi shell komutu yerine çoğunlukla isimlendirilmiş araçlar kullanması ve dosya işlemlerinde ev dizini sınırı/onay akışları bulunmasıdır. Ancak güvenlik sınırı tek bir merkezden uygulanmadığı için aynı tehlikeli sonuç farklı yollarla üretilebilmektedir.

### Genel karar

> **JARVIS şu anda deneysel/kişisel kullanım için geliştirilebilir durumda; sınırsız otonom masaüstü ajanı olarak güvenli kabul edilmemeli.**

En önemli riskler:

1. Kapatma/yeniden başlatma davranışı birden fazla araçta bulunuyor.
2. `game_updater` indirme tamamlanınca doğrudan işletim sistemi kapatabiliyor.
3. Kod üreten araçlar oluşturulan kodu gerçek Python/Node/Bash süreçleriyle çalıştırıyor.
4. `shutdown_jarvis` model aracının gerçek kullanıcı onayından bağımsız uygulamayı kapatma yolu var.
5. Dashboard sertifika yoksa HTTP kullanıyor; LAN üzerindeki trafik şifrelenmeyebilir.
6. Son eklenen gerçek kullanıcı onayı yalnızca ana canlı akışta çalışıyor; `agent_loop` onay yolu ile henüz tek bir ortak yetkilendirme mekanizması kullanmıyor.

## 2. Mimari harita

```text
Ses / UI / Dashboard komutu
          |
          v
     src/jarvis/main.py
          |
          +--> Gemini Live / LLM tool call
          |
          +--> computer_settings      -> OS ve klavye/fare
          +--> file_controller        -> dosya işlemleri
          +--> code_helper             -> kod yazma/çalıştırma
          +--> dev_agent               -> proje üretme/çalıştırma
          +--> game_updater            -> Steam/Epic ve otomatik kapatma
          +--> agent_loop              -> 60 saniyelik arka plan görevleri
          +--> dashboard.server       -> LAN üzerinden remote kontrol
          +--> brain_orchestrator     -> çoklu beyin görev akışı
```

### Yapısal gözlem

Ana risk, araçların farklı katmanlarda farklı onay politikaları uygulamasıdır:

- `main.py` doğrudan tool-call yürütür.
- `tools_kopru.py` arka plan görevlerinin araçlarını sınırlar.
- `security_ai.py` risk sınıflandırır.
- `agent_loop.py` ayrı bir onay akışı kullanır.
- Bazı özel araçlar (`game_updater`, `shutdown_jarvis`, kod yürütme) bu ortak kapının dışında kalır.

Bu nedenle güvenlik kararları tek bir **Policy/Capability Broker** üzerinden geçmiyor.

## 3. Bulgular ve önem dereceleri

### P0 — İşletim sistemi kapatma yolları tek merkezde değil

#### Kanıtlar

- `src/jarvis/actions/computer_settings.py`: `shutdown_computer()` ve `restart_computer()` doğrudan `systemctl poweroff/reboot` çağırıyor.
- `src/jarvis/actions/game_updater.py:634-665`: indirme tamamlanınca `_system_shutdown()` doğrudan işletim sistemini kapatıyor.
- `src/jarvis/main.py:750`: `shutdown_when_done` modeli tarafından verilen bir boolean parametre.
- `src/jarvis/main.py:1685-1696`: `shutdown_jarvis` aracı `os._exit(0)` ile JARVIS sürecini kapatıyor.
- Önceki terminal günlüğünde `shutdown` komutu gerçekten zamanlanmıştı:

```text
Shutdown scheduled for Sat 2026-10-03 00:50:19 +03
```

#### Etki

Bir kullanıcı isteği yanlış yorumlanırsa veya model bir parametreyi yanlış doldurursa Ubuntu kapanabilir. `computer_settings` için yeni onay kapısı eklendi; fakat `game_updater` ve `shutdown_jarvis` aynı ortak kapıyı kullanmıyor.

#### Öneri

- Tüm güç işlemlerini tek `PowerActionBroker` üzerinden geçirmek.
- `shutdown`, `reboot`, `poweroff`, `lock_screen`, otomatik indirme sonrası kapatma ve uygulama kapanmasını ayrı yetenekler olarak sınıflandırmak.
- Her işlem için işlem özeti + tek kullanımlık onay tokenı + süre sonu uygulamak.
- `shutdown_when_done=true` değerini ilk tool-call’da yalnızca önizleme olarak kabul etmek; gerçek kapatmayı onaydan sonraki ayrı çağrıya bırakmak.
- `shutdown_jarvis` için de “uygulamayı kapat” onayı uygulamak.

### P0 — Üretilen kod gerçek süreç olarak çalıştırılıyor

#### Kanıtlar

- `src/jarvis/actions/code_helper.py:363-390`: `.py`, `.js`, `.sh`, `.ps1` vb. dosyaları gerçek interpreter ile `subprocess.run()` üzerinden çalıştırıyor.
- `src/jarvis/actions/dev_agent.py`: modelin ürettiği `run_command` proje dizininde çalıştırılıyor.
- `dev_agent` içinde bazı tehlikeli kalıplar filtreleniyor; ancak bu bir sandbox değildir.

#### Etki

Ev dizini altında oluşturulmuş bir Python veya Bash dosyası; ağ erişimi, başka dosyaları okuma, kullanıcı verilerini değiştirme, süreç başlatma ve sistem araçlarına erişme yeteneğine sahip olabilir. `shell=False` kullanılması iyi bir önlem olmakla birlikte Python yorumlayıcıya verilen kodun kendisi zaten keyfi işletim sistemi işlemleri yapabilir.

#### Öneri

- Kod üretimi ile kod çalıştırmayı iki ayrı kullanıcı onayına ayırmak.
- Varsayılan davranışı yalnızca statik analiz + syntax check yapmak olarak değiştirmek.
- Çalıştırma gerekiyorsa ayrı düşük yetkili kullanıcı/container/namespace kullanmak.
- Ağ erişimini varsayılan olarak kapatmak.
- CPU, RAM, disk, süre ve child-process sınırı koymak.
- Çalıştırılan dosyanın hash'ini ve tam komutunu loglamak.

### P1 — `game_updater` otomatik kapatma için onay dışı kaçış yolu

`game_updater` içinde `shutdown_when_done` true olduğunda arka plan thread'i indirme bitince `systemctl poweroff` çalıştırıyor. Bu yol `computer_settings` onay kapısından geçmiyor.

Bu özellik kaldırılmalı veya:

1. İlk çağrıda yalnızca “indirme bitince kapatma isteği kaydedildi” önizlemesi dönmeli.
2. Kullanıcı açıkça onay vermeli.
3. Onay tokenı göreve kaydedilmeli.
4. Arka plan thread'i yalnızca geçerli token varsa kapatma yapmalı.
5. Görev iptal edildiğinde bekleyen thread iptal sinyali almalı.

### P1 — `agent_loop` ile canlı onay akışı ayrışıyor

`tools_kopru.py:84-93`, arka plan görevi içinde `confirmed` parametresini siliyor. Bu güvenlik açısından doğru yönde; fakat yeni `computer_settings` uygulama bayrağı `_user_confirmation_granted` yalnızca `main.py` tarafından veriliyor.

Sonuç:

- Canlı UI/voice akışında açık kullanıcı onayı çalışabilir.
- `agent_loop.approve_task()` sonrasında `computer_settings` kapatma/restart çağrısı gerçek işlemi yapamayabilir; çünkü özel uygulama bayrağı yok.
- Güvenlik ve işlevsellik arasında tutarsızlık oluşuyor.

Öneri: Onay tokenı üretme/doğrulama tek bir modüle taşınmalı. `main.py` ve `agent_loop.py` aynı `approval_service.py` API'sini kullanmalı; ham boolean veya özel gizli parametre kullanılmamalı.

### P1 — `lock_screen` sınıflandırılıyor fakat doğrudan akışta aynı şekilde korunmuyor

- `tools_kopru.py:206`: `lock_screen` ve `lock` yıkıcı sınıfta.
- `security_ai.py:27-28`: yüksek riskli kabul ediliyor.
- `computer_settings.py:585`: doğrudan onay listesinde yalnızca `restart` ve `shutdown` var.

Bu, farklı katmanların risk sözlüklerinin senkron olmadığını gösteriyor. Tek bir ortak risk tablosu kullanılmalı.

### P1 — Dashboard HTTP’ye düşebiliyor

`dashboard/server.py:419-421` sertifika yoksa URL'yi `http://...:8000` olarak döndürüyor.

Kimlik doğrulama tokenı ve dashboard verisi LAN üzerinden taşınırken TLS yoksa ağdaki başka bir cihaz trafiği izleyebilir. 6 karakterlik tek kullanımlık anahtar + rate limit iyi savunmalardır; ancak HTTP, tokenın ağ üzerinde ele geçirilmesi riskini ortadan kaldırmaz.

Öneri:

- Varsayılanı yalnızca `127.0.0.1` yapmak.
- Uzaktan kullanım için TLS yoksa çalışmayı reddetmek veya kullanıcıya açık risk uyarısı göstermek.
- Bearer tokenı `sessionStorage` yerine mümkünse `HttpOnly`, `Secure`, `SameSite` cookie ile taşımak.
- WebSocket bağlantısında token süresi ve cihaz iptalini ayrıca doğrulamak.

### P1 — `system_scan_and_repair` ile paket kurulumu

`system_scan.py` gerçek paket kurulumunu `JARVIS_ALLOW_DEP_INSTALL=1` ile sınırlandırmış; bu iyi bir düzeltmedir. Ancak:

- Araç açıklaması hâlâ eksik paketleri “onay istemeden otomatik kurar” diyor.
- Prompt/araç sözleşmesi ile gerçek davranış tutarlı değil.
- Ortam değişkeni açıkken model tarafından çağrılan tarama paket kurulumu yapabilir.

Öneri: Kurulum her zaman önizleme + kullanıcı onayı ile yapılmalı; ortam değişkeni yalnızca geliştirici modunda ek bir izin olarak kalmalı.

### P2 — Monolitik dosyalar bakım ve güvenlik riskini büyütüyor

En büyük modüller:

| Modül | Yaklaşık dosya boyutu |
|---|---:|
| `dev_agent.py` | 190.623 byte |
| `main.py` | 135.801 byte |
| `ui.py` | 135.736 byte |
| `brain_orchestrator.py` | 75.556 byte |
| `file_controller.py` | 55.075 byte |
| `game_updater.py` | 41.057 byte |
| `dashboard/server.py` | 40.799 byte |

Bu boyutlar tek başına hata demek değildir; ancak güvenlik politikasının kopyalanmasına, eski kuralların unutulmasına ve testlerin tüm yolları yakalayamamasına neden olur.

Öneri:

- `policy/` veya `security/` paketi oluşturmak.
- Güç işlemleri, dosya işlemleri, dış iletişim, kod çalıştırma ve ağ erişimi için ortak politika arayüzü.
- `main.py` yalnızca yönlendirme yapsın; araçların iş mantığı ayrı modüllerde kalsın.

### P2 — Test kapsamı gerçek donanım ve canlı entegrasyonları kapsamıyor

README ve mimari belgeleri açıkça şu alanların otomatik doğrulanmadığını belirtiyor:

- Gerçek Windows
- Mikrofon/hoparlör
- Gemini Live
- Kamera
- Dashboard ağ bağlantısı
- Firewall/UAC
- Gerçek ydotool/Wayland

Mevcut sandbox'ta `pytest` ve `ruff` kurulu değildi. `compileall` başarılı oldu ve yeni güvenlik senaryolarının bağımsız kontrolleri başarılı geçti; ancak tam test paketi bu oturumda çalıştırılamadı.

## 4. Güçlü taraflar

- `shell=True` tabanlı genel bir shell yürütücüsü görünmüyor.
- `windows_shell.py` sabit allowlist yaklaşımı kullanıyor.
- `tools_kopru.py` bilinmeyen araçlarda rastgele kod çalıştırmıyor.
- Dosya işlemlerinde ev dizini sınırı ve symlink çözümleme kontrolü var.
- Dosya taşıma/silme ve mevcut dosyanın üzerine yazmada iki aşamalı onay yaklaşımı bulunuyor.
- Dashboard login anahtarı tek kullanımlık ve başarısız denemeler IP bazında sınırlanıyor.
- Kullanıcı verisi ile kaynak kodu `JARVIS_HOME` üzerinden ayrılmaya çalışılmış.
- API anahtarının kaynak koda yazılmaması ve ortam değişkeni önceliği doğru yönde.
- Yeni bilgisayar kapatma onayı modelin `confirmed=yes` parametresine güvenmek yerine gerçek kullanıcı turuna bağlanmış durumda.

## 5. Öncelikli düzeltme planı

### Aşama 1 — Hemen

1. `game_updater.shutdown_when_done` otomatik kapatmasını kapat veya ortak onay brokerına taşı.
2. `shutdown_jarvis` aracına gerçek kullanıcı turu onayı ekle.
3. `lock_screen` işlemini doğrudan canlı akışta da onay kapsamına al.
4. `tools_kopru`, `main.py` ve `security_ai` için tek risk tablosu kullan.
5. Dashboard sertifika yoksa uzaktan erişim davranışını açıkça sınırla.

### Aşama 2 — Güvenli ajan çekirdeği

1. `ApprovalService`: işlem özeti, token, TTL, kullanıcı turu, tek kullanım.
2. `CapabilityPolicy`: her aracın izinleri ve risk seviyesi.
3. `ExecutionSandbox`: kod çalıştırma için ayrı kullanıcı/container ve kaynak sınırları.
4. `AuditLog`: kullanıcı isteği, model tool-call'ı, onay, gerçek argv, sonuç ve exit code.
5. Tool-call şemalarında `confirmed` gibi model tarafından üretilebilir güvenlik parametrelerini kaldır.

### Aşama 3 — Test

Her riskli yetenek için en az şu testler eklenmeli:

- Model doğrudan tehlikeli parametre gönderirse reddedilir.
- Aynı tool-call içinde ikinci kez onay üretirse reddedilir.
- Kullanıcı onayı olmadan arka plan görevi çalıştırılamaz.
- Onay tokenı başka işlemde kullanılamaz.
- Token süresi dolunca işlem yapılamaz.
- `game_updater` thread'i onaysız poweroff yapamaz.
- `type_text + Enter` ile tehlikeli komut bypass edilemez.
- Dashboard HTTP/TLS davranışı test edilir.
- Kod çalıştırma ağ erişimi ve ev dışı dosya erişimi olmadan çalışır.

## 6. Sonuç

JARVIS'in temel yönü doğru: araç tabanlı mimari, allowlist, dosya sınırları ve onay kavramı mevcut. Fakat sistem henüz tek bir güvenlik çekirdeği tarafından yönetilmiyor. Bu nedenle bir yoldaki düzeltme başka bir yoldaki aynı etkili işlemi otomatik olarak korumuyor.

En kritik tasarım kararı şudur:

> **LLM hiçbir zaman güvenlik onayının sahibi olmamalı; yalnızca istek üretmeli. İzin, risk sınıflandırması ve tek kullanımlık onay uygulama kodunda merkezi olarak verilmelidir.**

Bu ilke uygulanır ve kod çalıştırma sandbox'a alınırsa JARVIS, kişisel bilgisayarda kontrollü bir yapay zekâ geliştirici/masaüstü asistanına dönüşebilir. Mevcut haliyle ise tam otonom ve sınırsız terminal yöneticisi olarak çalıştırılması önerilmez.
