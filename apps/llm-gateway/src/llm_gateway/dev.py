"""Explicit development-only vLLM protocol simulator. Does not run a language model."""

import argparse
import asyncio
import json
import signal
import tempfile
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .main import create_app
from .settings import Settings

DEV_KEY = "local-development-only-key"
FREE_KEY = "local-free-development-key"


class Controls(BaseModel):
    status: int = Field(default=200, ge=200, le=599)
    waiting: int = Field(default=0, ge=0)
    delay_seconds: float = Field(default=0, ge=0, le=5)
    broken_stream: bool = False


def simulator(model):
    app = FastAPI(title=f"SIMULATED vLLM: {model}")
    app.state.controls = Controls()

    @app.post("/_dev/control")
    async def control(body: Controls):
        app.state.controls = body
        return body

    @app.get("/health")
    async def health():
        return Response(status_code=app.state.controls.status)

    @app.get("/metrics")
    async def metrics():
        return Response(
            f'vllm:num_requests_waiting{{model_name="{model}"}} '
            f"{app.state.controls.waiting}\n"
            f'vllm:kv_cache_usage_perc{{model_name="{model}"}} 0.25\n',
            media_type="text/plain",
        )

    @app.post("/v1/chat/completions")
    async def chat(body: dict):
        controls = app.state.controls
        if controls.status != 200:
            return JSONResponse(
                {"error": {"message": "simulated outage"}}, status_code=controls.status
            )
        await asyncio.sleep(controls.delay_seconds)
        count = min(body.get("max_tokens", 5), 5)
        usage = {"prompt_tokens": 20, "completion_tokens": count, "total_tokens": 20 + count}
        text = f"[{model}] 개발용 모의 응답입니다. 실제 모델 추론은 수행하지 않습니다."
        request_id = "chatcmpl-sim-" + uuid4().hex
        if not body.get("stream"):
            return {
                "id": request_id,
                "object": "chat.completion",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            }

        async def events():
            for part in (text[: len(text) // 2], text[len(text) // 2 :]):
                event = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}],
                }
                yield ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
                await asyncio.sleep(0.01)
            if controls.broken_stream:
                return
            finish = {
                "id": request_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
            yield ("data: " + json.dumps(finish) + "\n\n").encode()
            yield ("data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n").encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return app


class DevServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # One supervisor owns shutdown for all three servers in this process.
        yield


async def exercise(port, app):
    base = f"http://127.0.0.1:{port}"
    payload = {
        "model": "local-model",
        "messages": [{"role": "user", "content": "로컬 테스트"}],
        "max_tokens": 10,
    }
    auth = {"Authorization": f"Bearer {DEV_KEY}"}
    monitor = app.state.service.router.monitor
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        await monitor.poll()

        async def chat(headers=auth, **extra):
            return await client.post(
                base + "/v1/chat/completions", json={**payload, **extra}, headers=headers
            )

        normal = await chat()
        assert normal.status_code == 200 and normal.headers["x-llm-backend"] == "primary"
        print("PASS primary response and token settlement")
        streamed = await chat(stream=True)
        assert "[DONE]" in streamed.text and '"error"' not in streamed.text
        print("PASS SSE completion and final usage settlement")

        async def controls(**body):
            response = await client.post(f"http://127.0.0.1:{port + 1}/_dev/control", json=body)
            response.raise_for_status()
            await monitor.poll()

        await controls(waiting=11)
        queued = await chat()
        assert queued.status_code == 200 and queued.headers["x-routing-reason"] == "queue"
        print("PASS vLLM queue overload -> small model")
        await controls(status=503)
        outage = await chat()
        assert outage.status_code == 200 and outage.headers["x-model-used"] == "sim-small-model"
        print("PASS primary outage -> small model")
        await controls()
        free = await chat(headers={"Authorization": f"Bearer {FREE_KEY}"})
        assert free.status_code == 200 and free.headers["x-routing-reason"] == "budget"
        assert free.headers["x-charged-micro-usd"] == "0"
        print("PASS exhausted account budget -> audited free model")
        await controls(broken_stream=True)
        broken = await chat(stream=True)
        assert '"error"' in broken.text and "[DONE]" not in broken.text
        print("PASS interrupted SSE -> uncertain reservation, no replay")
        await controls(delay_seconds=0.15)
        overloaded = await asyncio.gather(*(chat() for _ in range(8)))
        assert any(r.status_code == 429 for r in overloaded)
        print("PASS gateway backpressure -> bounded queue and 429")
        metrics = await client.get(base + "/metrics")
        assert "llm_gateway_fallbacks_total" in metrics.text
        assert app.state.service.admission.active == app.state.service.admission.waiting == 0
        print("PASS Prometheus metrics and released request slots")


async def run(port, smoke, ledger_path):
    config = Settings(
        _env_file=None,
        accounts=[
            {
                "name": "developer",
                "api_key": DEV_KEY,
                "monthly_budget_micro_usd": 100_000_000,
                "department": "development",
                "admin": True,
            },
            {"name": "exhausted", "api_key": FREE_KEY, "monthly_budget_micro_usd": 0},
        ],
        primary_url=f"http://127.0.0.1:{port + 1}",
        primary_model="sim-primary-model",
        fallback_url=f"http://127.0.0.1:{port + 2}",
        fallback_model="sim-small-model",
        fallback_free=True,
        ledger_path=ledger_path,
        health_poll_interval_seconds=0.2,
        concurrency=2,
        queue_size=2,
        circuit_failure_threshold=3,
        circuit_recovery_seconds=0.1,
        global_monthly_budget_micro_usd=200_000_000,
        department_budgets={"development": 100_000_000},
    )
    app = create_app(config)
    apps = [app, simulator("sim-primary-model"), simulator("sim-small-model")]
    servers = [
        DevServer(
            uvicorn.Config(
                a, host="127.0.0.1", port=port + i, log_level="warning", access_log=False
            )
        )
        for i, a in enumerate(apps)
    ]
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        async with asyncio.timeout(10):
            while not all(s.started for s in servers):
                if any(t.done() for t in tasks):
                    await asyncio.gather(*tasks)
                    raise RuntimeError("Development server failed to start")
                await asyncio.sleep(0.05)
        if smoke:
            await exercise(port, app)
        else:
            print(f"SIMULATOR ONLY / no LLM inference: http://127.0.0.1:{port}/docs", flush=True)
            print(f"Bearer key: {DEV_KEY}\nZero-budget key: {FREE_KEY}", flush=True)
            print(f"Backend controls: http://127.0.0.1:{port + 1}/docs", flush=True)
            await stopped.wait()
    finally:
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="Run HTTP scenarios then stop")
    parser.add_argument("--base-port", type=int, default=18000)
    args = parser.parse_args()
    if not 1024 <= args.base_port <= 65000:
        parser.error("base-port must be between 1024 and 65000")
    if args.smoke:
        with tempfile.TemporaryDirectory(prefix="loopers-llm-smoke-") as directory:
            asyncio.run(run(args.base_port, True, str(Path(directory) / "billing.db")))
    else:
        asyncio.run(run(args.base_port, False, "./data/llm-dev-billing.sqlite3"))


if __name__ == "__main__":
    main()
