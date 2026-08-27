# Execution-ready Spec — FORGE R0 Product Workflow

## Metadata

- Profile: standard
- Context: brownfield
- Final ambiguity: 0.09
- Threshold: 0.20
- Context snapshot: `.omx/context/forge-r0-product-workflow-20260827T115218Z.md`

## Intent

코딩 경험이 적은 사용자를 대신해 Codex가 FORGE 개발을 주도한다. 변경되는 하드웨어와 기계·전기·embedded 산출물을 단일 revision과 검증 증거로 묶어 반복 재작업과 잘못된 완성 판정을 줄인다.

## Desired outcome

현재 계약 kernel 위에 테스트 가능한 R0 로컬 제품 흐름을 추가한다. 사용자는 프로젝트를 만들고, 승인된 명세/revision을 저장하고, 결정론적 분석을 실행하고, 판정·변경 영향·성숙도·예산 증거를 조회하고 내보낼 수 있어야 한다.

## In scope

- 로컬 SQLite 프로젝트 저장과 명시적 schema version
- 불변 승인 snapshot, idempotent create/run 경계와 optimistic concurrency
- 기존 분석 engine, revision, maturity, interface와 cost 계약을 연결하는 application service
- loopback 전용 API의 최소 수직 흐름
- JSON 응답·오류·Content-Type·Origin/CSRF·경로 경계 테스트
- 결정론적 재실행 및 evidence export
- 최소 사용자 흐름과 실행 문서

## Out of scope

- 실제 장치 discovery·제어·firmware 설치·HIL 실행
- 외부 네트워크·외부 solver·supplier API
- 로그인·클라우드·다중 사용자 협업
- CAD/FEA/CFD와 범용 자동 최적화
- 인증, 양산 승인과 Product 모드

## Decision boundaries

- Codex may decide: 내부 모듈 구조, SQLite schema 세부사항, 테스트 fixture, 로컬 API shape의 최소 세부사항과 구현 순서.
- User approval required: 실제 장치 동작, credential, 외부 배포, 유료 외부 서비스, 데이터 삭제·마이그레이션의 비가역 변경, 안전·법적 범위 확대.

## Constraints

- Pydantic schema가 계약의 단일 원본이다.
- 승인 전 실행 금지, 판정과 실행 상태 분리, 증거와 revision hash 고정.
- 서버는 `127.0.0.1` 기본값이며 입력을 code/shell로 실행하지 않는다.
- 경로 탈출·XSS·CSRF·내부 절대경로 노출을 방지한다.
- 최소 dependency와 90% 이상 core branch coverage를 유지한다.

## Acceptance criteria

1. 프로젝트와 draft/approved revision이 SQLite에 저장되고 승인 snapshot은 수정되지 않는다.
2. 승인되지 않은 명세 실행은 AnalysisRun 생성 없이 거부된다.
3. 동일 idempotency key 재요청은 중복 프로젝트/실행을 만들지 않는다.
4. stale version 수정은 충돌로 거부된다.
5. 기존 결정론적 engine 결과와 evidence manifest가 저장·재조회·재실행된다.
6. revision 변경 시 영향을 받는 evidence만 무효화되고 이유가 조회된다.
7. 예산과 maturity는 누락·만료·stale 증거에서 `INDETERMINATE` 또는 이전 단계로 보수적으로 판정된다.
8. API의 입력, 오류, Content-Type, Origin/CSRF와 경로 경계 테스트가 통과한다.
9. `./forge verify`가 통과하고 core branch coverage가 90% 이상이다.
10. README와 PRD가 실제 실행 방법과 현재 범위를 정확히 설명한다.

## Assumptions and resolutions

- 특정 transport가 제품 핵심이라는 가정은 폐기했다. transport는 profile adapter가 선택한다.
- simulation이 실물 완성을 의미한다는 가정은 폐기했다. SIL/HIL/commissioning은 별도 evidence class다.
- 모든 변경에 전체 재시험이 필요하다는 가정은 폐기하되, 의존성이 불명확하면 보수적으로 무효화한다.

## Pressure finding

연결 방식보다 변경 일관성과 증거 신뢰가 본질이라는 결론에 따라 R0는 실제 장치 제어가 아니라 저장·재현·무효화·판정 경계를 제품화한다.

## Execution contract

- Completion unit: one approved R0 local product workflow milestone
- Stop condition: 저장·application service·API 최소 수직 절편이 구현되고 review와 adversarial QA가 clean일 때
- Scope shrink: blocked가 아니면 허용하지 않음
