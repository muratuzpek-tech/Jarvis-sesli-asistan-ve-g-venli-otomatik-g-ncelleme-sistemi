#!/usr/bin/env python3
"""JARVIS ses profili: JARVIS yalnız sizin sesinize cevap versin.

Kullanım (proje klasöründe):
    .venv/bin/python scripts/ses_kaydi.py          # sesinizi kaydeder (3 cümle, ~1 dk)
    .venv/bin/python scripts/ses_kaydi.py --dene   # 4 sn konuşun: "siz" mi, "başkası" mı?
    .venv/bin/python scripts/ses_kaydi.py --sil    # profili siler (JARVIS her sesi dinler)
    .venv/bin/python scripts/ses_kaydi.py --esik 0.5   # benzerlik eşiğini elle ayarla

Ses kaydı ve ses izi YALNIZ bu bilgisayarda kalır (~/.local/share/MuratJARVIS/voice,
yalnız sizin okuyabileceğiniz izinlerle). Ham ses dosyası saklanmaz, yalnız ses izi
(256 sayı) saklanır. Mikrofon olarak JARVIS'in kullandığı mikrofon seçilir.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jarvis.core import voice_gate as vg  # noqa: E402

CUMLELER = [
    "Hey Jarvis, bugün hava nasıl olacak, dışarı çıkmadan önce bilmek istiyorum.",
    "Masaüstündeki dosyaları türlerine göre klasörlere ayır ve bana kısa bir rapor ver.",
    "Hey Jarvis, yarın sabah dokuzda toplantım var, bana yarım saat önce hatırlat.",
]
EN_AZ_KONUSMA_SN = 1.5


def jarvis_mikrofonu(cihaz: int | None) -> int | None:
    """JARVIS'in seçtiği mikrofon (ayarlardaki tercih dahil); bulunamazsa sistem varsayılanı."""
    if cihaz is not None:
        return cihaz
    try:
        from jarvis.actions.audio_devices import best_candidate
        idx, _ = best_candidate("input")
        return idx if isinstance(idx, int) else None
    except Exception:  # noqa: BLE001
        return None


def kaydet(saniye: float, cihaz: int | None) -> np.ndarray:
    import sounddevice as sd
    try:
        sd.check_input_settings(device=cihaz, samplerate=vg.SAMPLE_RATE, channels=1, dtype="int16")
        rate = vg.SAMPLE_RATE
    except Exception:  # noqa: BLE001
        rate = int(sd.query_devices(cihaz, "input")["default_samplerate"])
    ses = sd.rec(int(saniye * rate), samplerate=rate, channels=1, dtype="int16", device=cihaz)
    sd.wait()
    ses = ses.reshape(-1)[int(0.25 * rate):]      # açılış anındaki çıt sesini at
    if rate != vg.SAMPLE_RATE:
        n = int(len(ses) * vg.SAMPLE_RATE / rate)
        ses = np.interp(np.linspace(0, len(ses) - 1, n), np.arange(len(ses)), ses).astype(np.int16)
    return ses


def konusma(ses: np.ndarray) -> tuple[np.ndarray, str]:
    """(yalnız konuşma kısmı, ekrana yazılacak seviye bilgisi)."""
    kisim = vg.voiced(ses)
    tepe = int(np.abs(ses).max(initial=0))
    sn = len(kisim) / vg.SAMPLE_RATE
    tavan = float(np.mean(np.abs(kisim.astype(np.int32)) >= 32000)) * 100 if kisim.size else 0.0
    bilgi = f"konuşma {sn:.1f} sn, en yüksek seviye {tepe}"
    if tavan >= 0.5:
        bilgi += (f" — ⚠️ sesin %{tavan:.1f}'i TAVANA VURUYOR (bozuluyor): "
                  "Ayarlar → Ses → Giriş seviyesini ~%60'a indirin")
    return kisim, bilgi


def profil_kaydet(cihaz: int | None) -> int:
    import sounddevice as sd
    cihaz = jarvis_mikrofonu(cihaz)
    try:
        ad = sd.query_devices(cihaz, "input")["name"]
    except Exception:  # noqa: BLE001
        ad = "sistem varsayılanı"
    print(f"Mikrofon: {ad}" + (f" (#{cihaz})" if cihaz is not None else ""))
    print("Ses modeli hazırlanıyor…")
    vg.ensure_wake_model()
    dogrulayici = vg.SpeakerVerifier(vg.ensure_speaker_model())
    izler = []
    for i, cumle in enumerate(CUMLELER, 1):
        while True:
            input(f"\n[{i}/{len(CUMLELER)}] Enter'a basın ve HEMEN normal sesinizle okuyun (8 sn):\n"
                  f"   « {cumle} »\n")
            print("   🎙️  Kaydediliyor…")
            kisim, bilgi = konusma(kaydet(8.0, cihaz))
            print(f"   ({bilgi})")
            if len(kisim) < EN_AZ_KONUSMA_SN * vg.SAMPLE_RATE:
                print("   ⚠️  Yeterli konuşma duyulmadı. Mikrofona yaklaşıp Enter'dan hemen sonra okuyun.")
                continue
            izler.append(dogrulayici.embed(vg.normalized(kisim)))
            print("   ✅ Alındı.")
            break
    profil = np.mean(izler, axis=0)
    profil /= np.linalg.norm(profil) or 1.0
    # Tutarlılık: her kaydın, diğer ikisinin ortalamasına benzerliği.
    tutarlilik = []
    for i, iz in enumerate(izler):
        digerleri = np.mean([x for j, x in enumerate(izler) if j != i], axis=0)
        tutarlilik.append(vg.similarity(iz, digerleri))
    print(f"\nKayıtlarınızın birbirine benzerliği: {', '.join(f'{t:.2f}' for t in tutarlilik)}")
    kisa = []
    while True:
        input("\nSon adım: Enter'a basın ve hemen JARVIS'e konuşur gibi deyin (4 sn):\n"
              "   « Hey Jarvis, saat kaç, bugün hava nasıl? »\n")
        kisim, bilgi = konusma(kaydet(4.0, cihaz))
        print(f"   ({bilgi})")
        if len(kisim) >= 1.2 * vg.SAMPLE_RATE:
            kisa.append(vg.similarity(dogrulayici.embed(vg.normalized(kisim)), profil))
            break
        print("   ⚠️  Konuşma duyulmadı, tekrar deneyin.")
    kendi = min([*kisa, *tutarlilik])
    # Eşik: kendi sesinizin en düşük benzerliğinin biraz altı (0.60'ta tavan YOK:
    # 2026-09-30'da TV 0.64 alıp "siz" sayıldı).
    esik = round(float(min(0.80, max(0.45, kendi - 0.12))), 2)
    vg.VOICE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(vg.VOICE_DIR, 0o700)
    with open(vg.PROFILE_PATH, "wb") as fh:
        np.save(fh, profil.astype(np.float32))
    os.chmod(vg.PROFILE_PATH, 0o600)
    vg.save_settings({"benzerlik_esigi": esik})
    print(f"\n✅ Ses profili kaydedildi. JARVIS'e konuşma benzerliği: {kisa[0]:.2f} → eşik {esik:.2f}")
    if kendi < 0.45:
        print("⚠️  Kayıtlarınız birbirine az benziyor (mikrofon kısık ya da ortam gürültülü olabilir).\n"
              "   Daha sessiz bir ortamda ve mikrofona yakın yeniden kaydetmeniz iyi olur.")
    print("   Denemek için: .venv/bin/python scripts/ses_kaydi.py --dene")
    print("   Sonra JARVIS'i yeniden başlatın ve 'Hey Jarvis' diye başlayın.")
    return 0


def dene(cihaz: int | None, kez: int = 3) -> int:
    if not vg.PROFILE_PATH.is_file():
        print("Önce profil kaydedin: .venv/bin/python scripts/ses_kaydi.py")
        return 1
    cihaz = jarvis_mikrofonu(cihaz)
    dogrulayici = vg.SpeakerVerifier(vg.ensure_speaker_model())
    profil = np.load(vg.PROFILE_PATH)
    esik = vg.settings()["benzerlik_esigi"]
    print(f"Eşik: {esik:.2f}. JARVIS gibi değerlendirilir: 'Hey Jarvis' + komutun başı (~2.5 sn).")
    sonuclar = []
    for i in range(1, kez + 1):
        kim = input(f"\n[{i}/{kez}] Kim konuşacak? (s = siz, t = TV/başkası) ve Enter, sonra HEMEN konuşun (4 sn): ")
        kisim, bilgi = konusma(kaydet(4.0, cihaz))
        print(f"   ({bilgi})")
        if len(kisim) < 0.8 * vg.SAMPLE_RATE:
            print("   Konuşma duyulmadı; bu deneme sayılmadı.")
            continue
        sim = vg.similarity(dogrulayici.embed(vg.normalized(kisim[: int(2.5 * vg.SAMPLE_RATE)])), profil)
        karar = "✅ SİZ" if sim >= esik else "🚫 BAŞKASI"
        etiket = "siz" if kim.strip().lower().startswith("s") else "TV/başkası"
        print(f"   Benzerlik: {sim:.2f} → {karar}   (gerçekte: {etiket})")
        sonuclar.append((etiket, sim))
    siz = [x for e, x in sonuclar if e == "siz"]
    tv = [x for e, x in sonuclar if e != "siz"]
    print("\nÖzet:")
    if siz:
        print(f"   Siz:        {', '.join(f'{x:.2f}' for x in siz)}  (en düşük {min(siz):.2f})")
    if tv:
        print(f"   TV/başkası: {', '.join(f'{x:.2f}' for x in tv)}  (en yüksek {max(tv):.2f})")
    if siz and tv:
        if min(siz) > max(tv):
            oneri = round((min(siz) + max(tv)) / 2, 2)
            print(f"   İyi ayrışıyor. Önerilen eşik: {oneri:.2f}  →  .venv/bin/python scripts/ses_kaydi.py --esik {oneri:.2f}")
        else:
            print("   ⚠️ Sizin sesinizle TV karışıyor: mikrofon seviyesini düşürüp profili yeniden kaydedin.")
    return 0


def sil() -> int:
    for yol in (vg.PROFILE_PATH, vg.SETTINGS_PATH):
        if yol.exists():
            yol.unlink()
    print("Ses profili silindi. JARVIS yeniden başlayınca her sesi dinler.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dene", action="store_true", help="kayıtlı sesle karşılaştır")
    ap.add_argument("--sil", action="store_true", help="ses profilini sil")
    ap.add_argument("--esik", type=float, help="benzerlik eşiğini elle ayarla (ör. 0.5)")
    ap.add_argument("--cihaz", type=int, default=None, help="mikrofon numarası (varsayılan: JARVIS'in mikrofonu)")
    ap.add_argument("--kez", type=int, default=6, help="--dene ile kaç deneme yapılacak (varsayılan 6)")
    a = ap.parse_args()
    if a.sil:
        return sil()
    if a.esik is not None:
        vg.save_settings({"benzerlik_esigi": float(a.esik)})
        print(f"Eşik {a.esik:.2f} olarak kaydedildi.")
        return 0
    if a.dene:
        return dene(a.cihaz, a.kez)
    return profil_kaydet(a.cihaz)


if __name__ == "__main__":
    raise SystemExit(main())
