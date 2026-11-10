from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Account(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    api_key: SecretStr = Field(min_length=16)
    monthly_budget_micro_usd: int = Field(default=10_000_000, ge=0)
    department: str | None = None
    allowed_models: list[str] = Field(default_factory=list)
    admin: bool = False


class BackendConfig(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    url: str
    model: str = Field(min_length=1)
    api_key: SecretStr
    priority: int = Field(default=0, ge=0)
    input_price: int = Field(default=2, ge=0, le=1_000_000)
    output_price: int = Field(default=6, ge=0, le=1_000_000)
    max_queue: int = Field(default=10, ge=0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_", env_file=".env", extra="ignore")

    # No default caller credential: an unconfigured gateway fails closed at startup.
    accounts: list[Account] = Field(min_length=1)
    model: str = "local-model"
    primary_url: str = "http://127.0.0.1:8001"
    primary_api_key: SecretStr = SecretStr("local-vllm-primary")
    fallback_url: str | None = None
    fallback_api_key: SecretStr = SecretStr("local-vllm-fallback")
    primary_model: str | None = None
    fallback_model: str | None = None
    fallback_free: bool = True
    backends: list[BackendConfig] | None = Field(default=None, min_length=1)
    health_poll_interval_seconds: float = Field(default=5, ge=0, le=60)
    backend_max_queue: int = Field(default=10, ge=0)
    department_budgets: dict[str, int] = Field(default_factory=dict)
    canary_backend: str | None = None
    canary_ratio: float = Field(default=0, ge=0, le=1)
    cors_origins: list[str] = Field(default_factory=list)
    context_limit: int = Field(default=4096, ge=2, le=131072)
    max_output_tokens: int = Field(default=1024, ge=1)
    concurrency: int = Field(default=4, ge=1, le=1024)
    queue_size: int = Field(default=16, ge=0, le=10000)
    queue_wait_seconds: float = Field(default=5, gt=0, le=300)
    upstream_timeout_seconds: float = Field(default=60, gt=0, le=600)
    circuit_failure_threshold: int = Field(default=3, ge=1)
    circuit_recovery_seconds: float = Field(default=15, gt=0)
    global_monthly_budget_micro_usd: int = Field(default=20_000_000, ge=0)
    input_micro_usd_per_token: int = Field(default=2, ge=0, le=1_000_000)
    output_micro_usd_per_token: int = Field(default=6, ge=0, le=1_000_000)
    ledger_path: str = "./data/llm-billing.sqlite3"

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.max_output_tokens >= self.context_limit:
            raise ValueError("max_output_tokens must be below context_limit")
        if len({a.name for a in self.accounts}) != len(self.accounts):
            raise ValueError("Account names must be unique")
        if len({a.api_key.get_secret_value() for a in self.accounts}) != len(self.accounts):
            raise ValueError("Account API keys must be unique")
        for url in (self.primary_url, self.fallback_url):
            if url and not url.startswith(("http://", "https://")):
                raise ValueError("Backend URLs must use http(s)")
        if self.backends:
            if len({b.name for b in self.backends}) != len(self.backends):
                raise ValueError("Backend names must be unique")
            if any(not b.url.startswith(("http://", "https://")) for b in self.backends):
                raise ValueError("Backend URLs must use http(s)")
        names = {b.name for b in self.backend_configs()}
        if self.canary_backend and self.canary_backend not in names:
            raise ValueError("canary_backend must name a configured backend")
        if any(v < 0 for v in self.department_budgets.values()):
            raise ValueError("Department budgets must be non-negative")
        return self

    def backend_configs(self) -> list[BackendConfig]:
        if self.backends:
            return self.backends
        configs = [
            BackendConfig(
                name="primary",
                url=self.primary_url,
                api_key=self.primary_api_key,
                model=self.primary_model or self.model,
                input_price=self.input_micro_usd_per_token,
                output_price=self.output_micro_usd_per_token,
                max_queue=self.backend_max_queue,
            )
        ]
        if self.fallback_url:
            configs.append(
                BackendConfig(
                    name="fallback",
                    url=self.fallback_url,
                    api_key=self.fallback_api_key,
                    model=self.fallback_model or self.model,
                    priority=1,
                    input_price=0 if self.fallback_free else self.input_micro_usd_per_token,
                    output_price=0 if self.fallback_free else self.output_micro_usd_per_token,
                    max_queue=self.backend_max_queue,
                )
            )
        return configs
