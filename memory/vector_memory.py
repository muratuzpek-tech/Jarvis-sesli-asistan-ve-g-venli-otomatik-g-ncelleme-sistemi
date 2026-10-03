"""
memory/vector_memory.py — Jarvis 2.0 Vektörel Hafıza Motoru

Depolama:
  - Qdrant sunucu varsa → vektör DB (senkron/async)
  - Yoksa → SQLite + brute-force cosine search (yerel fallback)
  
İkisi de AYNI API'yi kullanır → kod değişmez!

Akış:
    remember("Python öğreniyorum")  → store
    recall("ne öğreniyordum?")      → semantik arama
    → ["Python öğrenmek istiyorum"]  (score: 0.87)
    → format_for_prompt()           → LLM context
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from memory.embedding_provider import (
    cosine_similarity,
    embed_text,
    embed_batch,
    get_embedding_dim,
)

logger = logging.getLogger("memory.vector")


@dataclass
class MemoryEntry:
    """Tek bir hafıza girdisi."""
    id: str = field(default_factory=lambda: str(uuid4()))
    text: str = ""
    category: str = "general"
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    importance: float = 0.5  # 0.0 (önemsiz) → 1.0 (kritik)


@dataclass
class SearchResult:
    """Arama sonucu."""
    entry: MemoryEntry
    score: float

    def __repr__(self) -> str:
        return f"[{self.score:.2f}] {self.entry.category}: {self.entry.text[:60]}"


class VectorMemory:
    """
    Uzun vadeli vektörel hafıza.
    
    Backend otomatik seçilir:
      1. Qdrant server (http://localhost:6333)
      2. SQLite + cosine (yerel, sıfır bağımlılık)
    
    Thread-safe: Kilitli yazma/okuma.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        qdrant_url: str | None = None,
        collection: str = "jarvis_memories",
    ) -> None:
        self._collection = collection
        self._dim = get_embedding_dim()
        self._lock = threading.Lock()

        # ── SQLite fallback ────────────────────────────────────
        if db_path is None:
            db_path = Path.home() / ".jarvis" / "memory.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._init_sqlite()

        # ── Qdrant (opsiyonel) ─────────────────────────────────
        self._qdrant = None
        self._qdrant_url = qdrant_url
        self._backend = "sqlite"
        self._qdrant_enabled = False

    # ── Initialization ─────────────────────────────────────────

    def _init_sqlite(self) -> None:
        """SQLite tablosunu oluştur."""
        with self._lock:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    metadata TEXT DEFAULT '{}',
                    timestamp TEXT,
                    importance REAL DEFAULT 0.5,
                    embedding BLOB
                )
            """)
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_category ON memories(category)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_timestamp ON memories(timestamp)"
            )
            self._db.commit()
        logger.info(f"[Memory] ✅ SQLite: {self._db_path}")

    async def connect_qdrant(self, url: str | None = None) -> bool:
        """Qdrant'a bağlanmayı dene."""
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams

            _url = url or self._qdrant_url or "http://localhost:6333"
            self._qdrant = QdrantClient(url=_url, timeout=5)

            # Collection var mı?
            collections = self._qdrant.get_collections().collections
            names = [c.name for c in collections]

            if self._collection not in names:
                self._qdrant.create_collection(
                    collection_name=self._collection,
                    vectors_config=VectorParams(size=self._dim, distance=Distance.COSINE),
                )
                logger.info(f"[Memory] ✅ Qdrant collection created: {self._collection}")
            else:
                logger.info(f"[Memory] ✅ Qdrant connected: {self._collection}")

            self._qdrant_enabled = True
            self._backend = "qdrant"
            return True

        except Exception as e:
            logger.info(f"[Memory] i️ Qdrant yok ({type(e).__name__}) — SQLite fallback aktif")
            self._qdrant = None
            self._qdrant_enabled = False
            return False

    # ── STORE ──────────────────────────────────────────────────

    async def remember(
        self,
        text: str,
        category: str = "general",
        metadata: dict | None = None,
        importance: float = 0.5,
    ) -> MemoryEntry:
        """
        Bilgiyi hafızaya kaydet.
        
        Args:
            text:       Saklanacak metin
            category:   personal | preference | fact | conversation | task
            metadata:   Ek veriler
            importance: 0.0-1.0 önemsiz→kritik
        
        Returns:
            MemoryEntry: Kaydedilen girdi
        """
        entry = MemoryEntry(
            text=text,
            category=category,
            metadata=metadata or {},
            importance=max(0.0, min(1.0, importance)),
        )

        # Embedding üret (executor'a at — CPU-bound olabilir)
        vec = await asyncio.get_event_loop().run_in_executor(
            None, embed_text, text
        )

        if self._qdrant_enabled:
            try:
                from qdrant_client.models import PointStruct
                self._qdrant.upsert(
                    collection_name=self._collection,
                    points=[PointStruct(
                        id=entry.id,
                        vector=vec,
                        payload={
                            "text": entry.text,
                            "category": entry.category,
                            "metadata": entry.metadata,
                            "timestamp": entry.timestamp,
                            "importance": entry.importance,
                        },
                    )],
                )
                logger.info(f"[Memory] 💾 Qdrant store: {category} → {text[:50]}")
                return entry
            except Exception as e:
                logger.warning(f"[Memory] Qdrant write hatası → SQLite: {e}")

        # SQLite fallback
        vec_blob = json.dumps(vec)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?)",
                (entry.id, entry.text, entry.category,
                 json.dumps(entry.metadata), entry.timestamp,
                 entry.importance, vec_blob),
            )
            self._db.commit()

        logger.info(f"[Memory] 💾 Store: {category} → {text[:50]}")
        return entry

    # ── RECALL (SEMANTIC SEARCH) ──────────────────────────────

    async def recall(
        self,
        query: str,
        top_k: int = 5,
        category: str | None = None,
        min_score: float = 0.05,
    ) -> list[SearchResult]:
        """
        Semantik arama — en alakalı anıları bul.
        
        Args:
            query:     Arama sorgusu (doğal dil)
            top_k:     En fazla kaç sonuç?
            category:  Sadece belirli kategori
            min_score: Minimum benzerlik skoru (0.0-1.0)
        
        Returns:
            list[SearchResult]: Score'a göre sıralı
        """
        q_vec = await asyncio.get_event_loop().run_in_executor(
            None, embed_text, query
        )

        if self._qdrant_enabled:
            try:
                from qdrant_client.models import Filter, FieldCondition, MatchValue
                q_filter = None
                if category:
                    q_filter = Filter(must=[
                        FieldCondition(key="category", match=MatchValue(value=category))
                    ])
                hits = self._qdrant.search(
                    collection_name=self._collection,
                    query_vector=q_vec,
                    query_filter=q_filter,
                    limit=top_k,
                    score_threshold=min_score,
                )
                results = []
                for hit in hits:
                    entry = MemoryEntry(
                        id=str(hit.id),
                        text=hit.payload.get("text", ""),
                        category=hit.payload.get("category", "general"),
                        metadata=hit.payload.get("metadata", {}),
                        timestamp=hit.payload.get("timestamp", ""),
                        importance=hit.payload.get("importance", 0.5),
                    )
                    results.append(SearchResult(entry=entry, score=hit.score))
                return results
            except Exception as e:
                logger.warning(f"[Memory] Qdrant search hatası → SQLite: {e}")

        # SQLite fallback: brute-force cosine
        with self._lock:
            rows = self._db.execute(
                "SELECT id, text, category, metadata, timestamp, importance, embedding FROM memories"
            ).fetchall()

        results = []
        for row in rows:
            rid, text, cat, meta_json, ts, imp, vec_blob = row
            if category and cat != category:
                continue
            try:
                stored_vec = json.loads(vec_blob)
                score = cosine_similarity(q_vec, stored_vec)
                if score >= min_score:
                    entry = MemoryEntry(
                        id=rid, text=text, category=cat,
                        metadata=json.loads(meta_json or "{}"),
                        timestamp=ts, importance=imp,
                    )
                    results.append(SearchResult(entry=entry, score=score))
            except (json.JSONDecodeError, TypeError):
                continue

        # Keyword overlap fallback: cosine düşükse kelime eşleşmesi dene
        query_words = set(query.lower().split())
        for row in rows:
            rid, text, cat, meta_json, ts, imp, vec_blob = row
            if category and cat != category:
                continue
            # Zaten sonuç var mı bu id için?
            existing_ids = {r.entry.id for r in results}
            if rid in existing_ids:
                continue
            
            text_words = set(text.lower().split())
            overlap = len(query_words & text_words) / max(len(query_words), 1)
            if overlap > 0:
                kw_score = 0.1 + overlap * 0.4  # 0.1–0.5 arası
                entry = MemoryEntry(
                    id=rid, text=text, category=cat,
                    metadata=json.loads(meta_json or "{}"),
                    timestamp=ts, importance=imp,
                )
                results.append(SearchResult(entry=entry, score=kw_score))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    # ── FORGET ────────────────────────────────────────────────

    async def forget(self, memory_id: str) -> bool:
        """Belirli bir anıyı sil."""
        if self._qdrant_enabled:
            try:
                self._qdrant.delete(collection_name=self._collection, points_selector=[memory_id])
                return True
            except Exception:
                pass

        with self._lock:
            cur = self._db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            self._db.commit()
            return cur.rowcount > 0

    async def clear_category(self, category: str) -> int:
        """Belirli kategorideki tüm anıları sil."""
        if self._qdrant_enabled:
            try:
                from qdrant_client.models import Filter, FieldCondition, MatchValue
                self._qdrant.delete(
                    collection_name=self._collection,
                    points_selector=Filter(must=[
                        FieldCondition(key="category", match=MatchValue(value=category))
                    ]),
                )
            except Exception:
                pass

        with self._lock:
            cur = self._db.execute("DELETE FROM memories WHERE category=?", (category,))
            self._db.commit()
            return cur.rowcount

    # ── FORMAT FOR PROMPT ─────────────────────────────────────

    @staticmethod
    def format_for_prompt(results: list[SearchResult], max_chars: int = 1500) -> str:
        """
        Arama sonuçlarını LLM prompt formatına dönüştür.
        
        Örnek çıktı:
        [LONG-TERM MEMORY — ilgili geçmiş bilgiler]
        • (0.87) personal: Kullanıcının adı Murat
        • (0.72) preference: Python öğrenmek istiyor
        [/LONG-TERM MEMORY]
        """
        if not results:
            return ""

        lines = ["[LONG-TERM MEMORY — ilgili geçmiş bilgiler]"]
        used = 0
        for r in results:
            line = f"• ({r.score:.2f}) {r.entry.category}: {r.entry.text}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        lines.append("[/LONG-TERM MEMORY]")
        return "\n".join(lines)

    # ── STATS ─────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            cats = self._db.execute(
                "SELECT category, COUNT(*) FROM memories GROUP BY category ORDER BY COUNT(*) DESC"
            ).fetchall()
        return {
            "backend": self._backend,
            "total_memories": total,
            "by_category": dict(cats),
            "db_path": str(self._db_path),
            "embedding_dim": self._dim,
        }

    # ── BATCH IMPORT ──────────────────────────────────────────

    async def import_conversations(self, conversations: list[dict]) -> int:
        """
        Mevcut konuşma loglarını hafızaya aktar.
        
        Args:
            conversations: [{"role": "user", "text": "...", "ts": "..."}, ...]
        
        Returns:
            int: Kaydedilen girdi sayısı
        """
        count = 0
        for conv in conversations:
            text = conv.get("text", "").strip()
            role = conv.get("role", "user")
            if text and len(text) > 10:  # Çok kısa olanları atla
                category = "conversation" if role == "user" else "conversation"
                importance = 0.3
                # Kişisel bilgi tespiti → importance artır
                lower = text.casefold()
                if any(w in lower for w in ("adım", "benim adım", "meslek", "seviyorum", "tercihim", "öğrenmek", "projem")):
                    importance = 0.8
                await self.remember(text, category=category, metadata={"role": role}, importance=importance)
                count += 1
        logger.info(f"[Memory] 📥 Import: {count} conversation entries")
        return count

    def close(self) -> None:
        with self._lock:
            self._db.close()
        if self._qdrant:
            try:
                self._qdrant.close()
            except Exception:
                pass


# ── Singleton ─────────────────────────────────────────────────
memory = VectorMemory()
