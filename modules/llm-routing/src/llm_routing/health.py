import asyncio
import math
import time
from dataclasses import dataclass

import httpx
from prometheus_client.parser import text_string_to_metric_families


@dataclass(frozen=True)
class Observation:
    healthy: bool
    waiting: float | None
    kv_usage: float | None
    checked_at: float


class BackendMonitor:
    """Observe engine queues, not the gateway's independent admission queue."""

    def __init__(self, client, backends, interval=5.0):
        self.client = client
        self.backends = backends
        self.interval = interval
        self.observations: dict[str, Observation] = {}

    async def poll(self):
        async def check(backend):
            healthy, waiting, kv = False, None, None
            try:
                async with asyncio.timeout(2.5):
                    health, metrics = await asyncio.gather(
                        self.client.get(f"{backend.url.rstrip('/')}/health", timeout=2),
                        self.client.get(f"{backend.url.rstrip('/')}/metrics", timeout=2),
                    )
                health.raise_for_status()
                metrics.raise_for_status()
                samples = [
                    sample
                    for family in text_string_to_metric_families(metrics.text)
                    for sample in family.samples
                ]
                queues = [s.value for s in samples if s.name == "vllm:num_requests_waiting"]
                cache = [s.value for s in samples if s.name == "vllm:kv_cache_usage_perc"]
                if queues and all(math.isfinite(v) and v >= 0 for v in queues):
                    waiting = sum(queues)
                if cache and all(math.isfinite(v) and 0 <= v <= 1 for v in cache):
                    kv = max(cache)
                healthy = True
            except (httpx.HTTPError, TimeoutError, ValueError):
                pass
            self.observations[backend.name] = Observation(healthy, waiting, kv, time.monotonic())

        await asyncio.gather(*(check(b) for b in self.backends))

    def available(self, backend) -> bool:
        observed = self.observations.get(backend.name)
        # Unknown/stale telemetry is never treated as an empty queue.
        return bool(
            observed
            and observed.healthy
            and time.monotonic() - observed.checked_at <= self.interval * 3
            and observed.waiting is not None
            and observed.waiting <= backend.max_queue
        )

    async def run(self):
        while True:
            await asyncio.sleep(self.interval)
            await self.poll()
