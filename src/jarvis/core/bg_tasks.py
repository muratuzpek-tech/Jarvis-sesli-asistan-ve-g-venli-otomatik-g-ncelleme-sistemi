"""Arka plan görevlerini canlı tutar.

asyncio yalnızca ZAYIF referans tutar: sonucu bir değişkende saklanmayan
`asyncio.create_task(...)` işi, bitmeden çöp toplayıcı tarafından silinebilir
(ruff RUF006). Sabah özetinin bir bölümü ya da dashboard'a giden bir mesaj bu
yüzden ara sıra sessizce kaybolabiliyordu. `keep()` görevi bitene kadar saklar.
"""
from __future__ import annotations

import asyncio

_TASKS: set[asyncio.Task] = set()


def keep(task: asyncio.Task) -> asyncio.Task:
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task
