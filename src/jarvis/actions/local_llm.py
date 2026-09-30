"""
local_llm.py — Gemini kota/devre kesici yüzünden kullanılamaz olduğunda
devreye giren SON ÇARE yerel düşme (fallback) katmanı.

NEDEN VAR: 'gemini-selfimprove' devre kesicisi API kota aşımından (429
RESOURCE_EXHAUSTED) açıldığında self_improve/agent_loop/discovery/
entegrasyon/recall_conversation gibi arka plan işlevleri TAMAMEN duruyordu.
Kullanıcının açıkça istediği: "Gemini kota bitince otomatik Ollama'ya
düşsün". health_check.py'nin ZATEN test ettiği aynı Ollama uç noktasını
(http://localhost:11434) kullanır — ayrı bir varsayım/health-check icat
etmiyoruz.

TASARIM SINIRI: Bu SADECE bir fallback — birincil model DEĞİL. Gemini her
zaman ÖNCE denenir (mevcut devre kesici/backoff davranışıyla hiçbir şey
değişmez). Ollama SADECE Gemini'nin TÜM denemeleri başarısız olduğunda
devreye girer. Ollama kurulu/çalışır değilse (health_check.py'nin de
tespit ettiği durum), orijinal Gemini hatası hiçbir şey gizlenmeden olduğu
gibi fırlatılır — sessiz bir başarısızlık YOKTUR, çağıran taraf her zaman
gerçek durumu öğrenir (bkz. resilience.py'nin aynı ilkesi).

NOT: dev_agent.py'nin kendi Ollama entegrasyonu (Ollama'yı HER ZAMAN ÖNCE
dener, hızlı kodlama görevleri için bilinçli bir tercih) buradan ayrıdır ve
kasıtlı olarak değiştirilmedi — burası SADECE Gemini başarısız olunca devreye
giren tersi bir öncelik sırası uyguluyor.
"""
from __future__ import annotations

OLLAMA_BASE = "http://localhost:11434"
# dev_agent.py'nin de kullandigi model ilk tercih - kuruluysa aninda
# kullanilir; degilse (health_check.py'nin tespit ettigi gibi baska bir
# model kurulu olabilir) kurulu olan ILK modele duseriz - hicbir zaman
# "model yok" diye sessizce basarisiz olmayiz, once GERCEKTEN neyin kurulu
# oldugunu sorariz.
# Bu fallback'i kullanan cagiranlarin (self_improve, agent_loop, discovery,
# entegrasyon) hepsi KOD YAZMA/TEKNIK KARAR gorevleri - rastgele sohbet degil.
# Kucuk genel-amacli qwen3.5:0.8b hizli ama Turkce dahil pek cok durumda
# tutarsiz/anlamsiz cevaplar uretiyor (2026-09-21'de elle, dogrudan Ollama
# API'sine istek atilarak test edildi - bir log dosyasina kaydedilmedi, bu
# yorum o testin ozeti). Kod-odakli qwen2.5-coder:7b hem dogru kod
# uretti hem de SOGUK yuklemede bile 30sn butcesinin cok altinda kaldi
# (Windows + NVIDIA GPU'da olculdu: load_duration ~6.6s, toplam ~9s, ollama ps
# "100% GPU" gosterdi). Bu yuzden coder modelleri once denenir; kucuk modeller
# sadece coder modeli hic kurulu degilse devreye girer. NOT: bu olcum GPU'lu
# bir makinede yapildi - GPU'suz/zayif bir makinede 7B model 30sn'yi asabilir,
# boyle bir ortamda bu sira tekrar gozden gecirilmeli.
_PREFERRED_MODELS = (
    "qwen2.5-coder:7b", "qwen2.5-coder",
    "qwen3.5:0.8b", "qwen3.5:2b", "qwen3.5:4b", "llama3.1", "llama3",
)


def _list_ollama_models() -> list[str]:
    import requests
    resp = requests.get(f"{OLLAMA_BASE}/api/tags", timeout=3)
    resp.raise_for_status()
    return [m["name"] for m in resp.json().get("models", [])]


def _pick_ollama_model() -> str | None:
    try:
        models = _list_ollama_models()
    except Exception:
        return None
    if not models:
        return None
    for preferred in _PREFERRED_MODELS:
        if preferred in models:
            return preferred
    return models[0]  # hicbiri tam eslesmezse, kurulu olan ilk modeli kullan


def ollama_generate(prompt: str, timeout: float = 120.0) -> str | None:
    """Başarılıysa düz metin, Ollama kullanılamazsa/hata verirse None döner
    — ASLA exception fırlatmaz (çağıran taraf zaten elinde tuttuğu asıl
    Gemini hatasını bununla kaybetmesin diye)."""
    model = _pick_ollama_model()
    if not model:
        return None
    try:
        import requests
        resp = requests.post(
            f"{OLLAMA_BASE}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "think": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        text = (resp.json().get("response", "") or "").strip()
        return text or None
    except Exception as e:
        print(f"[LocalLLM] ⚠️ Ollama denemesi başarısız ({model}): {e}")
        return None


# Canlı log 2026-09-30: Gemini ücretsiz API kotası (günde 20) dolunca arka plan
# işleri (keşif, agent_loop…) her çağrıda 30-40 sn yeniden deniyor ve logu kırmızı
# hatalarla dolduruyordu. Kota dolunca 6 saat Gemini hiç denenmez; önce Groq
# (anahtar kayıtlıysa), sonra yerel Ollama kullanılır.
_GEMINI_QUOTA_UNTIL = 0.0


def cloud_generate(prompt: str) -> str | None:
    """Kayıtlı ücretsiz bulut modeli (Groq) ile metin; yoksa/başarısızsa None."""
    try:
        from jarvis.actions.dev_agent import CLOUD_LLM_BACKGROUND_MODEL, _CloudLLM, _cloud_llm
        cfg = _cloud_llm(prefer=CLOUD_LLM_BACKGROUND_MODEL)   # büyük modelin kotası koda kalsın
        if not cfg:
            return None
        text = _CloudLLM(*cfg, fallback=None, timeout=90).generate_content(prompt).text
        return (text or "").strip() or None
    except Exception as e:  # noqa: BLE001
        print(f"[LocalLLM] ⚠️ Bulut modeli denemesi başarısız: {type(e).__name__}")
        return None


def generate_with_fallback(
    gemini_call,
    prompt_for_ollama: str,
    source: str = "?",
    ollama_timeout: float = 120.0,
) -> str:
    """gemini_call: parametre almayan, başarılı olursa '.text' alanlı bir
    yanıt nesnesi dönen (ör. call_with_resilience(...) sonucu) bir fonksiyon.

    Önce gemini_call() çağrılır. Herhangi bir şekilde başarısız olursa
    (devre kesici açık, kota aşımı, ağ hatası — hepsi aynı şekilde ele
    alınır, çünkü çağıran taraf için hepsi 'şu an Gemini kullanılamıyor'
    anlamına gelir) yerel Ollama denenir. O da başarısız olursa ORİJİNAL
    Gemini hatası hiçbir şey değiştirilmeden tekrar fırlatılır — böylece
    hem bu fonksiyonu hiç bilmeyen eski çağıranlar hem de loglar gerçek
    hatayı görmeye devam eder."""
    global _GEMINI_QUOTA_UNTIL
    import time as _time
    if _time.time() < _GEMINI_QUOTA_UNTIL:
        # Günlük kota zaten doldu: 30-40 sn'lik yeniden denemeleri hiç başlatma.
        gemini_error: Exception = RuntimeError("Gemini günlük kotası dolu (önceki denemeden biliniyor)")
    else:
        try:
            response = gemini_call()
            return (getattr(response, "text", None) or "").strip()
        except Exception as e:  # noqa: BLE001
            gemini_error = e
            msg = str(e)
            if any(k in msg for k in ("RESOURCE_EXHAUSTED", "429", "quota")):
                _GEMINI_QUOTA_UNTIL = _time.time() + 6 * 3600
                print(f"[LocalLLM] ℹ️ Gemini günlük kotası doldu; arka plan işleri 6 saat Groq/yerel modelle "
                      f"yapılacak ('{source}').")
    text = cloud_generate(prompt_for_ollama)
    if text:
        print(f"[LocalLLM] ☁️ '{source}': Gemini yerine ücretsiz bulut modeli (Groq) kullanıldı.")
        return text
    text = ollama_generate(prompt_for_ollama, timeout=ollama_timeout)
    if text is not None:
        print(f"[LocalLLM] ℹ️ '{source}': Gemini kullanılamadı, yerel Ollama'ya düşüldü.")
        return text
    raise gemini_error
