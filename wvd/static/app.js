// WireView dashboard: one client of the wvd API among many (CLI, tests, Prometheus).
const PINS = 6;
const TEMP_KEYS = ["in", "out", "ext1", "ext2"];
const TEMP_NAMES = { in: "In", out: "Out", ext1: "Ext 1", ext2: "Ext 2" };  // short: they share a legend row
const RENDER_MS = 250;

const $ = (id) => document.getElementById(id);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* private mode */ } },
};

const state = {
  token: store.get("wvd.token"),
  range: Number(store.get("wvd.range")) || 300,
  paused: false,
  limits: {},
  info: null,
  latest: null,
  lastSeq: 0,
  ws: null,
  wsRetry: 1000,
  dirty: false,
  activeSession: null,
  buf: { ts: [], power: [], cur: Array.from({ length: PINS }, () => []), volt: Array.from({ length: PINS }, () => []),
         temp: Object.fromEntries(TEMP_KEYS.map((k) => [k, []])) },
  charts: {},
  chartH: 240,
  tempKeys: [],
};

// ---------------------------------------------------------------- API
function withToken(path) {
  if (!state.token) return path;
  return path + (path.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(state.token);
}

async function api(path, opts = {}) {
  const headers = { ...(opts.body ? { "Content-Type": "application/json" } : {}) };
  if (state.token) headers.Authorization = "Bearer " + state.token;
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401) { showAuth(); throw new Error("unauthorized"); }
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(detail);
  }
  return r.json();
}

function showAuth() {
  $("auth").hidden = false;
  setStatus("down", "Token required");
}

$("auth-form").addEventListener("submit", (e) => {
  e.preventDefault();
  state.token = $("auth-token").value.trim() || null;
  store.set("wvd.token", state.token);
  $("auth").hidden = true;
  start();
});

// ---------------------------------------------------------------- formatting
const fmt = (v, d = 2) => (v == null || Number.isNaN(v) ? "–" : v.toFixed(d));
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
const dateTime = (ts) => new Date(ts * 1000).toLocaleString([], { hour12: false });
const shortTime = (ts) => (new Date(ts * 1000).toDateString() === new Date().toDateString() ? clock(ts) : dateTime(ts));
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const seriesColor = (i) => cssVar(`--series-${i + 1}`);

function setStatus(kind, text) {
  $("status").className = "status " + kind;
  $("status-text").textContent = text;
}

// ---------------------------------------------------------------- buffers
function pushSample(s) {
  if (s.seq <= state.lastSeq) return;
  state.lastSeq = s.seq;
  state.latest = s;
  const b = state.buf;
  b.ts.push(s.ts);
  b.power.push(s.total_w);
  for (let i = 0; i < PINS; i++) { b.cur[i].push(s.pins[i].a); b.volt[i].push(s.pins[i].v); }
  for (const k of TEMP_KEYS) b.temp[k].push(s.temps_c[k]);
  state.dirty = true;
}

function trim() {
  const b = state.buf;
  const cutoff = Date.now() / 1000 - state.range;
  let n = 0;
  while (n < b.ts.length && b.ts[n] < cutoff) n++;
  if (!n) return;
  b.ts.splice(0, n); b.power.splice(0, n);
  for (let i = 0; i < PINS; i++) { b.cur[i].splice(0, n); b.volt[i].splice(0, n); }
  for (const k of TEMP_KEYS) b.temp[k].splice(0, n);
}

function resetBuffers() {
  const b = state.buf;
  b.ts = []; b.power = [];
  b.cur = Array.from({ length: PINS }, () => []); b.volt = Array.from({ length: PINS }, () => []);
  b.temp = Object.fromEntries(TEMP_KEYS.map((k) => [k, []]));
  state.lastSeq = 0;
}

async function loadHistory() {
  document.querySelector(".charts").classList.add("loading");
  try {
    const { samples } = await api(`/api/v1/sensors/history?last=${state.range}s`);
    resetBuffers();
    samples.forEach(pushSample);
  } finally {
    document.querySelector(".charts").classList.remove("loading");
  }
}

// ---------------------------------------------------------------- charts
function limitLine(limitKey) {
  return (u) => {
    const v = state.limits[limitKey];
    if (v == null) return;
    const [lo, hi] = [u.scales.y.min, u.scales.y.max];
    if (v < lo || v > hi) return;
    const y = Math.round(u.valToPos(v, "y", true));
    const ctx = u.ctx;
    ctx.save();
    ctx.strokeStyle = cssVar("--critical");
    ctx.lineWidth = 2 * devicePixelRatio;
    ctx.setLineDash([6 * devicePixelRatio, 4 * devicePixelRatio]);
    ctx.beginPath();
    ctx.moveTo(u.bbox.left, y);
    ctx.lineTo(u.bbox.left + u.bbox.width, y);
    ctx.stroke();
    ctx.restore();
  };
}

// X-axis labels: drop the seconds once ticks are a minute or more apart, so they stay short.
// Min px between ticks: HH:MM labels (ranges of 2 min and up) pack tighter than HH:MM:SS.
const tickSpace = (u, axisIdx, min, max) => (max - min >= 120 ? 46 : 64);

function timeTicks(u, splits, axisIdx, space, incr) {
  const opts = incr >= 60 ? { hour: "2-digit", minute: "2-digit" } : { hour: "2-digit", minute: "2-digit", second: "2-digit" };
  return splits.map((v) => new Date(v * 1000).toLocaleTimeString([], { ...opts, hour12: false }));
}

function makeChart(el, series, { limit, decimals = 2, yMin } = {}) {
  const axis = { stroke: cssVar("--muted"), grid: { stroke: cssVar("--grid"), width: 1 },
                 ticks: { stroke: cssVar("--axis"), width: 1 }, font: "13px system-ui, sans-serif" };
  const opts = {
    width: el.clientWidth || 600,
    height: state.chartH,
    cursor: { y: false, points: { size: 8 } },
    legend: { live: true },
    scales: { x: { time: true }, y: { range: (u, min, max) => {
      if (min == null) return [0, 1];
      const pad = Math.max((max - min) * 0.1, Math.abs(max) * 0.02, 0.01);
      return [yMin != null ? Math.min(yMin, min) : min - pad, max + pad];
    } } },
    axes: [{ ...axis, font: "12px system-ui, sans-serif", space: tickSpace, values: timeTicks }, { ...axis, size: 62 }],
    series: [
      { value: (u, v) => (v == null ? "–" : clock(v)) },
      ...series.map((s) => ({ label: s.label, stroke: s.color, width: 2, points: { show: false },
                              value: (u, v) => fmt(v, decimals), spanGaps: false })),
    ],
    hooks: limit ? { draw: [limitLine(limit)] } : {},
  };
  return new uPlot(opts, [[], ...series.map(() => [])], el);
}

function presentTempKeys() {
  return TEMP_KEYS.filter((k) => state.buf.temp[k].some((v) => v != null));
}

function buildCharts() {
  for (const c of Object.values(state.charts)) c.destroy();
  for (const id of ["c-power", "c-current", "c-voltage", "c-temp"]) $(id).textContent = "";
  const pins = Array.from({ length: PINS }, (_, i) => ({ label: `P${i + 1}`, color: seriesColor(i) }));
  state.tempKeys = presentTempKeys();
  state.charts = {
    power: makeChart($("c-power"), [{ label: "Total", color: seriesColor(0) }], { limit: "total_w", yMin: 0 }),
    current: makeChart($("c-current"), pins, { limit: "pin_a", decimals: 3, yMin: 0 }),
    voltage: makeChart($("c-voltage"), pins, { decimals: 3 }),
    temp: makeChart($("c-temp"), state.tempKeys.map((k, i) => ({ label: TEMP_NAMES[k], color: seriesColor(i) })),
                    { limit: "temp_c", decimals: 1 }),
  };
  requestAnimationFrame(fitCharts);
  state.dirty = true;
}

function renderCharts() {
  const b = state.buf;
  const keys = presentTempKeys();
  if (keys.join() !== state.tempKeys.join()) buildCharts();
  const c = state.charts;
  c.power.setData([b.ts, b.power]);
  c.current.setData([b.ts, ...b.cur]);
  c.voltage.setData([b.ts, ...b.volt]);
  c.temp.setData([b.ts, ...state.tempKeys.map((k) => b.temp[k])]);
  // Off-chart, the legend reads out the newest values instead of dashes.
  for (const u of Object.values(c)) {
    if (u.cursor.left == null || u.cursor.left < 0) u.setLegend({ idx: b.ts.length - 1 });
  }
}

const CHART_IDS = { power: "c-power", current: "c-current", voltage: "c-voltage", temp: "c-temp" };
const WIDE = matchMedia("(min-width: 1500px)");

// Narrow screens scroll, so charts keep a fixed height. On a wide monitor the tile
// grid (charts + sessions + device) is sized so its rows end at the bottom of the viewport.
function chartHeight() {
  if (!WIDE.matches || !state.charts.power) return 240;
  const grid = document.querySelector(".charts");
  const top = grid.getBoundingClientRect().top + scrollY;
  const gap = parseFloat(getComputedStyle(grid).rowGap) || 16;
  const pad = parseFloat(getComputedStyle(document.querySelector("main")).paddingBottom) || 16;
  // Card chrome around the plot (title, padding, legend), worst case of the four.
  const chrome = Math.max(...Object.entries(CHART_IDS).map(([k, id]) =>
    $(id).closest(".chart").offsetHeight - state.charts[k].height));
  const cols = getComputedStyle(grid).gridTemplateColumns.split(" ").length;
  const rows = Math.ceil(grid.children.length / cols);
  const h = Math.floor((innerHeight - top - pad - gap * (rows - 1)) / rows - chrome);
  return Math.max(200, Math.min(520, h));
}

function fitCharts() {
  state.chartH = chartHeight();
  for (const [key, id] of Object.entries(CHART_IDS)) {
    const chart = state.charts[key];
    const w = $(id).clientWidth;
    if (chart && w && (chart.width !== w || chart.height !== state.chartH)) chart.setSize({ width: w, height: state.chartH });
  }
}

new ResizeObserver(fitCharts).observe(document.querySelector(".charts"));
addEventListener("resize", fitCharts);
WIDE.addEventListener("change", fitCharts);

// ---------------------------------------------------------------- KPIs & pins
function overLimit(el, value, limit, text) {
  const over = limit != null && value != null && value > limit;
  el.classList.toggle("over", over);
  el.textContent = over ? `${text} over ${limit} limit` : (limit != null ? `limit ${limit}` : "");
}

function buildPinRows() {
  const bars = $("pin-bars");
  const rows = $("pin-rows");
  bars.textContent = ""; rows.textContent = "";
  for (let i = 0; i < PINS; i++) {
    const row = document.createElement("div");
    row.className = "pin-row";
    row.innerHTML = `<span class="name"></span><div class="track"><div class="bar"></div><div class="limit"></div></div><span class="val"></span>`;
    row.querySelector(".name").textContent = `Pin ${i + 1}`;
    bars.appendChild(row);
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><span class="key"></span></td><td></td><td></td><td></td>`;
    tr.querySelector(".key").style.background = seriesColor(i);
    tr.firstChild.append(`Pin ${i + 1}`);
    rows.appendChild(tr);
  }
}

function renderNow() {
  const s = state.latest;
  if (!s) return;
  const L = state.limits;
  const d = s.derived;
  $("k-power").textContent = fmt(s.total_w, 1) + " W";
  overLimit($("k-power-sub"), s.total_w, L.total_w, "");
  $("k-current").textContent = fmt(s.total_a, 2) + " A";
  $("k-current-sub").textContent = `hottest pin ${fmt(d.max_pin_a, 2)} A`;
  $("k-voltage").textContent = fmt(s.avg_v, 3) + " V";
  const vs = s.pins.map((p) => p.v);
  $("k-voltage-sub").textContent = `pins ${fmt(Math.min(...vs), 3)}–${fmt(Math.max(...vs), 3)} V`;
  $("k-temp").textContent = d.max_temp_c == null ? "–" : fmt(d.max_temp_c, 1) + " °C";
  overLimit($("k-temp-sub"), d.max_temp_c, L.temp_c, "");
  $("k-imb").textContent = d.pin_imbalance == null ? "–" : "×" + fmt(d.pin_imbalance, 2);
  if (d.pin_imbalance == null) {
    $("k-imb-sub").classList.remove("over");
    $("k-imb-sub").textContent = `needs ≥ ${L.imbalance_min_load_a ?? 1} A load`;
  } else overLimit($("k-imb-sub"), d.pin_imbalance, L.imbalance, "");

  // Bars share one scale: the pin limit (or the largest pin if higher).
  const maxA = Math.max(L.pin_a || 0, ...s.pins.map((p) => p.a), 0.001) * 1.05;
  const rows = $("pin-bars").children;
  const trs = $("pin-rows").children;
  s.pins.forEach((p, i) => {
    const bar = rows[i].querySelector(".bar");
    bar.style.width = `${(p.a / maxA) * 100}%`;
    bar.classList.toggle("over", L.pin_a != null && p.a > L.pin_a);
    const lim = rows[i].querySelector(".limit");
    lim.hidden = L.pin_a == null;
    if (L.pin_a != null) lim.style.left = `${(L.pin_a / maxA) * 100}%`;
    rows[i].querySelector(".val").textContent = fmt(p.a, 3) + " A";
    rows[i].title = `Pin ${i + 1}: ${fmt(p.v, 3)} V · ${fmt(p.a, 3)} A · ${fmt(p.w, 2)} W`;
    const td = trs[i].children;
    td[1].textContent = fmt(p.v, 3) + " V";
    td[2].textContent = fmt(p.a, 3) + " A";
    td[3].textContent = fmt(p.w, 2) + " W";
  });
  $("pins-note").textContent = L.pin_a != null ? `dashed line = ${L.pin_a} A per-pin limit` : "";

  const badge = $("fault-badge");
  if (s.faults.length) {
    badge.className = "badge crit";
    badge.textContent = "Fault: " + s.faults.join(", ");
  } else if (s.fault_log) {
    badge.className = "badge warn";
    badge.textContent = "Fault logged";
  } else {
    badge.className = "badge ok";
    badge.textContent = "No faults";
  }
}

function renderInfo(info, health) {
  state.info = info;
  $("dev-name").textContent = info ? info.edition : "No device";
  $("dev-meta").textContent = info ? `${info.hw_rev} · ${info.build} · ${info.port}` : "";
  const dl = $("device-info");
  dl.textContent = "";
  const rows = info ? [["Edition", info.edition], ["UID", info.uid], ["Firmware", `v${info.fw_version} · ${info.build}`],
                       ["Port", info.port]] : [];
  if (health) rows.push(["Sampling", `${health.rate_hz} Hz (measured ${health.measured_hz})`],
                        ["Reads", `${health.counters.ok} ok · ${health.counters.corrupt} corrupt · ${health.counters.failed} failed`]);
  for (const [k, v] of rows) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    dl.append(dt, dd);
  }
}

// ---------------------------------------------------------------- events
const LIMIT_NAMES = { pin_a: ["Pin current", "A"], total_w: ["Total power", "W"], temp_c: ["Temperature", "°C"], imbalance: ["Pin imbalance", "×"] };

function describeEvent(e) {
  switch (e.type) {
    case "fault.set": case "fault.active": return ["crit", `${e.label} fault`];
    case "fault.clear": return ["ok", `${e.label} cleared`];
    case "fault.log": return ["warn", e.fault_log.length ? `Fault log: ${e.fault_log.join(", ")}` : "Fault log cleared"];
    case "limit.exceeded": { const [n, u] = LIMIT_NAMES[e.limit]; return ["crit", `${n} ${fmt(e.value)} ${u} over ${e.threshold} ${u}`]; }
    case "limit.normal": { const [n] = LIMIT_NAMES[e.limit]; return ["ok", `${n} back under limit`]; }
    case "device.connected": return ["ok", `Connected · ${e.port}`];
    case "device.disconnected": return ["crit", `Disconnected (${e.reason})`];
    case "command.clear_faults": return ["warn", "Faults cleared by command"];
    default: return ["", e.type];
  }
}

function addEvent(e, prepend = true) {
  const ol = $("events");
  if (ol.firstElementChild && ol.firstElementChild.classList.contains("muted")) ol.textContent = "";
  const [cls, text] = describeEvent(e);
  const li = document.createElement("li");
  const t = document.createElement("time");
  t.textContent = clock(e.ts);
  t.title = dateTime(e.ts);
  const span = document.createElement("span");
  span.className = cls;
  span.textContent = text;
  li.append(t, span);
  prepend ? ol.prepend(li) : ol.append(li);
  while (ol.children.length > 200) ol.lastChild.remove();
}

async function loadEvents() {
  const { events } = await api("/api/v1/events?last=24h&limit=200");
  $("events").innerHTML = '<li class="muted">No events</li>';
  events.reverse().forEach((e) => addEvent(e, false));
}

// ---------------------------------------------------------------- sessions
async function loadSessions() {
  const { sessions } = await api("/api/v1/sessions?limit=15");
  state.activeSession = sessions.find((s) => s.active) || null;
  renderSessionControl();
  const details = await Promise.all(sessions.map((s) => api(`/api/v1/sessions/${s.id}`).catch(() => s)));
  const tb = $("sessions");
  tb.textContent = "";
  for (const s of details) {
    const st = s.stats;
    const tr = document.createElement("tr");
    const cells = [
      s.label,
      s.active ? "running" : `${fmt(s.end_ts - s.start_ts, 1)} s`,
      st && st.fields.total_w ? fmt(st.fields.total_w.max, 1) : "–",
      st && st.fields.max_pin_a ? fmt(st.fields.max_pin_a.max, 2) : "–",
      st ? (st.faults_seen.join(", ") || "none") : "–",
    ];
    for (const c of cells) { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); }
    tr.title = `#${s.id} ${s.label} · started ${dateTime(s.start_ts)}`;
    const td = document.createElement("td");
    const a = document.createElement("a");
    a.href = withToken(`/api/v1/sessions/${s.id}/export?format=csv`);
    a.textContent = "CSV";
    td.appendChild(a);
    tr.appendChild(td);
    tb.appendChild(tr);
  }
  $("sess-note").textContent = sessions.length ? "" : "none yet";
}

function renderSessionControl() {
  const s = state.activeSession;
  $("session-form").classList.toggle("active", !!s);
  $("session-btn").textContent = s ? "Stop session" : "Start session";
  $("session-label").disabled = !!s;
  $("session-label").value = s ? s.label : "";
}

$("session-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    if (state.activeSession) {
      await api(`/api/v1/sessions/${state.activeSession.id}/stop`, { method: "POST" });
    } else {
      const label = $("session-label").value.trim() || `dashboard ${clock(Date.now() / 1000)}`;
      await api("/api/v1/sessions", { method: "POST", body: JSON.stringify({ label, meta: { source: "dashboard" } }) });
    }
  } catch (err) { alert(`Session: ${err.message}`); }
  loadSessions();
});

// ---------------------------------------------------------------- stream
function connectStream() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(withToken(`${proto}//${location.host}/api/v1/stream`));
  state.ws = ws;
  ws.onmessage = async (m) => {
    const msg = JSON.parse(m.data);
    if (msg.kind === "hello") {
      state.wsRetry = 1000;
      state.limits = { ...state.limits, ...msg.data.limits };
      // Backfill whatever arrived while we were disconnected.
      if (state.lastSeq && msg.data.last_seq > state.lastSeq) {
        const { samples } = await api(`/api/v1/sensors/history?after_seq=${state.lastSeq}`);
        samples.forEach(pushSample);
      }
      setStatus(msg.data.connected ? "live" : "down", msg.data.connected ? "Live" : "Device disconnected");
    } else if (msg.kind === "sample") {
      pushSample(msg.data);
      setStatus("live", "Live");
    } else if (msg.kind === "event") {
      addEvent(msg.data);
      if (msg.data.type === "device.connected" || msg.data.type === "device.disconnected") {
        setStatus(msg.data.type === "device.connected" ? "live" : "down",
                  msg.data.type === "device.connected" ? "Live" : "Device disconnected");
        refreshInfo();
      }
    } else if (msg.kind === "session") {
      loadSessions();
    }
  };
  ws.onclose = (e) => {
    if (e.code === 4401) { showAuth(); return; }
    setStatus("down", "Reconnecting…");
    setTimeout(connectStream, state.wsRetry);
    state.wsRetry = Math.min(state.wsRetry * 2, 15000);
  };
}

async function refreshInfo() {
  const health = await api("/api/v1/health");
  const info = await api("/api/v1/info").catch(() => null);
  renderInfo(info, health);
  if (!health.connected) setStatus("down", "Device disconnected");
}

// ---------------------------------------------------------------- controls
document.querySelectorAll(".range-row button").forEach((btn) => {
  btn.classList.toggle("on", Number(btn.dataset.range) === state.range);
  btn.addEventListener("click", async () => {
    state.range = Number(btn.dataset.range);
    store.set("wvd.range", String(state.range));
    document.querySelectorAll(".range-row button").forEach((b) => b.classList.toggle("on", b === btn));
    await loadHistory();
  });
});
$("pause").addEventListener("change", (e) => { state.paused = e.target.checked; });

$("theme-btn").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  store.set("wvd.theme", root.dataset.theme);
  buildPinRows(); buildCharts();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (!document.documentElement.dataset.theme) { buildPinRows(); buildCharts(); }
});

setInterval(() => {
  if (!state.dirty) return;
  state.dirty = false;
  trim();
  renderNow();
  if (!state.paused) renderCharts();
}, RENDER_MS);
setInterval(() => { refreshInfo().catch(() => {}); }, 10000);

// ---------------------------------------------------------------- boot
async function start() {
  try {
    const health = await api("/api/v1/health");
    state.limits = await api("/api/v1/limits");
    if (state.token) $("metrics-link").href = withToken("metrics");
    const info = await api("/api/v1/info").catch(() => null);
    renderInfo(info, health);
    setStatus(health.connected ? "live" : "down", health.connected ? "Live" : "Device disconnected");
    await loadHistory();
    buildCharts();
    await Promise.all([loadEvents(), loadSessions()]);
    connectStream();
  } catch (err) {
    if (err.message !== "unauthorized") {
      setStatus("down", "wvd unreachable");
      setTimeout(start, 3000);
    }
  }
}

const savedTheme = store.get("wvd.theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
buildPinRows();
start();
