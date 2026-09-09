# FORGE Integration Hub

FORGE의 외부 연결은 기존 CAD, PLM, Git, CI, 공급망, CAE 또는 장치를 대체하지
않는다. 각 시스템에서 읽은 결과를 출처, 조회 시점, 원본 식별자와 해시가 있는
증거로 변환하는 검증 계층이다.

## 공통 연결 수명주기

모든 새 provider는 다음 순서를 지킨다.

1. `forge_core/integration_hub.py`에 provider ID, 분류, 인증 방식, 최소 read-only
   capability와 credential field를 등록한다.
2. provider 전용 Pydantic 입력·상태·증거 모델을 정의한다. 입력은 unknown field를
   거부하고, 증거는 canonical hash를 재현해야 한다.
3. 네트워크 transport와 provider service를 분리한다. 고정된 HTTPS host, timeout,
   응답 크기 제한, redirect 차단과 안전한 공개 오류를 적용한다.
4. `test_connection`은 자격증명을 영속 설정으로 저장하지 않고 identity, scope,
   repository/project, 최소 권한을 확인한다. 로컬 GitHub 시험은 PEM을 0600 임시 후보로
   사용한 뒤 삭제하며, 중단된 후보는 다음 서비스 시작 때 정리한다.
5. `connect`는 시험이 성공한 뒤에만 설정을 저장한다. access token은 저장하지 않고,
   장기 secret은 Git에서 제외된 로컬 credential store 또는 운영 secret vault에 둔다.
6. 영속 `connect`/`sync` route는 `Idempotency-Key`를 요구한다. 같은 키·같은 입력은
   최초 응답을 재생하고 같은 키·다른 입력은 409로 거부한다. 외부 조회가 끝난 뒤에는
   설정·증거 반영보다 복구 가능한 prepared 결과를 먼저 저장하고, 로컬 반영 뒤
   completed로 전환한다. 중단된 prepared 요청은 같은 키로 재시도할 때 외부 API를
   다시 호출하지 않고 로컬 반영만 복구한다.
7. `sync`는 provider 원본 ID, API version, source endpoint, 조회 시각과 원본 결합
   정보를 포함한 immutable evidence를 생성한다.

Provider마다 인증·응답 계약이 다르므로 연결 API는 provider 전용 route와 입력 모델을
사용한다. Integration Hub의 catalog/status 계약만 공통으로 유지한다. 이렇게 하면
편의를 위한 범용 JSON credential route가 secret 누출이나 잘못된 권한 확대를 만들지
않는다.

## 구현 상태

- `local_artifacts`: 로컬 CAD·문서·시험 결과 입력 사용 가능. ASCII/binary STL은
  3D mesh로, STEP은 schema/product/unit/point-bounds metadata summary로 파싱
- `github`: GitHub App과 GitHub Actions read-only driver 사용 가능
  - commit changed files, open PR, exact-SHA Actions runs는 페이지를 끝까지 수집하고,
    수집 중 원본이 바뀌거나 안전 용량 한계에서 완전성을 증명할 수 없으면 evidence
    저장 없이 fail closed. open PR은 로컬 3,000개 안전 한도를 사용하고,
    `head_sha` Actions 검색은 GitHub의 공식 1,000개 provider 한도를 사용
  - workflow run status와 nullable conclusion은 공식 GitHub Checks 상태·결론 집합에
    포함된 값만 evidence로 허용
  - 계약 근거: [Workflow runs REST](https://docs.github.com/en/rest/actions/workflow-runs?apiVersion=2026-03-10),
    [Checks REST](https://docs.github.com/en/rest/checks/runs?apiVersion=2026-03-10)
  - 저장된 증거는 조회 시점 기준 `fresh`/`stale` 상태와 경과 시간을 제공
  - GitHub sync 결과는 독립 integration evidence이며 프로젝트 release evidence로
    명시적으로 import·binding되기 전에는 `READY` 근거로 사용할 수 없음
- `gitlab`: project access token/OAuth를 가진 read-only pipeline client 계약 구현
- `jenkins`: API token 기반 read-only build-result client 계약 구현
- `onshape`: Onshape document metadata read client 계약 구현
- `simscale`: SimScale simulation run metadata read client 계약 구현
- `llm_host_mcp`: stdio MCP와 localhost HTTP MCP 개발 endpoint 구현. hosted
  Responses/Embeddings client와 semantic retrieval index는 provider transport를 주입해
  사용하며 release 판정 권한 없음
- `hil_agent`, `ros2_edge`, `mqtt_device`, `opcua_lab`: raw device control은 여전히
  금지. 대신 approved allowlist command 또는 edge evidence import 결과만 tier-bound
  evidence로 수집하는 계약 구현
- DigiKey/Mouser/Nexar 같은 BOM 가격 공급처는 이번 범위에서 제외. 수동 quote evidence
  또는 추후 provider driver로만 release cost gate에 사용
- 나머지 enterprise provider: typed catalog와 credential 요구사항은 준비됐으며 실제
  driver를 구현하기 전까지 `Adapter required`로 표시

## 필수 보안·검증 체크리스트

- write/admin scope를 요청하지 않는다.
- secret, access token, PEM 본문을 API 응답·로그·SQLite evidence에 넣지 않는다.
- 외부 응답의 identity와 사용자가 선택한 tenant/project/repository를 정확히 비교한다.
- CI·시험 결과는 대상 commit, revision 또는 design hash와 정확히 결합한다.
- pagination 또는 provider 제한으로 일부만 수집했다면 완전한 결과처럼 표시하지 않는다.
- 영속 mutation의 멱등 replay와 같은 키의 충돌 거부를 테스트한다.
- simulation, bench, HIL, physical-device 결과의 evidence tier를 유지한다.
- mock/fixture 결과를 live evidence로 승격하지 않는다.
- unit test에서 credential 비영속, 권한 부족, identity mismatch, 변조, timeout과
  malformed response를 검증한다.
- dashboard route는 loopback Host/Origin/CSRF/body-limit 계약을 그대로 사용한다.
- 새 live driver를 공개하기 전에 `./forge verify`와 실제 read-only sandbox 계정의
  연결 시험을 모두 통과한다.

## 운영 전환

로컬 pilot은 `.forge-local` credential store를 사용한다. public 또는 multi-tenant
배포에서는 이 저장소를 그대로 공유하지 말고 tenant-scoped managed secret vault,
key rotation, SSO/RBAC, audit export, rate-limit/backoff, monitoring과 backup 정책을
provider별 production gate로 추가한다.
