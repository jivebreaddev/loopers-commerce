from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest


class Metrics:
    def __init__(self):
        # An app-local registry also isolates tests and embedded applications.
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "llm_gateway_requests",
            "Completion requests by outcome",
            ["outcome"],
            registry=self.registry,
        )
        self.attempts = Counter(
            "llm_gateway_backend_attempts",
            "Routing attempts",
            ["backend", "outcome"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "llm_gateway_request_seconds",
            "Total completion latency including queue",
            buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
            registry=self.registry,
        )
        self.tokens = Counter(
            "llm_gateway_tokens",
            "Settled tokens",
            ["kind"],
            registry=self.registry,
        )
        self.charges = Counter(
            "llm_gateway_charged_micro_usd",
            "Charges since process start",
            registry=self.registry,
        )
        self.active = Gauge("llm_gateway_active", "Running requests", registry=self.registry)
        self.waiting = Gauge("llm_gateway_waiting", "Queued requests", registry=self.registry)
        self.available = Gauge(
            "llm_gateway_budget_available_micro_usd",
            "Global available monthly budget",
            registry=self.registry,
        )
        self.committed = Gauge(
            "llm_gateway_budget_committed_micro_usd",
            "Global charges plus reservations",
            registry=self.registry,
        )
        self.circuit = Gauge(
            "llm_gateway_circuit_open",
            "Open or half-open circuit",
            ["backend"],
            registry=self.registry,
        )
        self.fallbacks = Counter(
            "llm_gateway_fallbacks",
            "Successful routing decisions by fallback reason",
            ["reason", "backend"],
            registry=self.registry,
        )
        self.backend_ready = Gauge(
            "llm_gateway_backend_ready",
            "Fresh healthy and non-overloaded backend",
            ["backend"],
            registry=self.registry,
        )
        self.engine_waiting = Gauge(
            "llm_gateway_engine_waiting",
            "Last observed vLLM waiting queue",
            ["backend"],
            registry=self.registry,
        )
        self.kv_usage = Gauge(
            "llm_gateway_engine_kv_usage",
            "Last observed KV utilization",
            ["backend"],
            registry=self.registry,
        )
        self.department_available = Gauge(
            "llm_gateway_department_available_micro_usd",
            "Department available monthly budget",
            ["department"],
            registry=self.registry,
        )
        self.department_budget = Gauge(
            "llm_gateway_department_budget_micro_usd",
            "Department monthly budget",
            ["department"],
            registry=self.registry,
        )

    def render(self) -> bytes:
        return generate_latest(self.registry)
