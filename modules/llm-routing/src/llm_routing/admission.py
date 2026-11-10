import asyncio
from contextlib import asynccontextmanager


class QueueFull(Exception):
    pass


class QueueTimeout(Exception):
    pass


class AdmissionController:
    """Bounds both running and waiting work within a single event loop/worker."""

    def __init__(self, concurrency: int, queue_size: int, wait_seconds: float):
        if concurrency < 1 or queue_size < 0 or wait_seconds <= 0:
            raise ValueError("Invalid admission limits")
        self.concurrency = concurrency
        self.queue_size = queue_size
        self.wait_seconds = wait_seconds
        self.active = 0
        self.waiting = 0
        self._slots = asyncio.Semaphore(concurrency)

    @asynccontextmanager
    async def slot(self):
        # No await between counting and admission: cancellation cannot leak a slot.
        if self.active + self.waiting >= self.concurrency + self.queue_size:
            raise QueueFull
        self.waiting += 1
        acquired = False
        try:
            try:
                async with asyncio.timeout(self.wait_seconds):
                    await self._slots.acquire()
                acquired = True
            except TimeoutError as exc:
                raise QueueTimeout from exc
        finally:
            self.waiting -= 1
        try:
            self.active += 1
            yield
        finally:
            if acquired:
                self.active -= 1
                self._slots.release()
