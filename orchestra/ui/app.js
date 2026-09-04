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
const CONFIG_TABS = ["profiles", "groups", "identities", "storage", "audit", "settings", "logs", "diagnostics"];
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
    if (filters.strategy && run.strategy !== filters.strategy) return false;
    if (text) {
      const haystack = `${run.id} ${run.slug ?? ""} ${run.title ?? ""} ${run.objective ?? ""}`.toLowerCase();
      if (!haystack.includes(text)) return false;
    }
    return true;
  });
}

// Server-side part of the fleet filters. Text and strategy stay client-side.
function runsQuery(filters, { before = null, limit = PAGE_RUNS } = {}) {
  const params = [`order=desc`, `limit=${limit}`];
  const statuses = [];
  for (const group of filters.status) statuses.push(...(STATUS_GROUPS[group] || []));
  if (statuses.length) params.push(`status=${statuses.join(",")}`);
  if (filters.group) params.push(`group=${encodeURIComponent(filters.group)}`);
  if (filters.profile) params.push(`profile=${encodeURIComponent(filters.profile)}`);
  if (before) params.push(`before=${before}`);
  return params.join("&");
}

// DSH journal messages carry typed parts: reasoning, text, tool-call, tool-result.
function messageParts(payload) {
  const content = payload?.message?.content ?? payload?.content;
  const parts = { reasoning: [], text: [] };
  if (typeof content === "string") parts.text.push(content);
  else if (Array.isArray(content)) {
    for (const part of content) {
      if (!part) continue;
      if (typeof part === "string") parts.text.push(part);
      else if (part.type === "reasoning" && part.text) parts.reasoning.push(part.text);
      else if (part.type === "text" && part.text) parts.text.push(part.text);
    }
  }
  return parts;
}

// A journal user message is the operator's prompt only when DSH marks its source as
// "user"; plugin snapshots (runtime context) carry named sections instead.
function promptSource(payload) {
  const source = payload?.source;
  if (!source || source.kind === "user") return { kind: "user" };
  return { kind: "context", plugin: source.plugin || source.kind, sections: Array.isArray(source.sections) ? source.sections : [] };
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

// Retained UI state: fleet filters, the machine-events toggle, and the last run
// section, as one JSON string. Decode tolerates any input and returns defaults.
const UI_STATE_KEY = "orchestra-next.ui";
function uiStateEncode(state) {
  const f = state.filters;
  const filters = { status: [...f.status], group: f.group, profile: f.profile, text: f.text };
  if ("strategy" in f) filters.strategy = f.strategy;
  return JSON.stringify({ filters, machine: Boolean(state.ui.machine), section: state.ui.section, configTab: state.ui.configTab });
}
function uiStateDecode(raw) {
  const out = { filters: { status: new Set(), group: "", profile: "", text: "" }, machine: false, section: "activity", configTab: "profiles" };
  try {
    const value = JSON.parse(raw);
    const f = value?.filters ?? {};
    if (Array.isArray(f.status)) out.filters.status = new Set(f.status.filter((key) => key in STATUS_GROUPS));
    for (const key of ["group", "profile", "text", "strategy"]) if (typeof f[key] === "string") out.filters[key] = f[key];
    out.machine = value?.machine === true;
    if (SECTIONS.includes(value?.section)) out.section = value.section;
    if (CONFIG_TABS.includes(value?.configTab)) out.configTab = value.configTab;
  } catch {
    // malformed or absent: defaults
  }
  return out;
}
// One delegation event → the strings the activity row and evidence table show.
function delegationSummary(payload) {
  const ok = payload?.status === "SUCCESS";
  const delta = payload?.delta || payload?.usage || {};
  const conversation = payload?.usage?.total;
  const parts = [`turn ${payload?.num_turns ?? "?"}`, `${fmt.tokens(delta.total || 0)} tokens this turn`];
  if (conversation != null) parts.push(`${fmt.tokens(conversation)} conversation`);
  if (payload?.duration_seconds != null) parts.push(`${Number(payload.duration_seconds).toFixed(1)}s`);
  return { ok, title: `🛰 delegation · ${payload?.model || "?"} · ${payload?.status || "?"}`, stats: parts.join(" · ") };
}
// --- end logic ---

// --- attention-logic ---
// The last few meaningful journal rows at or before an attention item opened: assistant
// text (or reasoning when there is no text), tool calls, and verification failures.
// Pure (no DOM, no store); tests/test_ui_attention.py slices this block.
function attentionLeadUp(events, createdAt, limit = 4) {
  const cutoff = createdAt ? Date.parse(createdAt) : Infinity;
  const rows = [];
  for (const event of events || []) {
    if (Date.parse(event.created_at) > cutoff) continue;
    const payload = event.payload || {};
    let kind, text;
    if (event.type === "dsh.assistant/message") {
      const parts = messageParts(payload);
      const source = parts.text.length ? parts.text : parts.reasoning;
      if (!source.length) continue;
      kind = parts.text.length ? "text" : "reasoning";
      text = `${kind === "text" ? "💬" : "💭"} ${fmt.excerpt(source.join("\n").trim(), 200)}`;
    } else if (event.type === "acp.tool_call") {
      kind = "tool";
      text = `⚙ ${payload.title || payload.name || "tool"}`;
    } else if (event.type === "verification.failed") {
      kind = "verification";
      text = `❌ verification failed${payload.output ? ` — ${fmt.excerpt(String(payload.output).trim(), 200)}` : ""}`;
    } else continue;
    rows.push({ at: event.created_at, id: event.id ?? 0, kind, text });
  }
  rows.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : a.id - b.id));
  return rows.slice(-limit).map(({ at, kind, text }) => ({ at, kind, text }));
}
// --- end attention-logic ---

// --- qr ---
// QR encoder: byte mode, error correction level M, versions 1 to 10. Pure
// functions only (no DOM): tests/test_ui_qr.py slices this block, runs it
// under node, and pins qrMatrix to matrices that macOS Vision decoded byte
// for byte (ported from the V2 dashboard encoder).
const QR_TOTAL = [26, 44, 70, 100, 134, 172, 196, 242, 292, 346];
const QR_BLOCKS = [
  [10, [[1, 16]]], [16, [[1, 28]]], [26, [[1, 44]]], [18, [[2, 32]]], [24, [[2, 43]]],
  [16, [[4, 27]]], [18, [[4, 31]]], [22, [[2, 38], [2, 39]]], [22, [[3, 36], [2, 37]]], [26, [[4, 43], [1, 44]]],
];
const QR_ALIGN = [[], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50]];
const QR_EXP = new Array(512);
const QR_LOG = new Array(256);
for (let i = 0, x = 1; i < 255; i += 1) {
  QR_EXP[i] = x;
  QR_LOG[x] = i;
  x <<= 1;
  if (x & 256) x ^= 0x11d;
}
for (let i = 255; i < 512; i += 1) QR_EXP[i] = QR_EXP[i - 255];

function qrMul(a, b) {
  return a && b ? QR_EXP[QR_LOG[a] + QR_LOG[b]] : 0;
}

function qrGenerator(degree) {
  let poly = [1];
  for (let i = 0; i < degree; i += 1) {
    const next = new Array(poly.length + 1).fill(0);
    for (let j = 0; j < poly.length; j += 1) {
      next[j] ^= qrMul(poly[j], 1);
      next[j + 1] ^= qrMul(poly[j], QR_EXP[i]);
    }
    poly = next;
  }
  return poly;
}

function qrRemainder(data, degree) {
  const gen = qrGenerator(degree);
  const out = new Array(degree).fill(0);
  for (const byte of data) {
    const factor = byte ^ out[0];
    out.shift();
    out.push(0);
    for (let i = 0; i < degree; i += 1) out[i] ^= qrMul(gen[i + 1], factor);
  }
  return out;
}

function qrBCH(value, poly) {
  const degree = 31 - Math.clz32(poly);
  let rest = value << degree;
  for (let i = 31 - Math.clz32(rest); i >= degree; i -= 1) {
    if (rest >>> i & 1) rest ^= poly << (i - degree);
  }
  return (value << degree) | rest;
}

function qrPenalty(grid, size) {
  let score = 0;
  const line = (cells) => {
    let run = 1;
    for (let i = 1; i < size; i += 1) {
      if (cells[i] === cells[i - 1]) {
        run += 1;
      } else {
        if (run >= 5) score += 3 + run - 5;
        run = 1;
      }
    }
    if (run >= 5) score += 3 + run - 5;
  };
  const FIND = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0];
  const finder = (cells) => {
    for (let i = 0; i + 11 <= size; i += 1) {
      const w = cells.slice(i, i + 11);
      if (FIND.every((v, k) => w[k] === v) || FIND.every((v, k) => w[10 - k] === v)) score += 40;
    }
  };
  for (let i = 0; i < size; i += 1) {
    const row = grid[i];
    const column = grid.map((r) => r[i]);
    line(row);
    line(column);
    finder(row);
    finder(column);
  }
  for (let i = 0; i < size - 1; i += 1) {
    for (let j = 0; j < size - 1; j += 1) {
      if (grid[i][j] === grid[i][j + 1] && grid[i][j] === grid[i + 1][j] && grid[i][j] === grid[i + 1][j + 1]) score += 3;
    }
  }
  score += Math.floor(Math.abs(grid.flat().reduce((n, v) => n + v, 0) * 100 / (size * size) - 50) / 5) * 10;
  return score;
}

function qrMatrix(text) {
  const bytes = Array.from(new TextEncoder().encode(text));
  const version = QR_TOTAL.findIndex((total, i) => QR_BLOCKS[i][1].reduce((n, [count, size]) => n + count * size, 0) * 8 >= 4 + (i < 9 ? 8 : 16) + bytes.length * 8) + 1;
  if (!version) throw new Error("Too much data for a compact QR code.");
  const [ecPerBlock, groups] = QR_BLOCKS[version - 1];
  const dataCount = groups.reduce((n, [count, size]) => n + count * size, 0);

  // Data bits: mode 4 (byte), length, payload, terminator, byte padding.
  const bits = [];
  const push = (value, width) => {
    for (let i = width - 1; i >= 0; i -= 1) bits.push(value >>> i & 1);
  };
  push(4, 4);
  push(bytes.length, version < 10 ? 8 : 16);
  bytes.forEach((b) => push(b, 8));
  for (let i = 0; i < 4 && bits.length < dataCount * 8; i += 1) bits.push(0);
  while (bits.length % 8) bits.push(0);
  const words = [];
  for (let i = 0; i < bits.length; i += 8) words.push(parseInt(bits.slice(i, i + 8).join(""), 2));
  for (let pad = 0; words.length < dataCount; pad ^= 1) words.push(pad ? 0x11 : 0xec);

  // Reed-Solomon blocks, interleaved data then EC.
  const blocks = [];
  const ecBlocks = [];
  let at = 0;
  for (const [count, size] of groups) {
    for (let i = 0; i < count; i += 1) {
      const block = words.slice(at, at + size);
      at += size;
      blocks.push(block);
      ecBlocks.push(qrRemainder(block, ecPerBlock));
    }
  }
  const stream = [];
  const widest = Math.max(...blocks.map((b) => b.length));
  for (let i = 0; i < widest; i += 1) for (const block of blocks) if (i < block.length) stream.push(block[i]);
  for (let i = 0; i < ecPerBlock; i += 1) for (const block of ecBlocks) stream.push(block[i]);

  // Function patterns: finders, alignment, timing, dark module.
  const size = version * 4 + 17;
  const grid = Array.from({ length: size }, () => new Array(size).fill(null));
  const set = (r, c, v) => {
    if (r >= 0 && r < size && c >= 0 && c < size) grid[r][c] = v;
  };
  const finder = (r, c) => {
    for (let i = -1; i < 8; i += 1) {
      for (let j = -1; j < 8; j += 1) {
        const inside = i >= 0 && i < 7 && j >= 0 && j < 7;
        set(r + i, c + j, inside && (i === 0 || i === 6 || j === 0 || j === 6 || (i >= 2 && i <= 4 && j >= 2 && j <= 4)) ? 1 : 0);
      }
    }
  };
  finder(0, 0);
  finder(0, size - 7);
  finder(size - 7, 0);
  for (const centre of QR_ALIGN[version - 1]) {
    for (const other of QR_ALIGN[version - 1]) {
      if ((centre === 6 && other === 6) || (centre === 6 && other === size - 7) || (centre === size - 7 && other === 6)) continue;
      for (let i = -2; i <= 2; i += 1) for (let j = -2; j <= 2; j += 1) set(centre + i, other + j, Math.max(Math.abs(i), Math.abs(j)) !== 1 ? 1 : 0);
    }
  }
  for (let i = 8; i < size - 8; i += 1) {
    set(6, i, i % 2 ? 0 : 1);
    set(i, 6, i % 2 ? 0 : 1);
  }
  set(size - 8, 8, 1);

  // Reserved cells: everything placed so far, format bits, version bits.
  const reserved = Array.from({ length: size }, () => new Array(size).fill(false));
  for (let i = 0; i < size; i += 1) for (let j = 0; j < size; j += 1) if (grid[i][j] !== null) reserved[i][j] = true;
  const formatCells = [];
  for (let i = 0; i < 15; i += 1) {
    const a = i < 6 ? [i, 8] : i === 6 ? [7, 8] : i === 7 ? [8, 8] : i === 8 ? [8, 7] : [8, 14 - i];
    const b = i < 8 ? [8, size - 1 - i] : [size - 15 + i, 8];
    formatCells.push([a, b]);
    reserved[a[0]][a[1]] = true;
    reserved[b[0]][b[1]] = true;
  }
  if (version >= 7) {
    for (let i = 0; i < 18; i += 1) {
      const r = Math.floor(i / 3);
      const c = i % 3;
      reserved[size - 11 + c][r] = true;
      reserved[r][size - 11 + c] = true;
    }
  }

  // Zigzag placement of the codeword bits, skipping the timing column.
  const payload = [];
  stream.forEach((word) => {
    for (let i = 7; i >= 0; i -= 1) payload.push(word >>> i & 1);
  });
  let bit = 0;
  for (let right = size - 1; right > 0; right -= 2) {
    if (right === 6) right = 5;
    for (let step = 0; step < size; step += 1) {
      const row = (Math.floor((size - 1 - right) / 2) % 2 === 0) ? size - 1 - step : step;
      for (const column of [right, right - 1]) if (!reserved[row][column]) grid[row][column] = bit < payload.length ? payload[bit++] : 0;
    }
  }

  // Try every mask; keep the lowest penalty.
  const maskAt = (m, i, j) => [
    (i + j) % 2, i % 2, j % 3, (i + j) % 3,
    (Math.floor(i / 2) + Math.floor(j / 3)) % 2,
    (i * j) % 2 + (i * j) % 3,
    ((i * j) % 2 + (i * j) % 3) % 2,
    ((i + j) % 2 + (i * j) % 3) % 2,
  ][m] === 0;
  let best = null;
  for (let mask = 0; mask < 8; mask += 1) {
    const view = grid.map((row) => row.slice());
    for (let i = 0; i < size; i += 1) for (let j = 0; j < size; j += 1) if (!reserved[i][j] && maskAt(mask, i, j)) view[i][j] ^= 1;
    const format = qrBCH(mask, 0x537) ^ 0x5412; // level M is 0, so the value is the mask
    formatCells.forEach(([a, b], i) => {
      const value = format >>> i & 1;
      view[a[0]][a[1]] = value;
      view[b[0]][b[1]] = value;
    });
    if (version >= 7) {
      const info = qrBCH(version, 0x1f25);
      for (let i = 0; i < 18; i += 1) {
        const value = info >>> i & 1;
        const r = Math.floor(i / 3);
        const c = i % 3;
        view[size - 11 + c][r] = value;
        view[r][size - 11 + c] = value;
      }
    }
    view[size - 8][8] = 1;
    const score = qrPenalty(view, size);
    if (best === null || score < best.score) best = { score, view };
  }
  return best.view;
}
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
  patch: (path, body) => req("PATCH", path, body),
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
  runsWindow: { signature: "", floor: null, exhausted: false }, // loaded page of /api/runs: its query, oldest id, end reached
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
  ui: { follow: true, machine: false, section: "activity", configTab: "profiles", inflight: new Set(), dispatchRequestId: crypto.randomUUID() },
};

// Retained state lives in one browser storage entry; storage may be unavailable.
let uiSaveTimer = null;
function saveUiState() {
  clearTimeout(uiSaveTimer);
  uiSaveTimer = setTimeout(() => { try { localStorage.setItem(UI_STATE_KEY, uiStateEncode(store)); } catch { /* storage unavailable */ } }, 200);
}
function loadUiState() {
  let raw = null;
  try { raw = localStorage.getItem(UI_STATE_KEY); } catch { /* storage unavailable */ }
  return uiStateDecode(raw);
}
function clearFilters() {
  for (const key of Object.keys(store.filters)) store.filters[key] = key === "status" ? new Set() : "";
  for (const field of document.querySelectorAll("[data-filter]")) {
    field.value = "";
    delete field._pending;
  }
  saveUiState();
  refetchFleet();
}

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
  store.runsWindow = { signature: "", floor: null, exhausted: false };
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
  const signature = runsQuery(store.filters);
  if (signature !== store.runsWindow.signature) {
    store.runs.clear();
    store.runsWindow = { signature, floor: null, exhausted: false };
  }
  const [runs, attention, profiles, groups] = await Promise.all([
    api.get(`/api/runs?${signature}`).catch((error) => {
      // A restored group/profile id can point at a record that no longer exists; drop it rather than 400 every tick.
      if (!(error instanceof ApiError) || error.status !== 400 || !(store.filters.group || store.filters.profile)) throw error;
      store.filters.group = store.filters.profile = "";
      for (const field of document.querySelectorAll("select[data-filter]")) { field.value = ""; delete field._pending; }
      saveUiState();
      toast("A saved group or profile filter no longer exists; cleared");
      return api.get(`/api/runs?${runsQuery(store.filters)}`);
    }),
    api.get("/api/attention?status=open"),
    api.get("/api/profiles"),
    api.get("/api/groups"),
  ]);
  for (const run of runs) store.runs.set(run.id, run);
  if (runs.length) {
    const floor = runs[runs.length - 1].id;
    if (store.runsWindow.floor === null || floor < store.runsWindow.floor) store.runsWindow.floor = floor;
    if (runs.length < PAGE_RUNS) store.runsWindow.exhausted = true;
  } else {
    store.runsWindow.exhausted = true;
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
    detail.delegations = await api.get(`/api/runs/${detail.id}/delegations`).catch(() => []);
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

// Server-side filters changed: the loaded window no longer matches, refetch page one.
function refetchFleet() {
  store.snapshotsStale = true;
  markDirty("fleet");
  schedule(true);
}

const ACTIONS = {
  "load-more": (button) => act("load-more", button, async () => {
    const older = await api.get(`/api/runs?${runsQuery(store.filters, { before: store.runsWindow.floor })}`);
    for (const run of older) store.runs.set(run.id, run);
    if (older.length) store.runsWindow.floor = older[older.length - 1].id;
    if (older.length < PAGE_RUNS) store.runsWindow.exhausted = true;
    markDirty("fleet");
  }),
  "filter-status": (button) => {
    const key = button.dataset.status;
    if (store.filters.status.has(key)) store.filters.status.delete(key);
    else store.filters.status.add(key);
    saveUiState();
    refetchFleet();
  },
  "filter-strategy": (button) => {
    const key = button.dataset.strategy;
    store.filters.strategy = store.filters.strategy === key ? "" : key;
    saveUiState();
    markDirty("fleet");
  },
  "filter-clear": clearFilters,
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
    if (values.allow_antigravity.checked) body.allow_antigravity = true;
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
  // _pending: a restored value whose option was not loaded yet; applied once it exists.
  select.value = select._pending ?? previous;
  if (select.selectedIndex === -1) select.selectedIndex = 0;
  else delete select._pending;
}

function renderFleet() {
  for (const select of document.querySelectorAll("select[data-options=profiles]")) fillOptions(select, store.profiles, select.dataset.filter ? "id" : "slug", "slug");
  for (const select of document.querySelectorAll("select[data-options=groups]")) fillOptions(select, store.groups, select.dataset.filter ? "group_id" : "slug", "slug");

  const runs = [...store.runs.values()].sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
  // Counts cover the loaded window; with a server-side filter that window is a subset, hence "in view".
  const counts = { queued: 0, running: 0, waiting: 0, failed: 0, goal: 0, ralph: 0 };
  for (const run of runs) {
    for (const [key, statuses] of Object.entries(STATUS_GROUPS)) {
      if (key !== "done" && statuses.includes(run.status)) counts[key] = (counts[key] || 0) + 1;
    }
    if (run.strategy in counts) counts[run.strategy] += 1;
  }
  const countsBox = document.getElementById("counts");
  const entries = [
    ["running", "running", "", "filter-status"],
    ["queued", "queued", "", "filter-status"],
    ["waiting", "waiting", "warn", "filter-status"],
    ["failed", "failed", "bad", "filter-status"],
    ["goal", "goal", "", "filter-strategy"],
    ["ralph", "ralph", "", "filter-strategy"],
  ];
  const pressed = (entry) => entry[3] === "filter-status" ? store.filters.status.has(entry[0]) : store.filters.strategy === entry[0];
  keyedList(countsBox, entries, (entry) => entry[0],
    (entry) => `${counts[entry[0]] || 0}:${pressed(entry)}`,
    (entry) => {
      const dataset = { action: entry[3], [entry[3] === "filter-status" ? "status" : "strategy"]: entry[0] };
      return el("button", { type: "button", class: entry[2], dataset, "aria-pressed": String(pressed(entry)) },
        el("strong", { text: String(counts[entry[0]] || 0) }), entry[1]);
    },
    (node, entry) => {
      node.querySelector("strong").textContent = String(counts[entry[0]] || 0);
      node.setAttribute("aria-pressed", String(pressed(entry)));
    });
  document.getElementById("counts-scope").hidden = runsQuery(store.filters) === runsQuery({ status: new Set() });

  const visible = filterRuns(runs, store.filters);
  const state = document.getElementById("fleet-state");
  const filtering = runsQuery(store.filters) !== runsQuery({ status: new Set() }) || store.filters.text.trim() || store.filters.strategy;
  if (store.boardRevision < 0 || (!runs.length && store.snapshotsStale)) state.textContent = "Loading runs…";
  else if (!runs.length && !filtering) state.textContent = "No runs yet. Dispatch the first one with New run.";
  else if (!visible.length) state.replaceChildren("No runs match the current filters. ", el("button", { type: "button", dataset: { action: "filter-clear" }, text: "Clear filters" }));
  state.hidden = Boolean(visible.length);

  const list = document.getElementById("run-list");
  keyedList(list, visible, (run) => run.id, runSignature, buildRunRow, updateRunRow);
  document.querySelector("[data-action=load-more]").hidden = store.runsWindow.exhausted || store.runsWindow.floor === null;
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
  queued: ["direct", "reroute", "stop"],
  starting: ["direct", "pause", "reroute", "stop"],
  running: ["direct", "pause", "reroute", "stop"],
  waiting: ["direct", "resume", "reroute", "stop"],
  completed: ["retry", "continue", "merge"],
  failed: ["retry", "continue", "merge"],
  timed_out: ["retry", "continue", "merge"],
  stopped: ["retry", "continue", "merge"],
  skipped: ["retry", "continue", "merge"],
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
  if (run.request_snapshot?.allow_antigravity) chips.push(el("span", { class: "chip warn", title: "Worktree contents may be sent to Google", text: "antigravity allowed" }));

  const actions = [];
  const allowed = RUN_ACTION_MATRIX[run.status] || [];
  if (allowed.includes("resume")) actions.push(el("button", { class: "btn btn-primary", dataset: { action: "run-resume" }, text: "Resume" }));
  if (allowed.includes("direct")) actions.push(el("button", { class: "btn", dataset: { action: "run-direct" }, title: "Tell or interrupt the run ( t )", text: "Direct…" }));
  if (allowed.includes("pause")) actions.push(el("button", { class: "btn", dataset: { action: "run-pause" }, title: "Park at the next safe boundary; Resume continues the same session", text: "Pause" }));
  if (allowed.includes("reroute")) actions.push(el("button", { class: "btn", dataset: { action: "run-reroute" }, title: "Continue the same goal on another provider/model", text: "Reroute…" }));
  if (allowed.includes("stop")) actions.push(el("button", { class: "btn btn-danger", dataset: { action: "run-stop" }, text: "Stop" }));
  if (allowed.includes("stop") && run.child_count > 0) actions.push(el("button", { class: "btn btn-danger", dataset: { action: "run-stop-tree" }, title: "Queue a stop for this run and every active descendant", text: "Stop tree" }));
  if (run.request_snapshot?.allow_antigravity && run.workdir) actions.push(el("button", { class: "btn", dataset: { action: "run-delegate" }, title: "Read-only review of the worktree by Antigravity", text: "Delegate review…" }));
  if (allowed.includes("retry")) actions.push(el("button", { class: "btn btn-primary", dataset: { action: "run-retry" }, title: "Start a new run from the same request", text: "Retry" }));
  if (allowed.includes("continue")) actions.push(el("button", { class: "btn", dataset: { action: "run-continue" }, title: "Start a new run from the same request plus a direction", text: "Continue…" }));
  if (allowed.includes("merge") && run.branch) actions.push(el("button", { class: "btn", dataset: { action: "run-merge" }, title: "Merge the run branch into the owner checkout", text: "Merge…" }));
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
  if (type === "delegation.antigravity") return "delegation";
  if (type === "dsh.user/message") return promptSource(entry.item.payload).kind === "user" ? "prompt" : "context";
  if (type === "dsh.assistant/message") {
    const parts = messageParts(entry.item.payload);
    return parts.reasoning.length || parts.text.length ? "assistant" : "machine";
  }
  // Stream fragments (dsh.assistant/chunk, acp.agent_thought_chunk) repeat what the
  // finished assistant message carries, so they stay behind the machine toggle.
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
  if (kind === "assistant") {
    const parts = messageParts(entry.item.payload);
    const body = el("div", { class: "item-body" });
    if (parts.reasoning.length) body.append(el("div", { class: "thinking" }, el("span", { class: "thinking-label", text: "💭 " }), boundedPre(parts.reasoning.join("\n\n"))));
    if (parts.text.length) body.append(boundedPre(parts.text.join("\n\n")));
    return el("div", { class: "feed-item assistant" }, head(parts.text.length ? "💬 assistant" : "💭 assistant"), body);
  }
  if (kind === "delegation") {
    const payload = entry.item.payload || {};
    const summary = delegationSummary(payload);
    const response = payload.response || "";
    const body = el("div", { class: "item-body" },
      el("div", { class: "muted", text: fmt.excerpt(payload.objective || "", 160) }),
      el("div", { class: "muted mono delegation-stats", text: summary.stats }));
    if (summary.ok) {
      const details = el("details", null, el("summary", { text: `response (${fmt.count(response.length)} chars)${payload.truncated ? ", truncated" : ""}` }), boundedPre(response));
      if (response.length < CLIP_CHARS) details.open = true;
      body.append(details);
    } else {
      body.append(el("div", { class: "form-error", text: payload.error || `Antigravity reported ${payload.status || "an error"}` }));
      if (payload.stderr) body.append(el("details", null, el("summary", { class: "muted", text: "stderr" }), boundedPre(payload.stderr)));
      if (response) body.append(el("details", null, el("summary", { class: "muted", text: "partial response" }), boundedPre(response)));
    }
    return el("div", { class: `feed-item delegation${summary.ok ? "" : " bad"}` }, head(summary.title), body);
  }
  if (kind === "prompt") {
    return el("div", { class: "feed-item prompt" }, head("🧑 prompt"), el("div", { class: "item-body" }, boundedPre(extractText(entry.item.payload))));
  }
  if (kind === "context") {
    const source = promptSource(entry.item.payload);
    const names = source.sections.map((section) => section.name).filter(Boolean);
    const body = source.sections.length
      ? source.sections.map((section) => el("div", { class: "context-section" }, el("div", { class: "muted mono", text: section.name || "" }), boundedPre(section.text || "")))
      : [boundedPre(extractText(entry.item.payload))];
    return el("div", { class: "feed-item context" }, head(`📎 context snapshot · ${source.plugin}`),
      el("details", null, el("summary", { text: names.length ? names.join(" · ") : "show" }), ...body));
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
  if (detail.mergeResult) blocks.push(mergeResultBlock(detail.mergeResult));
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
        el("td", { class: "mono" }, `${group.provider ?? "?"}/${group.model ?? "?"}`, group.provider === "antigravity" ? el("span", { class: "chip warn", text: "separate subscription" }) : null),
        el("td", { text: String(group.cache_epoch) }),
        el("td", { class: "mono faint", text: group.descendant ? group.descendant.slice(0, 10) : "" }),
        cell(group.input), cell(group.output), cell(group.cache_read), cell(group.cache_write), cell(group.total))),
      el("tr", null, el("td", { text: "Total" }), el("td"), el("td"),
        cell(totals.input), cell(totals.output), cell(totals.cache_read), cell(totals.cache_write), cell(totals.total))));
  const note = groups.some((group) => group.provider === "antigravity")
    ? el("p", { class: "usage-note", text: "Antigravity tokens are a separate subscription; they are not part of the run totals." }) : null;
  panel.replaceChildren(...[note, table].filter(Boolean));
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
  fact("antigravity", run.request_snapshot?.allow_antigravity ? "allowed" : "not allowed");
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
    el("div", { class: "evidence-block" }, el("h2", { text: "Delegations" }), delegationsTable(detail.delegations)),
    el("div", { class: "evidence-block" },
      el("details", null, el("summary", { text: "Request snapshot" }), boundedPre(JSON.stringify(run.request_snapshot ?? {}, null, 2))),
      el("details", null, el("summary", { text: "Profile snapshot" }), boundedPre(JSON.stringify(run.profile_snapshot ?? {}, null, 2)))),
  );
  renderRawEvents(raw);
}

function delegationsTable(rows) {
  if (!rows) return el("p", { class: "muted", text: "Loading delegations…" });
  if (!rows.length) return el("p", { class: "muted", text: "No delegations." });
  return el("table", { class: "plain" },
    el("thead", null, el("tr", null, ...["When", "Model", "Status", "Turn", "Tokens", "Conversation"].map((h) => el("th", { text: h })))),
    el("tbody", null, ...rows.map((row) => el("tr", { class: row.status === "SUCCESS" ? null : "faint" },
      el("td", { title: row.created_at, text: fmt.rel(row.created_at) }),
      el("td", { class: "mono", text: row.model || "" }),
      el("td", { text: row.status || "" }),
      el("td", { text: String(row.num_turns ?? "") }),
      el("td", { class: "mono", title: fmt.count(row.delta?.total || 0), text: fmt.tokens(row.delta?.total || 0) }),
      el("td", { class: "mono faint", title: row.conversation_id || "", text: (row.conversation_id || "").slice(0, 8) })))));
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

// Lead-up rows per open attention item, fetched once (the lead-up is historical, so
// revision changes never refetch). renderAttention drops entries that left the inbox.
store.attentionContext = {}; // attention_id -> { rows: [{at, kind, text}] | null, error: bool }

function fillLeadUp(node, entry) {
  const title = el("div", { class: "att-meta", text: "Before this question" });
  if (!entry.rows) return node.replaceChildren(title, el("div", { text: entry.error ? "Could not load activity" : "Loading…" }));
  if (!entry.rows.length) return node.replaceChildren(title, el("div", { text: "No assistant activity recorded before this item" }));
  node.replaceChildren(title, el("ul", null, entry.rows.map((row) =>
    el("li", null, el("span", { class: "att-meta", title: row.at, text: fmt.rel(row.at) }), " ", row.text))));
}

function leadUpNode(item) {
  const node = el("div", { class: "att-leadup" });
  let entry = store.attentionContext[item.attention_id];
  if (!entry) {
    entry = store.attentionContext[item.attention_id] = { rows: null, error: false };
    api.get(`/api/runs/${item.run_id}/events?order=desc&limit=80`)
      .then((events) => { entry.rows = attentionLeadUp(events, item.created_at); })
      .catch(() => { entry.error = true; })
      .then(() => {
        const live = document.querySelector(`#attention-list .att-item[data-attention="${item.attention_id}"] .att-leadup`);
        if (live) fillLeadUp(live, entry);
      });
  }
  fillLeadUp(node, entry);
  return node;
}

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
    answered ? null : leadUpNode(item),
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
  const openIds = new Set(open.map((item) => item.attention_id));
  for (const id of Object.keys(store.attentionContext)) if (!openIds.has(id)) delete store.attentionContext[id];
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
  renderConfigTabs();
  CONFIG_RENDERERS[store.ui.configTab]();
}

function renderConfigIdentities() {
  const box = document.getElementById("config-identities-list");
  const data = store.identities;
  if (!data || data.revision !== store.boardRevision) loadIdentities();
  if (!data) {
    configFrame(box, el("p", { class: "view-state", text: "Loading identities…" }));
    return;
  }
  const seen = (iso) => el("td", { title: iso || "", text: fmt.rel(iso) });
  const revokeButton = (action, id, name) => el("button", { class: "btn btn-danger", dataset: { action, id, name }, text: "Revoke" });
  const devices = el("table", { class: "plain" }, tableHead(["Name", "Created", "Last seen", "State", ""]),
    el("tbody", null, ...data.devices.map((device) => el("tr", { class: device.revoked_at ? "faint" : null },
      el("td", { text: device.name }), seen(device.created_at), seen(device.last_seen_at),
      el("td", { text: device.revoked_at ? "revoked" : device.device_id === store.me?.id ? "this browser" : "active" }),
      el("td", { class: "row-actions" }, device.revoked_at ? null : revokeButton("device-revoke", device.device_id, device.name))))));
  const tokens = el("table", { class: "plain" }, tableHead(["Name", "Authorities", "Created", "Last seen", "State", ""]),
    el("tbody", null, ...data.tokens.map((token) => el("tr", { class: token.revoked_at ? "faint" : null },
      el("td", { text: token.name }), el("td", { class: "mono", text: token.authorities.join(", ") }),
      seen(token.created_at), seen(token.last_seen_at),
      el("td", { text: token.revoked_at ? "revoked" : "active" }),
      el("td", { class: "row-actions" }, token.revoked_at ? null : revokeButton("token-revoke", token.token_id, token.name))))));
  configFrame(box,
    el("h3", { class: "config-sub", text: "Devices" }), devices,
    el("h3", { class: "config-sub", text: "Service tokens" }),
    el("p", { class: "config-toolbar" }, el("button", { class: "btn btn-primary", dataset: { action: "token-new" }, text: "New service token…" })),
    data.tokens.length ? tokens : el("p", { class: "view-state", text: "No service tokens." }));
}

function renderConfigProfiles() {
  const profiles = document.getElementById("config-profiles");
  const profileTable = el("table", { class: "plain" },
    tableHead(["Name", "Slug", "Route", "Tier", "Concurrency", "State", "Rev", ""]),
    el("tbody", null, ...store.profiles.map((profile) => el("tr", { class: profile.archived ? "faint" : null },
      el("td", { text: profile.name }),
      el("td", { class: "mono", text: profile.slug }),
      el("td", { class: "mono", text: `${profile.provider}/${profile.model}${profile.effort ? "·" + profile.effort : ""}` }),
      el("td", { text: String(profile.tier) }),
      el("td", { text: profile.max_concurrency == null ? "" : String(profile.max_concurrency) }),
      el("td", { text: profile.archived ? "archived" : profile.enabled === false ? "disabled" : "enabled" }),
      el("td", { text: String(profile.revision) }),
      el("td", { class: "row-actions" },
        el("button", { class: "btn", dataset: { action: "profile-edit", slug: profile.slug }, text: "Edit" }),
        el("button", { class: "btn", dataset: { action: "profile-toggle", slug: profile.slug, field: "enabled" }, text: profile.enabled === false ? "Enable" : "Disable" }),
        el("button", { class: "btn", dataset: { action: "profile-toggle", slug: profile.slug, field: "archived" }, text: profile.archived ? "Unarchive" : "Archive" }))))));
  configFrame(profiles,
    el("p", { class: "config-toolbar" }, el("button", { class: "btn btn-primary", dataset: { action: "profile-new" }, text: "New profile…" })),
    store.profiles.length ? profileTable : el("p", { class: "view-state", text: "No profiles yet. Create one with New profile." }));
}

function renderConfigGroups() {
  const groupsBox = document.getElementById("config-groups");
  const groupTable = el("table", { class: "plain" },
    tableHead(["Name", "Slug", "Default cwd", "Concurrency", "State", "Rev", ""]),
    el("tbody", null, ...store.groups.map((group) => el("tr", { class: group.archived ? "faint" : null },
      el("td", { text: group.name }),
      el("td", { class: "mono", text: group.slug }),
      el("td", { class: "mono", text: group.default_cwd || "" }),
      el("td", { text: group.max_concurrency == null ? "" : String(group.max_concurrency) }),
      el("td", { text: group.archived ? "archived" : "active" }),
      el("td", { text: String(group.revision) }),
      el("td", { class: "row-actions" },
        el("button", { class: "btn", dataset: { action: "group-rename", slug: group.slug }, text: "Rename" }),
        el("button", { class: "btn", dataset: { action: "group-cwd", slug: group.slug }, text: "Set directory" }),
        el("button", { class: "btn", dataset: { action: "group-archive", slug: group.slug }, text: group.archived ? "Unarchive" : "Archive" }))))));
  configFrame(groupsBox,
    el("p", { class: "config-toolbar" }, el("button", { class: "btn btn-primary", dataset: { action: "group-new" }, text: "New group…" })),
    store.groups.length ? groupTable : el("p", { class: "view-state", text: "No groups yet. Create one with New group." }));
}

function renderConfigDiagnostics() {
  const diagnostics = document.getElementById("config-diagnostics-facts");
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
const MODELS_TTL = 5 * 60 * 1000;
const lineageIds = new Map(); // `${runId}:${kind}` → request_id, kept until the POST succeeds

async function loadModels(refresh) {
  const cached = store.models;
  if (!refresh && cached && Date.now() - Date.parse(cached.checked_at) < MODELS_TTL) return cached;
  store.models = await api.get(refresh ? "/api/models?refresh=1" : "/api/models");
  return store.models;
}

function fillEfforts(form, preferred) {
  const select = form.elements.effort;
  select.replaceChildren(el("option", { value: "", text: "default" }),
    ...routeEfforts(store.models?.models, form.elements.route.value).map((effort) => el("option", { value: effort, text: effort })));
  select.value = preferred || "";
  if (select.selectedIndex === -1) select.selectedIndex = 0;
}

async function fillRoutes(form, refresh) {
  const run = store.detail.run;
  const retry = form.querySelector("[data-action=reroute-catalog]");
  formError(form, "");
  retry.hidden = true;
  try {
    const { models } = await loadModels(refresh);
    const select = form.elements.route;
    select.replaceChildren(...models.map((item) => el("option", { value: routeKey(item.provider, item.model), text: `${item.provider}/${item.model}` })));
    select.value = routeKey(run.route_provider, run.route_model);
    if (select.selectedIndex === -1) select.selectedIndex = 0;
    fillEfforts(form, run.route_effort);
  } catch (error) {
    formError(form, error.message);
    retry.hidden = false;
  }
}

async function spawnLineage(run, kind, extra) {
  const key = `${run.id}:${kind}`;
  if (!lineageIds.has(key)) lineageIds.set(key, crypto.randomUUID());
  const created = await api.post(`/api/runs/${run.id}/${kind}`, { request_id: lineageIds.get(key), ...extra });
  lineageIds.delete(key);
  store.snapshotsStale = true;
  toast(`Run ${runLabel(created)} queued as ${kind === "retry" ? "retry" : "continuation"} of run ${run.id}`);
  location.hash = `#/runs/${created.id}`;
}

function mergeResultBlock(result) {
  const ok = result.merged === true;
  const block = el("div", { class: `evidence-block merge-result ${ok ? "good" : "bad"}` },
    el("h2", { text: "Merge" }),
    el("p", { text: ok ? `Merged ${result.branch} into the owner checkout; HEAD is now ${result.commit}.` : `Not merged (HTTP ${result.status}).` }));
  if (!ok) block.append(boundedPre(result.error));
  return block;
}

Object.assign(ACTIONS, {
  "run-delegate": (button) => {
    const run = store.detail.run;
    if (!run?.request_snapshot?.allow_antigravity) return;
    const dialog = document.getElementById("delegate");
    const form = dialog.querySelector("form");
    document.getElementById("delegate-title").textContent = `Delegate a review of ${runLabel(run)}`;
    formError(form, "");
    dialog.showModal();
    return act("delegate-catalog", button, async () => {
      try {
        const { models } = await api.get("/api/antigravity/models");
        const select = form.elements.model;
        const previous = select.value;
        select.replaceChildren(...models.map((id) => el("option", { value: id, text: id })));
        select.value = previous || (models.find((id) => id.includes("flash-low")) ?? models[0] ?? "");
      } catch (error) {
        formError(form, error.message);
      }
    });
  },
  "delegate-cancel": () => document.getElementById("delegate").close(),
  "delegate-preset": (button) => {
    const select = button.form.elements.model;
    const id = button.dataset.model;
    if (![...select.options].some((option) => option.value === id)) select.append(el("option", { value: id, text: id }));
    select.value = id;
  },
  "run-reroute": (button) => {
    const run = store.detail.run;
    if (!run || !ACTIVE_STATUSES.has(run.status)) return;
    const dialog = document.getElementById("reroute");
    const form = dialog.querySelector("form");
    document.getElementById("reroute-title").textContent = `Reroute ${runLabel(run)}`;
    form.elements.message.value = "";
    dialog.showModal();
    return act("reroute-catalog", button, () => fillRoutes(form, false));
  },
  "reroute-catalog": (button) => act("reroute-catalog", button, () => fillRoutes(button.form, true)),
  "reroute-cancel": () => document.getElementById("reroute").close(),
  "run-retry": async (button) => {
    const run = store.detail.run;
    if (!run || ACTIVE_STATUSES.has(run.status)) return;
    const sure = await confirmDialog({
      title: `Retry ${runLabel(run)}?`,
      body: `Creates a new run that retries ${runLabel(run)}; the new run records retry_of = ${run.id}.`,
      confirmLabel: "Retry",
    });
    if (!sure) return;
    await act(`${run.id}:retry`, button, async () => {
      try {
        await spawnLineage(run, "retry", {});
        actionError("");
      } catch (error) {
        actionError(error.message);
      }
    });
  },
  "run-continue": () => {
    const run = store.detail.run;
    if (!run || ACTIVE_STATUSES.has(run.status)) return;
    const dialog = document.getElementById("continue");
    document.getElementById("continue-title").textContent = `Continue ${runLabel(run)}`;
    document.getElementById("continue-body").textContent = `A new run starts from the same request with your direction appended; the new run records continuation_of = ${run.id}.`;
    dialog.showModal();
    dialog.querySelector("textarea").focus();
  },
  "continue-cancel": () => document.getElementById("continue").close(),
  "run-merge": async (button) => {
    const run = store.detail.run;
    if (!run || ACTIVE_STATUSES.has(run.status) || !run.branch) return;
    const sure = await confirmDialog({
      title: `Merge ${run.branch}?`,
      body: `Merges branch ${run.branch} into the current branch of the owner checkout ${run.cwd}. Merges never happen automatically; a dirty checkout or a conflict is refused and the checkout is left as found.`,
      confirmLabel: "Merge",
      danger: true,
    });
    if (!sure) return;
    await act(`${run.id}:merge`, button, async () => {
      try {
        store.detail.mergeResult = await api.post(`/api/runs/${run.id}/merge`, {});
        toast(`Merged ${run.branch}`);
      } catch (error) {
        store.detail.mergeResult = { merged: false, status: error.status, error: error.message };
        toast("Merge refused; see Changes");
      }
      schedule(true);
      if (store.route.section === "changes") markDirty("run");
      else location.hash = `#/runs/${run.id}/changes`;
    });
  },
});

Object.assign(FORMS, {
  delegate: (form, event) => {
    const run = store.detail.run;
    const objective = form.elements.objective.value.trim();
    if (!run || !objective || !form.elements.model.value) return;
    return act(`${run.id}:delegate`, event?.submitter, async () => {
      formError(form, "Reviewing… this can take several minutes.");
      try {
        await api.post(`/api/runs/${run.id}/delegate`, { objective, model: form.elements.model.value, mode: "review" });
        formError(form, "");
        form.elements.objective.value = "";
        document.getElementById("delegate").close();
        toast("Review recorded; see the 🛰 row in Activity");
      } catch (error) {
        // 502 means the review ran and was recorded as a failure; the row explains it.
        formError(form, error.status === 502 ? "Antigravity reported an error; the delegation row has the details." : error.message);
        if (error.status === 502) document.getElementById("delegate").close();
      }
      store.snapshotsStale = true;
      schedule(true);
    });
  },
  reroute: (form, event) => {
    const key = form.elements.route.value;
    if (!key) return;
    return act(`${store.detail.id}:reroute`, event?.submitter, async () => {
      try {
        await api.post(`/api/runs/${store.detail.id}/reroute`, rerouteBody(key, form.elements.effort.value, form.elements.message.value));
        formError(form, "");
        document.getElementById("reroute").close();
        toast("Reroute queued; the run continues the same goal on the new route and its cache epoch increments");
        schedule(true);
      } catch (error) {
        formError(form, error.message);
      }
    });
  },
  continue: (form, event) => {
    const run = store.detail.run;
    const direction = form.elements.direction.value.trim();
    if (!run || !direction) return;
    return act(`${run.id}:continue`, event?.submitter, async () => {
      try {
        await spawnLineage(run, "continue", { direction });
        form.elements.direction.value = "";
        formError(form, "");
        document.getElementById("continue").close();
      } catch (error) {
        formError(form, error.message);
      }
    });
  },
});

document.querySelector("#reroute select[name=route]").addEventListener("change", (event) => fillEfforts(event.target.form, store.detail.run?.route_effort));
// --- end slice2-routing ---

// --- routing-logic ---
// Option values carry the DSH ACP choice encoding, so "/" inside a model name is safe.
function routeKey(provider, model) {
  return JSON.stringify([provider, model]);
}

function routeEfforts(models, key) {
  const hit = (models || []).find((item) => routeKey(item.provider, item.model) === key);
  return hit ? hit.efforts || [] : [];
}

function rerouteBody(key, effort, message) {
  const [provider, model] = JSON.parse(key);
  return { provider, model, effort: effort || null, message: (message || "").trim() || null };
}
// --- end routing-logic ---

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
store.catalog = null; // /api/models rows for the profile form, or null until loaded
store.catalogError = "";
store.identities = null; // { devices, tokens, revision }

function tableHead(labels) {
  return el("thead", null, el("tr", null, ...labels.map((label) => el("th", { text: label }))));
}

function configError(box, message) {
  const node = box._error ||= el("p", { class: "form-error" });
  node.textContent = message || "";
  node.hidden = !message;
  if (!node.isConnected) box.prepend(node);
}

function configFrame(box, ...children) {
  const previous = box._error?.hidden === false ? box._error.textContent : "";
  box.replaceChildren(...children);
  configError(box, previous);
}

function configMutation(key, button, boxId, work, success) {
  const box = document.getElementById(boxId);
  return act(key, button, async () => {
    try {
      await work();
      configError(box, "");
      toast(success);
    } catch (error) {
      configError(box, error.message);
    }
    if (store.identities) store.identities.revision = -1;
    store.snapshotsStale = true;
    schedule(true);
  });
}

function loadIdentities() {
  const revision = store.boardRevision;
  return act("identities", null, async () => {
    const [devices, tokens] = await Promise.all([api.get("/api/auth/devices"), api.get("/api/auth/service-tokens")]);
    store.identities = { devices, tokens, revision };
    markDirty("config");
  }).catch((error) => configError(document.getElementById("config-identities-list"), error.message));
}

function loadCatalog(form) {
  return api.get("/api/models").then((value) => {
    store.catalog = value.models;
    store.catalogError = "";
  }).catch((error) => {
    store.catalog = null;
    store.catalogError = `Model catalog unavailable (${error.message}). Enter provider, model, and effort by hand.`;
  }).then(() => {
    fillRouteLists(form);
    formError(form, store.catalogError);
  });
}

function fillRouteLists(form) {
  const models = store.catalog || [];
  const provider = form.elements.provider.value.trim();
  const model = form.elements.model.value.trim();
  const option = (value) => el("option", { value });
  document.getElementById("profile-providers").replaceChildren(...[...new Set(models.map((row) => row.provider))].map(option));
  document.getElementById("profile-models").replaceChildren(...models.filter((row) => !provider || row.provider === provider).map((row) => option(row.model)));
  document.getElementById("profile-efforts").replaceChildren(...modelEfforts(models, provider, model).map(option));
}

function openProfileDialog(profile) {
  const dialog = document.getElementById("profile-dialog");
  const form = dialog.querySelector("form");
  form.reset();
  form._original = profile || null;
  document.getElementById("profile-dialog-title").textContent = profile ? `Edit profile ${profile.slug}` : "New profile";
  form.elements.slug.closest("label").hidden = Boolean(profile);
  if (profile) for (const key of PROFILE_FIELDS) form.elements[key].value = profile[key] ?? "";
  formError(form, store.catalogError);
  fillRouteLists(form);
  dialog.showModal();
  if (store.catalog === null) loadCatalog(form);
}

async function groupEdit(button, field) {
  const group = store.groups.find((row) => row.slug === button.dataset.slug);
  if (!group) return;
  let value;
  if (field === "archived") {
    value = !group.archived;
    const sure = await confirmDialog({
      title: `${value ? "Archive" : "Unarchive"} group ${group.slug}?`,
      body: value ? "Archived groups leave the dispatch form. Existing runs keep their group." : "",
      confirmLabel: value ? "Archive" : "Unarchive",
      danger: value,
    });
    if (!sure) return;
  } else {
    const typed = await confirmDialog({
      title: field === "name" ? `Rename group ${group.slug}` : `Set default directory for ${group.slug}`,
      body: field === "name" ? `Current name: ${group.name}` : `Current: ${group.default_cwd || "none"}. Leave blank to clear.`,
      confirmLabel: "Save",
      input: field === "name" ? "Name" : "Directory",
    });
    if (typed === false) return;
    value = typed.trim() || null;
  }
  const retained = typeof value === "string" ? ` (you entered: ${value})` : "";
  await configMutation(`group:${group.slug}:${field}`, button, "config-groups", async () => {
    try {
      await api.patch(`/api/groups/${group.slug}`, groupPatch(field, value, group.revision));
    } catch (error) {
      throw new ApiError(error.status, error.message + retained);
    }
  }, `Group ${group.slug} updated`);
}

Object.assign(ACTIONS, {
  "dialog-cancel": (button) => button.closest("dialog").close(),
  "profile-new": () => openProfileDialog(null),
  "profile-edit": (button) => {
    const profile = store.profiles.find((row) => row.slug === button.dataset.slug);
    if (profile) openProfileDialog(profile);
  },
  "profile-toggle": (button) => {
    const profile = store.profiles.find((row) => row.slug === button.dataset.slug);
    const field = button.dataset.field;
    if (!profile) return;
    const next = field === "enabled" ? profile.enabled === false : !profile.archived;
    return configMutation(`profile:${profile.slug}:${field}`, button, "config-profiles",
      () => api.patch(`/api/profiles/${profile.slug}`, { expected_revision: profile.revision, [field]: next }),
      `Profile ${profile.slug} ${field === "enabled" ? (next ? "enabled" : "disabled") : (next ? "archived" : "unarchived")}`);
  },
  "group-new": () => {
    const dialog = document.getElementById("group-dialog");
    formError(dialog.querySelector("form"), "");
    dialog.showModal();
  },
  "group-rename": (button) => groupEdit(button, "name"),
  "group-cwd": (button) => groupEdit(button, "cwd"),
  "group-archive": (button) => groupEdit(button, "archived"),
  "token-new": () => {
    const dialog = document.getElementById("token-dialog");
    formError(dialog.querySelector("form"), "");
    dialog.showModal();
  },
  "device-revoke": async (button) => {
    const sure = await confirmDialog({ title: `Revoke device ${button.dataset.name}?`, body: "The device loses access immediately.", confirmLabel: "Revoke", danger: true });
    if (!sure) return;
    return configMutation(`device:${button.dataset.id}`, button, "config-identities-list", () => api.post(`/api/auth/devices/${button.dataset.id}/revoke`), "Device revoked");
  },
  "token-revoke": async (button) => {
    const sure = await confirmDialog({ title: `Revoke service token ${button.dataset.name}?`, body: "Integrations that hold this token lose access immediately.", confirmLabel: "Revoke", danger: true });
    if (!sure) return;
    return configMutation(`token:${button.dataset.id}`, button, "config-identities-list", () => api.post(`/api/auth/service-tokens/${button.dataset.id}/revoke`), "Service token revoked");
  },
});

Object.assign(FORMS, {
  profile: (form) => act("profile", form.querySelector("button[type=submit]"), async () => {
    const raw = Object.fromEntries(["slug", ...PROFILE_FIELDS].map((key) => [key, form.elements[key].value]));
    const original = form._original;
    const body = profileBody(raw);
    try {
      if (original) {
        const patch = profilePatch(original, body);
        if (Object.keys(patch).length > 1) await api.patch(`/api/profiles/${original.slug}`, patch);
      } else {
        await api.post("/api/profiles", body);
      }
      formError(form, "");
      form.closest("dialog").close();
      toast(original ? `Profile ${original.slug} updated` : `Profile ${body.slug || body.name} created`);
      store.snapshotsStale = true;
      schedule(true);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409 && original) {
        // Refresh the row, keep the operator's edits, and reload only the fields they left alone.
        const edited = profilePatch(original, body);
        store.profiles = await api.get("/api/profiles").catch(() => store.profiles);
        const fresh = store.profiles.find((row) => row.id === original.id) || original;
        form._original = fresh;
        for (const key of PROFILE_FIELDS) if (!(key in edited)) form.elements[key].value = fresh[key] ?? "";
        markDirty("config");
        formError(form, `This profile changed elsewhere (now revision ${fresh.revision}). Your edits are kept; review them and save again.`);
      } else {
        formError(form, error.message);
      }
    }
  }),
  group: (form) => act("group", form.querySelector("button[type=submit]"), async () => {
    const body = { name: form.elements.name.value.trim() };
    if (form.elements.slug.value.trim()) body.slug = form.elements.slug.value.trim();
    if (form.elements.cwd.value.trim()) body.cwd = form.elements.cwd.value.trim();
    try {
      const group = await api.post("/api/groups", body);
      formError(form, "");
      form.closest("dialog").close();
      form.reset();
      toast(`Group ${group.slug} created`);
      store.snapshotsStale = true;
      schedule(true);
    } catch (error) {
      formError(form, error.message);
    }
  }),
  token: (form) => act("token", form.querySelector("button[type=submit]"), async () => {
    const authorities = [...form.querySelectorAll("input[name=authorities]:checked")].map((box) => box.value);
    try {
      const { token } = await api.post("/api/auth/service-tokens", { name: form.elements.name.value.trim(), authorities });
      formError(form, "");
      form.closest("dialog").close();
      form.reset();
      document.getElementById("token-raw").textContent = token;
      document.getElementById("token-reveal").showModal();
      if (store.identities) store.identities.revision = -1;
      markDirty("config");
    } catch (error) {
      formError(form, error.message);
    }
  }),
});

document.querySelector("form[data-form=profile]").addEventListener("input", (event) => fillRouteLists(event.currentTarget));
document.getElementById("token-reveal").addEventListener("close", () => { document.getElementById("token-raw").textContent = ""; });
// --- end slice2-config ---

// --- config-logic ---
const PROFILE_FIELDS = ["name", "provider", "model", "effort", "tier", "max_concurrency", "note"];

function profileBody(raw) {
  const text = (key) => String(raw[key] ?? "").trim();
  const body = {
    name: text("name"), provider: text("provider"), model: text("model"),
    effort: text("effort") || null,
    tier: Number(text("tier")),
    max_concurrency: text("max_concurrency") ? Number(text("max_concurrency")) : null,
    note: text("note") || null,
  };
  if (text("slug")) body.slug = text("slug");
  return body;
}

function profilePatch(original, body) {
  const patch = { expected_revision: original.revision };
  for (const key of PROFILE_FIELDS) {
    if (key in body && body[key] !== (original[key] ?? null)) patch[key] = body[key];
  }
  return patch;
}

function groupPatch(field, value, revision) {
  return { expected_revision: revision, [field]: value };
}

function modelEfforts(models, provider, model) {
  return models.find((row) => row.provider === provider && row.model === model)?.efforts || [];
}
// --- end config-logic ---

// --- slice3-admin ---
// Storage prune plans, the controls/callbacks audit feeds, and pairing another
// device (code + countdown + QR of this console's #/pair/{code} link).
function adminState() {
  if (store.admin?.instance !== store.instanceId) {
    store.admin = {
      instance: store.instanceId, storage: null, storageLoading: false, plan: null, pairing: null,
      controls: { rows: [], after: 0, filter: "", loaded: false },
      callbacks: { rows: [], after: 0, filter: "", loaded: false },
    };
  }
  return store.admin;
}

function stale(node, sig) {
  if (node._sig === sig) return false;
  node._sig = sig;
  return true;
}

function planTotals(items) {
  return { count: items.length, bytes: items.reduce((n, item) => n + (item.size_bytes || 0), 0) };
}

function svgEl(tag, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function qrSvg(text) {
  let grid;
  try {
    grid = qrMatrix(text);
  } catch {
    return el("p", { class: "form-error", text: "The pairing link is too long for a QR code; type the code instead." });
  }
  const quiet = 4;
  const size = grid.length + quiet * 2;
  let path = "";
  for (let r = 0; r < grid.length; r += 1) for (let c = 0; c < grid.length; c += 1) if (grid[r][c]) path += `M${c + quiet} ${r + quiet}h1v1h-1z`;
  const svg = svgEl("svg", { class: "qr", viewBox: `0 0 ${size} ${size}`, "shape-rendering": "crispEdges", role: "img", "aria-label": "QR code of the pairing link" });
  svg.append(svgEl("rect", { class: "qr-ground", width: size, height: size }), svgEl("path", { class: "qr-ink", d: path }));
  return svg;
}

function renderConfigPairing() {
  const box = document.getElementById("config-pairing");
  const pairing = adminState().pairing;
  if (stale(box, pairing?.code ?? "")) {
    const parts = [
      el("p", { class: "muted", text: "A code pairs one more browser. Scan the QR with the other device to open this console with the code filled in, or type the code on its pairing screen." }),
      el("p", null, el("button", { type: "button", class: "btn btn-primary", dataset: { action: "pair-create" }, text: pairing ? "Create another code" : "Create pairing code" })),
      el("p", { class: "form-error", hidden: "" }),
    ];
    if (pairing) {
      const link = `${location.origin}/#/pair/${pairing.code}`;
      parts.push(el("div", { class: "pair-box" }, qrSvg(link), el("div", null,
        el("div", { class: "pair-code", text: pairing.code }),
        el("div", { class: "muted" }, "expires in ", el("span", { class: "countdown", dataset: { expires: pairing.expires_at }, text: fmt.countdown(pairing.expires_at) })),
        el("div", { class: "mono faint", text: link }))));
    }
    box.replaceChildren(...parts);
  }
  syncPairTimer();
}

let pairTimer = null;
function syncPairTimer() {
  if (pairTimer || !document.querySelector("#config-pairing .countdown")) return;
  pairTimer = setInterval(() => {
    const node = document.querySelector("#config-pairing .countdown");
    const live = node && !node.closest("[hidden]");
    if (live) {
      node.textContent = fmt.countdown(node.dataset.expires);
      node.classList.toggle("urgent", node.textContent !== "expired" && Date.parse(node.dataset.expires) - Date.now() < 60000);
    }
    if (!live || node.textContent === "expired") {
      clearInterval(pairTimer);
      pairTimer = null;
    }
  }, 1000);
}

function renderConfigStorage() {
  const box = document.getElementById("config-storage");
  const admin = adminState();
  if (admin.storage === null) loadStorage(box);
  const plan = admin.plan;
  if (!stale(box, JSON.stringify([admin.storage, plan?.plan_id, plan?.applied_at]))) return;
  const days = box.querySelector("[name=older_than_days]")?.value || "30";
  const parts = [];
  if (admin.storage) {
    const report = admin.storage;
    const kv = el("dl", { class: "kv" });
    const fact = (name, value) => kv.append(el("dt", { text: name }), el("dd", { class: "mono", text: value }));
    fact("database", fmt.bytes(report.database_bytes));
    fact("run state", fmt.bytes(report.run_bytes));
    fact("artifacts", fmt.bytes(report.artifact_bytes));
    fact("worktrees", fmt.bytes(report.worktree_bytes));
    fact("runs", `${fmt.count(report.runs)} · ${fmt.count(report.pinned_runs)} pinned`);
    fact("retention", report.retention);
    parts.push(kv);
  } else {
    parts.push(el("p", { class: "view-state", text: "Loading storage report…" }));
  }
  parts.push(el("form", { class: "admin-form", dataset: { form: "storage-plan" } },
    el("label", null, "Prune finished, unpinned runs older than (days)", el("input", { name: "older_than_days", type: "number", min: "0", value: days, required: "" })),
    el("button", { type: "submit", class: "btn", text: "Plan prune" }),
    el("p", { class: "form-error", hidden: "" })));
  if (plan) parts.push(buildPlan(plan));
  box.replaceChildren(...parts);
}

function loadStorage(box) {
  const admin = adminState();
  if (admin.storageLoading) return;
  admin.storageLoading = true;
  api.get("/api/storage").then((report) => {
    admin.storage = report;
    markDirty("config");
  }).catch((error) => formError(box, error.message)).finally(() => { admin.storageLoading = false; });
}

function buildPlan(plan) {
  const applied = Boolean(plan.applied_at);
  const items = applied ? plan.result.moved : plan.items;
  const totals = planTotals(items);
  const facts = el("dl", { class: "kv" });
  const fact = (name, value) => facts.append(el("dt", { text: name }), el("dd", { class: "mono", text: value }));
  fact("plan", plan.plan_id);
  fact("cutoff", `finished before ${plan.criteria.cutoff} (${plan.criteria.older_than_days} days)`);
  fact(applied ? "moved" : "items", `${fmt.count(totals.count)} · ${fmt.bytes(totals.bytes)}`);
  if (applied) {
    fact("applied", `${plan.applied_at} by ${plan.applied_by}`);
    fact("trash", plan.result.trash);
  }
  const table = items.length ? el("table", { class: "plain" },
    el("thead", null, el("tr", null, ...["Kind", "Run", applied ? "Recoverable at" : "Path", "Size"].map((h) => el("th", { text: h })))),
    el("tbody", null, ...items.map((item) => el("tr", null,
      el("td", { text: item.kind }),
      el("td", { class: "mono", text: String(item.run_id) }),
      el("td", { class: "mono", text: applied ? item.recoverable_at : item.path }),
      el("td", { text: fmt.bytes(item.size_bytes) })))))
    : el("p", { class: "muted", text: applied ? "Nothing was moved." : "Nothing to prune: no unpinned finished run is older than the cutoff." });
  const action = !applied && items.length ? el("p", null, el("button", { type: "button", class: "btn btn-danger", dataset: { action: "storage-apply" }, text: "Apply this plan" })) : null;
  return el("div", { class: "evidence-block plan" },
    el("h2", { text: applied ? "Applied prune plan" : "Prune plan (dry run)" }),
    facts, table, action, el("p", { class: "form-error", hidden: "" }));
}

function renderConfigAudit() {
  const box = document.getElementById("config-audit");
  const admin = adminState();
  for (const name of ["controls", "callbacks"]) if (!admin[name].loaded) loadFeed(name);
  if (!stale(box, ["controls", "callbacks"].map((name) => `${admin[name].loaded}:${admin[name].rows.length}`).join())) return;
  box.replaceChildren(buildFeed("controls", admin.controls), buildFeed("callbacks", admin.callbacks), el("p", { class: "form-error", hidden: "" }));
}

function loadFeed(name, button) {
  const feed = adminState()[name];
  return act(`audit:${name}`, button, async () => {
    try {
      const page = await api.get(`/api/${name}?after=${feed.after}`);
      if (feed.loaded && !page.length) toast(`No newer ${name}`);
      feed.loaded = true;
      feed.rows.push(...page);
      if (page.length) feed.after = page[page.length - 1].id;
      markDirty("config");
    } catch (error) {
      formError(document.getElementById("config-audit"), error.message);
    }
  });
}

function buildFeed(name, feed) {
  const filter = el("input", { type: "search", placeholder: "Filter", value: feed.filter, "aria-label": `Filter ${name}` });
  const rows = feed.rows.map((row) => el("tr", null,
    el("td", { title: row.created_at, text: fmt.rel(row.created_at) }),
    el("td", { class: "mono", text: row.actor }),
    el("td", { class: "mono", text: row.action }),
    el("td", { class: "mono", text: [row.target_type, row.target_id].filter(Boolean).join(" ") }),
    el("td", { text: row.outcome }),
    el("td", null, row.detail ? el("details", null, el("summary", { class: "muted", text: "detail" }), boundedPre(row.detail)) : null)));
  const apply = () => {
    feed.filter = filter.value;
    const needle = filter.value.trim().toLowerCase();
    for (const row of rows) row.hidden = Boolean(needle) && !row.textContent.toLowerCase().includes(needle);
  };
  filter.addEventListener("input", apply);
  apply();
  let body;
  if (!feed.loaded) body = el("p", { class: "view-state", text: `Loading ${name}…` });
  else if (!rows.length) body = el("p", { class: "muted", text: `No ${name} recorded.` });
  else body = el("table", { class: "plain" }, el("thead", null, el("tr", null, ...["When", "Actor", "Action", "Target", "Outcome", ""].map((h) => el("th", { text: h })))), el("tbody", null, rows));
  return el("div", { class: "audit-feed" },
    el("div", { class: "audit-tools" }, el("h3", { text: name }), filter, el("button", { type: "button", class: "btn", dataset: { action: "audit-more", feed: name }, text: "Load more" })),
    body);
}

Object.assign(ACTIONS, {
  "pair-create": (button) => act("pair-create", button, async () => {
    const box = document.getElementById("config-pairing");
    try {
      adminState().pairing = await api.post("/api/auth/pair", {});
      formError(box, "");
      markDirty("config");
    } catch (error) {
      formError(box, error.message);
    }
  }),
  "storage-apply": async (button) => {
    const admin = adminState();
    const plan = admin.plan;
    if (!plan || plan.applied_at) return;
    const totals = planTotals(plan.items);
    const sure = await confirmDialog({
      title: "Apply this prune plan?",
      body: `${fmt.count(totals.count)} items (${fmt.bytes(totals.bytes)}) move to the trash directory and their artifacts are marked pruned. Recover them from the trash by hand if needed.`,
      confirmLabel: `Move ${fmt.count(totals.count)} items to trash`,
      danger: true,
    });
    if (!sure) return;
    await act("storage-apply", button, async () => {
      try {
        admin.plan = await api.post(`/api/storage/plans/${plan.plan_id}/apply`, {});
        admin.storage = null;
        toast(`Moved ${fmt.count(admin.plan.result.moved.length)} items to the trash`);
        markDirty("config");
      } catch (error) {
        formError(document.querySelector("#config-storage .plan"), error.message);
      }
    });
  },
  "audit-more": (button) => loadFeed(button.dataset.feed, button),
});

Object.assign(FORMS, {
  "storage-plan": (form) => act("storage-plan", form.querySelector("button[type=submit]"), async () => {
    try {
      adminState().plan = await api.post("/api/storage/plans", { older_than_days: Number(form.elements.older_than_days.value) });
      formError(form, "");
      markDirty("config");
    } catch (error) {
      formError(form, error.message);
    }
  }),
});
// --- end slice3-admin ---

// --- config-tabs ---
// One Config tab renders at a time, so paged or expensive data (audit feeds,
// storage report, identities) loads only when its tab is shown.
function renderConfigTabs() {
  const tabs = document.getElementById("config-tabs");
  const current = store.ui.configTab;
  keyedList(tabs, CONFIG_TABS, (tab) => tab,
    (tab) => `${tab === current}`,
    (tab) => el("button", { role: "tab", "aria-selected": String(tab === current), dataset: { action: "config-tab", tab }, text: tab[0].toUpperCase() + tab.slice(1) }),
    (node, tab) => node.setAttribute("aria-selected", String(tab === current)));
  for (const panel of document.querySelectorAll("[data-ctab]")) panel.hidden = panel.dataset.ctab !== current;
}

Object.assign(ACTIONS, {
  "config-tab": (button) => { location.hash = `#/config/${button.dataset.tab}`; },
});

// config-tab:identities
function renderConfigIdentitiesTab() {
  renderConfigIdentities();
  renderConfigPairing();
}
// /config-tab:identities

// config-tab:settings
function renderConfigSettings() {
  // Shell: the Settings tab is static until its feature lands here.
}
// /config-tab:settings

// config-tab:logs
function renderConfigLogs() {
  // Shell: the Logs tab is static until its feature lands here.
}
// /config-tab:logs

const CONFIG_RENDERERS = {
  // config-tab:profiles
  profiles: renderConfigProfiles,
  // /config-tab:profiles
  // config-tab:groups
  groups: renderConfigGroups,
  // /config-tab:groups
  identities: renderConfigIdentitiesTab,
  // config-tab:storage
  storage: renderConfigStorage,
  // /config-tab:storage
  // config-tab:audit
  audit: renderConfigAudit,
  // /config-tab:audit
  settings: renderConfigSettings,
  logs: renderConfigLogs,
  // config-tab:diagnostics
  diagnostics: renderConfigDiagnostics,
  // /config-tab:diagnostics
};
// --- end config-tabs ---

// --- router ---
function parseHash() {
  const segments = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (segments[0] === "runs" && /^\d+$/.test(segments[1] || "")) {
    const section = SECTIONS.includes(segments[2]) ? segments[2] : segments[2] ? "activity" : store.ui.section;
    return { view: "run", runId: Number(segments[1]), section };
  }
  if (segments[0] === "attention") return { view: "attention", runId: null, section: null };
  if (segments[0] === "config") return { view: "config", runId: null, section: CONFIG_TABS.includes(segments[1]) ? segments[1] : store.ui.configTab };
  if (segments[0] === "pair" && segments[1]) return { view: "pair", runId: null, section: null, code: segments[1] };
  return { view: "fleet", runId: null, section: null };
}

function applyRoute() {
  const route = parseHash();
  if (route.view === "pair") {
    // A scanned pairing link: prefill the code, then continue to Config. The
    // pairing screen shows itself while the browser is unpaired.
    if (store.auth !== "ok") document.querySelector("form[data-form=pair] input[name=code]").value = route.code;
    location.hash = "#/config";
    return;
  }
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
  if (route.view === "run" && route.section !== store.ui.section) {
    store.ui.section = route.section;
    saveUiState();
  }
  if (route.view === "config" && route.section !== store.ui.configTab) {
    store.ui.configTab = route.section;
    saveUiState();
  }
  markDirty("fleet", "run", "attention", "config", "nav");
}
// --- end router ---

// --- stop-tree ---
Object.assign(ACTIONS, {
  "run-stop-tree": async (button) => {
    const run = store.detail.run;
    const reason = await confirmDialog({
      title: `Stop run ${runLabel(run)} and its ${fmt.count(run.child_count)} children?`,
      body: "A stop is queued for this run and every active descendant; each is delivered at its next safe boundary.",
      confirmLabel: "Stop tree",
      danger: true,
      input: "Reason (optional)",
    });
    if (reason === false) return;
    runVerb("stop-tree", button, { reason: reason || "stopped by operator" });
  },
});
// --- end stop-tree ---

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
  delete field._pending;
  filterDebounce = setTimeout(() => {
    store.filters[field.dataset.filter] = field.value;
    saveUiState();
    if (field.dataset.filter === "text") markDirty("fleet");
    else refetchFleet();
  }, field.dataset.filter === "text" ? 150 : 0);
}
document.addEventListener("input", onFilterInput);
document.addEventListener("change", onFilterInput);

document.addEventListener("keydown", (event) => {
  const typing = event.target.matches("input,textarea,select") || event.target.isContentEditable;
  const dialogOpen = Boolean(document.querySelector("dialog[open]"));
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
  if (store.route.view === "config" && (event.key === "[" || event.key === "]")) {
    const at = CONFIG_TABS.indexOf(store.ui.configTab);
    location.hash = `#/config/${CONFIG_TABS[(at + (event.key === "]" ? 1 : CONFIG_TABS.length - 1)) % CONFIG_TABS.length]}`;
  }
});

document.getElementById("feed").addEventListener("scroll", () => {
  const feed = document.getElementById("feed");
  const atTail = feed.scrollHeight - feed.scrollTop - feed.clientHeight < TAIL_THRESHOLD;
  if (atTail !== store.ui.follow) setFollow(atTail);
});
document.getElementById("follow-live").addEventListener("change", (event) => setFollow(event.target.checked));
document.getElementById("return-live").addEventListener("click", () => setFollow(true));
document.getElementById("show-machine").addEventListener("change", (event) => {
  store.ui.machine = event.target.checked;
  saveUiState();
  markDirty("run");
});

// Restore retained state before the first route and render.
{
  const saved = loadUiState();
  for (const key of Object.keys(store.filters)) if (key in saved.filters) store.filters[key] = saved.filters[key];
  store.ui.machine = saved.machine;
  store.ui.section = saved.section;
  store.ui.configTab = saved.configTab;
  document.getElementById("show-machine").checked = saved.machine;
  for (const field of document.querySelectorAll("[data-filter]")) {
    const value = store.filters[field.dataset.filter];
    if (typeof value !== "string" || !value) continue;
    field.value = value;
    if (field.tagName === "SELECT" && field.value !== value) field._pending = value;
  }
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) schedule(true);
});

window.addEventListener("hashchange", applyRoute);
applyRoute();
tick();
// --- end boot ---
