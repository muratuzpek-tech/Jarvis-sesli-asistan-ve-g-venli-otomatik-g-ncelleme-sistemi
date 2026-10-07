# Jarvis kendi kendine geliştirme: prompt listesi

Kural: her satır tek, ölçülebilir iş. Önce başarısız (kırmızı) test yazılır,
sonra düzeltme yapılır. Testler gerçek HOME / MuratJARVIS verisine dokunmaz.
Mevcut testleri silme, zayıflatma, skip/xfail ekleme. Bağımlılık ekleme.
Sıra: kolaydan zora, düşük riskliden yükseğe.

## Düşük risk (küçük, tek yer)

- [ ] "sağ ol", "teşekkürler", "teşekkür ederim" gibi ifadeler shutdown_jarvis aracını tetiklememeli. Tetiklenme kararının verildiği yeri bul, bu ifadelerle kırmızı test yaz (araç çağrılmamalı), sonra düzelt. "kapat", "kendini kapat" gibi gerçek kapatma komutları çalışmaya devam etmeli (bunun için de test).
- [ ] file_controller "Not a file: Masaüstü" hatası: "Masaüstü", "Belgeler", "İndirilenler" gibi Türkçe klasör adları bir klasöre çözümlenmeli ve dosya gibi işlenmemeli. Çözümleme fonksiyonunu bul, bu adlar için kırmızı test yaz (geçici HOME içinde sahte klasörlerle), sonra düzelt.
- [ ] Mikrofon gürültüsünden gelen çok kısa veya anlamsız transkriptler (tek harf, sadece noktalama, "hmm", "ee") araç çağrısı tetiklememeli. Transkriptin araca gittiği yeri bul, bu girdilerle kırmızı test yaz, sonra güvenli bir alt sınır filtresi ekle. Normal kısa komutlar ("dur", "saat kaç") engellenmemeli (test).
- [ ] Zararsız teşekkür/selam gibi kısa sohbet cümleleri hiçbir yan etkili araç çağırmamalı: bunun için mevcut araç yönlendirme katmanına tablo-tabanlı bir test ekle (en az 10 cümle).

## Orta risk (tools/developer)

- [ ] Tkinter hesap makinesi görevlerinde modelin eval kullanması sık reddediliyor. tools/developer/ altında güvenli bir ifade hesaplayıcı yardımcısı (safe_math.py, ast tabanlı, yalnızca sayılar ve + - * / ** % parantez, eval/exec yok) ekle. Önce kırmızı testler: geçerli ifadeler, sıfıra bölme, geçersiz karakter, çok uzun ifade. Sonra agentic_coder'daki eval reddi mesajında modele bu yardımcıyı kullanmasını öner.
- [ ] Arka planda çalışan agentic_code görevi için iptal aracı ekle: iş kimliğiyle iptal edilebilmeli, gerçekten durmalı ve durum "cancelled" olmalı. Model iptal edilmediği halde "iptal ettim" dememeli: iptal başarısızsa araç hata dönmeli. Önce kırmızı testler (çalışan iş iptal edilir, olmayan iş için hata, iki kez iptal güvenli), sonra uygula.
- [ ] agentic_coder üretilen kod alt süreçlerine ortam değişkenlerini aktarırken GEMINI_API_KEY, API anahtarı, token, secret, password içeren değişkenleri çıkar (izin listesi yaklaşımı: PATH, HOME, LANG, PYTHONPATH gibi yalnızca gerekli olanlar). Önce kırmızı test (sahte anahtar ortamda, alt süreç onu görmemeli), sonra uygula. Çalışan testler ve ruff etkilenmemeli.

## Yüksek risk (ses / ana döngü): yalnızca tek dosya, çok küçük değişiklik

- [ ] "ZORLA ROTATE" hatasının nedenini kodda bul. Hatayı yeniden üreten kırmızı bir test yaz. Düzeltme 30 satırı geçmeyecek; geçecekse görevi [!] işaretle ve nedenini yaz, kod değiştirme.
- [ ] Ollama tercih ayarı: kullanıcı ayarlarında "yerel modeli tercih et" seçeneği açıksa agentic_coder önce Ollama'yı denemeli, kapalıysa mevcut davranış korunmalı. Önce kırmızı test (ayar açık: Ollama seçilir; kapalı: eski sıra), sonra uygula.
