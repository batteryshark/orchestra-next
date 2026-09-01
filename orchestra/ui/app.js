// Orchestra Next operator console. One module, no dependencies; every API
// string enters the page through textContent-built DOM nodes.

// --- constants ---
const REFRESH_VISIBLE = 2000;
const REFRESH_HIDDEN = 30000;
const BACKOFF_MAX = 30000;
const PAGE_EVENTS = 500;
const PAGE_RUNS = 200;
const TAIL_THRESHOLD = 72;
const CLIP_CHARS = 2000;
const HARD_SLICE = 50000;
const SECTIONS = ["activity", "changes", "artifacts", "usage", "evidence"];
const ACTIVE_STATUSES = new Set(["queued", "starting", "running", "waiting"]);
const STATUS_GROUPS = {
  queued: ["queued"],
  running: ["starting", "running"],
  waiting: ["waiting"],
  failed: ["failed", "timed_out"],
  done: ["completed", "stopped", "skipped"],
};
// --- end constants ---

// --- fmt ---
const fmt = {
  rel(iso) {
    if (!iso) return "";
    const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (seconds < 60) return `${Math.floor(seconds)}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    if (seconds < 30 * 86400) return `${Math.floor(seconds / 86400)}d`;
    return iso.slice(0, 10);
  },
  duration(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
    return `${Math.floor(seconds / 3600)}h ${String(Math.floor(seconds / 60) % 60).padStart(2, "0")}m`;
  },
  tokens(value) {
    value = value || 0;
    if (value < 10000) return value.toLocaleString("en-US");
    if (value < 1e6) return `${(value / 1e3).toFixed(1)}k`;
    return `${(value / 1e6).toFixed(1)}M`;
  },
  count(value) {
    return (value || 0).toLocaleString("en-US");
  },
  bytes(value) {
    value = value || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / 1024 / 1024).toFixed(1)} MB`;
  },
  excerpt(text, max = 120) {
    const line = String(text || "").split("\n", 1)[0];
    return line.length > max ? line.slice(0, max - 1) + "…" : line;
  },
  countdown(expiresIso, now = Date.now()) {
    const left = Math.floor((Date.parse(expiresIso) - now) / 1000);
    if (left <= 0) return "expired";
    return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
  },
};
// --- end fmt ---

// --- logic ---
function mergeThread(events, messages) {
  const thread = [];
  for (const event of events) {
    thread.push({ key: `e${event.id}`, at: event.created_at, ord: event.id, kind: "event", item: event });
  }
  for (const message of messages) {
    thread.push({ key: `m${message.message_id ?? message.id}`, at: message.created_at, ord: 0, kind: "message", item: message });
  }
  thread.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : a.ord - b.ord));
  return thread;
}

function parseDiff(text) {
  const classify = (line) => {
    if (line.startsWith("+++") || line.startsWith("---")) return "d-meta";
    if (line.startsWith("@@")) return "d-hunk";
    if (line.startsWith("diff --git") || line.startsWith("index ") || line.startsWith("new file") || line.startsWith("deleted file") || line.startsWith("similarity") || line.startsWith("rename")) return "d-meta";
    if (line.startsWith("+")) return "d-add";
    if (line.startsWith("-")) return "d-del";
    return "";
  };
  const files = [];
  if (!text) return { files };
  const chunks = text.split(/^diff --git /m);
  const head = chunks.shift();
  if (chunks.length === 0) {
    if (head.trim()) files.push({ path: "patch", add: 0, del: 0, lines: head.split("\n").map((line) => ({ cls: classify(line), text: line })) });
    return { files };
  }
  for (const chunk of chunks) {
    const body = "diff --git " + chunk;
    const match = chunk.match(/ b\/(\S+)/);
    const lines = body.split("\n").map((line) => ({ cls: classify(line), text: line }));
    let add = 0, del = 0;
    for (const line of lines) {
      if (line.cls === "d-add") add += 1;
      if (line.cls === "d-del") del += 1;
    }
    files.push({ path: match ? match[1] : "unknown", add, del, lines });
  }
  return { files };
}

function aggregateUsage(rows) {
  const groups = new Map();
  const totals = { input: 0, output: 0, cache_read: 0, cache_write: 0, total: 0 };
  for (const row of rows) {
    const key = `${row.provider ?? "?"}|${row.model ?? "?"}|${row.cache_epoch}|${row.descendant_session_id ?? ""}`;
    let group = groups.get(key);
    if (!group) {
      group = { provider: row.provider, model: row.model, cache_epoch: row.cache_epoch, descendant: row.descendant_session_id, input: 0, output: 0, cache_read: 0, cache_write: 0, total: 0, events: 0 };
      groups.set(key, group);
    }
    group.input += row.input_tokens || 0;
    group.output += row.output_tokens || 0;
    group.cache_read += row.cache_read_tokens || 0;
    group.cache_write += row.cache_write_tokens || 0;
    group.total += row.total_tokens || 0;
    group.events += 1;
    totals.input += row.input_tokens || 0;
    totals.output += row.output_tokens || 0;
    totals.cache_read += row.cache_read_tokens || 0;
    totals.cache_write += row.cache_write_tokens || 0;
    totals.total += row.total_tokens || 0;
  }
  return { groups: [...groups.values()], totals };
}

function runSignature(run) {
  return `${run.revision}:${run.updated_at}:${run.status}:${run.waiting_kind ?? ""}:${run.tokens_total}:${run.active_seconds}:${run.rounds_started}`;
}

function filterRuns(runs, filters) {
  const statuses = new Set();
  for (const group of filters.status) for (const status of STATUS_GROUPS[group] || []) statuses.add(status);
  const text = filters.text.trim().toLowerCase();
  return runs.filter((run) => {
    if (statuses.size && !statuses.has(run.status)) return false;
    if (filters.group && String(run.group_id) !== filters.group) return false;
    if (filters.profile && String(run.profile_id) !== filters.profile) return false;
    if (text) {
      const haystack = `${run.id} ${run.slug ?? ""} ${run.title ?? ""} ${run.objective ?? ""}`.toLowerCase();
      if (!haystack.includes(text)) return false;
    }
    return true;
  });
}

function extractText(payload) {
  if (payload == null) return "";
  if (typeof payload === "string") return payload;
  if (typeof payload.text === "string") return payload.text;
  if (typeof payload.message === "string") return payload.message;
  const content = payload.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map((part) => (typeof part === "string" ? part : part?.text ?? "")).filter(Boolean).join("\n");
  }
  if (content && typeof content === "object" && typeof content.text === "string") return content.text;
  return "";
}
// --- end logic ---

// --- qr ---
// Reserved for the pairing QR encoder (slice 3).
// --- end qr ---

// --- api ---
class ApiError extends Error {
  constructor(status, message) {
    super(message || `HTTP ${status}`);
    this.status = status;
  }
}

async function req(method, path, body) {
  let response;
  try {
    response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "Orchestra is unreachable");
  }
  let value = null;
  try {
    value = await response.json();
  } catch {
    value = null;
  }
  if (!response.ok) {
    if (response.status === 401) setAuth("unpaired");
    throw new ApiError(response.status, value?.error?.message);
  }
  if (!value || typeof value.board_revision !== "number" && typeof value.board_revision !== "string") {
    if (!path.startsWith("/api/openapi")) {
      fatal("The server reply is not an Orchestra Next envelope. Update the server or this console.");
      throw new ApiError(500, "malformed envelope");
    }
    return value;
  }
  if (store.instanceId && value.instance_id !== store.instanceId) resetStore(value.instance_id);
  store.instanceId = value.instance_id;
  const revision = Number(value.board_revision);
  if (revision !== store.boardRevision) {
    store.boardRevision = revision;
    store.snapshotsStale = true;
  }
  return value.data;
}

const api = {
  get: (path) => req("GET", path),
  post: (path, body) => req("POST", path, body ?? {}),
};
// --- end api ---

// --- store ---
const store = {
  auth: "unknown", // unknown | ok | unpaired
  me: null,
  instanceId: null,
  boardRevision: -1,
  snapshotsStale: true,
  runs: new Map(),
  runsFloor: null, // smallest loaded run id, for Load older
  runsExhausted: false,
  attention: [],
  profiles: [],
  groups: [],
  readiness: null,
  route: { view: "fleet", runId: null, section: "activity" },
  detail: {
    id: null,
    run: null,
    events: [],
    eventsAfter: 0, // newest loaded event id; forward polls continue from here
    historyFloor: null, // oldest loaded event id; null until the first page arrives
    historyDone: false,
    eventFilter: "",
    previews: {},
    messages: [],
    usage: [],
    changes: null,
    artifacts: [],
    loaded: { changes: false, usage: false, artifacts: false },
  },
  filters: { status: new Set(), group: "", profile: "", text: "" },
  ui: { follow: true, inflight: new Set(), dispatchRequestId: crypto.randomUUID() },
};

const dirty = new Set();
let renderQueued = false;
function markDirty(...views) {
  for (const view of views) dirty.add(view);
  if (!renderQueued) {
    renderQueued = true;
    if (document.hidden) setTimeout(render, 0);
    else requestAnimationFrame(render);
  }
}

function resetStore(instanceId) {
  store.instanceId = instanceId;
  store.boardRevision = -1;
  store.snapshotsStale = true;
  store.runs.clear();
  store.runsFloor = null;
  store.runsExhausted = false;
  store.attention = [];
  store.cursorsEvents = null;
  resetDetail(store.detail.id);
  toast("Server instance changed; state reloaded");
  markDirty("fleet", "run", "attention", "config", "nav");
}

function resetDetail(runId) {
  store.detail = {
    id: runId, run: null, events: [], eventsAfter: 0, historyFloor: null, historyDone: false, eventFilter: "", previews: {},
    messages: [], usage: [], changes: null, artifacts: [], loaded: { changes: false, usage: false, artifacts: false },
  };
  const feed = document.getElementById("feed");
  feed.replaceChildren();
  feed._keyed = new Map();
  feed._tools = new Map();
  feed._appliedUpdates = new Set();
  document.getElementById("load-history").hidden = true;
  document.getElementById("feed-brief").hidden = true;
  document.getElementById("feed-handoff").hidden = true;
  setFollow(true);
}

function setAuth(state) {
  if (store.auth !== state) {
    store.auth = state;
    markDirty("auth");
  }
}
// --- end store ---

// --- dom ---
function el(tag, props, ...children) {
  const node = document.createElement(tag);
  if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value == null) continue;
      if (key === "class") node.className = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    }
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function keyedList(container, items, keyFn, sigFn, buildFn, updateFn) {
  const map = container._keyed || (container._keyed = new Map());
  const seen = new Set();
  let cursor = container.firstChild;
  for (const item of items) {
    const key = keyFn(item);
    seen.add(key);
    let entry = map.get(key);
    const sig = sigFn(item);
    if (!entry) {
      entry = { el: buildFn(item), sig };
      map.set(key, entry);
    } else if (entry.sig !== sig) {
      updateFn(entry.el, item);
      entry.sig = sig;
    }
    if (entry.el === cursor) {
      cursor = cursor.nextSibling;
    } else {
      container.insertBefore(entry.el, cursor);
    }
  }
  for (const [key, entry] of map) {
    if (!seen.has(key)) {
      entry.el.remove();
      map.delete(key);
    }
  }
}

function boundedPre(text, className) {
  text = String(text ?? "");
  const sliced = text.length > HARD_SLICE;
  const shown = sliced ? text.slice(0, HARD_SLICE) : text;
  const pre = el("pre", { class: className }, shown);
  const wrap = el("div", null, pre);
  if (shown.length > CLIP_CHARS) {
    pre.classList.add("clip");
    const button = el("button", { class: "expand-btn", type: "button", text: `Expand (${fmt.count(text.length)} chars)` });
    button.addEventListener("click", () => {
      pre.classList.toggle("clip-open");
      button.textContent = pre.classList.contains("clip-open") ? "Collapse" : `Expand (${fmt.count(text.length)} chars)`;
    });
    wrap.append(button);
  }
  if (sliced) {
    wrap.append(el("details", null, el("summary", { text: `Show ${fmt.count(text.length - HARD_SLICE)} more characters` }), el("pre", { class: className }, text.slice(HARD_SLICE))));
  }
  return wrap;
}

function toast(message) {
  const node = document.getElementById("toast");
  node.textContent = message;
  node.classList.add("show");
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove("show"), 3500);
}

function fatal(message) {
  const node = document.getElementById("fatal");
  node.textContent = message;
  node.hidden = false;
  stopPolling();
}

function confirmDialog({ title, body, confirmLabel = "Confirm", danger = false, input = null }) {
  const dialog = document.getElementById("confirm");
  document.getElementById("confirm-title").textContent = title;
  document.getElementById("confirm-body").textContent = body || "";
  const inputLabel = document.getElementById("confirm-input-label");
  const field = document.getElementById("confirm-input");
  inputLabel.hidden = input === null;
  if (input !== null) {
    document.getElementById("confirm-input-name").textContent = input;
    field.value = "";
  }
  const ok = document.getElementById("confirm-ok");
  ok.textContent = confirmLabel;
  ok.classList.toggle("btn-danger", danger);
  ok.classList.toggle("btn-primary", !danger);
  return new Promise((resolve) => {
    dialog.returnValue = "cancel";
    dialog.onclose = () => resolve(dialog.returnValue === "ok" ? (input !== null ? field.value : true) : false);
    dialog.showModal();
  });
}

function formError(form, message) {
  const node = form.querySelector(".form-error");
  if (!node) return;
  node.textContent = message || "";
  node.hidden = !message;
}
// --- end dom ---

// --- poller ---
let pollTimer = null;
let pollStopped = false;
let errorCount = 0;
store.cursorsEvents = null;

function stopPolling() {
  pollStopped = true;
  clearTimeout(pollTimer);
}

function schedule(now = false) {
  if (pollStopped) return;
  clearTimeout(pollTimer);
  const backoff = errorCount ? Math.min(BACKOFF_MAX, REFRESH_VISIBLE * 2 ** errorCount) : 0;
  const base = document.hidden ? REFRESH_HIDDEN : REFRESH_VISIBLE;
  pollTimer = setTimeout(tick, now ? 0 : Math.max(base, backoff));
}

async function tick() {
  if (pollStopped) return;
  clearTimeout(pollTimer);
  try {
    if (store.auth === "unknown") {
      const me = await api.get("/api/auth/me");
      store.me = me;
      setAuth(me.authenticated ? "ok" : "unpaired");
    }
    if (store.auth !== "ok") return schedule();
    if (store.cursorsEvents === null) {
      const newest = await api.get("/api/events?order=desc&limit=1");
      store.cursorsEvents = newest.length ? newest[0].id : 0;
    } else {
      for (let page = 0; page < 10; page += 1) {
        const events = await api.get(`/api/events?after=${store.cursorsEvents}`);
        if (events.length) store.cursorsEvents = events[events.length - 1].id;
        if (events.length < PAGE_EVENTS) break;
      }
    }
    if (store.snapshotsStale) {
      await refreshSnapshots();
      store.snapshotsStale = false;
    }
    if (store.route.view === "run" && store.detail.id) await pollRun();
    if (errorCount) markDirty("nav");
    errorCount = 0;
    setBanner(null);
  } catch (error) {
    if (!(error instanceof ApiError) || error.status === 0 || error.status >= 500) {
      errorCount += 1;
      if (errorCount >= 2) setBanner(`Orchestra unreachable, retrying in ${Math.round(Math.min(BACKOFF_MAX, REFRESH_VISIBLE * 2 ** errorCount) / 1000)}s`);
    }
  }
  schedule();
}

function setBanner(message) {
  const banner = document.getElementById("net-banner");
  banner.hidden = !message;
  banner.textContent = message || "";
}

async function refreshSnapshots() {
  const [runs, attention, profiles, groups] = await Promise.all([
    api.get(`/api/runs?order=desc&limit=${PAGE_RUNS}`),
    api.get("/api/attention?status=open"),
    api.get("/api/profiles"),
    api.get("/api/groups"),
  ]);
  for (const run of runs) store.runs.set(run.id, run);
  if (runs.length) {
    const floor = runs[runs.length - 1].id;
    if (store.runsFloor === null || floor < store.runsFloor) store.runsFloor = floor;
    if (runs.length < PAGE_RUNS) store.runsExhausted = true;
  } else {
    store.runsExhausted = true;
  }
  store.attention = attention;
  store.profiles = profiles;
  store.groups = groups;
  if (store.route.view === "run" && store.detail.id) {
    try {
      store.detail.run = await api.get(`/api/runs/${store.detail.id}`);
      store.detail.messages = await api.get(`/api/runs/${store.detail.id}/messages`);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        toast(`Run ${store.detail.id} no longer exists`);
        location.hash = "#/";
      } else {
        throw error;
      }
    }
    store.detail.loaded.changes = false;
    store.detail.loaded.usage = false;
    store.detail.loaded.artifacts = false;
  }
  markDirty("fleet", "attention", "config", "nav", "run");
}

async function pollRun() {
  const detail = store.detail;
  if (!detail.run) {
    detail.run = await api.get(`/api/runs/${detail.id}`);
    detail.messages = await api.get(`/api/runs/${detail.id}/messages`);
  }
  if (detail.historyFloor === null) {
    // First open: the newest page only, so long runs open fast. Load earlier history pages backwards on demand.
    absorbHistory(detail, await api.get(`/api/runs/${detail.id}/events?order=desc&limit=${PAGE_EVENTS}`));
    markDirty("run");
  } else {
    for (let page = 0; page < 10; page += 1) {
      const events = await api.get(`/api/runs/${detail.id}/events?after=${detail.eventsAfter}`);
      if (events.length) {
        detail.eventsAfter = events[events.length - 1].id;
        detail.events.push(...events);
        markDirty("run");
      }
      if (events.length < PAGE_EVENTS) break;
    }
  }
  const section = store.route.section;
  if (section === "changes" && !detail.loaded.changes) {
    detail.changes = await api.get(`/api/runs/${detail.id}/changes`);
    detail.loaded.changes = true;
    markDirty("run");
  }
  if (section === "usage" && !detail.loaded.usage) {
    detail.usage = await api.get(`/api/runs/${detail.id}/usage`);
    detail.loaded.usage = true;
    markDirty("run");
  }
  if (section === "artifacts" && !detail.loaded.artifacts) {
    detail.artifacts = await api.get(`/api/runs/${detail.id}/artifacts`);
    detail.loaded.artifacts = true;
    markDirty("run");
  }
  if (section === "evidence" && !detail.dependencies) {
    detail.dependencies = await api.get(`/api/runs/${detail.id}/dependencies`);
    detail.children = await api.get(`/api/runs/${detail.id}/children`);
    markDirty("run");
  }
}
// --- end poller ---

// --- actions ---
async function act(key, element, work) {
  if (store.ui.inflight.has(key)) return;
  store.ui.inflight.add(key);
  if (element) {
    element.disabled = true;
    element.setAttribute("aria-busy", "true");
  }
  try {
    await work();
  } finally {
    store.ui.inflight.delete(key);
    if (element) {
      element.disabled = false;
      element.removeAttribute("aria-busy");
    }
  }
}

function actionError(message) {
  const header = document.getElementById("run-header");
  let strip = header.querySelector(".action-error");
  if (!strip) {
    strip = el("p", { class: "form-error action-error" });
    header.append(strip);
  }
  strip.textContent = message;
  strip.hidden = !message;
}

const ACTIONS = {
  "load-more": (button) => act("load-more", button, async () => {
    const older = await api.get(`/api/runs?order=desc&limit=${PAGE_RUNS}&before=${store.runsFloor}`);
    for (const run of older) store.runs.set(run.id, run);
    if (older.length) store.runsFloor = older[older.length - 1].id;
    if (older.length < PAGE_RUNS) store.runsExhausted = true;
    markDirty("fleet");
  }),
  "filter-status": (button) => {
    const key = button.dataset.status;
    if (store.filters.status.has(key)) store.filters.status.delete(key);
    else store.filters.status.add(key);
    markDirty("fleet");
  },
  "open-run": (node) => {
    location.hash = `#/runs/${node.dataset.run}`;
  },
  "run-direct": () => {
    const run = store.detail.run;
    if (!run || !ACTIVE_STATUSES.has(run.status)) return;
    const dialog = document.getElementById("direct");
    document.getElementById("direct-title").textContent = `Direct ${runLabel(run)}`;
    dialog.showModal();
    dialog.querySelector("textarea").focus();
  },
  "direct-cancel": () => document.getElementById("direct").close(),
  "run-pause": (button) => runVerb("pause", button, {}),
  "run-resume": (button) => runVerb("resume", button, {}),
  "run-stop": async (button) => {
    const run = store.detail.run;
    const reason = await confirmDialog({
      title: `Stop run ${runLabel(run)}?`,
      body: "The stop is queued and delivered at the next safe boundary.",
      confirmLabel: "Stop run",
      danger: true,
      input: "Reason (optional)",
    });
    if (reason === false) return;
    runVerb("stop", button, { reason: reason || "stopped by operator" });
  },
  "logout": async (button) => {
    const sure = await confirmDialog({ title: "Log out this browser?", body: "The browser device token is revoked and the cookie is cleared.", confirmLabel: "Log out" });
    if (!sure) return;
    await act("logout", button, async () => {
      await api.post("/api/auth/logout");
      setAuth("unpaired");
    });
  },
  "answer-option": (button) => {
    const form = button.closest(".att-item").querySelector("form textarea");
    form.value = button.dataset.value;
    form.focus();
  },
};

async function runVerb(verb, element, body) {
  const runId = store.detail.id;
  if (!runId) return;
  await act(`${runId}:${verb}`, element, async () => {
    try {
      await api.post(`/api/runs/${runId}/${verb}`, body);
      actionError("");
      toast(verb === "resume" ? "Run requeued"
        : verb === "pause" ? "Pause queued; the run parks at the next safe boundary"
        : `${verb} queued for delivery`);
      schedule(true);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        actionError("Run is no longer waiting.");
        schedule(true);
      } else {
        actionError(error.message);
      }
    }
  });
}

const FORMS = {
  pair: (form) => act("pair", form.querySelector("button[type=submit]"), async () => {
    const code = form.elements.code.value.trim();
    const name = form.elements.name.value.trim() || "Browser";
    try {
      await req("POST", "/api/auth/pair/redeem", { code, name, cookie: true });
      formError(form, "");
      setAuth("ok");
      store.snapshotsStale = true;
      schedule(true);
      toast(`Paired as ${name}`);
    } catch (error) {
      formError(form, error.message);
    }
  }),
  direct: (form, event) => {
    const verb = event?.submitter?.value === "interrupt" ? "interrupt" : "tell";
    return act(`${store.detail.id}:${verb}`, event?.submitter, async () => {
      const message = form.elements.message.value.trim();
      if (!message) return;
      try {
        await api.post(`/api/runs/${store.detail.id}/${verb}`, { message });
        form.elements.message.value = "";
        formError(form, "");
        document.getElementById("direct").close();
        toast(verb === "interrupt" ? "Interrupt queued; the current step is cancelled" : "Direction queued for the next boundary");
        schedule(true);
      } catch (error) {
        formError(form, error.message);
      }
    });
  },
  dispatch: (form) => act("dispatch", form.querySelector("button[type=submit]"), async () => {
    const values = form.elements;
    const body = {
      request_id: store.ui.dispatchRequestId,
      profile: values.profile.value,
      objective: values.objective.value,
      group: values.group.value || "general",
      strategy: values.strategy.value,
      permission_mode: values.permission_mode.value,
      requested_by: "operator",
    };
    if (values.title.value.trim()) body.title = values.title.value.trim();
    if (values.cwd.value.trim()) body.cwd = values.cwd.value.trim();
    if (values.ref.value.trim()) body.ref = values.ref.value.trim();
    const limits = {};
    if (values.max_rounds.value) limits.max_rounds = Number(values.max_rounds.value);
    if (values.active_seconds.value) limits.active_seconds = Number(values.active_seconds.value);
    if (Object.keys(limits).length) body.limits = limits;
    const verifyArgv = values.verify.value.split("\n").map((line) => line.trim()).filter(Boolean);
    if (verifyArgv.length) body.verify = { argv: verifyArgv, timeout_seconds: Number(values.verify_timeout.value) || 600 };
    if (values.max_children.value) body.max_children = Number(values.max_children.value);
    if (values.max_child_tier.value) body.max_child_tier = Number(values.max_child_tier.value);
    const after = [];
    for (const line of values.after.value.split("\n")) {
      const parts = line.trim().split(/\s+/).filter(Boolean);
      if (!parts.length) continue;
      const entry = { run_id: Number(parts[0]) };
      if (parts[1]) entry.condition = parts[1];
      after.push(entry);
    }
    if (after.length) body.after = after;
    if (body.permission_mode === "danger-full-access") {
      const cwd = body.cwd || groupCwd(body.group) || "the group default directory";
      const sure = await confirmDialog({
        title: "Allow full access?",
        body: `This run may modify anything reachable from ${cwd}.`,
        confirmLabel: "Dispatch with full access",
        danger: true,
      });
      if (!sure) return;
    }
    try {
      const run = await api.post("/api/runs", body);
      formError(form, "");
      form.reset();
      store.ui.dispatchRequestId = crypto.randomUUID();
      document.getElementById("new-run").open = false;
      toast(`Run ${runLabel(run)} queued`);
      store.snapshotsStale = true;
      location.hash = `#/runs/${run.id}`;
    } catch (error) {
      formError(form, error.message);
    }
  }),
  answer: (form) => act(`answer:${form.dataset.attention}`, form.querySelector("button[type=submit]"), async () => {
    const item = store.attention.find((entry) => entry.attention_id === form.dataset.attention);
    const answer = form.elements.answer.value.trim();
    if (!answer && item?.kind !== "alert") return;
    if (item?.lease && item.lease.expires_at && Date.parse(item.lease.expires_at) > Date.now()) {
      const sure = await confirmDialog({
        title: "Override the automation lease?",
        body: `${item.lease.holder} currently holds a lease on this item.`,
        confirmLabel: "Answer anyway",
        danger: true,
      });
      if (!sure) return;
    }
    try {
      await api.post(`/api/attention/${form.dataset.attention}/answer`, { answer: answer || "acknowledged" });
      formError(form, "");
      form.closest(".att-item").classList.add("answered");
      toast("Answered");
      store.snapshotsStale = true;
      schedule(true);
    } catch (error) {
      formError(form, error.message);
      schedule(true);
    }
  }),
};
// --- end actions ---

// --- views ---
function runPrefix(run) {
  const group = store.groups.find((entry) => entry.group_id === run.group_id);
  return group ? `${group.slug}#${run.group_seq}` : `#${run.group_seq ?? run.id}`;
}

function runLabel(run) {
  if (!run) return "";
  const prefix = runPrefix(run);
  return run.title ? `${prefix} ${run.title}` : prefix;
}

function profileSlug(run) {
  const profile = store.profiles.find((entry) => entry.id === run.profile_id);
  return profile ? profile.slug : "";
}

function groupCwd(slugOrId) {
  const group = store.groups.find((entry) => entry.slug === slugOrId || String(entry.group_id) === String(slugOrId));
  return group?.default_cwd || "";
}

function statusNode(run) {
  if (run.paused) return el("span", { class: "status st-paused", text: "paused" });
  const text = run.status === "waiting" && run.waiting_kind ? `waiting · ${run.waiting_kind}` : run.status;
  return el("span", { class: `status st-${run.status}`, text });
}

function renderNav() {
  const open = store.attention.length;
  const badge = document.getElementById("attention-badge");
  badge.hidden = !open;
  badge.textContent = String(open);
  for (const link of document.querySelectorAll("[data-nav]")) {
    const current = (store.route.view === "run" ? "fleet" : store.route.view) === link.dataset.nav;
    if (current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function fillOptions(select, rows, valueKey, labelKey) {
  const signature = JSON.stringify(rows.map((row) => [row[valueKey], row[labelKey], row.archived]));
  if (select._sig === signature) return;
  select._sig = signature;
  const previous = select.value;
  const keepFirst = select.querySelector("option[value='']");
  select.replaceChildren(...(keepFirst ? [keepFirst] : []));
  for (const row of rows) {
    if (row.archived) continue;
    select.append(el("option", { value: String(row[valueKey]), text: row[labelKey] }));
  }
  select.value = previous;
  if (select.selectedIndex === -1) select.selectedIndex = 0;
}

function renderFleet() {
  for (const select of document.querySelectorAll("select[data-options=profiles]")) fillOptions(select, store.profiles, select.dataset.filter ? "id" : "slug", "slug");
  for (const select of document.querySelectorAll("select[data-options=groups]")) fillOptions(select, store.groups, select.dataset.filter ? "group_id" : "slug", "slug");

  const runs = [...store.runs.values()].sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
  const counts = { queued: 0, running: 0, waiting: 0, failed: 0 };
  for (const run of runs) {
    for (const [key, statuses] of Object.entries(STATUS_GROUPS)) {
      if (key !== "done" && statuses.includes(run.status)) counts[key] = (counts[key] || 0) + 1;
    }
  }
  const countsBox = document.getElementById("counts");
  const entries = [
    ["running", "running", ""],
    ["queued", "queued", ""],
    ["waiting", "waiting", "warn"],
    ["failed", "failed", "bad"],
  ];
  keyedList(countsBox, entries, (entry) => entry[0],
    (entry) => `${counts[entry[0]] || 0}:${store.filters.status.has(entry[0])}`,
    (entry) => {
      const button = el("button", { type: "button", class: entry[2], dataset: { action: "filter-status", status: entry[0] }, "aria-pressed": String(store.filters.status.has(entry[0])) },
        el("strong", { text: String(counts[entry[0]] || 0) }), entry[1]);
      return button;
    },
    (node, entry) => {
      node.querySelector("strong").textContent = String(counts[entry[0]] || 0);
      node.setAttribute("aria-pressed", String(store.filters.status.has(entry[0])));
    });

  const visible = filterRuns(runs, store.filters);
  const state = document.getElementById("fleet-state");
  if (store.boardRevision < 0) state.textContent = "Loading runs…";
  else if (!runs.length) state.textContent = "No runs yet. Dispatch the first one with New run.";
  else if (!visible.length) state.textContent = "No runs match the current filters.";
  state.hidden = Boolean(visible.length) || store.boardRevision < 0 && false;
  state.hidden = Boolean(visible.length);

  const list = document.getElementById("run-list");
  keyedList(list, visible, (run) => run.id, runSignature, buildRunRow, updateRunRow);
  document.querySelector("[data-action=load-more]").hidden = store.runsExhausted || store.runsFloor === null;
}

function runRowChildren(run) {
  const meta = [];
  const profile = profileSlug(run);
  if (profile) meta.push(el("span", { class: "mono", text: profile }));
  meta.push(el("span", { class: "mono", text: `${run.route_provider}/${run.route_model}${run.route_effort ? "·" + run.route_effort : ""}` }));
  if (run.strategy === "ralph") meta.push(el("span", { class: "chip accent", text: "ralph" }));
  meta.push(el("span", { text: `rounds ${run.rounds_started}/${run.max_rounds}` }));
  meta.push(el("span", { text: fmt.duration(run.active_seconds) }));
  meta.push(el("span", { title: fmt.count(run.tokens_total), text: `${fmt.tokens(run.tokens_total)} tok` }));
  if (run.cache_epoch > 0) meta.push(el("span", { class: "chip warn", text: `epoch ${run.cache_epoch}` }));
  meta.push(el("span", { title: run.updated_at, text: fmt.rel(run.updated_at) }));
  const children = [
    el("div", { class: "row-1" }, statusNode(run), el("span", { class: "mono muted", text: runPrefix(run) }),
      el("span", { class: "row-title", text: fmt.excerpt(run.title || run.objective, 160) })),
    el("div", { class: "row-2" }, meta),
  ];
  if ((run.status === "failed" || run.status === "timed_out") && run.error) {
    children.push(el("div", { class: "row-error", text: fmt.excerpt(run.error, 200) }));
  }
  return children;
}

function buildRunRow(run) {
  const row = el("li", { class: "run-row", tabindex: "0", role: "link", dataset: { action: "open-run", run: String(run.id) } }, runRowChildren(run));
  updateRowClasses(row, run);
  return row;
}

function updateRunRow(row, run) {
  row.replaceChildren(...runRowChildren(run));
  updateRowClasses(row, run);
}

function updateRowClasses(row, run) {
  row.classList.toggle("hot-waiting", run.status === "waiting" && !run.paused);
  row.classList.toggle("hot-failed", run.status === "failed" || run.status === "timed_out");
  row.classList.toggle("quiet", ["completed", "stopped", "skipped"].includes(run.status) || run.paused);
}

const RUN_ACTION_MATRIX = {
  queued: ["direct", "stop"],
  starting: ["direct", "pause", "stop"],
  running: ["direct", "pause", "stop"],
  waiting: ["direct", "resume", "stop"],
};

function renderRunHeader(run) {
  const header = document.getElementById("run-header");
  const chips = [];
  const profile = profileSlug(run);
  if (profile) chips.push(el("span", { class: "chip", text: profile }));
  chips.push(el("span", { class: "chip mono", text: `${run.route_provider}/${run.route_model}${run.route_effort ? "·" + run.route_effort : ""}` }));
  chips.push(el("span", { class: run.permission_mode === "danger-full-access" ? "chip bad" : "chip", text: run.permission_mode }));
  chips.push(el("span", { class: "chip", text: run.strategy }));
  if (run.strategy === "goal") {
    chips.push(el("span", { class: "chip accent", text: `goal ${run.goal_state || "pending"} · rounds ${run.rounds_started}/${run.max_rounds}` }));
    if (run.verification_repairs > 0) chips.push(el("span", { class: "chip warn", text: `repairs ${run.verification_repairs}` }));
    if (run.goal_corrected) chips.push(el("span", { class: "chip warn", text: "corrected" }));
  } else {
    chips.push(el("span", { class: "chip accent", text: `attempts ${run.rounds_started}/${run.max_rounds}` }));
  }
  chips.push(el("span", { class: "chip", title: `limit ${fmt.duration(run.active_seconds_limit)}`, text: `active ${fmt.duration(run.active_seconds)}` }));
  if (run.resume_count > 0) chips.push(el("span", { class: "chip", text: `resumed ×${run.resume_count}` }));
  if (run.cache_epoch > 0) chips.push(el("span", { class: "chip warn", text: `cache epoch ${run.cache_epoch}` }));

  const actions = [];
  const allowed = RUN_ACTION_MATRIX[run.status] || [];
  if (allowed.includes("resume")) actions.push(el("button", { class: "btn btn-primary", dataset: { action: "run-resume" }, text: "Resume" }));
  if (allowed.includes("direct")) actions.push(el("button", { class: "btn", dataset: { action: "run-direct" }, title: "Tell or interrupt the run ( t )", text: "Direct…" }));
  if (allowed.includes("pause")) actions.push(el("button", { class: "btn", dataset: { action: "run-pause" }, title: "Park at the next safe boundary; Resume continues the same session", text: "Pause" }));
  if (allowed.includes("stop")) actions.push(el("button", { class: "btn btn-danger", dataset: { action: "run-stop" }, text: "Stop" }));
  if (run.status === "waiting" && ["attention", "permission", "verification"].includes(run.waiting_kind)) {
    actions.push(el("a", { class: "btn", href: "#/attention", text: "Answer →" }));
  }

  const previousError = header.querySelector(".action-error")?.textContent || "";
  header.replaceChildren(...[
    el("div", { class: "head-1" },
      el("a", { href: "#/", text: "← Fleet" }),
      el("span", { class: "head-title", text: runLabel(run) }),
      statusNode(run),
      el("span", { class: "muted", title: run.created_at, text: `created ${fmt.rel(run.created_at)}` })),
    run.waiting_detail ? el("p", { class: "waiting-detail", text: fmt.excerpt(run.waiting_detail, 300) }) : null,
    el("div", { class: "head-chips" }, chips),
    el("div", { class: "head-actions" }, actions),
  ].filter(Boolean));
  if (previousError) actionError(previousError);
}

function renderRunTabs() {
  const tabs = document.getElementById("run-tabs");
  keyedList(tabs, SECTIONS, (section) => section,
    (section) => `${section === store.route.section}`,
    (section) => el("button", { role: "tab", "aria-selected": String(section === store.route.section), dataset: { action: "run-tab", }, text: section[0].toUpperCase() + section.slice(1) }),
    (node, section) => node.setAttribute("aria-selected", String(section === store.route.section)));
  let index = 0;
  for (const node of tabs.children) {
    const section = SECTIONS[index];
    node.onclick = () => { location.hash = `#/runs/${store.detail.id}/${section}`; };
    index += 1;
  }
  for (const panel of document.querySelectorAll("[data-sec]")) {
    panel.hidden = panel.dataset.sec !== store.route.section;
  }
}

const VISIBLE_TYPES = new Set(["run.queued", "run.started", "run.terminal", "run.resume_scheduled", "run.paused",
  "attention.opened", "attention.answered", "children.waiting", "children.settled", "verification.failed"]);
const QUIET_LIFECYCLE = new Set(["run.queued", "run.started", "run.resume_scheduled",
  "children.waiting", "children.settled"]);
const TERMINAL_EMOJI = { completed: "✅", failed: "❌", timed_out: "⏱", stopped: "⏹", skipped: "⏭" };

function threadEntryKind(entry) {
  if (entry.kind === "message") return entry.item.kind === "tell" ? "tell" : "control";
  const type = entry.item.type;
  if (VISIBLE_TYPES.has(type)) return "lifecycle";
  if (type === "acp.tool_call") return "tool";
  if (type === "acp.tool_call_update") return "tool-update";
  if (type.startsWith("dsh.goal")) return "goal";
  if (type.includes("compact")) return "compaction";
  if (type.startsWith("dsh.") && /thought|think|reason/.test(type)) return "thinking";
  const text = extractText(entry.item.payload);
  if (type.startsWith("dsh.") && text) return "text";
  return "machine";
}

function isMachineEntry(entry) {
  const kind = threadEntryKind(entry);
  if (kind === "machine" || kind === "goal") return true;
  return kind === "lifecycle" && QUIET_LIFECYCLE.has(entry.item.type);
}

function buildThreadRow(entry, toolIndex) {
  const kind = threadEntryKind(entry);
  const machine = isMachineEntry(entry);
  const at = entry.at || "";
  const head = (label) => el("div", { class: "item-head" }, el("span", { title: at, text: fmt.rel(at) }), el("span", { class: "mono", text: label }));
  const finish = (row) => {
    if (machine) row.classList.add("machine");
    return row;
  };
  if (kind === "tell") {
    const message = entry.item;
    return el("div", { class: "feed-item tell" },
      head(`📨 tell · ${message.sender || "operator"} · ${message.status || ""}`),
      el("div", { class: "item-body" }, boundedPre(message.body)));
  }
  if (kind === "control") {
    const message = entry.item;
    const body = message.body ? ` — ${fmt.excerpt(message.body, 140)}` : "";
    return el("div", { class: "feed-item marker", title: message.body || "", text: `🎛 ${message.kind} · ${message.sender || ""} · ${message.status || ""}${body} · ${fmt.rel(at)}` });
  }
  if (kind === "lifecycle") {
    const event = entry.item;
    let cls = "", label = event.type;
    if (event.type === "run.terminal") {
      const status = event.payload?.status || "terminal";
      cls = status === "completed" ? "good" : status === "failed" || status === "timed_out" ? "bad" : "";
      label = `${TERMINAL_EMOJI[status] || "🏁"} run ${status}`;
    } else if (event.type === "attention.opened") {
      cls = "warn";
      label = `✋ attention opened · ${event.payload?.kind || ""}`;
    } else if (event.type === "attention.answered") {
      label = "🤝 attention answered";
    } else if (event.type === "run.paused") {
      label = event.payload?.reason ? `⏸ paused — ${fmt.excerpt(event.payload.reason, 120)}` : "⏸ paused by operator";
    } else if (event.type === "verification.failed") {
      cls = "warn";
      label = "❌ verification failed";
    } else {
      label = event.type.replace("run.", "run ").replace("children.", "children ");
    }
    const row = el("div", { class: `feed-item marker ${cls}` }, el("span", { title: at, text: `${label} · ${fmt.rel(at)}` }));
    if (event.type === "verification.failed" && event.payload?.output) {
      row.classList.remove("marker");
      row.append(el("div", { class: "item-body" }, boundedPre(event.payload.output)));
    }
    if (event.type === "attention.opened" && event.payload?.prompt) {
      row.classList.remove("marker");
      row.append(el("div", { class: "item-body" }, el("a", { href: "#/attention", text: fmt.excerpt(event.payload.prompt, 200) })));
    }
    return finish(row);
  }
  if (kind === "goal") {
    const payload = entry.item.payload || {};
    return finish(el("div", { class: "feed-item marker" }, el("span", { title: at, text: `🎯 goal ${payload.state || payload.phase || "changed"} · ${fmt.rel(at)}` })));
  }
  if (kind === "compaction") {
    return el("div", { class: "feed-item marker", text: `🗜 context compacted · ${fmt.rel(at)}` });
  }
  if (kind === "thinking") {
    return el("div", { class: "feed-item thinking" }, head(`💭 ${entry.item.type}`),
      el("div", { class: "item-body" }, boundedPre(extractText(entry.item.payload))));
  }
  if (kind === "tool") {
    const payload = entry.item.payload || {};
    const id = payload.toolCallId || payload.tool_call_id || `tool-${entry.item.id}`;
    const details = el("details", null,
      el("summary", { text: `⚙ ${payload.title || payload.name || "tool"} · ${payload.status || "running"}` }),
      boundedPre(JSON.stringify(payload, null, 1), "tool-payload"));
    const row = el("div", { class: "feed-item tool", dataset: { tool: id } }, head(entry.item.type), details);
    toolIndex.set(id, row);
    return row;
  }
  if (kind === "text") {
    return el("div", { class: "feed-item" }, head(`💬 ${entry.item.type}`), el("div", { class: "item-body" }, boundedPre(extractText(entry.item.payload))));
  }
  const event = entry.item;
  return finish(el("div", { class: "feed-item" }, head(`🛠 ${event.type}`),
    el("details", null, el("summary", { class: "muted", text: "payload" }), boundedPre(JSON.stringify(event.payload ?? {}, null, 1)))));
}

function applyToolUpdate(entry, toolIndex) {
  const payload = entry.item.payload || {};
  const id = payload.toolCallId || payload.tool_call_id;
  const row = id && toolIndex.get(id);
  if (!row) return buildThreadRow({ ...entry, item: entry.item }, toolIndex);
  const summary = row.querySelector("summary");
  if (payload.status) {
    const base = summary.textContent.split(" · ")[0];
    summary.textContent = `${base} · ${payload.status}`;
  }
  const output = extractText(payload);
  if (output && !row.querySelector(".tool-output")) {
    row.querySelector("details").append(el("div", { class: "tool-output" }, boundedPre(output)));
  }
  return null;
}

function renderActivity() {
  const detail = store.detail;
  const run = detail.run;
  const feed = document.getElementById("feed");
  const thread = mergeThread(detail.events, detail.messages);
  const atTail = feed.scrollHeight - feed.scrollTop - feed.clientHeight < TAIL_THRESHOLD;
  const previousTop = feed.scrollTop;
  feed._tools = feed._tools || new Map();
  const machineTotal = thread.filter(isMachineEntry).length;
  document.getElementById("machine-count").textContent = machineTotal ? `(${machineTotal})` : "";
  document.getElementById("load-history").hidden = detail.historyDone || detail.historyFloor === null;

  const brief = document.getElementById("feed-brief");
  brief.hidden = false;
  brief.replaceChildren(
    el("span", { class: "brief-icon", text: "🎯" }),
    el("span", { title: run.objective, text: fmt.excerpt(run.objective, 300) }));
  const handoff = document.getElementById("feed-handoff");
  const terminal = !ACTIVE_STATUSES.has(run.status);
  handoff.hidden = !terminal;
  if (terminal) {
    handoff.classList.toggle("bad", run.status === "failed" || run.status === "timed_out");
    handoff.replaceChildren(
      el("div", { class: "handoff-head", text: `${TERMINAL_EMOJI[run.status] || "🏁"} ${run.status}` }),
      boundedPre(run.summary || run.error || "The run left no summary."));
  }

  const rows = [];
  const updates = [];
  for (const entry of thread) {
    if (threadEntryKind(entry) === "tool-update") {
      updates.push(entry);
      continue;
    }
    rows.push(entry);
  }
  keyedList(feed, rows, (entry) => entry.key,
    (entry) => entry.kind === "message" ? `${entry.item.status}` : "",
    (entry) => buildThreadRow(entry, feed._tools),
    (node, entry) => {
      if (entry.kind === "message" && entry.item.kind === "tell") {
        node.querySelector(".item-head .mono").textContent = `📨 tell · ${entry.item.sender || "operator"} · ${entry.item.status || ""}`;
      } else if (entry.kind === "message") {
        const body = entry.item.body ? ` — ${fmt.excerpt(entry.item.body, 140)}` : "";
        node.textContent = `🎛 ${entry.item.kind} · ${entry.item.sender || ""} · ${entry.item.status || ""}${body} · ${fmt.rel(entry.at)}`;
      }
    });
  for (const entry of updates) {
    if (!feed._appliedUpdates) feed._appliedUpdates = new Set();
    if (feed._appliedUpdates.has(entry.key)) continue;
    const orphan = applyToolUpdate(entry, feed._tools);
    if (orphan && !detail.historyDone) continue; // its tool call is probably on an earlier page; retry once that loads
    feed._appliedUpdates.add(entry.key);
    if (orphan) feed.append(orphan);
  }
  feed.classList.toggle("show-machine", document.getElementById("show-machine").checked);
  if (store.ui.follow) feed.scrollTop = feed.scrollHeight;
  else feed.scrollTop = previousTop;
  updateReturnLive();
}

function setFollow(value) {
  store.ui.follow = value;
  document.getElementById("follow-live").checked = value;
  if (value) {
    const feed = document.getElementById("feed");
    feed.scrollTop = feed.scrollHeight;
  }
  updateReturnLive();
}

function updateReturnLive() {
  document.getElementById("return-live").hidden = store.ui.follow;
}

function renderChanges() {
  const panel = document.getElementById("sec-changes");
  const detail = store.detail;
  const run = detail.run;
  const blocks = [];
  const facts = el("dl", { class: "kv" });
  const fact = (name, value, mono = true) => {
    if (!value) return;
    facts.append(el("dt", { text: name }), el("dd", { class: mono ? "mono" : "", text: value }));
  };
  fact("branch", run.branch);
  fact("base", run.start_ref);
  fact("head", run.end_ref);
  if (run.git?.diff_stat) fact("diff stat", run.git.diff_stat);
  blocks.push(el("div", { class: "evidence-block" }, facts));
  if (!detail.loaded.changes) {
    blocks.push(el("p", { class: "view-state", text: "Loading changes…" }));
  } else {
    const changes = detail.changes || {};
    if (changes.status) blocks.push(el("div", { class: "evidence-block" }, el("h2", { text: "Working tree" }), boundedPre(changes.status)));
    const parsed = parseDiff(changes.diff);
    if (changes.diff && changes.diff.length >= 200000) blocks.push(el("p", { class: "view-state", text: "The patch reached the 200 KB API bound and is probably truncated." }));
    if (!parsed.files.length) blocks.push(el("p", { class: "view-state", text: "No diff against the base reference." }));
    const open = parsed.files.length <= 3;
    for (const file of parsed.files) {
      const lines = el("div", { class: "diff-lines" },
        el("pre", null, ...file.lines.map((line) => el("span", { class: line.cls || null, text: line.text + "\n" }))));
      blocks.push(el("details", { class: "diff-file", ...(open ? { open: "" } : {}) },
        el("summary", null, el("span", { text: file.path + " " }), el("span", { class: "d-plus", text: `+${file.add}` }), " ", el("span", { class: "d-minus", text: `−${file.del}` })),
        lines));
    }
  }
  panel.replaceChildren(...blocks);
}

function renderArtifacts() {
  const panel = document.getElementById("sec-artifacts");
  const detail = store.detail;
  if (!detail.loaded.artifacts) {
    panel.replaceChildren(el("p", { class: "view-state", text: "Loading artifacts…" }));
    return;
  }
  if (!detail.artifacts.length) {
    panel.replaceChildren(el("p", { class: "view-state", text: "No artifacts were published." }));
    return;
  }
  const rows = [];
  for (const artifact of detail.artifacts) {
    const plan = previewPlan(artifact);
    const preview = detail.previews[artifact.artifact_id];
    rows.push(el("tr", null,
      el("td", { class: "mono", text: artifact.name }),
      el("td", { text: artifact.media_type }),
      el("td", { text: fmt.bytes(artifact.byte_size) }),
      el("td", { class: "mono faint", title: artifact.sha256, text: (artifact.sha256 || "").slice(0, 12) }),
      el("td", null,
        plan ? el("button", { type: "button", class: "expand-btn", dataset: { action: "artifact-preview", artifact: artifact.artifact_id }, text: preview ? "hide" : "preview" }) : null,
        plan ? " " : null,
        el("a", { href: `/api/artifacts/${artifact.artifact_id}/content`, text: "download" }))));
    if (preview) rows.push(el("tr", { class: "artifact-preview" }, el("td", { colspan: "5" }, buildArtifactPreview(artifact, preview))));
  }
  const table = el("table", { class: "plain" },
    el("thead", null, el("tr", null, ...["Name", "Type", "Size", "Digest", ""].map((h) => el("th", { text: h })))),
    el("tbody", null, rows));
  panel.replaceChildren(table);
}

function renderUsage() {
  const panel = document.getElementById("sec-usage");
  const detail = store.detail;
  if (!detail.loaded.usage) {
    panel.replaceChildren(el("p", { class: "view-state", text: "Loading usage…" }));
    return;
  }
  const { groups, totals } = aggregateUsage(detail.usage);
  if (!groups.length) {
    panel.replaceChildren(el("p", { class: "view-state", text: "No usage was recorded yet." }));
    return;
  }
  const cell = (value) => el("td", { class: "mono", title: fmt.count(value), text: fmt.tokens(value) });
  const table = el("table", { class: "plain" },
    el("thead", null, el("tr", null, ...["Route", "Epoch", "Descendant", "Input", "Output", "Cache read", "Cache write", "Total"].map((h) => el("th", { text: h })))),
    el("tbody", null,
      ...groups.map((group) => el("tr", null,
        el("td", { class: "mono", text: `${group.provider ?? "?"}/${group.model ?? "?"}` }),
        el("td", { text: String(group.cache_epoch) }),
        el("td", { class: "mono faint", text: group.descendant ? group.descendant.slice(0, 10) : "" }),
        cell(group.input), cell(group.output), cell(group.cache_read), cell(group.cache_write), cell(group.total))),
      el("tr", null, el("td", { text: "Total" }), el("td"), el("td"),
        cell(totals.input), cell(totals.output), cell(totals.cache_read), cell(totals.cache_write), cell(totals.total))));
  panel.replaceChildren(table);
}

function renderEvidence() {
  const panel = document.getElementById("sec-evidence");
  const detail = store.detail;
  const run = detail.run;
  const lineage = el("dl", { class: "kv" });
  const link = (name, id) => {
    if (!id || id === run.id) return;
    lineage.append(el("dt", { text: name }), el("dd", null, el("a", { href: `#/runs/${id}`, class: "mono", text: `run ${id}` })));
  };
  link("root", run.root_run_id);
  link("parent", run.parent_run_id);
  link("retry of", run.retry_of_run_id);
  link("continuation of", run.continuation_of_run_id);
  for (const dependency of detail.dependencies || []) {
    lineage.append(el("dt", { text: `after (${dependency.condition})` }), el("dd", null, el("a", { href: `#/runs/${dependency.depends_on_run_id}`, class: "mono", text: `run ${dependency.depends_on_run_id}` })));
  }
  for (const child of detail.children || []) {
    lineage.append(el("dt", { text: "child" }), el("dd", null, el("a", { href: `#/runs/${child.id}`, class: "mono", text: `run ${child.id} · ${child.status}` })));
  }
  if (!lineage.childNodes.length) lineage.append(el("dt", { text: "lineage" }), el("dd", { text: "none" }));

  const facts = el("dl", { class: "kv" });
  const fact = (name, value) => { if (value != null && value !== "") facts.append(el("dt", { text: name }), el("dd", { class: "mono", text: String(value) })); };
  fact("run id", run.id);
  fact("request id", run.request_id);
  fact("worktree", run.workdir);
  fact("cwd", run.cwd);
  fact("session", run.dsh_session_id);
  fact("resume count", run.resume_count);
  fact("requested by", run.requested_by);
  fact("created", run.created_at);
  fact("started", run.started_at);
  fact("finished", run.finished_at);
  if (run.verify) fact("verifier", `${(run.verify.argv || []).join(" ")} (${run.verify.timeout_seconds}s)`);

  // The raw events block persists across renders so its filter box keeps focus while the run polls.
  let raw = panel.querySelector(".raw-events");
  if (!raw) panel.append(raw = buildRawEvents());
  for (const node of [...panel.children]) if (node !== raw) node.remove();
  panel.prepend(
    el("div", { class: "evidence-block" }, el("h2", { text: "Family" }), lineage),
    el("div", { class: "evidence-block" }, el("h2", { text: "Facts" }), facts),
    el("div", { class: "evidence-block" },
      el("details", null, el("summary", { text: "Request snapshot" }), boundedPre(JSON.stringify(run.request_snapshot ?? {}, null, 2))),
      el("details", null, el("summary", { text: "Profile snapshot" }), boundedPre(JSON.stringify(run.profile_snapshot ?? {}, null, 2)))),
  );
  renderRawEvents(raw);
}

function renderRun() {
  const detail = store.detail;
  if (!detail.run) return;
  renderRunHeader(detail.run);
  renderRunTabs();
  const section = store.route.section;
  if (section === "activity") renderActivity();
  if (section === "changes") renderChanges();
  if (section === "artifacts") renderArtifacts();
  if (section === "usage") renderUsage();
  if (section === "evidence") renderEvidence();
}

const KIND_CLASSES = { permission: "warn", protocol_failure: "bad", verification: "warn" };

function buildAttentionItem(item) {
  const answered = item.status !== "open";
  const form = el("form", { dataset: { form: "answer", attention: item.attention_id } },
    el("textarea", { name: "answer", rows: "2", placeholder: item.kind === "alert" ? "Optional note" : "Answer", "aria-label": "Answer" }),
    el("button", { type: "submit", class: "btn btn-primary", text: item.kind === "alert" ? "Acknowledge" : "Answer" }),
    el("p", { class: "form-error", hidden: "" }));
  const context = item.context && Object.keys(item.context).length ? item.context : null;
  const options = Array.isArray(context?.options) && context.options.every((option) => typeof option === "string") ? context.options : null;
  let contextNode = null;
  const rest = context ? Object.entries(context).filter(([key]) => !(options && key === "options")) : [];
  if (rest.length) {
    const kv = el("dl", { class: "kv" });
    for (const [key, value] of rest) {
      kv.append(el("dt", { text: key }), el("dd", { class: "mono", text: typeof value === "string" ? value : JSON.stringify(value) }));
    }
    contextNode = el("div", { class: "att-context" }, kv);
  }
  const expiry = item.expires_at ? el("span", { class: "att-meta" }, "expires ", el("span", { class: "countdown", dataset: { expires: item.expires_at }, text: fmt.countdown(item.expires_at) })) : null;
  const lease = item.lease ? el("span", { class: "att-meta", text: `leased by ${item.lease.holder} until ${fmt.rel(item.lease.expires_at)}` }) : null;
  const run = store.runs.get(item.run_id);
  const node = el("div", { class: `att-item${answered ? " answered" : ""}`, dataset: { attention: item.attention_id } },
    el("div", { class: "att-head" },
      el("span", { class: `chip ${KIND_CLASSES[item.kind] || "accent"}`, text: item.kind }),
      item.blocking === false ? el("span", { class: "chip", text: "non-blocking" }) : null,
      el("span", { class: "att-meta", title: item.created_at, text: `opened ${fmt.rel(item.created_at)}` }),
      expiry, lease,
      el("a", { class: "att-meta", href: `#/runs/${item.run_id}`, text: "open activity →" })),
    run ? el("p", { class: "att-run-context", title: run.objective, text: `🎯 ${fmt.excerpt(run.objective, 200)}` }) : null,
    el("p", { class: "att-prompt", text: item.prompt }),
    contextNode,
    options ? el("div", { class: "att-options" }, options.map((option) => el("button", { type: "button", class: "btn", dataset: { action: "answer-option", value: option }, text: option }))) : null,
    answered ? el("p", { class: "muted", text: `answered: ${fmt.excerpt(item.response || "", 200)}` }) : form,
  );
  return node;
}

function renderAttention() {
  const list = document.getElementById("attention-list");
  const state = document.getElementById("attention-state");
  const open = store.attention;
  state.hidden = open.length > 0;
  if (store.boardRevision >= 0) state.textContent = "Nothing needs attention.";
  const byRun = new Map();
  for (const item of open) {
    if (!byRun.has(item.run_id)) byRun.set(item.run_id, []);
    byRun.get(item.run_id).push(item);
  }
  const groups = [...byRun.entries()];
  keyedList(list, groups, ([runId]) => runId,
    ([runId, items]) => `${items.map((item) => item.attention_id).join()}:${items.length}`,
    ([runId, items]) => {
      const run = store.runs.get(runId);
      return el("section", { class: "att-group" },
        el("h2", null, el("a", { href: `#/runs/${runId}`, text: run ? runLabel(run) : `run ${runId}` }), run ? statusNode(run) : null),
        ...items.map(buildAttentionItem));
    },
    (node, [runId, items]) => {
      const run = store.runs.get(runId);
      const heading = el("h2", null, el("a", { href: `#/runs/${runId}`, text: run ? runLabel(run) : `run ${runId}` }), run ? statusNode(run) : null);
      node.replaceChildren(heading, ...items.map(buildAttentionItem));
    });
  syncCountdownTimer();
}

let countdownTimer = null;
function syncCountdownTimer() {
  const nodes = document.querySelectorAll("#view-attention .countdown");
  const needed = nodes.length > 0 && !document.getElementById("view-attention").hidden;
  if (needed && !countdownTimer) {
    countdownTimer = setInterval(() => {
      for (const node of document.querySelectorAll("#view-attention .countdown")) {
        const text = fmt.countdown(node.dataset.expires);
        node.textContent = text;
        node.classList.toggle("urgent", text !== "expired" && Date.parse(node.dataset.expires) - Date.now() < 60000);
      }
    }, 1000);
  } else if (!needed && countdownTimer) {
    clearInterval(countdownTimer);
    countdownTimer = null;
  }
}

function renderConfig() {
  renderConfigProfiles();
  renderConfigGroups();
  renderConfigIdentities();
  renderConfigPairing();
  renderConfigStorage();
  renderConfigAudit();
  renderConfigDiagnostics();
}

function renderConfigIdentities() {}
function renderConfigPairing() {}
function renderConfigStorage() {}
function renderConfigAudit() {}

function renderConfigProfiles() {
  const profiles = document.getElementById("config-profiles");
  const profileTable = el("table", { class: "plain" },
    el("thead", null, el("tr", null, ...["Name", "Slug", "Route", "Tier", "Concurrency", "State", "Rev"].map((h) => el("th", { text: h })))),
    el("tbody", null, ...store.profiles.map((profile) => el("tr", null,
      el("td", { text: profile.name }),
      el("td", { class: "mono", text: profile.slug }),
      el("td", { class: "mono", text: `${profile.provider}/${profile.model}${profile.effort ? "·" + profile.effort : ""}` }),
      el("td", { text: String(profile.tier) }),
      el("td", { text: profile.max_concurrency == null ? "" : String(profile.max_concurrency) }),
      el("td", { text: profile.archived ? "archived" : profile.enabled === false ? "disabled" : "enabled" }),
      el("td", { text: String(profile.revision) })))));
  profiles.replaceChildren(store.profiles.length ? profileTable : el("p", { class: "view-state", text: "No profiles. Create one with the CLI: orchestra-next profile-create." }));
}

function renderConfigGroups() {
  const groupsBox = document.getElementById("config-groups");
  const groupTable = el("table", { class: "plain" },
    el("thead", null, el("tr", null, ...["Name", "Slug", "Default cwd", "Concurrency", "State", "Rev"].map((h) => el("th", { text: h })))),
    el("tbody", null, ...store.groups.map((group) => el("tr", null,
      el("td", { text: group.name }),
      el("td", { class: "mono", text: group.slug }),
      el("td", { class: "mono", text: group.default_cwd || "" }),
      el("td", { text: group.max_concurrency == null ? "" : String(group.max_concurrency) }),
      el("td", { text: group.archived ? "archived" : "active" }),
      el("td", { text: String(group.revision) })))));
  groupsBox.replaceChildren(groupTable);
}

function renderConfigDiagnostics() {
  const diagnostics = document.getElementById("config-diagnostics");
  const kv = el("dl", { class: "kv" });
  const fact = (name, value) => kv.append(el("dt", { text: name }), el("dd", { class: "mono", text: String(value ?? "") }));
  if (store.me) fact("identity", store.me.kind === "network" ? `trusted network peer ${store.me.id}` : `${store.me.kind} · ${store.me.device?.name ?? store.me.id}`);
  document.querySelector("[data-action=logout]").hidden = store.me?.kind === "network";
  fact("instance", store.instanceId);
  fact("board revision", store.boardRevision);
  fact("poll", errorCount ? `retrying (${errorCount} failures)` : "healthy");
  if (store.readiness) {
    fact("schema", store.readiness.schema);
    fact("dsh", store.readiness.dsh.ok ? `ok · ${store.readiness.dsh.version || ""}` : store.readiness.dsh.error);
    fact("claude sidecar", store.readiness.claude.ok ? "ready" : "unavailable");
    fact("models cached", store.readiness.models_cached);
  }
  diagnostics.replaceChildren(kv);
  if (!store.readiness && store.auth === "ok") {
    api.get("/api/readiness").then((value) => { store.readiness = value; markDirty("config"); }).catch(() => {});
  }
}

function showView(view) {
  const views = { fleet: "view-fleet", run: "view-run", attention: "view-attention", config: "view-config", pairing: "view-pairing" };
  for (const [name, id] of Object.entries(views)) {
    document.getElementById(id).hidden = name !== view;
  }
}

function render() {
  renderQueued = false;
  const marks = new Set(dirty);
  dirty.clear();
  if (store.auth === "unpaired") {
    showView("pairing");
    return;
  }
  if (store.auth !== "ok") return;
  showView(store.route.view);
  renderNav();
  if ((marks.has("fleet") || marks.has("nav")) && store.route.view === "fleet") renderFleet();
  if (marks.has("run") && store.route.view === "run") renderRun();
  if ((marks.has("attention") || marks.has("nav")) && store.route.view === "attention") renderAttention();
  if (marks.has("config") && store.route.view === "config") renderConfig();
  if (marks.has("auth")) markDirty(store.route.view === "run" ? "run" : store.route.view);
}
// --- end views ---

// Feature blocks below extend ACTIONS/FORMS with Object.assign and add their
// own render functions. Each block is owned by one feature; keep them apart.
// --- slice2-routing ---
// --- end slice2-routing ---

// --- slice2-evidence ---
document.getElementById("history-note").after(
  el("button", { id: "load-history", type: "button", class: "btn", dataset: { action: "load-history" }, hidden: "", text: "Load earlier history" }));

Object.assign(ACTIONS, {
  "load-history": (button) => act("load-history", button, async () => {
    const detail = store.detail;
    if (detail.historyDone || !detail.historyFloor) return;
    const page = await api.get(`/api/runs/${detail.id}/events?order=desc&limit=${PAGE_EVENTS}&before=${detail.historyFloor}`);
    absorbHistory(detail, page);
    const feed = document.getElementById("feed");
    const height = feed.scrollHeight, top = feed.scrollTop;
    markDirty("run"); render();
    feed.scrollTop = top + feed.scrollHeight - height; // keep the rows the operator was reading in place
  }),
  "artifact-preview": (button) => {
    const id = button.dataset.artifact;
    const detail = store.detail;
    if (detail.previews[id]) {
      delete detail.previews[id];
      markDirty("run");
      return;
    }
    const artifact = detail.artifacts.find((item) => item.artifact_id === id);
    const plan = artifact && previewPlan(artifact);
    if (!plan) return;
    return act(`preview:${id}`, button, async () => {
      let preview = plan.note ? { note: plan.note } : plan.kind === "image" ? { image: true } : null;
      if (!preview) {
        try {
          const response = await fetch(`/api/artifacts/${id}/content`, { credentials: "same-origin" });
          if (!response.ok) {
            const value = await response.json().catch(() => null);
            throw new ApiError(response.status, value?.error?.message);
          }
          preview = { text: await response.text() };
        } catch (error) {
          preview = { note: `Preview failed: ${error.message}` };
        }
      }
      detail.previews[id] = preview;
      markDirty("run");
    });
  },
});

function buildArtifactPreview(artifact, preview) {
  if (preview.note) return el("p", { class: "muted", text: preview.note });
  if (preview.image) return el("img", { src: `/api/artifacts/${artifact.artifact_id}/content`, alt: artifact.name });
  return boundedPre(preview.text);
}

function buildRawEvents() {
  const input = el("input", { type: "search", placeholder: "Filter by event type", "aria-label": "Filter raw events by type" });
  const block = el("div", { class: "evidence-block raw-events" },
    el("h2", { text: "Raw events" }),
    el("div", { class: "raw-toolbar" }, input, el("span", { class: "raw-count" })),
    el("div", { class: "raw-rows" }),
    el("p", { class: "muted raw-note", hidden: "" }));
  input.addEventListener("input", () => {
    store.detail.eventFilter = input.value;
    renderRawEvents(block);
  });
  return block;
}

function renderRawEvents(block) {
  const detail = store.detail;
  const input = block.querySelector("input");
  if (input.value !== detail.eventFilter) input.value = detail.eventFilter;
  const { rows, matched } = rawEventRows(detail.events, detail.eventFilter);
  block.querySelector(".raw-count").textContent = `${fmt.count(matched)} of ${fmt.count(detail.events.length)} loaded events`;
  const note = block.querySelector(".raw-note");
  note.hidden = rows.length === matched;
  note.textContent = `Showing the most recent ${fmt.count(rows.length)} of ${fmt.count(matched)} matching events.`;
  keyedList(block.querySelector(".raw-rows"), rows, (event) => event.id, () => "", buildRawRow, () => {});
}

function buildRawRow(event) {
  return el("div", { class: "raw-row" },
    el("span", { class: "mono", text: event.type }),
    el("span", { class: "faint", text: `#${event.id}` }),
    el("span", { class: "muted", title: event.created_at, text: fmt.rel(event.created_at) }),
    el("details", null, el("summary", { text: "payload" }), boundedPre(JSON.stringify(event.payload ?? {}, null, 1))));
}
// --- end slice2-evidence ---

// --- evidence-logic ---
const PREVIEW_TEXT_BYTES = 256 * 1024;
const PREVIEW_IMAGE_BYTES = 2 * 1024 * 1024;
const RAW_EVENT_ROWS = 500;

// Fold one descending events page into the detail: the first page seeds the forward cursor, later pages prepend.
function absorbHistory(detail, page) {
  if (page.length) {
    detail.events = [...page].reverse().concat(detail.events);
    detail.historyFloor = page[page.length - 1].id;
    if (!detail.eventsAfter) detail.eventsAfter = page[0].id;
  } else if (detail.historyFloor === null) {
    detail.historyFloor = 0;
  }
  detail.historyDone = page.length < PAGE_EVENTS;
}

function rawEventRows(events, query) {
  const needle = (query || "").trim().toLowerCase();
  const matched = needle ? events.filter((event) => event.type.toLowerCase().includes(needle)) : events;
  return { rows: matched.slice(-RAW_EVENT_ROWS).reverse(), matched: matched.length };
}

function previewPlan(artifact) {
  if (artifact.available === false) return null;
  const type = artifact.media_type || "";
  const image = type.startsWith("image/");
  if (!image && !type.startsWith("text/") && type !== "application/json") return null;
  const cap = image ? PREVIEW_IMAGE_BYTES : PREVIEW_TEXT_BYTES;
  if (artifact.byte_size > cap) return { note: `Too large to preview (${fmt.bytes(artifact.byte_size)}; the cap is ${fmt.bytes(cap)}). Use download.` };
  return { kind: image ? "image" : "text" };
}
// --- end evidence-logic ---

// --- slice2-config ---
// --- end slice2-config ---

// --- slice3-admin ---
// --- end slice3-admin ---

// --- router ---
function parseHash() {
  const segments = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (segments[0] === "runs" && /^\d+$/.test(segments[1] || "")) {
    const section = SECTIONS.includes(segments[2]) ? segments[2] : "activity";
    return { view: "run", runId: Number(segments[1]), section };
  }
  if (segments[0] === "attention") return { view: "attention", runId: null, section: null };
  if (segments[0] === "config") return { view: "config", runId: null, section: null };
  return { view: "fleet", runId: null, section: null };
}

function applyRoute() {
  const route = parseHash();
  const previous = store.route;
  store.route = route;
  if (route.view === "run" && route.runId !== store.detail.id) {
    resetDetail(route.runId);
    const known = store.runs.get(route.runId);
    if (known) store.detail.run = { ...known };
    schedule(true);
  } else if (route.view === "run" && route.section !== previous.section) {
    schedule(true);
  }
  markDirty("fleet", "run", "attention", "config", "nav");
}
// --- end router ---

// --- boot ---
document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const handler = ACTIONS[target.dataset.action];
  if (!handler) return;
  if (target.dataset.action === "open-run" && event.target.closest("a,button:not([data-action=open-run])")) return;
  event.preventDefault();
  handler(target, event);
});

document.addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-form]");
  if (!form) return;
  event.preventDefault();
  FORMS[form.dataset.form]?.(form, event);
});

let filterDebounce = null;
function onFilterInput(event) {
  const field = event.target.closest("[data-filter]");
  if (!field) return;
  clearTimeout(filterDebounce);
  filterDebounce = setTimeout(() => {
    store.filters[field.dataset.filter] = field.value;
    markDirty("fleet");
  }, field.dataset.filter === "text" ? 150 : 0);
}
document.addEventListener("input", onFilterInput);
document.addEventListener("change", onFilterInput);

document.addEventListener("keydown", (event) => {
  const typing = event.target.matches("input,textarea,select") || event.target.isContentEditable;
  const dialogOpen = document.getElementById("confirm").open;
  if (event.key === "Escape" && !dialogOpen) {
    if (store.route.view !== "fleet" && !typing) location.hash = "#/";
    return;
  }
  if (typing) {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      const form = event.target.closest("form[data-form]");
      if (form) {
        event.preventDefault();
        FORMS[form.dataset.form]?.(form);
      }
    }
    return;
  }
  if (event.key === "/") {
    event.preventDefault();
    document.getElementById("filter-text").focus();
    return;
  }
  if (store.route.view === "fleet") {
    if (event.key === "n") {
      event.preventDefault();
      document.getElementById("new-run").open = true;
      document.querySelector("form[data-form=dispatch] textarea[name=objective]").focus();
      return;
    }
    if (["j", "k", "ArrowDown", "ArrowUp", "Enter", "Home", "End"].includes(event.key)) {
      const rows = [...document.querySelectorAll("#run-list .run-row")];
      if (!rows.length) return;
      const current = rows.indexOf(document.activeElement);
      if (event.key === "Enter" && current >= 0) {
        rows[current].click();
        return;
      }
      let next = current;
      if (event.key === "j" || event.key === "ArrowDown") next = Math.min(rows.length - 1, current + 1);
      if (event.key === "k" || event.key === "ArrowUp") next = Math.max(0, current - 1);
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = rows.length - 1;
      if (next !== current && next >= 0) {
        event.preventDefault();
        rows[next].focus();
      }
    }
  }
  if (store.route.view === "run") {
    const index = Number(event.key) - 1;
    if (index >= 0 && index < SECTIONS.length && String(index + 1) === event.key) {
      location.hash = `#/runs/${store.detail.id}/${SECTIONS[index]}`;
      return;
    }
    if (event.key === "[" || event.key === "]") {
      const at = SECTIONS.indexOf(store.route.section);
      const next = SECTIONS[(at + (event.key === "]" ? 1 : SECTIONS.length - 1)) % SECTIONS.length];
      location.hash = `#/runs/${store.detail.id}/${next}`;
      return;
    }
    if (event.key === "t") {
      event.preventDefault();
      ACTIONS["run-direct"]();
      return;
    }
    if (event.key === "End") {
      setFollow(true);
    }
  }
});

document.getElementById("feed").addEventListener("scroll", () => {
  const feed = document.getElementById("feed");
  const atTail = feed.scrollHeight - feed.scrollTop - feed.clientHeight < TAIL_THRESHOLD;
  if (atTail !== store.ui.follow) setFollow(atTail);
});
document.getElementById("follow-live").addEventListener("change", (event) => setFollow(event.target.checked));
document.getElementById("return-live").addEventListener("click", () => setFollow(true));
document.getElementById("show-machine").addEventListener("change", () => markDirty("run"));

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) schedule(true);
});

window.addEventListener("hashchange", applyRoute);
applyRoute();
tick();
// --- end boot ---
