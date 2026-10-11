# Jarvis self-dev: sandbox'ta kendi kendini geliştirme

`scripts/self_dev.py`, `prompts.md` içindeki `- [ ]` görevlerini ana depoya
dokunmadan bir sandbox klonunda (yerel Ollama ile) işler. Ana depo yalnızca
`init` sırasında okunur; sandbox'ta git uzağı yoktur, push imkânsızdır.

## Kurulum

```bash
python scripts/self_dev.py init          # ~/jarvis-sandbox klonu + .venv
```

Yollar ortamla değiştirilebilir: `JARVIS_SANDBOX` (~/jarvis-sandbox),
`JARVIS_SELF_DIR` (~/.jarvis-self), `JARVIS_REPO` (bu depo).

## Çalıştırma

```bash
python scripts/self_dev.py run --backlog prompts.md            # tek tur
python scripts/self_dev.py run --forever --backlog prompts.md  # turlar
```

- Her göreve sabit önek eklenir: önce başarısız test, sonra düzeltme; test
  silme/zayıflatma, skip/xfail ve bağımlılık ekleme yasak (koruma kuralları da
  bunları reddeder).
- Görev geçerse (koruma + `pytest` + `ruff`) sandbox'ta commit atılır; geçmezse
  sandbox görev öncesine döner.
- `--forever`: başarısız görev sonraki turda son 2 denemenin kısaltılmış hata
  çıktısıyla yeniden denenir; görev başına en çok 3 tur. Hepsi `[x]` olunca ya
  da tur sınırı dolunca durur. `prompts.md`'ye eklenen görevler sonraki turda
  alınır. Varsayılan süre sınırı yoktur (`--max-hours` ile verilebilir).
- Durum `~/jarvis-sandbox/state.json`'dadır; süreç ölürse kaldığı yerden devam
  eder (yarıda kalan görevin turu sayılmış olur).
- Ön kontrol: ilk görevden önce doğrulama sandbox'ta bir kez çalışır; kırmızıysa
  hiç görev işlenmez ("sandbox baştan kırmızı", çıkış 4, raporda son 40 satır).
- Coder değişiklik yapmadan traceback ile çökerse bu altyapı hatasıdır: görevin
  hakkı yanmaz, state'e yazılmaz; art arda 2 kez olursa döngü durur (çıkış 5).
- Ortam: `PYTHONPATH=<sandbox>/src` (src/ düzeni; `init` ayrıca
  `pip install -e . --no-deps` dener), `PYTHONDONTWRITEBYTECODE=1`. Doğrulamanın
  `HOME`'u `~/.jarvis-self/home`; coder'ın `HOME`'u sandbox'ın üst dizini (o
  gerçek HOME ise sandbox'ın kendisi), çünkü AgenticCoder proje yolunun $HOME
  altında olmasını şart koşar. `XDG_*` her zaman `~/.jarvis-self/home` altında.
- `JARVIS_SELF_DEV_YIELD=1`: Jarvis çalışırken yeni görev başlatmaz, bekler.

### Arka plan servisi (systemd kullanıcı birimi, root gerekmez)

```bash
python scripts/self_dev.py install-service --backlog prompts.md
systemctl --user daemon-reload
systemctl --user enable --now jarvis-self-dev.service
journalctl --user -u jarvis-self-dev.service -f
```

`install-service` yalnızca `~/.config/systemd/user/jarvis-self-dev.service`
dosyasını yazar (`Restart=on-failure`, `Nice=10`); systemctl komutlarını sizin
çalıştırmanız gerekir. Kaldırma: `uninstall-service`, ardından yazdırılan
`systemctl --user stop …` / `daemon-reload`.

## Durdurma

```bash
touch ~/.jarvis-self/STOP     # mevcut görev bitince temiz çıkış
rm ~/.jarvis-self/STOP        # yeniden çalıştırmadan önce
```

Aynı anda tek örnek çalışır (`~/.jarvis-self/lock`).

Eski başarısız kayıtları sıfırlamak (yalnızca sandbox `state.json`'ı siler,
git'e dokunmaz): `python scripts/self_dev.py reset-state`

Çıkış kodları: 0 normal/STOP, 1 hata, 2 Ollama yok, 3 kilit dolu,
4 sandbox baştan kırmızı, 5 coder art arda çöktü.

## Rapor

Her çalıştırma `~/.jarvis-self/reports/<zaman>.md` yazar: durma nedeni, görev
başına durum/tur/deneme/süre, değişen dosyalar, commit. Deneme günlükleri ve
diff'ler `~/.jarvis-self/jobs/` altındadır.

## Promote (ana depoya alma)

```bash
bash scripts/self_dev_promote.sh
```

Sandbox'taki yeni commit'leri `~/.jarvis-self/out/<zaman>/*.patch` olarak
yazar ve `git am --3way …` komutunu yazdırır. Ana depoyu değiştirmez;
yamaları gözden geçirip Jarvis kapalıyken kendiniz uygulayın.
