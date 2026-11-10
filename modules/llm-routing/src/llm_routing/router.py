import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx


class UpstreamUnavailable(Exception):
    def __init__(self, uncertain: bool = False):
        self.uncertain = uncertain


class UpstreamRejected(Exception):
    def __init__(self, status_code: int):
        self.status_code = status_code


@dataclass(frozen=True)
class Backend:
    name: str
    url: str
    api_key: str
    model: str
    input_price: int = 2
    output_price: int = 6
    priority: int = 0
    max_queue: int = 10

    @property
    def free(self):
        return self.input_price == self.output_price == 0


class CircuitBreaker:
    def __init__(self, threshold: int, recovery_seconds: float):
        self.threshold = threshold
        self.recovery_seconds = recovery_seconds
        self.failures = 0
        self.open_until = 0.0
        self.probing = False

    def allow(self) -> bool:
        if self.open_until:
            if time.monotonic() < self.open_until or self.probing:
                return False
            self.probing = True
        return True

    def success(self):
        self.failures = 0
        self.open_until = 0.0
        self.probing = False

    def failure(self):
        self.failures += 1
        if self.failures >= self.threshold or self.probing:
            self.open_until = time.monotonic() + self.recovery_seconds
        self.probing = False


class Router:
    """Primary-first routing; no retry after an ambiguous read/write failure."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        backends: list[Backend],
        failure_threshold: int = 3,
        recovery_seconds: float = 15,
        timeout_seconds: float = 60,
        on_attempt=None,
        monitor=None,
        canary_backend: str | None = None,
        canary_ratio: float = 0,
    ):
        if not backends:
            raise ValueError("At least one backend is required")
        self.client = client
        self.backends = backends
        self.timeout_seconds = timeout_seconds
        self.on_attempt = on_attempt or (lambda backend, outcome: None)
        self.monitor = monitor
        self.canary_backend = canary_backend
        self.canary_ratio = canary_ratio
        self.breakers = {
            b.name: CircuitBreaker(failure_threshold, recovery_seconds) for b in backends
        }

    def candidates(self, preferred=None, subject="", free_only=False):
        ordered = sorted(self.backends, key=lambda b: b.priority)
        if not preferred and self.canary_backend:
            bucket = int.from_bytes(hashlib.sha256(subject.encode()).digest()[:8], "big") / 2**64
            if bucket < self.canary_ratio:
                preferred = self.canary_backend
        if preferred:
            ordered.sort(key=lambda b: b.name != preferred)
        return [b for b in ordered if not free_only or b.free]

    def by_name(self, name):
        return next(b for b in self.backends if b.name == name)

    def _eligible(self, backend):
        if self.monitor and not self.monitor.available(backend):
            self.on_attempt(backend.name, "queue_or_health")
            return False
        if not self.breakers[backend.name].allow():
            self.on_attempt(backend.name, "circuit_open")
            return False
        return True

    async def complete(
        self, payload: dict[str, Any], candidates=None
    ) -> tuple[str, dict[str, Any]]:
        uncertain = False
        for backend in self.backends if candidates is None else candidates:
            breaker = self.breakers[backend.name]
            if not self._eligible(backend):
                continue
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    response = await self.client.post(
                        f"{backend.url.rstrip('/')}/v1/chat/completions",
                        json={**payload, "model": backend.model},
                        headers={"Authorization": f"Bearer {backend.api_key}"},
                    )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                breaker.failure()
                self.on_attempt(backend.name, "unreachable")
                continue
            except (httpx.TransportError, TimeoutError) as exc:
                breaker.failure()
                self.on_attempt(backend.name, "ambiguous_failure")
                # Generation may still be running. Preserve budget; do not replay.
                raise UpstreamUnavailable(uncertain=True) from exc
            except asyncio.CancelledError:
                breaker.failure()
                raise
            if response.status_code == 429 or response.status_code >= 500:
                breaker.failure()
                self.on_attempt(backend.name, "unavailable")
                uncertain |= response.status_code >= 500
                continue
            if response.status_code != 200:
                breaker.success()
                self.on_attempt(backend.name, "rejected")
                # A previous failed attempt might still have consumed tokens.
                if uncertain:
                    raise UpstreamUnavailable(uncertain=True)
                raise UpstreamRejected(response.status_code)
            try:
                data = response.json()
                if not isinstance(data, dict) or not data.get("choices"):
                    raise ValueError("Invalid completion response")
            except ValueError as exc:
                breaker.failure()
                self.on_attempt(backend.name, "invalid_response")
                raise UpstreamUnavailable(uncertain=True) from exc
            breaker.success()
            self.on_attempt(backend.name, "success")
            return backend.name, data
        raise UpstreamUnavailable(uncertain=uncertain)

    @asynccontextmanager
    async def open_stream(self, payload, candidates=None):
        """Fail over only before accepting a stream; never replay a partial answer."""
        uncertain = False
        for backend in self.backends if candidates is None else candidates:
            breaker = self.breakers[backend.name]
            if not self._eligible(backend):
                continue
            response = None
            accepted = False
            deadline = time.monotonic() + self.timeout_seconds
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    request = self.client.build_request(
                        "POST",
                        f"{backend.url.rstrip('/')}/v1/chat/completions",
                        json={
                            **payload,
                            "model": backend.model,
                            "stream": True,
                            "stream_options": {"include_usage": True},
                        },
                        headers={"Authorization": f"Bearer {backend.api_key}"},
                    )
                    response = await self.client.send(request, stream=True)
                if response.status_code == 429 or response.status_code >= 500:
                    uncertain |= response.status_code >= 500
                    breaker.failure()
                    self.on_attempt(backend.name, "unavailable")
                    continue
                if response.status_code != 200:
                    breaker.success()
                    if uncertain:
                        raise UpstreamUnavailable(uncertain=True)
                    raise UpstreamRejected(response.status_code)
                if "text/event-stream" not in response.headers.get("content-type", ""):
                    raise UpstreamUnavailable(uncertain=True)
                accepted = True
                yield backend.name, self._events(response, deadline)
                breaker.success()
                self.on_attempt(backend.name, "success")
                return
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                breaker.failure()
                if accepted:
                    self.on_attempt(backend.name, "ambiguous_failure")
                    raise UpstreamUnavailable(uncertain=True) from exc
                self.on_attempt(backend.name, "unreachable")
                continue
            except UpstreamRejected:
                raise
            except (httpx.TransportError, TimeoutError, ValueError) as exc:
                breaker.failure()
                self.on_attempt(backend.name, "ambiguous_failure")
                raise UpstreamUnavailable(uncertain=True) from exc
            except BaseException:
                # Also releases a half-open probe after cancellation/early close.
                breaker.failure()
                raise
            finally:
                if response is not None:
                    await response.aclose()
        raise UpstreamUnavailable(uncertain=uncertain)

    @staticmethod
    async def _events(response, deadline):
        data = []
        size = 0
        lines = response.aiter_lines().__aiter__()
        while True:
            try:
                async with asyncio.timeout(max(0, deadline - time.monotonic())):
                    line = await anext(lines)
            except StopAsyncIteration:
                break
            size += len(line)
            if size > 262144:
                raise ValueError("SSE event too large")
            if line == "":
                if not data:
                    size = 0
                    continue
                raw = "\n".join(data)
                data, size = [], 0
                if raw == "[DONE]":
                    return
                event = json.loads(raw)
                if not isinstance(event, dict) or "error" in event:
                    raise ValueError("Invalid SSE event")
                yield event
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        raise ValueError("SSE ended without DONE")

    async def ready(self) -> bool:
        async def healthy(backend):
            try:
                response = await self.client.get(
                    f"{backend.url.rstrip('/')}/health",
                    timeout=2,
                    headers={"Authorization": f"Bearer {backend.api_key}"},
                )
                return response.status_code == 200
            except httpx.HTTPError:
                return False

        return any(await asyncio.gather(*(healthy(b) for b in self.backends)))
