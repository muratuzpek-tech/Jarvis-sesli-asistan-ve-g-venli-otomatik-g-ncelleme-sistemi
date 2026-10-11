"""
base_brain.py — JARVIS Çoklu Beyin Takımı'ndaki her "beyin"in ortak temeli.

TASARIM SINIRI (kullanıcı talimatı, 2026-09-15):
Her beyin kendi sistem promptuna, kendi görevine, kendi karar mekanizmasına,
kendi çalışma durumuna, kendi hata yönetimine ve kendi giriş/çıkış formatına
sahip olmalı; beyinler birbirinin görevini ÜSTLENMEMELİ. Bu dosya bu ortak
iskeleti sağlar, ama HİÇBİR alt sınıfın davranışını gizlice değiştirmez.

SÜREÇ MODELİ (bilinçli tercih, kullanıcıyla netleştirildi): Talimatta "her
beyin ayrı bir işletim sistemi process'i olabilmeli, biri çökerse Jarvis
çökmemeli" deniyor. Bunu canlı test edemeden (bu geliştirme uzaktan, cihazda
kabuk erişimi olmadan yapıldı) doğrudan multiprocessing/IPC ile yazmak ilk
seferde çalışmama riski taşıyordu. Bunun yerine İLK FAZ olarak: her beyin
MANTIKSAL olarak tam bağımsız (kendi durumu, kendi hata sınırı, birbirinin
işini asla yapmıyor) ama aynı process içinde çalışan bir nesne olarak
tasarlandı — tıpkı halihazırda kanıtlanmış agent_loop.py'nin arka plan
döngüsü gibi. Bir beyin içindeki hata try/except ile o beyne hapsedilir,
Jarvis'in geri kalanına ASLA sızmaz (bkz. call() metodu). Gerçek OS-seviyesi
process ayrımı, bu faz kanıtlandıktan sonra 2. faz olarak eklenebilir.

GEMINI KULLANIMI: Her beyin actions/resilience.py'nin ZATEN VAR olan
call_with_resilience() + actions/local_llm.py'nin generate_with_fallback()
mekanizmasını kullanır - agent_loop.py ve entegrasyon.py'nin de kullandığı
AYNI kanıtlanmış desen. Böylece Gemini kota/kesinti durumunda (bu oturumda
gerçekten yaşandı) otomatik olarak yerel Ollama'ya düşülür, ayrı bir hata
yönetimi icat edilmez. API anahtarı ÖNCE ortam değişkeni GEMINI_API_KEY'den,
yoksa mevcut config/api_keys.json'dan okunur (mevcut sistemi bozmadan,
kullanıcının "hiçbir Python dosyasına sabit yazılmamalı" talimatına uyar).
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import sys
import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.actions.resilience import CircuitBreaker, call_with_resilience
from jarvis.actions.local_llm import generate_with_fallback
from jarvis.paths import logs_dir
from jarvis.core.secure_config import api_keys_path, get_gemini_api_key


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _get_base_dir()
LOGS_DIR = logs_dir()
API_KEYS_PATH = api_keys_path()

DEFAULT_MODEL = "gemini-flash-latest"
MAX_RETRIES_DEFAULT = 2  # bir beynin kendi Gemini cagrisi icin (agent_loop ile tutarli)

# Kullanici talimati: "Timeout sistemi - Agent sonsuza kadar beklememeli."
# actions/resilience.py'nin call_with_resilience() fonksiyonu SADECE
# exception FIRLATILDIKTAN SONRAKI tekrar denemeleri yonetiyor - eger
# model.generate_content() cagrisinin kendisi (agsal bir sorun yuzunden)
# hicbir exception firlatmadan sonsuza kadar asili kalirsa, bu deseni
# YAKALAYAMAZ. _run_with_timeout() bu bosluğu dolduruyor: cagriyi ayri bir
# thread'de calistirip sabit bir sure sonra vazgeciyor (Python bir thread'i
# GERCEKTEN oldurmuyor - o thread arka planda calismaya devam edebilir -
# ama caginan taraf ARTIK BEKLEMIYOR, TimeoutError aliyor ve
# call_with_resilience bunu 'unknown'/retryable olarak isleyip normal
# backoff + Ollama fallback zincirine sokuyor - bkz. asagidaki call_llm()).
LLM_CALL_TIMEOUT_SECONDS = 45.0
OLLAMA_FALLBACK_TIMEOUT_SECONDS = 30.0


def _run_with_timeout(fn, timeout: float = LLM_CALL_TIMEOUT_SECONDS):
    """fn()'i sabit bir sure icinde bekler; sure asilirsa TimeoutError
    firlatir (fn'in kendisi arka planda calismaya devam edebilir - Python
    bir thread'i disaridan guvenle olduremez - ama caginan taraf sonsuza
    kadar BEKLEMEZ, bu da kullanicinin '1. Timeout sistemi' talebinin
    ozeti). Tek-kullanimlik bir ThreadPoolExecutor kullanilir - her cagri
    kendi executor'unu acip kapatir, brain basina kalici bir thread havuzu
    TUTULMAZ."""
    # ONEMLI: 'with ... as pool:' KULLANILMIYOR - o deyimin __exit__'i
    # pool.shutdown(wait=True) cagirir, yani zaman asimina ugrayan (hala
    # arka planda calisan) is bitene kadar YINE BEKLERDI - tam da onlemeye
    # calistigimiz "sonsuza kadar bekleme" sorununu __exit__ asamasinda
    # geri getirirdi. wait=False ile caginan taraf ANINDA serbest kalir;
    # terk edilen thread kendi basina (arka planda, sonucu artik kimsenin
    # okumadigi halde) calismaya devam edip normal sekilde biter.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as e:
        raise TimeoutError(
            f"Çağrı {timeout:.1f} saniye içinde tamamlanmadı (sonsuza kadar beklenmedi)."
        ) from e
    finally:
        pool.shutdown(wait=False)


# ── AI FIX 1+3+4: Retry + Error Classification + Metrics ──────────────────
# 2026-10-03: Geçici hatalar (network flicker, 429 rate-limit, 503 overload)
# tek denemede LLM çağrısını öldürüyordu. Artık akıllı retry var.

import time as _time
from collections import defaultdict as _defaultdict

_llm_metrics: dict = _defaultdict(lambda: {
    "calls": 0, "successes": 0, "failures": 0,
    "retries": 0, "total_time_s": 0.0,
})


def _is_transient_error(e: Exception) -> bool:
    """Geçici hata mı? Retry mantıklı mı? 
    
    TRANSIENT (retry et):
    - TimeoutError (ağ geçikmesi, LLM yavaş)
    - ConnectionError / ConnectionResetError (network flicker)
    - BrokenPipeError / EOFError (bağlantı kesildi)
    - OSError: connection-related errno'lar
    
    PERMANENT (retry ETME, HEMEN fırlat):
    - ValueError / TypeError / KeyError (kod hatası)
    - json.JSONDecodeError (bozuk yanıt)
    - KeyboardInterrupt / SystemExit
    - MemoryError
    """
    # Permanent — asla retry
    if isinstance(e, (ValueError, TypeError, KeyError, AttributeError,
                      SyntaxError, ImportError, MemoryError,
                      KeyboardInterrupt, SystemExit)):
        return False
    try:
        import json as _json
        if isinstance(e, _json.JSONDecodeError):
            return False
    except ImportError:
        pass
    # HTTP durum kodları — sadece geçici olanlar
    try:
        import requests as _req
        if isinstance(e, _req.exceptions.HTTPError):
            resp = getattr(e, 'response', None)
            status = getattr(resp, 'status_code', 0) if resp else 0
            return status in (408, 425, 429, 500, 502, 503, 504)
        if isinstance(e, (_req.exceptions.ConnectionError,
                          _req.exceptions.Timeout)):
            return True
    except ImportError:
        pass
    # Transient OS seviyesi
    return isinstance(e, (TimeoutError, ConnectionError,
                          ConnectionResetError, BrokenPipeError,
                          EOFError))


def call_with_retry(
    fn,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 15.0,
    timeout: float | None = None,
    label: str = "LLM-call",
):
    """fn'yi _run_with_timeout ile sarar ve geçici hatalarda exponential
    backoff ile yeniden dener.
    
    Kullanim:
        result = call_with_retry(lambda: client.models.generate_content(...))
    
    Davranış:
    - Geçici hata: 3 deneme (1s → 2s → 4s backoff)
    - Permanent hata: HEMEN fırlat (retry zaman kaybı)
    - Tüm denemeler başarısız: son hata fırlatılır
    - Her çağrı metrik kaydı üretir
    """
    if timeout is None:
        timeout = LLM_CALL_TIMEOUT_SECONDS
    start = _time.monotonic()
    metrics = _llm_metrics[label]
    metrics["calls"] += 1

    for attempt in range(max_retries):
        try:
            result = _run_with_timeout(fn, timeout=timeout)
            metrics["successes"] += 1
            metrics["total_time_s"] += _time.monotonic() - start
            return result
        except Exception as e:
            is_last = attempt == max_retries - 1
            transient = _is_transient_error(e)
            
            if not transient:
                metrics["failures"] += 1
                metrics["total_time_s"] += _time.monotonic() - start
                raise
            
            if is_last:
                metrics["failures"] += 1
                metrics["total_time_s"] += _time.monotonic() - start
                print(f"[BaseBrain] {label}: TÜM {max_retries} deneme başarısız.")
                raise
            
            delay = min(base_delay * (2 ** attempt), max_delay)
            metrics["retries"] += 1
            err_type = type(e).__name__
            print(f"[BaseBrain] {label}: deneme {attempt + 1}/{max_retries} "
                  f"başarısız ({err_type}: {e})")
            print(f"[BaseBrain] {label}: {delay:.1f}sn sonra yeniden denenecek...")
            _time.sleep(delay)


def get_llm_metrics(label: str | None = None) -> dict:
    """LLM çağrı metriklerini döndürür. Tümü veya tek etiket için."""
    if label:
        return dict(_llm_metrics.get(label, {}))
    return {k: dict(v) for k, v in _llm_metrics.items()}


def reset_llm_metrics():
    """Metrikleri sıfırla (test/debug için)."""
    _llm_metrics.clear()


def _get_api_key() -> str:
    # Once GEMINI_API_KEY ortam degiskeni, sonra kullanici veri dizinindeki
    # api_keys.json (bkz. jarvis.core.secure_config).
    return get_gemini_api_key()


def _get_client():
    from google import genai
    return genai.Client(api_key=_get_api_key())


def _strip_fences(text: str) -> str:
    text = re.sub(r"```(?:json)?", "", text).strip()
    return text.rstrip("`").strip()


# 2026-09-17 tespit edildi (bkz. logs/coder_ai.log, görev 88023dc1): model
# bazen büyük/çok satırlı bir string değeri (ör. bir .py dosyasının tüm
# içeriği) standart JSON kaçışı yerine Python'un üçlü-tırnak (\"\"\")
# sözdizimiyle sarmalıyor - özellikle hedef dosyanın kendisi zaten bir
# üçlü-tırnaklı docstring ile başlıyorsa, model bu sözdizimini JSON çıktısına
# da yansıtıyor. Bu GEÇERSİZ JSON'dur (json.loads başarısız olur) ama
# tutarlı, tanınabilir bir kalıptır. Bu fonksiyon SADECE bu spesifik kalıbı
# (herhangi bir JSON anahtarı için, şemaya bağımlı olmadan) gerçek, düzgün
# kaçışlanmış JSON string'ine çevirir - belirsiz/tanınmayan durumlarda None
# döner ki çağıran orijinal hatayı olduğu gibi raporlayabilsin. call_llm_json
# zaten json.loads BAŞARILI olduğunda bu fonksiyonu hiç ÇAĞIRMAZ - yani
# normal, geçerli JSON döndüren çağrılarda davranış hiç değişmez.
_TRIPLE_QUOTE_KEY_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:\s*"""')


def _repair_triple_quoted_json(text: str) -> str | None:
    matches = list(_TRIPLE_QUOTE_KEY_RE.finditer(text))
    if not matches:
        return None
    parts: list[str] = []
    cursor = 0
    for i, m in enumerate(matches):
        parts.append(text[cursor:m.start()])
        key = m.group(1)
        value_start = m.end()
        search_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        segment = text[value_start:search_end]
        # Degerin kapanisi: bir sonraki anahtardan (veya metnin sonundan)
        # HEMEN ONCEKI son \"\"\" - modelin degeri fiilen kapattigi nokta.
        close_idx = segment.rfind('"""')
        if close_idx == -1:
            return None  # taninmayan bicim - guvenli sekilde vazgec
        raw_value = segment[:close_idx]
        parts.append(f'"{key}": {json.dumps(raw_value)}')
        cursor = value_start + close_idx + 3
    parts.append(text[cursor:])
    return "".join(parts)


def validate_llm_json(
    parsed: dict | list,
    required_keys: list[str] | None = None,
    max_depth: int = 20,
    max_string_len: int = 500_000,
    max_list_len: int = 10_000,
    max_dict_keys: int = 500,
) -> tuple[bool, str]:
    """LLM'den dönen JSON'u doğrular — execute edilmeden ÖNCE kalite kontrolü.

    Kontrol edilenler:
    - Tip doğruluğu (dict beklenen yerde dict mi?)
    - Required key'ler var mı?
    - İç içe derinlik makul mü? (stack overflow koruması)
    - String uzunluğu makul mü? (OOM koruması)  
    - Liste/dict boyutu makul mü? (resource koruması)
    - Null byte / control char sızıntısı var mı? (enjeksiyon koruması)
    
    Döner: (basarili: bool, mesaj: str)
    Başarısızsa mesaj NE bozuk olduğunu açıklar (debug kolaylığı).
    """

    problems: list[str] = []

    # Type check
    if required_keys is not None and isinstance(parsed, dict):
        missing = [k for k in required_keys if k not in parsed]
        if missing:
            problems.append(f"Eksik key'ler: {missing}")

    def _walk(obj, depth=0, path="$"):
        if depth > max_depth:
            problems.append(f"Derinlik limiti asildi ({depth}): {path}")
            return
        if isinstance(obj, dict):
            if len(obj) > max_dict_keys:
                problems.append(f"Cok fazla dict key ({len(obj)}): {path}")
            for k, v in obj.items():
                ks = str(k)
                if any(ord(c) < 32 and c not in '\t\n\r' for c in ks):
                    problems.append(f"Control char in key: {path}.{ks[:50]}")
                _walk(v, depth + 1, f"{path}.{ks[:30]}")
        elif isinstance(obj, list):
            if len(obj) > max_list_len:
                problems.append(f"Cok uzun liste ({len(obj)}): {path}")
            for i, v in enumerate(obj[:100]):  # first 100 for speed
                _walk(v, depth + 1, f"{path}[{i}]")
        elif isinstance(obj, str):
            if len(obj) > max_string_len:
                problems.append(f"Cok uzun string ({len(obj)}): {path}")
            if "\x00" in obj:
                problems.append(f"Null byte detected: {path}")
        elif isinstance(obj, float):
            import math
            if math.isnan(obj) or math.isinf(obj):
                problems.append(f"NaN/Inf value: {path}")

    _walk(parsed)

    if problems:
        summary = "; ".join(problems[:5])
        if len(problems) > 5:
            summary += f" (+{len(problems) - 5} daha)"
        return False, f"QUALITY GATE RED: {summary}"
    
    return True, "Kalite kontrolü gecti."


def make_message(from_agent: str, to_agent: str, task: str, payload: dict | None = None,
                  priority: str = "medium") -> dict:
    """11. MESSAGE BUS bölümündeki şemaya birebir uyan bir istek mesajı üretir."""
    return {
        "message_id": uuid.uuid4().hex,
        "from": from_agent,
        "to": to_agent,
        "task": task,
        "priority": priority,
        "created_at": datetime.now().isoformat(),
        "payload": payload or {},
    }


class BrainError(Exception):
    """Bir beynin KENDİ İÇİNDE yakalayamadığı, çağırana bildirmesi gereken
    hata. Orchestrator bunu HER ZAMAN yakalar - hiçbir beyin hatası Jarvis'in
    tamamını çökertmez (20. HATA YÖNETİMİ maddesi)."""


class BaseBrain(ABC):
    """Her 'beyin'in miras aldığı ortak iskelet. Alt sınıflar SADECE
    SYSTEM_PROMPT'u ve handle()'ı tanımlar - Gemini çağırma, loglama, hata
    yakalama, durum takibi burada, TEK YERDE, tutarlı şekilde yapılır."""

    NAME: str = "base_brain"          # alt sinif override eder
    SYSTEM_PROMPT: str = ""           # alt sinif override eder
    MAX_RETRIES: int = MAX_RETRIES_DEFAULT

    def __init__(self) -> None:
        self.status = "idle"          # idle | running | error
        self.last_error: str | None = None
        self._breaker = CircuitBreaker(
            name=f"gemini-brain-{self.NAME}", failure_threshold=3, cooldown_seconds=45.0,
        )
        self._logger = self._make_logger()
        # Kullanici talimati: "Heartbeat / health check - Agent gercekten
        # calisiyor mu, yoksa takildi mi anlasilmali." status ZATEN vardi
        # ama NE ZAMANDAN BERI o durumda oldugu hicbir yerde tutulmuyordu -
        # bu yuzden "running" durumu, 2 saniye once mi yoksa 2 saat once mi
        # basladi ayirt edilemiyordu. _state_changed_at bunu cozuyor.
        self._state_changed_at = time.monotonic()

    def _set_status(self, status: str) -> None:
        self.status = status
        self._state_changed_at = time.monotonic()

    def heartbeat(self) -> dict:
        """Bu beynin dışarıdan sorulabilecek 'nabzı': şu an ne durumda ve bu
        durumda ne kadar süredir. core/brain_orchestrator.get_team_health()
        bunu her beyin için toplayıp tek bir sağlık raporu üretir."""
        return {
            "name": self.NAME,
            "status": self.status,
            "seconds_in_state": round(time.monotonic() - self._state_changed_at, 1),
            "last_error": self.last_error,
        }

    # ── Loglama ──────────────────────────────────────────────────────────
    def _make_logger(self) -> logging.Logger:
        logger = logging.getLogger(f"jarvis.brain.{self.NAME}")
        if not logger.handlers:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(LOGS_DIR / f"{self.NAME}.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        return logger

    def log(self, msg: str, level: str = "info") -> None:
        getattr(self._logger, level, self._logger.info)(msg)

    # ── Gemini çağrısı (TÜM beyinler için ortak, tutarlı hata yönetimi) ───
    def call_llm(self, user_content: str, *, json_mode: bool = False) -> str:
        """Kendi sistem promptuyla Gemini'yi çağırır. Kota/ağ hatasında
        otomatik Ollama'ya düşer (actions/local_llm.py). BAŞARISIZ olursa
        BrainError fırlatır - ASLA sessizce None/boş dönmez, çağıran (her
        zaman kendi handle() metodu, try/except içinde) durumu bilir."""
        prompt = f"{self.SYSTEM_PROMPT}\n\n{user_content}"
        if json_mode:
            prompt += (
                "\n\nSADECE gecerli JSON don, markdown/aciklama/kod bloğu YOK. "
                "Çok satırlı bir metni (ör. bir kod dosyasının tam içeriği) JSON "
                "string alanına gömerken ASLA Python'un üçlü tırnak (\"\"\") "
                "sözdizimini kullanma - satır sonları için \\n, tırnak işaretleri "
                "için \\\" standart JSON kaçış dizilerini kullan."
            )

        def _call():
            client = _get_client()
            try:
                # "1. Timeout sistemi": ciplak SDK cagrisi degil, sabit sureli
                # bir sarmalayici cagriliyor - agin/servisin sessizce sonsuza
                # kadar asili kalmasi ihtimaline karsi (bkz. _run_with_timeout
                # dosya basi notu). TimeoutError, call_with_resilience'in zaten
                # bildigi 'unknown/retryable' yoluna dusuyor - ayri bir hata
                # yolu icat edilmedi.
                return _run_with_timeout(
                    lambda: client.models.generate_content(model=DEFAULT_MODEL, contents=prompt)
                )
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

        try:
            raw = generate_with_fallback(
                lambda: call_with_resilience(
                    _call, breaker=self._breaker, max_attempts=1,
                    base_delay=2.0, max_delay=15.0,
                ),
                prompt_for_ollama=prompt,
                source=f"brain-{self.NAME}",
                ollama_timeout=OLLAMA_FALLBACK_TIMEOUT_SECONDS,
            )
        except Exception as e:
            self.log(f"LLM çağrısı başarısız: {e}", level="error")
            raise BrainError(f"[{self.NAME}] Gemini/Ollama çağrısı başarısız: {e}") from e

        text = raw.strip()
        return _strip_fences(text) if json_mode else text

    def call_llm_json(self, user_content: str) -> dict | list:
        text = self.call_llm(user_content, json_mode=True)
        try:
            return json.loads(text)
        except Exception as e:
            # 2026-09-17: bilinen "üçlü tırnak (\"\"\") JSON" hatasını dene -
            # bkz. _repair_triple_quoted_json dosya başı notu. Onarım
            # başarısız/uygulanamaz olursa ORİJİNAL hata olduğu gibi
            # fırlatılır - burada hiçbir şey "sessizce" yutulmaz.
            repaired = _repair_triple_quoted_json(text)
            if repaired is not None:
                try:
                    result = json.loads(repaired)
                except Exception:
                    result = None
                if result is not None:
                    self.log(
                        f"[{self.NAME}] Model standart olmayan (\"\"\") JSON "
                        "string sözdizimi kullandı, otomatik onarıldı.",
                        level="warning",
                    )
                    return result
            raise BrainError(f"[{self.NAME}] Model gecerli JSON dondurmedi: {e} | ham: {text[:300]}") from e

    # ── Her alt sınıfın uygulaması gereken tek metod ──────────────────────
    @abstractmethod
    def handle(self, message: dict) -> dict:
        """message: message_bus.py şemasına uygun bir istek. Dönen değer,
        aynı şemadaki bir SONUÇ mesajı olmalı (status/result alanlarıyla).
        Bu metod SADECE kendi rolüne ait işi yapar - başka bir beynin işini
        asla üstlenmez (2-8. bölümlerdeki kural)."""

    # ── Dışarıdan çağrılan, HİÇBİR ZAMAN exception fırlatmayan sarmalayıcı ─
    def call(self, message: dict) -> dict:
        """Orchestrator/diğer beyinler SADECE bunu çağırır, handle()'ı değil.
        Böylece bir beynin içindeki HERHANGİ bir hata (Gemini hatası, araç
        hatası, beklenmeyen exception) burada yakalanır ve yapılandırılmış
        bir 'failed' sonucuna çevrilir - hiçbir zaman Jarvis'in tamamına
        sızmaz (20. HATA YÖNETİMİ maddesi)."""
        self._set_status("running")
        self.log(f"Görev alındı: {message.get('task', '')!r} (from={message.get('from')})")
        try:
            result = self.handle(message)
            self._set_status("idle")
            self.last_error = None
            return result
        except BrainError as e:
            self._set_status("error")
            self.last_error = str(e)
            self.log(str(e), level="error")
            return {
                "message_id": message.get("message_id"),
                "from": self.NAME, "to": message.get("from"),
                "status": "failed", "result": {"error": str(e)},
            }
        except Exception as e:  # beklenmeyen HER şey - beyni asla disariya cokertme
            self._set_status("error")
            self.last_error = str(e)
            self.log(f"Beklenmeyen hata: {e}", level="error")
            return {
                "message_id": message.get("message_id"),
                "from": self.NAME, "to": message.get("from"),
                "status": "failed", "result": {"error": f"Beklenmeyen hata: {e}"},
            }

    def ok(self, message: dict, result: Any) -> dict:
        """Alt sınıfların başarılı bir sonuç üretirken kullanacağı yardımcı."""
        return {
            "message_id": message.get("message_id"),
            "from": self.NAME, "to": message.get("from"),
            "status": "completed", "result": result,
        }
