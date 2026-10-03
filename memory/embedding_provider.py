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
from typing import Any

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

def _tfidf_hash_embed(text: str, dim: int = 256) -> list[float]:
    """
    Sıfır bağımlılıklı hashing trick embedding.
    
    Anahtar fikir: Kelime trigramlarını hash'leyip TF-IDF ağırlıklı
    yoğun vektör oluştur. Cosine similarity ile semantik arama mümkün.
    """
    tokens = _tokenize(text.lower())
    vec = [0.0] * dim
    total = len(tokens) or 1

    for token in tokens:
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        idx = h % dim
        sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
        tf = 1.0 / total
        vec[idx] += sign * tf * math.log(total + 1)

    # L2 normalize
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _tokenize(text: str) -> list[str]:
    """Türkçe destekli tokenizasyon + bigram."""
    words = re.findall(r"[a-zçğıöşü0-9]+", text, re.IGNORECASE)
    unigrams = words
    bigrams = [f"{words[i]}_{words[i+1]}" for i in range(len(words) - 1)]
    return unigrams + bigrams


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """İki vektör arasındaki cosine benzerliği (0-1)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a)) or 1.0
    norm_b = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (norm_a * norm_b)
