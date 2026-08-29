# Design

## Source of truth

- Status: Active
- Last refreshed: 2026-08-29
- Primary product surfaces: 로컬 release-readiness 대시보드와 근거 drill-down
- Evidence reviewed: `PRD.md`, `README.md`, `.omx/ultragoal/brief.md`, `forge_core/loopback_api.py`, `forge_core/release_readiness.py`, `forge_core/impact_engine.py`
- Existing UI evidence: 기존 화면, 컴포넌트, 토큰, 브랜드 자산, 스크린샷은 없음

## Brand

- Personality: 차분하고 정밀한 현장 엔지니어링 검증 도구. 화려한 AI 비서보다 계측기와 릴리스 체크리스트에 가깝다.
- Trust signals: 원본 시스템, revision, hash, 시각, 증거 tier, 판정 규칙을 숨기지 않는다. READY보다 BLOCKED 근거를 먼저 설명한다.
- Avoid: AI 마법 표현, 과도한 그라디언트, 네온 색상, 게임형 점수, 증거 없는 성공 애니메이션, CAD·PLM·Git·CI를 대체한다는 인상.

## Product goals

- Goals: 무엇이 바뀌었는지, 무엇이 불일치하는지, 어떤 재시험이 필요한지, 어떤 증거가 제출됐는지, 왜 출시가 가능한지 또는 불가능한지를 한 화면에서 확인한다.
- Non-goals: CAD/PLM 편집, firmware build 실행, 시험 실행, 증거 수정, 출시 승인 자체를 UI에서 수행하지 않는다.
- Success signals: 운영자가 project ID와 decision hash로 30초 이내에 판정과 핵심 blocker를 찾고, 키보드만으로 모든 근거를 펼쳐 볼 수 있다.

## Personas and jobs

- Primary personas: 로봇·임베디드 릴리스 엔지니어, 시스템 엔지니어, QA/HIL 담당자, 기술 리드.
- User jobs: hardware revision 변경 검토, cross-artifact mismatch triage, required retest 배정 확인, BOM 가격 출처 확인, build/test tier 진위 확인, 출시 회의용 근거 확인.
- Key contexts of use: 개발 장비의 loopback 서버, 릴리스 리뷰 회의, 변경 영향 조사, CI·시험 결과 수집 후 최종 판정 확인.

## Information architecture

- Primary navigation: 단일 화면 내 `결정 요약`, `변경`, `불일치`, `재시험`, `증거`, `원본 근거` 앵커.
- Core routes/screens: `/app/` 대시보드, same-origin `/api/v1/projects/{project}/release-decisions/{hash}` 읽기.
- Content hierarchy: READY/BLOCKED와 평가 시각 → blocker 요약 → hardware revision delta → mismatch → required retest → tier별 증거 → BOM/build provenance → canonical IDs/hashes.

## Design principles

- 근거 우선: 색상만으로 상태를 전달하지 않고 status, reason code, source, time을 함께 표시한다.
- Tier 정직성: simulation, bench, HIL, physical device를 합치거나 상위·하위 tier로 암시하지 않고 별도 lane으로 표시한다.
- 읽기 전용 경계: UI는 검증 결과를 조회·정리하며 외부 source-of-truth나 증거를 수정하지 않는다.
- 점진적 공개: 핵심 blocker는 즉시 보이고 긴 hash·원본 provenance는 `<details>`로 펼친다.
- Tradeoffs: R0는 project/decision 검색과 단일 decision 검토에 집중하고 목록, 사용자 계정, 실시간 갱신은 만들지 않는다.

## Visual language

- Color: 따뜻한 회백색 배경, 잉크색 본문, 청록색 정보·READY, 적갈색 BLOCKED, 호박색 주의. 모든 상태는 텍스트와 아이콘을 병행한다.
- Typography: 외부 폰트 없이 system sans와 고정폭 system font. 숫자·hash·revision은 tabular/monospace로 구분한다.
- Spacing/layout rhythm: 4px 기반, 주요 간격 8/12/16/24/32px. 최대 본문 폭 1440px.
- Shape/radius/elevation: 8~12px의 절제된 radius, 1px 경계, 그림자는 최소화한다.
- Motion: 기본적으로 없음. 상태 갱신은 짧은 opacity 변화만 허용하며 reduced-motion에서는 제거한다.
- Imagery/iconography: 외부 이미지 없음. 문자·간단한 CSS 기호만 사용하고 장식용 일러스트를 사용하지 않는다.

## Components

- Existing components to reuse: 없음. API의 versioned domain vocabulary와 reason code를 그대로 재사용한다.
- New/changed components: QueryForm, StatusBanner, MetricStrip, SectionCard, ArtifactDeltaTable, FindingList, RetestMatrix, EvidenceTierGrid, ProvenanceDetails, EmptyState, ErrorNotice.
- Variants and states: READY/BLOCKED/unknown, pass/fail/required/missing/rejected, loading/empty/error/loaded.
- Token/component ownership: `/app/styles.css`의 CSS custom properties가 유일한 시각 토큰 소유자이고 `/app/app.js`는 안전한 DOM API로 화면을 구성한다.

## Accessibility

- Target standard: WCAG 2.2 AA 수준의 대비·구조·키보드 접근.
- Keyboard/focus behavior: form에서 Enter로 조회, skip link 제공, 명확한 `:focus-visible`, 모든 drill-down은 native `<details>` 사용.
- Contrast/readability: 상태 배경과 텍스트는 AA 대비를 목표로 하며 색상 외 status label과 reason code를 제공한다.
- Screen-reader semantics: 한 개의 `<main>`, heading 순서, `aria-live` 상태, table caption, 목록 semantics, status role을 사용한다.
- Reduced motion and sensory considerations: `prefers-reduced-motion`을 존중하고 blinking, 자동 이동, 색상 단독 전달을 금지한다.

## Responsive behavior

- Supported breakpoints/devices: macOS 데스크톱 우선, 360px 이상 브라우저 대응.
- Layout adaptations: 넓은 화면은 12-column grid, 900px 아래는 카드 1열, 표는 자체 가로 스크롤을 갖는다.
- Touch/hover differences: 핵심 정보는 hover에 의존하지 않으며 터치 대상은 최소 44px 높이로 유지한다.

## Interaction states

- Loading: 조회 버튼 disabled, `aria-busy=true`, 기존 결과를 지우고 명시적 로딩 문구 표시.
- Empty: project ID와 canonical decision hash 입력 방법 및 읽기 전용 범위를 설명한다.
- Error: API의 안전한 error code를 보여주되 내부 경로나 원문 stack은 표시하지 않는다.
- Success: URL fragment에 project와 decision hash를 보존해 새로고침 가능한 로컬 북마크를 제공한다.
- Disabled: form 검증 실패 또는 요청 중에만 버튼을 비활성화한다.
- Offline/slow network: loopback 연결 실패를 `로컬 FORGE 서버에 연결할 수 없음`으로 구분한다.

## Content voice

- Tone: 짧고 사실적이며 감사 가능한 문장. 판정과 사실을 구분한다.
- Terminology: `READY`, `BLOCKED`, `hardware revision`, `required retest`, `simulation`, `bench`, `HIL`, `physical device`, `source`, `captured at`, `hash`를 번역으로 흐리지 않는다.
- Microcopy rules: “안전함”이나 “승인됨” 대신 “제출된 근거로 READY”를 사용한다. 빈 값은 추정하지 않고 `없음` 또는 `미제출`로 표시한다.

## Implementation constraints

- Framework/styling system: Python stdlib loopback server가 정적 HTML/CSS/ES module을 same-origin으로 제공한다. 새 런타임·패키지 의존성을 추가하지 않는다.
- Design-token constraints: CSS custom properties만 사용하고 외부 CDN, inline style, inline script를 금지한다.
- Performance constraints: 초기 정적 자산 합계 100KB 이하, 단일 decision JSON만 요청, polling 없음.
- Compatibility constraints: 최신 macOS Safari/Chromium, ES2022, JavaScript 비활성 시 읽기 전용 안내 표시.
- Test/screenshot expectations: 정적 자산 route·MIME·CSP·경로 제한 테스트, hostile text의 `innerHTML` 미사용 검사, 실제 loopback smoke test, 1440px와 390px 렌더링 확인.

## Open questions

- [ ] 실제 고객의 상태 색상·용어 선호 / 제품 책임자 / 파일럿 인터뷰 후 조정
- [ ] decision 목록·검색 API 필요성 / 릴리스 엔지니어 / R1 정보 구조에 영향
- [ ] 조직별 release policy 이름과 승인자 표시 / 시스템 엔지니어 / 정책 provenance 확장 시 반영
