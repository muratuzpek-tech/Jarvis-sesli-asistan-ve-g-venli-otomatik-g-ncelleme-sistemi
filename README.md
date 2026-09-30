# MuratJARVIS 25.1.0

Gemini Live tabanlı, PyQt6 arayüzlü çok beyinli bir masaüstü asistanı. Paket;
sesli oturum, yerel kullanıcı verisi, ekran/kamera araçları ve isteğe bağlı
yerel dashboard bileşenlerini içerir. **Bu depodaki otomatik kontroller Linux
üzerinde, donanım ve canlı API kullanılmadan çalışır; gerçek Windows ses/API
kabulü yapılmış sayılmaz.**

## Kurulum

Desteklenen Python aralığı **3.11 veya 3.12**'dir. Python 3.13 ve üzeri bu
paket için doğrulanmış değildir.

### Windows

Depo klasöründeki `KUR_WINDOWS.cmd` dosyasını çalıştırın. Betik kendi klasörüne
geçer; `py` launcher üzerinden önce Python 3.12'yi, sonra 3.11'i arar ve
`.venv` yoksa oluşturur. Uyuşmayan mevcut sanal ortamı silmez; durup açık bir
uyarı verir. Kurulum, yalnızca `.venv` içine editable paket kurar.

Arayüzü backend ve ses başlatmadan görmek için `UI_ONIZLE_WINDOWS.cmd`
çalıştırılabilir. Normal başlatma `BASLAT_WINDOWS.cmd` ile yapılır. Bu
betikler yönetici yetkisi istemez, kalıcı PowerShell ExecutionPolicy ayarı
yapmaz ve kullanıcı yapılandırmasını üzerine yazmaz. PowerShell kurulumu
kullanılacaksa `scripts/setup_windows.ps1` yalnızca açıkça başlatılan bir
alternatiftir; gerçek Windows doğrulaması ayrıca yapılmalıdır.

Windows'ta mikrofon için **Settings > Privacy & security > Microphone** altında
mikrofon erişimini ve masaüstü uygulamalarının erişimini kullanıcı olarak
kontrol edin. Bu adım sürücü, PortAudio, varsayılan cihaz veya Gemini Live
sorunlarını otomatik olarak çözmez. Gerçek kayıt yapmadan mikrofonun düzeldiği
iddia edilmemelidir.

### Linux / macOS / geliştirme

Aşağıdaki komutlar yalnızca Unix-benzeri geliştirme ortamları içindir:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## API anahtarı ve kullanıcı verisi

Anahtar kaynak koduna veya depoya yazılmaz. Okuma önceliği şöyledir:

1. `GEMINI_API_KEY` ortam değişkeni.
2. `JARVIS_API_KEYS` verilmişse o dosya yolu.
3. Kullanıcı veri dizinindeki `config/api_keys.json`.

İlk açılışta UI anahtarı kullanıcıdan alır ve kullanıcı veri dizinine yazar;
`--ui-only` yolu sahte anahtar üretmez ve backend'e bağlanmaz. Anahtarı
paylaşmayın veya örnek dosyaya gerçek değer koymayın. Şema için
[`docs/api_keys.example.json`](docs/api_keys.example.json) dosyasına bakın.

Kullanıcı verisi platforma göre aşağıdaki dizindedir:

| Platform | Varsayılan veri dizini |
| --- | --- |
| Windows | `%LOCALAPPDATA%\MuratJARVIS` |
| macOS | `~/Library/Application Support/MuratJARVIS` |
| Linux | `$XDG_DATA_HOME/MuratJARVIS` veya `~/.local/share/MuratJARVIS` |

`JARVIS_HOME` verilirse tüm bellek, log, görev, yapılandırma ve sertifika
alt dizinleri o köke gider. Deneme kurulumunu mevcut kullanıcı verisinden
ayırmak için Windows'ta geçici bir klasöre `JARVIS_HOME` tanımlayın; test
bitince bu klasörü kullanıcı kendisi silebilir. Uygulama onay olmadan kullanıcı
verisini silmez veya toplu dosya işlemi onaylamaz.

## NVIDIA Nemotron / NIM kullanımı

RTX 3060 12 GB ile Ollama üzerinden 4B–8B sınıfı quantized modeller (ör. NVIDIA Nemotron 3 Nano 4B GGUF veya Llama-3.1 Nemotron Nano 8B) yerel
olarak çalıştırılabilir. Nemotron model etiketleri Ollama kataloğunda mevcutsa
modeli indirip yapılandırmadaki `llm_model` alanına yazın. Yerel kullanımda
`llm_provider` değeri `ollama` kalır; konuşmalar NVIDIA sunucusuna gönderilmez.

NVIDIA’nın barındırılan NIM API’sini denemek için NVIDIA Developer hesabından
`build.nvidia.com` üzerinden anahtar oluşturun ve Windows PowerShell’de anahtarı
kalıcı dosyaya yazmadan oturum değişkeni olarak tanımlayın:

```powershell
$env:NVIDIA_API_KEY = "nvapi-..."
$env:JARVIS_LLM_PROVIDER = "nvidia"
$env:JARVIS_LLM_MODEL = "nvidia/llama-3.1-nemotron-nano-8b-v1"
.\BASLAT_WINDOWS.cmd
```

NVIDIA API prototip/test için ücretsiz erişim sunabilir; ticari üretim kullanımı
ve NIM’in kendi altyapınızda kurumsal dağıtımı lisans koşullarına tabidir.

## Çalıştırma

### Windows

`BASLAT_WINDOWS.cmd` dosyasını çalıştırın veya yerel ortam içinden
`.venv\Scripts\python.exe -m jarvis` komutunu kullanın. Bu yol UI ile
backend'i birlikte başlatır.

### Linux / macOS

```bash
jarvis
# veya
python -m jarvis
```

Yalnızca arayüz tanılama yolu her platformda `python -m jarvis --ui-only`
şeklindedir; bu modda Gemini, ses akışı, dashboard ve ana backend başlatılmaz.

UI durum etiketleri bağlantı ve backend sinyallerine göre
`CONNECTING`, `AUTH_REQUIRED`, `ERROR`, `SLEEPING`, `SPEAKING` ve `LISTENING`
gibi durumları gösterebilir. Görsel UI'nin açılması tek başına mikrofon,
Gemini veya cihaz bağlantısının doğrulandığı anlamına gelmez.

## Dosyalar ve güvenlik sınırları

| İçerik | Konum |
| --- | --- |
| Kod (salt okunur kabul edilir) | `src/jarvis/` |
| Kullanıcı verisi | `JARVIS_HOME` veya platform varsayılanı |
| Hafıza / log / görev / sırlar | veri dizini altında `memory/`, `logs/`, `tasks/`, `config/` |
| Manuel ve canlı kontroller | `tests/manual/` |

Ortam değişkenleri arasında `JARVIS_HOME`, `JARVIS_API_KEYS`, `JARVIS_FACE` ve
isteğe bağlı `JARVIS_ALLOW_FIREWALL_SETUP=1` bulunur. Dashboard güvenlik duvarı/UAC
hazırlığı varsayılan olarak KAPALIDIR; bu adımı etkinleştirmek açık bir kullanıcı
tercihi (bu değişkeni ayarlamak) gerektirir. Paketleme testleri bu ağ yolunu
çalıştırmaz.

## Test ve doğrulama kapsamı

`pytest` ve `ruff check .` offline otomatik kontroller içindir. Paketleme
kontrolleri geçici `JARVIS_HOME` kullanır ve gerçek anahtar, ağ, ses, kamera,
dashboard, firewall veya ana backend başlatmaz. `tests/manual/` altındaki
betikler otomatik test değildir. Windows işletim sistemi, mikrofon gizlilik
ayarları, PortAudio sürücüleri, PyQt DPI davranışı, Gemini Live ve gerçek
cihazlar bu ortamda doğrulanmamıştır.

Ayrıntılı yapı ve doğrulama sınırları için
[`docs/MIMARI.md`](docs/MIMARI.md) ve [`docs/FINAL_AUDIT_2026-09-18.md`](docs/FINAL_AUDIT_2026-09-18.md)
dosyalarına bakın.
