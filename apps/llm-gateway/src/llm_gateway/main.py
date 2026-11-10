import asyncio
import secrets
import sqlite3
from contextlib import asynccontextmanager, suppress
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from llm_monitoring import Metrics
from llm_routing import AdmissionController, Backend, BackendMonitor, Router
from prometheus_client import CONTENT_TYPE_LATEST
from starlette.background import BackgroundTask
from token_billing import Ledger

from .application import CompletionService, GatewayError
from .domain import ChatRequest
from .settings import Account, Settings


class BodyLimit:
    """Limit raw payload before JSON/Pydantic parsing, including chunked requests."""

    def __init__(self, app, limit=262144):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.limit:
                response = JSONResponse(
                    {"error": {"code": "body_too_large", "message": "Body exceeds 256 KiB"}},
                    status_code=413,
                )
                return await response(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class RequestIds:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def traced(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode()))
            await send(message)

        await self.app(scope, receive, traced)


class ConfiguredCors:
    def __init__(self, app):
        self.app = app
        self.cors = None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.cors is None:
            self.cors = CORSMiddleware(
                self.app,
                allow_origins=scope["app"].state.config.cors_origins,
                allow_methods=["GET", "POST"],
                allow_headers=["Authorization", "Content-Type"],
                expose_headers=["X-Model-Used", "X-Fallback", "X-Request-ID", "X-Routing-Reason"],
            )
        await self.cors(scope, receive, send)


def create_app(settings: Settings | None = None, client: httpx.AsyncClient | None = None):
    @asynccontextmanager
    async def lifespan(app):
        config = settings or Settings()
        metrics = Metrics()
        upstream = client or httpx.AsyncClient(
            timeout=httpx.Timeout(config.upstream_timeout_seconds, connect=2),
            limits=httpx.Limits(max_connections=config.concurrency, max_keepalive_connections=8),
            trust_env=False,
        )
        backends = [
            Backend(
                b.name,
                b.url,
                b.api_key.get_secret_value(),
                b.model,
                b.input_price,
                b.output_price,
                b.priority,
                b.max_queue,
            )
            for b in config.backend_configs()
        ]
        monitor_client = client or httpx.AsyncClient(timeout=2, trust_env=False)
        monitor = (
            BackendMonitor(monitor_client, backends, config.health_poll_interval_seconds)
            if config.health_poll_interval_seconds
            else None
        )
        router = Router(
            upstream,
            backends,
            config.circuit_failure_threshold,
            config.circuit_recovery_seconds,
            config.upstream_timeout_seconds,
            on_attempt=lambda backend, outcome: metrics.attempts.labels(backend, outcome).inc(),
            monitor=monitor,
            canary_backend=config.canary_backend,
            canary_ratio=config.canary_ratio,
        )
        ledger = Ledger(config.ledger_path)
        admission = AdmissionController(
            config.concurrency,
            config.queue_size,
            config.queue_wait_seconds,
        )
        app.state.config = config
        app.state.service = CompletionService(config, router, ledger, admission, metrics)
        monitor_task = None
        try:
            if monitor:
                await monitor.poll()
                monitor_task = asyncio.create_task(monitor.run())
            yield
        finally:
            if monitor_task:
                monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor_task
            if client is None:
                await upstream.aclose()
                await monitor_client.aclose()

    app = FastAPI(title="Local LLM Gateway", version="0.2.0", lifespan=lifespan)
    app.add_middleware(BodyLimit)
    app.add_middleware(RequestIds)
    app.add_middleware(ConfiguredCors)
    bearer = HTTPBearer(auto_error=False)

    @app.exception_handler(GatewayError)
    async def gateway_error(request, exc):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after is not None else {}
        return JSONResponse(
            {
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "request_id": request.state.request_id,
                }
            },
            status_code=exc.status,
            headers=headers,
        )

    @app.exception_handler(sqlite3.Error)
    async def ledger_error(request, exc):
        return JSONResponse(
            {
                "error": {
                    "code": "ledger_unavailable",
                    "message": "Billing ledger unavailable",
                    "request_id": request.state.request_id,
                }
            },
            status_code=503,
        )

    async def authenticate(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Account:
        supplied = credentials.credentials if credentials else ""
        # Check all configured keys; never expose credentials in metrics or responses.
        matched = None
        for account in request.app.state.config.accounts:
            if secrets.compare_digest(
                supplied.encode(), account.api_key.get_secret_value().encode()
            ):
                matched = account
        if matched is None:
            raise GatewayError(401, "invalid_api_key", "A valid Bearer API key is required")
        return matched

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready(request: Request):
        healthy = await request.app.state.service.router.ready()
        return JSONResponse({"ready": healthy}, status_code=200 if healthy else 503)

    @app.get("/v1/models")
    async def models(request: Request, account: Annotated[Account, Depends(authenticate)]):
        configured = request.app.state.service.router.backends
        visible = {
            b.model
            for b in configured
            if not account.allowed_models or b.model in account.allowed_models
        }
        if visible:
            visible.add(request.app.state.config.model)
        return {
            "object": "list",
            "data": [
                {
                    "id": model,
                    "object": "model",
                    "owned_by": "local",
                }
                for model in sorted(visible)
            ],
        }

    @app.post("/v1/chat/completions")
    async def complete(
        body: ChatRequest,
        request: Request,
        account: Annotated[Account, Depends(authenticate)],
    ):
        service = request.app.state.service
        if body.stream:
            events = service.stream(body, account, request.state.request_id)
            headers = await anext(events)
            headers.update({"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

            async def close_events():
                await events.aclose()

            return StreamingResponse(
                events,
                media_type="text/event-stream",
                headers=headers,
                background=BackgroundTask(close_events),
            )
        data, headers = await service.complete(
            body,
            account,
            request.state.request_id,
        )
        return JSONResponse(data, headers=headers)

    @app.get("/v1/billing")
    async def billing(request: Request, account: Annotated[Account, Depends(authenticate)]):
        ledger = request.app.state.service.ledger
        return {
            **ledger.snapshot(account.name, account.monthly_budget_micro_usd),
            "recent_requests": ledger.recent(account.name),
        }

    @app.get("/v1/admin/usage")
    async def summary(request: Request, account: Annotated[Account, Depends(authenticate)]):
        if not account.admin:
            raise GatewayError(403, "admin_required", "Administrator access is required")
        config, ledger = request.app.state.config, request.app.state.service.ledger
        return {
            "global": ledger.global_snapshot(config.global_monthly_budget_micro_usd),
            "accounts": [
                ledger.snapshot(a.name, a.monthly_budget_micro_usd) for a in config.accounts
            ],
            "departments": [
                ledger.department_snapshot(name, budget)
                for name, budget in config.department_budgets.items()
            ],
        }

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request):
        service = request.app.state.service
        service.metrics.active.set(service.admission.active)
        service.metrics.waiting.set(service.admission.waiting)
        snapshot = service.ledger.global_snapshot(service.settings.global_monthly_budget_micro_usd)
        service.metrics.available.set(snapshot["available"])
        service.metrics.committed.set(snapshot["committed"])
        for name, breaker in service.router.breakers.items():
            service.metrics.circuit.labels(name).set(bool(breaker.open_until))
        if service.router.monitor:
            for backend in service.router.backends:
                observed = service.router.monitor.observations.get(backend.name)
                service.metrics.backend_ready.labels(backend.name).set(
                    service.router.monitor.available(backend)
                )
                if observed and observed.waiting is not None:
                    service.metrics.engine_waiting.labels(backend.name).set(observed.waiting)
                if observed and observed.kv_usage is not None:
                    service.metrics.kv_usage.labels(backend.name).set(observed.kv_usage)
        for department, budget in service.settings.department_budgets.items():
            used = service.ledger.department_snapshot(department, budget)
            service.metrics.department_available.labels(department).set(used["available"])
            service.metrics.department_budget.labels(department).set(budget)
        return Response(service.metrics.render(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
