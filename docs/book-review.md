# 참고 도서 검토와 구현 대응

2026-11-10에 agent-desktop으로 브라우저 본문을 읽고 확인한 기록입니다.
첫 책은 18개 장·부록 3개·목차를, 두 번째 책은 현재 공개된 14개 절을 확인했습니다.
두 번째 책의 2.4 절 뒤에는 마지막 페이지라고 표시되어 있으며, 소개에서 예고한 후속 장을
이미 공개된 본문으로 간주하지 않았습니다.

## 온프레미스 LLM 구축기: 데이터 주권부터 서빙까지

| 읽은 장 | 현재 프로젝트에 반영한 내용 / 연결이 필요한 부분 |
| --- | --- |
| [1장. 왜 온프레미스인가](https://wikidocs.net/419288) | 로컬 HTTP backend 분리와 외부 LLM 전송 없는 기본 구성 |
| [2장. 클라우드 LLM의 한계](https://wikidocs.net/419290) | 명시적 용량 제한·요금 설정·월 예산 관리 |
| [3장. 규제와 데이터 주권](https://wikidocs.net/419293) | 프롬프트·응답 원문을 원장에 저장하지 않음; 법적 판단은 구현 범위 밖 |
| [4장. GPU 서버 선택과 구성](https://wikidocs.net/419296) | GPU 구매 수치 대신 Mac 개발 경로와 NVIDIA 실행 구성을 구분 |
| [5장. 네트워크와 스토리지 설계](https://wikidocs.net/419299) | loopback 포트, backend별 인증 키, 모델·원장 볼륨 분리 |
| [6장. 오픈소스 모델 선택](https://wikidocs.net/419302) | 모델 ID와 로컬 가중치 경로를 환경변수로 구성 |
| [7장. 양자화와 경량화](https://wikidocs.net/419304) | 가중치 선택은 서버 설정으로 분리; 자동 양자화 작업은 없음 |
| [8장. vLLM 서빙 인프라](https://wikidocs.net/419306) | 별도 vLLM 서버, OpenAI HTTP 호환 API, 캐시·배치 설정 |
| [9장. Ollama와 로컬 실험환경](https://wikidocs.net/419309) | Mac용 모의 HTTP 서버 제공; Ollama 자동 설치·다운로드는 없음 |
| [10장. 로드밸런싱과 고가용성](https://wikidocs.net/419311) | 우선순위 체인·health 관측·circuit breaker; 공유 gateway HA는 확장 대상 |
| [11장. FastAPI 게이트웨이 설계](https://wikidocs.net/419314) | 앱·공통 모듈 분리, API 키 인증, 모델 권한, CORS |
| [12장. Authentik SSO 연동](https://wikidocs.net/419317) | API 키 기반 로컬 사용자 매핑; OIDC/LDAP 배포는 확장 대상 |
| [13장. 토큰 과금 시스템](https://wikidocs.net/419319) | SQLite 원장, 계정·부서·전체 예산, 원자적 예약·실제 usage 정산 |
| [14장. 스트리밍 SSE 구현](https://wikidocs.net/419322) | 분할 UTF-8/SSE 읽기, 최종 usage 정산, 중단 정리와 예약 보존 |
| [15장. 모델 폴백 전략](https://wikidocs.net/419325) | 장애 전환, 엔진 큐 기반 우회, 무료 모델 강등, 실제 모델 응답 헤더 |
| [16장. 모니터링과 장애 대응](https://wikidocs.net/419328) | Prometheus/Grafana, 서버·큐·예산 경보; 외부 알림 연결은 별도 |
| [17장. 모델 업데이트와 A/B 테스트](https://wikidocs.net/419331) | 안정적인 사용자 해시 기반 canary와 실제 모델 원장 기록 |
| [18장. 하이브리드 클라우드 전략](https://wikidocs.net/419334) | 로컬 backend만 기본 연결; 외부 provider 자동 연결은 없음 |
| [부록 A. 구축 체크리스트](https://wikidocs.net/419336) | pytest, 포맷·lint, 패키지 빌드, HTTP smoke 명령 |
| [부록 B. 비용 계산 가이드](https://wikidocs.net/419339) | 예제 내부 요금과 실제 GPU 원가를 구분 |
| [부록 C. 자주 묻는 질문 (FAQ)](https://wikidocs.net/419343) | KV·배치·context 설정과 Mac/GPU 실행 범위 문서화 |

## vLLM 실습

| 확인한 공개 본문 |
| --- |
| [0. 이 책의 특징](https://wikidocs.net/385763) |
| [1. 이 책의 흐름과 구조](https://wikidocs.net/385773) |
| [2. 고성능 환경 구축](https://wikidocs.net/385777) |
| [제 1 장 대규모 언어 모델의 기초](https://wikidocs.net/385766) |
| [1.1 Transformer 기반 추론의 핵심 원리](https://wikidocs.net/386248) |
| [1.2 토큰화 파이프라인과 입력 처리](https://wikidocs.net/386249) |
| [1.3 메모리 레이아웃과 KV 캐시의 기초](https://wikidocs.net/386250) |
| [1.4 서빙 환경에서의 처리량과 지연 시간의 균형](https://wikidocs.net/386251) |
| [1.5 프로덕션 추론 시스템의 제약 사항](https://wikidocs.net/386252) |
| [제 2 장 vLLM 아키텍처와 실행 모델](https://wikidocs.net/386028) |
| [2.1 vLLM 런타임의 내부 설계](https://wikidocs.net/386245) |
| [2.2 PagedAttention 메커니즘과 메모리 효율성](https://wikidocs.net/386246) |
| [2.3 스케줄러 요청 및 큐 실행](https://wikidocs.net/386242) |
| [2.4 연속 배치와 동적 할당](https://wikidocs.net/386247) |

이 책에서는 특히 1.2의 입력 토큰 처리, 1.3·2.2의 KV 메모리, 1.4의 지연·처리량,
1.5의 backpressure, 2.3·2.4의 스케줄링·연속 배치를 참고했습니다.
KV 블록과 토큰 단위 스케줄링은 vLLM에 맡기고, gateway의 요청 수용 한도와 모델 선택은
독립된 모듈로 구현했습니다.

## 예제에서 그대로 가져오지 않은 부분

- 첫 책 13장의 90% 경고 다음에 100% 초과 분기를 두면 초과 분기에 도달하지 못합니다.
  구현은 추론 전 트랜잭션 안에서 예산을 검사하고 무료 체인으로 전환합니다.
- 첫 책 17장의 Python 내장 hash는 프로세스마다 결과가 달라질 수 있습니다.
  사용자별 canary는 SHA-256으로 계산해 재시작 후에도 같은 배정을 유지합니다.
- 스트림 청크를 받은 뒤 다른 모델을 다시 호출하면 응답이 중복되거나 혼합될 수 있습니다.
  이미 수락한 스트림은 재생성하지 않고 오류 및 미정산 예약으로 남깁니다.
- 토큰 수를 문자 길이로 추측하지 않습니다. 실제 정산은 서버의 usage를 검증해 처리하고,
  사전 예산 확인에는 context 상한을 사용합니다.
- 책의 CPU swap 설명을 모든 vLLM 버전에 동일하게 적용하지 않습니다. 고정한 vLLM 0.11.2
  V1은 기본 선점 방식으로 RECOMPUTE를 사용합니다. 기다림·실행·preemption 메트릭으로
  관측하며 gateway 안에 GPU 메모리 관리기를 재구현하지 않습니다.
- 소개의 하드웨어 가격·외부 API 가격·법률 해석은 현재의 구매·법률 판단으로 재사용하지 않았습니다.
  이 프로젝트의 예제 단가는 사용자 설정용 내부 요금입니다.

버전별 동작은 [vLLM 0.11.2 최적화 공식 문서](https://docs.vllm.ai/en/v0.11.2/configuration/optimization/)와
[엔진 설정](https://docs.vllm.ai/en/v0.11.2/configuration/engine_args/)으로 대조했습니다.

## 검증 범위

테스트는 API 인증·권한, 요청 한도, 모델 장애와 복구, 엔진 대기열 기반 우회, 무료 모델 전환,
계정·부서·전체 예산, 동시 예약, 실제 모델 기록, SSE 분할·중단·타임아웃·정산을 다룹니다.
Python wheel과 source distribution은 네 workspace 패키지 각각 빌드합니다.

Mac의 개발용 시뮬레이터는 실제 모델 추론을 하지 않습니다. GPU 가중치 다운로드, 실제 KV
적중률·성능 수치, Authentik/PostgreSQL/클라우드 연동은 여기서 실행 검증한 항목이 아닙니다.
