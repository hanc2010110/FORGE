# FORGE Design Source of Truth

## Source of truth

- **Status:** Active
- **Last refreshed:** 2026-09-09
- **Primary product surfaces:** ChatGPT/Codex-compatible LLM host conversation,
  contextual Engineering Workspace, secondary operator/evidence console.
- **Evidence reviewed:** `PRD.md`, `README.md`, the current dependency-free web
  implementation under `forge_core/web/`, browser QA from G017, the supplied
  “FORGE 최종 UX 구조 설계 보고서” dated 2026-09-03, and the 2026-09-09
  GPT-like simplification review of the live loopback UI and Integration Hub.

This file is the UI/UX decision contract. When old dashboard markup or copy
conflicts with this document, the LLM-hosted agent contract here wins.

## Brand

- **Personality:** calm senior engineering collaborator; direct, careful and
  evidence-literate rather than theatrical or autonomous.
- **Trust signals:** source labels, timestamps, immutable identifiers, evidence
  class, confidence, missing-information notices and explicit human gates.
- **Avoid:** dashboard-first onboarding, fake “AI is thinking” theatrics,
  unexplained certainty, decorative 3D, workflow jargon as primary navigation,
  or UI that suggests FORGE wrote to CAD, PLM, Git or a device.

## Product goals

- Let a user start in their LLM host with one message and progressively attach the
  context FORGE actually needs.
- Keep the design loop conversational while rendering proposals, simulations,
  evidence and release decisions as inspectable engineering objects.
- Make confirmation, acceptance, formal verification and release four visibly
  different acts.
- Prepare a production boundary for authenticated actors, organization isolation,
  auditable authorization and source-grounded LLM/RAG without weakening the
  deterministic evidence kernel.

Non-goals remain native CAD authoring, an in-product physics solver, direct device
control, automatic source-system writeback and evidence-free engineering truth.

Success means a pilot engineer can begin without setup training, understand the
current proposal and missing evidence in three interactions or fewer, and never
mistake chat output for verified release evidence.

## Personas and jobs

- **Design engineer:** attach current design context, discuss a change, compare
  alternatives, confirm a candidate and review simulation evidence.
- **Firmware/electronics engineer:** inspect pin, voltage, unit, range and protocol
  effects and know which builds/tests must run again.
- **Verification engineer:** import CI, bench, HIL and device results without
  collapsing evidence tiers.
- **Release approver:** verify policy, lineage, separation of duties and blockers
  before recording a release decision.
- **Organization administrator:** manage project access and audit export without
  gaining authority to invent engineering evidence.

Primary contexts are desktop engineering review, laptop bench work and narrow-screen
status review. Keyboard use and dense identifiers are first-class requirements.

## Product Experience

FORGE is an independent engineering agent for robotics and embedded teams. The LLM
host owns natural conversation. FORGE owns typed engineering objects, provenance,
candidate freezing, evidence binding, deterministic checks, audit, and the final
release-policy calculation.

FORGE is the validation layer connecting CAD, PLM, Git, CI, simulation, bench, HIL,
and physical-device systems. It does not replace or silently write to those systems.

The user-facing loop is:

`CONNECT → TALK → PROPOSE → DISCUSS → CONFIRM → SIMULATE → REVIEW → MODIFY →
RE-CONFIRM → RE-SIMULATE → VERIFY → RELEASE`

`PROJECT → PLAN → EXECUTE → VERIFY → RELEASE` may remain an internal process model,
but it must not force the user through disconnected screens.

## Success Signals

- A user can begin from one conversation composer by attaching a CAD/design file,
  importing a project, or choosing a read-only machine/robot connection.
- A change request becomes an inspectable recommendation with `Why`, evidence,
  expected impact, confidence, and `Discuss / Modify / Confirm & Simulate` actions.
- No simulation can run before explicit human confirmation creates an immutable
  candidate snapshot. Confirmation is a two-step backend gate: the authenticated
  operator approves one exact candidate request with a single-use nonce, then the
  backend consumes it into a receipt binding actor, request and candidate hashes.
- Current, Proposed, and Difference views remain visible beside the conversation.
- Failed results return to the same conversation as revision options. Earlier
  candidates and simulations remain available for comparison.
- A passed simulation is not presented as verified, measured, safe, or releasable.
- READY or BLOCKED uses imported source-bound evidence and policy, never chat text.

## Primary Host Experience

The default surface is ChatGPT, Codex, or another approved LLM host:

- **Conversation:** the host supplies message history, composer, attachments and
  familiar navigation. FORGE must not rebuild these as its primary product shell.
- **Context:** the host attachment action progressively adds CAD, project,
  engineering files, BOM, datasheets, test results, requirements or a future
  read-only machine connection.
- **FORGE objects:** recommendations, alternatives, approval summaries, simulation
  results and release decisions render as tool-backed objects in the conversation.
- **Engineering Workspace:** an optional host component or linked view opens when
  a user chooses
  `Open 3D`, `View Simulation`, `Compare Revisions`, an evidence source, or an
  equivalent contextual action.

The local `/app/` surface is a secondary operator/evidence console. It owns
connection setup, source provenance, audit history, exact hashes and failure
diagnostics. It may retain a compact GPT-like visual language for consistency, but
it must not present static scripted conversation as if it were the production LLM.

When the Engineering Workspace is open, desktop becomes two panes:

- **Left — Engineering View:** connected asset context, Current/Proposed/Difference,
  properties, Live Plan, confirmed candidate, simulation results, evidence, and
  revision comparison.
- **Right — FORGE Conversation:** user intent, assistant reasoning, recommendation
  cards, proposal controls, candidate confirmation, simulation review, and iterative
  follow-up.
- **Bottom — Session Status:** goal, proposal revision, confirmed candidate,
  simulation result, and verification readiness.

Closing the workspace returns to the same chat position. The transition must feel
like the conversation expanded, not like navigation to another product area. The
conversation remains the interaction surface; engineering objects are structured
cards rather than long prose. On tablet and mobile the workspace is an in-flow
panel or full-screen sheet after the conversation trigger. Tables scroll locally.

The legacy project explorer, top stage tracker, detached AI side rail and scripted
demo conversation are not primary product surfaces. The evidence console remains
available for exact API, connector and provenance inspection.

Operational readiness matrices and unavailable-provider catalogs are not welcome
content. The sidebar shows only a compact connection summary, while Settings owns
credential entry, synchronization, and the expandable full provider catalog.

## Information architecture

- **Primary navigation:** New chat, Projects, Recent sessions, History.
- **Core surfaces:** Chat Home → Engineering Session → optional contextual
  Workspace; Advanced Evidence is a secondary disclosure, not a main destination.
- **Content hierarchy:** latest user intent → FORGE response → engineering object
  card → evidence/why → next human action. Process state is supporting metadata.
- **Progressive context:** users may start with CAD + natural language, repository
  + BOM + natural language, a project connection, a datasheet/photo, or plain text.
  FORGE asks for missing decision-changing context inside the conversation.

## Design principles

1. **Talk first. Expand when needed. Verify with evidence.**
2. **Progressive context over setup gates.** Ask only for information that can
   change the engineering decision.
3. **Engineering objects over prose walls.** Recommendations, Live Plans,
   candidates, simulation results and release decisions are structured timeline
   cards.
4. **Human gates are deliberate.** `Confirm & Simulate`, `Accept Design`,
   `Run Verification` and release approval cannot be inferred from casual text.
5. **Truth classes never blur.** Fact, calculated, simulated, measured and inferred
   content remain visually and structurally distinct.

The main tradeoff is lower always-visible dashboard density in exchange for a much
lower learning cost. Exact provenance remains one click away in cards, workspace
and the advanced console.

## Conversation Contract

FORGE remembers the active project, selected component, goal, constraints,
candidate, simulation, and unresolved evidence gaps. It asks only questions that
can change the engineering decision.

Recommendations use this shape:

1. Recommendation
2. Why
3. Evidence
4. Expected impact
5. Confidence
6. Discuss / Modify / Confirm & Simulate

Natural language never implies confirmation. Phrases such as “괜찮아 보이네” or
“이걸로 할까?” require an explicit confirmation prompt. Only a deliberate
`Confirm & Simulate` action may freeze a candidate and schedule a simulation.

The MVP example is the upper-arm change session:

- User intent: increase arm length from 420 mm to 520 mm.
- Initial recommendation: retain the existing J2 motor and evaluate a Ribbed Box
  cross section before expanding the motor/BOM/control change scope.
- Candidate `DC-014`: +100 mm, Al 6061, Ribbed Box, mass ≤ 1.89 kg, deflection
  ≤ 3.0 mm, payload 5 kg, top speed unchanged.
- Fixed demo result `SIM-023`: stress PASS, mass PASS, torque PASS, deflection
  4.1 mm FAIL.
- Revision discussion: keep rib height at 14 mm and increase width 42 → 45 mm.
- Candidate `DC-015` and fixed demo result `SIM-024`: deflection 2.8 mm and all
  displayed structural requirements PASS.
- Result acceptance still leaves thermal simulation and a measured duty-cycle test
  required before formal verification.

## Live Plan

The Live Plan is generated continuously from the conversation and always shows:

- goal;
- confirmed constraints;
- current recommendation;
- affected systems;
- required simulations;
- required verification and missing evidence.

It is not a separate questionnaire or page. Updating a constraint updates the Live
Plan and current proposal without mutating a confirmed candidate. A material change
after confirmation creates a new proposal and candidate revision.

## Engineering State Model

Backend and UI language distinguish these states:

`IDEA → PROPOSED → SELECTED → CONFIRMED → SIMULATED → REVISED → CONFIRMED →
SIMULATED → ACCEPTED → VERIFIED`

- `IDEA`: unstructured user intent.
- `PROPOSED`: inspectable FORGE recommendation; not an execution input.
- `SELECTED`: preferred alternative; still mutable and unconfirmed.
- `CONFIRMED`: immutable candidate snapshot bound to baseline, proposal, user, and
  UTC confirmation time.
- `SIMULATED`: tool output bound to the exact candidate hash.
- `REVISED`: a new proposal produced from review; prior candidate/result retained.
- `ACCEPTED`: the engineer accepts a simulation result for design progression.
- `VERIFIED`: required imported evidence is present and checked. Acceptance alone
  can never reach this state.

Allowed transitions fail closed. `SELECTED → SIMULATED` is forbidden. Simulation
must bind both candidate and simulation hashes. `ACCEPTED → VERIFIED` requires
evidence references.

## Evidence and Reasoning

Every engineering claim is visibly classified:

- `FACT`: directly imported CAD, BOM, requirement, datasheet, source, or snapshot.
- `CALCULATED`: deterministic calculation from named inputs.
- `SIMULATED`: output from a named simulation tool and exact candidate.
- `MEASURED`: imported bench, HIL, or physical-device measurement.
- `INFERRED`: LLM recommendation or interpretation, always with confidence.

Unknown or missing data remains explicit. FORGE must never convert an inference
into a fact, a simulation into a measurement, or an accepted result into release
evidence.

The responsibility boundary is visible in the interface:

- LLM/FORGE: conversation, context synthesis, alternatives, explanations,
  recommendation, and identifying missing verification.
- Deterministic tools: calculations, schema checks, simulation execution, evidence
  parsing, and policy evaluation.
- Human: confirmation, design acceptance, waivers, and release authority.

## Simulation and Revision

A simulation request follows this sequence:

1. Resolve project and baseline context.
2. Build a structured proposal.
3. Obtain explicit confirmation.
4. Freeze an immutable Design Candidate.
5. Bind the tool invocation and results to the candidate hash.
6. Render result, requirement, verdict, provenance, and uncertainty together.
7. Offer `Ask FORGE`, `Modify Design`, `Accept Result`, and
   `Run Additional Verification`.

Simulation results belong in Engineering View and may also be referenced in the
conversation. A revision creates a new candidate and result; it never overwrites the
previous record. Revision comparison shows parameter and result deltas.

The browser MVP uses clearly labelled deterministic fixtures for `SIM-023` and
`SIM-024`. It must say that no live CAE solver or physical device produced them.
Production simulation requires connector-mediated tool invocation and captured
provenance.

## Verification and Release

Simulation pass and formal verification are separate gates. Formal verification
compares planned versus actual change and checks required evidence environments:
simulation, CI/build, bench, HIL, and physical device. Each environment stays
distinct.

Release readiness evaluates immutable source snapshots, policy, BOM quote source
and lookup time, firmware build, retest requirements, protocol/interface checks, and
test evidence. It returns `READY` or `BLOCKED` with blocker codes and an audit report.
It does not mean safety certification, regulatory approval, or replacement of human
release authority.

## Visual language

- Quiet GPT-like dark neutral canvas: one continuous conversation surface, a
  slightly darker navigation rail, and restrained dividers instead of nested
  dashboard containers.
- Teal for active context and selected proposals; amber for unknown, inferred, or
  missing information; red for failed requirements; green only for evidence-backed
  pass or readiness.
- System sans-serif UI with monospace reserved for identifiers, hashes,
  measurements, and states.
- One-pixel dividers, generous conversation whitespace, restrained radius and
  elevation, no decorative imagery.
- Dense but readable tables and cards; no nested cards without a distinct
  engineering object boundary.
- Minimum 44 px touch controls, visible focus, text paired with status color, and
  reduced-motion support.

## Components

- **Chat shell:** collapsible navigation rail, chronological conversation, context
  chips, and a sticky multiline composer with an explicit attachment menu.
- **Welcome state:** centered FORGE mark, one plain-language prompt, three compact
  example intents, and the composer. No readiness dashboard or product-tour card.
- **Integration settings:** connected provider and synchronization controls first;
  full future-provider catalog is collapsed by default and filterable by domain.
- **Engineering message:** assistant summary plus a typed object card. Long-form
  analysis is collapsed behind `Why` or `Evidence`.
- **Recommendation card:** recommendation, rationale, evidence references,
  expected impact, confidence, and `Discuss / Modify / Confirm & Simulate`.
- **Live Plan card:** goal, constraints, affected systems, proposed work, required
  simulation and missing verification. It updates in place until confirmation.
- **Candidate card:** immutable revision identifier, baseline hash, proposal hash,
  actor, UTC confirmation time and the exact constraints being approved.
- **Simulation card:** tool/source, exact candidate, progress or terminal state,
  requirement-by-requirement results, uncertainty and next actions.
- **Evidence card:** source system, source locator, observed time, captured time,
  evidence class and immutable artifact hash.
- **Release card:** READY/BLOCKED, evaluated policy, blockers, evidence lineage and
  human approval state. Green is reserved for this evidence-backed result.
- **Contextual workspace:** optional CAD/simulation/comparison/evidence panel. Its
  empty state explains which source is missing; it never renders decorative fake
  geometry as an imported model.

## Accessibility

- All primary actions and timeline objects are keyboard reachable in reading order.
- Focus is always visible; opening or closing the workspace restores focus to the
  triggering control.
- Status never depends on color alone. Icons, short labels and accessible text
  accompany PASS, FAIL, BLOCKED, INFERRED and missing states.
- The composer, attachment button, navigation toggle and card actions have explicit
  names; expandable regions expose their state.
- Motion is limited to short disclosure transitions and disabled under
  `prefers-reduced-motion`.

## Responsive behavior

- At wide desktop widths, chat stays primary and the contextual workspace opens as
  a resizable adjacent pane.
- At laptop widths, the workspace may narrow but the composer and latest response
  remain visible without horizontal page scrolling.
- At tablet and phone widths, navigation becomes a drawer and the workspace opens
  as an in-flow region or full-screen sheet with a clear return to conversation.
- Engineering tables scroll inside their own region. Identifiers and units do not
  wrap into ambiguous fragments.

## Interaction states

- **Loading:** skeleton only for the object being resolved; existing conversation
  and provenance remain readable.
- **Empty:** one suggested prompt and context actions, not a dashboard of empty
  widgets.
- **Missing context:** name the missing source and why it can change the decision.
- **Provider unavailable:** preserve the user message, explain that no engineering
  conclusion was recorded, and allow retry or deterministic inspection.
- **Unauthorized:** show the required permission and actor/organization boundary;
  do not hide an action that the user needs to request.
- **Conflict/stale revision:** keep both revisions visible and require an explicit
  rebase or new confirmation.
- **Simulation failure:** show partial source-bound results, logs and revision
  options; never convert a tool failure into an inferred PASS.

## Content voice

FORGE writes like a concise senior engineering collaborator. It leads with the
recommendation or blocker, states assumptions, uses explicit units, and separates
what is known from what is inferred. Buttons use verbs. Errors say what was
preserved, what was rejected and what evidence or permission is needed next.

## Non-Goals and Safety Boundaries

- No native CAD/EDA authoring.
- No unapproved CAD, PLM, Git, ticket, or document writeback.
- No device command, flashing, actuator movement, or rig control.
- No built-in claim of FEA/CFD/SPICE/thermal solver execution.
- No evidence-free root cause or release decision.
- No collapsing simulation, bench, HIL, and physical-device evidence into one tier.
- No claim that a browser-local attachment was uploaded, parsed, or persisted.

## Implementation constraints

- Frontend remains dependency-free static HTML, CSS, and ES modules.
- DOM rendering uses safe text nodes; no `innerHTML`, dynamic script, or remote
  asset dependency.
- Dashboard assets remain same-origin, allowlisted, and under the existing byte
  budget.
- Backend contracts are frozen Pydantic models with unknown fields rejected,
  canonical SHA-256 hashes, UTC timestamps, canonical ordering, and exact lineage.
- Existing release and evidence APIs remain authoritative. Conversational design
  contracts do not become release evidence by themselves.

## Deployment-readiness sequence

The next deployment test is gated by foundations, not by the number of external
integrations:

1. Ship the chat-first shell and preserve the deterministic evidence console.
2. Enforce actor, organization, project membership and RBAC at every project API;
   persist append-only allowed and denied audit results. This is implemented for
   the local pilot.
3. Run project/tenant-scoped cited retrieval through a provider that cannot create
   measured evidence or READY. A deterministic no-network provider is implemented;
   hosted model and embedding providers remain deployment choices.
4. Verify local health, backup and non-overwriting restore. These are implemented;
   secrets and hosted monitoring remain public/multi-tenant gates.
5. Parse one high-value CAD geometry path before broad external integration. Bounded
   ASCII/binary STL parsing and a read-only canvas are implemented. The first
   credentialed Git/CI path is now a separate `GitHubIntegrationService` with a
   fixed-host network transport, non-persistent short-lived token, local key vault,
   canonical evidence record and exact commit-to-Actions binding. Its persistent
   connect/sync mutations use a prepared/completed recovery journal, and exact-SHA
   Actions collection fails closed at GitHub's 1,000-result filtered-search cap.

STEP/SolidWorks-export attachment inspection, hosted AI/embedding transport contracts,
semantic retrieval, GitLab/Jenkins/Onshape read-only clients, exact-request-approved
SimScale/MATLAB execution clients, a durable SQLite CAE execution ledger,
authenticated localhost HTTP MCP and approved
simulation/bench/HIL/device evidence-runner contracts are now part of the local
pilot. SolidWorks native editing, enterprise PLM kernels, vendor CAE solvers, BOM
price suppliers, SSO and hosted multitenant operations remain outside this round.

Automatic re-verification uses a durable SQLite outbox: release evidence and its
exact approval trigger commit in one project transaction; the deterministic release
decision and completion receipt commit in the next. Dashboard and MCP startup drain
pending triggers, so a process exit between those commits cannot silently lose the
approved verification request.

### Local-pilot composition boundary

`ReleaseIntegrationService` is deliberately the local application's transaction
script and authorization composition root. It coordinates atomic writes but delegates
engineering rules to the domain modules (`impact_engine`, `preview_engine`,
`release_readiness`, `conversation_runtime`, `local_rag`, and `cad_geometry`). It must
not absorb connector SDK, hosted-model, CAD-kernel, or device-transport logic. Before
the first credentialed connector, hosted LLM, SSO provider, or multi-tenant deployment,
split the matching adapter/application service from this local facade. The GitHub
vertical slice satisfies this boundary: `ReleaseIntegrationService` remains unchanged,
while `IntegrationHub` owns the typed provider catalog and the dedicated GitHub service
owns credentials, authentication, transport and evidence normalization. Future drivers
must use the same separation instead of adding SDK logic to the release facade.

### Integration Hub contract

- Settings is the single operator entry point for Git, CAD/PLM, CAE/simulation,
  CI/test, BOM/supply, AI/RAG and robot/lab connections.
- Every provider advertises typed credentials and canonically ordered read-only
  capabilities. The catalog never stores credential values.
- `live`, `local`, `driver_available`, and `adapter_required` are product truth states.
  `driver_available` means client code and source/time/hash connector envelopes exist,
  but credential lifecycle, connection test, sync and domain release-evidence
  normalization are not wired. A catalog card cannot claim
  connection merely because a provider or driver is listed.
- Network drivers must fix their upstream host, bound time and response size, redact
  secrets and persist normalized source/time/hash evidence rather than bearer tokens.
- Device, Bench and HIL drivers require a mutually authenticated edge agent and remain
  evidence-read paths; the hosted FORGE service does not receive a generic command or
  device-control route.
- The signed edge contract accepts only explicit ROS2, MQTT, OPC-UA or HIL-artifact
  source schemes, binds candidate/firmware/artifact hashes, and rejects nonce or
  sequence replay. Development HMAC is replaceable through the production verifier
  boundary and is not a production identity mechanism.
- The approved iteration orchestrator reacts to a newly recorded evidence hash only
  after user release confirmation, invokes the deterministic release verifier once,
  and never grants that authority to the LLM host.

The local extractive RAG provider may combine at most three retrieved chunks, cites
each excerpt separately, and redacts English and Korean release-authority phrases.
Only `release_readiness` may calculate `READY` or `BLOCKED`.

## Open questions

- Which identity provider and organization provisioning model will the first pilot
  use?
- Will tenant isolation begin with a database-per-organization design or enforced
  row-level ownership in a shared database?
- After the implemented STL geometry pilot, which metadata path has the greatest
  value: STEP assembly structure or a SolidWorks export package?
- Which LLM and embedding providers satisfy data residency, retention and customer
  opt-out requirements?
- Which external connector is the first deployment gate: GitHub/GitLab CI, BOM
  quote capture, or PLM revision snapshots?

## Current MVP Boundary

Implemented now:

- ChatGPT/Grok-style chat-first default shell with left navigation, progressive
  `+` context attachment, bottom composer, recent/session affordances, and local
  operator identity;
- contextual Engineering Workspace that is hidden by default and opens only after
  an explicit workspace action or relevant context connection;
- pre-deployment essentials gate that shows enforced local RBAC/audit, cited local
  RAG, STL geometry and local operations separately from driver-only CAD/CAE/device
  and hosted operations;
- attachment/connect affordances labelled as local/read-only concepts;
- structured evidence-backed recommendation and alternative selection;
- constraint refinement and Live Plan;
- explicit confirmation gate and candidate snapshots;
- deterministic failed simulation, design revision, passed re-simulation, result
  acceptance, and missing-verification guidance;
- Current/Proposed/Difference and revision comparison;
- immutable backend contracts for evidence claims, candidates, simulation binding,
  and guarded state transitions;
- SQLite v13 persistence and secure loopback API routes for candidate approvals and confirmed candidates,
  exact simulation bindings, conversational evidence claims, and append-only state
  history;
- direct-file launch recovery: local assets still render and the interface explains
  that persistence and connectors require the loopback server;
- existing advanced change, evidence, diagnosis, resolution, and release console.
- organization/actor/membership/project-access RBAC enforcement, separation-of-duty
  checks and tamper-visible append-only audit events;
- tenant/project-scoped local RAG execution with exact prompt, provider, retrieved
  context, citations and classified-claim binding;
- bounded ASCII/binary STL parsing, immutable content/geometry hashes, bounds and
  triangle metadata, secure upload/read API and draggable read-only canvas;
- deep SQLite health/readiness, verified backup and non-overwriting restore;
- fail-closed deployment assessment that blocks public profiles missing any
  required operational control.

Not yet production-connected:

- production hosted LLM/embedding credential storage and enterprise data controls;
- SolidWorks native assembly parsing and CAD editing;
- vendor CAE/thermal/dynamics solver execution beyond the allowlisted evidence-runner contract;
- identity-provider login and self-service organization administration;
- live robot/machine control beyond tier-bound evidence import and allowlisted edge runs;
- connector-mediated source writeback.
- hosted deployment monitoring, secret management and multitenancy controls.
