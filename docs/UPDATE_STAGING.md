# İmzalı güncelleme staging akışı

## Güvenlik sözleşmesi

JARVIS açılışında yapılandırılmış bir HTTPS manifestini arka planda kontrol eder. Manifestin Ed25519 imzası doğrulanmadan ve indirilen dosyanın boyut/SHA-256 değeri manifestle eşleşmeden dosya staging alanına alınmaz. İstekler yönlendirmeyi takip etmez; manifest ve artifact URL’leri aynı HTTPS origin’inde olmalıdır. İçerik akışı boyut sınırıyla yazılır ve dosya atomik olarak yayımlanır.

Artifact, JARVIS kod dizini yerine `data_dir()/updates/staging/<version>/` altına kaydedilir. UI, sürüm, tür, dosya adı, boyut, SHA-256 ve sürüm notlarını gösteren bir onay penceresi açar. Kullanıcının kararı aynı staging klasörüne `approval.json` olarak kaydedilir. `is_update_approved()` kurulumdan hemen önce kararı ve dosya özetini yeniden doğrulayacak yardımcıdır.

**Bu bileşen artifact’i çalıştırmaz, kurmaz, patch uygulamaz ve JARVIS kaynak dosyalarını değiştirmez.** Kurulum/uygulama için ayrıca platforma uygun bir updater gerekir; o bileşen yalnızca `is_update_approved()` başarılı olduğunda çalışmalıdır. JARVIS normal açılışı staging, ağ veya dialog hatası nedeniyle durmaz.

## İstemci yapılandırması

Güncelleme kontrolü fail-closed çalışır. Aşağıdaki üç değer yoksa ağ isteği yapılmaz:

- `JARVIS_UPDATE_MANIFEST_URL`: imzalı `latest.json` URL’si.
- `JARVIS_UPDATE_ORIGIN`: yalnızca HTTPS origin, ör. `https://updates.example.com`.
- `JARVIS_UPDATE_PUBLIC_KEY_B64`: Ed25519 public key’in Base64 biçimi (32 bayt).

Özel imza anahtarı istemciye konmamalı veya Git’e eklenmemelidir. Bu değişiklik herhangi bir özel anahtar üretmez, saklamaz ya da yayınlamaz.

## Manifest sözleşmesi

Manifest tam olarak aşağıdaki alanları içerir. `signature`, diğer altı alanın UTF-8 canonical JSON gösteriminin Ed25519 imzasıdır (`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`). `source_url` yalnızca yapılandırılmış origin üzerindeki HTTPS bir URL olabilir. Desteklenen artifact türleri `.patch`, `.zip`, `.msi`, `.exe` ve `.whl` uzantılarıdır; bu kod hiçbirini çalıştırmaz.

```json
{
  "version": "25.1.1",
  "source_url": "https://updates.example.com/releases/jarvis-25.1.1.zip",
  "sha256": "<64 lowercase hex characters>",
  "size_bytes": 123456,
  "artifact_type": "zip",
  "release_notes": "Sürüm notları",
  "signature": "<base64 Ed25519 signature>"
}
```

## Şu anki yayın durumu

`https://jarvispatch-jttmazmp.manus.space` şu anda tam Windows JARVIS paketi değil, tek bir kalite-kapısı `.patch` dosyası sunuyor; imzalı `latest.json` ve imzalama anahtarı yapılandırılmış değil. Bu nedenle bu kod varsayılan olarak hiçbir dosya indirmez. Ayrıca mevcut patch’in temiz GitHub `main` üzerindeki `git apply --check` doğrulaması başarısız olmuştur. Patch’i herhangi bir kurulumda otomatik uygulamayın; önce hedef çalışma ağacında ayrıca doğrulayın.
