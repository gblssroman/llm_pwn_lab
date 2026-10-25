const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  conversations: [],
  selectedId: null,
  conversation: null,
  messages: [],
  events: [],
  snapshots: [],
  langfuse: null,
  benchmark: null,
  scenarios: [],
  vmMaster: null,
  vmPool: [],
  vmPoolSubmitting: false,
  activeView: "relay",
  eventSource: null,
  liveDraft: null,
  followLiveOutput: true,
  activeTab: "details",
};

const terminalStatuses = new Set(["stopped", "timed_out", "completed", "failed"]);
let conversationRefreshTimer = null;
let langfuseRefreshTimer = null;
let programmaticMessageScroll = false;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body);
    } catch (_) { /* response was not JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

function shortId(id) { return id ? id.slice(0, 8) : "—"; }
function compact(text, max = 74) { return text.length > max ? `${text.slice(0, max)}…` : text; }
function fmtDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("ru-RU", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}
function fmtDateTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("ru-RU", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  }).format(new Date(value));
}
function fmtDuration(ms) {
  if (ms == null) return "—";
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}
function fmtNumber(value) { return value == null ? "—" : new Intl.NumberFormat("ru-RU").format(value); }
function statusLabel(status) { return String(status || "unknown").replaceAll("_", " "); }
function fmtMemory(memoryMb) {
  return `${new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 }).format(memoryMb / 1024)} GB`;
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text != null) element.textContent = text;
  return element;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 2600);
}

async function checkHealth() {
  const [backend, vm] = await Promise.allSettled([
    api("/health/ready"),
    api("/v1/vm-master/health"),
  ]);
  if (backend.status === "fulfilled") {
    $("#database-status").className = "status-square " + backend.value.database;
    $("#langfuse-status").className = "status-square " + backend.value.langfuse;
  } else {
    $("#database-status").className = "status-square unknown";
    $("#langfuse-status").className = "status-square unknown";
  }
  if (vm.status === "fulfilled") {
    state.vmMaster = vm.value;
    $("#vm-master-status").className = "status-square " + vm.value.status;
  } else {
    state.vmMaster = null;
    $("#vm-master-status").className = "status-square unknown";
  }
}

function setVmPoolControls() {
  const disabled = state.vmPoolSubmitting;
  $("#warm-all-vms").disabled = disabled;
  $("#shutdown-all-vms").disabled = disabled;
  $("#refresh-vm-pool").disabled = disabled;
  $$("[data-vm-pool-action]").forEach((button) => { button.disabled = disabled; });
}

function renderVmPool() {
  const activeStates = new Set(["warming", "ready", "in_use", "resetting"]);
  const ready = state.vmPool.filter((item) => item.state === "ready").length;
  const activeMemory = state.vmPool
    .filter((item) => activeStates.has(item.state))
    .reduce((total, item) => total + item.memory_mb, 0);
  const desiredWarm = state.vmPool.filter((item) => item.desired === "warm").length;
  const summary = $("#vm-pool-summary");
  summary.replaceChildren();
  for (const [label, value] of [
    ["Ready", `${ready}/${state.vmPool.length}`],
    ["Active guest RAM", fmtMemory(activeMemory)],
    ["Keep warm", String(desiredWarm)],
  ]) {
    const card = node("div", "stat");
    card.append(node("span", "", label), node("strong", "", value));
    summary.append(card);
  }

  const grid = $("#vm-pool-grid");
  grid.replaceChildren();
  if (!state.vmPool.length) {
    grid.append(node("div", "vm-pool-empty", "No scenario VMs found."));
    setVmPoolControls();
    return;
  }

  for (const item of state.vmPool) {
    const card = node("article", "vm-pool-card");
    const head = node("div", "vm-pool-card-head");
    const title = node("div");
    title.append(
      node("h2", "", item.title),
      node("div", "message-meta", `${item.scenario_id} · ${item.mode}`),
    );
    head.append(title, node("span", `vm-pool-state ${item.state}`, statusLabel(item.state)));
    card.append(head);

    const meta = node("div", "vm-pool-meta");
    meta.append(
      node("span", "", `Desired: ${item.desired}`),
      node("span", "", fmtMemory(item.memory_mb)),
    );
    card.append(meta);
    if (item.error) card.append(node("pre", "vm-pool-error", item.error));

    let action = item.desired === "warm" ? "shutdown" : "warm";
    if (["error", "dirty"].includes(item.state)) action = "warm";
    let label = action === "warm" ? "Turn on" : "Shutdown";
    if (item.state === "error") label = "Retry warm-up";
    if (item.state === "dirty") label = "Reset & warm";
    if (item.state === "in_use" && action === "shutdown") label = "Shutdown after run";
    const actions = node("div", "vm-pool-card-actions");
    const button = node("button", action === "warm" ? "primary-button" : "danger-button", label);
    button.type = "button";
    button.dataset.vmPoolAction = action;
    button.addEventListener("click", () => changeVmPool(`/v1/vm-pool/${item.scenario_id}/${action}`));
    actions.append(button);
    card.append(actions);
    grid.append(card);
  }
  setVmPoolControls();
}

async function refreshVmPool() {
  if (state.vmPoolSubmitting) return;
  const errorBox = $("#vm-pool-error");
  try {
    state.vmPool = await api("/v1/vm-pool");
    errorBox.classList.add("hidden");
    renderVmPool();
  } catch (error) {
    errorBox.textContent = `Cannot load VM pool: ${error.message}`;
    errorBox.classList.remove("hidden");
  }
}

async function changeVmPool(path) {
  state.vmPoolSubmitting = true;
  setVmPoolControls();
  const errorBox = $("#vm-pool-error");
  try {
    const result = await api(path, { method: "POST", body: "{}" });
    if (Array.isArray(result)) {
      state.vmPool = result;
    } else {
      state.vmPool = state.vmPool.map((item) => (
        item.scenario_id === result.scenario_id ? result : item
      ));
    }
    errorBox.classList.add("hidden");
    renderVmPool();
  } catch (error) {
    errorBox.textContent = `VM pool action failed: ${error.message}`;
    errorBox.classList.remove("hidden");
  } finally {
    state.vmPoolSubmitting = false;
    setVmPoolControls();
  }
}

function selectWorkspace(view) {
  state.activeView = view;
  const manager = view === "manager";
  $("#agent-relay-view").classList.toggle("hidden", manager);
  $("#vm-manager-view").classList.toggle("hidden", !manager);
  $("#agent-relay-tab").classList.toggle("active", !manager);
  $("#vm-manager-tab").classList.toggle("active", manager);
  $("#agent-relay-tab").setAttribute("aria-selected", String(!manager));
  $("#vm-manager-tab").setAttribute("aria-selected", String(manager));
  if (manager) refreshVmPool();
}

function selectedScenario() {
  return state.scenarios.find((item) => item.id === $("#scenario-id").value) || null;
}

function scenarioModeLabel(scenario) {
  if (scenario.mode !== "agent") return scenario.mode;
  const owasp = (scenario.category || "").match(/(?:^|;)\s*OWASP(?:\s+laboratory)?\s*:\s*([^;]+)/i);
  return owasp ? "agent · OWASP: " + owasp[1].trim() : scenario.mode;
}

function populateScenarioPrompts() {
  const scenario = selectedScenario();
  if (!scenario || $("#run-type").value !== "benchmark") return;
  $("#agent-a-system-prompt").value = scenario.prompt_a;
  $("#agent-b-system-prompt").value = scenario.prompt_b;
}

function renderScenarioPreview() {
  const preview = $("#scenario-preview");
  const scenario = selectedScenario();
  if (!scenario) {
    preview.textContent = state.scenarios.length
      ? "Select a scenario."
      : "VM master is unavailable or not configured.";
    return;
  }
  preview.replaceChildren(
    node("strong", "", scenario.title + " · " + scenarioModeLabel(scenario)),
    node("div", "message-meta", scenario.category),
    node("p", "", "The scenario prompts are loaded below and can be edited before the run."),
  );
}

async function loadScenarios() {
  const select = $("#scenario-id");
  try {
    state.scenarios = await api("/v1/benchmark-scenarios");
    select.replaceChildren(...state.scenarios.map((scenario) => {
      const option = node("option", "", "[" + scenarioModeLabel(scenario) + "] " + scenario.title);
      option.value = scenario.id;
      return option;
    }));
    select.disabled = false;
  } catch (error) {
    state.scenarios = [];
    select.replaceChildren();
    select.disabled = true;
    $("#scenario-preview").textContent = "Cannot load scenarios: " + error.message;
  }
  renderScenarioPreview();
  populateScenarioPrompts();
  renderConversationList();
}

function toggleRunType() {
  const benchmark = $("#run-type").value === "benchmark";
  $("#benchmark-settings").classList.toggle("hidden", !benchmark);
  $("#scenario-id").disabled = !benchmark || !state.scenarios.length;
  $("#start-title").textContent = benchmark
    ? "Start VM benchmark"
    : "Start agent conversation";
  $("#submit-conversation").textContent = benchmark
    ? "Start benchmark"
    : "Start conversation";
  const timeout = $('#start-form [name="timeout_seconds"]');
  if (benchmark && timeout.value === "1800") timeout.value = "3600";
  if (!benchmark && timeout.value === "3600") timeout.value = "1800";
}

async function loadConversations({ selectFirst = false } = {}) {
  const filter = $("#status-filter").value;
  const query = new URLSearchParams({ limit: "200" });
  if (filter) query.set("status", filter);
  const page = await api(`/v1/conversations?${query}`);
  state.conversations = page.items;
  renderConversationList();
  if (selectFirst && !state.selectedId && page.items.length) selectConversation(page.items[0].id);
}

function scheduleConversationRefresh() {
  if (conversationRefreshTimer) return;
  conversationRefreshTimer = setTimeout(() => {
    conversationRefreshTimer = null;
    loadConversations().catch(() => {});
  }, 1000);
}

function scheduleLangfuseRefresh() {
  if (langfuseRefreshTimer) return;
  langfuseRefreshTimer = setTimeout(() => {
    langfuseRefreshTimer = null;
    if (state.selectedId) loadLangfuse(state.selectedId);
  }, 1500);
}

function renderConversationList() {
  const list = $("#conversation-list");
  const search = $("#conversation-search").value.trim().toLowerCase();
  const scenarioFor = (item) => state.scenarios.find((scenario) => {
    if (item.benchmark_scenario_id) return scenario.id === item.benchmark_scenario_id;
    const prompt = item.agent_a_system_prompt.trim();
    const scenarioPrompt = scenario.prompt_a.trim();
    return prompt === scenarioPrompt;
  });
  const items = state.conversations.filter((item) => {
    if (!search) return true;
    const scenario = scenarioFor(item);
    return item.agent_a_system_prompt.toLowerCase().includes(search)
      || item.id.includes(search)
      || scenario?.id.includes(search)
      || scenario?.title.toLowerCase().includes(search);
  });
  list.replaceChildren();
  if (!items.length) {
    list.append(node("div", "empty-list", "No conversations found."));
    return;
  }
  for (const item of items) {
    const card = node("article", `conversation-card${item.id === state.selectedId ? " active" : ""}`);
    card.dataset.id = item.id;
    const top = node("div", "conversation-card-top");
    top.append(node("span", `status-pill ${item.status}`, statusLabel(item.status)), node("span", "message-meta", `${item.turn_count} turns`));
    const scenario = scenarioFor(item);
    const title = scenario?.title || compact(item.agent_a_system_prompt);
    card.append(top, node("div", "conversation-card-title", title));
    const meta = node("div", "conversation-card-meta");
    meta.append(node("span", "", fmtDateTime(item.created_at)), node("span", "", shortId(item.id)));
    card.append(meta);
    card.addEventListener("click", () => selectConversation(item.id));
    list.append(card);
  }
}

async function fetchAllMessages(id) {
  const messages = [];
  let after = 0;
  while (true) {
    const page = await api(`/v1/conversations/${id}/messages?after_sequence=${after}&limit=1000`);
    messages.push(...page.items);
    if (!page.next_after_sequence) return messages;
    after = page.next_after_sequence;
  }
}

async function fetchBenchmark(id) {
  try {
    return await api("/v1/benchmark-runs/" + id);
  } catch (error) {
    if (error.message.startsWith("404 ")) return null;
    throw error;
  }
}

async function selectConversation(id) {
  if (state.selectedId === id && state.conversation) return;
  closeEventSource();
  state.selectedId = id;
  state.liveDraft = null;
  renderConversationList();
  setStreamState("connecting", "connecting");
  try {
    const [conversation, messages, events, snapshots, benchmark] = await Promise.all([
      api(`/v1/conversations/${id}`),
      fetchAllMessages(id),
      api(`/v1/conversations/${id}/events?limit=5000`),
      api(`/v1/conversations/${id}/context-snapshots`),
      fetchBenchmark(id),
    ]);
    if (state.selectedId !== id) return;
    state.conversation = conversation;
    state.messages = messages;
    state.events = events;
    state.snapshots = snapshots;
    state.benchmark = benchmark;
    state.langfuse = null;
    renderAll();
    connectEventSource(id);
    loadLangfuse(id);
  } catch (error) {
    showToast(`Cannot load conversation: ${error.message}`);
    setStreamState("offline", "error");
  }
}

function renderAll() {
  $("#empty-state").classList.add("hidden");
  $("#conversation-view").classList.remove("hidden");
  renderHeader();
  renderActivity();
  renderStats();
  renderMessages();
  renderInspector();
  renderConversationList();
}

function renderHeader() {
  const convo = state.conversation;
  if (!convo) return;
  const benchmark = state.benchmark;
  const agentAPrompt = convo.config.agent_a.system_prompt;
  $("#conversation-title").textContent = compact(agentAPrompt, 92);
  $("#conversation-meta").textContent = benchmark
    ? shortId(convo.id) + " · " + benchmark.scenario_id + " · " + statusLabel(benchmark.stage)
    : shortId(convo.id) + " · " + statusLabel(convo.status) + " · next " + convo.next_speaker;
  const isTerminal = terminalStatuses.has(convo.status);
  $("#stop-conversation").classList.toggle("hidden", Boolean(benchmark) || isTerminal);
  $("#finish-benchmark").classList.toggle(
    "hidden",
    !benchmark || benchmark.stage !== "conversation",
  );
  $("#abort-benchmark").classList.toggle(
    "hidden",
    !benchmark || !["queued", "provisioning", "conversation"].includes(benchmark.stage),
  );
  const link = $("#langfuse-link");
  link.classList.toggle("hidden", !convo.langfuse_url);
  if (convo.langfuse_url) link.href = convo.langfuse_url;
}

function openPromptDialog(agentKey) {
  const convo = state.conversation;
  if (!convo) return;
  const agentName = agentKey === "agent_a" ? "Agent A" : "Agent B";
  $("#prompt-dialog-title").textContent = `${agentName} system prompt`;
  const content = $("#prompt-dialog-content");
  content.textContent = convo.config[agentKey].system_prompt;
  content.scrollTop = 0;
  $("#prompt-dialog").showModal();
}

function closePromptDialog() {
  $("#prompt-dialog").close();
}

function renderStats() {
  const totalTokens = state.messages.reduce((sum, item) => sum + (item.prompt_tokens || 0) + (item.completion_tokens || 0), 0);
  const avgLatency = state.messages.length ? Math.round(state.messages.reduce((sum, item) => sum + item.latency_ms, 0) / state.messages.length) : null;
  const streamed = state.messages.filter((item) => item.streamed).length;
  const stats = [
    ["Turns", state.messages.length],
    ["Total tokens", fmtNumber(totalTokens)],
    ["Avg latency", fmtDuration(avgLatency)],
    ["Streamed", `${streamed}/${state.messages.length}`],
  ];
  const row = $("#stats-row"); row.replaceChildren();
  for (const [label, value] of stats) {
    const card = node("div", "stat"); card.append(node("span", "", label), node("strong", "", String(value))); row.append(card);
  }
}

function cleanActivityText(value) {
  return String(value || "")
    .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, "")
    .replaceAll("\r", "")
    .trim();
}

function latestVmLog(action) {
  return [...state.events].reverse().find(
    (event) => event.event_type === "vm_job_log" && event.payload?.action === action,
  );
}

function activeToolCall() {
  const event = [...state.events].reverse().find(
    (item) => ["tool_started", "tool_completed"].includes(item.event_type),
  );
  return event?.event_type === "tool_started" ? event.payload : null;
}

function verificationInProgress() {
  const event = [...state.events].reverse().find(
    (item) => [
      "benchmark_verification_started",
      "benchmark_progress_checked",
      "benchmark_progress_check_failed",
      "benchmark_verified",
      "benchmark_evaluation_failed",
    ].includes(item.event_type),
  );
  return event?.event_type === "benchmark_verification_started";
}

function currentActivity() {
  const convo = state.conversation;
  const run = state.benchmark;
  if (!convo) return { tone: "idle", title: "No conversation selected", detail: "" };

  const tool = activeToolCall();
  if (tool) {
    const command = cleanActivityText(tool.arguments?.command);
    return { tone: "active", title: `Running ${tool.name}`, detail: command || `Tool call ${tool.ordinal}` };
  }

  if (state.liveDraft) {
    const agent = state.liveDraft.speaker === "agent_a" ? "Agent A" : "Agent B";
    return { tone: "active", title: `${agent} is generating`, detail: state.liveDraft.model || "Waiting for model output" };
  }

  if (run?.stage === "conversation" && verificationInProgress()) {
    return { tone: "active", title: "Checking benchmark", detail: "Running scenario checks after Agent B turn" };
  }

  if (run) {
    if (run.stage === "queued") {
      return { tone: "active", title: "Waiting for VM", detail: "Queued for exclusive VM access" };
    }
    if (run.stage === "provisioning") {
      const detail = cleanActivityText(latestVmLog("up")?.payload?.text);
      return { tone: "active", title: "Preparing VM", detail: detail || "Restoring the clean snapshot and provisioning the scenario" };
    }
    if (run.stage === "conversation") {
      return { tone: "active", title: "Conversation active", detail: `Waiting for ${convo.next_speaker === "agent_a" ? "Agent A" : "Agent B"} · turn ${convo.turn_count + 1}` };
    }
    if (run.stage === "evaluating") {
      return { tone: "active", title: "Evaluating benchmark", detail: "Running scenario checks" };
    }
    if (run.stage === "cleaning_up") {
      const detail = cleanActivityText(latestVmLog("down")?.payload?.text);
      return { tone: "active", title: "Cleaning up VM", detail: detail || "Restoring the clean snapshot and powering off the VM" };
    }
    if (["failed", "cleanup_failed"].includes(run.stage)) {
      return { tone: "error", title: "Benchmark failed", detail: run.error || run.cleanup_error || "See benchmark details" };
    }
    if (run.stage === "aborted") {
      return { tone: "idle", title: "Benchmark aborted", detail: "VM cleanup completed" };
    }
    if (run.stage === "completed") {
      return { tone: "done", title: "Benchmark completed", detail: run.verdict ? `Passed: ${run.verdict.passed === true ? "yes" : "no"}` : "Run finished" };
    }
  }

  if (convo.status === "running") {
    return { tone: "active", title: "Conversation active", detail: `Waiting for ${convo.next_speaker === "agent_a" ? "Agent A" : "Agent B"} · turn ${convo.turn_count + 1}` };
  }
  if (convo.status === "failed") return { tone: "error", title: "Conversation failed", detail: convo.error || "See run details" };
  if (convo.status === "completed") return { tone: "done", title: "Conversation completed", detail: `${convo.turn_count} turns` };
  return { tone: "idle", title: statusLabel(convo.status), detail: "" };
}

function renderActivity() {
  const activity = currentActivity();
  const row = $("#activity-row");
  row.className = `activity-row ${activity.tone}`;
  const copy = node("div", "activity-copy");
  copy.append(node("span", "", "Current activity"), node("strong", "", activity.title));
  if (activity.detail) copy.append(node("small", "", activity.detail));
  row.replaceChildren(node("i", "activity-dot"), copy);
}

function messageElement(message, live = false) {
  const article = node("article", `message ${message.speaker}${live ? " live" : ""}`);
  article.dataset.sequence = message.sequence;
  article.append(node("div", "avatar", message.speaker === "agent_a" ? "A" : "B"));
  const body = node("div", "message-body");
  const head = node("div", "message-head");
  head.append(node("span", "message-name", message.speaker === "agent_a" ? "Agent A" : "Agent B"), node("span", "message-meta", `#${message.sequence} · ${message.model || "generating"}`));
  const content = node("div", "message-content");
  if (live || !globalThis.AgentRelayMarkdown) {
    content.textContent = message.content || "";
  } else {
    content.innerHTML = globalThis.AgentRelayMarkdown.render(message.content || "");
  }
  body.append(head, content);
  if (!live) {
    const metrics = node("div", "message-metrics");
    metrics.append(
      node("span", "", fmtDuration(message.latency_ms)),
      node("span", "", `TTFT ${fmtDuration(message.first_token_ms)}`),
      node("span", "", `${fmtNumber(message.prompt_tokens)} in`),
      node("span", "", `${fmtNumber(message.completion_tokens)} out`),
      node("span", "", message.streamed ? "stream" : "response"),
      node("span", "", `${message.attempts} API request${message.attempts === 1 ? "" : "s"} incl. tool steps/retries`),
    );
    body.append(metrics);
  }
  article.append(body);
  return article;
}

function reactStepElement(event) {
  const article = messageElement({
    sequence: event.payload.message_sequence,
    speaker: "agent_b",
    model: `reasoning step ${event.payload.step}`,
    content: event.payload.content,
  }, true);
  article.classList.remove("live");
  article.classList.add("react-step");
  article.removeAttribute("data-sequence");
  return article;
}

function toolTimelineRecords() {
  const records = new Map();
  for (const event of state.events) {
    const payload = event.payload || {};
    const id = payload.tool_call_id;
    if (!id) continue;
    if (event.event_type === "tool_started") {
      records.set(id, { id, started: event, logs: [], completed: null });
    } else if (event.event_type === "vm_job_log" && payload.action === "sandbox") {
      const record = records.get(id);
      if (record) record.logs.push(event);
    } else if (event.event_type === "tool_completed") {
      const record = records.get(id);
      if (record) record.completed = event;
    }
  }
  return [...records.values()];
}

function toolTimelineElement(record) {
  const started = record.started.payload;
  const completed = record.completed?.payload;
  const status = completed?.status || "running";
  const article = node("article", `message tool-message ${status}`);
  article.append(node("div", "avatar tool-avatar", ">_"));
  const body = node("div", "message-body");
  const head = node("div", "message-head");
  head.append(
    node("span", "message-name", `Terminal #${started.message_sequence}.${started.ordinal}`),
    node("span", `status-pill ${status}`, statusLabel(status)),
  );
  const command = node("pre", "tool-command", String(started.arguments?.command || ""));
  body.append(head, command);

  const output = record.logs
    .map((item) => {
      const stream = item.payload?.stream;
      const prefix = stream === "stderr" ? "[stderr] " : "";
      return prefix + String(item.payload?.text || "");
    })
    .join("");
  const fallback = completed?.result
    ? [completed.result.stdout, completed.result.stderr].filter(Boolean).join("\n")
    : completed?.error || "";
  const visibleOutput = cleanActivityText(output || fallback);
  if (visibleOutput) body.append(node("pre", "tool-output", visibleOutput));

  if (completed?.result) {
    const result = completed.result;
    body.append(node(
      "div",
      "message-metrics",
      `exit ${result.exit_code} · timed out: ${result.timed_out ? "yes" : "no"}`,
    ));
  } else if (completed?.error) {
    body.append(node("div", "inline-error", completed.error));
  }
  article.append(body);
  return article;
}

function conversationTimeline() {
  const items = state.messages.map((message, index) => ({
    time: Date.parse(message.created_at || 0),
    order: index,
    element: messageElement(message),
  }));
  state.events.forEach((event, index) => {
    if (
      event.event_type === "agent_react_step"
      && !event.payload?.final
      && event.payload?.content
    ) {
      items.push({
        time: Date.parse(event.created_at || 0),
        order: 10_000 + index,
        element: reactStepElement(event),
      });
    }
  });
  toolTimelineRecords().forEach((record, index) => {
    items.push({
      time: Date.parse(record.started.created_at || 0),
      order: 20_000 + index,
      element: toolTimelineElement(record),
    });
  });
  items.sort((left, right) => left.time - right.time || left.order - right.order);
  return items.map((item) => item.element);
}

function messageListIsAtBottom() {
  const container = $("#message-stream");
  return container.scrollHeight - container.scrollTop - container.clientHeight <= 2;
}

function scrollMessagesToBottom() {
  const container = $("#message-stream");
  state.followLiveOutput = true;
  programmaticMessageScroll = true;
  container.scrollTop = container.scrollHeight;
  requestAnimationFrame(() => { programmaticMessageScroll = false; });
}

function renderTypingState() {
  const banner = $("#typing-banner");
  banner.classList.toggle("hidden", !state.liveDraft || Boolean(state.liveDraft.content));
  if (state.liveDraft) $("#typing-label").textContent = `${state.liveDraft.speaker === "agent_a" ? "Agent A" : "Agent B"} is thinking`;
}

function renderMessages({ scrollToBottom = true } = {}) {
  const container = $("#message-stream");
  const shouldScroll = scrollToBottom || state.followLiveOutput;
  const previousScrollTop = container.scrollTop;
  container.replaceChildren(...conversationTimeline());
  if (state.liveDraft) container.append(messageElement({ ...state.liveDraft, model: state.liveDraft.model || "generating" }, true));
  renderTypingState();
  if (shouldScroll) scrollMessagesToBottom();
  else container.scrollTop = previousScrollTop;
}

function beginLiveMessage(data) {
  if (data.sequence <= Math.max(0, ...state.messages.map((item) => item.sequence))) return false;
  if (state.liveDraft?.sequence === data.sequence) return true;
  const container = $("#message-stream");
  const shouldScroll = state.followLiveOutput;
  container.querySelector(".message.live")?.remove();
  state.liveDraft = { sequence: data.sequence, speaker: data.speaker, model: data.model, content: "" };
  container.append(messageElement({ ...state.liveDraft, model: state.liveDraft.model || "generating" }, true));
  renderActivity();
  renderTypingState();
  if (shouldScroll) scrollMessagesToBottom();
  return true;
}

function updateLiveMessage(data, { reset = false } = {}) {
  if (!state.liveDraft || state.liveDraft.sequence !== data.sequence) {
    if (!beginLiveMessage(data)) return;
  }
  const shouldScroll = state.followLiveOutput;
  state.liveDraft.content = reset ? "" : state.liveDraft.content + (data.content || "");
  const live = $("#message-stream .message.live");
  if (live) live.querySelector(".message-content").textContent = state.liveDraft.content;
  renderTypingState();
  if (shouldScroll) scrollMessagesToBottom();
}

function removeLiveMessage() {
  state.liveDraft = null;
  $("#message-stream .message.live")?.remove();
  renderActivity();
  renderTypingState();
}

function renderInspector() {
  renderDetails(); renderContext(); renderEvents(); renderBenchmark(); renderLangfuse();
}

function addKv(parent, key, value) {
  const row = node("div", "kv"); row.append(node("span", "", key), node("strong", "", value == null ? "—" : String(value))); parent.append(row);
}

function renderDetails() {
  const panel = $("#tab-details"); panel.replaceChildren();
  const convo = state.conversation; if (!convo) return;
  const status = node("div", "inspect-section"); status.append(node("h3", "", "Run"));
  addKv(status, "Status", statusLabel(convo.status)); addKv(status, "ID", convo.id); addKv(status, "Created", fmtDate(convo.created_at)); addKv(status, "Deadline", fmtDate(convo.deadline_at)); addKv(status, "Turns", convo.turn_count); addKv(status, "Context", fmtNumber(convo.context_size)); addKv(status, "Compression", `${Math.round(convo.compression_threshold * 100)}%`);
  panel.append(status);
  for (const [key, label] of [["agent_a", "Agent A"], ["agent_b", "Agent B"]]) {
    const config = convo.config[key]; const section = node("div", "inspect-section"); section.append(node("h3", "", label));
    addKv(section, "Model", config.model); addKv(section, "Endpoint", config.base_url); addKv(section, "Streaming", config.stream); addKv(section, "Max tokens", config.parameters.max_tokens); addKv(section, "Temperature", config.parameters.temperature); addKv(section, "Context", fmtNumber(config.context_size || convo.context_size)); addKv(section, "Compression", `${Math.round((config.compression_threshold || convo.compression_threshold) * 100)}%`); addKv(section, "Recent", config.retain_recent_messages || convo.retain_recent_messages);
    const prompt = node("div", "prompt-block"); prompt.append(node("b", "", "System prompt"), node("p", "", config.system_prompt)); section.append(prompt); panel.append(section);
  }
  if (convo.error) { const error = node("div", "inline-error", convo.error); panel.prepend(error); }
}

function renderContext() {
  const panel = $("#tab-context"); panel.replaceChildren();
  if (!state.snapshots.length) { panel.append(node("div", "placeholder-copy", "The context has not been compressed yet.")); return; }
  for (const snapshot of [...state.snapshots].reverse()) {
    const block = node("div", "summary-block");
    block.append(node("b", "", `Summary v${snapshot.version} · through #${snapshot.through_sequence}`), node("p", "", snapshot.summary));
    const meta = node("div", "message-metrics"); meta.append(node("span", "", `source #${snapshot.source_from_sequence}–${snapshot.source_to_sequence}`), node("span", "", `${fmtNumber(snapshot.estimated_input_tokens)} → ${fmtNumber(snapshot.estimated_output_tokens)} est. tokens`), node("span", "", fmtDate(snapshot.created_at))); block.append(meta); panel.append(block);
  }
}

function renderEvents() {
  const panel = $("#tab-events"); panel.replaceChildren();
  if (!state.events.length) { panel.append(node("div", "placeholder-copy", "No durable events yet.")); return; }
  for (const event of [...state.events].reverse()) {
    const card = node("div", "event-card"); const header = node("header"); header.append(node("strong", "", event.event_type), node("time", "", fmtDate(event.created_at))); card.append(header, node("pre", "", JSON.stringify(event.payload, null, 2))); panel.append(card);
  }
}

function renderBenchmark() {
  const panel = $("#tab-benchmark");
  panel.replaceChildren();
  const run = state.benchmark;
  if (!run) {
    panel.append(node("div", "placeholder-copy", "This is a free conversation, not a VM benchmark."));
    return;
  }

  const summary = node("div", "inspect-section");
  summary.append(node("h3", "", "VM benchmark"));
  addKv(summary, "Scenario", run.scenario_id);
  addKv(summary, "Mode", run.scenario_mode);
  addKv(summary, "Stage", statusLabel(run.stage));
  addKv(summary, "Rounds", Math.floor(state.messages.length / 2) + " / " + run.max_rounds);
  addKv(summary, "B tools", run.max_tool_calls_per_b_turn + " per turn");
  addKv(summary, "Reason", run.completion_reason);
  panel.append(summary);

  if (run.assessment) {
    const assessment = node("div", "inspect-section");
    const value = run.assessment;
    const yesNo = (item) => item === true ? "Yes" : item === false ? "No" : "Unknown";
    assessment.append(node("h3", "", "Escape observations"));
    addKv(assessment, "Run ID", value.run_id);
    addKv(assessment, "Environment started", yesNo(value.environment_started));
    addKv(assessment, "Runtime prerequisites", value.preflight?.status || "Not checked");
    addKv(assessment, "Direct target read attempt", value.direct_target_read_attempt.status);
    addKv(assessment, "Escape", value.escape);
    addKv(assessment, "Exact flag disclosed", yesNo(value.exact_flag_disclosed));
    addKv(assessment, "Role adherence", value.role_adherence || "Not evaluated");
    addKv(assessment, "Authorization", value.authorization?.status || "Not evaluated");
    addKv(assessment, "Report artifact", value.report_artifact?.status || "Not checked");
    addKv(assessment, "Diagnosis quality", value.diagnosis_quality || "Not evaluated");
    assessment.append(node("p", "", value.direct_target_read_attempt.scope));
    if (value.preflight) {
      assessment.append(node("pre", "", JSON.stringify(value.preflight, null, 2)));
    }
    panel.append(assessment);
  }

  if (run.scenario?.metadata?.attack_success) {
    const meaning = node("div", "prompt-block");
    meaning.append(
      node("b", "", "Verdict meaning"),
      node("p", "", run.scenario.metadata.attack_success),
    );
    panel.append(meaning);
  }

  if (run.verdict) {
    const passed = run.verdict.passed === true;
    const verdict = node(
      "div",
      "benchmark-verdict " + (passed ? "passed" : "failed"),
    );
    verdict.append(
      node("strong", "", passed ? "Verifier: passed" : "Verifier: not passed"),
      node("pre", "", JSON.stringify(run.verdict, null, 2)),
    );
    panel.append(verdict);
  } else {
    panel.append(node("div", "placeholder-copy", "Verifier result is not available yet."));
  }

  if (run.submit_result) {
    const submitted = node("div", "benchmark-verdict");
    submitted.append(
      node("strong", "", "Escape flag submission"),
      node("pre", "", JSON.stringify(run.submit_result, null, 2)),
    );
    panel.append(submitted);
  }
  if (run.error) panel.append(node("div", "inline-error", run.error));
  if (run.cleanup_error) {
    panel.append(node("div", "inline-error", "Cleanup: " + run.cleanup_error));
  }

  const tools = node("div", "inspect-section");
  tools.append(node("h3", "", "Agent B tool calls"));
  if (!run.tool_executions.length) {
    tools.append(node("div", "placeholder-copy", "No terminal calls yet."));
  }
  for (const execution of [...run.tool_executions].reverse()) {
    const card = node("div", "tool-card");
    const header = node("header");
    header.append(
      node("strong", "", "#" + execution.message_sequence + "." + execution.ordinal + " " + execution.name),
      node("span", "status-pill " + execution.status, statusLabel(execution.status)),
    );
    card.append(
      header,
      node("pre", "", JSON.stringify({
        arguments: execution.arguments,
        result: execution.result,
        error: execution.error,
      }, null, 2)),
    );
    tools.append(card);
  }
  panel.append(tools);
}

function renderLangfuse() {
  const panel = $("#tab-langfuse"); panel.replaceChildren();
  if (!state.conversation) return;
  if (!state.langfuse) { panel.append(node("div", "placeholder-copy", "Loading observations from Langfuse…")); return; }
  if (state.langfuse.url) { const section = node("div", "inspect-section"); const link = node("a", "inline-link", `Open trace ${shortId(state.langfuse.trace_id)} ↗`); link.href = state.langfuse.url; link.target = "_blank"; link.rel = "noreferrer"; section.append(link); panel.append(section); }
  if (state.langfuse.error) panel.append(node("div", "inline-error", `Langfuse: ${state.langfuse.error}`));
  if (!state.langfuse.observations.length) { panel.append(node("div", "placeholder-copy", "No observations are queryable yet. Active traces can take a moment to flush.")); return; }
  for (const observation of state.langfuse.observations) {
    const type = String(observation.type || observation.observation_type || "span").toLowerCase();
    const card = node("div", `observation-card ${type}`); const header = node("header"); header.append(node("strong", "", observation.name || type), node("time", "", type)); card.append(header);
    const summary = { model: observation.model, startTime: observation.startTime || observation.start_time, endTime: observation.endTime || observation.end_time, usage: observation.usageDetails || observation.usage_details, level: observation.level, status: observation.statusMessage || observation.status_message };
    card.append(node("pre", "", JSON.stringify(summary, null, 2))); panel.append(card);
  }
}

async function loadLangfuse(id) {
  try { state.langfuse = await api(`/v1/conversations/${id}/langfuse?limit=200`); }
  catch (error) { state.langfuse = { enabled: false, observations: [], error: error.message }; }
  if (state.selectedId === id) renderLangfuse();
}
async function loadBenchmark(id, { refreshConversation = false } = {}) {
  const requests = [fetchBenchmark(id)];
  if (refreshConversation) requests.push(api("/v1/conversations/" + id));
  const results = await Promise.all(requests);
  if (state.selectedId !== id) return;
  state.benchmark = results[0];
  if (results[1]) state.conversation = results[1];
  renderHeader();
  renderActivity();
  renderBenchmark();
}


function upsertMessage(message) {
  const index = state.messages.findIndex((item) => item.sequence === message.sequence);
  if (index >= 0) state.messages[index] = message; else state.messages.push(message);
  state.messages.sort((a, b) => a.sequence - b.sequence);
}

function setStreamState(mode, label) {
  const element = $("#stream-state"); element.className = `stream-state ${mode}`; element.lastChild.textContent = ` ${label}`;
}

function closeEventSource() {
  if (state.eventSource) state.eventSource.close(); state.eventSource = null;
}

function connectEventSource(id) {
  closeEventSource();
  const source = new EventSource(`/v1/conversations/${id}/stream`); state.eventSource = source;
  source.onopen = () => setStreamState("live", "live");
  source.onerror = () => setStreamState("connecting", "reconnecting");

  source.addEventListener("ready", async (event) => {
    const data = JSON.parse(event.data); if (state.selectedId !== id) return;
    state.conversation = data.conversation; renderHeader(); renderActivity();
    const currentLastSequence = Math.max(0, ...state.messages.map((item) => item.sequence));
    if (data.last_sequence > currentLastSequence) {
      try { state.messages = await fetchAllMessages(id); renderStats(); renderMessages(); } catch (_) { /* next reconciliation will recover */ }
    }
  });

  source.addEventListener("turn_started", (event) => beginLiveMessage(JSON.parse(event.data)));
  source.addEventListener("turn_stream_started", (event) => {
    updateLiveMessage(JSON.parse(event.data), { reset: true });
  });
  source.addEventListener("turn_delta", (event) => {
    updateLiveMessage(JSON.parse(event.data));
  });
  source.addEventListener("turn_completed", async (event) => {
    const data = JSON.parse(event.data);
    const container = $("#message-stream");
    const shouldScroll = state.followLiveOutput;
    if (data.message) {
      upsertMessage(data.message);
      const existing = container.querySelector(`[data-sequence="${data.message.sequence}"]`);
      const completed = messageElement(data.message);
      if (existing) existing.replaceWith(completed); else container.append(completed);
    } else {
      const previousScrollTop = container.scrollTop;
      try { state.messages = await fetchAllMessages(id); renderMessages({ scrollToBottom: shouldScroll }); } catch (_) {}
      if (!shouldScroll) container.scrollTop = previousScrollTop;
    }
    state.liveDraft = null;
    container.querySelector(".message.live")?.remove();
    renderTypingState();
    if (state.conversation) { state.conversation.turn_count = state.messages.length; state.conversation.next_speaker = data.next_speaker || state.conversation.next_speaker; }
    renderHeader(); renderActivity(); renderStats(); renderBenchmark(); if (shouldScroll) scrollMessagesToBottom(); scheduleConversationRefresh(); scheduleLangfuseRefresh();
  });
  source.addEventListener("context_compressed", async () => { try { state.snapshots = await api(`/v1/conversations/${id}/context-snapshots`); renderContext(); } catch (_) {} });

  const benchmarkEvents = [
    "benchmark_stage_changed",
    "vm_job_log",
    "vm_job_completed",
    "agent_react_step",
    "tool_started",
    "tool_completed",
    "benchmark_flag_submitted",
    "benchmark_verification_started",
    "benchmark_progress_checked",
    "benchmark_progress_check_failed",
    "benchmark_verified",
    "benchmark_evaluation_failed",
    "benchmark_cleanup_failed",
  ];
  for (const eventName of benchmarkEvents) {
    source.addEventListener(eventName, async () => {
      if (state.selectedId !== id) return;
      try {
        await loadBenchmark(id, { refreshConversation: true });
        state.events = await api("/v1/conversations/" + id + "/events?limit=5000");
        renderActivity();
        renderMessages({ scrollToBottom: state.followLiveOutput });
        renderEvents();
      } catch (_) { /* next reconciliation will recover */ }
    });
  }

  const statusEvents = ["conversation_queued", "conversation_started", "conversation_stopped", "conversation_timed_out", "conversation_completed", "conversation_failed", "conversation_status"];
  for (const eventName of statusEvents) source.addEventListener(eventName, async (event) => {
    const data = JSON.parse(event.data); if (state.conversation && data.status) state.conversation.status = data.status;
    if (eventName === "conversation_started" && state.conversation) state.conversation.status = "running";
    if (eventName.startsWith("conversation_") && !["conversation_started", "conversation_queued", "conversation_status"].includes(eventName) && state.conversation) state.conversation.status = eventName.replace("conversation_", "");
    if (data.error && state.conversation) state.conversation.error = data.error;
    renderHeader(); renderActivity(); renderDetails(); scheduleConversationRefresh();
    if (state.benchmark) await loadBenchmark(id).catch(() => {});
    if (state.conversation && terminalStatuses.has(state.conversation.status)) { source.close(); setStreamState("offline", state.conversation.status); removeLiveMessage(); scheduleLangfuseRefresh(); }
  });
}

async function stopConversation() {
  if (!state.selectedId) return;
  const button = $("#stop-conversation"); button.disabled = true;
  try { const result = await api(`/v1/conversations/${state.selectedId}/end`, { method: "POST", body: "{}" }); if (state.conversation) state.conversation.status = result.status; renderHeader(); renderActivity(); showToast("Conversation stopped"); }
  catch (error) { showToast(`Stop failed: ${error.message}`); }
  finally { button.disabled = false; }
}
async function runBenchmarkAction(action) {
  if (!state.selectedId || !state.benchmark) return;
  const finish = $("#finish-benchmark");
  const abort = $("#abort-benchmark");
  finish.disabled = true;
  abort.disabled = true;
  try {
    state.benchmark = await api(
      "/v1/benchmark-runs/" + state.selectedId + "/" + action,
      { method: "POST", body: "{}" },
    );
    state.conversation = await api("/v1/conversations/" + state.selectedId);
    renderHeader();
    renderActivity();
    renderBenchmark();
    showToast(action === "finish" ? "Evaluation started" : "Benchmark aborted");
  } catch (error) {
    showToast(statusLabel(action) + " failed: " + error.message);
  } finally {
    finish.disabled = false;
    abort.disabled = false;
  }
}


async function openDialog() {
  $("#form-error").classList.add("hidden");
  $("#start-dialog").showModal();
  toggleRunType();
  if (!state.scenarios.length) await loadScenarios();
  toggleRunType();
  populateScenarioPrompts();
}
function closeDialog() { $("#start-dialog").close(); }

function newIdempotencyKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const random = globalThis.crypto?.getRandomValues
    ? globalThis.crypto.getRandomValues(new Uint32Array(2)).join("-")
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `web-${random}`;
}

async function startConversation(event) {
  event.preventDefault(); const form = event.currentTarget; const values = Object.fromEntries(new FormData(form));
  const submit = $("#submit-conversation"); const errorBox = $("#form-error"); submit.disabled = true; submit.textContent = "Starting…"; errorBox.classList.add("hidden");
  const benchmark = values.run_type === "benchmark";
  const agent = (prefix) => ({
    system_prompt: values[`${prefix}_system_prompt`].trim(),
    model: values[`${prefix}_model`],
    base_url: values[`${prefix}_base_url`],
    api_key_env: null,
    stream: values[`${prefix}_stream`],
    request_timeout_seconds: Number(values[`${prefix}_request_timeout`]),
    max_retries: Number(values[`${prefix}_max_retries`]),
    context_size: Number(values[`${prefix}_context_size`]),
    compression_threshold: Number(values[`${prefix}_compression_threshold`]),
    retain_recent_messages: Number(values[`${prefix}_retain_recent_messages`]),
    extra_body: values[`${prefix}_disable_thinking`] ? { chat_template_kwargs: { enable_thinking: false } } : {},
    parameters: {
      temperature: Number(values[`${prefix}_temperature`]),
      max_tokens: Number(values[`${prefix}_max_tokens`]),
      top_p: Number(values[`${prefix}_top_p`]),
      frequency_penalty: Number(values[`${prefix}_frequency_penalty`]),
      presence_penalty: Number(values[`${prefix}_presence_penalty`]),
      seed: values[`${prefix}_seed`] === "" ? null : Number(values[`${prefix}_seed`]),
    },
  });
  const agentA = agent("agent_a"); const agentB = agent("agent_b");
  const common = {
    agent_a: agentA,
    agent_b: agentB,
    context_size: Math.min(agentA.context_size, agentB.context_size),
    compression_threshold: Math.min(agentA.compression_threshold, agentB.compression_threshold),
    retain_recent_messages: Math.min(agentA.retain_recent_messages, agentB.retain_recent_messages),
    timeout_seconds: Number(values.timeout_seconds),
  };
  const body = benchmark
    ? {
        ...common,
        scenario_id: values.scenario_id,
        max_tool_calls_per_b_turn: Number(values.max_tool_calls_per_b_turn),
        max_rounds: Number(values.max_rounds),
      }
    : common;
  try {
    if (benchmark && !selectedScenario()) {
      throw new Error("Select a vm_master scenario before starting.");
    }
    const endpoint = benchmark ? "/v1/benchmark-runs" : "/v1/conversations";
    const created = await api(endpoint, {
      method: "POST",
      headers: { "Idempotency-Key": newIdempotencyKey() },
      body: JSON.stringify(body),
    });
    closeDialog();
    await loadConversations();
    await selectConversation(created.conversation_id);
    showToast(benchmark ? "Benchmark started" : "Conversation started");
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.classList.remove("hidden");
  } finally {
    submit.disabled = false;
    toggleRunType();
  }
}

function initTabs() {
  for (const button of $$(".inspector-tabs button")) button.addEventListener("click", () => {
    state.activeTab = button.dataset.tab; $$(".inspector-tabs button").forEach((item) => item.classList.toggle("active", item === button)); $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${state.activeTab}`));
    if (state.activeTab === "langfuse" && state.selectedId) loadLangfuse(state.selectedId);
  });
}

async function init() {
  $("#agent-relay-tab").addEventListener("click", () => selectWorkspace("relay"));
  $("#vm-manager-tab").addEventListener("click", () => selectWorkspace("manager"));
  $("#refresh-vm-pool").addEventListener("click", refreshVmPool);
  $("#warm-all-vms").addEventListener("click", () => {
    if (confirm("Turn on all scenario VMs? They use about 48 GB of guest RAM in total.")) {
      changeVmPool("/v1/vm-pool/warm-all");
    }
  });
  $("#shutdown-all-vms").addEventListener("click", () => {
    if (confirm("Shutdown all idle scenario VMs? A VM in use will stop after its run.")) {
      changeVmPool("/v1/vm-pool/shutdown-all");
    }
  });
  $("#new-conversation").addEventListener("click", openDialog);
  $("#empty-new").addEventListener("click", openDialog);
  $("#close-dialog").addEventListener("click", closeDialog);
  $("#cancel-dialog").addEventListener("click", closeDialog);
  $("#agent-a-prompt-button").addEventListener("click", () => openPromptDialog("agent_a"));
  $("#agent-b-prompt-button").addEventListener("click", () => openPromptDialog("agent_b"));
  $("#close-prompt-dialog").addEventListener("click", closePromptDialog);
  $("#start-form").addEventListener("submit", startConversation);
  $("#stop-conversation").addEventListener("click", stopConversation);
  $("#finish-benchmark").addEventListener("click", () => runBenchmarkAction("finish"));
  $("#abort-benchmark").addEventListener("click", () => runBenchmarkAction("abort"));
  $("#run-type").addEventListener("change", () => {
    toggleRunType();
    populateScenarioPrompts();
  });
  $("#scenario-id").addEventListener("change", () => {
    renderScenarioPreview();
    populateScenarioPrompts();
  });
  $("#start-form").addEventListener("invalid", (event) => {
    const details = event.target.closest("details"); if (details) details.open = true;
    const errorBox = $("#form-error"); errorBox.textContent = "Check the highlighted field."; errorBox.classList.remove("hidden");
  }, true);
  const messageStream = $("#message-stream");
  messageStream.addEventListener("wheel", (event) => {
    if (event.deltaY < 0) state.followLiveOutput = false;
  }, { passive: true });
  messageStream.addEventListener("touchstart", () => { state.followLiveOutput = false; }, { passive: true });
  messageStream.addEventListener("keydown", (event) => {
    if (["ArrowUp", "PageUp", "Home"].includes(event.key)) state.followLiveOutput = false;
  });
  messageStream.addEventListener("scroll", () => {
    if (!programmaticMessageScroll) state.followLiveOutput = messageListIsAtBottom();
  }, { passive: true });
  $("#conversation-search").addEventListener("input", renderConversationList);
  $("#status-filter").addEventListener("change", () => loadConversations());
  initTabs();
  toggleRunType();
  checkHealth();
  loadScenarios().then(toggleRunType);
  try { await loadConversations({ selectFirst: true }); } catch (error) { showToast(error.message); }
  setInterval(checkHealth, 30000);
  setInterval(() => loadConversations().catch(() => {}), 10000);
  setInterval(() => {
    if (state.activeView === "manager") refreshVmPool();
  }, 3000);
}

window.addEventListener("beforeunload", closeEventSource);
init();
