# Project FORGE

FORGE는 로봇과 임베디드 제품의 하드웨어 변경을 감지해 펌웨어, BOM, 시험, 프로토콜, 문서의 불일치를 찾아내고 출시 준비 상태를 증거 기반으로 판정하는 엔지니어링 변경관리 AI입니다.

FORGE는 CAD·PLM·Git·CI를 대체하지 않습니다. 각 시스템이 소유한 revision과 실행 결과를 읽기 전용 snapshot으로 연결하고, 변경 영향·필수 재시험·출시 차단 근거를 만드는 **검증 계층**입니다.

현재 상태는 **변경관리 계약 kernel, 보안 루프백 API와 읽기 전용 release-readiness 대시보드**입니다. 출시 후보나 실제 장치 제어 제품이 아니며, 결정론적 변경 영향·증거·판정 계약과 읽기 전용 통합 경계부터 고정합니다.

## 먼저 읽을 문서

1. `PRD.md` — 제품 범위, 데이터 계약, 품질 게이트, 마일스톤
2. `AGENTS.md` — 구현 규칙과 완료 조건

## 현재 포함된 것

- 변경관리 중심 PRD v4
- 재착수용 작업 규칙
- 생성 파일과 비밀정보를 제외하는 ignore 정책
- Pydantic 기반 핵심 계약과 결정론적 fake plugin
- CAD·PLM·Git·CI 등 외부 원본을 보존하는 읽기 전용 artifact snapshot 계약
- 하드웨어 revision 변경 영향과 도메인별 근거 계약
- 시스템 설계 revision과 변경 영향별 증거 무효화 계약
- simulation·bench·HIL·실제 장치 증거를 서로 대체할 수 없게 구분하는 계약
- pin·전압·단위·명령 범위·safe state 인터페이스 검증
- 출처·시점·MOQ·배송·세금 범위를 강제하는 예산 판정
- 변경 항목에 따른 필수 재시험과 증거 기반 `READY`/`BLOCKED` 판정
- append-only connector snapshot·변경 영향·원시 증거·출시 판정 이력
- 프로젝트 생성 시 고정되고 비용 증거가 임의로 바꿀 수 없는 release budget policy
- CAD·PLM·Git·CI 전용 읽기 전용 adapter manifest와 결정론적 fake connector
- Host·Origin·CSRF·JSON framing·body limit·멱등성·낙관적 동시성을 강제하는 `127.0.0.1` API
- hardware revision delta, mismatch, required retest, BOM·build provenance와 명시적 evidence tier를 보여주는 `/app/` 읽기 전용 대시보드
- 한 번에 실행하는 lint·타입·잠금·테스트·커버리지 검증 명령

## 현재 포함되지 않은 것

- CAD 편집·생성, PLM 승인, Git 호스팅 또는 CI 실행 기능
- 외부 시스템의 원본 artifact 수정
- 설치되지 않은 미래 마일스톤용 빈 폴더
- 실제 장치 discovery·제어, firmware 생성·설치와 HIL

## 다음 작업

첫 수직 절편은 hardware revision snapshot을 비교하고, firmware·BOM·시험·protocol·문서의 영향을 계산하고, 필요한 재시험과 출시 차단 근거를 API와 대시보드로 연결합니다. 다음 작업은 end-to-end traceability, adversarial QA와 파일 기반 CAD/PLM export·로컬 Git·CI result connector의 production 경계를 검증하는 것입니다.

코드를 추가하기 전 `PRD.md`의 R0 완료 게이트와 `AGENTS.md`의 검증 규칙을 기준으로 작업 범위를 고정합니다.

## 현재 실행 방법

Python 3.14.3 환경에서 개발 의존성을 설치한 뒤 다음 root 명령을 사용합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
./forge dev
./forge dashboard
./forge verify
```

`dev`는 결정론적 read-only PLM·Git connector로 `HW-11`→`HW-12` 변경을 저장·분석하고, 자동 지정된 재시험과 BOM·build·시험 증거를 수집한 뒤 저장된 `READY` 판정과 canonical hash를 출력합니다. 동일 판정 요청의 idempotent replay도 검증하며, 외부 CAD·PLM·Git·CI를 수정하거나 실제 장치를 제어하지 않습니다.

`dashboard`는 기본 `forge.db`를 열어 `http://127.0.0.1:43127/app/`에서 읽기 전용 화면을 제공합니다. 다른 DB나 포트는 `FORGE_DATABASE_PATH`와 `FORGE_DASHBOARD_PORT`로 지정합니다. 화면에 project ID와 API가 반환한 canonical decision hash를 입력하면 저장된 판정만 조회하며, build·시험·외부 시스템 변경을 시작하지 않습니다.
