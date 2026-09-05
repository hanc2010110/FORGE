# Project FORGE

FORGE는 로봇·임베디드 팀을 위한 **AI Engineering Change & Resolution Platform**입니다. 하드웨어 변경 의도를 source-bound plan으로 만들고, 실제 firmware, BOM, protocol, test, documentation 변경과 증거를 검증해 출시 준비 상태를 판정합니다.

사용자에게 보이는 흐름은 `CONNECT/IMPORT -> PLAN -> VERIFY -> RELEASE`입니다.

1. **CONNECT/IMPORT** — 만들어진 기계·로봇, CAD model, 설계도, PLM snapshot 또는 manual snapshot을 읽기 전용 기준으로 묶습니다.
2. **PLAN** — 변경하려는 부품, 설계 의도 또는 운용 시나리오를 입력하고 예상 영향, 대안, 필요한 수정과 재시험을 정리합니다.
3. **VERIFY** — 실제 snapshot과 imported evidence를 계획과 비교해 예상된 변경, 누락된 변경, 예상 밖 변경을 구분합니다.
4. **RELEASE** — 실제 evidence와 policy만 사용해 `READY` 또는 `BLOCKED` 판정과 감사 가능한 readiness breakdown을 생성합니다.

내부 제품 루프는 `PLAN <-> DESIGN -> VERIFY -> DIAGNOSE -> FIX -> RE-VERIFY -> RELEASE`입니다. 현재 구현은 배포 시험용 로컬 pilot입니다. deterministic evidence kernel, 실제 ASCII/binary STL 형상 파싱, project-scoped cited RAG, RBAC 감사 경계와 운영 백업·복구까지 연결되어 있습니다. Hosted LLM, STEP/SolidWorks, CAE와 외부 시스템 live connector는 아직 production 기능으로 약속하지 않습니다.

`/app/`은 GPT처럼 대화에서 바로 시작하는 Engineering Session입니다. 첫 진입은
`What do you want to build or change?`에서 시작하고 CAD, 프로젝트, BOM, 데이터시트,
시험 결과 같은 source-bound context는 대화 중 점진적으로 추가합니다. 3D, simulation,
revision comparison 또는 evidence를 자세히 볼 때만 Engineering Workspace가 같은 대화
옆에 열립니다.

## 신뢰 경계

- FORGE는 CAD·PLM·Git·CI·simulation·bench·HIL·device-test 시스템을 대체하지 않습니다.
- FORGE는 자체 CAD authoring, physics solver, firmware flash, device control, 외부 시스템 write-back을 하지 않습니다.
- Preview, diagnosis, fix recommendation은 release evidence가 아닙니다.
- Root cause와 release status는 source-bound evidence와 policy 없이 확정하지 않습니다.
- `simulation`, `bench`, `HIL`, `physical_device` 증거는 서로 다른 tier이며 대체할 수 없습니다.

## 현재 구현

- read-only connector snapshot과 immutable sidecar persistence
- source-bound component change preview
- pin, voltage, unit, command range, safe state, protocol schema, BOM quote 검사
- firmware, BOM, protocol, test, documentation 예상 영향과 required retest 지정
- operating scenario 기반 external evidence plan
- simulation, bench, HIL, physical-device evidence tier 분리
- plan-vs-actual deviation 검증
- imported test evidence와 external evidence plan 검증
- immutable SQLite/API records for planning-only design proposals, evidence-bound blocker diagnoses, fix proposals, and replan seeds
- confirmed design candidates, exact simulation bindings, evidence claims, append-only conversation state transitions를 저장하는 SQLite v7/API ledger
- release evidence ingest와 deterministic `READY`/`BLOCKED` decision
- loopback-only API with Host, Origin, CSRF, body-limit, idempotency and optimistic concurrency checks
- `/app/` chat-first static Engineering Session with progressive context, structured
  engineering cards, explicit confirmation gates and a contextual Engineering
  Workspace
- ASCII/binary STL 파일의 bounded parsing, 형상·bounds·content/geometry hash 저장,
  프로젝트 export와 drag-to-rotate dependency-free 3D canvas
- project-scoped text ingestion, deterministic retrieval, cited local answer와
  immutable conversation runtime ledger. 이 로컬 provider는 release 판정 권한이 없음
- organization/actor/membership/project-access RBAC를 모든 project API에 강제하고
  허용·거부 결과를 append-only audit event로 저장
- deep SQLite health/readiness, SHA-256 verified backup과 기존 파일을 덮어쓰지 않는
  restore verification

## 배포 시험 기준 완성도

현재 로컬 pilot 범위의 가중 완성도는 약 **80%**입니다. 이는 테스트 coverage와 다른
제품 범위 지표입니다: 검증·릴리스 kernel 90%, chat-first workflow 88%, 권한·감사·근거
RAG 85%, 로컬 운영 기반 78%, 외부 live ecosystem 35%를 pilot 우선순위로 가중했습니다.

남은 production 범위는 hosted LLM/embedding, STEP·SolidWorks assembly metadata,
실제 CAE/동역학 실행, GitHub/GitLab·PLM·CI·BOM 공급처 credential 연동, SSO/secret
management, hosted monitoring과 멀티테넌시입니다. FORGE는 이들을 대체하지 않고
read-only connector와 source-bound evidence로 연결합니다.

## 실행

Python 3.14.3 가상환경과 locked dependencies를 설치한 작업 사본에서 다음 명령을 사용합니다.

```bash
./forge dev
./forge dashboard
```

`./forge dev`는 후보 부품 계획, 실제 revision 변경, plan-vs-actual 검증, external evidence planning, 자동 재시험, BOM/build/test evidence와 release decision을 순서대로 재현합니다.

`./forge dashboard`는 기본적으로 `http://127.0.0.1:43127/app/`에서 로컬 화면을 제공합니다. 다른 DB나 포트는 `FORGE_DATABASE_PATH`와 `FORGE_DASHBOARD_PORT`로 지정합니다.

`forge_core/web/index.html`을 `file://`로 직접 열면 정적 데모 화면과 실행 안내는
표시되지만, 프로젝트 영속화·커넥터·증거 API는 동작하지 않습니다. 실제 작업은
항상 `./forge dashboard`가 출력한 loopback URL에서 진행합니다.

제품 계약과 단계별 완료 조건은 `PRD.md`, operator 경험의 기준은 `DESIGN.md`를 따릅니다.
