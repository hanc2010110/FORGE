const PROJECT_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const DECISION_PATTERN = /^sha256:[0-9a-f]{64}$/;
const TIERS = [
  ["static", "Static", "BOM·build·contract"],
  ["simulation", "Simulation", "모델 기반 결과"],
  ["bench", "Bench", "fixture 기반 bench"],
  ["hil", "HIL", "hardware-in-the-loop"],
  ["physical_device", "Physical device", "실제 장치 instance"],
];

const byId = (id) => document.getElementById(id);

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

function setLoadStatus(kind, title, copy) {
  const status = byId("load-status");
  status.className = `load-status${kind === "error" ? " error" : ""}`;
  replace(status, element("strong", "", title), element("span", "", copy));
}

function renderMetrics(assessment, report) {
  const values = [
    ["Revision", `${assessment.from_hardware_revision_id} → ${assessment.to_hardware_revision_id}`],
    ["Artifact changes", assessment.artifact_changes?.length || 0],
    ["Blockers", report.blocker_codes?.length || 0],
    ["Accepted evidence", report.evidence?.length || 0],
  ];
  const nodes = values.map(([label, value]) => {
    const wrapper = element("div");
    wrapper.append(element("dt", "", label), element("dd", "", value));
    return wrapper;
  });
  replace(byId("metrics"), ...nodes);
}

function renderSummary(report) {
  const statusValue = display(report.status, "unknown").toLowerCase();
  const summary = byId("decision-summary");
  summary.className = `decision-summary ${statusValue}`;
  const status = byId("decision-status");
  status.className = `status-chip ${statusValue}`;
  status.textContent = statusValue.toUpperCase();
  byId("evaluated-at").textContent = formatTime(report.evaluated_at);
  byId("summary-copy").textContent = statusValue === "ready"
    ? "필수 finding과 retest가 제출된 근거로 해소되었습니다. 이는 인증이나 생산 승인이 아닙니다."
    : "하나 이상의 blocker 또는 미충족 retest가 남아 있어 출시 준비가 증명되지 않았습니다.";

  const box = byId("blocker-summary");
  const heading = element("h3", "", report.blocker_codes?.length ? "Blocking reasons" : "Gate result");
  const list = element("ul");
  const reasons = report.blocker_codes?.length ? report.blocker_codes : ["no_unresolved_blockers"];
  for (const reason of reasons) list.append(element("li", "", reason));
  replace(box, heading, list);
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

const form = byId("decision-form");
form.addEventListener("submit", (event) => {
  event.preventDefault();
  const project = byId("project-id").value.trim();
  const decision = byId("decision-hash").value.trim();
  if (!PROJECT_PATTERN.test(project) || !DECISION_PATTERN.test(decision)) {
    form.reportValidity();
    setLoadStatus("error", "입력 형식 확인", "안전한 project ID와 canonical sha256 decision hash가 필요합니다.");
    return;
  }
  loadDecision(project, decision);
});

const fragment = readFragment();
if (fragment.project && fragment.decision) {
  byId("project-id").value = fragment.project;
  byId("decision-hash").value = fragment.decision;
  if (PROJECT_PATTERN.test(fragment.project) && DECISION_PATTERN.test(fragment.decision)) {
    loadDecision(fragment.project, fragment.decision);
  }
}
