const PROJECT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const SAFE_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const SHA_PATTERN = /^sha256:[0-9a-f]{64}$/;
const DECISION_PATTERN = SHA_PATTERN;
const VERSION_PATTERN = /^[1-9][0-9]*$/;
const PLAN_RECOMMENDATIONS = new Set([
  "changes_required",
  "incompatible",
  "indeterminate",
]);
const RELEASE_STATES = new Set(["ready", "blocked"]);
const IS_FILE_PROTOCOL = window.location.protocol === "file:";
const TIERS = [
  ["static", "Static", "BOM·build·contract"],
  ["simulation", "Simulation", "모델 기반 결과"],
  ["bench", "Bench", "fixture 기반 bench"],
  ["hil", "HIL", "hardware-in-the-loop"],
  ["physical_device", "Physical device", "실제 장치 instance"],
];

const byId = (id) => document.getElementById(id);
let activeDiagnosisHash = "";
let activeFixProposalHash = "";
let activeAlternativeHash = "";
let flowFocusTimer = 0;
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function setWorkspaceValue(id, value) {
  const target = byId(id);
  if (target) target.textContent = display(value);
}

function setWorkflowStage(stage) {
  const order = ["project", "plan", "execute", "verify", "release"];
  const activeIndex = order.indexOf(stage);
  for (const [index, name] of order.entries()) {
    const item = byId(`stage-${name}`);
    if (!item) continue;
    item.classList.toggle("complete", index < activeIndex);
    item.classList.toggle("current", index === activeIndex);
    item.classList.toggle("active", index === activeIndex);
    if (index === activeIndex) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
    const marker = item.querySelector("span");
    if (marker) marker.textContent = index < activeIndex ? "✓" : index === activeIndex ? "●" : "○";
  }
}

function scrollToFlowTarget(targetId, focusId = "") {
  const target = byId(targetId);
  if (!target) return;
  window.clearTimeout(flowFocusTimer);
  for (const panel of document.querySelectorAll(".workflow-focus")) panel.classList.remove("workflow-focus");
  target.classList.add("workflow-focus");
  target.scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "start" });
  const focusTarget = focusId ? byId(focusId) : null;
  window.setTimeout(() => focusTarget?.focus({ preventScroll: true }), reducedMotion.matches ? 0 : 360);
  flowFocusTimer = window.setTimeout(() => target.classList.remove("workflow-focus"), reducedMotion.matches ? 20 : 1300);
}

function setFlowGuide({ step, title, copy, label, targetId, focusId = "", stage = "plan", complete = false }) {
  const guide = byId("workflow-guide");
  const button = byId("workflow-next-action");
  if (!guide || !button) return;
  byId("workflow-guide-step").textContent = step;
  byId("workflow-guide-title").textContent = title;
  byId("workflow-guide-copy").textContent = copy;
  button.querySelector("span").textContent = label;
  button.dataset.target = targetId;
  button.dataset.focus = focusId;
  button.disabled = complete;
  guide.classList.toggle("complete", complete);
  setWorkflowStage(stage);
}

function updateWorkspaceStats(values = {}) {
  const ids = {
    changes: "workspace-stat-changes",
    risks: "workspace-stat-risks",
    verified: "workspace-stat-verified",
    blockers: "workspace-stat-blockers",
    release: "workspace-stat-release",
  };
  for (const [key, value] of Object.entries(values)) {
    if (ids[key]) setWorkspaceValue(ids[key], value);
  }
}

function appendCollaborationMessage(role, copy) {
  const transcript = byId("forge-ai-transcript");
  if (!transcript) return;
  const message = element("article", `collaboration-message ${role.toLowerCase()}`);
  message.append(element("strong", "", role), element("p", "", copy));
  transcript.append(message);
  transcript.scrollTop = transcript.scrollHeight;
}

function element(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function replace(node, ...children) {
  node.replaceChildren(...children);
  return node;
}

function display(value, fallback = "없음") {
  if (value === undefined || value === null || value === "") return fallback;
  return String(value);
}

function firstNamed(object, names) {
  for (const name of names) {
    if (object?.[name] !== undefined && object?.[name] !== null) return object[name];
    if (object?.data?.[name] !== undefined && object?.data?.[name] !== null) return object.data[name];
  }
  return object || {};
}

function listFrom(value) {
  if (Array.isArray(value)) return value;
  if (value === undefined || value === null || value === "") return [];
  return [value];
}

function inputValue(id) {
  return byId(id).value.trim();
}

function inputNumber(id) {
  const value = Number(inputValue(id));
  if (!Number.isFinite(value)) throw new Error(`invalid_number:${id}`);
  return value;
}

function inputInteger(id) {
  const value = Number(inputValue(id));
  if (!Number.isInteger(value) || value < 1) throw new Error(`invalid_integer:${id}`);
  return value;
}

function utcTimestamp(id) {
  const raw = inputValue(id);
  if (!raw) throw new Error(`missing_timestamp:${id}`);
  const parsed = new Date(raw);
  if (Number.isNaN(parsed.valueOf())) throw new Error(`invalid_timestamp:${id}`);
  return parsed.toISOString();
}

function formatTime(value) {
  if (!value) return "시각 없음";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return String(value);
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(parsed) + " UTC";
}

function chip(value) {
  const normalized = display(value, "unknown").toLowerCase();
  return element("span", `mini-chip ${normalized}`, normalized);
}

function tagList(values) {
  const wrapper = element("div", "tag-list");
  for (const value of values || []) wrapper.append(element("span", "tag", value));
  return wrapper;
}

function pathList(values) {
  const wrapper = element("div", "path-list");
  for (const value of values || []) wrapper.append(element("code", "", value));
  return wrapper;
}

function facts(entries) {
  const list = element("dl", "fact-list");
  for (const [label, value] of entries) {
    list.append(element("dt", "", label), element("dd", "", display(value)));
  }
  return list;
}

const guidedState = {
  step: "project",
  projectConnected: false,
  connectionMode: "",
  assetName: "",
  changeRequest: "Delivery Robot V3의 payload를 10kg에서 20kg으로 높이고 싶어. 최고 속도 3m/s는 유지하고 BOM 증가는 20% 이내로 제한해.",
  planAnalyzed: false,
  selectedAlternative: "",
  inspectedNode: "Drive system",
  planSimulationRun: false,
  planSimulationRefined: false,
  planRefinementOpen: false,
  designView: "difference",
  refinementMessage: "냉각 덕트와 thermal derating을 추가하되 최고 속도 3m/s와 BOM 증가 20% 이내 조건을 유지해.",
  planApproved: false,
  implementationComplete: false,
  verificationRun: false,
  evidenceOpen: false,
  resolved: false,
  selectedFix: "cooling",
  releaseApproved: false,
  collaborationPhase: "idea",
  selectedArmOption: "",
  armView: "difference",
  massLimit: 8,
  editingConstraint: false,
  whyMotorOpen: false,
  verificationPlanOpen: false,
  workspaceOpen: false,
  pendingKnowledge: null,
  ragAnswer: null,
  cadGeometry: null,
  cadView: { yaw: -0.55, pitch: 0.35 },
  ledger: {
    projectId: "",
    proposalHash: "",
    projectVersion: 0,
    sessionId: "",
    sequence: 0,
    state: "idea",
    candidateHash: "",
    candidateRevision: 0,
    simulationHash: "",
    simulationRevision: 0,
    claimRevision: 0,
    status: "local",
  },
};

function guidedButton(label, action, variant = "", value = "") {
  const button = element("button", variant, label);
  button.type = "button";
  button.dataset.guidedAction = action;
  if (value) button.dataset.guidedValue = value;
  return button;
}

function guidedHeader(kicker, title, copy) {
  const header = element("header", "guided-screen-header");
  header.append(element("p", "section-index", kicker), element("h3", "", title), element("p", "", copy));
  return header;
}

function conversationMessage(role, title, copy, ...children) {
  const message = element("article", `conversation-message ${role}`);
  const avatar = element("span", "conversation-avatar", role === "user" ? "U" : "F");
  const body = element("div", "conversation-message-body");
  body.append(element("strong", "conversation-speaker", title), element("p", "", copy), ...children);
  message.append(avatar, body);
  return message;
}

function artifactCanvas(title, subtitle, ...children) {
  const canvas = element("section", "artifact-canvas");
  const header = element("header", "artifact-header");
  header.append(element("div", "", ""), element("span", "artifact-mode", "STRUCTURED ARTIFACT"));
  header.firstElementChild.append(element("h4", "", title), element("p", "", subtitle));
  canvas.append(header, ...children);
  return canvas;
}

function engineeringComposer({ placeholder, action, label, value = "", attachments = false, shortcuts = [] }) {
  const composer = element("section", "engineering-composer");
  if (attachments) {
    const tray = element("div", "asset-attachment-tray");
    tray.append(
      guidedButton("＋ 파일", "upload-cad", "composer-tool"),
      guidedButton("◇ 장치 연결", "connect-machine", "composer-tool"),
      guidedButton("⌁ 프로젝트", "connect-project", "composer-tool"),
    );
    const input = element("input");
    input.id = "composer-cad-file";
    input.type = "file";
    input.accept = ".stl,.txt,.md,.csv,.json,.yaml,.yml";
    input.hidden = true;
    tray.append(input);
    composer.append(tray);
  }
  if (shortcuts.length) {
    const shortcutTray = element("div", "composer-shortcuts");
    for (const [shortcutLabel, shortcutValue] of shortcuts) {
      shortcutTray.append(guidedButton(shortcutLabel, "composer-suggestion", "composer-chip", shortcutValue));
    }
    composer.append(shortcutTray);
  }
  const inputRow = element("div", "composer-input-row");
  const textarea = element("textarea");
  textarea.id = "engineering-chat-input";
  textarea.rows = 3;
  textarea.maxLength = 1200;
  textarea.placeholder = placeholder;
  textarea.value = value;
  textarea.setAttribute("aria-label", "Message FORGE about this engineering change");
  const send = guidedButton("↑", action, "composer-send");
  send.setAttribute("aria-label", label);
  send.title = label;
  inputRow.append(textarea, send);
  composer.append(inputRow, element("p", "composer-boundary", "FORGE는 연결된 원본을 읽기만 하며, 확정 전에는 실행하지 않습니다."));
  return composer;
}

// Legacy staged-plan renderers were removed in favor of the conversational loop.

function guidedActions(...buttons) {
  const actions = element("div", "guided-actions");
  actions.append(...buttons);
  return actions;
}

function boundaryNote(copy) {
  const note = element("div", "boundary-note");
  note.append(element("strong", "", "DEMO BOUNDARY"), element("span", "", copy));
  return note;
}

function guidedMetric(label, value, detail = "") {
  const metric = element("div", "guided-metric");
  metric.append(element("span", "", label), element("strong", "", value));
  if (detail) metric.append(element("small", "", detail));
  return metric;
}


const COLLABORATION_PHASES = ["idea", "proposed", "selected", "simulated", "revised", "resimulated", "accepted"];

function phaseReached(phase) {
  return COLLABORATION_PHASES.indexOf(guidedState.collaborationPhase) >= COLLABORATION_PHASES.indexOf(phase);
}

function evidenceBadge(type, copy) {
  const badge = element("span", `evidence-badge ${type.toLowerCase()}`);
  badge.append(element("strong", "", type), element("span", "", copy));
  return badge;
}

function evidenceStack(items) {
  const stack = element("div", "evidence-stack");
  for (const [type, copy] of items) stack.append(evidenceBadge(type, copy));
  return stack;
}

function engineeringObjectHeader(type, id, state) {
  const header = element("header", "engineering-object-header");
  header.append(
    element("span", "engineering-object-type", type),
    element("strong", "", id),
    element("span", `engineering-object-state ${state.toLowerCase()}`, state),
  );
  return header;
}

function workspaceToggleButton() {
  return guidedButton(
    guidedState.workspaceOpen ? "Close workspace" : "Open workspace",
    guidedState.workspaceOpen ? "close-workspace" : "open-workspace",
    "secondary-button workspace-toggle",
  );
}

function predeployEssentialsCard() {
  const card = element("section", "predeploy-card");
  card.append(
    engineeringObjectHeader("LOCAL PILOT GATE", "PILOT-080", "READY"),
    element("p", "", "로컬 배포 시험 범위는 약 80%입니다. 실제 운영 credential이 필요한 hosted AI, CAE, 장치와 enterprise connector는 별도 production gate로 남습니다."),
  );
  const list = element("div", "predeploy-grid");
  for (const [label, state, detail] of [
    ["Org / RBAC / Audit", "complete", "project API enforcement and append-only audit"],
    ["Local cited RAG", "complete", "project-scoped retrieval; no release authority"],
    ["STL parsing / 3D viewer", "complete", "ASCII/binary geometry, hashes and read-only canvas"],
    ["CAE / Dynamics simulator", "blocked", "deterministic fixture only"],
    ["Robot / Bench / HIL", "blocked", "no device control or live telemetry"],
    [
      "Git / PLM / CI / BOM suppliers",
      "partial",
      "GitHub live read-only · remaining adapters typed",
    ],
    ["Health / Backup / Restore", "complete", "deep readiness and verified recovery"],
    ["SSO / Hosted monitoring / Multitenancy", "partial", "required before public SaaS"],
  ]) {
    const row = element("div", `predeploy-row ${state}`);
    row.append(element("strong", "", label), element("span", "", detail));
    list.append(row);
  }
  card.append(list);
  return card;
}

function conversationWelcome() {
  const welcome = element("section", "conversation-welcome");
  const mark = element("span", "conversation-welcome-mark", "F");
  const title = element("h3", "", "FORGE Agent backend");
  const copy = element(
    "p",
    "",
    guidedState.projectConnected
      ? `${guidedState.assetName}이 연결되었습니다. 원하는 변경이나 검증할 상황을 말해 주세요.`
      : "대화는 ChatGPT·Codex 같은 LLM에서 진행하고, 이 화면에서는 연결·증거·감사 상태를 확인합니다.",
  );
  const hostCard = element("section", "agent-host-card");
  hostCard.append(
    element("strong", "", "LLM host → FORGE tools → engineering systems"),
    element("p", "", "FORGE는 설계 후보, 시뮬레이션 증거, 검증 정책의 독립된 기록 계층입니다."),
  );
  const steps = element("ol", "agent-host-steps");
  for (const [label, detail] of [
    ["1", "LLM에 CAD·프로젝트·BOM·데이터시트를 첨부"],
    ["2", "대화로 설계안을 수정하고 명시적으로 후보 확정"],
    ["3", "실제 Simulation·Test 증거를 연결한 뒤 FORGE가 READY/BLOCKED 판정"],
  ]) {
    const item = element("li", "");
    item.append(element("span", "", label), element("p", "", detail));
    steps.append(item);
  }
  hostCard.append(steps);
  welcome.append(mark, title, copy, hostCard);
  return welcome;
}

function armOptionCard(id, title, summary, details, recommended = false) {
  const selected = guidedState.selectedArmOption === id;
  const card = element("article", `arm-option${selected ? " selected" : ""}${recommended ? " recommended" : ""}`);
  card.append(
    element("div", "option-kicker", recommended ? `OPTION ${id} · RECOMMENDED` : `OPTION ${id}`),
    element("h4", "", title),
    element("p", "", summary),
  );
  const list = element("ul");
  for (const detail of details) list.append(element("li", "", detail));
  card.append(list, guidedButton(selected ? "Selected" : "Choose option", "select-arm-option", selected ? "selected-option" : "secondary-button", id));
  return card;
}

function initialRecommendationCard() {
  const card = element("section", "recommendation-card");
  card.append(
    engineeringObjectHeader("FORGE RECOMMENDATION", "REC-014", "INFERRED"),
    element("h4", "", "B. 팔 길이 + 단면 보강부터 검토"),
    element("p", "recommendation-why", "Motor를 바로 교체하지 않고 Ribbed Box 단면으로 굽힘 강성을 확보하는 방향이 현재 변경 범위와 비용을 가장 적게 늘립니다."),
  );
  const metrics = element("div", "recommendation-metrics");
  metrics.append(
    guidedMetric("Torque margin", "31%", "current baseline"),
    guidedMetric("Static torque", "+18%", "+100 mm estimate"),
    guidedMetric("Deflection", "+40%", "baseline estimate"),
    guidedMetric("Change scope", "Smallest", "motor unchanged"),
  );
  const options = element("div", "arm-option-grid");
  options.append(
    armOptionCard("A", "Length only", "팔 길이만 420 → 520 mm", ["최소 부품 변경", "Deflection risk 높음"]),
    armOptionCard("B", "Section reinforcement", "길이 증가 + Ribbed Box 단면", ["Motor 유지 가능성", "질량·가공 trade-off"], true),
    armOptionCard("C", "Joint motor upgrade", "길이 증가 + Joint 2 Motor 변경", ["Torque margin 최대", "BOM·제어 변경 확대"]),
  );
  card.append(
    metrics,
    element("h5", "evidence-title", "WHY · EVIDENCE"),
    evidenceStack([
      ["FACT", "Robot Arm CAD · upper_arm_rev4.step"],
      ["FACT", "Joint 2 motor datasheet · peak 54 Nm"],
      ["CALCULATED", "+100 mm static torque estimate · +18%"],
      ["INFERRED", "Option B minimizes affected systems · HIGH confidence"],
    ]),
    options,
  );
  return card;
}

function proposalCard(revision = 1, interactive = true) {
  const revised = revision === 2;
  const card = element("section", "proposal-card");
  card.append(engineeringObjectHeader("DESIGN PROPOSAL", revised ? "PROPOSAL V2" : "PROPOSAL V1", "PROPOSED"));
  const table = element("div", "proposal-parameters");
  for (const [label, current, proposed] of revised ? [
    ["Length", "420 mm", "520 mm"],
    ["Material", "Al 6061", "Al 6061"],
    ["Cross section", "Solid", "Ribbed Box"],
    ["Width", "42 mm", "45 mm"],
    ["Rib height", "14 mm", "14 mm"],
    ["Mass target", "1.80 kg", "≤ 1.89 kg"],
  ] : [
    ["Length", "420 mm", "520 mm"],
    ["Material", "Al 6061", "Al 6061"],
    ["Cross section", "Solid", "Ribbed Box"],
    ["Mass increase", "—", `≤ ${guidedState.massLimit}%`],
    ["Deflection target", "—", "≤ 3.0 mm"],
    ["Joint 2 Motor", "Existing", "Existing"],
  ]) {
    const row = element("div", "proposal-parameter");
    row.append(element("span", "", label), element("code", "", current), element("strong", "", proposed));
    table.append(row);
  }
  const persistenceBoundary = guidedState.ledger.proposalHash
    ? "PROPOSED는 아직 simulation input이 아닙니다. Confirm & Simulate를 선택하면 candidate와 simulation 결과를 연결된 project ledger에 저장합니다."
    : "PROPOSED는 아직 simulation input이 아닙니다. 연결된 project ledger가 없으므로 Confirm & Simulate 결과도 browser-local demo로만 유지됩니다.";
  card.append(table, boundaryNote(persistenceBoundary));
  if (interactive) {
    const actions = guidedActions(
      guidedButton("Discuss", "ask-why-motor", "secondary-button"),
      guidedButton("Modify", revised ? "edit-revision" : "edit-constraint", "secondary-button"),
      guidedButton(revised ? "Confirm Revision 2 & Re-simulate" : "Confirm & Simulate", revised ? "confirm-revision-two" : "confirm-and-simulate", "guided-primary"),
    );
    card.append(actions);
  }
  return card;
}

function candidateSnapshot(revision = 1) {
  const revised = revision === 2;
  const snapshot = element("section", "candidate-snapshot");
  snapshot.append(engineeringObjectHeader("DESIGN CANDIDATE", revised ? "DC-015" : "DC-014", "CONFIRMED"));
  snapshot.append(facts([
    ["Component", "Upper Arm"],
    ["Length", "420 → 520 mm"],
    ["Material", "Al 6061"],
    ["Cross section", "Solid → Ribbed Box"],
    ["Width", revised ? "42 → 45 mm" : "42 mm"],
    ["Rib height", "14 mm"],
    ["Mass target", "≤ 1.89 kg"],
    ["Deflection requirement", "≤ 3.0 mm"],
    ["Payload", "5 kg"],
    ["Top speed", "Unchanged"],
    ["Joint 2 Motor", "Existing Motor"],
  ]));
  snapshot.append(element("p", "snapshot-lock", "🔒 Frozen simulation input · later conversation cannot mutate this candidate"));
  return snapshot;
}

function structuralSimulationCard(revision = 1) {
  const passed = revision === 2;
  const card = element("section", `formal-simulation-card ${passed ? "passed" : "failed"}`);
  card.append(
    engineeringObjectHeader("CAE RESULT · DEMO FIXTURE", passed ? "SIM-024" : "SIM-023", "SIMULATED"),
    boundaryNote("브라우저에 고정된 재현용 결과입니다. 실제 CAE solver 실행이나 물리 장치 측정값이 아닙니다."),
  );
  const results = element("div", "simulation-result-list");
  for (const [label, actual, requirement, result] of passed ? [
    ["Maximum Stress", "181 MPa", "≤ 210 MPa", "PASS"],
    ["Maximum Deflection", "2.8 mm", "≤ 3.0 mm", "PASS"],
    ["Joint 2 Peak Torque", "48 Nm", "≤ 54 Nm · 11% margin", "PASS"],
    ["Estimated Mass", "1.88 kg", "≤ 1.89 kg", "PASS"],
  ] : [
    ["Maximum Stress", "173 MPa", "≤ 210 MPa", "PASS"],
    ["Maximum Deflection", "4.1 mm", "≤ 3.0 mm", "FAIL"],
    ["Joint 2 Peak Torque", "47 Nm", "≤ 54 Nm · 13% margin", "PASS"],
    ["Estimated Mass", "1.86 kg", "≤ 1.89 kg", "PASS"],
  ]) {
    const row = element("div", `simulation-result-row ${result.toLowerCase()}`);
    row.append(element("strong", "", label), element("code", "", actual), element("span", "", requirement), element("b", "", result));
    results.append(row);
  }
  card.append(
    results,
    element("h5", "evidence-title", "RESULT PROVENANCE"),
    evidenceStack([
      ["SIMULATED", `${passed ? "SIM-024" : "SIM-023"} · structural solver output`],
      ["FACT", "Allowables from material and motor datasheets"],
      ["CALCULATED", `Torque margin · ${passed ? "11" : "13"}%`],
    ]),
  );
  return card;
}

function livePlanCard() {
  const card = element("section", "live-plan-card");
  card.append(engineeringObjectHeader("LIVE PLAN", "LP-014", phaseReached("accepted") ? "ACCEPTED" : "ACTIVE"));
  const items = element("div", "live-plan-grid");
  for (const [label, value, state] of [
    ["Goal", "Upper Arm +100 mm", "active"],
    ["Constraints", `Payload 5 kg · Mass +${guidedState.massLimit}% · Speed 유지`, "active"],
    ["Recommendation", phaseReached("revised") ? "Width 42 → 45 mm" : "Ribbed Box · existing J2 motor", phaseReached("proposed") ? "active" : "pending"],
    ["Affected", "Structure · Joint 2 load · manufacturing", phaseReached("proposed") ? "active" : "pending"],
    ["Required simulation", "Structural CAE", phaseReached("simulated") ? "complete" : "pending"],
    ["Required verification", "Thermal + physical duty-cycle", guidedState.verificationPlanOpen ? "planned" : "missing"],
  ]) {
    const item = element("div", `live-plan-item ${state}`);
    item.append(element("span", "", label), element("strong", "", value));
    items.append(item);
  }
  card.append(items);
  return card;
}

function toolBoundaryStrip() {
  const strip = element("section", "tool-boundary-strip");
  strip.append(
    element("strong", "", "LLM / TOOL BOUNDARY"),
    element("span", "", "FORGE: 대화·비교·추천"),
    element("span", "", "Tool: 계산·simulation·증거"),
    element("span", "", "MVP: deterministic local fixture"),
  );
  return strip;
}

function revisionComparisonCard() {
  const card = element("section", "revision-comparison-card");
  card.append(engineeringObjectHeader("REVISION COMPARISON", "DC-014 → DC-015", "REVISED"));
  const grid = element("div", "revision-comparison-grid");
  for (const [label, first, second, change] of [
    ["Width", "42 mm", "45 mm", "+3 mm"],
    ["Rib height", "14 mm", "14 mm", "unchanged"],
    ["Deflection", "4.1 mm · FAIL", "2.8 mm · PASS", "−1.3 mm"],
    ["Mass", "1.86 kg", "1.88 kg", "+0.02 kg"],
  ]) {
    const row = element("div", "revision-comparison-row");
    row.append(element("strong", "", label), element("code", "", first), element("code", "", second), element("span", "", change));
    grid.append(row);
  }
  card.append(grid);
  return card;
}

function resultRecommendationCard() {
  const card = element("section", "recommendation-card result-recommendation");
  card.append(
    engineeringObjectHeader("FORGE RECOMMENDATION", "REC-015", "INFERRED"),
    element("h4", "", "Arm Width 42 → 45 mm"),
    element("p", "recommendation-why", "Deflection failure는 중앙부 굽힘 강성 부족이 주원인입니다. Rib 높이와 Motor를 유지하면서 폭만 45 mm로 늘리는 것이 가장 작은 수정입니다."),
    evidenceStack([
      ["SIMULATED", "Maximum deflection 4.1 mm · requirement ≤ 3.0 mm"],
      ["SIMULATED", "Maximum stress 173 MPa · allowable 210 MPa"],
      ["CALCULATED", "Remaining mass budget · 30 g"],
      ["INFERRED", "Local width change is minimum-scope fix · HIGH confidence"],
    ]),
  );
  const impacts = element("div", "expected-impact-row");
  for (const [label, value] of [["Deflection", "↓"], ["Mass", "↑ slight"], ["Motor load", "↑ slight"], ["Manufacturing", "unchanged"]]) {
    impacts.append(guidedMetric(label, value));
  }
  card.append(impacts, guidedActions(
    guidedButton("Ask FORGE", "ask-why-motor", "secondary-button"),
    guidedButton("Modify Design", "modify-design", "guided-primary"),
    guidedButton("Run Additional Verification", "plan-additional-verification", "secondary-button"),
  ));
  return card;
}

function revisedAlternativesCard() {
  const card = element("section", "recommendation-card");
  card.append(
    engineeringObjectHeader("REVISION RECOMMENDATION", "REC-016", "INFERRED"),
    element("h4", "", "Rib 높이를 유지한다면 폭 45 mm를 추천"),
  );
  const options = element("div", "revision-alternatives");
  for (const [id, title, good, bad] of [
    ["A", "Rib 2 → 3개", "외형 변화 작음", "가공 복잡도 증가"],
    ["B", "Arm 폭 42 → 45 mm", "구조 단순 · 제조 용이", "외형 폭 +3 mm"],
  ]) {
    const option = element("article", id === "B" ? "recommended" : "");
    option.append(element("span", "option-kicker", `OPTION ${id}${id === "B" ? " · RECOMMENDED" : ""}`), element("h5", "", title), facts([["Advantage", good], ["Trade-off", bad]]));
    options.append(option);
  }
  card.append(options, evidenceStack([
    ["FACT", "Rib height constraint · 14 mm unchanged"],
    ["CALCULATED", "Estimated revised mass · 1.88 kg"],
    ["INFERRED", "Option B preserves manufacturing simplicity · MEDIUM confidence"],
  ]));
  return card;
}

function triangleVertices(triangle) {
  if (Array.isArray(triangle.vertices)) return triangle.vertices;
  return [triangle.a, triangle.b, triangle.c].filter(Boolean);
}

function vertexCoordinates(vertex) {
  if (Array.isArray(vertex)) return vertex.map(Number);
  return [Number(vertex.x), Number(vertex.y), Number(vertex.z)];
}

function mountSTLViewer(canvas, geometry) {
  const context = canvas.getContext("2d");
  if (!context) return;
  const triangles = geometry.triangles.slice(0, 5000).map((triangle) => triangleVertices(triangle).map(vertexCoordinates));
  const points = triangles.flat();
  if (!points.length || points.some((point) => point.length !== 3 || point.some((value) => !Number.isFinite(value)))) return;
  const center = [0, 1, 2].map((axis) => (Math.min(...points.map((point) => point[axis])) + Math.max(...points.map((point) => point[axis]))) / 2);
  const span = Math.max(...[0, 1, 2].map((axis) => Math.max(...points.map((point) => point[axis])) - Math.min(...points.map((point) => point[axis]))), 1e-9);

  function render() {
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.max(1, Math.floor(rect.width * ratio));
    canvas.height = Math.max(1, Math.floor(rect.height * ratio));
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, rect.width, rect.height);
    const { yaw, pitch } = guidedState.cadView;
    const cosY = Math.cos(yaw), sinY = Math.sin(yaw), cosP = Math.cos(pitch), sinP = Math.sin(pitch);
    const scale = Math.min(rect.width, rect.height) * 0.72 / span;
    const projected = triangles.map((triangle) => {
      const screen = triangle.map((point) => {
        const x = point[0] - center[0], y = point[1] - center[1], z = point[2] - center[2];
        const rx = x * cosY - z * sinY;
        const rz = x * sinY + z * cosY;
        const ry = y * cosP - rz * sinP;
        const depth = y * sinP + rz * cosP;
        return [rect.width / 2 + rx * scale, rect.height / 2 - ry * scale, depth];
      });
      return { screen, depth: screen.reduce((sum, point) => sum + point[2], 0) / 3 };
    }).sort((left, right) => left.depth - right.depth);
    for (const [index, triangle] of projected.entries()) {
      const [first, second, third] = triangle.screen;
      const shade = 38 + Math.round((index / Math.max(projected.length, 1)) * 24);
      context.beginPath();
      context.moveTo(first[0], first[1]);
      context.lineTo(second[0], second[1]);
      context.lineTo(third[0], third[1]);
      context.closePath();
      context.fillStyle = `hsl(195 72% ${shade}%)`;
      context.fill();
      context.strokeStyle = "rgba(142, 225, 255, 0.24)";
      context.stroke();
    }
  }

  let drag = null;
  canvas.addEventListener("pointerdown", (event) => {
    drag = { x: event.clientX, y: event.clientY };
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!drag) return;
    guidedState.cadView.yaw += (event.clientX - drag.x) * 0.01;
    guidedState.cadView.pitch = Math.max(-1.35, Math.min(1.35, guidedState.cadView.pitch + (event.clientY - drag.y) * 0.01));
    drag = { x: event.clientX, y: event.clientY };
    render();
  });
  canvas.addEventListener("pointerup", () => { drag = null; });
  canvas.addEventListener("pointercancel", () => { drag = null; });
  render();
}

function armEngineeringView() {
  const pane = element("section", "arm-engineering-view");
  const mode = element("div", "engineering-view-tabs");
  for (const [value, label] of [["current", "Current"], ["proposed", "Proposed"], ["difference", "Difference"]]) {
    mode.append(guidedButton(label, "set-arm-view", guidedState.armView === value ? "selected-option" : "secondary-button", value));
  }
  const canvas = element("section", `arm-cad-canvas ${guidedState.armView}`);
  const canvasHeader = element("header");
  canvasHeader.append(element("strong", "", "ROBOT ARM · UPPER ARM"), element("span", "", phaseReached("simulated") ? (phaseReached("resimulated") ? "SIM-024" : "SIM-023") : "CAD CONTEXT · READ ONLY"));
  const geometry = guidedState.cadGeometry;
  const length = guidedState.armView === "current" ? "420 mm" : "520 mm";
  if (geometry) {
    const geometryCanvas = document.createElement("canvas");
    geometryCanvas.className = "stl-geometry-canvas";
    geometryCanvas.setAttribute("aria-label", `${geometry.asset_id} STL geometry viewer`);
    const hint = element("small", "stl-viewer-hint", "드래그해 회전 · 실제 STL 형상 · 읽기 전용");
    canvas.append(canvasHeader, geometryCanvas, hint);
    requestAnimationFrame(() => mountSTLViewer(geometryCanvas, geometry));
  } else {
    const schematic = element("div", "arm-schematic");
    schematic.append(element("span", "joint base-joint", "J2"), element("div", "upper-arm-member", ""), element("span", "joint wrist-joint", "J3"));
    schematic.append(element("div", "arm-dimension", `↔ ${length}`));
    if (guidedState.armView === "difference") schematic.append(element("div", "arm-extension", "+100 mm"));
    if (phaseReached("simulated")) {
      const deflection = element("div", phaseReached("resimulated") ? "deflection-curve pass" : "deflection-curve fail");
      deflection.append(element("span", "", phaseReached("resimulated") ? "2.8 mm" : "4.1 mm"));
      schematic.append(deflection);
    }
    canvas.append(canvasHeader, schematic);
  }
  const properties = element("div", "arm-property-grid");
  const width = phaseReached("revised") ? "45 mm" : "42 mm";
  for (const [label, value, evidence] of [
    ["Geometry", geometry ? `${geometry.triangle_count} triangles` : length, "FACT"],
    ["Material", "Al 6061", "FACT"],
    ["Width", width, phaseReached("revised") ? "PROPOSED" : "FACT"],
    ["Payload", "5 kg", "FACT"],
    ["Motor peak", "54 Nm", "FACT"],
    ["Mass target", "≤ 1.89 kg", "REQUIREMENT"],
  ]) {
    const property = element("div", "arm-property");
    property.append(element("span", "", label), element("strong", "", value), element("small", "", evidence));
    properties.append(property);
  }
  pane.append(mode, canvas, properties, livePlanCard(), toolBoundaryStrip());
  if (phaseReached("simulated")) {
    pane.append(structuralSimulationCard(phaseReached("resimulated") ? 2 : 1));
    if (phaseReached("resimulated")) pane.append(revisionComparisonCard());
  } else if (phaseReached("selected")) pane.append(proposalCard(1, false));
  else pane.append(evidenceStack([
    ["FACT", "CAD · upper_arm_rev4.step"],
    ["FACT", "BOM · robot_arm_revC"],
    ["FACT", "Requirement · payload 5 kg"],
    ["FACT", "Motor datasheet · J2-M54"],
  ]));
  return pane;
}

function sessionStatusBar() {
  const status = element("footer", "session-status-bar");
  for (const [label, value, state] of [
    ["Goal", "Arm +100 mm", "active"],
    ["Proposal", phaseReached("revised") ? "v2" : phaseReached("proposed") ? "v1" : "Draft", phaseReached("proposed") ? "active" : "pending"],
    ["Candidate", phaseReached("resimulated") ? "DC-015" : phaseReached("simulated") ? "DC-014" : "Not confirmed", phaseReached("simulated") ? "active" : "pending"],
    ["Simulation", phaseReached("resimulated") ? "SIM-024 · PASS" : phaseReached("simulated") ? "SIM-023 · 1 FAIL" : "Not run", phaseReached("resimulated") ? "success" : phaseReached("simulated") ? "blocked" : "pending"],
    ["Verify", guidedState.verificationPlanOpen ? "Thermal planned" : "Pending", "pending"],
    [
      "Ledger",
      guidedState.ledger.status === "synced"
        ? `v${guidedState.ledger.projectVersion} · persisted`
        : guidedState.ledger.status === "saving"
          ? "Saving…"
          : guidedState.ledger.status === "error"
            ? "Sync blocked"
            : "Browser-local",
      guidedState.ledger.status === "synced" ? "success" : guidedState.ledger.status === "error" ? "blocked" : "pending",
    ],
  ]) {
    const item = element("div", `session-status-item ${state}`);
    item.append(element("span", "", label), element("strong", "", value));
    status.append(item);
  }
  return status;
}

function whyMotorMessage() {
  return conversationMessage(
    "forge",
    "FORGE · WHY?",
    "Joint 2 Peak Torque가 47–48 Nm이고 datasheet 허용 Peak Torque가 54 Nm라 11–13% margin이 남습니다. 그래서 구조 결과만 보면 Motor 교체가 필수는 아닙니다. 다만 Continuous Duty Thermal Margin은 아직 검증되지 않아 Thermal Simulation 또는 실제 Duty Cycle Test가 필요합니다.",
    evidenceStack([
      ["SIMULATED", "Joint 2 peak torque · 47–48 Nm"],
      ["FACT", "Motor allowable peak torque · 54 Nm"],
      ["INFERRED", "Motor replacement not required for peak torque · HIGH confidence"],
      ["UNKNOWN", "Continuous-duty thermal margin · verification required"],
    ]),
  );
}

function renderEngineeringSession() {
  const screen = element("section", `collaboration-workspace${guidedState.workspaceOpen ? " workspace-open" : ""}`);
  if (IS_FILE_PROTOCOL) {
    const notice = element("section", "load-status error file-mode-notice");
    notice.setAttribute("role", "status");
    notice.append(
      element("strong", "", "로컬 파일 미리보기 모드"),
      element("span", "", "스타일과 대화형 데모는 사용할 수 있습니다. Project ledger와 검증 API는 ./forge dashboard가 출력한 loopback URL에서 연결됩니다."),
    );
    screen.append(notice);
  }
  const engineeringPane = element("section", "engineering-pane");
  engineeringPane.append(guidedHeader("ENGINEERING VIEW", "Upper Arm", "CAD context, candidate snapshot and solver results share one evidence-bound view."), armEngineeringView());
  const conversationPane = element("section", "forge-conversation-pane");
  const conversationHeader = guidedHeader(
    "FORGE",
    guidedState.collaborationPhase === "idea"
      ? "Agent operator console"
      : "Upper Arm change",
    "LLM host가 대화를 담당하고 이 화면은 도구·증거 상태를 보여줍니다.",
  );
  conversationHeader.append(workspaceToggleButton());
  conversationPane.append(conversationHeader);
  const thread = element("section", "conversation-thread");
  if (guidedState.collaborationPhase === "idea") {
    thread.append(conversationWelcome());
    conversationPane.append(thread, engineeringComposer({
      placeholder: "변경하고 싶은 부분이나 검증할 상황을 입력하세요",
      action: "send-arm-intent",
      label: "보내기",
      value: "",
      attachments: true,
      shortcuts: [["로봇 팔 +10cm", "로봇 팔 길이를 10cm 늘리고 싶어."], ["payload 20kg", "payload를 20kg으로 늘렸을 때 영향 분석해줘."], ["열 조건 검토", "40C 환경에서 thermal margin을 시뮬레이션하고 싶어."]],
    }));
  } else {
    thread.append(conversationMessage("user", "YOU", guidedState.changeRequest));
    const ragDraft = guidedState.ragAnswer;
    const responseEvidence = ragDraft
      ? evidenceStack(
          ragDraft.claims?.length
            ? ragDraft.claims.map((claim) => ["CITED", `${claim.statement} · ${claim.context_refs.join(", ")}`])
            : (ragDraft.questions || ragDraft.unknowns || []).map((item) => ["QUESTION", item]),
        )
      : initialRecommendationCard();
    thread.append(conversationMessage(
      "forge",
      "FORGE",
      ragDraft?.message || "가능합니다. 다만 팔 길이를 100 mm 늘리면 Joint 2 torque와 끝단 deflection이 증가합니다. 현재 5 kg payload와 최고속도를 유지한다면 B안부터 검토하는 것을 추천합니다.",
      responseEvidence,
    ));
    if (!phaseReached("selected")) {
      conversationPane.append(thread);
    } else {
      thread.append(conversationMessage("user", "YOU", "B안 괜찮은데 무게가 너무 늘면 안 돼."));
      thread.append(conversationMessage(
        "forge",
        "FORGE",
        `기존 Al 6061을 유지하고 Solid를 Ribbed Box로 바꾸면 Motor 교체 없이 질량 증가 ${guidedState.massLimit}% 이내와 deflection ≤3 mm를 함께 목표로 잡을 수 있습니다.`,
        proposalCard(1, !phaseReached("simulated")),
      ));
      if (guidedState.massLimit === 5) {
        thread.append(conversationMessage("user", "YOU", "무게 증가는 5%로 제한하자."));
        thread.append(conversationMessage(
          "forge",
          "FORGE",
          "Mass constraint를 +5%로 갱신했습니다. 이 변경은 Live Plan과 Proposal v1에 반영되며, 확정 전까지 simulation input은 만들지 않습니다.",
          evidenceStack([
            ["FACT", "User constraint · mass increase ≤ 5%"],
            ["INFERRED", "Ribbed Box remains feasible · MEDIUM confidence"],
          ]),
        ));
      }
      if (guidedState.editingConstraint && !phaseReached("simulated")) {
        conversationPane.append(thread, engineeringComposer({
          placeholder: "제약을 수정하세요…",
          action: "apply-mass-limit",
          label: "Constraint 적용",
          value: "무게 증가는 5%로 제한하자.",
          shortcuts: [["Mass 5%", "무게 증가는 5%로 제한"], ["Deflection 3mm", "처짐은 3mm 이하"], ["Motor 유지", "Joint 2 Motor 유지"]],
        }));
      } else if (!phaseReached("simulated")) {
        conversationPane.append(thread);
      } else {
        thread.append(conversationMessage("user", "YOU", "이 설계를 확정하고 Simulation 해봐."));
        thread.append(conversationMessage("forge", "FORGE", "Design Candidate DC-014를 freeze하고 Structural Simulation SIM-023을 실행했습니다.", candidateSnapshot(1), structuralSimulationCard(1)));
        thread.append(conversationMessage(
          "forge",
          "FORGE",
          "질량과 stress, torque는 기준을 만족했지만 deflection이 4.1 mm로 요구사항을 1.1 mm 초과했습니다. Motor나 material보다 국부 구조 수정이 변경 범위가 작습니다.",
          resultRecommendationCard(),
        ));
        if (guidedState.whyMotorOpen) thread.append(whyMotorMessage());
        if (phaseReached("revised")) {
          thread.append(conversationMessage("user", "YOU", "Rib 높이는 늘리고 싶지 않아. Arm 폭은 45 mm까지만 늘려봐."));
          thread.append(conversationMessage(
            "forge",
            "FORGE",
            "가능합니다. Rib 개수 증가와 폭 증가를 비교하면 제조성을 유지하는 B안이 유리합니다. Revised Design v2는 Width 42 → 45 mm, Rib height 14 mm 유지이며 예상 질량 1.88 kg입니다.",
            revisedAlternativesCard(),
            proposalCard(2, !phaseReached("resimulated")),
          ));
        }
        if (phaseReached("resimulated")) {
          thread.append(conversationMessage("user", "YOU", "Revision 2로 확정하고 다시 Simulation 해."));
          thread.append(conversationMessage(
            "forge",
            "FORGE",
            "DC-015를 별도 snapshot으로 freeze해 SIM-024를 실행했습니다. Deflection 2.8 mm, stress 181 MPa, mass 1.88 kg로 현재 Structural 요구사항을 모두 만족합니다.",
            candidateSnapshot(2),
            structuralSimulationCard(2),
            guidedActions(
              guidedButton("Ask FORGE", "ask-why-motor", "secondary-button"),
              guidedButton("Modify Design", "modify-design", "secondary-button"),
              guidedButton("Accept Result", "accept-result", "guided-primary"),
              guidedButton("Run Additional Verification", "plan-additional-verification", "secondary-button"),
            ),
          ));
        }
        if (phaseReached("accepted")) {
          const verification = artifactCanvas("Next verification", "Accepted structural result does not imply release readiness.", facts([
            ["Structural CAE", "SIM-024 · PASS · accepted"],
            ["Thermal simulation", "REQUIRED · not run"],
            ["Joint 2 duty-cycle test", "MEASURED · REQUIRED · not imported"],
            ["Release readiness", "PENDING"],
          ]));
          thread.append(conversationMessage(
            "forge",
            "FORGE",
            "Structural 결과는 승인됐지만 Thermal Verification과 실제 Duty Cycle Test가 없습니다. 최종 설계와 release를 확정하기 전에 두 증거를 추가하는 것을 추천합니다.",
            verification,
          ));
        }
        conversationPane.append(thread);
      }
    }
  }
  if (guidedState.workspaceOpen) screen.append(engineeringPane);
  screen.append(conversationPane);
  if (guidedState.collaborationPhase !== "idea") screen.append(sessionStatusBar());
  return screen;
}

function renderGuidedWorkflow() {
  const content = byId("guided-workflow-content");
  if (!content) return;
  const stage = guidedState.step === "resolve" ? "verify" : guidedState.step === "complete" ? "release" : guidedState.step;
  setWorkflowStage(stage);
  byId("guided-workflow").dataset.flowStage = stage;
  const revision = phaseReached("resimulated") ? "REVISION 2" : phaseReached("simulated") ? "REVISION 1" : phaseReached("proposed") ? "PROPOSAL" : "IDEA";
  byId("guided-revision").textContent = revision;
  byId("guided-session-id").textContent = phaseReached("resimulated") ? "DC-015" : phaseReached("simulated") ? "DC-014" : "SESSION-014";
  setWorkspaceValue("workspace-project-name", "Robot Arm Project");
  setWorkspaceValue("workspace-project-id", "robot-arm-project");
  setWorkspaceValue("workspace-change-id", "EC-014");
  replace(content, renderEngineeringSession());
}

function resetGuidedDemo() {
  Object.assign(guidedState, {
    step: "project",
    projectConnected: false,
    connectionMode: "",
    assetName: "",
    changeRequest: "Delivery Robot V3의 payload를 10kg에서 20kg으로 높이고 싶어. 최고 속도 3m/s는 유지하고 BOM 증가는 20% 이내로 제한해.",
    planAnalyzed: false,
    selectedAlternative: "",
    inspectedNode: "Drive system",
    planSimulationRun: false,
    planSimulationRefined: false,
    planRefinementOpen: false,
    designView: "difference",
    refinementMessage: "냉각 덕트와 thermal derating을 추가하되 최고 속도 3m/s와 BOM 증가 20% 이내 조건을 유지해.",
    planApproved: false,
    implementationComplete: false,
    verificationRun: false,
    evidenceOpen: false,
    resolved: false,
    selectedFix: "cooling",
    releaseApproved: false,
    collaborationPhase: "idea",
    selectedArmOption: "",
    armView: "difference",
    massLimit: 8,
    editingConstraint: false,
    whyMotorOpen: false,
    verificationPlanOpen: false,
    workspaceOpen: false,
    pendingKnowledge: null,
    ragAnswer: null,
    ledger: {
      projectId: "",
      proposalHash: "",
      projectVersion: 0,
      sessionId: "",
      sequence: 0,
      state: "idea",
      candidateHash: "",
      candidateRevision: 0,
      simulationHash: "",
      simulationRevision: 0,
      claimRevision: 0,
      status: "local",
    },
  });
  replace(byId("guided-history-list"), (() => {
    const item = element("li");
    item.append(element("span", "", "Idea"), element("strong", "", "SESSION-014"));
    return item;
  })());
  renderGuidedWorkflow();
}

function appendGuidedHistory(label, value) {
  const item = element("li");
  item.append(element("span", "", label), element("strong", "", value));
  byId("guided-history-list").append(item);
}

function beginGuidedConnection(mode, assetName) {
  const content = byId("guided-workflow-content");
  guidedState.connectionMode = mode;
  guidedState.assetName = assetName;
  content.setAttribute("aria-busy", "true");
  replace(content, conversationMessage(
    "forge",
    "FORGE",
    `${assetName}의 revision, BOM, firmware, protocol, test 관계를 browser-local demo graph로 구성하고 있습니다…`,
  ));
  window.setTimeout(() => {
    guidedState.projectConnected = true;
    content.removeAttribute("aria-busy");
    appendGuidedHistory("Connected", assetName);
    renderGuidedWorkflow();
  }, reducedMotion.matches ? 20 : 520);
}

async function persistGuidedMutation(path, payload) {
  const ledger = guidedState.ledger;
  if (!ledger.projectId || !ledger.proposalHash || ledger.projectVersion < 1) return null;
  ledger.status = "saving";
  renderGuidedWorkflow();
  const data = await postMutation(
    ledger.projectId,
    `/api/v1/projects/${ledger.projectId}/${path}`,
    payload,
    ledger.projectVersion,
  );
  ledger.projectVersion = Number(data.project_version);
  ledger.status = "synced";
  return data;
}

async function ensureChatProject() {
  const ledger = guidedState.ledger;
  if (ledger.projectId && ledger.projectVersion > 0) return ledger;
  const projectId = `chat-${Date.now()}`;
  const created = await postProjectMutation({ project_id: projectId, name: "FORGE Chat Project" });
  ledger.projectId = projectId;
  ledger.projectVersion = Number(created.project_version);
  ledger.sessionId = `session-${Date.now()}`;
  ledger.status = "synced";
  return ledger;
}

async function ingestPendingKnowledge() {
  const pending = guidedState.pendingKnowledge;
  if (!pending || IS_FILE_PROTOCOL) return;
  const ledger = await ensureChatProject();
  const result = await postMutation(
    ledger.projectId,
    `/api/v1/projects/${ledger.projectId}/knowledge-sources`,
    {
      source_id: pending.sourceId,
      source_kind: "document",
      source_uri: `upload://${pending.name}`,
      source_version: "uploaded-1",
      captured_at: new Date().toISOString(),
      text: pending.text,
    },
    ledger.projectVersion,
  );
  ledger.projectVersion = Number(result.project_version);
  ledger.status = "synced";
  guidedState.pendingKnowledge = null;
  appendCollaborationMessage("FORGE", `${pending.name}을 project-scoped cited context로 저장했습니다.`);
}

async function askLocalRag(message) {
  if (IS_FILE_PROTOCOL) return null;
  const ledger = await ensureChatProject();
  await ingestPendingKnowledge();
  const result = await postMutation(
    ledger.projectId,
    `/api/v1/projects/${ledger.projectId}/conversations`,
    {
      request_id: `request-${Date.now()}`,
      user_message: message,
      requested_at: new Date().toISOString(),
    },
    ledger.projectVersion,
  );
  ledger.projectVersion = Number(result.project_version);
  ledger.status = "synced";
  return result.conversation.runtime.draft;
}

function bytesToBase64(bytes) {
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 32768) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 32768));
  }
  return btoa(binary);
}

async function ingestSTLFile(file) {
  if (IS_FILE_PROTOCOL) throw new Error("서버 대시보드에서 STL을 연결하세요");
  if (file.size > 730000) throw new Error("STL 파일은 730 KB 이하여야 합니다");
  const ledger = await ensureChatProject();
  const safeName = file.name.replace(/[^A-Za-z0-9._-]/g, "-");
  const result = await postMutation(
    ledger.projectId,
    `/api/v1/projects/${ledger.projectId}/cad-geometries`,
    {
      asset_id: `stl-${Date.now()}`,
      source_uri: `upload://${safeName}`,
      source_version: "uploaded-1",
      captured_at: new Date().toISOString(),
      content_base64: bytesToBase64(new Uint8Array(await file.arrayBuffer())),
    },
    ledger.projectVersion,
  );
  ledger.projectVersion = Number(result.project_version);
  ledger.status = "synced";
  guidedState.cadGeometry = result.cad_geometry.asset;
  appendCollaborationMessage(
    "FORGE",
    `${file.name} 형상을 파싱했습니다 · ${guidedState.cadGeometry.triangle_count} triangles · ${guidedState.cadGeometry.format}`,
  );
}

async function persistGuidedTransition(toState, { candidateHash = null, simulationHash = null, evidenceRefs = [] } = {}) {
  const ledger = guidedState.ledger;
  const data = await persistGuidedMutation("design-transitions", {
    session_id: ledger.sessionId,
    sequence: ledger.sequence + 1,
    from_state: ledger.state,
    to_state: toState,
    candidate_hash: candidateHash,
    simulation_hash: simulationHash,
    evidence_refs: evidenceRefs,
  });
  if (data) {
    ledger.sequence += 1;
    ledger.state = toState;
  }
  return data;
}

async function persistGuidedCandidateAndSimulation(revision) {
  const ledger = guidedState.ledger;
  if (!ledger.projectId || !ledger.proposalHash) return false;
  try {
    if (ledger.state === "idea") await persistGuidedTransition("proposed");
    if (ledger.state === "proposed") await persistGuidedTransition("selected");
    if (revision === 2 && ledger.state === "simulated") {
      await persistGuidedTransition("revised", { simulationHash: ledger.simulationHash });
    }
    const candidateId = revision === 1 ? "DC-014" : "DC-015";
    if (ledger.candidateRevision !== revision) {
      const approvalId = `candidate-approval-${Date.now()}`;
      const approvalNonce = `${crypto.randomUUID()}${crypto.randomUUID()}`;
      const candidatePayload = {
        session_id: ledger.sessionId,
        candidate_id: candidateId,
        revision,
        proposal_hash: ledger.proposalHash,
        parameters: [
          { name: "arm_length", current_value: "420 mm", proposed_value: "520 mm", source_refs: [`proposal:${ledger.proposalHash}`] },
          { name: "arm_width", current_value: "42 mm", proposed_value: revision === 1 ? "42 mm" : "45 mm", source_refs: [`proposal:${ledger.proposalHash}`] },
          { name: "mass_limit", current_value: "baseline", proposed_value: `+${guidedState.massLimit}%`, source_refs: ["user:mass-constraint"] },
        ],
        requirements: ["maximum deflection <= 3 mm", "payload 5 kg", "preserve top speed"],
      };
      await persistGuidedMutation("design-candidate-approvals", {
        approval_id: approvalId,
        approval_nonce: approvalNonce,
        ...candidatePayload,
      });
      const candidateData = await persistGuidedMutation("design-candidates", {
        approval_id: approvalId,
        approval_nonce: approvalNonce,
        ...candidatePayload,
        confirmed_by: "local-operator",
      });
      ledger.candidateHash = candidateData.design_candidate.candidate_hash;
      ledger.candidateRevision = revision;
    }
    if (ledger.state === "selected" || ledger.state === "revised") {
      await persistGuidedTransition("confirmed", { candidateHash: ledger.candidateHash });
    }
    const simulationId = revision === 1 ? "SIM-023" : "SIM-024";
    if (ledger.simulationRevision !== revision) {
      const simulationData = await persistGuidedMutation("design-simulations", {
        session_id: ledger.sessionId,
        simulation_id: simulationId,
        candidate_hash: ledger.candidateHash,
        tool_ref: { kind: "user", identifier: "guided-browser-fixture" },
        metrics: revision === 1
          ? [
              { metric: "maximum_deflection", actual: "4.1 mm", requirement: "<= 3 mm", verdict: "FAIL" },
              { metric: "peak_stress", actual: "176 MPa", requirement: "<= 190 MPa", verdict: "PASS" },
            ]
          : [
              { metric: "maximum_deflection", actual: "2.8 mm", requirement: "<= 3 mm", verdict: "PASS" },
              { metric: "peak_stress", actual: "181 MPa", requirement: "<= 190 MPa", verdict: "PASS" },
            ],
      });
      ledger.simulationHash = simulationData.simulation_binding.simulation_hash;
      ledger.simulationRevision = revision;
    }
    if (ledger.claimRevision !== revision) {
      await persistGuidedMutation("conversational-claims", {
        session_id: ledger.sessionId,
        claim_id: revision === 1 ? "claim-deflection-fail" : "claim-structural-pass",
        kind: "simulated",
        statement: revision === 1
          ? "Maximum deflection exceeds the confirmed requirement."
          : "The revised candidate satisfies the imported structural thresholds.",
        evidence_refs: [`simulation:${ledger.simulationHash}`],
      });
      ledger.claimRevision = revision;
    }
    if (ledger.state === "confirmed") {
      await persistGuidedTransition("simulated", {
        candidateHash: ledger.candidateHash,
        simulationHash: ledger.simulationHash,
      });
    }
    appendCollaborationMessage("FORGE", `${candidateId}와 ${simulationId}을 project v${ledger.projectVersion} immutable ledger에 저장했습니다.`);
    return true;
  } catch (error) {
    ledger.status = "error";
    const code = error instanceof Error ? error.message : "unknown_error";
    appendCollaborationMessage("FORGE", `Ledger 저장이 중단됐습니다 (${code}). 브라우저 데모 상태는 승인 기록으로 승격하지 않았습니다.`);
    renderGuidedWorkflow();
    return false;
  }
}

async function handleGuidedAction(action, value) {
  if (action === "send-arm-intent") {
    const message = byId("engineering-chat-input")?.value.trim();
    guidedState.changeRequest = message || "로봇 팔 길이를 10cm 늘리고 싶어.";
    try {
      guidedState.ragAnswer = await askLocalRag(guidedState.changeRequest);
    } catch (error) {
      guidedState.ledger.status = "error";
      appendCollaborationMessage("FORGE", `Local RAG 요청이 중단됐습니다 (${error instanceof Error ? error.message : "unknown_error"}).`);
    }
    guidedState.collaborationPhase = "proposed";
    guidedState.step = "plan";
    guidedState.workspaceOpen = false;
    appendGuidedHistory("Intent", "Arm +100 mm");
    renderGuidedWorkflow();
    return;
  }
  if (action === "select-arm-option") {
    guidedState.selectedArmOption = value;
    guidedState.collaborationPhase = "selected";
    appendGuidedHistory("Selected", `Option ${value}`);
    renderGuidedWorkflow();
    return;
  }
  if (action === "set-arm-view") {
    guidedState.armView = value;
    renderGuidedWorkflow();
    return;
  }
  if (action === "open-workspace") {
    guidedState.workspaceOpen = true;
    renderGuidedWorkflow();
    return;
  }
  if (action === "close-workspace") {
    guidedState.workspaceOpen = false;
    renderGuidedWorkflow();
    return;
  }
  if (action === "edit-constraint") {
    guidedState.editingConstraint = true;
    renderGuidedWorkflow();
    return;
  }
  if (action === "apply-mass-limit") {
    guidedState.massLimit = 5;
    guidedState.editingConstraint = false;
    appendGuidedHistory("Constraint", "Mass ≤ +5%");
    renderGuidedWorkflow();
    return;
  }
  if (action === "confirm-and-simulate") {
    if (guidedState.ledger.proposalHash && !(await persistGuidedCandidateAndSimulation(1))) return;
    guidedState.collaborationPhase = "simulated";
    guidedState.step = "execute";
    appendGuidedHistory("Confirmed", "DC-014");
    appendGuidedHistory("Simulated", "SIM-023 · FAIL");
    renderGuidedWorkflow();
    return;
  }
  if (action === "ask-why-motor") {
    guidedState.whyMotorOpen = true;
    renderGuidedWorkflow();
    return;
  }
  if (action === "modify-design" || action === "edit-revision") {
    guidedState.collaborationPhase = "revised";
    guidedState.step = "plan";
    guidedState.whyMotorOpen = false;
    appendGuidedHistory("Revised", "Proposal v2");
    renderGuidedWorkflow();
    return;
  }
  if (action === "confirm-revision-two") {
    if (guidedState.ledger.proposalHash && !(await persistGuidedCandidateAndSimulation(2))) return;
    guidedState.collaborationPhase = "resimulated";
    guidedState.step = "execute";
    appendGuidedHistory("Confirmed", "DC-015");
    appendGuidedHistory("Re-simulated", "SIM-024 · PASS");
    renderGuidedWorkflow();
    return;
  }
  if (action === "accept-result") {
    if (guidedState.ledger.proposalHash) {
      try {
        await persistGuidedTransition("accepted", { simulationHash: guidedState.ledger.simulationHash });
      } catch (error) {
        guidedState.ledger.status = "error";
        const code = error instanceof Error ? error.message : "unknown_error";
        appendCollaborationMessage("FORGE", `결과 수락을 ledger에 기록하지 못했습니다 (${code}).`);
        renderGuidedWorkflow();
        return;
      }
    }
    guidedState.collaborationPhase = "accepted";
    guidedState.step = "verify";
    appendGuidedHistory("Accepted", "Structural result");
    renderGuidedWorkflow();
    return;
  }
  if (action === "plan-additional-verification") {
    guidedState.verificationPlanOpen = true;
    if (phaseReached("resimulated")) guidedState.collaborationPhase = "accepted";
    guidedState.step = "verify";
    appendGuidedHistory("Verify next", "Thermal + duty cycle");
    renderGuidedWorkflow();
    return;
  }
  if (action === "connect-project") {
    guidedState.workspaceOpen = true;
    beginGuidedConnection("project snapshot", "Delivery Robot V3");
    return;
  }
  if (action === "connect-machine") {
    guidedState.workspaceOpen = true;
    beginGuidedConnection("machine read-only", "Delivery Robot V3");
    return;
  }
  if (action === "upload-cad") {
    guidedState.workspaceOpen = true;
    byId("composer-cad-file")?.click();
    return;
  }
  if (action === "send-project-message") {
    const message = byId("engineering-chat-input")?.value.trim();
    if (message) guidedState.changeRequest = message;
    beginGuidedConnection("manual conversation baseline", "Unlinked engineering draft");
    return;
  }
  if (action === "send-change") {
    const message = byId("engineering-chat-input")?.value.trim();
    if (message) guidedState.changeRequest = message;
    guidedState.step = "plan";
    appendGuidedHistory("Change request", "Payload 20 kg");
  }
  if (action === "continue-plan") guidedState.step = "plan";
  if (action === "analyze-change") {
    guidedState.planAnalyzed = true;
    appendGuidedHistory("Analyzed", "7 impacts");
  }
  if (action === "inspect-node") guidedState.inspectedNode = value;
  if (action === "select-alternative") {
    if (guidedState.selectedAlternative !== value) {
      guidedState.planSimulationRun = false;
      guidedState.planSimulationRefined = false;
      guidedState.planRefinementOpen = false;
    }
    guidedState.selectedAlternative = value;
  }
  if (action === "view-design-mode") guidedState.designView = value;
  if (action === "run-plan-simulation") {
    guidedState.planSimulationRun = true;
    appendGuidedHistory("Simulated", "Run 1 · 92 °C risk");
  }
  if (action === "refine-simulation") {
    const message = byId("engineering-chat-input")?.value.trim();
    if (message) guidedState.refinementMessage = message;
    guidedState.planSimulationRefined = true;
    guidedState.planRefinementOpen = false;
    appendGuidedHistory("Refined", "Run 2 · 82 °C");
  }
  if (action === "open-refinement") guidedState.planRefinementOpen = true;
  if (action === "composer-suggestion") {
    const input = byId("engineering-chat-input");
    if (input) {
      input.value = input.value.trim() ? `${input.value.trim()} · ${value}` : value;
      input.focus();
    }
    return;
  }
  if (action === "modify-forge") {
    const panel = byId("forge-ai-panel");
    panel.classList.remove("collapsed");
    byId("forge-ai-toggle").setAttribute("aria-expanded", "true");
    byId("forge-ai-input").value = "Option A를 유지하되 thermal margin을 높이고 BOM 증가를 20% 이내로 유지해.";
    appendCollaborationMessage("FORGE", "Revision 1 계획을 유지하면서 thermal margin을 높일 수정 조건을 local PLAN draft에 준비했습니다.");
    byId("forge-ai-input").focus();
    return;
  }
  if (action === "approve-plan") {
    guidedState.planApproved = true;
    guidedState.step = "execute";
    appendGuidedHistory("Approved locally", "Revision 1");
  }
  if (action === "run-implementation") guidedState.implementationComplete = true;
  if (action === "continue-verify") {
    guidedState.step = "verify";
    guidedState.verificationRun = false;
  }
  if (action === "run-verification") {
    guidedState.verificationRun = true;
    appendGuidedHistory(guidedState.resolved ? "Re-verified" : "Verified", guidedState.resolved ? "10/10" : "8/10");
  }
  if (action === "toggle-evidence") guidedState.evidenceOpen = !guidedState.evidenceOpen;
  if (action === "resolve-blocker") guidedState.step = "resolve";
  if (action === "select-fix") guidedState.selectedFix = value;
  if (action === "create-revision") {
    guidedState.resolved = true;
    guidedState.step = "execute";
    guidedState.implementationComplete = false;
    guidedState.verificationRun = false;
    guidedState.evidenceOpen = false;
    appendGuidedHistory("Re-planned", "Revision 2");
  }
  if (action === "continue-release") guidedState.step = "release";
  if (action === "approve-release") {
    guidedState.releaseApproved = true;
    guidedState.step = "complete";
    appendGuidedHistory("Released locally", "Revision 2");
  }
  if (action === "open-advanced") {
    const details = byId("advanced-console");
    details.open = true;
    details.scrollIntoView({ behavior: reducedMotion.matches ? "auto" : "smooth", block: "start" });
    return;
  }
  if (action === "reset-demo") {
    resetGuidedDemo();
    return;
  }
  renderGuidedWorkflow();
}

function setLoadStatus(kind, title, copy) {
  const status = byId("load-status");
  status.className = `load-status ${kind}`.trim();
  replace(status, element("strong", "", title), element("span", "", copy));
}

function setPanelStatus(id, kind, title, copy) {
  const status = byId(id);
  status.className = `load-status ${kind}`.trim();
  replace(status, element("strong", "", title), element("span", "", copy));
}

function enterFileProtocolMode() {
  const serverCopy = "정식 API, connector snapshot, release decision 조회는 ./forge dashboard로 로컬 서버를 실행한 뒤 http://127.0.0.1:43127/app/에서 사용하세요.";
  setPanelStatus(
    "connect-status",
    "error",
    "서버 모드 필요",
    "이 화면은 file://에서도 대화형 데모를 보여주지만 project 생성과 connector 조회는 loopback API가 필요합니다.",
  );
  setPanelStatus(
    "preview-status",
    "error",
    "서버 모드 필요",
    "change preview, design alternatives, external evidence plan 저장은 loopback API에서만 실행됩니다.",
  );
  setPanelStatus(
    "verification-status",
    "error",
    "서버 모드 필요",
    "simulation, bench, HIL, 실제 장치 증거 검증은 저장된 project ledger가 있을 때만 수행됩니다.",
  );
  setPanelStatus("diagnosis-status", "error", "서버 모드 필요", "blocker diagnosis와 resolution PLAN은 release decision hash에 묶여야 합니다.");
  setLoadStatus("error", "로컬 파일 미리보기 모드", serverCopy);
  for (const id of [
    "create-project-button",
    "connect-button",
    "preview-button",
    "design-proposal-button",
    "verify-button",
    "load-button",
    "diagnosis-button",
    "sample-connect-button",
    "sample-preview-button",
  ]) {
    const button = byId(id);
    if (button) button.disabled = true;
  }
  appendCollaborationMessage(
    "FORGE",
    "파일로 직접 연 상태입니다. CAD 업로드/기계 연결 UX와 대화형 설계 시뮬레이션 데모는 볼 수 있고, source-bound 저장과 READY/BLOCKED 판정은 로컬 서버에서 실행합니다.",
  );
}

async function sessionToken() {
  const response = await fetch("/api/v1/session", {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    credentials: "same-origin",
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.error?.code || `http_${response.status}`);
  return payload.csrf_token;
}

async function postMutation(project, path, payload, expectedVersion) {
  const csrf = await sessionToken();
  const idempotency = `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const response = await fetch(path, {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "Idempotency-Key": idempotency,
      "If-Match": `"project:${project}:v${expectedVersion}"`,
      "X-FORGE-CSRF": csrf,
    },
    body: JSON.stringify(payload),
    cache: "no-store",
    credentials: "same-origin",
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data?.error?.code || `http_${response.status}`);
  return data;
}

async function postProjectMutation(payload) {
  const csrf = await sessionToken();
  const idempotency = `ui-project-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const response = await fetch("/api/v1/projects", {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      "Idempotency-Key": idempotency,
      "X-FORGE-CSRF": csrf,
    },
    body: JSON.stringify(payload),
    cache: "no-store",
    credentials: "same-origin",
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data?.error?.code || `http_${response.status}`);
  return data;
}

async function loadConnectors() {
  const response = await fetch("/api/v1/connectors", {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    credentials: "same-origin",
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.error?.code || `http_${response.status}`);
  return payload.data || [];
}

async function githubRequest(path, payload = null) {
  const options = {
    method: payload === null ? "GET" : "POST",
    headers: { Accept: "application/json" },
    cache: "no-store",
    credentials: "same-origin",
  };
  if (payload !== null) {
    const csrf = await sessionToken();
    options.headers["Content-Type"] = "application/json";
    options.headers["X-FORGE-CSRF"] = csrf;
    options.headers["Idempotency-Key"] = `github-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result?.error?.code || `http_${response.status}`);
  return result.data;
}

let integrationCatalogEntries = [];
let integrationCatalogFilter = "all";

function renderIntegrationCatalog(entries) {
  integrationCatalogEntries = entries || integrationCatalogEntries;
  const catalog = byId("integration-catalog");
  replace(catalog);
  const visibleEntries = integrationCatalogFilter === "all"
    ? integrationCatalogEntries
    : integrationCatalogEntries.filter((entry) => entry.provider?.category === integrationCatalogFilter);
  for (const entry of visibleEntries) {
    const provider = entry.provider || {};
    const card = element("article", `integration-card ${entry.connection_state || ""}`);
    card.dataset.integrationCategory = display(provider.category);
    card.append(
      element("strong", "", provider.name),
      element("small", "", display(provider.category).replaceAll("_", " · ")),
      element("small", "integration-auth", display(provider.auth_strategy)),
      element("span", "catalog-state", display(entry.status_summary)),
    );
    card.title = `${display(provider.auth_strategy)} · ${(provider.capabilities || []).join(", ")}`;
    catalog.append(card);
  }
  const connected = integrationCatalogEntries.filter((entry) => ["connected", "local_available"].includes(entry.connection_state)).length;
  const drivers = integrationCatalogEntries.filter((entry) => entry.connection_state === "driver_available").length;
  byId("integration-catalog-count").textContent = `${connected} connected/local · ${drivers} drivers`;
}

function setIntegrationFilter(filter) {
  integrationCatalogFilter = filter;
  for (const button of document.querySelectorAll("[data-integration-filter]")) {
    button.classList.toggle("active", button.dataset.integrationFilter === filter);
  }
  byId("integration-catalog-section").open = true;
  renderIntegrationCatalog(integrationCatalogEntries);
}

async function loadIntegrationCatalog() {
  const entries = await githubRequest("/api/v1/integrations");
  renderIntegrationCatalog(entries);
}

function setGitHubSettingsStatus(state, title, copy) {
  const status = byId("github-settings-status");
  status.className = `settings-status ${state}`;
  replace(status, element("strong", "", title), element("span", "", copy));
}

function renderGitHubEvidence(evidence, freshnessState = "fresh", ageSeconds = 0) {
  const summary = byId("github-evidence-summary");
  if (!evidence) {
    summary.hidden = true;
    return;
  }
  summary.hidden = false;
  const latestRun = evidence.workflow_runs?.[0];
  const conclusion = display(latestRun?.conclusion || latestRun?.status, "NO RUN").toUpperCase();
  const actions = byId("github-actions-state");
  actions.textContent = conclusion;
  actions.className = `connection-badge ${conclusion === "SUCCESS" ? "success" : conclusion === "FAILURE" ? "failure" : ""}`;
  const factList = byId("github-evidence-facts");
  replace(factList);
  for (const [label, value] of [
    ["Repository", evidence.repository_full_name],
    ["Exact commit", evidence.head_sha],
    ["Commit time", formatTime(evidence.head_commit_at)],
    ["Collected", formatTime(evidence.collected_at)],
    [
      "Freshness",
      `${display(freshnessState, "missing").toUpperCase()}${Number.isFinite(ageSeconds) ? ` · ${ageSeconds}s` : ""}`,
    ],
    ["Changed files", evidence.total_changed_files],
    ["Open PRs", evidence.open_pull_requests?.length || 0],
    ["Evidence hash", evidence.evidence_hash],
  ]) {
    const row = element("div");
    row.append(element("dt", "", label), element("dd", "", value));
    factList.append(row);
  }
  const files = byId("github-changed-files");
  replace(files);
  const changedFiles = evidence.changed_files || [];
  if (!changedFiles.length) files.append(element("span", "", "변경 파일 없음"));
  for (const path of changedFiles) files.append(element("code", "", path));
}

function renderGitHubStatus(status) {
  const connected = Boolean(status?.configured);
  const badge = byId("github-connection-badge");
  badge.textContent = connected ? "Connected" : "Not connected";
  badge.className = `connection-badge ${connected ? "connected" : ""}`;
  byId("github-sync-button").disabled = !connected;
  byId("github-readiness-dot").className = `ready-dot ${connected ? "complete" : "partial"}`;
  byId("github-readiness-label").textContent = connected
    ? `GitHub · ${status.repository_full_name}`
    : "GitHub App not connected";
  if (connected) {
    byId("github-app-id").value = status.app_id;
    byId("github-installation-id").value = status.installation_id;
    const [owner, repository] = status.repository_full_name.split("/", 2);
    byId("github-owner").value = owner;
    byId("github-repository").value = repository;
    byId("github-key-help").textContent = `${status.private_key_filename} · ${status.private_key_sha256} · 키 내용과 토큰은 표시하지 않습니다.`;
    setGitHubSettingsStatus(
      "success",
      "GitHub App 연결됨",
      `${status.repository_full_name} · ${formatTime(status.connected_at)}`,
    );
  }
  renderGitHubEvidence(
    status?.latest_evidence,
    status?.latest_evidence_state,
    status?.latest_evidence_age_seconds,
  );
}

async function loadGitHubStatus() {
  const status = await githubRequest("/api/v1/integrations/github");
  renderGitHubStatus(status);
  return status;
}

async function githubConnectPayload() {
  const form = byId("github-settings-form");
  if (!form.reportValidity()) throw new Error("missing_github_settings");
  const file = byId("github-private-key").files?.[0];
  if (!file) throw new Error("missing_private_key");
  if (file.size > 32768) throw new Error("private_key_too_large");
  if (!/\.pem$/i.test(file.name)) throw new Error("private_key_must_be_pem");
  return {
    app_id: inputValue("github-app-id"),
    installation_id: inputValue("github-installation-id"),
    owner: inputValue("github-owner"),
    repository: inputValue("github-repository"),
    private_key_filename: file.name,
    private_key_pem: await file.text(),
  };
}

async function runGitHubConnection(action) {
  const button = byId(action === "test" ? "github-test-button" : "github-connect-button");
  button.disabled = true;
  setGitHubSettingsStatus("loading", action === "test" ? "연결 시험 중" : "안전하게 저장 중", "GitHub App ID, installation, repository와 read-only 권한을 검증합니다.");
  try {
    const payload = await githubConnectPayload();
    const path = action === "test" ? "/api/v1/integrations/github/test" : "/api/v1/integrations/github";
    const result = await githubRequest(path, payload);
    setGitHubSettingsStatus(
      "success",
      action === "test" ? "연결 시험 성공" : "GitHub App 연결 완료",
      `${result.repository_full_name} · ${result.permissions?.join(", ") || "read-only"}`,
    );
    if (action === "connect") {
      byId("github-private-key").value = "";
      await Promise.all([loadGitHubStatus(), loadIntegrationCatalog()]);
    }
  } catch (error) {
    const code = error instanceof Error ? error.message : "unknown_error";
    setGitHubSettingsStatus("error", "GitHub 연결 실패", `오류 코드: ${code}`);
  } finally {
    button.disabled = false;
  }
}

function quantity(value, unit, dimension, identifier) {
  return {
    value,
    unit,
    dimension,
    source: { kind: "user", identifier },
    uncertainty: null,
  };
}

function sourceRef(prefix) {
  return {
    artifact_id: inputValue(`${prefix}-artifact-id`),
    domain: inputValue(`${prefix}-domain`),
    source_system: inputValue(`${prefix}-source-system`),
    source_revision: inputValue(`${prefix}-source-revision`),
    content_hash: inputValue(`${prefix}-content-hash`),
    captured_at: utcTimestamp(`${prefix}-captured-at`),
  };
}

function selectedIntent() {
  const selected = document.querySelector("input[name='plan-intent']:checked");
  return selected ? selected.value : "change_impact";
}

function selectedVerifyMode() {
  const selected = document.querySelector("input[name='verify-mode']:checked");
  return selected ? selected.value : "change_preview";
}

function assetInputPayload() {
  return {
    asset_id: inputValue("asset-id"),
    kind: inputValue("asset-kind"),
    source_system: inputValue("asset-source-system"),
    source_locator: inputValue("asset-source-locator"),
    source_revision: inputValue("asset-source-revision"),
    content_hash: inputValue("asset-content-hash"),
    captured_at: utcTimestamp("asset-captured-at"),
  };
}

function captureSnapshotPayload() {
  const project = inputValue("connect-project-id");
  return {
    captures: [
      {
        connector_id: inputValue("connect-connector-id"),
        capture_key: inputValue("connect-capture-key"),
        project_id: project,
        snapshot_id: inputValue("connect-snapshot-id"),
        hardware_revision_id: inputValue("connect-hardware-revision-id"),
        captured_at: utcTimestamp("connect-captured-at"),
      },
    ],
  };
}

function externalEvidenceDraftPayload() {
  const evidenceIds = inputValue("verify-evidence-ids")
    .split("\n")
    .map((value) => value.trim())
    .filter((value) => value !== "");
  return {
    evidence_plan_hash: inputValue("verify-evidence-plan-hash"),
    actual_change_analysis_hash: inputValue("verify-analysis-hash"),
    imported_evidence_ids: evidenceIds,
  };
}

function interfaceContract(prefix) {
  const commandMin = inputValue(`${prefix}-command-min`);
  const commandMax = inputValue(`${prefix}-command-max`);
  const safeValue = inputValue(`${prefix}-safe-value`);
  const hasCommandRange = commandMin !== "" || commandMax !== "" || safeValue !== "";
  const signal = {
    name: inputValue(`${prefix}-signal-name`),
    pin: inputValue(`${prefix}-pin`),
    direction: inputValue(`${prefix}-direction`),
    voltage_min: quantity(inputNumber(`${prefix}-voltage-min`), "V", "voltage", `${prefix}:voltage_min`),
    voltage_max: quantity(inputNumber(`${prefix}-voltage-max`), "V", "voltage", `${prefix}:voltage_max`),
  };
  if (hasCommandRange) {
    signal.command_min = quantity(inputNumber(`${prefix}-command-min`), "%", "duty_cycle", `${prefix}:command_min`);
    signal.command_max = quantity(inputNumber(`${prefix}-command-max`), "%", "duty_cycle", `${prefix}:command_max`);
    signal.safe_value = quantity(inputNumber(`${prefix}-safe-value`), "%", "duty_cycle", `${prefix}:safe_value`);
  }
  return {
    schema_version: "1.0.0",
    contract_id: `${inputValue("component-id")}:${prefix}`,
    protocol_schema_hash: inputValue(`${prefix}-protocol-schema-hash`),
    signals: [signal],
  };
}

function optionalQuote(prefix, partNumber) {
  if (byId(`${prefix}-quote-supplier`) === null) return null;
  const supplier = inputValue(`${prefix}-quote-supplier`);
  const unitPrice = inputValue(`${prefix}-quote-unit-price`);
  const sourceUrl = inputValue(`${prefix}-quote-source-url`);
  const sourceHash = inputValue(`${prefix}-quote-source-hash`);
  const observedAt = inputValue(`${prefix}-quote-observed-at`);
  const expiresAt = inputValue(`${prefix}-quote-expires-at`);
  const values = [supplier, unitPrice, sourceUrl, sourceHash, observedAt, expiresAt];
  if (values.every((value) => value === "")) return null;
  if (values.some((value) => value === "")) throw new Error("incomplete_quote_snapshot");
  return {
    quote_id: `${inputValue("change-id")}:${prefix}:quote`,
    part_number: partNumber,
    supplier,
    region: "global",
    currency: "USD",
    unit_price: unitPrice,
    minimum_quantity: 1,
    observed_at: utcTimestamp(`${prefix}-quote-observed-at`),
    expires_at: utcTimestamp(`${prefix}-quote-expires-at`),
    shipping_included: null,
    shipping_cost: null,
    tax_included: null,
    tax_cost: null,
    source_url: sourceUrl,
    source_hash: sourceHash,
  };
}

function componentDraft(prefix) {
  const partNumber = inputValue(`${prefix}-part-number`);
  return {
    component_id: inputValue("component-id"),
    manufacturer: inputValue(`${prefix}-manufacturer`),
    part_number: partNumber,
    quantity: inputInteger(`${prefix}-quantity`),
    source_ref: sourceRef(prefix),
    interface_contract: interfaceContract(prefix),
    quote: optionalQuote(prefix, partNumber),
    attributes: {
      logic_voltage_max: quantity(
        inputNumber(`${prefix}-voltage-max`),
        "V",
        "voltage",
        `${prefix}:logic_voltage_max`,
      ),
    },
  };
}

function changePreviewPayload() {
  const action = inputValue("change-action");
  const rationale = inputValue("rationale");
  const change = {
    change_id: inputValue("change-id"),
    action,
    before: action === "add" ? null : componentDraft("before"),
    after: action === "remove" ? null : componentDraft("after"),
    rationale: rationale ? [rationale] : [],
  };
  return {
    scenario_id: inputValue("scenario-id"),
    asset_input: assetInputPayload(),
    intent_kind: selectedIntent(),
    baseline_snapshot_id: inputValue("baseline-snapshot-id"),
    proposed_hardware_revision_id: inputValue("proposed-hardware-revision-id"),
    changes: [change],
  };
}

function externalEvidencePlanPayload() {
  return {
    scenario_id: inputValue("scenario-id"),
    asset_input: assetInputPayload(),
    baseline_snapshot_id: inputValue("baseline-snapshot-id"),
    scenario_text: inputValue("operating-scenario"),
    external_tool_ref: inputValue("external-tool-ref"),
    required_evidence: [
      {
        test_id: inputValue("external-test-id"),
        required_tier: inputValue("external-required-tier"),
        acceptance_criteria: inputValue("external-acceptance-criteria"),
      },
    ],
  };
}

function designProposalPayload() {
  return {
    goal: inputValue("engineering-goal"),
    priority: inputValue("engineering-priority"),
    constraints: inputValue("engineering-constraints"),
    preview_hash: inputValue("design-preview-hash"),
  };
}

function releaseDiagnosisPayload() {
  return {
    decision_hash: inputValue("diagnosis-decision-hash"),
  };
}

function resolutionPlanPayload(diagnosisHash, fixProposalHash) {
  return {
    diagnosis_hash: diagnosisHash,
    fix_proposal_hash: fixProposalHash,
  };
}

function endpointInputs(prefix) {
  return Array.from(byId(`${prefix === "before" ? "current" : "candidate"}-endpoint`).querySelectorAll("input, select"));
}

function setEndpointEnabled(prefix, enabled) {
  for (const control of endpointInputs(prefix)) {
    control.disabled = !enabled;
    if (control.dataset.optional !== "true") {
      control.required = enabled && control.tagName !== "SELECT";
    }
  }
}

function syncActionState() {
  const action = inputValue("change-action");
  setEndpointEnabled("before", action !== "add");
  setEndpointEnabled("after", action !== "remove");
}

function syncIntentState() {
  const isChangeImpact = selectedIntent() === "change_impact";
  byId("operating-scenario-field").hidden = isChangeImpact;
  byId("external-plan-fields").hidden = isChangeImpact;
  byId("operating-scenario").required = !isChangeImpact;
  byId("external-tool-ref").required = !isChangeImpact;
  byId("external-test-id").required = !isChangeImpact;
  byId("external-acceptance-criteria").required = !isChangeImpact;
  byId("change-id").closest("fieldset").disabled = !isChangeImpact;
  byId("current-endpoint").disabled = !isChangeImpact;
  byId("candidate-endpoint").disabled = !isChangeImpact;
  byId("preview-button").querySelector("span").textContent = isChangeImpact
    ? "예상 영향 생성"
    : "증거 계획 생성";
  if (isChangeImpact) syncActionState();
}

function syncVerifyModeState() {
  const isChangePreview = selectedVerifyMode() === "change_preview";
  byId("verify-preview-hash").closest("label").hidden = !isChangePreview;
  byId("verify-preview-hash").required = isChangePreview;
  byId("verify-evidence-plan-field").hidden = isChangePreview;
  byId("verify-evidence-plan-hash").required = !isChangePreview;
  byId("verify-evidence-ids-field").hidden = isChangePreview;
  byId("verify-evidence-ids").required = !isChangePreview;
  byId("verify-button").querySelector("span").textContent = isChangePreview
    ? "누락 검사"
    : "증거 계획 확인";
}

function syncAssetKindState() {
  const sourceByKind = {
    connected_device: "manual",
    cad_model: "cad",
    design_drawing: "cad",
    plm_snapshot: "plm",
    manual_snapshot: "manual",
  };
  const target = sourceByKind[inputValue("asset-kind")];
  if (target) byId("asset-source-system").value = target;
}

function setValue(id, value) {
  byId(id).value = value;
}

function fillSampleConnect() {
  const now = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString().slice(0, 16);
  setValue("connect-project-id", "project-1");
  setValue("connect-project-name", "Controller Release Demo");
  setValue("connect-version", "1");
  setValue("connect-connector-id", "fake-plm");
  setValue("connect-capture-key", "before");
  setValue("connect-snapshot-id", "snapshot-11");
  setValue("connect-hardware-revision-id", "HW-12");
  setValue("connect-captured-at", now);
}

function fillSamplePreview() {
  const hashA = `sha256:${"1".repeat(64)}`;
  const hashB = `sha256:${"8".repeat(64)}`;
  const hashC = `sha256:${"2".repeat(64)}`;
  const now = new Date();
  const before = new Date(now.valueOf() - 2 * 60 * 60 * 1000).toISOString().slice(0, 16);
  const after = new Date(now.valueOf() - 1 * 60 * 60 * 1000).toISOString().slice(0, 16);

  setValue("preview-project-id", "project-1");
  setValue("preview-version", "3");
  setValue("verify-version", "5");
  setValue("asset-id", "mobile-base-alpha");
  setValue("asset-kind", "plm_snapshot");
  setValue("asset-source-system", "plm");
  setValue("asset-source-locator", "plm://robot-controller/mobile-base-alpha");
  setValue("asset-source-revision", "HW-12");
  setValue("asset-content-hash", hashC);
  setValue("asset-captured-at", before);
  setValue("external-tool-ref", "sim-suite-v4");
  setValue("external-test-id", "motor-margin-sim");
  setValue("external-required-tier", "simulation");
  setValue("external-acceptance-criteria", "driver temperature margin >= 15C and no protocol timeout");
  setValue("engineering-goal", "controller board EOL 대응 설계 대안 비교");
  setValue("engineering-priority", "reliability");
  setValue("engineering-constraints", "pinout 변경 최소화; 3.3V logic 유지; BOM delta 8% 이하");
  setValue("scenario-id", "scenario-1");
  setValue("baseline-snapshot-id", "snapshot-11");
  setValue("proposed-hardware-revision-id", "HW-13-proposed");
  setValue("change-id", "replace-controller-board");
  setValue("change-action", "replace");
  document.querySelector("input[name='plan-intent'][value='change_impact']").checked = true;
  setValue("component-id", "controller-board");
  setValue("rationale", "supplier-eol");

  setValue("before-manufacturer", "Acme Components");
  setValue("before-part-number", "CTRL-1A");
  setValue("before-quantity", "1");
  setValue("before-artifact-id", "controller-board");
  setValue("before-domain", "hardware");
  setValue("before-source-system", "plm");
  setValue("before-source-revision", "12");
  setValue("before-content-hash", hashA);
  setValue("before-captured-at", before);
  setValue("before-signal-name", "pwm-out");
  setValue("before-pin", "J3-4");
  setValue("before-direction", "output");
  setValue("before-voltage-min", "0");
  setValue("before-voltage-max", "3.3");
  setValue("before-command-min", "0");
  setValue("before-command-max", "100");
  setValue("before-safe-value", "0");
  setValue("before-protocol-schema-hash", hashC);
  setValue("before-quote-supplier", "Example Distributor");
  setValue("before-quote-unit-price", "18.10");
  setValue("before-quote-observed-at", before);
  setValue("before-quote-expires-at", new Date(now.valueOf() + 30 * 24 * 60 * 60 * 1000).toISOString().slice(0, 16));
  setValue("before-quote-source-url", "https://supplier.example/CTRL-1A");
  setValue("before-quote-source-hash", hashA);

  setValue("after-manufacturer", "Acme Components");
  setValue("after-part-number", "CTRL-2A");
  setValue("after-quantity", "1");
  setValue("after-artifact-id", "controller-board");
  setValue("after-domain", "hardware");
  setValue("after-source-system", "plm");
  setValue("after-source-revision", "13-candidate");
  setValue("after-content-hash", hashB);
  setValue("after-captured-at", after);
  setValue("after-signal-name", "pwm-out");
  setValue("after-pin", "J3-7");
  setValue("after-direction", "output");
  setValue("after-voltage-min", "0");
  setValue("after-voltage-max", "3.3");
  setValue("after-command-min", "0");
  setValue("after-command-max", "100");
  setValue("after-safe-value", "0");
  setValue("after-protocol-schema-hash", hashC);
  setValue("after-quote-supplier", "Example Distributor");
  setValue("after-quote-unit-price", "19.20");
  setValue("after-quote-observed-at", after);
  setValue("after-quote-expires-at", new Date(now.valueOf() + 30 * 24 * 60 * 60 * 1000).toISOString().slice(0, 16));
  setValue("after-quote-source-url", "https://supplier.example/CTRL-2A");
  setValue("after-quote-source-hash", hashB);
  byId("operating-scenario").value = "경사로 10도, payload 18kg, 주변 온도 40C에서 모터 드라이버 여유율 확인";
  syncActionState();
  syncIntentState();
}

function previewFacts(stored) {
  const preview = stored.preview;
  return facts([
    ["Recommendation", preview.recommendation],
    ["Asset", stored.scenario?.asset_input?.asset_id],
    ["Asset source", stored.scenario?.asset_input?.kind],
    ["Intent", stored.scenario?.intent_kind],
    ["Preview hash", stored.preview_hash],
    ["Scenario hash", stored.scenario_hash],
    ["Baseline snapshot", stored.baseline_snapshot_id],
    ["Baseline hash", stored.baseline_snapshot_hash],
    ["Generated", formatTime(preview.generated_at)],
  ]);
}

function renderConnectorList(connectors) {
  const panel = byId("connect-output");
  const rows = connectors.map((item) => {
    const row = element("tr");
    row.append(
      element("td", "mono", item.adapter_id),
      element("td", "mono", item.source_system),
      element("td", "", ""),
      element("td", "", item.read_only ? "read-only" : "write-capable"),
    );
    row.children[2].append(tagList(item.capabilities || []));
    return row;
  });
  const table = element("table");
  table.append(element("caption", "sr-only", "available read-only connectors"));
  const thead = element("thead");
  const head = element("tr");
  head.append(
    element("th", "", "Adapter"),
    element("th", "", "Source"),
    element("th", "", "Capabilities"),
    element("th", "", "Boundary"),
  );
  thead.append(head);
  const tbody = element("tbody");
  tbody.append(...rows);
  table.append(thead, tbody);
  const wrapper = element("div", "table-wrap");
  wrapper.append(table);
  replace(
    panel,
    sectionBlock(
      "Available read-only connectors",
      rows.length ? wrapper : element("div", "empty-inline", "등록된 connector가 없습니다."),
    ),
  );
  panel.hidden = false;
}

function renderProjectCreated(data) {
  const project = data.project || {};
  const panel = byId("connect-output");
  replace(
    panel,
    sectionBlock("Project created", facts([
      ["Project", project.project_id],
      ["Name", project.name],
      ["Version", project.version],
      ["Updated", formatTime(project.updated_at)],
    ])),
  );
  panel.hidden = false;
  setWorkspaceValue("workspace-project-id", project.project_id);
  setWorkspaceValue("workspace-project-name", project.name);
}

function renderSnapshotCaptured(data) {
  const stored = data.connector_snapshot || data || {};
  const snapshot = stored.snapshot || {};
  const panel = byId("connect-output");
  replace(
    panel,
    sectionBlock("Source-bound baseline", facts([
      ["Project", stored.project_id],
      ["Snapshot", stored.snapshot_id],
      ["Snapshot hash", stored.snapshot_hash],
      ["Hardware revision", snapshot.hardware_revision_id],
      ["Captured", formatTime(snapshot.captured_at)],
      ["Artifacts", snapshot.artifacts?.length || 0],
    ])),
    sectionBlock("Boundary", tagList([
      "read-only-capture",
      "no-cad-plm-writeback",
      "no-device-control",
      "no-firmware-flash",
    ])),
  );
  panel.hidden = false;
  setWorkspaceValue("workspace-project-id", stored.project_id);
  setWorkspaceValue("workspace-project-name", stored.project_id);
  updateWorkspaceStats({ changes: 0, risks: 0, verified: "0/0", blockers: 0, release: "PLAN" });
  setWorkflowStage("plan");
}

function applySnapshotToPlan(data) {
  const stored = data.connector_snapshot || data || {};
  const snapshot = stored.snapshot || {};
  if (!stored.project_id || !stored.snapshot_id) return;
  const captured = snapshot.captured_at ? new Date(snapshot.captured_at).toISOString().slice(0, 16) : inputValue("connect-captured-at");
  setValue("preview-project-id", stored.project_id);
  setValue("baseline-snapshot-id", stored.snapshot_id);
  setValue("asset-id", stored.snapshot_id);
  setValue("asset-kind", inputValue("asset-kind") || "plm_snapshot");
  setValue("asset-source-system", "plm");
  setValue("asset-source-locator", `${inputValue("connect-connector-id")}://${stored.project_id}/${stored.snapshot_id}`);
  setValue("asset-source-revision", snapshot.hardware_revision_id || inputValue("connect-hardware-revision-id"));
  setValue("asset-content-hash", stored.snapshot_hash || inputValue("asset-content-hash"));
  setValue("asset-captured-at", captured);
}

function renderOperatingScenarioPlan(stored) {
  const record = stored.external_evidence_plan || stored;
  const plan = record.plan || record;
  const scenario = plan.operating_scenario || {};
  const asset = scenario.asset_input || {};
  const requirements = plan.required_evidence || [];
  const panel = byId("preview-output");
  const banner = element("section", "preview-banner indeterminate");
  banner.append(
    chip("external_evidence_plan"),
    element("h3", "", "운용 시나리오 외부 증거 계획"),
    element("p", "", "FORGE는 physics simulation이나 장치 제어를 실행하지 않고, 외부 실행 결과가 가져와야 할 증거 조건만 지정합니다."),
  );
  const requiredEvidence = requirements.length ? requirements.map((item) => {
    const criteria = (item.acceptance_criteria || [])
      .map((criterion) => criterion.expected)
      .join("; ");
    return `${item.tier}: ${item.test_id} · ${criteria}`;
  }) : [
    "firmware/build hash, protocol schema hash and BOM cost provenance must remain source-bound",
    "simulation, bench, HIL and physical_device evidence cannot substitute for each other",
  ];
  replace(
    panel,
    banner,
    facts([
      ["Asset", asset.asset_id],
      ["Input kind", asset.kind],
      ["Source", `${asset.source_system} · ${asset.source_revision}`],
      ["Locator", asset.source_locator],
      ["Scenario", scenario.scenario_text],
      ["External adapters", requirements.map((item) => item.external_adapter_id).join(", ")],
      ["Plan hash", record.plan_hash || plan.plan_hash],
      ["Baseline snapshot", scenario.baseline_snapshot_id || record.baseline_snapshot_id],
    ]),
    sectionBlock("Required external evidence", tagList(requiredEvidence)),
    sectionBlock("FORGE checks after import", tagList([
      "evidence-tier-not-interchangeable",
      "firmware-build-and-protocol-hash-match",
      "bom-cost-source-and-observed-at-present",
      "retest-scope-satisfied-by-required-tier",
    ])),
  );
  panel.hidden = false;
  setFlowGuide({
    step: "3 / 4 · VERIFY",
    title: "외부 시험 결과를 가져와 검증하세요",
    copy: "증거 계획은 준비됐습니다. simulation, bench, HIL, 실제 장치 결과를 tier별로 가져온 뒤 계획 대비 누락을 확인합니다.",
    label: "VERIFY로 이동",
    targetId: "verify-panel",
    focusId: "verify-analysis-hash",
    stage: "verify",
  });
  scrollToFlowTarget("preview-output");
}

function renderExternalEvidenceVerification(stored) {
  const panel = byId("verification-output");
  const record = stored.external_evidence_plan_verification || stored;
  const verification = record.verification || {};
  const evidenceLabel = verification.imported_evidence?.length
    ? verification.imported_evidence.map((item) => item.evidence.evidence_id)
    : ["no-imported-evidence-ids"];
  replace(
    panel,
    sectionBlock("External evidence plan verification", facts([
      ["Evidence plan hash", record.plan_hash],
      ["Actual analysis", record.actual_change_analysis_hash],
      ["Check result", verification.check_result],
      ["Verification hash", record.verification_hash],
    ])),
    sectionBlock("Imported evidence IDs", tagList(evidenceLabel)),
    sectionBlock("Missing required evidence", tagList(verification.missing_required_evidence_ids || [])),
    sectionBlock("Failed required evidence", tagList(verification.failed_required_evidence_ids || [])),
    sectionBlock("Unexpected imported evidence", tagList(verification.unexpected_imported_evidence_ids || [])),
  );
  panel.hidden = false;
  const required = listFrom(verification.imported_evidence).length
    + listFrom(verification.missing_required_evidence_ids).length;
  updateWorkspaceStats({
    verified: `${listFrom(verification.imported_evidence).length}/${required}`,
    release: "VERIFY",
  });
  setFlowGuide({
    step: "4 / 4 · RELEASE",
    title: "검증 결과를 release decision과 연결하세요",
    copy: "계획 대비 증거 검사가 끝났습니다. 저장된 canonical decision hash로 READY 또는 BLOCKED 근거를 조회합니다.",
    label: "RELEASE로 이동",
    targetId: "release-panel",
    focusId: "decision-hash",
    stage: "release",
  });
  scrollToFlowTarget("verification-output");
}

function verificationFacts(stored) {
  const verification = stored.verification;
  return facts([
    ["Matches plan", verification.matches_plan],
    ["Verification hash", stored.verification_hash],
    ["Preview hash", stored.preview_hash],
    ["Actual analysis", stored.actual_change_analysis_hash],
  ]);
}

function renderEndpointComparison(change) {
  const table = element("table", "comparison-table");
  const caption = element("caption", "sr-only", "current component and candidate component comparison");
  const thead = element("thead");
  const head = element("tr");
  head.append(
    element("th", "", "Field"),
    element("th", "", "Current"),
    element("th", "", "Candidate"),
  );
  thead.append(head);
  const tbody = element("tbody");
  const before = change.before || {};
  const after = change.after || {};
  const rows = [
    ["Manufacturer", before.manufacturer, after.manufacturer],
    ["Part number", before.part_number, after.part_number],
    ["Quantity", before.quantity, after.quantity],
    ["Source revision", before.source_ref?.source_revision, after.source_ref?.source_revision],
    ["Source hash", before.source_ref?.content_hash, after.source_ref?.content_hash],
  ];
  for (const [label, current, candidate] of rows) {
    const row = element("tr");
    row.append(element("td", "", label), element("td", "mono", display(current)), element("td", "mono", display(candidate)));
    tbody.append(row);
  }
  table.append(caption, thead, tbody);
  const wrapper = element("div", "table-wrap");
  wrapper.append(table);
  return wrapper;
}

function sectionBlock(title, child) {
  const section = element("section", "result-section");
  section.append(element("h3", "", title), child);
  return section;
}

function findingList(items) {
  const list = element("ul", "code-list");
  for (const item of items || []) {
    const entry = element("li");
    entry.append(
      chip(item.severity),
      element("span", "", ` ${item.rule_id} · ${item.summary}`),
      pathList(item.evidence_refs),
    );
    list.append(entry);
  }
  return list.childElementCount ? list : element("div", "empty-inline", "예상 불일치가 없습니다.");
}

function retestTable(items) {
  const table = element("table");
  table.append(element("caption", "sr-only", "predicted retest requirements"));
  const thead = element("thead");
  const head = element("tr");
  head.append(
    element("th", "", "Test"),
    element("th", "", "Tier"),
    element("th", "", "Status"),
    element("th", "", "Reasons"),
  );
  thead.append(head);
  const tbody = element("tbody");
  for (const item of items || []) {
    const row = element("tr");
    row.append(
      element("td", "mono", display(item.test_id)),
      element("td", "", ""),
      element("td", "", ""),
      element("td", "", ""),
    );
    row.children[1].append(chip(item.required_tier));
    row.children[2].append(chip(item.status));
    row.children[3].append(tagList(item.reason_codes), pathList(item.triggered_by));
    tbody.append(row);
  }
  table.append(thead, tbody);
  const wrapper = element("div", "table-wrap");
  if (!tbody.childElementCount) return element("div", "empty-inline", "필요한 시험이 없습니다.");
  wrapper.append(table);
  return wrapper;
}

function severityWeight(value) {
  return { blocker: 4, error: 3, warning: 2, info: 1 }[String(value).toLowerCase()] || 0;
}

function renderImpactInspector(target, domain, findings, retests) {
  const reasons = findings.map((item) => `${item.rule_id} · ${item.summary}`);
  const evidence = Array.from(new Set(findings.flatMap((item) => listFrom(item.evidence_refs))));
  const linkedRetests = retests.filter((item) => {
    const haystack = [...listFrom(item.triggered_by), ...listFrom(item.reason_codes)].join(" ").toLowerCase();
    return haystack.includes(String(domain).toLowerCase());
  });
  const strongest = findings.reduce(
    (current, item) => severityWeight(item.severity) > severityWeight(current) ? item.severity : current,
    "review",
  );
  replace(
    target,
    element("p", "eyebrow", "IMPACT EVIDENCE INSPECTOR"),
    element("h3", "", display(domain).toUpperCase()),
    chip(strongest),
    sectionBlock(
      "Why affected",
      reasons.length ? tagList(reasons) : element("p", "", "Preview scope에 포함됐지만 구체 rule evidence가 부족합니다."),
    ),
    sectionBlock(
      "Evidence",
      evidence.length ? pathList(evidence) : element("p", "", "추가 source-bound evidence가 필요합니다."),
    ),
    sectionBlock(
      "Required verification",
      linkedRetests.length
        ? tagList(linkedRetests.map((item) => `${item.test_id}:${item.required_tier}`))
        : element("p", "", "이 domain에 직접 연결된 retest는 preview payload에 없습니다."),
    ),
  );
}

function impactMap(preview) {
  const domains = listFrom(preview.predicted_affected_domains);
  const findings = listFrom(preview.predicted_findings);
  const retests = listFrom(preview.predicted_retests);
  if (!domains.length) return element("div", "empty-inline", "예측된 영향 domain이 없습니다.");

  const workspace = element("div", "impact-workspace");
  const graph = element("div", "impact-map");
  graph.setAttribute("role", "group");
  graph.setAttribute("aria-label", "Source-bound change impact map");
  graph.append(element("div", "impact-root", "CHANGE INTENT"));
  const nodes = element("div", "impact-node-grid");
  const inspector = element("aside", "impact-inspector");
  inspector.setAttribute("aria-live", "polite");

  for (const domain of domains) {
    const related = findings.filter((item) => listFrom(item.affected_domains).includes(domain));
    const strongest = related.reduce(
      (current, item) => severityWeight(item.severity) > severityWeight(current) ? item.severity : current,
      "review",
    );
    const button = element("button", `impact-node severity-${String(strongest).toLowerCase()}`);
    button.type = "button";
    button.setAttribute("aria-pressed", "false");
    button.append(
      element("strong", "", display(domain)),
      element("span", "", related.length ? `${related.length} linked finding` : "verification review"),
    );
    button.addEventListener("click", () => {
      for (const node of nodes.querySelectorAll("button")) node.setAttribute("aria-pressed", "false");
      button.setAttribute("aria-pressed", "true");
      renderImpactInspector(inspector, domain, related, retests);
    });
    nodes.append(button);
  }
  graph.append(nodes);
  workspace.append(graph, inspector);
  const first = nodes.querySelector("button");
  if (first) first.click();
  return workspace;
}

function scoreGrid(scores = {}) {
  const list = element("dl", "tradeoff-score-grid");
  for (const [label, key] of [
    ["Cost", "cost"],
    ["Change scope", "change"],
    ["Performance", "performance"],
    ["Risk control", "risk"],
    ["Goal fit", "goal_satisfaction"],
  ]) {
    const wrapper = element("div");
    wrapper.append(element("dt", "", label), element("dd", "", `${display(scores[key], 0)}/100`));
    list.append(wrapper);
  }
  return list;
}

function renderSelectedPlanDraft(target, item) {
  const hash = item.alternative_hash || item.proposal_hash || item.hash || "";
  replace(
    target,
    element("p", "eyebrow", "LOCAL PLAN DRAFT"),
    element("h3", "", display(item.title || item.strategy || item.alternative_id)),
    element("p", "", "이 선택은 현재 브라우저의 PLAN 초안입니다. 승인, source writeback 또는 release evidence로 저장되지 않습니다."),
    facts([
      ["Strategy", item.strategy],
      ["Alternative hash", hash],
      ["Proposed changes", listFrom(item.proposed_change_refs).join(", ")],
      ["Affected domains", listFrom(item.affected_domains).join(", ")],
    ]),
    sectionBlock("Rationale", element("p", "", display(item.rationale))),
    sectionBlock("Required follow-up", tagList([
      ...listFrom(item.missing_information),
      "operator-review-required",
      "actual-change-verification-required",
    ])),
  );
  target.hidden = false;
  activeAlternativeHash = hash;
  setWorkspaceValue("workspace-change-id", item.alternative_id || "PLAN draft");
  appendCollaborationMessage("FORGE", `${display(item.title || item.strategy)} 대안을 로컬 PLAN 초안으로 선택했습니다. 서버 승인 상태는 생성하지 않았습니다.`);
  setFlowGuide({
    step: "3 / 4 · VERIFY",
    title: "선택한 PLAN과 실제 변경을 비교하세요",
    copy: "선택은 로컬 PLAN 초안입니다. 실제 change analysis 또는 외부 증거를 가져와 expected vs actual 차이를 확인합니다.",
    label: "VERIFY 준비",
    targetId: "verify-panel",
    focusId: "verify-analysis-hash",
    stage: "verify",
  });
  scrollToFlowTarget("selected-plan-draft");
}

function designAlternativeCards(alternatives) {
  const wrapper = element("div", "alternative-workspace");
  const cards = element("div", "alternative-grid");
  const draft = element("section", "selected-plan-draft");
  draft.id = "selected-plan-draft";
  draft.hidden = true;
  for (const item of alternatives) {
    const card = element("article", "alternative-card");
    const hash = item.alternative_hash || item.proposal_hash || item.hash || "";
    const button = element("button", "secondary-button select-alternative-button", "Use as PLAN draft");
    button.type = "button";
    button.dataset.alternativeHash = hash;
    button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", () => {
      for (const candidate of cards.querySelectorAll(".alternative-card")) candidate.classList.remove("selected");
      for (const control of cards.querySelectorAll(".select-alternative-button")) control.setAttribute("aria-pressed", "false");
      card.classList.add("selected");
      button.setAttribute("aria-pressed", "true");
      renderSelectedPlanDraft(draft, item);
    });
    card.append(
      element("p", "eyebrow", display(item.alternative_id || "DESIGN OPTION")),
      element("h3", "", display(item.title || item.strategy || item.name)),
      element("p", "", display(item.rationale || item.expected_impact)),
      scoreGrid(item.tradeoff_scores),
      sectionBlock("Change refs", tagList(listFrom(item.proposed_change_refs))),
      sectionBlock("Affected domains", tagList(listFrom(item.affected_domains))),
      sectionBlock("Evidence refs", pathList(listFrom(item.evidence_refs))),
      sectionBlock("Missing information", tagList(listFrom(item.missing_information))),
      element("code", "alternative-hash", hash),
      button,
    );
    cards.append(card);
  }
  if (!cards.childElementCount) return element("div", "empty-inline", "생성된 design alternative가 없습니다.");
  wrapper.append(cards, draft);
  return wrapper;
}

function renderDesignProposals(data) {
  const record = firstNamed(data, ["design_proposal", "design_proposals", "proposal"]);
  const proposalHash = record.proposal_hash || record.design_proposal_hash || record.hash || "";
  const project = inputValue("preview-project-id");
  const projectVersion = Number(data.project_version || 0);
  if (PROJECT_PATTERN.test(project) && SHA_PATTERN.test(proposalHash) && projectVersion > 0) {
    guidedState.ledger = {
      projectId: project,
      proposalHash,
      projectVersion,
      sessionId: `ui-${Date.now()}`,
      sequence: 0,
      state: "idea",
      candidateHash: "",
      candidateRevision: 0,
      simulationHash: "",
      simulationRevision: 0,
      claimRevision: 0,
      status: "synced",
    };
    setValue("preview-version", String(projectVersion));
    appendCollaborationMessage(
      "FORGE",
      `Design proposal이 ${project} project v${projectVersion}에 연결됐습니다. Guided workflow의 Confirm & Simulate부터 candidate, simulation, claim, transition이 서버 ledger에 기록됩니다.`,
    );
  }
  const alternatives = listFrom(record.design_alternatives || record.alternatives || record.proposals);
  const panel = byId("design-proposal-output");
  replace(
    panel,
    sectionBlock("Planning boundary", tagList([
      "planning-only",
      "advisory",
      "no-source-writeback",
      "no-device-control",
    ])),
    facts([
      ["Goal", record.goal || inputValue("engineering-goal")],
      ["Preview hash", record.preview_hash || inputValue("design-preview-hash")],
      ["Proposal hash", record.proposal_hash || record.design_proposal_hash || record.hash],
      ["Generated", formatTime(record.generated_at || record.created_at)],
    ]),
    sectionBlock("Choose a change strategy", designAlternativeCards(alternatives)),
  );
  panel.hidden = false;
  setFlowGuide({
    step: "2 / 4 · PLAN",
    title: "설계 대안을 비교하고 하나를 초안으로 선택하세요",
    copy: "비용, 변경 범위, 성능, 위험 통제와 source-bound 근거를 함께 비교할 수 있습니다.",
    label: "대안 비교",
    targetId: "design-proposal-output",
    stage: "plan",
  });
  scrollToFlowTarget("design-proposal-output");
}

function renderPreviewResult(stored) {
  const preview = stored.preview;
  if (!PLAN_RECOMMENDATIONS.has(preview.recommendation)) {
    throw new Error("invalid_preview_recommendation");
  }
  const scenario = stored.scenario;
  const firstChange = scenario?.changes?.[0] || {};
  const panel = byId("preview-output");
  const banner = element("section", `preview-banner ${preview.recommendation}`);
  banner.append(
    chip(preview.recommendation),
    element("h3", "", "사전 검토 결과"),
    element("p", "", "이 값은 예상 영향입니다. 출시 판정이나 제출 증거로 사용하지 않습니다."),
  );
  replace(
    panel,
    banner,
    previewFacts(stored),
    sectionBlock("Component comparison", renderEndpointComparison(firstChange)),
    sectionBlock("Inspectable impact map", impactMap(preview)),
    sectionBlock("Predicted affected domains", tagList(preview.predicted_affected_domains)),
    sectionBlock("Predicted required actions", tagList(preview.predicted_required_actions)),
    sectionBlock("Predicted findings", findingList(preview.predicted_findings)),
    sectionBlock("Required retests by evidence tier", retestTable(preview.predicted_retests)),
    sectionBlock("Assumptions", tagList(preview.assumptions)),
    sectionBlock("Missing information", tagList(preview.missing_information)),
  );
  panel.hidden = false;
  setWorkspaceValue("workspace-project-id", stored.project_id || stored.scenario?.project_id);
  setWorkspaceValue("workspace-change-id", firstChange.change_id || stored.scenario_id);
  updateWorkspaceStats({
    changes: stored.scenario?.changes?.length || 0,
    risks: preview.predicted_findings?.length || 0,
    release: "PLAN",
  });
  setFlowGuide({
    step: "2 / 4 · PLAN",
    title: "영향을 확인했으면 설계 대안을 비교하세요",
    copy: "영향 지도와 재시험 범위를 확인한 뒤 같은 preview hash에 묶인 여러 변경 전략을 생성합니다.",
    label: "설계 대안 입력",
    targetId: "design-proposal-form",
    focusId: "engineering-goal",
    stage: "plan",
  });
  scrollToFlowTarget("preview-output");
}

function renderVerificationResult(stored) {
  const verification = stored.verification;
  const panel = byId("verification-output");
  const observed = listFrom(verification.observed_planned_refs);
  const deviations = listFrom(verification.deviations);
  const missing = deviations.filter((item) => item.kind === "missing_planned_change");
  const expectedTotal = observed.length + missing.length;
  const summary = element("section", "verification-progress");
  const progress = element("progress");
  progress.max = Math.max(expectedTotal, 1);
  progress.value = observed.length;
  summary.append(
    element("strong", "", `${observed.length} / ${expectedTotal} planned refs observed`),
    progress,
    element("span", "", deviations.length ? `${deviations.length} deviation(s) require review` : "Expected and actual refs match"),
  );

  const table = element("table", "expected-actual-table");
  table.append(element("caption", "sr-only", "expected plan references compared with actual project state"));
  const thead = element("thead");
  const head = element("tr");
  head.append(
    element("th", "", "Expected"),
    element("th", "", "Actual"),
    element("th", "", "Status"),
    element("th", "", "Reason"),
  );
  thead.append(head);
  const tbody = element("tbody");
  for (const ref of observed) {
    const row = element("tr");
    row.append(element("td", "mono", ref), element("td", "mono", ref), element("td", "", ""), element("td", "", "Observed planned change"));
    row.children[2].append(chip("pass"));
    tbody.append(row);
  }
  for (const item of deviations) {
    const isMissing = item.kind === "missing_planned_change";
    const row = element("tr");
    row.append(
      element("td", "mono", display(isMissing ? item.planned_ref : "not-planned")),
      element("td", "mono", display(isMissing ? "missing" : item.actual_ref)),
      element("td", "", ""),
      element("td", "", item.summary),
    );
    row.children[2].append(chip(isMissing ? "missing" : "unplanned"));
    tbody.append(row);
  }
  table.append(thead, tbody);
  const tableWrap = element("div", "local-table-wrap");
  tableWrap.append(table);
  replace(
    panel,
    summary,
    verificationFacts(stored),
    sectionBlock("Expected vs actual", tableWrap),
  );
  panel.hidden = false;
  updateWorkspaceStats({ verified: `${observed.length}/${expectedTotal}`, release: "VERIFY" });
  setFlowGuide({
    step: "4 / 4 · RELEASE",
    title: "검증 결과를 release decision과 연결하세요",
    copy: "expected vs actual 검사가 끝났습니다. 저장된 canonical decision hash로 READY 또는 BLOCKED 근거를 조회합니다.",
    label: "RELEASE로 이동",
    targetId: "release-panel",
    focusId: "decision-hash",
    stage: "release",
  });
  scrollToFlowTarget("verification-output");
}

function renderMetrics(assessment, report) {
  const retests = listFrom(report.required_retests);
  const passedRetests = retests.filter((item) => item.status === "passed").length;
  const blockers = listFrom(report.blocker_codes).length;
  const values = [
    ["Revision", `${assessment.from_hardware_revision_id} → ${assessment.to_hardware_revision_id}`],
    ["Changes", assessment.artifact_changes?.length || 0],
    ["Tests", `${passedRetests} / ${retests.length}`],
    ["Evidence", listFrom(report.evidence).length],
    ["Blockers", blockers],
  ];
  const nodes = values.map(([label, value]) => {
    const wrapper = element("div");
    wrapper.append(element("dt", "", label), element("dd", "", value));
    return wrapper;
  });
  replace(byId("metrics"), ...nodes);
  updateWorkspaceStats({
    changes: assessment.artifact_changes?.length || 0,
    risks: listFrom(report.findings).length,
    verified: `${passedRetests}/${retests.length}`,
    blockers,
    release: display(report.status).toUpperCase(),
  });
}

function renderSummary(report) {
  const statusValue = display(report.status, "unknown").toLowerCase();
  if (!RELEASE_STATES.has(statusValue)) throw new Error("invalid_release_state");
  const summary = byId("decision-summary");
  summary.className = `decision-summary ${statusValue}`;
  const status = byId("decision-status");
  status.className = `status-chip ${statusValue}`;
  status.textContent = statusValue.toUpperCase();
  byId("evaluated-at").textContent = formatTime(report.evaluated_at);
  byId("summary-copy").textContent = statusValue === "ready"
    ? "모든 configured finding과 exact-tier retest가 제출된 근거로 충족되었습니다. 이는 인증이나 생산 승인이 아닙니다."
    : `${report.blocker_codes?.length || 0}개 blocker 또는 미충족 retest가 남아 출시 준비가 증명되지 않았습니다.`;

  const box = byId("blocker-summary");
  const heading = element("h3", "", report.blocker_codes?.length ? "Blocking reasons" : "Gate result");
  const list = element("ul");
  const reasons = report.blocker_codes?.length ? report.blocker_codes : ["no_unresolved_blockers"];
  for (const reason of reasons) list.append(element("li", "", reason));
  replace(box, heading, list);
  setFlowGuide({
    step: "COMPLETE · RELEASE",
    title: `${statusValue.toUpperCase()} 판정과 근거를 확인했습니다`,
    copy: statusValue === "ready"
      ? "필수 증거와 exact-tier 재시험이 충족됐습니다. 아래 감사 근거를 검토해 최종 운영 결정을 내리세요."
      : "남은 blocker와 재시험을 확인한 뒤 Resolve with FORGE로 새 PLAN 초안을 만들 수 있습니다.",
    label: "Release 상태 확인 완료",
    targetId: "decision-summary",
    stage: "release",
    complete: true,
  });
}

function renderChanges(assessment) {
  byId("revision-line").textContent = `${assessment.from_hardware_revision_id} → ${assessment.to_hardware_revision_id}`;
  const rows = [];
  for (const change of assessment.artifact_changes || []) {
    const endpoint = change.after || change.before || {};
    const beforeRevision = change.before?.source_revision || "—";
    const afterRevision = change.after?.source_revision || "—";
    const row = element("tr");
    row.append(
      element("td", "mono", display(change.domain)),
      element("td", "", display(endpoint.artifact_id)),
      element("td", "", ""),
      element("td", "mono", `${beforeRevision} → ${afterRevision}`),
      element("td", "", ""),
    );
    row.children[2].append(chip(change.change_type));
    row.children[4].append(pathList(change.changed_paths), tagList(change.facets));
    rows.push(row);
  }
  replace(byId("change-rows"), ...rows);
  byId("change-empty").hidden = rows.length !== 0;
  byId("change-rows").closest(".table-wrap").hidden = rows.length === 0;
}

function renderFindings(report) {
  const cards = [];
  for (const finding of report.findings || []) {
    const card = element("article", `finding-card ${display(finding.severity).toLowerCase()}`);
    card.append(
      chip(finding.severity),
      element("h3", "", display(finding.summary)),
      element("p", "mono", display(finding.rule_id)),
      tagList(finding.affected_domains),
      pathList(finding.evidence_refs),
    );
    cards.push(card);
  }
  if (!cards.length) cards.push(element("div", "empty-inline", "cross-artifact 불일치가 없습니다."));
  replace(byId("finding-list"), ...cards);
}

function renderRetests(report) {
  const rows = [];
  for (const retest of report.required_retests || []) {
    const row = element("tr");
    row.append(
      element("td", "mono", display(retest.test_id)),
      element("td", "", ""),
      element("td", "", ""),
      element("td", "", ""),
      element("td", "mono", display(retest.satisfied_by_evidence_id, "미제출")),
    );
    row.children[1].append(chip(retest.required_tier));
    row.children[2].append(chip(retest.status));
    row.children[3].append(tagList(retest.reason_codes), pathList(retest.triggered_by));
    rows.push(row);
  }
  replace(byId("retest-rows"), ...rows);
}

function renderEvidence(report, decision) {
  const cards = [];
  for (const [tierId, label, description] of TIERS) {
    const card = element("section", "tier-card");
    card.append(element("h3", "", label), element("p", "", description));
    const list = element("ul");
    const evidence = (report.evidence || []).filter((item) => item.tier === tierId);
    for (const item of evidence) {
      const entry = element("li", "evidence-item");
      entry.append(
        chip(item.verdict),
        element("strong", "", item.test_id),
        element("small", "", `${item.source_system} · ${formatTime(item.recorded_at)}`),
        element("small", "", item.fixture_id || item.device_instance_id || item.result_ref),
      );
      list.append(entry);
    }
    if (!evidence.length) list.append(element("li", "evidence-item", "미제출"));
    card.append(list);
    cards.push(card);
  }
  replace(byId("tier-grid"), ...cards);

  const rejected = decision.rejected_evidence || [];
  byId("rejected-count").textContent = `(${rejected.length})`;
  replace(
    byId("rejected-list"),
    ...(rejected.length
      ? rejected.map((item) => element("li", "", `${item.evidence_id} · ${item.reason}`))
      : [element("li", "", "없음")]),
  );
}

function renderProvenance(stored, decision, report, assessment) {
  const cost = decision.cost_evaluation;
  const bom = byId("bom-provenance");
  if (cost) {
    const costFacts = facts([
      ["Verdict", cost.evaluation?.verdict],
      ["Budget", `${cost.evaluation?.budget_limit} ${cost.evaluation?.currency}`],
      ["Projected", cost.evaluation?.projected_total],
      ["Reserve", cost.evaluation?.reserve_rate],
      ["Evaluated", formatTime(cost.evaluated_at)],
      ["BOM hash", cost.bom_artifact_hash],
      ["Dependency", cost.dependency_hash],
    ]);
    const quotes = element("ul", "quote-list");
    for (const quote of cost.quotes || []) {
      const item = element("li");
      item.append(
        element("strong", "", `${quote.part_number} · ${quote.supplier}`),
        element("span", "", `${quote.unit_price} ${quote.currency} · MOQ ${quote.minimum_quantity}`),
        element("span", "", `observed ${formatTime(quote.observed_at)}`),
        element("span", "", `source ${quote.source_url}`),
        element("span", "", quote.source_hash),
      );
      quotes.append(item);
    }
    replace(bom, costFacts, quotes);
  } else {
    replace(bom, element("div", "empty-inline", "선택된 BOM cost evidence가 없습니다."));
  }

  const build = decision.selected_firmware_build;
  const buildBox = byId("build-provenance");
  if (build) {
    replace(buildBox, facts([
      ["Verdict", build.verdict],
      ["Build ID", build.build_id],
      ["Source revision", build.firmware_source_revision],
      ["Source hash", build.firmware_source_hash],
      ["Toolchain", `${build.toolchain_id} ${build.toolchain_version}`],
      ["Toolchain hash", build.toolchain_hash],
      ["Artifact hash", build.firmware_build_artifact_hash],
      ["Protocol hash", build.protocol_schema_hash],
      ["Completed", formatTime(build.completed_at)],
      ["Result ref", build.result_ref],
    ]));
  } else {
    replace(buildBox, element("div", "empty-inline", "선택된 firmware build evidence가 없습니다."));
  }

  const bindings = [
    ["Project", stored.project_id],
    ["Report", stored.report_id],
    ["Decision", stored.decision_hash],
    ["Policy", decision.policy_hash],
    ["Analysis", assessment.analysis_hash],
    ["Snapshot", report.snapshot_hash],
    ["Protocol schema", assessment.target_protocol_schema_hash],
    ["Previous decision", stored.previous_decision_hash],
  ];
  const nodes = [];
  for (const [label, value] of bindings) {
    nodes.push(element("dt", "", label), element("dd", "", display(value)));
  }
  replace(byId("canonical-bindings"), ...nodes);
}

function diagnosisTable(items) {
  const table = element("table", "diagnosis-table");
  table.append(element("caption", "sr-only", "evidence-bound blocker diagnosis"));
  const thead = element("thead");
  const head = element("tr");
  head.append(
    element("th", "", "Blocker"),
    element("th", "", "Evidence"),
    element("th", "", "Likely cause"),
    element("th", "", "Confidence"),
  );
  thead.append(head);
  const tbody = element("tbody");
  for (const item of items) {
    const row = element("tr");
    row.append(
      element("td", "mono", display(item.blocker_code || item.code || item.rule_id)),
      element("td", "", ""),
      element("td", "", display(item.likely_cause || item.cause || item.summary)),
      element("td", "", display(item.confidence || item.confidence_level)),
    );
    row.children[1].append(pathList(listFrom(item.evidence_refs || item.evidence || item.source_refs)));
    tbody.append(row);
  }
  table.append(thead, tbody);
  const wrapper = element("div", "local-table-wrap");
  if (!tbody.childElementCount) return element("div", "empty-inline", "증거에 묶인 blocker diagnosis가 없습니다.");
  wrapper.append(table);
  return wrapper;
}

function fixRecommendationList(items, diagnosisHash) {
  const list = element("div", "fix-list");
  for (const item of items) {
    const card = element("article", "fix-card");
    const fixHash = display(item.fix_proposal_hash || item.proposal_hash || item.hash, "");
    card.append(
      element("h3", "", display(item.title || item.name || item.fix_id || "Fix recommendation")),
      element("p", "", display(item.summary || item.recommendation || item.description)),
      facts([
        ["Fix proposal hash", fixHash],
        ["Expected effect", item.expected_effect || item.expected_impact],
        ["Required verification", listFrom(item.required_verification || item.required_evidence).join(", ")],
      ]),
    );
    if (SHA_PATTERN.test(diagnosisHash) && SHA_PATTERN.test(fixHash)) {
      const actions = element("div", "recommendation-actions");
      const button = element("button", "secondary-button resolve-button");
      button.type = "button";
      button.dataset.diagnosisHash = diagnosisHash;
      button.dataset.fixProposalHash = fixHash;
      button.append(element("span", "", "Resolve with FORGE"), element("span", "", "→"));
      actions.append(button);
      card.append(actions);
    }
    list.append(card);
  }
  return list.childElementCount ? list : element("div", "empty-inline", "fix recommendation이 없습니다.");
}

function renderReleaseDiagnosis(data) {
  const record = firstNamed(data, ["release_diagnosis", "diagnosis"]);
  const diagnosisHash = record.diagnosis_hash || record.hash || "";
  const blockers = listFrom(record.blocker_diagnoses || record.blockers || record.diagnoses);
  const fixes = listFrom(record.fix_recommendations || record.fix_proposals || record.recommendations);
  activeDiagnosisHash = diagnosisHash;
  activeFixProposalHash = fixes[0]?.fix_proposal_hash || fixes[0]?.proposal_hash || fixes[0]?.hash || "";
  const panel = byId("diagnosis-output");
  replace(
    panel,
    sectionBlock("Evidence requirement", tagList([
      "requires-release-evidence",
      "diagnosis-is-advisory",
      "no-source-writeback",
      "no-device-control",
    ])),
    facts([
      ["Decision hash", record.decision_hash || inputValue("diagnosis-decision-hash")],
      ["Diagnosis hash", diagnosisHash],
      ["Generated", formatTime(record.generated_at || record.created_at)],
    ]),
    sectionBlock("Blocker Diagnosis", diagnosisTable(blockers)),
    sectionBlock("Fix Recommendations", fixRecommendationList(fixes, diagnosisHash)),
  );
  panel.hidden = false;
}

function applyResolutionPlanToDraft(data) {
  const record = firstNamed(data, ["resolution_plan", "plan"]);
  const draft = record.plan_draft || record.draft || record;
  const title = draft.title || draft.goal || draft.summary || "blocker resolution plan";
  const constraints = listFrom(draft.constraints || draft.required_constraints || draft.assumptions).join("; ");
  document.querySelector("input[name='plan-intent'][value='change_impact']").checked = true;
  setValue("engineering-goal", title);
  setValue("engineering-priority", draft.priority || "reliability");
  setValue("engineering-constraints", constraints);
  if (draft.scenario_id) setValue("scenario-id", draft.scenario_id);
  if (draft.change_id) setValue("change-id", draft.change_id);
  if (draft.component_id) setValue("component-id", draft.component_id);
  if (draft.rationale || draft.rationale_code) setValue("rationale", draft.rationale || draft.rationale_code);
  setWorkspaceValue("workspace-change-id", draft.change_id || "Resolution PLAN draft");
  updateWorkspaceStats({ release: "RE-PLAN" });
  setFlowGuide({
    step: "2 / 4 · RE-PLAN",
    title: "Blocker 해결안을 새 PLAN 초안으로 검토하세요",
    copy: "진단 결과가 변경 목표와 제약조건에 복사됐습니다. 실제 source 수정과 승인은 운영자가 수행합니다.",
    label: "PLAN 초안 검토",
    targetId: "plan-panel",
    focusId: "engineering-goal",
    stage: "plan",
  });
  syncIntentState();
  scrollToFlowTarget("plan-panel", "engineering-goal");
}

function renderDecision(stored) {
  if (!stored || typeof stored !== "object") {
    throw new Error("invalid_release_decision_shape");
  }
  const decision = stored.decision;
  if (!decision || typeof decision !== "object") {
    throw new Error("invalid_release_decision_shape");
  }
  const report = decision.report;
  const assessment = decision.change_assessment;
  if (!report || !assessment || stored.project_id !== report.project_id) {
    throw new Error("invalid_release_decision_shape");
  }
  renderSummary(report);
  renderMetrics(assessment, report);
  renderChanges(assessment);
  renderFindings(report);
  renderRetests(report);
  renderEvidence(report, decision);
  renderProvenance(stored, decision, report, assessment);
  const decisionHash = stored.decision_hash || "";
  setValue("diagnosis-project-id", stored.project_id || "");
  setValue("diagnosis-decision-hash", decisionHash);
  setWorkspaceValue("workspace-project-id", stored.project_id);
  setWorkspaceValue("workspace-change-id", assessment.analysis_id || assessment.analysis_hash);
}

function readFragment() {
  const values = new URLSearchParams(window.location.hash.slice(1));
  return { project: values.get("project") || "", decision: values.get("decision") || "" };
}

function writeFragment(project, decision) {
  const values = new URLSearchParams({ project, decision });
  window.history.replaceState(null, "", `#${values.toString()}`);
}

async function loadDecision(project, decisionHash) {
  const dashboard = byId("dashboard");
  const button = byId("load-button");
  dashboard.hidden = true;
  dashboard.setAttribute("aria-busy", "true");
  button.disabled = true;
  setLoadStatus("loading", "근거 조회 중", "loopback evidence store에서 불변 decision을 읽고 있습니다.");
  try {
    const path = `/api/v1/projects/${project}/release-decisions/${decisionHash}`;
    const response = await fetch(path, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      credentials: "same-origin",
    });
    const payload = await response.json();
    if (!response.ok) {
      const code = payload?.error?.code || `http_${response.status}`;
      throw new Error(code);
    }
    renderDecision(payload.data);
    writeFragment(project, decisionHash);
    dashboard.hidden = false;
    setLoadStatus("success", "근거 로드 완료", "저장된 decision payload를 변경 없이 시각화했습니다.");
    byId("decision-summary").focus({ preventScroll: true });
  } catch (error) {
    const code = error instanceof Error ? error.message : "unknown_error";
    const offline = code === "Failed to fetch" || code === "NetworkError when attempting to fetch resource.";
    setLoadStatus(
      "error",
      offline ? "로컬 FORGE 서버에 연결할 수 없음" : "Decision을 불러오지 못함",
      offline ? "127.0.0.1의 FORGE 서버가 실행 중인지 확인하세요." : `오류 코드: ${code}`,
    );
  } finally {
    dashboard.setAttribute("aria-busy", "false");
    button.disabled = false;
  }
}

async function loadDesignProposal(project, proposalHash) {
  const response = await fetch(`/api/v1/projects/${project}/design-proposals/${proposalHash}`, {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    credentials: "same-origin",
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.error?.code || `http_${response.status}`);
  return payload.data;
}

async function loadReleaseDiagnosis(project, diagnosisHash) {
  const response = await fetch(`/api/v1/projects/${project}/release-diagnoses/${diagnosisHash}`, {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    credentials: "same-origin",
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.error?.code || `http_${response.status}`);
  return payload.data;
}

async function loadResolutionPlan(project, planHash) {
  const response = await fetch(`/api/v1/projects/${project}/resolution-plans/${planHash}`, {
    method: "GET",
    headers: { Accept: "application/json" },
    cache: "no-store",
    credentials: "same-origin",
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.error?.code || `http_${response.status}`);
  return payload.data;
}

const startChangeForm = byId("start-change-form");
if (startChangeForm) {
  startChangeForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const intent = inputValue("start-change-input");
    if (!intent) {
      startChangeForm.reportValidity();
      setPanelStatus("start-status", "error", "변경 목표 확인", "변경하고 싶은 목표를 한 문장으로 입력하세요.");
      return;
    }
    setValue("engineering-goal", intent.slice(0, 240));
    setWorkspaceValue("workspace-change-id", "PLAN draft");
    setPanelStatus("start-status", "success", "PLAN 초안 시작", "변경 목표를 Engineering Session에 복사했습니다. Source-bound baseline과 변경 항목을 확인하세요.");
    appendCollaborationMessage("USER", intent);
    appendCollaborationMessage("FORGE", "목표를 PLAN 초안에 반영했습니다. 설계 방향을 바꾸는 우선순위와 source-bound 입력만 추가로 확인합니다.");
    setFlowGuide({
      step: "1 / 4 · CONNECT",
      title: "변경 목표를 기준 자산과 연결하세요",
      copy: "목표는 PLAN 초안에 보존했습니다. 이제 로봇, CAD, 설계도 또는 PLM snapshot을 읽기 전용 기준으로 연결합니다.",
      label: "기준 자산 연결",
      targetId: "connect-panel",
      focusId: "connect-project-id",
      stage: "plan",
    });
    scrollToFlowTarget("connect-panel", "connect-project-id");
  });
}

for (const button of document.querySelectorAll(".priority-action")) {
  button.addEventListener("click", () => {
    const priority = button.dataset.priority || "";
    if (!priority) return;
    setValue("engineering-priority", priority);
    for (const candidate of document.querySelectorAll(".priority-action")) candidate.setAttribute("aria-pressed", "false");
    button.setAttribute("aria-pressed", "true");
    appendCollaborationMessage("FORGE", `${priority} 우선순위를 PLAN에 반영했습니다. 이 선택은 design alternative 생성 입력으로만 사용됩니다.`);
    byId("engineering-priority").focus({ preventScroll: true });
  });
}

const forgeAiForm = byId("forge-ai-form");
if (forgeAiForm) {
  forgeAiForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const instruction = inputValue("forge-ai-input");
    if (!instruction) {
      forgeAiForm.reportValidity();
      setPanelStatus("forge-ai-status", "error", "PLAN 지시 확인", "목표 또는 제약조건을 입력하세요.");
      return;
    }
    const currentGoal = inputValue("engineering-goal");
    const currentConstraints = inputValue("engineering-constraints");
    if (!currentGoal) setValue("engineering-goal", instruction.slice(0, 240));
    else {
      const combined = [currentConstraints, instruction].filter((value) => value).join("; ");
      setValue("engineering-constraints", combined.slice(0, 800));
    }
    appendCollaborationMessage("USER", instruction);
    appendCollaborationMessage("FORGE", "요청을 로컬 PLAN 입력에 반영했습니다. 새 분석 결과는 preview 또는 design alternative를 다시 생성한 뒤 CENTER에서 확인하세요.");
    setPanelStatus("forge-ai-status", "success", "PLAN 입력 반영", "대화 내용은 production AI 응답이나 승인 기록이 아니라 현재 브라우저의 structured draft입니다.");
    setValue("forge-ai-input", "");
  });
}

const form = byId("decision-form");
form.addEventListener("submit", (event) => {
  event.preventDefault();
  const project = inputValue("project-id");
  const decision = inputValue("decision-hash");
  if (!PROJECT_PATTERN.test(project) || !DECISION_PATTERN.test(decision)) {
    form.reportValidity();
    setLoadStatus("error", "입력 형식 확인", "안전한 project ID와 canonical sha256 decision hash가 필요합니다.");
    return;
  }
  loadDecision(project, decision);
});

const connectForm = byId("connect-form");
byId("create-project-button").addEventListener("click", async () => {
  const project = inputValue("connect-project-id");
  const name = inputValue("connect-project-name");
  const button = byId("create-project-button");
  if (!PROJECT_PATTERN.test(project) || name === "") {
    connectForm.reportValidity();
    setPanelStatus("connect-status", "error", "Project 입력 확인", "안전한 project ID와 project name이 필요합니다.");
    return;
  }
  button.disabled = true;
  setPanelStatus("connect-status", "loading", "Project 생성 중", "loopback store에 release policy와 project record를 생성합니다.");
  try {
    const data = await postProjectMutation({ project_id: project, name });
    renderProjectCreated(data);
    setValue("preview-project-id", project);
    setValue("verify-project-id", project);
    setValue("project-id", project);
    setValue("connect-version", "1");
    setPanelStatus("connect-status", "success", "Project 생성 완료", "다음 단계에서 읽기 전용 connector snapshot을 가져올 수 있습니다.");
    setFlowGuide({
      step: "1 / 4 · CONNECT",
      title: "Project가 준비됐습니다. 기준 snapshot을 가져오세요",
      copy: "읽기 전용 connector와 capture key를 확인한 뒤 hardware revision 기준을 저장합니다.",
      label: "Snapshot 입력 확인",
      targetId: "connect-panel",
      focusId: "connect-connector-id",
      stage: "plan",
    });
  } catch (error) {
    const code = error instanceof Error ? error.message : "unknown_error";
    setPanelStatus("connect-status", "error", "Project 생성 실패", `오류 코드: ${code}`);
  } finally {
    button.disabled = false;
  }
});

connectForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const project = inputValue("connect-project-id");
  const version = inputValue("connect-version");
  const button = byId("connect-button");
  if (
    !PROJECT_PATTERN.test(project)
    || !VERSION_PATTERN.test(version)
    || !SAFE_ID_PATTERN.test(inputValue("connect-connector-id"))
    || !SAFE_ID_PATTERN.test(inputValue("connect-capture-key"))
    || !SAFE_ID_PATTERN.test(inputValue("connect-snapshot-id"))
    || !SAFE_ID_PATTERN.test(inputValue("connect-hardware-revision-id"))
  ) {
    connectForm.reportValidity();
    setPanelStatus("connect-status", "error", "CONNECT 입력 확인", "project, connector, capture key, snapshot, hardware revision 값이 필요합니다.");
    return;
  }
  byId("connect-output").hidden = true;
  button.disabled = true;
  setPanelStatus("connect-status", "loading", "Snapshot 캡처 중", "원본 connector를 읽기 전용으로 호출해 aggregate snapshot을 저장합니다.");
  try {
    const data = await postMutation(
      project,
      `/api/v1/projects/${project}/connector-snapshots`,
      captureSnapshotPayload(),
      Number(version),
    );
    renderSnapshotCaptured(data.connector_snapshot);
    applySnapshotToPlan(data.connector_snapshot);
    setValue("preview-version", String(Number(version) + 1));
    setPanelStatus("connect-status", "success", "Snapshot 연결 완료", "Baseline snapshot이 PLAN 입력으로 복사되었습니다.");
    setFlowGuide({
      step: "2 / 4 · PLAN",
      title: "기준 자산에서 변경 항목을 지정하세요",
      copy: "baseline snapshot이 PLAN에 복사됐습니다. 변경 부품 또는 운용 시나리오를 입력해 예상 영향과 재시험 범위를 계산합니다.",
      label: "변경 영향 계획",
      targetId: "plan-panel",
      focusId: "change-id",
      stage: "plan",
    });
    scrollToFlowTarget("plan-panel", "change-id");
  } catch (error) {
    const code = error instanceof Error ? error.message : "unknown_error";
    setPanelStatus("connect-status", "error", "Snapshot 연결 실패", `오류 코드: ${code}`);
  } finally {
    button.disabled = false;
  }
});

const previewForm = byId("preview-form");
previewForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const project = inputValue("preview-project-id");
  const version = inputValue("preview-version");
  const button = byId("preview-button");
  if (
    !PROJECT_PATTERN.test(project)
    || !VERSION_PATTERN.test(version)
    || !SAFE_ID_PATTERN.test(inputValue("asset-id"))
    || !SHA_PATTERN.test(inputValue("asset-content-hash"))
    || !SAFE_ID_PATTERN.test(inputValue("scenario-id"))
    || !SAFE_ID_PATTERN.test(inputValue("baseline-snapshot-id"))
    || !SAFE_ID_PATTERN.test(inputValue("proposed-hardware-revision-id"))
  ) {
    previewForm.reportValidity();
    setPanelStatus("preview-status", "error", "입력 형식 확인", "asset, project, scenario, baseline, proposed revision 값이 필요합니다.");
    return;
  }
  if (
    selectedIntent() === "operating_scenario"
    && (
      inputValue("operating-scenario") === ""
      || !SAFE_ID_PATTERN.test(inputValue("external-tool-ref"))
      || !SAFE_ID_PATTERN.test(inputValue("external-test-id"))
      || inputValue("external-acceptance-criteria") === ""
    )
  ) {
    previewForm.reportValidity();
    setPanelStatus("preview-status", "error", "운용 시나리오 입력 확인", "scenario, external tool, test ID, acceptance criteria가 필요합니다.");
    return;
  }
  byId("preview-output").hidden = true;
  button.disabled = true;
  if (selectedIntent() === "operating_scenario") {
    setPanelStatus("preview-status", "loading", "증거 계획 저장 중", "외부 simulation, bench, HIL, 실제 장치 증거 요구사항을 저장합니다.");
    try {
      const data = await postMutation(
        project,
        `/api/v1/projects/${project}/external-evidence-plans`,
        externalEvidencePlanPayload(),
        Number(version),
      );
      renderOperatingScenarioPlan(data.external_evidence_plan || data.data?.external_evidence_plan || data);
      byId("verify-project-id").value = project;
      byId("verify-version").value = String(Number(version) + 1);
      const planHash = data.external_evidence_plan?.plan_hash || data.data?.external_evidence_plan?.plan_hash || data.plan_hash;
      if (planHash) byId("verify-evidence-plan-hash").value = planHash;
      document.querySelector("input[name='verify-mode'][value='external_evidence_plan']").checked = true;
      syncVerifyModeState();
      setPanelStatus("preview-status", "success", "증거 계획 저장 완료", "실행 결과는 외부 simulation, bench, HIL, 실제 장치 증거로 가져와야 합니다.");
    } catch (error) {
      const code = error instanceof Error ? error.message : "unknown_error";
      setPanelStatus("preview-status", "error", "증거 계획 저장 실패", `오류 코드: ${code}`);
    } finally {
      button.disabled = false;
    }
    return;
  }
  setPanelStatus("preview-status", "loading", "예상 영향 계산 중", "source-bound 부품 변경안의 영향과 필요한 시험을 계산합니다.");
  try {
    const data = await postMutation(
      project,
      `/api/v1/projects/${project}/change-previews`,
      changePreviewPayload(),
      Number(version),
    );
    renderPreviewResult(data.change_preview);
    const previewHash = data.change_preview.preview_hash || "";
    byId("verify-project-id").value = project;
    byId("verify-preview-hash").value = previewHash;
    byId("design-preview-hash").value = previewHash;
    setPanelStatus("preview-status", "success", "사전 검토 완료", "결과는 예상 영향이며 release evidence가 아닙니다.");
  } catch (error) {
    const code = error instanceof Error ? error.message : "unknown_error";
    setPanelStatus("preview-status", "error", "Preview 생성 실패", `오류 코드: ${code}`);
  } finally {
    button.disabled = false;
  }
});

const designProposalForm = byId("design-proposal-form");
designProposalForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const project = inputValue("preview-project-id");
  const version = inputValue("preview-version");
  const previewHash = inputValue("design-preview-hash");
  const button = byId("design-proposal-button");
  if (!PROJECT_PATTERN.test(project) || !VERSION_PATTERN.test(version) || !SHA_PATTERN.test(previewHash) || inputValue("engineering-goal") === "") {
    designProposalForm.reportValidity();
    setPanelStatus("preview-status", "error", "Engineering Session 입력 확인", "goal과 preview hash가 필요합니다.");
    return;
  }
  byId("design-proposal-output").hidden = true;
  button.disabled = true;
  setPanelStatus("preview-status", "loading", "Design alternatives 생성 중", "preview hash에 묶인 planning-only 대안을 요청합니다.");
  try {
    const data = await postMutation(
      project,
      `/api/v1/projects/${project}/design-proposals`,
      designProposalPayload(),
      Number(version),
    );
    renderDesignProposals(data);
    setPanelStatus("preview-status", "success", "Design alternatives 생성 완료", "대안은 advisory이며 실제 source writeback이나 device control을 실행하지 않습니다.");
  } catch (error) {
    const code = error instanceof Error ? error.message : "unknown_error";
    setPanelStatus("preview-status", "error", "Design alternatives 생성 실패", `오류 코드: ${code}`);
  } finally {
    button.disabled = false;
  }
});

const verificationForm = byId("plan-verification-form");
verificationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const project = inputValue("verify-project-id");
  const version = inputValue("verify-version");
  const previewHash = inputValue("verify-preview-hash");
  const evidencePlanHash = inputValue("verify-evidence-plan-hash");
  const analysisHash = inputValue("verify-analysis-hash");
  const button = byId("verify-button");
  const isChangePreview = selectedVerifyMode() === "change_preview";
  const validExternalPlan = SHA_PATTERN.test(evidencePlanHash);
  if (!PROJECT_PATTERN.test(project) || !VERSION_PATTERN.test(version) || !SHA_PATTERN.test(analysisHash) || (isChangePreview && !SHA_PATTERN.test(previewHash)) || (!isChangePreview && !validExternalPlan)) {
    verificationForm.reportValidity();
    setPanelStatus("verification-status", "error", "입력 형식 확인", "선택한 검증 모드에 맞는 hash와 증거 ID가 필요합니다.");
    return;
  }
  byId("verification-output").hidden = true;
  button.disabled = true;
  if (!isChangePreview) {
    setPanelStatus("verification-status", "loading", "외부 증거 검증 중", "저장된 계획과 가져온 simulation, bench, HIL, 실제 장치 증거를 비교합니다.");
    try {
      const data = await postMutation(
        project,
        `/api/v1/projects/${project}/external-evidence-plan-verifications`,
        externalEvidenceDraftPayload(),
        Number(version),
      );
      renderExternalEvidenceVerification(data.external_evidence_plan_verification);
      setPanelStatus("verification-status", "success", "외부 증거 검증 완료", "이 결과는 계획 대비 증거 검사이며 READY/BLOCKED 판정은 RELEASE에서만 수행됩니다.");
    } catch (error) {
      const code = error instanceof Error ? error.message : "unknown_error";
      setPanelStatus("verification-status", "error", "외부 증거 검증 실패", `오류 코드: ${code}`);
    } finally {
      button.disabled = false;
    }
    return;
  }
  setPanelStatus("verification-status", "loading", "누락 검사 중", "계획된 영향과 실제 change analysis를 비교합니다.");
  try {
    const data = await postMutation(
      project,
      `/api/v1/projects/${project}/plan-verifications`,
      {
        preview_hash: previewHash,
        actual_change_analysis_hash: analysisHash,
      },
      Number(version),
    );
    renderVerificationResult(data.plan_verification);
    setPanelStatus("verification-status", "success", "누락 검사 완료", "Deviation은 release evidence가 아닙니다.");
  } catch (error) {
    const code = error instanceof Error ? error.message : "unknown_error";
    setPanelStatus("verification-status", "error", "누락 검사 실패", `오류 코드: ${code}`);
  } finally {
    button.disabled = false;
  }
});

const diagnosisForm = byId("release-diagnosis-form");
diagnosisForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const project = inputValue("diagnosis-project-id");
  const decisionHash = inputValue("diagnosis-decision-hash");
  const button = byId("diagnosis-button");
  if (!PROJECT_PATTERN.test(project) || !SHA_PATTERN.test(decisionHash)) {
    diagnosisForm.reportValidity();
    setPanelStatus("diagnosis-status", "error", "진단 입력 확인", "project ID와 canonical decision hash가 필요합니다.");
    return;
  }
  byId("diagnosis-output").hidden = true;
  button.disabled = true;
  setPanelStatus("diagnosis-status", "loading", "Blocker diagnosis 생성 중", "제출된 release evidence와 decision hash에 묶어 원인을 분석합니다.");
  try {
    const data = await postMutation(
      project,
      `/api/v1/projects/${project}/release-diagnoses`,
      releaseDiagnosisPayload(),
      inputValue("verify-version") || inputValue("preview-version") || "1",
    );
    renderReleaseDiagnosis(data);
    setPanelStatus("diagnosis-status", "success", "Blocker diagnosis 생성 완료", "Fix recommendations는 PLAN 초안 생성용 advisory입니다.");
  } catch (error) {
    const code = error instanceof Error ? error.message : "unknown_error";
    setPanelStatus("diagnosis-status", "error", "Blocker diagnosis 생성 실패", `오류 코드: ${code}`);
  } finally {
    button.disabled = false;
  }
});

byId("diagnosis-output").addEventListener("click", async (event) => {
  const target = event.target instanceof Element ? event.target : null;
  const button = target?.closest(".resolve-button");
  if (!button) return;
  const project = inputValue("diagnosis-project-id");
  const diagnosisHash = button.dataset.diagnosisHash || activeDiagnosisHash;
  const fixProposalHash = button.dataset.fixProposalHash || activeFixProposalHash;
  if (!PROJECT_PATTERN.test(project) || !SHA_PATTERN.test(diagnosisHash) || !SHA_PATTERN.test(fixProposalHash)) {
    setPanelStatus("diagnosis-status", "error", "Resolution plan 입력 확인", "diagnosis hash와 fix proposal hash가 필요합니다.");
    return;
  }
  button.disabled = true;
  setPanelStatus("diagnosis-status", "loading", "Resolution PLAN 초안 생성 중", "FORGE는 새 PLAN 입력만 채우며 원본 source나 장치를 변경하지 않습니다.");
  try {
    const data = await postMutation(
      project,
      `/api/v1/projects/${project}/resolution-plans`,
      resolutionPlanPayload(diagnosisHash, fixProposalHash),
      inputValue("verify-version") || inputValue("preview-version") || "1",
    );
    applyResolutionPlanToDraft(data);
    setPanelStatus("diagnosis-status", "success", "PLAN 초안 준비 완료", "Resolve with FORGE는 planning draft만 만들었습니다. 실행과 writeback은 수행하지 않았습니다.");
  } catch (error) {
    const code = error instanceof Error ? error.message : "unknown_error";
    setPanelStatus("diagnosis-status", "error", "Resolution PLAN 생성 실패", `오류 코드: ${code}`);
  } finally {
    button.disabled = false;
  }
});

byId("change-action").addEventListener("change", syncActionState);
byId("asset-kind").addEventListener("change", syncAssetKindState);
byId("workflow-next-action").addEventListener("click", (event) => {
  const button = event.currentTarget;
  scrollToFlowTarget(button.dataset.target, button.dataset.focus);
});
byId("guided-workflow").addEventListener("click", (event) => {
  const button = event.target.closest("[data-guided-action]");
  if (!button) return;
  handleGuidedAction(button.dataset.guidedAction, button.dataset.guidedValue || "");
});
byId("chat-sidebar").addEventListener("click", (event) => {
  const button = event.target.closest("[data-guided-action]");
  if (!button) return;
  handleGuidedAction(button.dataset.guidedAction, button.dataset.guidedValue || "");
});
byId("guided-workflow").addEventListener("change", (event) => {
  if (event.target.id !== "composer-cad-file") return;
  const file = event.target.files?.[0];
  if (!file) return;
  beginGuidedConnection("CAD attachment", file.name);
  if (/\.stl$/i.test(file.name)) {
    ingestSTLFile(file)
      .then(renderGuidedWorkflow)
      .catch((error) => appendCollaborationMessage("FORGE", `STL 형상 처리 실패 (${error instanceof Error ? error.message : "unknown_error"}).`));
    return;
  }
  const textExtensions = /\.(txt|md|csv|json|yaml|yml)$/i;
  if (textExtensions.test(file.name) && file.size <= 256000) {
    file.text()
      .then(async (text) => {
        guidedState.pendingKnowledge = {
          sourceId: `upload-${Date.now()}`,
          name: file.name.replace(/[^A-Za-z0-9._-]/g, "-"),
          text,
        };
        await ingestPendingKnowledge();
        renderGuidedWorkflow();
      })
      .catch((error) => appendCollaborationMessage("FORGE", `첨부 문서 처리 실패 (${error instanceof Error ? error.message : "unknown_error"}).`));
  }
});
byId("guided-workflow").addEventListener("keydown", (event) => {
  if (event.target.id !== "engineering-chat-input" || event.key !== "Enter" || event.shiftKey) return;
  event.preventDefault();
  byId("guided-workflow").querySelector(".composer-send")?.click();
});
byId("forge-ai-toggle").addEventListener("click", () => {
  const panel = byId("forge-ai-panel");
  const collapsed = panel.classList.toggle("collapsed");
  byId("forge-ai-toggle").setAttribute("aria-expanded", String(!collapsed));
  document.querySelector(".forge-workspace")?.classList.toggle("ai-collapsed", collapsed);
});
for (const control of document.querySelectorAll("input[name='plan-intent']")) {
  control.addEventListener("change", syncIntentState);
}
for (const control of document.querySelectorAll("input[name='verify-mode']")) {
  control.addEventListener("change", syncVerifyModeState);
}
byId("sample-connect-button").addEventListener("click", fillSampleConnect);
byId("sample-preview-button").addEventListener("click", fillSamplePreview);
byId("open-settings-button").addEventListener("click", () => {
  const dialog = byId("settings-dialog");
  dialog.showModal();
  if (IS_FILE_PROTOCOL) {
    setGitHubSettingsStatus("error", "서버 모드 필요", "터미널에서 ./forge dashboard를 실행하고 표시된 http://127.0.0.1 주소로 여세요.");
    return;
  }
  Promise.all([loadGitHubStatus(), loadIntegrationCatalog()]).catch((error) => {
    const code = error instanceof Error ? error.message : "unknown_error";
    setGitHubSettingsStatus("error", "설정 조회 실패", `오류 코드: ${code}`);
  });
});
byId("close-settings-button").addEventListener("click", () => byId("settings-dialog").close());
byId("settings-dialog").addEventListener("click", (event) => {
  if (event.target === byId("settings-dialog")) byId("settings-dialog").close();
});
for (const button of document.querySelectorAll("[data-integration-filter]")) {
  button.addEventListener("click", () => setIntegrationFilter(button.dataset.integrationFilter || "all"));
}
byId("github-test-button").addEventListener("click", () => runGitHubConnection("test"));
byId("github-settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  runGitHubConnection("connect");
});
byId("github-sync-button").addEventListener("click", async () => {
  const button = byId("github-sync-button");
  button.disabled = true;
  setGitHubSettingsStatus("loading", "GitHub 증거 동기화 중", "최신 commit, changed files, open PR, exact-commit Actions를 수집합니다.");
  try {
    const evidence = await githubRequest("/api/v1/integrations/github/sync", {});
    renderGitHubEvidence(evidence, "fresh", 0);
    setGitHubSettingsStatus("success", "증거 동기화 완료", `${evidence.head_sha} · ${evidence.evidence_hash}`);
  } catch (error) {
    const code = error instanceof Error ? error.message : "unknown_error";
    setGitHubSettingsStatus("error", "증거 동기화 실패", `오류 코드: ${code}`);
  } finally {
    button.disabled = false;
  }
});
if ("IntersectionObserver" in window) {
  const stageObserver = new IntersectionObserver((entries) => {
    const visible = entries
      .filter((entry) => entry.isIntersecting)
      .sort((left, right) => right.intersectionRatio - left.intersectionRatio);
    const stage = visible[0]?.target.dataset.flowStage;
    if (stage) setWorkflowStage(stage);
  }, { rootMargin: "-18% 0px -64% 0px", threshold: [0.05, 0.25, 0.5] });
  for (const panel of document.querySelectorAll("[data-flow-stage]")) stageObserver.observe(panel);
}
syncAssetKindState();
syncActionState();
syncIntentState();
syncVerifyModeState();
renderGuidedWorkflow();
appendCollaborationMessage("FORGE", "변경 목표를 입력하거나 우선순위를 선택하면 PLAN 초안에 반영합니다. 분석 결과는 CENTER Engineering View에 구조화해 표시합니다.");

if (IS_FILE_PROTOCOL) {
  enterFileProtocolMode();
} else {
  loadIntegrationCatalog().catch(() => undefined);
  loadGitHubStatus().catch(() => undefined);
  loadConnectors()
    .then((connectors) => {
      renderConnectorList(connectors);
      setPanelStatus("connect-status", "success", "Connector 목록 로드", "등록된 읽기 전용 adapter를 선택해 baseline snapshot을 가져올 수 있습니다.");
    })
    .catch((error) => {
      const code = error instanceof Error ? error.message : "unknown_error";
      setPanelStatus("connect-status", "error", "Connector 목록 조회 실패", `오류 코드: ${code}`);
    });

  const fragment = readFragment();
  if (fragment.project && fragment.decision) {
    byId("project-id").value = fragment.project;
    byId("decision-hash").value = fragment.decision;
    if (PROJECT_PATTERN.test(fragment.project) && DECISION_PATTERN.test(fragment.decision)) {
      loadDecision(fragment.project, fragment.decision);
    }
  }
}
