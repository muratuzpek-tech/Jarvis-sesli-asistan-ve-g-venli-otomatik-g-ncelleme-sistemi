"""tests/test_memory.py — Memory regression tests"""
import sys, asyncio
sys.path.insert(0, '.')
from memory.vector_memory import VectorMemory


def test_basic_recall():
    mem = VectorMemory()
    async def t():
        await mem.remember("pytest memory test", category="test")
        results = await mem.recall("pytest memory")
        assert len(results) > 0
    asyncio.run(t())
    mem.close()

def test_concurrent_writes():
    mem = VectorMemory()
    async def t():
        tasks = [mem.remember(f"conc {i}", category="test") for i in range(10)]
        await asyncio.gather(*tasks)
        results = await mem.recall("conc")
        assert len(results) > 0
    asyncio.run(t())
    mem.close()
