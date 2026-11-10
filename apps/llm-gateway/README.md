# LLM Gateway

`llm_gateway.main:app`이 실행 진입점입니다. routing, billing, monitoring 패키지를 조립하는
FastAPI 앱이며, 개발용 시뮬레이터는 `python -m llm_gateway.dev`로 실행합니다.

설치·테스트·wheel 빌드·컨테이너 빌드·Mac 개발 환경·vLLM 연결 방법은
[루트 README](../../README.md)에 정리되어 있습니다.

[설계 및 API 정책](../../docs/architecture.md) ·
[책 검토 기록](../../docs/book-review.md)
