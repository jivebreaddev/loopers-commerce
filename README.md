# Loopers LLM Serving

FastAPI 게이트웨이와 로컬 vLLM을 연결하는 Python 멀티 모듈 프로젝트입니다.
인증, 모델 장애 전환, 엔진 큐 기반 라우팅, 토큰 사용량 정산, 예산 초과 시 무료 모델 전환과
Prometheus/Grafana 모니터링을 제공합니다.

## 프로젝트 구조

```text
apps/
  llm-gateway/          FastAPI, API 인증, 설정, 유스케이스, 개발용 시뮬레이터
modules/
  llm-routing/          모델 체인, health/queue 관측, circuit breaker, SSE 클라이언트
  token-billing/        SQLite 원장, 계정·부서·전체 월 예산, 예약과 정산
supports/
  llm-monitoring/       Prometheus 메트릭
docker/                vLLM·게이트웨이·모니터링 Compose와 Grafana 대시보드
docs/                  API 정책과 책 검토 기록
```

루트 `pyproject.toml`이 uv workspace를 정의하고, `uv.lock`이 의존성 버전을 고정합니다.
실행 앱과 공통 모듈은 각각 독립적인 Python 패키지입니다.

## 1. 설치 및 Mac에서 개발

Python 3.11~3.14와 [uv](https://docs.astral.sh/uv/getting-started/installation/)가 필요합니다.
CI에서는 Python 3.12를 사용합니다. 모든 명령은 저장소 루트에서 실행합니다.

```bash
uv sync --frozen --all-packages
```

GPU 없이 게이트웨이를 실행하려면 개발용 시뮬레이터를 사용합니다.
별도의 `.env` 파일 없이 동작하며, 실제 LLM 대신 고정된 모의 응답을 반환합니다.

```bash
uv run --no-sync python -m llm_gateway.dev
```

| 서비스 | 주소 |
| --- | --- |
| 게이트웨이 API 문서 | http://127.0.0.1:18000/docs |
| 주 모델 시뮬레이터 제어 | http://127.0.0.1:18001/docs |
| 소형 모델 시뮬레이터 제어 | http://127.0.0.1:18002/docs |
| 게이트웨이 메트릭 | http://127.0.0.1:18000/metrics |

개발용 API 키는 `local-development-only-key`입니다.
예산이 0인 계정은 `local-free-development-key`를 사용합니다.
시뮬레이터는 loopback 주소에만 바인딩하며 `Ctrl+C`로 세 서버를 함께 종료합니다.
개발 원장은 `data/llm-dev-billing.sqlite3`에 저장됩니다.

```bash
curl -N http://127.0.0.1:18000/v1/chat/completions \
  -H 'Authorization: Bearer local-development-only-key' \
  -H 'Content-Type: application/json' \
  -d '{"model":"local-model","messages":[{"role":"user","content":"안녕"}],"max_tokens":32,"stream":true}'
```

장애 시나리오를 자동으로 실행하고 서버를 종료하는 명령도 제공합니다.
임시 원장을 사용하므로 기존 개발 데이터에 영향을 주지 않습니다.

```bash
uv run --no-sync python -m llm_gateway.dev --smoke
# 기본 포트가 사용 중이면
uv run --no-sync python -m llm_gateway.dev --smoke --base-port 18100
```

자동 시나리오는 정상 응답·SSE·엔진 큐 증가·주 서버 장애·예산 소진·스트림 중단·게이트웨이
대기열 제한·메트릭 노출을 검사합니다. 이 명령은 로컬 포트를 열 수 있는 환경에서 실행합니다.

## 2. 테스트 및 빌드

```bash
# 단위·통합 테스트: GPU, Docker, 실행 중인 모델 서버가 필요하지 않습니다.
uv run --no-sync pytest -q -W error

# 정적 검사와 포맷 검사
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .

# 전체 workspace 패키지의 wheel / source distribution 생성
uv build --all-packages
```

빌드 결과는 `dist/`에 생성됩니다. 네 패키지 각각의 `.whl`과 `.tar.gz`가 만들어집니다.
게이트웨이만 빌드하려면 `uv build --package llm-gateway`를 사용합니다.
게이트웨이 wheel은 routing/billing/monitoring 패키지에도 의존합니다.

컨테이너 이미지는 저장소 루트를 빌드 컨텍스트로 사용합니다.

```bash
docker build -f apps/llm-gateway/Dockerfile -t loopers-llm-gateway:local .
```

Dockerfile은 Python 3.12와 고정한 uv 버전을 사용하고,
`uv sync --frozen --no-dev --all-packages --no-editable`로 실행 의존성을 설치합니다.

## 3. 실제 vLLM 연결

Mac에서는 FastAPI 게이트웨이를 실행하고, vLLM 주소를 설정해 연결합니다.
제공하는 GPU Compose는 Linux + NVIDIA GPU + NVIDIA Container Toolkit용입니다.

```bash
cp .env.llm.example .env
# .env의 LLM_ACCOUNTS에 API 키와 월 예산을 입력합니다.
# LLM_PRIMARY_URL / LLM_PRIMARY_MODEL을 실제 서버와 맞춥니다.
uv run --no-sync uvicorn llm_gateway.main:app \
  --host 127.0.0.1 --port 8000 --workers 1
```

기본 모델 API 별칭은 `local-model`입니다. 실제 주 모델의 이름은 `LLM_PRIMARY_MODEL`,
소형 모델의 이름은 `LLM_FALLBACK_MODEL`로 설정합니다.
`LLM_FALLBACK_URL`을 지정하면 보조 서버를 사용합니다.
`LLM_FALLBACK_FREE=true`이면 해당 모델의 입력·출력 요금을 0으로 계산합니다.

이미 vLLM이 설치된 GPU 환경에서는 다음처럼 실행할 수 있습니다.
가중치 경로와 API 키를 실제 값으로 바꾸고, 게이트웨이 설정과 일치시킵니다.

```bash
vllm serve /absolute/path/to/model \
  --served-model-name local-primary \
  --host 127.0.0.1 --port 8001 \
  --api-key local-vllm-primary \
  --generation-config vllm \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.85 \
  --kv-cache-dtype auto --block-size 16 \
  --enable-prefix-caching --enable-chunked-prefill \
  --max-num-seqs 8 --max-num-batched-tokens 4096
```

원격 GPU 서버를 SSH 터널로 연결하는 경우:

```bash
ssh -N -L 8001:127.0.0.1:8001 -L 8002:127.0.0.1:8002 user@gpu-server
```

모든 연결 모델의 `--max-model-len`을 `LLM_CONTEXT_LIMIT`과 맞춥니다.
소형 모델은 다른 가중치·토크나이저를 사용할 수 있으며 사용량은 각 서버가 반환한 값을 정산합니다.

## 4. GPU 서버에서 전체 스택 실행

`.env`에서 실제 모델 경로, GPU ID, 메모리 비율을 설정합니다.
컨테이너 사이의 주소도 다음과 같이 추가합니다.

```dotenv
LLM_DOCKER_PRIMARY_URL=http://vllm-primary:8000
LLM_DOCKER_FALLBACK_URL=http://vllm-fallback:8000
```

```bash
docker compose --env-file .env -f docker/llm-compose.yml \
  --profile gpu --profile gpu-fallback up -d --build
```

| 서비스 | 주소 |
| --- | --- |
| 게이트웨이 | http://localhost:8000/docs |
| Prometheus | http://localhost:9091 |
| Grafana | http://localhost:3001 |

Grafana는 `admin`과 `.env`의 `GRAFANA_ADMIN_PASSWORD`로 로그인합니다.
**LLM Serving → Loopers LLM Serving** 대시보드가 자동 등록됩니다.
주 모델만 실행한다면 `LLM_DOCKER_FALLBACK_URL`을 비우고 `--profile gpu-fallback`을 생략합니다.

기본 예제 가중치는 두 서버 모두 작은 Qwen 모델입니다. 실제 대형/소형 조합은
`VLLM_MODEL_ID`와 `VLLM_FALLBACK_MODEL_ID`를 각각 지정합니다.
로컬 가중치는 `VLLM_MODEL_DIR=/absolute/host/models`,
`VLLM_MODEL_ID=/models/your-primary-model`처럼 마운트합니다.
기본 GPU ID는 주 서버 0, 보조 서버 1입니다. 같은 GPU를 공유할 때는 두 프로세스의
가중치·KV·런타임 메모리를 합산해 각 메모리 비율을 낮춰야 합니다.

이미 호스트/원격에서 동작하는 vLLM에 gateway 컨테이너만 연결할 경우에는 GPU profile을
생략하고 `LLM_DOCKER_*_URL`을 접근 가능한 주소로 바꿉니다.
`docker/llm-monitoring/prometheus.yml`의 vLLM scrape 주소도 함께 변경합니다.

Compose 문법과 환경변수만 검사하려면 Docker 데몬 없이 다음 명령을 사용할 수 있습니다.

```bash
LLM_ENV_FILE=../.env.llm.example docker compose --env-file .env.llm.example \
  -f docker/llm-compose.yml --profile gpu --profile gpu-fallback config --quiet
```

## 동작 정책과 문서

- 주 서버 장애 또는 엔진 대기열 초과: 다음 모델로 라우팅합니다.
- 계정·부서·전체 예산 부족: 권한이 허용된 무료 모델로 전환합니다. 무료 모델이 없으면 402입니다.
- 게이트웨이 자체의 총 처리 한도 초과: 429와 `Retry-After`를 반환합니다.
- SSE 완료: 최종 usage로 정산합니다. 중단되거나 사용량이 불확실하면 예약을 유지합니다.
- 응답의 실제 모델은 `X-Model-Used`, 전환 여부는 `X-Fallback`으로 확인합니다.

[API·과금·라우팅 설계](docs/architecture.md)와
[두 책의 전체 검토 및 구현 대응표](docs/book-review.md)를 참고하세요.
