import asyncio
import json
import time

import httpx
import pytest
from llm_routing import (
    AdmissionController,
    Backend,
    QueueFull,
    QueueTimeout,
    Router,
    UpstreamRejected,
    UpstreamUnavailable,
)

BACKENDS = [
    Backend("primary", "http://primary", "primary-key", "same-model"),
    Backend("fallback", "http://fallback", "fallback-key", "same-model"),
]


async def test_primary_down_fails_over_and_circuit_recovers():
    seen = []
    primary_up = False

    def handle(request):
        seen.append(request.url.host)
        assert json.loads(request.content)["model"] == "same-model"
        assert request.headers["authorization"] == f"Bearer {request.url.host}-key"
        if request.url.host == "primary" and not primary_up:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json={"choices": [1]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        router = Router(client, BACKENDS, failure_threshold=1)
        assert (await router.complete({}))[0] == "fallback"
        assert (await router.complete({}))[0] == "fallback"
        assert seen == ["primary", "fallback", "fallback"]
        primary_up = True
        router.breakers["primary"].open_until = time.monotonic() - 1
        assert (await router.complete({}))[0] == "primary"
        assert router.breakers["primary"].open_until == 0


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_overloaded_primary_uses_fallback(status):
    def handle(request):
        return httpx.Response(
            status if request.url.host == "primary" else 200, json={"choices": [1]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        assert (await Router(client, BACKENDS).complete({}))[0] == "fallback"


async def test_read_timeout_preserves_uncertainty_without_replay():
    seen = []

    def handle(request):
        seen.append(request.url.host)
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(UpstreamUnavailable) as exc:
            await Router(client, BACKENDS).complete({})
        assert exc.value.uncertain is True
    assert seen == ["primary"]


async def test_total_deadline_also_stops_slow_upstream():
    async def handle(request):
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(UpstreamUnavailable) as exc:
            await Router(client, BACKENDS, timeout_seconds=0.01).complete({})
        assert exc.value.uncertain


async def test_client_error_is_not_retried():
    seen = []

    def handle(request):
        seen.append(request.url.host)
        return httpx.Response(400)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(UpstreamRejected):
            await Router(client, BACKENDS).complete({})
    assert seen == ["primary"]


async def test_all_unreachable_is_known_failure():
    def handle(request):
        raise httpx.ConnectError("down", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(UpstreamUnavailable) as exc:
            await Router(client, BACKENDS).complete({})
        assert not exc.value.uncertain


async def test_admission_caps_queue_and_cancellation_frees_capacity():
    admission = AdmissionController(1, 1, 1)
    started = asyncio.Event()

    async def work():
        async with admission.slot():
            started.set()
            await asyncio.Event().wait()

    active = asyncio.create_task(work())
    await started.wait()
    queued = asyncio.create_task(work())
    await asyncio.sleep(0)
    assert (admission.active, admission.waiting) == (1, 1)
    with pytest.raises(QueueFull):
        async with admission.slot():
            pytest.fail("Over capacity")
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert (admission.active, admission.waiting) == (0, 0)
    async with admission.slot():
        assert admission.active == 1


async def test_queue_deadline_and_zero_waiting_capacity():
    admission = AdmissionController(1, 1, 0.01)
    async with admission.slot():
        with pytest.raises(QueueTimeout):
            async with admission.slot():
                pytest.fail("Queue should time out")
    assert admission.waiting == admission.active == 0
    no_queue = AdmissionController(1, 0, 1)
    async with no_queue.slot():
        with pytest.raises(QueueFull):
            async with no_queue.slot():
                pytest.fail("Queue must be disabled")
