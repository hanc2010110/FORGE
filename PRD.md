# Project FORGE — AI Engineering Change & Resolution Platform PRD v7

- 작성일: 2026-08-31
- 상태: 제품 및 구현 기준선
- 대체 문서: PRD v1~v5의 범용 공동설계·장치 제어·물리 solver·증거 없는 AI 판정 로드맵

## 1. 제품 정의

FORGE는 로봇과 임베디드 제품의 하드웨어 변경을 source-bound evidence로 계획, 검증, 진단, 해결하고 출시 준비 상태를 판정하는 AI Engineering Change & Resolution Platform이다.

사용자에게 보이는 흐름은 `CONNECT/IMPORT -> PLAN -> VERIFY -> RELEASE`다. 내부 작업 루프는 `PLAN <-> DESIGN -> VERIFY -> DIAGNOSE -> FIX -> RE-VERIFY -> RELEASE`다.

FORGE는 CAD, PLM, Git, CI, simulation, bench, HIL, 실제 장치 시험 시스템을 대체하지 않는다. 각 시스템이 소유한 revision, artifact, log, 결과를 읽기 전용 snapshot과 immutable sidecar record로 연결하는 validation layer다.

현재 로컬 pilot은 deterministic rule/policy checks, guided conversational workspace, immutable ledgers, 실제 STL 형상 parsing/viewing, source-grounded local RAG, RBAC enforcement와 audit, health/backup/restore를 제공한다. Hosted production AI agent와 live external tool execution은 제공하지 않는다. 향후 hosted LLM session과 connector-mediated automation도 같은 evidence boundary를 지켜야 하며, 자동 write-back이나 device control을 제품 계약으로 추가하지 않는다.

## 2. 해결할 문제

로봇·임베디드 팀은 부품 단종, 원가 절감, 기능 개선, 공급망 이슈 때문에 hardware revision을 자주 바꾼다. 그러나 변경 전에는 가능한 설계 대안과 예상 작업량을 비교하기 어렵고, 변경 후에는 firmware, BOM, protocol, test, documentation 반영 누락을 사람이 여러 시스템에서 확인한다.

FORGE가 줄이는 실패는 다음과 같다.

- 후보 센서의 전압, pin map, signal direction 또는 safe value가 기존 회로와 맞지 않는다.
- analog 부품을 I2C 부품으로 바꿨지만 firmware driver와 protocol schema가 남는다.
- 단위나 command range가 바뀌었지만 validation과 문서가 이전 계약을 사용한다.
- 대체 부품 가격에 출처, 조회 시점, MOQ, 배송 또는 세금 범위가 없다.
- hardware revision은 바뀌었지만 이전 revision의 build, bench, HIL 결과가 재사용된다.
- simulation 통과가 physical device 시험 통과처럼 취급된다.
- 계획에 없던 변경이 실제 revision에 함께 들어간다.
- blocker가 발견돼도 원인, 불확실성, 필요한 fix, 재검증 범위가 감사 가능한 형태로 남지 않는다.

## 3. 핵심 사용자

- 5~100명 규모 로봇·임베디드 제품 팀
- systems 또는 release engineer
- hardware·firmware lead
- validation, bench, HIL 담당자
- 품질 및 기술 검토자

## 4. 핵심 사용자 작업

### CONNECT/IMPORT

만들어진 기계·로봇, CAD model, 설계도, PLM snapshot, Git/CI 결과 또는 manual snapshot을 읽기 전용 기준으로 묶는다.

### PLAN

기준 revision에서 변경할 위치, 변경 의도, 현재 부품, 후보 부품 또는 운용 시나리오를 입력하고 source-bound plan을 만든다.

### DESIGN

여러 설계 대안을 비교한다. 각 대안은 source refs, 예상 변경 세트, tradeoff, 필요한 시험, 누락 정보와 canonical hash를 가진다.

### VERIFY

실제 revision snapshot과 imported evidence를 선택한 plan과 비교해 예상된 변경, 누락된 변경, 예상 밖 변경을 구분한다.

### DIAGNOSE

blocker를 evidence-bound로 설명한다. 원인 후보, 근거, 반증 가능성, 불확실성, 추가로 필요한 증거를 함께 보여준다. 증거가 부족하면 root cause를 확정하지 않는다.

### FIX

firmware, BOM, protocol, test, documentation, connector source 또는 evidence collection에 필요한 수정 권장을 생성한다. FORGE는 외부 artifact를 직접 수정하지 않는다.

### RE-VERIFY

Resolve가 만든 새 immutable PLAN draft에 대해 actual snapshot과 evidence를 다시 제출받아 검증한다.

### RELEASE

실제 build, test, BOM, change-impact evidence와 policy만 사용해 `READY` 또는 `BLOCKED` 판정과 감사 보고서를 만든다.

## 5. MVP Scope

MVP에 포함한다.

- read-only connector manifest와 manual/imported snapshot 계약
- connected device, CAD model, design drawing, PLM snapshot, manual snapshot 자산 입력 계약
- source-bound component replacement/add/remove scenario
- deterministic change impact preview
- pin, voltage, unit, command range, safe value, protocol schema, quantity, BOM quote 검사
- firmware, BOM, protocol, test, documentation 예상 영향 전파
- required retest 자동 지정
- simulation, bench, HIL, physical_device evidence tier 분리
- external evidence plan과 imported test evidence 검증
- plan-vs-actual deviation 검증
- planning-only DesignProposalSet, BlockerDiagnosis, FixProposalSet and ReplanSeed contracts
- release evidence ingest와 deterministic readiness decision
- append-only SQLite sidecar persistence
- loopback-only API with Host, Origin, CSRF, content-type, body-limit, idempotency and optimistic concurrency protections
- `/app/` static operator console for CONNECT/IMPORT, PLAN, VERIFY and RELEASE
- bounded ASCII/binary STL parsing, immutable geometry provenance and contextual
  read-only 3D canvas
- project/tenant-scoped text ingestion, deterministic cited local RAG and persisted
  runtime results without release authority
- enforced organization/actor/membership/project RBAC and append-only allowed/denied audit events
- deep local database readiness, verified backup and non-overwriting restore verification

MVP에서 구조만 정의하고 production 기능으로 약속하지 않는 항목은 다음과 같다.

- hosted LLM/embedding-backed conversational engineering session
- production conversational design alternative authoring beyond structured forms
- evidence-bound diagnosis and Resolve API routes where not wired into a release workflow
- automatic re-verification after Resolve
- connector-mediated automation against CAD, PLM, Git, CI, ticketing or test systems
- semantic/vector RAG and externally hosted AI explanation drafts

### 5.1 배포 시험 범위 완성도

로컬 pilot 범위는 약 **80%**로 판정한다. 이 수치는 코드 coverage가 아니라 첫 배포
시험에 필요한 제품 역량의 가중치다.

| 역량 | 가중치 | 현재 |
| --- | ---: | ---: |
| 검증·릴리스 evidence kernel | 30% | 90% |
| Chat-first 변경·설계 loop | 25% | 88% |
| 권한·감사·source-grounded AI | 20% | 85% |
| 로컬 운영·복구 기반 | 15% | 78% |
| 실제 외부 ecosystem 연동 | 10% | 35% |

가중 결과는 약 81%이며 보수적으로 80%로 보고한다. 외부 ecosystem 35%는 실제 STL
import와 connector 계약까지 포함하지만 STEP/SolidWorks, CAE, device, Git/CI/PLM/BOM
credential 연동은 포함하지 않는다. 따라서 이 평가는 로컬 pilot 배포 시험 준비도이며
완전한 enterprise SaaS 또는 안전 인증 완료를 의미하지 않는다.

이 항목은 future scope이며, 구현 전에도 동일한 immutable sidecar, source-binding and no-writeback constraints를 따라야 한다.

## 6. Product Principles

1. 모든 plan, alternative, diagnosis, fix recommendation, release decision은 source, revision, timestamp, policy와 canonical hash를 가진다.
2. DESIGN 대안은 source-bound 입력이 없으면 생성하지 않는다.
3. Preview와 design recommendation은 release evidence가 아니다.
4. DIAGNOSE는 evidence-bound blocker 설명이며, 증거 없는 root cause 확정은 금지한다.
5. Resolve는 외부 artifact를 수정하지 않고 새 immutable PLAN draft만 만든다.
6. 실제 release 판정은 actual snapshot, actual evidence, immutable policy와 canonical hash만 사용한다.
7. pin, voltage, unit, range, safe state and protocol schema checks are deterministic.
8. hardware 변경은 보수적으로 firmware, BOM, test, protocol, documentation 영향으로 전파한다.
9. `simulation`, `bench`, `HIL`, `physical_device`는 서로 다른 evidence tier이며 서로 대체할 수 없다.
10. FORGE는 external systems의 source of truth를 유지하고 read-only snapshot만 소비한다.
11. AI는 설명과 탐색을 보조할 수 있지만 수치 판정, deviation, readiness status는 versioned code와 policy가 생성한다.
12. 판단 근거가 부족하면 `indeterminate`, `missing`, `blocked` 또는 high-uncertainty diagnosis로 남긴다.

## 7. PLAN and DESIGN Contracts

### 7.1 ChangeScenario

Required fields:

- project ID
- source-bound asset input
- baseline snapshot ID and hash
- intent kind
- proposed hardware revision
- ordered planned changes
- UTC creation time
- scenario hash

Acceptance:

- baseline snapshot must exist in the same project
- asset input is read-only
- source revision and content hash are required
- duplicate change IDs are rejected
- scenario hash changes when any nested payload changes

### 7.2 ComponentSpecification

Required fields:

- component ID, manufacturer, part number, quantity
- hardware or BOM source reference
- specification hash

Optional fields:

- interface contract
- quote snapshot
- normalized attributes with unit, dimension and source

Acceptance:

- quote part number must match component part number
- unknown fields are rejected
- non-canonical hashes are rejected
- missing source-bound data makes downstream preview `indeterminate`

### 7.3 DesignAlternative

Current implementation status: structured planning-only contract exists as `DesignProposalSet`; production conversational authoring is not implemented.

Required fields when implemented:

- alternative ID and source scenario hash
- changed components and unchanged constraints
- expected change set across firmware, BOM, protocol, test and documentation
- compatibility findings
- required external evidence
- cost and supply assumptions
- tradeoffs
- missing information
- recommendation
- alternative hash

Acceptance:

- every alternative binds to source refs and the same baseline
- alternatives cannot contain release `READY` or `BLOCKED`
- alternatives with unsupported solver claims are rejected or marked external-evidence-required
- operator selection freezes a new plan draft hash

## 8. VERIFY, DIAGNOSE and FIX Contracts

### 8.1 PlanVerification

Implemented fields:

- preview hash
- actual change-analysis hash
- deterministic deviations
- observed planned refs
- verification hash

Deviation kinds:

- `missing_planned_change`
- `unplanned_actual_change`
- `domain_mismatch`
- `retest_gap`
- `finding_gap`

Acceptance:

- preview and actual assessment must exist in the same project
- stored hashes must match the payloads
- `matches_plan` must be exactly true only when there are no deviations
- plan verification is not release evidence

### 8.2 ExternalEvidencePlanVerification

Implemented fields:

- stored plan hash
- actual change-analysis hash
- imported immutable `TestExecutionEvidence`
- missing required evidence IDs
- unexpected imported evidence IDs
- failed required evidence IDs
- verification hash

Acceptance:

- external evidence plan covers simulation, bench, HIL and physical device tiers
- imported evidence binds to existing test evidence hash
- failed, missing and unexpected evidence produce `differs_from_plan`
- verification result does not create `READY` or `BLOCKED`

### 8.3 Diagnosis

Current implementation status: structured planning-only `BlockerDiagnosis` contract exists and is generated from release decision blockers. It is not a production root-cause engine.

Required fields when implemented:

- diagnosis ID
- source verification or release decision hash
- blocker code
- supported facts
- candidate causes
- uncertainty level and missing evidence
- recommended fix options
- diagnosis hash

Acceptance:

- a candidate cause must cite verification deviations, failed evidence, missing evidence, policy blockers or source refs
- unsupported causes are shown as hypotheses with high uncertainty, not root causes
- diagnosis cannot satisfy a release policy

### 8.4 FixRecommendation and Resolve

Current implementation status: structured planning-only `FixProposalSet`, `ProposalSelection` and `ReplanSeed` contracts exist. Automatic source write-back is not implemented.

Required fields when implemented:

- recommendation ID
- diagnosis hash
- target domain: firmware, BOM, protocol, test, documentation, connector source or evidence collection
- expected change set
- required retest delta
- source refs and assumptions
- fix recommendation hash

Resolve behavior:

- Resolve creates a new immutable PLAN draft from selected fix recommendations.
- Resolve does not modify CAD, PLM, Git, CI, documents or devices.
- Release remains blocked until updated actual snapshots and evidence pass RE-VERIFY.

## 9. RELEASE Contracts

Release decision input:

- actual hardware revision delta
- cross-artifact consistency findings
- required retest assignment
- source- and timestamp-bound BOM cost
- firmware build result bound to Git revision, toolchain, artifact and protocol hash
- exact-tier test evidence
- rejected evidence and rejection reason
- immutable policy binding

Readiness breakdown:

- hardware change consistency
- firmware build status and provenance
- BOM cost source, observed time, expiry and quote scope
- protocol/schema compatibility
- required retest coverage by exact tier
- failed or stale evidence
- open deviations and blockers
- policy scope disclaimer

Output:

- deterministic `READY` or `BLOCKED`
- machine-readable and human-readable report
- append-only decision chain
- canonical decision hash

`READY` means configured release policy is satisfied. It does not mean safety certification, legal compliance, production approval or human approval replacement.

## 10. API Acceptance Contracts

All R0/R1 routes use `/api/v1` and bind only to `127.0.0.1`. Every POST retains Host, Origin, CSRF, content-type and body-limit protections. Project mutations additionally require `Idempotency-Key` and optimistic `If-Match` concurrency (except project creation). Persistent GitHub integration mutations require `Idempotency-Key`; connection tests are explicitly non-persistent and do not require it.

Implemented R0/R1 routes:

| Method | Route | Purpose |
| --- | --- | --- |
| POST | `/api/v1/projects` | create project and release policy |
| GET | `/api/v1/projects/{project}` | read project |
| GET | `/api/v1/connectors` | read local connector manifests |
| GET | `/api/v1/integrations` | read typed future/live integration catalog |
| GET | `/api/v1/integrations/github` | read redacted GitHub connection status |
| POST | `/api/v1/integrations/github/test` | test App credentials without persistence |
| POST | `/api/v1/integrations/github` | verify and store local GitHub App configuration |
| POST | `/api/v1/integrations/github/sync` | capture commit/PR/exact-SHA Actions evidence |
| POST | `/api/v1/projects/{project}/connector-snapshots` | store read-only snapshot |
| GET | `/api/v1/projects/{project}/connector-snapshots/{snapshot_id}` | read snapshot |
| POST | `/api/v1/projects/{project}/change-impacts` | store actual change impact assessment |
| GET | `/api/v1/projects/{project}/change-impacts/{assessment_hash}` | read actual assessment |
| POST | `/api/v1/projects/{project}/change-previews` | persist source-bound scenario and deterministic preview |
| GET | `/api/v1/projects/{project}/change-previews/{preview_hash}` | read immutable preview |
| POST | `/api/v1/projects/{project}/external-evidence-plans` | persist operating scenario and required external evidence plan |
| GET | `/api/v1/projects/{project}/external-evidence-plans/{plan_hash}` | read immutable external evidence plan |
| POST | `/api/v1/projects/{project}/external-evidence-plan-verifications` | compare imported test evidence with immutable external evidence plan |
| GET | `/api/v1/projects/{project}/external-evidence-plan-verifications/{verification_hash}` | read immutable external evidence deviations |
| POST | `/api/v1/projects/{project}/plan-verifications` | compare preview with actual assessment |
| GET | `/api/v1/projects/{project}/plan-verifications/{verification_hash}` | read immutable deviations |
| POST | `/api/v1/projects/{project}/release-evidence` | ingest release evidence |
| GET | `/api/v1/projects/{project}/release-evidence/{evidence_id}` | read release evidence |
| POST | `/api/v1/projects/{project}/release-decisions` | evaluate release readiness |
| GET | `/api/v1/projects/{project}/release-decisions/{decision_hash}` | read release decision |

Structured resolution route contracts:

| Method | Route | Purpose |
| --- | --- | --- |
| POST | `/api/v1/projects/{project}/design-proposals` | create planning-only design alternatives from a source-bound preview |
| GET | `/api/v1/projects/{project}/design-proposals/{proposal_hash}` | read immutable design proposal set |
| POST | `/api/v1/projects/{project}/release-diagnoses` | create evidence-bound blocker diagnosis from a release decision |
| GET | `/api/v1/projects/{project}/release-diagnoses/{diagnosis_hash}` | read immutable blocker diagnosis and fix proposals |
| POST | `/api/v1/projects/{project}/resolution-plans` | select a fix proposal and create a planning-only replan seed |
| GET | `/api/v1/projects/{project}/resolution-plans/{plan_hash}` | read immutable resolution plan |

API acceptance:

- mutation responses include project version
- project and persistent GitHub integration mutations require an opaque `Idempotency-Key`
- exact idempotent replay returns the original response and `Idempotency-Replayed: true`
- conflicting idempotency payloads are rejected with 409
- GitHub connect/sync writes a recoverable prepared result before local mutation, so an
  interrupted retry with the same key completes locally without repeating GitHub calls
- exact-SHA Actions collection rejects undocumented status/conclusion values and fails
  closed when GitHub's filtered-search cap exceeds 1,000 runs
- stale `If-Match` versions are rejected
- cross-project hash binding is rejected
- duplicate JSON keys and unknown fields are rejected
- user input never becomes a filesystem path, query fragment, template or executable command
- structured diagnosis, resolve and replan APIs must satisfy the same route, hash, security and idempotency contracts

## 11. Operator Experience

The `/app/` console uses a persistent four-stage rail.

### CONNECT/IMPORT Surface

- project creation
- read-only connector manifest visibility
- connected robot/device, CAD, design drawing, PLM or manual snapshot identity
- connector snapshot capture request
- source-bound baseline copied into PLAN

### PLAN Surface

- project and baseline context
- component proposal form
- operating scenario form
- current/candidate comparison
- predicted impact surface
- external simulation/bench/HIL/physical-device evidence plan request
- required actions and retests
- gaps and assumptions
- immutable preview identity

### VERIFY Surface

- preview and actual assessment lookup
- external evidence plan and imported evidence lookup
- expected, missing and unplanned deviations
- missing, unexpected and failed imported evidence
- baseline/target identity warnings
- no release `READY` or `BLOCKED` vocabulary

### RELEASE Surface

- current immutable decision lookup
- actual changes, findings, retests and evidence tiers
- BOM/build provenance and canonical bindings
- readiness breakdown
- `READY`/`BLOCKED` decision and blocker codes

Future conversational session behavior:

- The user can describe a change or failure in engineering language.
- FORGE may propose source-bound design alternatives and fix recommendations.
- The current production path remains structured records and deterministic checks until conversational AI and connector mediation are implemented and verified.

## 12. Persistence and Audit

- SQLite schema migrations are explicit and forward-only.
- snapshots, previews, external evidence plans, verifications, release evidence and release decisions are project-scoped append-only records.
- scenario, preview, actual assessment, imported evidence, verification and release hashes are checked again at the storage boundary.
- stored records cannot be silently replaced.
- idempotency and optimistic concurrency include preview and verification mutations.
- exports preserve versioned canonical payloads.
- future diagnosis and plan-draft records must be immutable and hash-bound.

## 13. Security and Privacy

- loopback-only binding
- strict Host and Origin allowlist
- double-submit CSRF for mutations
- exact JSON framing and bounded body size
- duplicate key and unknown field rejection
- safe opaque IDs and no path traversal
- no raw source-system credentials in DB, logs or reports
- no user text execution or HTML interpretation
- read-only connector capabilities checked before capture
- no native device transport, shell command, build execution or external write path

## 14. Quality Gates

### Contract Gates

- invalid add/remove/replace endpoint shapes rejected
- duplicate change IDs rejected
- quote part mismatch rejected
- non-UTC timestamps rejected
- altered decimal scale or nested payload changes alter canonical hash
- preview status cannot accept release `READY`/`BLOCKED`
- preview, diagnosis and fix recommendations cannot satisfy release evidence contracts

### Engine Gates

- pin, voltage, unit, command range, safe value and schema changes detected
- every hardware change conservatively assigns required retests
- missing source data produces `indeterminate`; it is never treated as an acceptable plan
- identical input and policy produce byte-stable preview
- plan-vs-actual identifies expected, missing and unplanned changes
- release decision uses actual evidence and policy only

### Persistence and API Gates

- migrations preserve existing records
- cross-project preview, assessment, evidence, verification and release binding rejected
- idempotency replay and conflict behavior tested
- stale version rejected before mutation
- API security error matrix remains stable

### UI Gates

- user-visible work remains conversational while the underlying lifecycle stays
  `CONNECT/IMPORT -> PLAN -> VERIFY -> RELEASE`
- first-run state is a GPT-like chat shell that asks `What do you want to build or
  change?` before exposing detailed source-bound fields
- Engineering Workspace is hidden by default and expands contextually for 3D,
  simulation, comparison or evidence inspection without losing chat position
- left navigation provides New chat, Projects, Recent sessions and History without
  turning the home surface into a dashboard
- impact nodes are keyboard-operable and reveal reason, evidence and uncertainty without inventing unsupported dependencies
- design alternative selection is labeled as local planning state until an immutable approval contract exists
- plan verification renders observed, missing and unplanned references as an expected-vs-actual matrix
- release summary exposes status, passed/required retests, accepted evidence and blocker count before detailed evidence sections
- internal `DESIGN`, `DIAGNOSE`, `FIX` and `RE-VERIFY` loop does not leak release vocabulary into PLAN
- hostile text cannot create markup
- 1440px and 390px layouts have no page overflow
- keyboard-only core flow succeeds
- diagnosis uncertainty is visible as text

### Release Gates

- lint and format checks pass
- strict mypy passes
- locked dependency and pip checks pass
- full test suite passes with at least 90% total coverage
- deterministic demo covers CONNECT/IMPORT -> PLAN -> VERIFY -> RELEASE
- independent code review recommends APPROVE
- independent architecture review reports CLEAR

## 15. Delivery Roadmap

### R0 — Evidence-Based Release Kernel — complete

- read-only connector contracts
- actual snapshot impact analysis
- exact-tier retest and evidence model
- SQLite append-only release records
- secure loopback API
- release decision viewer

### R1A — Change Impact Preview — current

- component proposal contracts
- deterministic candidate comparison
- predicted impact, action and retest report
- append-only preview persistence and API
- PLAN operator surface

### R1B — Plan vs Actual and External Evidence Verification — current

- preview-to-actual binding
- expected/missing/unplanned deviation engine
- external evidence plan
- imported test evidence verification
- verification persistence and API
- VERIFY operator surface

### R1C — Structured Resolution Sidecar

- DesignAlternative contract and persistence
- evidence-bound Diagnosis contract
- FixRecommendation contract
- Resolve to immutable PLAN draft
- RE-VERIFY status and readiness breakdown integration

### R1D — Pre-deployment Foundations — current

- chat-first conversational shell with progressive context and lazy Engineering
  Workspace disclosure
- organization, actor, project membership, enforced project RBAC and append-only audit events
- fail-closed public deployment readiness assessment
- project/tenant-scoped local retrieval/provider runtime with cited, classified
  claims and persisted results
- explicit boundary: provider prose and retrieval results are not release evidence
- deep database readiness, verified backup and non-overwriting restore are implemented
- public/multi-tenant production remains BLOCKED until SSO, tenant isolation,
  secrets and hosted monitoring are configured

### R2 — Production Source Adapters

- bounded ASCII/binary STL import and contextual 3D viewing — implemented
- STEP/SolidWorks assembly metadata and file-based CAD/EDA/PLM export connector
- local Git connector
- CI result connector
- golden component and revision corpus
- connector health and freshness

### R3 — Team Workflow and Authorization

- project/revision lists and search
- authenticated reviewer identity and approval record
- organization/project authorization enforcement and audit export
- waiver with expiry and separation of duties
- notifications

### R4 — Evidence Integrity

- signed build/test artifacts and raw log hash
- fixture and instrument identity
- calibration freshness
- evidence revocation, retention and historical replay

### R5 — Rule Catalog and Enterprise Operations

- sensor, actuator, controller and power component rules
- electrical, mechanical, thermal and protocol substitution checks
- SSO provisioning and production organization isolation
- PostgreSQL/object storage deployment profile
- secret management, backup, monitoring and audit retention

Firmware flash, OTA, device command, own solver execution and automatic external-system write-back are not roadmap milestones for this product.

## 16. Product Success Metrics

- pilot users can create a component proposal and understand required work in under 10 minutes.
- at least two source-bound alternatives can be compared without losing baseline provenance.
- golden candidate-change corpus has zero missed high-risk pin, voltage, unit, range or schema incompatibilities.
- golden actual-change corpus has zero missed planned/unplanned deviations.
- missing or stale required release evidence produces `BLOCKED` in 100% of cases.
- every blocker is traceable to source, rule, revision, evidence and uncertainty in three interactions or fewer.
- Resolve-created plan drafts never modify external source systems automatically.
- identical input, policy and evidence reproduce byte-stable reports.

## 17. Principal Risks

| Risk | Mitigation |
| --- | --- |
| Preview is mistaken for physics simulation | name it Change Impact Preview; show assumptions, missing information and external test requirements |
| Compatible preview is mistaken for release approval | separate vocabulary, route, record type and UI stage from READY/BLOCKED |
| Diagnosis is mistaken for proven root cause | require evidence refs, uncertainty and missing-evidence fields |
| Fix recommendation is mistaken for automatic source edit | Resolve creates a PLAN draft only; no write-back path |
| Candidate specification is incomplete or invented | require source binding and return indeterminate when required fields are missing |
| Plan input is used as actual evidence | enforce separate types and storage tables; release engine accepts only snapshot-bound evidence |
| Actual change contains extra work | compare preview hash to actual assessment and report unplanned deviations |
| Rules miss a component-specific failure | conservative defaults, versioned rule catalog and golden false-negative corpus |
| Product scope expands into authoring or device control | explicit non-goals, read-only connector manifest, no command routes |
| Price is mistaken for guaranteed procurement cost | preserve quote time, MOQ, currency, shipping, tax, expiry and source hash |
| READY is mistaken for certification | show policy scope disclaimer in report and UI |
