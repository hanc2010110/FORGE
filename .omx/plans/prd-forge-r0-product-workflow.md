# FORGE R0 Local Product Workflow — Consensus Plan

## Requirements summary

FORGE의 이미 구현된 결정론적 계약 kernel을 사용자가 실제로 다룰 수 있는 로컬 제품 흐름으로 만든다. 이번 마일스톤은 프로젝트·명세·설계 revision·분석 실행·증거를 SQLite에 저장하고, loopback FastAPI와 최소 Next.js 화면으로 생성 → 승인 → 실행 → 증거 조회 → 재실행 → 내보내기를 완성한다. 실제 장치 I/O는 포함하지 않는다.

근거:

- R0 산출물과 완료 게이트는 프로젝트·명세·실행 저장, 최소 웹 흐름과 loopback API를 요구한다 (`PRD.md:757-773`).
- API 표면과 idempotency/preflight 규칙은 `PRD.md:568-590`에 정의되어 있다.
- SQLite, export/import, 재현성 계약은 `PRD.md:615-623`에 정의되어 있다.
- Origin/CSRF, 경로 탈출, CSP와 내부 경로 비노출은 `PRD.md:625-638`의 필수 경계다.
- 현재 kernel은 Pydantic immutable contract (`forge_core/models.py:21-339`), 중앙 실행 경계 (`forge_core/engine.py:52-347`), revision 무효화·성숙도 (`forge_core/design.py:13-334`)와 예산·인터페이스 검증 (`forge_core/constraints.py:13-224`)을 이미 제공한다.

## RALPLAN-DR summary

### Principles

1. 승인된 snapshot과 완료 evidence는 append-only로 취급한다.
2. HTTP는 얇은 경계이며 상태 전이와 공학 판정은 application/core가 소유한다.
3. 누락·충돌·stale 상태는 추정하지 않고 명시적으로 거부한다.
4. 한 번에 검증 가능한 수직 절편을 만들되 실제 장치 경계를 넘지 않는다.
5. 보안은 loopback이라는 가정에 의존하지 않고 Origin·Host·CSRF·Content-Type을 각각 검사한다.

### Decision drivers

1. 기존 68개 계약 테스트와 결정론적 engine을 깨뜨리지 않는 재사용성
2. 승인·idempotency·동시 수정의 원자성 및 재현성
3. 코딩 경험이 적은 사용자가 한 화면에서 성공 흐름을 완료하는 사용성

### Viable options

#### Option A — API-first vertical slice, then thin Next.js UI (chosen)

- 장점: 저장·상태 전이·보안을 먼저 테스트하고 UI는 안정된 OpenAPI 계약만 사용한다.
- 단점: 첫 구현 중간에는 API만 보이므로 UI 결과가 늦게 나타난다.

#### Option B — Web/API/DB를 동시에 큰 모노레포로 구성

- 장점: 최종 디렉터리 구조와 사용자 화면을 일찍 확보한다.
- 단점: 아직 변하는 계약이 frontend까지 전파되어 디버깅 범위와 dependency surface가 급격히 커진다.

#### Option C — 표준 라이브러리 HTTP 서버와 SQLite만 사용

- 장점: Python dependency가 거의 늘지 않는다.
- 단점: PRD의 FastAPI/OpenAPI 방향과 어긋나며 validation, error contract와 보안 middleware를 직접 재구현해야 한다.

## Architecture decision record

### Decision

Option A를 채택한다. `forge_core`에는 framework-independent repository/application service를 두고, `services/api`는 FastAPI adapter, `apps/web`은 Next.js TypeScript client로 둔다. SQLite는 Python 3.14의 `sqlite3`를 직접 사용하고 명시적 transaction과 schema version을 적용한다.

### Drivers

- 기존 Pydantic 계약 재사용
- transaction 단위의 승인·실행·idempotency 보장
- API와 UI의 독립 테스트 가능성

### Alternatives considered

- Option B는 초기 변경 폭과 실패 표면이 너무 크다.
- Option C는 프레임워크 보안·validation 기능을 중복 구현하고 PRD 기준선과 충돌한다.

### Why chosen

FastAPI 공식 문서는 명시적 CORS origin, TrustedHost middleware, 엄격한 JSON Content-Type과 TestClient 경계를 제공한다. Python 3.14 `sqlite3` 공식 문서는 `autocommit=False`와 명시적 commit/rollback을 권장한다. 이 조합이 작은 코드로 계약과 보안 테스트를 고정하기에 가장 적합하다.

### Consequences

- Python runtime dependency에 FastAPI/Uvicorn이, dev dependency에 HTTPX가 추가된다.
- Node workspace에 Next.js/React/TypeScript가 추가된다.
- DB JSON payload는 항상 Pydantic 재검증 뒤 반환한다.
- UI는 API가 제공하지 않는 판정이나 성숙도를 자체 계산하지 않는다.

### Follow-ups

- R1 인터뷰/정역학 plugin은 R0 제품 흐름이 clean review와 QA를 통과한 뒤 추가한다.
- 실제 연결 API는 `PRD.md:592-613`에 따라 R6 전까지 생성하지 않는다.

## Implementation steps

### 1. Repository, dependency and command baseline

- `pyproject.toml:1-43`, `requirements.lock:1-12`, `forge:1-49`를 갱신한다.
- 현재 `.git`이 없으므로 생성 파일 제외 규칙을 확인한 뒤 로컬 Git baseline을 만들고 verify 전후 변경 탐지 기준을 확보한다. 사용자의 원격 저장소나 외부 서비스에는 연결하지 않는다.
- Python package는 현재 환경에서 검증된 정확한 버전으로 잠근다.
- `./forge api`, `./forge web`, `./forge verify` 진입점을 제공하고 기존 `dev` demo 호환성을 유지한다.
- Node lockfile과 script를 커밋하고 production dependency 취약점 검사를 verify에 포함한다.

### 2. Two-stage engine execution boundary

- `forge_core/engine.py:69-173`의 단일 `execute()`를 `prepare()`와 `execute_prepared()`로 분리한다.
- `prepare()`는 부작용 없이 승인·plugin identity·preflight를 수행하고 immutable `PreparedAnalysis`를 반환한다.
- `PreparedAnalysis`는 project/spec/revision ID, spec·revision hash, engine/policy/plugin identity, normalized input hash, 예상 metric ID, 필수 승인과 prepare hash를 고정한다. 수치 결과나 판정을 미리 만들지 않는다.
- `execute_prepared()`는 prepare hash, 현재 spec/revision hash와 plugin identity가 모두 일치할 때만 실행한다.
- application service는 preflight를 먼저 저장하고, accepted인 경우에만 run ID와 queued/running event를 만든 뒤 `execute_prepared()`를 호출한다.
- 기존 `execute()`는 하위 호환 wrapper로 유지하고 기존 68개 계약 테스트를 그대로 통과시킨다.

### 3. Persistence contracts and SQLite repository

- `forge_core/persistence.py`에 `ProjectRecord`, `StoredSpec`, `StoredRun`, `IdempotencyRecord`, 안정적 오류 타입을 Pydantic으로 정의한다.
- `forge_core/sqlite_store.py`에 schema version table, foreign key, WAL, busy timeout, 명시적 transaction을 구현한다.
- 프로젝트 생성/조회/삭제, draft 저장, 단일 승인, 실행 결과 저장, evidence 조회와 export snapshot을 제공한다.
- 승인 snapshot과 run은 UPDATE하지 않고 append-only insert만 허용한다.
- DB transaction은 짧게 유지하고 plugin/engine 실행 중에는 열어두지 않는다. 실행 전 prepared/queued 상태를 commit하고 실행 후 새 terminal event를 별도 transaction으로 append한다.
- SQLite write lock은 connection timeout 100 ms 뒤 최대 3회만 재시도하며 injected sleeper 기준 25 ms, 50 ms, 100 ms backoff를 사용한다. 소진 시 stable `storage_busy` conflict를 반환하고 idempotency row와 domain row 어느 쪽도 부분 commit하지 않는다.

### 4. Application service and state transitions

- `forge_core/service.py`가 repository, `AnalysisEngine`과 first-party plugin registry를 조합한다.
- create project, create spec version, approve spec, execute, retry, revision impact/maturity 조회, export를 구현한다.
- `expected_version`을 사용한 optimistic concurrency와 operation+idempotency key 원자 처리를 적용한다.
- 명세 상태는 `draft → approved → superseded`, 실행 상태는 `prepared → queued → running → succeeded | failed | cancelled`로 별도 관리한다. 허용되지 않은 전이는 domain conflict와 HTTP 409다.
- 모든 mutation은 현재 resource version을 요구하며 stale 요청의 409 응답에는 서버의 현재 version만 포함한다.
- idempotency identity는 `(operation, project_id, local_installation_id, key)`이며 canonical request hash가 다르면 conflict, 같으면 저장된 동일 응답을 반환한다.
- preflight 거부는 결과를 저장하지만 run row는 만들지 않는다.
- repository는 row insert/select와 atomic compare-and-set 같은 원시 persistence만 제공한다. 승인, 상태 전이, verdict 집계와 maturity 평가는 application service만 호출할 수 있으며 API/UI가 repository나 core 판정 함수를 직접 호출하지 않는 architecture test를 둔다.
- service 시작 시 마지막 event가 `running`인 모든 run은 orphan으로 간주한다. injected UTC clock의 startup timestamp로 actor=`system-recovery`, state=`failed`, reason=`process_interrupted` event를 append한다. 기존 event/payload는 수정하지 않는다.

### 5. FastAPI boundary and security middleware

- `services/api/app.py`에 app factory와 dependency injection을 둔다.
- 첫 수직 절편 endpoint는 프로젝트 create/get/delete, spec create/get/approve, analysis create/get/retry, project export와 health만 구현한다.
- 안정적인 error envelope, 요청 크기, JSON Content-Type, explicit Origin, CSRF token, TrustedHost와 내부 경로 비노출을 적용한다.
- Uvicorn 실행은 기본 `127.0.0.1`만 허용한다.
- 요청 검사는 loopback bind 강제 → Host allowlist → Origin → mutation CSRF → 정확한 JSON Content-Type → body size → Pydantic schema 순으로 적용한다. CORS만으로 CSRF를 대체하지 않는다.

### 6. Minimal Next.js user flow

- `apps/web`에 프로젝트 생성, 예제 spec 불러오기/수정, 승인, 실행, verdict·requirement evidence·manifest 확인과 재실행 화면을 만든다.
- 사용자 텍스트는 React text rendering만 사용하고 raw HTML 삽입을 금지한다.
- API 오류는 안정적 code와 이해 가능한 한국어 메시지를 분리해 표시한다.
- UI는 simulation/analysis 결과를 `bench_verified` 또는 `commissioned`로 표현하지 않는다.
- UI는 서버 응답 전 상태를 성공으로 확정하지 않고 domain transition, verdict 또는 maturity를 복제 계산하지 않는다.

### 7. Export/import-safe evidence bundle

- project export는 schema version, spec, preflight, run, verification, revision/prepare/run manifest와 각 payload hash를 canonical JSON bundle로 제한한다.
- export manifest는 포함 entry의 canonical path·size·SHA-256을 정렬된 순서로 기록한다. 사용자 제공 output path나 archive member path는 받지 않는다.
- 파일명은 `project-{project_id}-v{project_version}.forge.json`으로 고정한다. export payload는 요청 시각을 넣지 않고 snapshot에 이미 저장된 UTC `Z` timestamp만 사용한다. key와 entry는 canonical JSON 순서로 직렬화해 같은 project version의 반복 export가 byte-for-byte 동일해야 한다.
- 이번 마일스톤에서 import는 검증 기능까지만 구현하고, 기존 project overwrite는 하지 않는다.
- artifact path가 없으므로 임의 filesystem path를 export payload에 포함하지 않는다.

### 8. Documentation and examples

- `README.md`를 실제 `api`, `web`, `verify` 명령과 화면 흐름에 맞춘다.
- `PRD.md` §24에 R0 제품 흐름 절편의 구현 상태와 미구현 장치 기능을 구분한다.
- `examples/`에 승인 전/후 흐름을 재현하는 고정 입력을 유지한다.

### 9. Verification, review and QA

- targeted unit/integration/API/web tests 후 전체 `./forge verify`를 실행한다.
- 별도 code-review가 APPROVE/CLEAR를 내고 UltraQA가 hostile API/UI 시나리오를 통과해야 완료한다.
- 실패하면 findings를 계획으로 되돌려 수정 후 review와 QA를 반복한다.

## Acceptance criteria

1. 임시 SQLite DB에서 프로젝트 생성·조회·삭제와 schema initialization이 결정론적으로 통과한다.
2. draft spec 승인 시 새 immutable approved snapshot이 생기고 기존 payload를 바꾸려는 요청은 거부된다.
3. stale `expected_version` 요청은 안정적 conflict code와 HTTP 409를 반환한다.
4. 같은 endpoint·project·idempotency key·동일 request hash는 같은 결과를 반환하고 row를 중복 생성하지 않는다.
5. 같은 key에 다른 payload는 HTTP 409를 반환한다.
6. draft 또는 누락 입력 분석은 run row 없이 저장된 preflight와 HTTP 422를 반환한다.
7. accepted preflight는 run보다 먼저 저장되며 run 상태가 queued → running → succeeded/failed append-only event로 남는다.
8. 승인 spec 분석·재실행은 별도 run ID를 만들되 동일 manifest hash를 보존한다.
9. 저장된 payload를 읽을 때마다 현재 Pydantic schema로 재검증하며 손상 데이터는 사용자 payload가 아닌 안정적 integrity error로 차단한다.
10. revision 변경 영향과 maturity 응답은 기존 `compare_revisions`/`assess_maturity` 결과와 일치한다.
11. Origin, Host, CSRF, Content-Type, oversized JSON과 path traversal hostile 요청이 계약된 상태 코드로 거부된다.
12. API 응답과 export에는 DB path, source path, stack trace, secret가 포함되지 않는다.
13. 실제 브라우저에서 프로젝트 생성 → spec 승인 → 분석 → evidence 확인 → 재실행 흐름이 완료된다.
14. UI는 분석 PASS를 bench/HIL/commissioned로 표시하지 않는다.
15. Python strict mypy, Ruff, lock consistency, Python tests, TypeScript typecheck/lint/test/build가 root verify로 통과한다.
16. core branch coverage 90% 이상, critical/high dependency vulnerability 0건 또는 승인된 예외 기록을 유지한다.
17. engine 실행 중 SQLite write transaction이 열려 있지 않고 crash 뒤 running run은 명시적 interrupted/failed recovery event로 종결된다.
18. retry는 기존 failed run/evidence/audit를 수정하지 않고 새 run ID와 새 event sequence를 만든다.
19. held SQLite write lock은 100 ms timeout과 3회 bounded retry 뒤 `storage_busy`로 끝나며 lock 해제 후 같은 idempotency 요청은 중복 없이 한 번만 성공한다.
20. 같은 project version을 두 번 export하면 파일명, bytes와 SHA-256이 모두 동일하다.
21. source scan/architecture test에서 API와 UI adapter가 repository 또는 verdict/maturity 함수를 직접 호출하는 경로가 0건이다.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| SQLite transaction 경계 밖 중복 생성 | idempotency row와 domain write를 같은 transaction에 저장하고 uniqueness constraint 적용 |
| JSON blob이 schema drift로 손상 | schema_version과 Pydantic read-time revalidation, migration registry 없이는 읽기 전용 실패 |
| loopback을 신뢰해 CSRF가 열림 | strict Content-Type, explicit Origin/Host, double-submit CSRF token과 mutation middleware |
| Next.js가 판정을 재계산 | frontend에는 표시용 DTO만 제공하고 verdict/maturity 계산 code를 금지 |
| 전체 범위를 한 번에 구현해 회귀 원인 불명 | persistence → service → API → UI 순서의 검증 가능한 commits/checkpoints |
| 실제 장치 기능으로 범위가 새어 나감 | 연결 endpoint/imported device schema 생성 금지 테스트와 PRD R6 경계 유지 |
| engine 실행 중 DB lock과 crash orphan | 실행 전후 짧은 transaction, running lease/heartbeat 없이 시작 시 recovery event로 명시적 종결 |

## Pre-mortem

1. **승인과 실행 사이 race**: API가 stale draft를 승인하거나 다른 payload를 실행한다. 원인은 version check와 insert가 다른 transaction에 있음. 해결은 `BEGIN IMMEDIATE`에 준하는 단일 write transaction, expected version과 approved payload hash 재검사다.
2. **보안 테스트는 통과하지만 브라우저 공격은 성공**: TestClient 기본 header만 검사하고 simple CORS request/잘못된 Content-Type을 놓친다. 해결은 hostile Origin·Host·form/plain body와 no-preflight 시나리오를 별도 통합/E2E로 실행한다.
3. **UI가 제품 완성도를 과장**: 분석 PASS를 “완성”으로 표시한다. 해결은 API DTO에 maturity와 blockers를 명시하고 UI copy test에서 HIL 없는 `bench_verified/commissioned` 표현을 금지한다.
4. **프로세스 종료 뒤 run이 영구 running으로 남음**: 시작 시 latest event가 running인 run을 찾아 원본을 보존하고 `failed/process_interrupted/system-recovery` event를 정확히 한 번 append한다.

## Expanded test plan

### Unit

- repository row/payload conversion, schema version, hash, immutable transition
- engine prepare/execute_prepared token binding과 기존 execute wrapper parity
- spec 상태와 run 상태의 독립 전이표, invalid transition 409 mapping
- adapter import graph와 service-only transition ownership
- idempotency request hash와 conflict
- optimistic concurrency
- service의 preflight/no-run, retry와 export bundle

### Integration

- 임시 파일 SQLite에서 transaction rollback, FK, duplicate, reopen persistence
- crash recovery, concurrent approve/run에서 단일 승인·단일 prepare hash 보장
- held write lock의 100 ms × 최대 3회 retry, `storage_busy`, 해제 후 idempotent single-write
- FastAPI endpoint status/error envelope와 store/engine 실제 조합
- 동일 승인 spec 재실행 manifest 일치

### E2E

- 브라우저 프로젝트 생성 → 예제 spec → 승인 → 실행 → evidence → retry
- 실패/누락 입력 흐름과 사용자 메시지
- 분석 PASS가 maturity `analyzed`를 넘지 않는 표시
- stale approval 409를 받은 뒤 최신 version reload와 재승인
- 승인/실행 버튼 double-submit이 같은 resource를 한 번만 생성
- failed run retry 뒤 이전 evidence 보존과 새 run 표시

### Security/hostile

- invalid Origin/Host/CSRF/Content-Type/oversize/malformed JSON
- encoded traversal, absolute path, symlink 및 internal path response leak
- 동일 idempotency key 다른 body, stale expected version, rapid duplicate requests
- malicious Host/Origin/CSRF 조합과 검증 순서 우회 시도

### Observability

- request ID와 안정적 error code는 기록하되 body/secret/절대경로는 기록하지 않는다.
- mutation, approval, run 상태 전이를 actor=`local-user`, timestamp, reason으로 audit record에 남긴다.
- startup recovery는 injected clock의 UTC timestamp, actor=`system-recovery`, reason=`process_interrupted`를 사용하고 event order를 검증한다.

## Verification commands

- `./forge lint`
- `./forge typecheck`
- `./forge lockcheck`
- `./forge test`
- `./forge verify`
- API smoke: loopback app factory + TestClient
- Web smoke: production build와 핵심 browser E2E

## Available agent types and staffing

- `architect` high: storage/API/UI boundary와 transaction/security review
- `critic` high: 계획의 testability·risk·scope 검증
- `executor` medium: persistence/service 구현
- `executor` medium: FastAPI adapter 구현
- `designer` medium: 최소 Next.js 사용자 흐름
- `test-engineer` high: hostile integration/E2E matrix
- `code-reviewer` high: 최종 merge readiness
- `verifier` high: completion claims와 clean-tree 검증

Ultragoal이 ledger와 milestone 완료를 소유한다. 구현이 병렬화될 때 Team은 storage/service와 web/test lane만 나누고 공유 계약 파일은 leader가 통합한다. Ralph는 사용자가 명시적으로 요청한 경우에만 단일-owner fallback으로 사용한다.

## Goal-mode follow-up suggestions

- 기본: `$ultragoal create-goals --brief-file .omx/plans/prd-forge-r0-product-workflow.md`
- 병렬 구현 필요 시: Ultragoal story 안에서 `$team`을 사용하고 결과를 ledger checkpoint로 회수한다.
- 이 작업은 연구 또는 성능 최적화가 아니므로 `$autoresearch-goal`, `$performance-goal` 대상이 아니다.

## Team launch hints and verification path

- Suggested: `$team .omx/plans/prd-forge-r0-product-workflow.md`
- Lane A: persistence/service + unit/integration tests
- Lane B: FastAPI/security + API tests
- Lane C: Next.js UI + browser tests
- Team 종료 전 각 lane은 targeted checks를 통과하고 변경 파일·위험을 보고한다.
- Leader는 전체 `./forge verify`, code-review와 UltraQA evidence를 Ultragoal ledger에 기록한다.

## Planning changelog

- 기존 kernel을 다시 구현하지 않고 persistence/application/API/web adapter로 재사용하도록 범위를 고정했다.
- simulation과 실물 maturity 표현을 UI acceptance criterion으로 추가했다.
- official FastAPI/SQLite guidance에 맞춰 explicit Origin, strict Content-Type, TrustedHost와 명시적 transaction을 계획에 반영했다.
- Architect WATCH 의견을 반영해 spec/run 상태 기계, prepared hash binding, idempotency scope, 짧은 DB transaction, crash recovery와 deterministic export 계약을 구체화했다.
- Critic ITERATE 의견을 반영해 service-only transition ownership, exact recovery event, byte-identical export, bounded lock retry와 hostile browser E2E를 고정했다.
