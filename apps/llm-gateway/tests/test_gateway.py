import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest
from llm_gateway.main import create_app
from llm_gateway.settings import Settings

AUTH = {"Authorization": "Bearer test-api-key-alice"}
PAYLOAD = {
    "model": "local-model",
    "messages": [{"role": "user", "content": "안녕"}],
    "max_tokens": 10,
}
RESPONSE = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "model": "local-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "안녕하세요"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
}


@asynccontextmanager
async def gateway(tmp_path, handler=None, **overrides):
    config = Settings(
        _env_file=None,
        **{
            "accounts": [
                {
                    "name": "alice",
                    "api_key": "test-api-key-alice",
                    "monthly_budget_micro_usd": 1000,
                },
                {"name": "bob", "api_key": "test-api-key-bobbb", "monthly_budget_micro_usd": 1000},
            ],
            "ledger_path": str(tmp_path / "billing.db"),
            "context_limit": 100,
            "max_output_tokens": 20,
            "primary_url": "http://primary",
            "fallback_url": "http://fallback",
            "fallback_free": False,
            "health_poll_interval_seconds": 0,
            **overrides,
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            handler or (lambda request: httpx.Response(200, json=RESPONSE)),
        )
    ) as upstream:
        app = create_app(config, upstream)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://gateway",
            ) as client:
                yield client, app


async def test_completion_billing_account_isolation_and_metrics(tmp_path):
    async with gateway(tmp_path) as (client, app):
        response = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert response.status_code == 200
        assert response.headers["x-charged-micro-usd"] == "70"
        assert response.headers["x-llm-backend"] == "primary"
        billing = (await client.get("/v1/billing", headers=AUTH)).json()
        assert (billing["charged"], billing["reserved"], billing["available"]) == (70, 0, 930)
        assert billing["recent_requests"][0]["request_id"] == response.headers["x-request-id"]
        bob = (
            await client.get(
                "/v1/billing",
                headers={
                    "Authorization": "Bearer test-api-key-bobbb",
                },
            )
        ).json()
        assert bob["charged"] == 0 and bob["recent_requests"] == []
        metrics = (await client.get("/metrics")).text
        assert 'llm_gateway_tokens_total{kind="prompt"} 20.0' in metrics
        assert 'llm_gateway_requests_total{outcome="success"} 1.0' in metrics
        assert "test-api-key" not in metrics and "안녕" not in metrics


async def test_auth_required(tmp_path):
    async with gateway(tmp_path) as (client, app):
        for path in ("/v1/models", "/v1/billing"):
            assert (await client.get(path)).status_code == 401
        assert (await client.post("/v1/chat/completions", json=PAYLOAD)).status_code == 401


async def test_budget_denied_before_upstream_and_global_limit(tmp_path):
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json=RESPONSE)

    async with gateway(tmp_path, handle, global_monthly_budget_micro_usd=259) as (client, app):
        response = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert response.status_code == 402
        assert response.json()["error"]["code"] == "budget_exceeded"
        assert seen == []
        assert app.state.service.ledger.recent("alice") == []


async def test_failover_is_charged_only_once(tmp_path):
    def handle(request):
        if request.url.host == "primary":
            return httpx.Response(503)
        return httpx.Response(200, json=RESPONSE)

    async with gateway(tmp_path, handle) as (client, app):
        response = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert response.headers["x-llm-backend"] == "fallback"
        billing = (await client.get("/v1/billing", headers=AUTH)).json()
        assert billing["charged"] == 70 and len(billing["recent_requests"]) == 1


@pytest.mark.parametrize("kind", ["timeout", "missing_usage", "oversized_usage", "bad_choices"])
async def test_unknown_generation_keeps_budget_reserved(tmp_path, kind):
    def handle(request):
        if kind == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if kind == "missing_usage":
            return httpx.Response(200, json={"choices": RESPONSE["choices"]})
        if kind == "bad_choices":
            return httpx.Response(200, json={**RESPONSE, "choices": [1]})
        return httpx.Response(
            200,
            json={
                **RESPONSE,
                "usage": {
                    "prompt_tokens": 90,
                    "completion_tokens": 50,
                    "total_tokens": 140,
                },
            },
        )

    async with gateway(tmp_path, handle) as (client, app):
        response = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert response.status_code in (502, 503)
        billing = (await client.get("/v1/billing", headers=AUTH)).json()
        assert (billing["charged"], billing["reserved"]) == (0, 260)
        assert billing["recent_requests"][0]["state"] == "uncertain"


async def test_known_connection_failure_releases_budget(tmp_path):
    def handle(request):
        raise httpx.ConnectError("down", request=request)

    async with gateway(tmp_path, handle) as (client, app):
        assert (
            await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        ).status_code == 503
        assert app.state.service.ledger.snapshot("alice", 1000)["reserved"] == 0


async def test_queue_overload_has_retry_after_without_reservation(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handle(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=RESPONSE)

    async with gateway(tmp_path, handle, concurrency=1, queue_size=0) as (client, app):
        active = asyncio.create_task(
            client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        )
        await entered.wait()
        response = await client.post("/v1/chat/completions", json=PAYLOAD, headers=AUTH)
        assert response.status_code == 429 and response.headers["retry-after"] == "1"
        assert len(app.state.service.ledger.recent("alice")) == 1
        release.set()
        assert (await active).status_code == 200


@pytest.mark.parametrize(
    "extra",
    [
        {"n": 2},
        {"max_tokens": 21},
        {"max_tokens": True},
        {"tools": []},
        {"model": "unknown-model"},
    ],
)
async def test_unsupported_or_unbounded_options_rejected(tmp_path, extra):
    async with gateway(tmp_path) as (client, app):
        response = await client.post(
            "/v1/chat/completions", json={**PAYLOAD, **extra}, headers=AUTH
        )
        assert response.status_code in (404, 422)
        assert app.state.service.ledger.recent("alice") == []


async def test_large_body_rejected_before_validation(tmp_path):
    async with gateway(tmp_path) as (client, app):
        response = await client.post("/v1/chat/completions", content=b"x" * 262145, headers=AUTH)
        assert response.status_code == 413


async def test_readiness_accepts_fallback_and_detects_total_outage(tmp_path):
    up = True

    def handle(request):
        return httpx.Response(200 if up and request.url.host == "fallback" else 503)

    async with gateway(tmp_path, handle) as (client, app):
        assert (await client.get("/health/ready")).status_code == 200
        up = False
        assert (await client.get("/health/ready")).status_code == 503
        assert (await client.get("/health/live")).status_code == 200
