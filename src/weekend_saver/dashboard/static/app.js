// weekend-saver dashboard. Plain DOM, no dependencies. Everything that comes from Azure
// or the API is written with textContent, never innerHTML.

const SVG_NS = "http://www.w3.org/2000/svg";
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const TYPE_LABELS = {
  "microsoft.compute/virtualmachines": "Virtual machine",
  "microsoft.compute/virtualmachinescalesets": "VM scale set",
  "microsoft.containerservice/managedclusters": "AKS cluster",
  "microsoft.web/sites": "App Service",
  "microsoft.dbforpostgresql/flexibleservers": "PostgreSQL server",
  "microsoft.dbformysql/flexibleservers": "MySQL server",
  "microsoft.network/applicationgateways": "Application Gateway",
};
const STEP_STATUS_TEXT = {
  idle: "",
  running: "running…",
  waiting: "needs approval",
  done: "done",
  failed: "failed",
};
const REFRESH_SCHEDULE_MS = 30000;

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else if (value !== false && value != null) node.setAttribute(key, value);
  }
  node.append(...children.filter((child) => child != null));
  return node;
}

function svg(tag, attrs = {}, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text != null) node.textContent = text;
  return node;
}

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Weekend-Saver": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let envelope;
  try {
    envelope = await response.json();
  } catch {
    throw new Error(`The dashboard server did not answer properly (HTTP ${response.status}).`);
  }
  if (!envelope.success) throw new Error(envelope.error || `Request failed (${response.status}).`);
  return envelope.data;
}

function formatWhen(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function typeLabel(type) {
  return TYPE_LABELS[type] || type;
}

function pill(value) {
  return el("span", { class: `pill pill-${value}`, text: value.replace("_", " ") });
}

function summarise(results) {
  const counts = {};
  for (const result of results) counts[result.outcome] = (counts[result.outcome] || 0) + 1;
  return Object.entries(counts)
    .map(([outcome, count]) => `${count} ${outcome}`)
    .join(", ");
}

// ---------- workflow graph ----------

const graph = { nodes: new Map() };

function drawGraph(definition) {
  const width = 1000;
  const nodeWidth = 132;
  const nodeHeight = 56;
  const top = 72;
  const margin = 14;
  const gap = (width - 2 * margin - definition.nodes.length * nodeWidth) / (definition.nodes.length - 1);
  const x = (index) => margin + index * (nodeWidth + gap);
  const indexOf = new Map(definition.nodes.map((node, index) => [node.id, index]));
  const root = $("graph");
  root.replaceChildren();
  root.setAttribute("viewBox", `0 0 ${width} 214`);

  const defs = svg("defs");
  const marker = svg("marker", {
    id: "arrow",
    viewBox: "0 0 10 10",
    refX: "9",
    refY: "5",
    markerWidth: "7",
    markerHeight: "7",
    orient: "auto-start-reverse",
  });
  marker.append(svg("path", { d: "M 0 0 L 10 5 L 0 10 z", class: "arrow" }));
  defs.append(marker);
  root.append(defs);

  for (const edge of definition.edges) {
    const from = indexOf.get(edge.from);
    const to = indexOf.get(edge.to);
    const fromCentre = x(from) + nodeWidth / 2;
    const toCentre = x(to) + nodeWidth / 2;
    let path;
    if (edge.kind === "main") {
      path = `M ${x(from) + nodeWidth} ${top + nodeHeight / 2} L ${x(to) - 2} ${top + nodeHeight / 2}`;
    } else if (edge.kind === "loop") {
      // Back edges arc over the top
      path = `M ${fromCentre} ${top} C ${fromCentre} ${top - 62}, ${toCentre} ${top - 62}, ${toCentre} ${top - 2}`;
      const label = svg(
        "text",
        { x: (fromCentre + toCentre) / 2, y: top - 52, "text-anchor": "middle", class: "edge-label" },
        "approved: plan again"
      );
      root.append(label);
    } else {
      // Early exits arc underneath; longer jumps dip lower so they do not overlap
      const depth = 34 + (to - from) * 14;
      const bottom = top + nodeHeight;
      const landing = toCentre - 30 + from * 12;
      path = `M ${fromCentre} ${bottom} C ${fromCentre} ${bottom + depth}, ${landing} ${bottom + depth}, ${landing} ${bottom + 2}`;
    }
    root.append(svg("path", { d: path, class: `edge edge-${edge.kind}`, "marker-end": "url(#arrow)" }));
  }

  graph.nodes.clear();
  definition.nodes.forEach((node, index) => {
    const group = svg("g", { class: "node idle", "data-step": node.id });
    group.append(svg("title", {}, node.description));
    group.append(svg("rect", { x: x(index), y: top, width: nodeWidth, height: nodeHeight, rx: 8 }));
    group.append(
      svg("text", { x: x(index) + nodeWidth / 2, y: top + 24, "text-anchor": "middle", class: "label" }, node.label)
    );
    const status = svg("text", {
      x: x(index) + nodeWidth / 2,
      y: top + 42,
      "text-anchor": "middle",
      class: "status",
    });
    group.append(status);
    root.append(group);
    graph.nodes.set(node.id, { group, status });
  });
}

function setStep(step, status) {
  const node = graph.nodes.get(step);
  if (!node) return;
  node.group.setAttribute("class", `node ${status}`);
  node.status.textContent = STEP_STATUS_TEXT[status] ?? status;
}

function resetSteps() {
  for (const step of graph.nodes.keys()) setStep(step, "idle");
}

// ---------- runs ----------

let activeRun = null;

function describeRun(run) {
  const kind = run.dry_run ? `preview of ${run.action}` : run.action;
  return `${kind} (${run.trigger.replace("-", " ")})`;
}

function setRunning(run) {
  activeRun = run;
  for (const id of ["run-disable", "run-enable"]) $(id).disabled = Boolean(run);
  if (run) $("run-status").textContent = `Running ${describeRun(run)}…`;
}

async function startRun(action) {
  try {
    await api("/api/runs", {
      method: "POST",
      body: {
        action,
        dry_run: $("dry-run").checked,
        ignore_prior_state: action === "enable" && $("ignore-prior-state").checked,
      },
    });
  } catch (error) {
    $("run-status").textContent = error.message;
  }
}

function resultsTable(items, lastColumn) {
  const body = el("tbody");
  for (const item of items) {
    body.append(
      el(
        "tr",
        {},
        el("td", { class: "cell-name", text: item.name }),
        el("td", {}, pill(item.state)),
        el("td", {}, pill(item.outcome)),
        el("td", { text: item.detail })
      )
    );
  }
  return el(
    "table",
    {},
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        el("th", { text: "Resource" }),
        el("th", { text: "State" }),
        el("th", { text: "Outcome" }),
        el("th", { text: lastColumn })
      )
    ),
    body
  );
}

// "approval" while a run waits for an answer, "record" while showing a finished run
let planDialogMode = null;

function openPlanDialog({ mode, title, summary, bad = false, items, lastColumn, actions }) {
  planDialogMode = mode;
  $("plan-title").textContent = title;
  $("plan-summary").textContent = summary;
  $("plan-summary").className = bad ? "dialog-summary bad" : "dialog-summary";
  $("plan-body").replaceChildren(
    items.length ? resultsTable(items, lastColumn) : el("p", { class: "empty", text: "No resources." })
  );
  $("plan-actions").replaceChildren(...actions);
  const dialog = $("plan-dialog");
  if (!dialog.open) dialog.showModal();
}

function closePlanDialog() {
  planDialogMode = null;
  if ($("plan-dialog").open) $("plan-dialog").close();
}

function closeApprovalDialog() {
  if (planDialogMode === "approval") closePlanDialog();
}

function showApproval(plan, runId) {
  const answer = (approved) => async () => {
    closePlanDialog();
    try {
      await api("/api/runs/approval", { method: "POST", body: { run_id: runId, approved } });
    } catch (error) {
      $("run-status").textContent = error.message;
    }
  };
  openPlanDialog({
    mode: "approval",
    title: `Approve ${plan.action}?`,
    summary: `${plan.actionable} of ${plan.total} resources will change. The plan is checked again against Azure before it runs.`,
    items: plan.items,
    lastColumn: "Why",
    actions: [
      el("button", { class: "button button-danger", text: "Reject", onclick: answer(false) }),
      el("button", { class: "button button-primary", text: `Approve ${plan.action}`, onclick: answer(true) }),
    ],
  });
}

function showRecord(record) {
  const finished = formatWhen(record.finished_at);
  const summary = record.error || `${summarise(record.results) || "No resources"} · ${finished}`;
  openPlanDialog({
    mode: "record",
    title: `${record.dry_run ? "Preview of " : ""}${record.action}: ${record.status.replace("_", " ")}`,
    summary,
    bad: record.failed,
    items: record.results,
    lastColumn: "Detail",
    actions: [el("button", { class: "button button-primary", text: "Close", onclick: closePlanDialog })],
  });
}

let seenFirstSnapshot = false;

function onSnapshot(run) {
  resetSteps();
  setRunning(run);
  if (seenFirstSnapshot) {
    // A reconnect: whatever finished while disconnected is not on screen yet
    loadResources();
    loadHistory();
  }
  seenFirstSnapshot = true;
  if (!run) {
    closeApprovalDialog();
    if ($("run-status").textContent.startsWith("Running")) $("run-status").textContent = "Idle";
    return;
  }
  for (const [step, status] of Object.entries(run.steps)) setStep(step, status);
  if (run.pending_plan) {
    $("run-status").textContent = "Waiting for your approval";
    showApproval(run.pending_plan, run.id);
  } else {
    closeApprovalDialog();
  }
}

function onEvent(event) {
  if (event.type === "snapshot") {
    onSnapshot(event.run);
  } else if (event.type === "run_started") {
    resetSteps();
    setRunning(event.run);
  } else if (event.type === "step") {
    setStep(event.step, event.status);
  } else if (event.type === "approval_required") {
    setStep("approval", "waiting");
    $("run-status").textContent = "Waiting for your approval";
    showApproval(event.plan, event.run_id);
  } else if (event.type === "run_finished") {
    const record = event.run;
    const wasManual = record.trigger === "manual";
    setRunning(null);
    // Leave a finished run someone is reading alone; only a stale approval goes away
    closeApprovalDialog();
    if (record.status === "crashed") for (const [step, node] of graph.nodes) {
      if (node.group.classList.contains("running")) setStep(step, "failed");
    }
    $("run-status").textContent =
      `Last run: ${describeRun(record)} ${record.failed ? "FAILED" : record.status.replace("_", " ")} · ${formatWhen(record.finished_at)}`;
    if (wasManual) showRecord(record);
    loadResources();
    loadHistory();
  }
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = (message) => onEvent(JSON.parse(message.data));
  source.onerror = () => {
    // EventSource reconnects by itself; the snapshot it gets on reconnect restores the view
    if (!activeRun) $("run-status").textContent = "Reconnecting to the dashboard server…";
  };
  source.onopen = () => {
    if (!activeRun && $("run-status").textContent.startsWith("Reconnecting")) {
      $("run-status").textContent = "Idle";
    }
  };
}

// ---------- managed resources ----------

function emptyState(message, { bad = false, action } = {}) {
  return el("div", { class: bad ? "empty empty-bad" : "empty" }, el("p", { text: message }), action);
}

function flags(row) {
  const badges = [];
  if (row.disabled_by_us) badges.push(el("span", { class: "badge", text: "disabled by weekend-saver" }));
  if (row.skip) badges.push(el("span", { class: "badge", text: "skip tag" }));
  if (row.error) badges.push(el("span", { class: "row-error", text: `could not read: ${row.error}` }));
  return badges;
}

async function loadResources() {
  const body = $("resources-body");
  if (!body.querySelector("table")) body.replaceChildren(emptyState("Loading resources…"));
  let rows;
  try {
    rows = await api("/api/resources");
  } catch (error) {
    body.replaceChildren(
      emptyState(error.message, {
        bad: true,
        action: el("button", { class: "button", text: "Try again", onclick: loadResources }),
      })
    );
    return;
  }
  $("resource-count").textContent = rows.length ? `(${rows.length})` : "";
  if (!rows.length) {
    body.replaceChildren(
      emptyState("Nothing is managed yet. Choose the resources weekend-saver may stop and start.", {
        action: el("button", { class: "button button-primary", text: "Manage resources", onclick: openPicker }),
      })
    );
    return;
  }
  const tbody = el("tbody");
  for (const row of rows) {
    tbody.append(
      el(
        "tr",
        {},
        el("td", { class: "cell-name", text: row.name }, el("span", { class: "cell-sub", text: typeLabel(row.type) })),
        el("td", { text: row.resource_group }),
        el("td", {}, pill(row.state)),
        el("td", { class: "cell-num", text: String(row.order) }),
        el("td", {}, ...flags(row))
      )
    );
  }
  body.replaceChildren(
    el(
      "table",
      {},
      el(
        "thead",
        {},
        el(
          "tr",
          {},
          el("th", { text: "Resource" }),
          el("th", { text: "Resource group" }),
          el("th", { text: "Power state" }),
          el("th", { text: "Order" }),
          el("th", { text: "Notes" })
        )
      ),
      tbody
    )
  );
}

// ---------- resource picker ----------

// Selectable rows; unsupported resources are shown greyed out and tracked separately
let pickerRows = [];
let pickerGreyedRows = [];
let managedWhenPickerOpened = 0;

function updatePickerCount() {
  const selected = pickerRows.filter((row) => row.checkbox.checked).length;
  const greyed = pickerGreyedRows.length
    ? ` · ${pickerGreyedRows.length} greyed out cannot be stopped and started`
    : "";
  $("picker-count").textContent = pickerRows.length
    ? `${selected} of ${pickerRows.length} selected · lower order is disabled first and enabled last${greyed}`
    : "Nothing here can be stopped and started by weekend-saver.";
}

function greyedPickerRow(resource, group) {
  const checkbox = el("input", { type: "checkbox", disabled: true, "aria-label": `${resource.name} is not supported` });
  const node = el(
    "div",
    { class: "picker-row picker-row-unsupported", title: "weekend-saver has no way to stop and start this type" },
    checkbox,
    el("span", { class: "cell-name", text: resource.name }, el("span", { class: "cell-sub", text: typeLabel(resource.resource_type) })),
    el("span", { class: "hint", text: resource.location }),
    el("span", { class: "hint", text: "not supported" })
  );
  return { node, haystack: `${resource.name} ${group} ${resource.resource_type}`.toLowerCase() };
}

function setPickerReady(ready) {
  // Saving before the list has loaded would write an empty selection
  $("picker-save").disabled = !ready;
}

function filterPicker() {
  const needle = $("picker-search").value.trim().toLowerCase();
  for (const row of [...pickerRows, ...pickerGreyedRows]) {
    row.node.hidden = Boolean(needle) && !row.haystack.includes(needle);
  }
}

async function openPicker() {
  const body = $("picker-body");
  $("picker-search").value = "";
  pickerRows = [];
  pickerGreyedRows = [];
  setPickerReady(false);
  body.replaceChildren(emptyState("Looking up the resources your account can see…"));
  $("picker-count").textContent = "";
  $("picker-dialog").showModal();
  let found;
  try {
    found = await api("/api/discovery");
  } catch (error) {
    body.replaceChildren(emptyState(error.message, { bad: true }));
    return;
  }
  if (!found.length) {
    body.replaceChildren(
      emptyState("No resources were found in the subscriptions this account can see.")
    );
    return;
  }
  managedWhenPickerOpened = found.filter((resource) => resource.managed).length;
  // With nothing selectable there is nothing to save
  setPickerReady(found.some((resource) => resource.supported));
  body.replaceChildren();
  let currentGroup = "";
  for (const resource of found) {
    const group = resource.found
      ? `${resource.subscription_name} / ${resource.resource_group}`
      : "Managed, but not visible to this account right now";
    if (group !== currentGroup) {
      body.append(el("div", { class: "picker-group", text: group }));
      currentGroup = group;
    }
    if (!resource.supported) {
      const greyed = greyedPickerRow(resource, group);
      body.append(greyed.node);
      pickerGreyedRows.push(greyed);
      continue;
    }
    const checkbox = el("input", { type: "checkbox", "aria-label": `Manage ${resource.name}` });
    checkbox.checked = resource.managed;
    const order = el("input", {
      type: "number",
      min: "-1000",
      max: "1000",
      step: "1",
      "aria-label": `Order for ${resource.name}`,
    });
    order.value = String(resource.order);
    order.disabled = !resource.managed;
    checkbox.addEventListener("change", () => {
      order.disabled = !checkbox.checked;
      updatePickerCount();
    });
    const node = el(
      "label",
      { class: "picker-row" },
      checkbox,
      el("span", { class: "cell-name", text: resource.name }, el("span", { class: "cell-sub", text: typeLabel(resource.resource_type) })),
      el("span", { class: "hint", text: resource.location }),
      order
    );
    body.append(node);
    pickerRows.push({
      node,
      checkbox,
      order,
      id: resource.resource_id,
      haystack: `${resource.name} ${group} ${typeLabel(resource.resource_type)}`.toLowerCase(),
    });
  }
  updatePickerCount();
}

async function savePicker() {
  const resources = pickerRows
    .filter((row) => row.checkbox.checked)
    .map((row) => ({ id: row.id, order: Number.parseInt(row.order.value, 10) || 0 }));
  const clearing = resources.length === 0 && managedWhenPickerOpened > 0;
  if (clearing && !window.confirm("Stop managing every resource? Scheduled runs will then do nothing.")) {
    return;
  }
  try {
    await api("/api/resources", { method: "PUT", body: { resources } });
  } catch (error) {
    $("picker-count").textContent = error.message;
    return;
  }
  $("picker-dialog").close();
  loadResources();
}

// ---------- schedule ----------

function fillWeekdays(select) {
  WEEKDAYS.forEach((name, index) => select.append(el("option", { value: String(index), text: name })));
}

function renderSchedule(schedule) {
  $("schedule-enabled").checked = schedule.enabled;
  $("disable-weekday").value = String(schedule.disable_at.weekday);
  $("disable-time").value = schedule.disable_at.time;
  $("enable-weekday").value = String(schedule.enable_at.weekday);
  $("enable-time").value = schedule.enable_at.time;
  $("next-run").textContent = schedule.enabled
    ? `Next: disable ${formatWhen(schedule.next.disable)} · enable ${formatWhen(schedule.next.enable)}`
    : "Schedule is off";
  $("notices").replaceChildren(
    ...schedule.notices.map((notice) => el("div", { class: "notice", text: notice }))
  );
}

async function loadSchedule({ keepForm = false } = {}) {
  try {
    const schedule = await api("/api/schedule");
    if (keepForm) {
      // Periodic refresh: update the header and notices without clobbering unsaved edits
      const form = readScheduleForm();
      renderSchedule(schedule);
      writeScheduleForm(form);
    } else {
      renderSchedule(schedule);
    }
  } catch (error) {
    $("next-run").textContent = error.message;
  }
}

function readScheduleForm() {
  return {
    enabled: $("schedule-enabled").checked,
    disable_at: { weekday: Number($("disable-weekday").value), time: $("disable-time").value },
    enable_at: { weekday: Number($("enable-weekday").value), time: $("enable-time").value },
  };
}

function writeScheduleForm(form) {
  $("schedule-enabled").checked = form.enabled;
  $("disable-weekday").value = String(form.disable_at.weekday);
  $("disable-time").value = form.disable_at.time;
  $("enable-weekday").value = String(form.enable_at.weekday);
  $("enable-time").value = form.enable_at.time;
}

async function saveSchedule(event) {
  event.preventDefault();
  const message = $("schedule-message");
  try {
    renderSchedule(await api("/api/schedule", { method: "PUT", body: readScheduleForm() }));
    message.className = "form-message";
    message.textContent = "Saved.";
  } catch (error) {
    message.className = "form-message bad";
    message.textContent = error.message;
  }
}

// ---------- history ----------

async function loadHistory() {
  const list = $("history");
  let runs;
  try {
    runs = (await api("/api/runs")).recent;
  } catch (error) {
    list.replaceChildren(el("li", { class: "empty empty-bad", text: error.message }));
    return;
  }
  if (!runs.length) {
    list.replaceChildren(el("li", { class: "empty", text: "No runs yet." }));
    return;
  }
  list.replaceChildren(
    ...runs.map((record) =>
      el(
        "li",
        {},
        el(
          "button",
          { class: "history-item", onclick: () => showRecord(record) },
          el("span", { class: "history-title", text: `${record.dry_run ? "preview " : ""}${record.action}` }),
          pill(record.failed ? "failed" : record.status),
          el("span", {
            class: "history-meta",
            text: `${formatWhen(record.finished_at)} · ${record.trigger.replace("-", " ")} · ${summarise(record.results) || record.error || "nothing to do"}`,
          })
        )
      )
    )
  );
}

// ---------- sign-in ----------

function renderAuth(status) {
  $("demo-badge").hidden = !status.demo;
  $("sign-in").hidden = status.signed_in;
  $("account-name").textContent = status.signed_in
    ? `${status.account || "Signed in"}${status.source ? ` · ${status.source}` : ""}`
    : "Not signed in to Azure";
}

async function loadAuth() {
  try {
    renderAuth(await api("/api/auth"));
  } catch (error) {
    $("account-name").textContent = error.message;
  }
}

async function signIn() {
  const button = $("sign-in");
  button.disabled = true;
  $("account-name").textContent = "Finish signing in in the browser window that opened…";
  try {
    renderAuth(await api("/api/auth/login", { method: "POST" }));
    loadResources();
  } catch (error) {
    $("account-name").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

// ---------- start ----------

async function init() {
  fillWeekdays($("disable-weekday"));
  fillWeekdays($("enable-weekday"));
  $("run-disable").addEventListener("click", () => startRun("disable"));
  $("run-enable").addEventListener("click", () => startRun("enable"));
  $("refresh").addEventListener("click", loadResources);
  $("manage").addEventListener("click", openPicker);
  $("picker-cancel").addEventListener("click", () => $("picker-dialog").close());
  // Esc must not dismiss a pending approval: the run would keep waiting with no way back
  $("plan-dialog").addEventListener("cancel", (event) => {
    if (planDialogMode === "approval") event.preventDefault();
    else planDialogMode = null;
  });
  $("picker-save").addEventListener("click", savePicker);
  $("picker-search").addEventListener("input", filterPicker);
  $("schedule-form").addEventListener("submit", saveSchedule);
  $("sign-in").addEventListener("click", signIn);

  try {
    drawGraph(await api("/api/workflow"));
  } catch (error) {
    $("run-status").textContent = error.message;
  }
  connectEvents();
  loadAuth();
  loadResources();
  loadSchedule();
  loadHistory();
  setInterval(() => loadSchedule({ keepForm: true }), REFRESH_SCHEDULE_MS);
}

init();
