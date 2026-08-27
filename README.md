# Project FORGE

기계·전기·software·비용·실물 증거를 버전된 설계로 묶는 엔지니어링 공동설계 AI를 clean-slate 방식으로 개발하는 저장소입니다.

현재 상태는 **R0 계약 kernel WIP**입니다. R0 전체, 출시 후보 또는 실제 장치 제어 제품이 아닙니다. 장기적으로 승인된 장치 profile을 Local Device Gateway로 연결하지만, 현재는 장치 제어나 firmware 설치 없이 결정론적 데이터·판정·증거 계약부터 고정합니다.

## 먼저 읽을 문서

1. `PRD.md` — 제품 범위, 데이터 계약, 품질 게이트, 마일스톤
2. `AGENTS.md` — 구현 규칙과 완료 조건

## 현재 포함된 것

- 개선된 PRD v3
- 재착수용 작업 규칙
- 생성 파일과 비밀정보를 제외하는 ignore 정책
- Pydantic 기반 핵심 계약과 결정론적 fake plugin
- 시스템 설계 revision과 변경 영향별 증거 무효화 계약
- 분석·build·SIL·HIL·commissioning을 건너뛸 수 없는 성숙도 판정
- pin·전압·단위·명령 범위·safe state 인터페이스 검증
- 출처·시점·MOQ·배송·세금 범위를 강제하는 예산 판정
- 한 번에 실행하는 lint·타입·잠금·테스트·커버리지 검증 명령

## 현재 포함되지 않은 것

- 기존 도메인 특화 프로토타입
- 생성된 프로젝트와 CAD 산출물
- 설치되지 않은 미래 마일스톤용 빈 폴더
- 실제 장치 discovery·제어, firmware 생성·설치와 HIL

## 다음 작업

R0에서 남은 로컬 프로젝트 저장, API, 최소 웹 흐름을 구현합니다. 실제 장치 연결 전에는 R5C에서 계측기 교정·증거 진위, 부품 대체 동등성, firmware 복구와 credential 경계를 추가 검증합니다.

코드를 추가하기 전 `PRD.md`의 R0 완료 게이트와 `AGENTS.md`의 검증 규칙을 기준으로 작업 범위를 고정합니다.

## 현재 실행 방법

Python 3.14.3 환경에서 개발 의존성을 설치한 뒤 다음 root 명령을 사용합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
./forge dev
./forge verify
```

`dev`는 현재 R0 계약 kernel의 예제 명세를 승인 상태로 실행하고 evidence manifest를 출력합니다. 웹/API와 SQLite 저장은 다음 R0 절편에서 추가합니다.
