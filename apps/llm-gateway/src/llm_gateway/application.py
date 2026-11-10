import json
import time
from contextlib import asynccontextmanager

from llm_monitoring import Metrics
from llm_routing import (
    AdmissionController,
    QueueFull,
    QueueTimeout,
    Router,
    UpstreamRejected,
    UpstreamUnavailable,
)
from token_billing import BudgetExceeded, InvalidUsage, Ledger

from .domain import ChatRequest
from .settings import Account, Settings


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: int | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.retry_after = retry_after


class CompletionService:
    def __init__(
        self,
        settings: Settings,
        router: Router,
        ledger: Ledger,
        admission: AdmissionController,
        metrics: Metrics,
    ):
        self.settings, self.router, self.ledger = settings, router, ledger
        self.admission, self.metrics = admission, metrics

    def _candidates(self, request, account):
        models = {b.model for b in self.router.backends}
        if request.model != self.settings.model and request.model not in models:
            raise GatewayError(404, "model_not_found", "Unknown model")
        if request.max_tokens > self.settings.max_output_tokens:
            raise GatewayError(422, "token_limit", "max_tokens exceeds gateway limit")
        preferred = None
        if request.model != self.settings.model:
            preferred = next(b.name for b in self.router.backends if b.model == request.model)
            if account.allowed_models and request.model not in account.allowed_models:
                raise GatewayError(403, "model_forbidden", "Model access denied")
        candidates = self.router.candidates(preferred, account.name)
        if account.allowed_models:
            candidates = [b for b in candidates if b.model in account.allowed_models]
        if not candidates:
            raise GatewayError(403, "model_forbidden", "No permitted model is configured")
        return candidates

    def _reserve(self, request, account, request_id, candidates):
        reason = "preferred"

        def reserve(chain):
            self.ledger.reserve(
                request_id,
                account.name,
                account.monthly_budget_micro_usd,
                self.settings.global_monthly_budget_micro_usd,
                self.settings.context_limit,
                request.max_tokens,
                max(b.input_price for b in chain),
                max(b.output_price for b in chain),
                account.department,
                self.settings.department_budgets.get(account.department),
            )

        try:
            reserve(candidates)
        except BudgetExceeded as exc:
            candidates = [b for b in candidates if b.free]
            if not candidates:
                raise GatewayError(
                    402,
                    "budget_exceeded",
                    "No budget or free model is available",
                ) from exc
            reserve(candidates)
            reason = "budget"
        return candidates, reason

    @asynccontextmanager
    async def _execution(self, request, account, request_id):
        start, outcome, reserved = time.monotonic(), "error", False
        try:
            candidates = self._candidates(request, account)
            async with self.admission.slot():
                candidates, reason = self._reserve(request, account, request_id, candidates)
                reserved = True
                yield candidates, reason
                outcome = "success"
        except QueueFull as exc:
            outcome = "queue_full"
            raise GatewayError(429, outcome, "Gateway queue is full", 1) from exc
        except QueueTimeout as exc:
            outcome = "queue_timeout"
            raise GatewayError(429, outcome, "Queue wait limit exceeded", 1) from exc
        except UpstreamRejected as exc:
            outcome = "upstream_rejected"
            self.ledger.finish_failed(request_id, uncertain=False)
            raise GatewayError(
                exc.status_code if exc.status_code in (400, 413, 422) else 502,
                outcome,
                "Model server rejected the request",
            ) from exc
        except UpstreamUnavailable as exc:
            outcome = "model_unavailable"
            self.ledger.finish_failed(request_id, uncertain=exc.uncertain)
            raise GatewayError(503, outcome, "No model response is available", 5) from exc
        except InvalidUsage as exc:
            outcome = "invalid_usage"
            self.ledger.finish_failed(request_id, uncertain=True)
            raise GatewayError(502, outcome, "Model usage cannot be safely settled") from exc
        except GatewayError as exc:
            outcome = exc.code
            raise
        except BaseException:
            outcome = "cancelled" if reserved else "error"
            if reserved:
                self.ledger.finish_failed(request_id, uncertain=True)
            raise
        finally:
            self.metrics.requests.labels(outcome).inc()
            self.metrics.latency.observe(time.monotonic() - start)

    @staticmethod
    def _usage(data):
        usage = data.get("usage")
        if not isinstance(usage, dict):
            raise InvalidUsage("Missing usage")
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if (
            type(prompt) is not int
            or type(completion) is not int
            or type(usage.get("total_tokens")) is not int
            or usage["total_tokens"] != prompt + completion
        ):
            raise InvalidUsage("Invalid token counts")
        return prompt, completion

    def _settle(self, request_id, backend, data):
        prompt, completion = self._usage(data)
        model = self.router.by_name(backend)
        charged = self.ledger.settle(
            request_id,
            prompt,
            completion,
            backend,
            input_price=model.input_price,
            output_price=model.output_price,
            model=model.model,
        )
        self.metrics.tokens.labels("prompt").inc(prompt)
        self.metrics.tokens.labels("completion").inc(completion)
        self.metrics.charges.inc(charged)
        return charged

    def _headers(self, backend, candidates, reason):
        fallback = reason == "budget" or backend != candidates[0].name
        if backend != candidates[0].name and reason != "budget":
            monitor = self.router.monitor
            observed = monitor.observations.get(candidates[0].name) if monitor else None
            overloaded = (
                observed
                and observed.healthy
                and observed.waiting is not None
                and observed.waiting > candidates[0].max_queue
            )
            reason = "queue" if overloaded else "unavailable"
        if fallback:
            self.metrics.fallbacks.labels(reason, backend).inc()
        return {
            "X-LLM-Backend": backend,
            "X-Model-Used": self.router.by_name(backend).model,
            "X-Fallback": str(fallback).lower(),
            "X-Routing-Reason": reason,
        }

    async def complete(self, request: ChatRequest, account: Account, request_id: str):
        async with self._execution(request, account, request_id) as (candidates, reason):
            backend, data = await self.router.complete(
                request.model_dump(exclude_none=True),
                candidates,
            )
            choices = data.get("choices")
            if (
                not isinstance(choices, list)
                or len(choices) != 1
                or not isinstance(choices[0], dict)
                or not isinstance(choices[0].get("message"), dict)
                or choices[0]["message"].get("role") != "assistant"
                or not isinstance(choices[0]["message"].get("content"), str)
            ):
                raise InvalidUsage("Invalid chat completion")
            charged = self._settle(request_id, backend, data)
            headers = self._headers(backend, candidates, reason)
            headers["X-Charged-Micro-USD"] = str(charged)
            return {**data, "model": self.router.by_name(backend).model}, headers

    async def stream(self, request: ChatRequest, account: Account, request_id: str):
        """First yield is headers; remaining yields are SSE bytes.

        Prefetch headers to reject admission/budget/connect failures before HTTP 200.
        aclose() always closes the upstream and releases the execution slot.
        """
        started = False
        try:
            async with self._execution(request, account, request_id) as (candidates, reason):
                async with self.router.open_stream(
                    request.model_dump(exclude_none=True),
                    candidates,
                ) as (backend, events):
                    started = True
                    yield self._headers(backend, candidates, reason)
                    last_usage = None
                    async for event in events:
                        if event.get("usage") is not None:
                            counts = self._usage(event)
                            if last_usage is not None and counts != self._usage(last_usage):
                                raise InvalidUsage("Conflicting final usage")
                            last_usage = event
                        if not isinstance(event.get("choices"), list):
                            raise InvalidUsage("Invalid stream choices")
                        event = {**event, "model": self.router.by_name(backend).model}
                        if request.stream_options and not request.stream_options.include_usage:
                            event.pop("usage", None)
                            if not event["choices"]:
                                continue
                        yield self._sse(event)
                    if last_usage is None:
                        raise InvalidUsage("Stream has no final usage")
                    self._settle(request_id, backend, last_usage)
            yield b"data: [DONE]\n\n"
        except GatewayError as exc:
            if not started:
                raise
            yield self._sse(
                {"error": {"code": exc.code, "message": exc.message, "request_id": request_id}}
            )

    @staticmethod
    def _sse(event):
        return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
