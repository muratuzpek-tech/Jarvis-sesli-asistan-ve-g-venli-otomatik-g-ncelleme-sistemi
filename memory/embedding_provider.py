"""
memory/embedding_provider.py — Çok Katmanlı Embedding Sağlayıcı

Öncelik sırası:
  1. Ollama nomic-embed-text  → 768 boyut, hızlı, YEREL
  2. sentence-transformers    → 384 boyut, orta hız, pip install
  3. TF-IDF Hashing           → 256 boyut, instant, SIFIR bağımlılık

Hiçbir dış servise GEREK YOK → her koşulda çalışır.
"""
from __future__ import annotations

import hashlib
import math
import re
import logging

logger = logging.getLogger("memory.embedding")

# ── Global state ──────────────────────────────────────────────
_dim = 256  # default: TF-IDF hash boyutu
_backend = "tfidf"
_ollama_client = None
_st_model = None


def _init_ollama() -> bool:
    """Ollama embedding provider'ı başlat."""
    global _dim, _backend, _ollama_client
    try:
        import ollama
        _ollama_client = ollama.Client()
        # Test: embedding al
        test = _ollama_client.embed(model="nomic-embed-text", input="test")
        vec = test.get("embeddings", [[[]]])[0][0]
        if vec:
            _dim = len(vec)
            _backend = "ollama"
            logger.info(f"[Embedding] ✅ Ollama nomic-embed-text ({_dim}d)")
            return True
    except Exception as e:
        logger.info(f"[Embedding] ⚠️ Ollama yok ({type(e).__name__}) — sıradaki")
    return False


def _init_sentence_transformers() -> bool:
    """sentence-transformers provider'ı başlat."""
    global _dim, _backend, _st_model
    try:
        from sentence_transformers import SentenceTransformer
        _st_model = SentenceTransformer("all-MiniLM-L6-v2")
        _dim = _st_model.get_sentence_embedding_dimension()
        _backend = "sentence-transformers"
        logger.info(f"[Embedding] ✅ sentence-transformers ({_dim}d)")
        return True
    except Exception as e:
        logger.info(f"[Embedding] ⚠️ sentence-transformers yok ({type(e).__name__}) — TF-IDF fallback")
    return False


def _ensure_initialized() -> None:
    """En iyi mevcut backend'i seç."""
    global _backend
    if _backend != "tfidf":
        return  # zaten初始化ılmış
    if _init_ollama():
        return
    if _init_sentence_transformers():
        return
    _backend = "tfidf"
    logger.info(f"[Embedding] i️ TF-IDF hashing fallback aktif ({_dim}d)")


def get_embedding_dim() -> int:
    """Embedding vektör boyutu."""
    _ensure_initialized()
    return _dim


def get_backend() -> str:
    """Aktif embedding backend adı."""
    _ensure_initialized()
    return _backend


def embed_text(text: str) -> list[float]:
    """
    Metni embedding vektörüne dönüştür.
    
    Returns:
        list[float]: {_dim} boyutlu vektör
    """
    _ensure_initialized()

    if _backend == "ollama":
        try:
            resp = _ollama_client.embed(model="nomic-embed-text", input=text)
            return list(resp["embeddings"][0][0])
        except Exception as e:
            logger.warning(f"[Embedding] Ollama hatası → TF-IDF: {e}")

    if _backend == "sentence-transformers":
        try:
            vec = _st_model.encode(text, normalize_embeddings=True)
            return vec.tolist()
        except Exception as e:
            logger.warning(f"[Embedding] ST hatası → TF-IDF: {e}")

    return _tfidf_hash_embed(text)


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Toplu embedding (performans için)."""
    _ensure_initialized()

    if _backend == "ollama" and len(texts) > 1:
        try:
            resp = _ollama_client.embed(model="nomic-embed-text", input=texts)
            return [list(e) for e in resp["embeddings"]]
        except Exception:
            pass

    return [embed_text(t) for t in texts]


# ── TF-IDF Hashing Fallback ──────────────────────────────────

def _tfidf_hash_embed(text: str, dim: int = 512) -> list[float]:
    """
    Sıfır bağımlılıklı hashing trick embedding (İYİLEŞTİRİLMİŞ).
    
    Geliştirmeler:
    - 512 boyut (256 yerine → daha az collision)
    - Token frequency aware weighting
    - Both signed and unsigned hash (daha zengin sinyal)
    """
    tokens = _tokenize(text.lower())
    if not tokens:
        return [0.0] * dim
    
    vec = [0.0] * dim
    freq: dict[str, int] = {}
    for t in tokens:
        freq[t] = freq.get(t, 0) + 1
    total = len(tokens)

    for token, count in freq.items():
        # 3 ayrı hash → 3 farklı boyuta yay (collision azaltır)
        h1 = int(hashlib.sha256(token.encode()).hexdigest(), 16)
        h2 = int(hashlib.blake2b(token.encode(), digest_size=8).hexdigest(), 16)
        
        idx = h1 % dim
        sign = 1.0 if (h1 >> 8) % 2 == 0 else -1.0
        tf = count / total
        weight = sign * tf * math.log(total + 1)
        vec[idx] += weight
        
        # İkinci hash → yanındaki boyuta da dağıt
        idx2 = h2 % dim
        sign2 = 1.0 if (h2 >> 4) % 2 == 0 else -1.0
        vec[idx2] += sign2 * weight * 0.5

    # L2 normalize
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


# Türkçe suffix stripping (basit stemming)
_TR_SUFFIXES = [
    "'nın", "'nin", "'nun", "'nün",  # genitive
    "'ın", "'in", "'un", "'ün",      # accusative
    "'da", "'de", "'ta", "'te",      # locative
    "'a", "'e",                      # dative
    "'ı", "'i", "'u", "'ü",          # definite accusative
    "'nı", "'ni", "'nu", "'nü",
    "nın", "nin", "nun", "nün",
    "ın", "in", "un", "ün",
    "lar", "ler",
    "ımız", "imiz", "umuz", "ümüz",
    "ları", "leri",
    "mak", "mek",
    "yor", "ir", "er", "ar",
    "dı", "di", "du", "dü",
    "mış", "miş", "muş", "müş",
    "acak", "ecek", "acağız", "eceğiz",
    "lı", "li", "lu", "lü",
    "lık", "lik", "luk", "lük",
    "siz", "siz", "suz", "süz",
    "lık", "lik",
    "çı", "ci", "cu", "cü",
    "sel", "sal",
    "ca", "ce",
]

_COMMON_STOP = {
    "bir", "bu", "şu", "o", "ve", "ile", "için", "gibi", "daha",
    "çok", "var", "mı", "mi", "mu", "mü", "ne", "ki", "de", "da",
    "ben", "sen", "biz", "siz", "onlar", "benim", "senin", "ondan",
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "could", "should", "may", "might", "can", "shall", "to", "of",
    "in", "for", "on", "with", "at", "by", "from", "it", "that",
    "this", "not", "but", "or", "if", "then", "so", "what", "how",
}


def _stem_tr(word: str) -> str:
    """Basit Türkçe suffix stripping (hızlı stemming)."""
    for suffix in sorted(_TR_SUFFIXES, key=len, reverse=True):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def _tokenize(text: str) -> list[str]:
    """
    Türkçe destekli tokenizasyon + stemming + char trigram.
    
    Daha iyi semantic matching için:
    1. Kelimeler → stem (suffix strip)
    2. Stop words kaldır
    3. Unigram + bigram + char trigram ekle
    """
    words = re.findall(r"[a-zçğıöşü0-9]+", text, re.IGNORECASE)
    
    tokens = []
    stemmed = []
    for w in words:
        w_lower = w.lower()
        if w_lower in _COMMON_STOP:
            continue
        stem = _stem_tr(w_lower)
        stemmed.append(stem)
        tokens.append(stem)
        # Char trigramlar (kelime içi yakınlık için)
        if len(stem) >= 3:
            for i in range(len(stem) - 2):
                tokens.append(f"#{stem[i:i+3]}")
    
    # Bigrams (stem'ler arasında)
    for i in range(len(stemmed) - 1):
        tokens.append(f"{stemmed[i]}_{stemmed[i+1]}")
    
    return tokens if tokens else words  # fallback: stem'leme hepsini öldürdüyse


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """İki vektör arasındaki cosine benzerliği (0-1)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (norm_a * norm_b)
