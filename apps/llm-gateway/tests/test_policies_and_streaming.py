import asyncio
import json
import time

import httpx
import pytest
from llm_gateway.domain import ChatRequest
from llm_routing import BackendMonitor
from test_gateway import AUTH, PAYLOAD, RESPONSE, gateway


def encoded_events(usage=True, done=True):
    events = [
        {
            "id": "stream-test",
            "object": "chat.completion.chunk",
            "model": "local-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "안녕"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "stream-test",
            "object": "chat.completion.chunk",
            "model": "local-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    ]
    if usage:
        events.append({"choices": [], "usage": RESPONSE["usage"]})
    raw = b"".join(("data: " + json.dumps(e, ensure_ascii=False) + "\n\n").encode() for e in events)
    return raw + (b"data: [DONE]\n\n" if done else b"")


class FragmentedStream(httpx.AsyncByteStream):
    def __init__(self, raw, stall=False):
        self.raw, self.stall, self.closed = raw, stall, False

    async def __aiter__(self):
        # Splits UTF-8 characters and SSE/JSON fields across network chunks.
        for i in range(0, len(self.raw), 2):
            yield self.raw[i : i + 2]
        if self.stall:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


async def test_budget_exhaustion_uses_free_model_and_audits_tokens(tmp_path):
    seen = []

    def handle(request):
        seen.append((request.url.host, json.loads(request.content)["model"]))
        return httpx.Response(200, json=RESPONSE)

    async with gateway(
        tmp_path,
        handle,
        fallback_free=True,
        fallback_model="small-model",
        global_monthly_budget_micro_usd=0,
    ) as (client, app):
        response = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert response.status_code == 200
        assert response.headers["x-routing-reason"] == "budget"
        assert response.headers["x-fallback"] == "true"
        assert response.headers["x-model-used"] == response.json()["model"] == "small-model"
        assert seen == [("fallback", "small-model")]
        billing = (await client.get("/v1/billing", headers=AUTH)).json()
        assert (billing["charged"], billing["prompt_tokens"], billing["reserved"]) == (0, 20, 0)
        assert billing["recent_requests"][0]["model"] == "small-model"


async def test_model_permissions_cannot_be_bypassed_by_budget_downgrade(tmp_path):
    accounts = [
        {
            "name": "alice",
            "api_key": "test-api-key-alice",
            "monthly_budget_micro_usd": 0,
            "allowed_models": ["large-model"],
        }
    ]
    async with gateway(
        tmp_path,
        accounts=accounts,
        fallback_free=True,
        primary_model="large-model",
        fallback_model="small-model",
    ) as (client, app):
        denied = await client.post(
            "/v1/chat/completions", json={**PAYLOAD, "model": "small-model"}, headers=AUTH
        )
        assert denied.status_code == 403
        assert (
            await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        ).status_code == 402


async def test_engine_queue_metric_routes_without_calling_overloaded_model(tmp_path):
    seen = []

    def handle(request):
        if request.method == "GET":
            waiting = 11 if request.url.host == "primary" else 0
            if request.url.path == "/health":
                return httpx.Response(200)
            return httpx.Response(
                200,
                text=(
                    f'vllm:num_requests_waiting{{engine="0"}} {waiting}\n'
                    'vllm:kv_cache_usage_perc{engine="0"} 0.5\n'
                ),
            )
        seen.append(request.url.host)
        return httpx.Response(200, json=RESPONSE)

    async with gateway(tmp_path, handle) as (client, app):
        router = app.state.service.router
        router.monitor = BackendMonitor(router.client, router.backends)
        await router.monitor.poll()
        response = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert response.status_code == 200 and response.headers["x-routing-reason"] == "queue"
        assert seen == ["fallback"]
        router.monitor.observations.clear()
        assert (
            await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        ).status_code == 503
        assert seen == ["fallback"]


async def test_stream_fragmentation_usage_and_single_settlement(tmp_path):
    upstream = FragmentedStream(encoded_events())

    def handle(request):
        assert json.loads(request.content)["stream_options"]["include_usage"] is True
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream)

    async with gateway(tmp_path, handle) as (client, app):
        response = await client.post(
            "/v1/chat/completions", json={**PAYLOAD, "stream": True}, headers=AUTH
        )
        assert response.status_code == 200
        assert "안녕" in response.text
        assert response.text.count("data: [DONE]") == 1
        assert response.headers["x-accel-buffering"] == "no"
        billing = (await client.get("/v1/billing", headers=AUTH)).json()
        assert billing["charged"] == 70 and billing["reserved"] == 0
        assert len(billing["recent_requests"]) == 1
        assert upstream.closed and app.state.service.admission.active == 0


@pytest.mark.parametrize("usage,done", [(False, True), (True, False)])
async def test_incomplete_stream_keeps_reservation_and_does_not_report_success(
    tmp_path, usage, done
):
    seen = []

    def handle(request):
        seen.append(request.url.host)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=FragmentedStream(encoded_events(usage, done)),
        )

    async with gateway(tmp_path, handle) as (client, app):
        response = await client.post(
            "/v1/chat/completions", json={**PAYLOAD, "stream": True}, headers=AUTH
        )
        assert '"error"' in response.text and "data: [DONE]" not in response.text
        assert seen == ["primary"]  # no replay after a partial answer
        row = app.state.service.ledger.recent("alice")[0]
        assert row["state"] == "uncertain" and row["charged"] == 0
        assert app.state.service.admission.active == 0


async def test_stream_closing_after_headers_releases_connection_and_slot(tmp_path):
    upstream = FragmentedStream(encoded_events(), stall=True)

    def handle(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=upstream)

    async with gateway(tmp_path, handle) as (client, app):
        service = app.state.service
        events = service.stream(
            ChatRequest(**{**PAYLOAD, "stream": True}),
            app.state.config.accounts[0],
            "disconnect-test",
        )
        assert (await anext(events))["X-LLM-Backend"] == "primary"
        assert service.admission.active == 1
        await events.aclose()
        assert upstream.closed and service.admission.active == 0
        assert service.ledger.recent("alice")[0]["state"] == "uncertain"


async def test_stream_deadline_is_an_error_even_after_output(tmp_path):
    def handle(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=FragmentedStream(encoded_events(done=False), stall=True),
        )

    async with gateway(tmp_path, handle, upstream_timeout_seconds=0.02) as (client, app):
        response = await client.post(
            "/v1/chat/completions", json={**PAYLOAD, "stream": True}, headers=AUTH
        )
        assert '"model_unavailable"' in response.text and "[DONE]" not in response.text
        assert app.state.service.ledger.recent("alice")[0]["state"] == "uncertain"


async def test_transport_connect_error_after_stream_start_never_replays(tmp_path):
    seen = []

    class FailsAfterOutput(FragmentedStream):
        async def __aiter__(self):
            yield encoded_events(done=False)
            raise httpx.ConnectError("connection lost after output")

    def handle(request):
        seen.append(request.url.host)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=FailsAfterOutput(b"")
        )

    async with gateway(tmp_path, handle) as (client, app):
        response = await client.post(
            "/v1/chat/completions", json={**PAYLOAD, "stream": True}, headers=AUTH
        )
        assert '"error"' in response.text and "[DONE]" not in response.text
        assert seen == ["primary"]
        assert app.state.service.admission.active == 0


async def test_stream_fallback_before_headers_and_preflight_errors(tmp_path):
    def handle(request):
        if request.url.host == "primary":
            return httpx.Response(503)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=FragmentedStream(encoded_events()),
        )

    async with gateway(tmp_path, handle) as (client, app):
        response = await client.post(
            "/v1/chat/completions", json={**PAYLOAD, "stream": True}, headers=AUTH
        )
        assert response.headers["x-llm-backend"] == "fallback"
        assert "[DONE]" in response.text
    async with gateway(tmp_path / "denied", handle, global_monthly_budget_micro_usd=0) as (
        client,
        app,
    ):
        response = await client.post(
            "/v1/chat/completions", json={**PAYLOAD, "stream": True}, headers=AUTH
        )
        assert response.status_code == 402 and response.json()["error"]["code"] == "budget_exceeded"


async def test_department_budget_shared_by_accounts_and_admin_summary(tmp_path):
    accounts = [
        {"name": name, "api_key": key, "department": "engineering", "admin": name == "alice"}
        for name, key in [("alice", "test-api-key-alice"), ("bob", "test-api-key-bobbb")]
    ]
    async with gateway(
        tmp_path,
        accounts=accounts,
        department_budgets={"engineering": 300},
        fallback_free=True,
        fallback_model="free-model",
    ) as (client, app):
        assert (await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)).headers[
            "x-llm-backend"
        ] == "primary"
        bob_auth = {"Authorization": "Bearer test-api-key-bobbb"}
        response = await client.post("/v1/chat/completions", json=PAYLOAD, headers=bob_auth)
        assert response.headers["x-routing-reason"] == "budget"
        summary = (await client.get("/v1/admin/usage", headers=AUTH)).json()
        assert summary["departments"][0]["committed"] == 70
        assert (await client.get("/v1/admin/usage", headers=bob_auth)).status_code == 403


async def test_canary_assignment_is_stable_and_model_is_audited(tmp_path):
    async with gateway(
        tmp_path, canary_backend="fallback", canary_ratio=1, fallback_model="new-version"
    ) as (client, app):
        for _ in range(2):
            response = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
            assert response.headers["x-model-used"] == "new-version"
        assert {r["model"] for r in app.state.service.ledger.recent("alice")} == {"new-version"}


async def test_cors_uses_environment_loaded_during_lifespan(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_CORS_ORIGINS", '["http://localhost:5173"]')
    async with gateway(tmp_path) as (client, app):
        response = await client.options(
            "/v1/chat/completions",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


async def test_stale_or_invalid_telemetry_is_not_empty_queue(tmp_path):
    def handle(request):
        return httpx.Response(200, text="vllm:num_requests_waiting NaN\n")

    async with gateway(tmp_path, handle) as (client, app):
        monitor = BackendMonitor(app.state.service.router.client, app.state.service.router.backends)
        await monitor.poll()
        state = monitor.observations["primary"]
        assert state.waiting is None
        assert not monitor.available(app.state.service.router.backends[0])
        assert time.monotonic() - state.checked_at < 1
