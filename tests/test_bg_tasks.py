import asyncio
import gc

from jarvis.core import bg_tasks


def test_kept_task_survives_gc_and_is_released_when_done():
    ran = []

    async def work():
        await asyncio.sleep(0.05)
        ran.append(1)

    async def main():
        bg_tasks.keep(asyncio.create_task(work()))
        gc.collect()
        assert len(bg_tasks._TASKS) == 1
        await asyncio.sleep(0.1)

    asyncio.run(main())
    assert ran == [1] and not bg_tasks._TASKS
