# Jarvis Tam Otomatik Kodlama Hattı

Bu kurulumda Jarvis görevleri **RTX 3060 bulunan Ubuntu sunucuda Docker içinde** yürütür. Ollama GPU’ya ayrılmış servis olarak çalışır; Jarvis ajanı ayrı container’da çalışır. Ajan, hedef Git deposunu salt-okunur olarak görür, geçici bir Git worktree içinde özgürce kod yazar ve test eder. Ana dal yalnızca tüm kalite komutları başarılı olduktan sonra fast-forward merge ile güncellenir.

## Mimari

```text
Kullanıcı / zamanlayıcı
        |
        v
jarvis-agent container ----HTTP----> ollama container ----NVIDIA----> RTX 3060
        |
        +-- geçici worktree: /workhome/jobs/<id>/workspace
        +-- pytest + ruff kalite kapıları
        +-- report.json + job.log
        +-- başarılıysa commit ve fast-forward merge
```

Ana depo kirliyse görev başlatılmaz. Her görev başlangıç commit’ini kaydeder. Hata veya süre aşımında worktree silinir ve ana dal değişmeden kalır. Varsayılan sınırlar beş deneme, 60 dakika, 4096 context’tir. `JARVIS_MAX_ATTEMPTS` ve `JARVIS_MAX_MINUTES` ile değiştirilebilir.

## Sunucu kurulumu

Ubuntu 24.04 üzerinde NVIDIA sürücüsü, Docker Engine, Docker Compose plugin ve NVIDIA Container Toolkit kurulu olmalıdır. Kurulumdan sonra GPU’yu doğrulayın:

```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

İlk modelleri indirin:

```bash
docker compose -f docker-compose.autonomous.yml up -d ollama
docker compose -f docker-compose.autonomous.yml exec ollama ollama pull qwen2.5-coder:14b
```

12 GB RTX 3060 için önerilen başlangıç ayarı `qwen2.5-coder:14b` ve `JARVIS_OLLAMA_NUM_CTX=4096`’dır. `ollama ps` çıktısında `100% GPU` görülmesi hedeflenir. Eğer offload olursa `qwen3:8b` kullanılabilir.

## Bir görevi tam otomatik çalıştırma

```bash
docker compose -f docker-compose.autonomous.yml build jarvis-agent
docker compose -f docker-compose.autonomous.yml run --rm \
  -e JARVIS_REPO=/repo \
  jarvis-agent \
  "React mobil görünümündeki taşmayı bul, düzelt ve testleri çalıştır"
```

`JARVIS_VERIFY_COMMANDS` noktalı virgülle ayrılmış komutlardan oluşur:

```bash
JARVIS_VERIFY_COMMANDS='python -m pytest;ruff check .;python -m compileall -q src' \
docker compose -f docker-compose.autonomous.yml run --rm --env-file .env.autonomous jarvis-agent \
  "Görevi çöz"
```

Görev sonunda `.jarvis-jobs` altında `job.log`, `report.json` ve başlangıç commit’i saklanır. Başarılı görev ana dala `jarvis: verified autonomous fix (...)` mesajıyla commit eder. Başarısız görev ana dalı değiştirmez.

## Full otomasyon bağlantısı

Mevcut Jarvis arka plan görev sistemi, bu komutu bir zamanlayıcı/worker üzerinden çağırabilir. Üretim ortamında `jarvis-agent` container’ına Docker socket verilmez; ajan yalnızca kendi container’ında test çalıştırır. Böylece Jarvis kod yazmada serbesttir fakat host Docker’ını veya gizli dosyaları yönetemez. GitHub push gerekiyorsa ayrı bir dış teslim adımı olarak ve korumalı token scope’larıyla eklenmelidir; bu kurulum otomatik olarak uzak depoya push yapmaz.

## Bilinçli sınırlar

“En doğru sonucu bulana kadar” döngüsü sonsuz bırakılmadı. Aynı görevin maksimum deneme ve süre sınırı vardır. Test komutları zararlı sistem komutları içermemelidir; üretim ortamında repo içindeki `JARVIS_VERIFY_COMMANDS` değeri yalnızca yöneticinin ayarlayabildiği bir environment dosyasından verilmelidir.
