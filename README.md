# Project FORGE

FORGE는 로봇·임베디드 팀을 위한 **독립형 Engineering Reasoning Agent**입니다. ChatGPT, Codex 또는 다른 LLM host 안에서 엔지니어처럼 대화하되, 하드웨어 변경 의도를 source-bound plan으로 만들고 실제 firmware, BOM, protocol, test, documentation 변경과 증거를 검증해 출시 준비 상태를 판정하는 backend는 독립적으로 유지합니다.

사용자에게 보이는 흐름은 `ATTACH -> TALK -> PLAN -> CONFIRM -> SIMULATE/TEST -> REVISE -> VERIFY -> RELEASE`입니다.

1. **CONNECT/IMPORT** — 만들어진 기계·로봇, CAD model, 설계도, PLM snapshot 또는 manual snapshot을 읽기 전용 기준으로 묶습니다.
2. **PLAN** — 변경하려는 부품, 설계 의도 또는 운용 시나리오를 입력하고 예상 영향, 대안, 필요한 수정과 재시험을 정리합니다.
3. **VERIFY** — 실제 snapshot과 imported evidence를 계획과 비교해 예상된 변경, 누락된 변경, 예상 밖 변경을 구분합니다.
4. **RELEASE** — 실제 evidence와 policy만 사용해 `READY` 또는 `BLOCKED` 판정과 감사 가능한 readiness breakdown을 생성합니다.

내부 제품 루프는 `PLAN <-> DESIGN -> VERIFY -> DIAGNOSE -> FIX -> RE-VERIFY -> RELEASE`입니다. 현재 구현은 배포 시험용 로컬 pilot입니다. deterministic evidence kernel, 실제 ASCII/binary STL 형상 파싱과 STEP metadata summary, project-scoped lexical/semantic cited RAG, RBAC 감사 경계, 운영 백업·복구, read-only GitHub App 연결, GitLab/Jenkins/Onshape/SimScale credential-ready client, simulation/bench/HIL/device evidence runner 계약까지 이어집니다. SolidWorks native CAD editing, BOM live 가격 공급처, 로그인/멀티테넌시 운영은 아직 production 기능으로 약속하지 않습니다.

기본 대화 UX는 LLM host가 담당합니다. `./forge agent`는 host가 사용할 MCP tool server를
실행하고, `/app/`은 연결 상태, source evidence, candidate, simulation, audit와 release
decision을 확인하는 보조 operator console입니다. 자체 웹 화면은 ChatGPT를 복제하는
독립 채팅 제품으로 확장하지 않습니다.

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
- `/app/` secondary operator/evidence console with a compact local workflow preview,
  structured engineering cards, exact provenance and a contextual Engineering
  Workspace
- host-neutral MCP agent gateway with typed read tools, approval-required mutation
  tools, idempotent backend writes, and explicit evidence/release authority metadata
- repository-local Codex plugin that turns the LLM into the conversation surface
  while preserving FORGE as the independent engineering backend
- ASCII/binary STL 파일의 bounded parsing, 형상·bounds·content/geometry hash 저장,
  프로젝트 export와 drag-to-rotate dependency-free 3D canvas
- STEP 파일의 bounded metadata parsing, schema/product/unit/point-bounds 추출과
  source/hash-bound summary
- project-scoped text ingestion, deterministic retrieval, cited local answer와
  immutable conversation runtime ledger. 이 로컬 provider는 release 판정 권한이 없음
- hosted AI Responses/Embeddings provider를 꽂을 수 있는 HTTPS transport 계약과
  semantic RAG index. secret은 transport/log repr에서 마스킹되고 release 권한은 없음
- organization/actor/membership/project-access RBAC를 모든 project API에 강제하고
  허용·거부 결과를 append-only audit event로 저장
- deep SQLite health/readiness, SHA-256 verified backup과 기존 파일을 덮어쓰지 않는
  restore verification
- operator Settings의 Integration Hub: Git, CAD/PLM, CAE, CI, BOM 공급처,
  LLM host/MCP, robot/bench/HIL 연결을 동일한 read-only driver 계약으로 추가할 수 있는
  typed catalog와 상태 API
- GitLab pipeline, Jenkins build, Onshape document, SimScale run을 읽기 전용으로 붙일
  수 있는 credential-ready client 계약
- approved allowlist command만 실행 가능한 local evidence runner. simulation, bench,
  HIL, physical-device tier를 분리하고 stdout/stderr/result hash를 남김
- `./forge agent-http`의 localhost Streamable-HTTP-shaped MCP 개발 endpoint
- GitHub App ID, Installation ID와 `.pem` 파일 선택을 통한 실제 연결 시험,
  저장소 metadata·최신 commit 변경 파일·open PR·exact-commit Actions 수집
- GitHub App token은 메모리에서만 사용하고, private key는 Git 추적에서 제외된
  `.forge-local/github/keys`에 0600 권한과 SHA-256 무결성 검증으로 저장
- `FORGE verify`를 exact Python/locked dependencies로 실행하는 최소 권한 GitHub
  Actions workflow

## 배포 시험 기준 완성도

현재 로컬 pilot 범위의 가중 완성도는 약 **88%**입니다. 전체 상용 제품 기준으로는
BOM 가격 공급처와 로그인/멀티테넌시를 이번 범위에서 제외했을 때 약 **80%** 수준입니다.
이는 테스트 coverage와 다른 제품 범위 지표입니다: 검증·릴리스 kernel 90%, LLM
agent/tool workflow 88%, 권한·감사·근거 RAG 88%, 로컬 운영 기반 84%, 외부 live
ecosystem 60%를 pilot 우선순위로 가중했습니다.

남은 production 범위는 SolidWorks native 편집/assembly kernel, Autodesk·Teamcenter·
Windchill·Aras 같은 enterprise PLM driver, Ansys/MATLAB 같은 vendor solver driver,
BOM 공급처 live 가격, SSO/managed secret vault, hosted monitoring과 멀티테넌시입니다.
자격증명이 필요한 provider는 실제 credential을 넣기 전까지 연결 상태가 되지 않습니다.

## 실행

Python 3.14.3 가상환경과 locked dependencies를 설치한 작업 사본에서 다음 명령을 사용합니다.

```bash
./forge dev
./forge dashboard
./forge agent
./forge agent-http
```

`./forge dev`는 후보 부품 계획, 실제 revision 변경, plan-vs-actual 검증, external evidence planning, 자동 재시험, BOM/build/test evidence와 release decision을 순서대로 재현합니다.

`./forge dashboard`는 기본적으로 `http://127.0.0.1:43127/app/`에서 로컬 화면을 제공합니다. 다른 DB나 포트는 `FORGE_DATABASE_PATH`와 `FORGE_DASHBOARD_PORT`로 지정합니다.

`./forge agent`는 newline-delimited JSON-RPC MCP server를 stdio로 실행합니다. Codex용
repo-local plugin은 `plugins/forge-engineering-agent`에 있으며, 다른 위치에 설치했다면
`FORGE_REPOSITORY_ROOT`를 이 저장소 경로로 지정합니다. 같은 `FORGE_DATABASE_PATH`를
사용하면 LLM host와 operator console이 동일한 evidence ledger를 봅니다.

`./forge agent-http`는 개발용 localhost MCP endpoint를
`http://127.0.0.1:43128/mcp`에 엽니다. public 배포·OAuth·SSO는 이번 범위(9번 제외)에
넣지 않았고, 필요한 경우 `FORGE_REMOTE_MCP_TOKEN`으로 bearer token만 추가할 수 있습니다.

AI가 말한 BOM 가격은 `INFERRED ESTIMATE`로만 취급합니다. Nexar/Octopart는 필수 연결이
아니며 catalog에서 제거했습니다. RELEASE 원가 gate에는 DigiKey/Mouser 같은 공급처
응답 또는 조회 시각과 출처가 있는 수동 quote evidence가 필요합니다.

### GitHub App 연결

1. `./forge dashboard`가 출력한 주소를 Safari/Chrome에서 엽니다.
2. 왼쪽 아래 `⚙ Settings`를 누릅니다.
3. App ID, Installation ID, repository owner/name을 입력합니다.
4. Finder에서 GitHub가 내려받은 `.pem` 파일을 선택합니다.
5. `연결 시험`으로 App·installation·repository·read-only 권한을 확인합니다.
6. `안전하게 저장 및 연결` 후 `지금 증거 동기화`를 누릅니다.

동기화 결과는 source API version, 조회 시각, latest commit SHA, changed files, open
PR, 해당 SHA와 정확히 일치하는 Actions runs, canonical evidence hash를 포함합니다.
changed files, open PR, Actions runs는 모든 페이지가 완전히 수집됐을 때만 저장하며,
수집 중 원본이 바뀌거나 provider/안전 용량 한계로 완전성을 증명할 수 없으면 동기화를
차단합니다. 특히 `head_sha`로 조회하는 Actions 검색은 GitHub의 공식 1,000건 한계를
넘으면 일부 결과를 증거로 저장하지 않고 차단합니다. workflow status/conclusion도
GitHub가 문서화한 제한된 값만 허용합니다. 연결 저장과 동기화 POST에는
`Idempotency-Key`가 필수이며, 같은 키와 같은 입력의 재시도는 최초 응답을 그대로
반환하고 다른 입력으로 키를 재사용하면 409로 거부합니다. 설정 또는 증거 반영 전에
복구 가능한 준비 기록을 먼저 저장하므로, 완료 표시 직전 중단된 요청도 같은 키로
재시도하면 GitHub를 다시 호출하지 않고 로컬 반영을 마칩니다. 브라우저 UI가 이 키를
자동 생성합니다. 저장된 GitHub
증거는 `fresh`/`stale` 상태와 경과 시간을 명시합니다. 이 증거는 자동으로 release
evidence가 되지 않으며, 프로젝트 정책에 명시적으로 import·binding되지 않은 GitHub
상태는 `READY` 판정에 사용할 수 없습니다.
App ID와 Installation ID는 비밀이 아니지만 private key 내용과 발급된 installation
token은 어떤 API 응답·DB·로그에도 기록하지 않습니다. 연결 시험 중 PEM은 0600 권한의
임시 후보 파일로만 사용하고 정상 종료 시 삭제하며, 다음 서비스 시작 때 중단된 후보도
정리합니다. 설정이 참조하지 않는 중단된 commit 후보 키도 시작 시 정리합니다. 다른
로컬 위치를 사용하려면 `FORGE_CONFIG_DIR`을 설정할 수 있습니다.

`forge_core/web/index.html`을 `file://`로 직접 열면 정적 데모 화면과 실행 안내는
표시되지만, 프로젝트 영속화·커넥터·증거 API는 동작하지 않습니다. 실제 작업은
항상 `./forge dashboard`가 출력한 loopback URL에서 진행합니다.

제품 계약과 단계별 완료 조건은 `PRD.md`, operator 경험의 기준은 `DESIGN.md`를 따릅니다.
새 외부 API driver를 추가하는 공통 수명주기와 보안 gate는
`docs/integrations.md`에 정리되어 있습니다.
