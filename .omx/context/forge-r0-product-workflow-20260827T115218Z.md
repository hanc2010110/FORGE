# Autopilot Context — FORGE R0 Product Workflow

- Activation prompt status: `activation-prompt`
- Context type: brownfield
- Prompt-safe initial-context summary: `not_needed`
- Source of truth: `PRD.md`, `AGENTS.md`

## Task seed

사용자는 코딩 경험이 적으며 FORGE를 처음부터 끝까지 함께 완성하되, 구현·테스트·문서화는 Codex가 주도하기를 원한다. 우선 해결할 유료 문제는 hardware revision drift, 팀 간 수작업 전달, 인터페이스 불일치, 근거 없는 예산 판정, simulation과 실물 완성의 혼동이다.

## Desired outcome

현재 R0 결정론적 계약 kernel을 실제로 사용할 수 있는 로컬 제품 흐름으로 확장한다. 다음 안전한 절편은 프로젝트 저장, 승인된 revision 실행, 증거 조회와 내보내기를 제공하는 로컬 API 및 최소 UI 기반이다.

## Known facts and evidence

- `forge_core/design.py`가 불변 `SystemDesignRevision`, evidence dependency hash, 선택적 무효화와 순차 `DesignMaturity`를 구현한다.
- `forge_core/constraints.py`가 pin·단위·전압·명령 범위·safe state 및 quote 기반 예산 계약을 구현한다.
- 현재 전체 검증은 68개 테스트와 branch 포함 97% coverage로 통과한다.
- 실제 장치 discovery·제어, firmware 설치와 HIL은 R5C/R6 게이트 전까지 금지된다.
- PRD는 R0 저장소로 SQLite, 로컬 loopback API, 최소 웹 흐름을 지정한다.

## Constraints

- AI는 수치 판정이나 장치 명령을 직접 생성하지 않는다.
- 승인되지 않은 명세를 실행하지 않는다.
- 외부 네트워크·외부 프로세스·실제 장치 I/O는 R0에서 제외한다.
- 프로젝트 경로 탈출, Origin/CSRF, XSS와 내부 경로 노출을 차단한다.
- 새 의존성은 필요한 최소 범위로 제한하고 모든 변경은 `./forge verify`로 검증한다.

## Non-goals

- R0에서 실제 로봇·기계 연결 또는 firmware 설치
- 범용 CAD/FEA/CFD와 자동 최적화
- 계정, 클라우드 동기화, 외부 공급처 연동
- 안전 인증, 양산 승인 또는 Product 모드 선언

## Decision boundaries

- Codex는 PRD 안의 안전하고 되돌릴 수 있는 로컬 구현·테스트·문서 결정을 자율적으로 내린다.
- 실제 장치 동작, credential, 외부 배포, 유료 서비스 계약, 안전·법적 범위 확대는 별도 명시적 승인 없이는 수행하지 않는다.

## Open questions

- R0 최소 UI의 구체 시각 디자인은 제품 흐름을 검증하는 범위에서 Codex가 합리적 기본값을 선택한다.
- FastAPI/Next.js 도입 순서는 테스트 가능한 최소 수직 절편을 기준으로 계획 단계에서 확정한다.

## Likely touchpoints

- `forge_core/` 계약·저장·서비스 경계
- `tests/` 저장·API·보안·재실행 계약 테스트
- `forge`, `pyproject.toml`, `requirements.lock`
- `README.md`, `PRD.md`

## Inspected guidance

- `AGENTS.md`: R0 범위, 안전 경계, 테스트·완료 기준
- `README.md`: 현재 kernel 상태와 다음 작업
- `PRD.md`: R0 제품 흐름, 데이터 계약, API·보안·마일스톤

## Terminology ledger

- `PASS/FAIL/INDETERMINATE`는 요구조건 판정이다.
- `DesignMaturity`는 확보한 증거 단계이며 판정과 분리한다.
- `commissioned`는 인증·양산 승인이 아니다.
- “연결”은 하나의 통신 규격이 아니라 검증된 profile과 Local Device Gateway adapter 경계다.
