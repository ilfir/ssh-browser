"""Transfer queue: at most N streams at once so the remote stays idle-ish."""
from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from PySide6.QtCore import QObject, Signal

_ids = itertools.count(1)


@dataclass
class Transfer:
    kind: str            # "Upload" | "Download"
    label: str
    total: int = 0       # 0 => unknown (indeterminate)
    done: int = 0
    current: str = ""
    status: str = "Queued"   # Queued | Running | Done | Failed | Cancelled
    error: str = ""
    id: int = field(default_factory=lambda: next(_ids))
    task: asyncio.Task | None = None


class TransferManager(QObject):
    changed = Signal(object)   # Transfer
    finished = Signal(object)  # Transfer

    def __init__(self, max_parallel: int = 3):
        super().__init__()
        self._sem = asyncio.Semaphore(max_parallel)
        self.items: list[Transfer] = []

    def add(self, t: Transfer, work: Callable[[Callable[[str, int, int], None]], Awaitable[None]]) -> Transfer:
        self.items.append(t)
        self.changed.emit(t)
        last: dict[str, int] = {}

        def progress(name: str, done: int, total: int) -> None:
            t.done += max(0, done - last.get(name, 0))
            last[name] = done
            t.current = name
            if not t.total or t.done > t.total:
                pass  # indeterminate / size grew: UI handles it
            self.changed.emit(t)

        async def runner():
            async with self._sem:
                t.status = "Running"
                self.changed.emit(t)
                try:
                    await work(progress)
                    t.status = "Done"
                    if t.total:
                        t.done = t.total
                except asyncio.CancelledError:
                    t.status = "Cancelled"
                except Exception as e:  # noqa: BLE001 - surfaced in UI
                    t.status, t.error = "Failed", str(e) or type(e).__name__
                self.changed.emit(t)
                self.finished.emit(t)

        t.task = asyncio.ensure_future(runner())
        return t

    def cancel(self, t: Transfer) -> None:
        if t.task and not t.task.done():
            t.task.cancel()

    def clear_finished(self) -> None:
        self.items = [t for t in self.items if t.status in ("Queued", "Running")]
