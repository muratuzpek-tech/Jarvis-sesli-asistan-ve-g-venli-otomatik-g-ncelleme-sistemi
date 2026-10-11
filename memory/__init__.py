"""
Jarvis 2.0 — Vektörel Hafıza (RAG)

Mimari:
    Kullanıcı konuşması
      ↓
    embedding_provider.embed() → vektör
      ↓
    vector_memory.store() → yerel depo
      ↓
    (sonraki turda)
    vector_memory.search() → semantik arama
      ↓
    İlgili anılar → LLM context'e geri getir
      ↓
    "Anımsıyor musun?" → CEVAP VAR!

Kullanım:
    from memory import memory

    await memory.remember("Python öğrenmek istiyorum", category="preference")
    results = await memory.recall("Ne öğrenmek istiyordum?")
    ctx_str = memory.format_for_prompt(results)
"""

from memory.vector_memory import VectorMemory, MemoryEntry, SearchResult, memory
from memory.embedding_provider import embed_text, embed_batch, get_embedding_dim

__all__ = [
    "memory", "VectorMemory", "MemoryEntry", "SearchResult",
    "embed_text", "embed_batch", "get_embedding_dim",
]
