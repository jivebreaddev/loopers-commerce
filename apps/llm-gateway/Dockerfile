FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /bin/uv
WORKDIR /workspace
COPY pyproject.toml uv.lock /workspace/
COPY modules/llm-routing /workspace/modules/llm-routing
COPY modules/token-billing /workspace/modules/token-billing
COPY supports/llm-monitoring /workspace/supports/llm-monitoring
COPY apps/llm-gateway /workspace/apps/llm-gateway
RUN uv sync --frozen --no-dev --all-packages --no-editable \
    && useradd --create-home gateway \
    && mkdir /data && chown gateway:gateway /data
USER gateway
ENV LLM_LEDGER_PATH=/data/llm-billing.sqlite3
ENV PATH="/workspace/.venv/bin:$PATH"
EXPOSE 8000
# Queue/circuit state is process-local: one worker for the local deployment.
CMD ["uvicorn", "llm_gateway.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
