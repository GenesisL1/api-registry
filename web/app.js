/* GenesisL1 Public API Registry — directory application.
 *
 * Reads registry records from the chain and quality measurements from signed
 * checker feeds. Nothing is rendered from a feed whose signature does not
 * recover to an address the registry currently authorizes.
 *
 * No build step, no framework, no third-party runtime: ethers, the fonts and
 * the contract ABI are all served from this origin.
 */
"use strict";

const CFG = window.GENESIS_API_CONFIG;
const ABI = window.GENESIS_ABI;

const TYPES = [
  ["EVM RPC", "HTTPS JSON-RPC"],
  ["EVM WebSocket", "WSS"],
  ["Cosmos LCD", "REST, port 1317"],
  ["CometBFT RPC", "port 26657"],
  ["Cosmos gRPC", "port 9090"],
  ["Cosmos gRPC-Web", "HTTPS"],
  ["Indexer API", "HTTPS"],
  ["Snapshot service", "HTTPS"],
  ["Other public API", "custom"]
];

const KIND_EVM_HTTP = 0;
const STALE_FEED_SECONDS = 30 * 60;

/* ----------------------------------------------------------------- state */

const state = {
  provider: null,
  registry: null,
  lens: null,
  signer: null,
  account: null,
  summary: null,
  instances: [],
  endpoints: [],
  aggregates: new Map(),   // endpointId -> on-chain DayAggregate
  verifications: new Map(),// endpointId -> attestation count
  inactive: new Map(),     // instanceId -> [endpoint] the owner could restore
  feeds: [],
  feedProblems: [],
  fee: 0n,
  threshold: 9900,
  loading: false
};

/* ---------------------------------------------------------------- helpers */

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const shortAddress = (value) => (value ? `${value.slice(0, 6)}…${value.slice(-4)}` : "—");

const asNumber = (value) => (value === null || value === undefined ? null : Number(value));

function stamp(seconds) {
  const value = Number(seconds);
  if (!value) return "—";
  return new Date(value * 1000).toISOString().replace("T", " ").slice(0, 16) + "Z";
}

function day(seconds) {
  const value = Number(seconds);
  if (!value) return "—";
  return new Date(value * 1000).toISOString().slice(0, 10);
}

function ago(seconds) {
  const value = Number(seconds);
  if (!value) return "never";
  const delta = Math.max(0, Math.floor(Date.now() / 1000) - value);
  if (delta < 90) return `${delta}s ago`;
  if (delta < 5400) return `${Math.round(delta / 60)}m ago`;
  if (delta < 172800) return `${Math.round(delta / 3600)}h ago`;
  return `${Math.round(delta / 86400)}d ago`;
}

const percent = (bps) => (bps === null || bps === undefined ? "—" : `${(Number(bps) / 100).toFixed(2)}%`);
const millis = (value) => (value === null || value === undefined ? "—" : `${Math.round(Number(value))} ms`);
const count = (value) => (value === null || value === undefined ? "—" : Number(value).toLocaleString("en-US"));

function median(values) {
  const list = values.filter((value) => value !== null && value !== undefined).map(Number).sort((a, b) => a - b);
  if (!list.length) return null;
  const middle = Math.floor(list.length / 2);
  return list.length % 2 ? list[middle] : Math.round((list[middle - 1] + list[middle]) / 2);
}

let toastTimer = null;
function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), 4600);
}

function notice(message) {
  const element = $("#notice");
  if (!message) { element.classList.add("hidden"); return; }
  element.innerHTML = message;
  element.classList.remove("hidden");
}

/* Errors should say what happened, in the interface's voice. */
function explain(error) {
  const raw = error?.shortMessage || error?.reason || error?.message || String(error);
  if (/user rejected|ACTION_REJECTED/i.test(raw)) return "Transaction cancelled in your wallet.";
  if (/IncorrectFee/.test(raw)) return "The fee sent did not match the registry fee. Refresh and try again.";
  if (/LimitReached/.test(raw)) return "That would exceed a registry limit: four instances per address, 24 endpoints per instance.";
  if (/InvalidInput/.test(raw)) return "The registry rejected that input. Check the URL scheme and length.";
  if (/Unauthorized/.test(raw)) return "That action is only available to the record's owner.";
  if (/NotFound/.test(raw)) return "That record is no longer active.";
  if (/insufficient funds/i.test(raw)) return "Not enough L1 coin in this account to cover the fee and gas.";
  return raw.length > 180 ? raw.slice(0, 180) + "…" : raw;
}

/* ------------------------------------------------------- modal management */

let lastFocused = null;

function openModal(id) {
  lastFocused = document.activeElement;
  const modal = $(id);
  modal.classList.add("open");
  modal.setAttribute("aria-hidden", "false");
  const focusable = modal.querySelector("input, select, textarea, button:not(.close)");
  if (focusable) focusable.focus();
}

function closeModal(id) {
  const modal = typeof id === "string" ? $(id) : id;
  modal.classList.remove("open");
  modal.setAttribute("aria-hidden", "true");
  if (lastFocused) lastFocused.focus();
}

document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const open = $(".modal.open");
  if (open) closeModal(open);
});

/* Keep tab focus inside an open dialog. */
document.addEventListener("keydown", (event) => {
  if (event.key !== "Tab") return;
  const modal = $(".modal.open");
  if (!modal) return;
  const items = Array.from(modal.querySelectorAll("a[href], button, input, select, textarea"))
    .filter((element) => !element.disabled && element.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
});

/* -------------------------------------------------------- chain plumbing */

/* Try each RPC in turn. A directory of API endpoints that dies with one API
 * would be a poor advertisement for itself. */
async function connectProvider(urls) {
  const errors = [];
  for (const url of urls) {
    try {
      const provider = new ethers.JsonRpcProvider(url, CFG.chainId, { staticNetwork: true });
      const network = await withTimeout(provider.getNetwork(), 6000);
      if (Number(network.chainId) !== Number(CFG.chainId)) {
        errors.push(`${url}: reports chain ${network.chainId}`);
        continue;
      }
      return provider;
    } catch (error) {
      errors.push(`${url}: ${explain(error)}`);
    }
  }
  throw new Error(`No usable RPC endpoint. Tried ${urls.length}: ${errors.join("; ")}`);
}

function withTimeout(promise, ms) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error("timed out")), ms))
  ]);
}

/* Once the register has healthy EVM endpoints of its own, prefer them. The
 * directory ends up bootstrapping from the infrastructure it lists. */
function rpcCandidates() {
  const configured = Array.isArray(CFG.rpcUrls) ? CFG.rpcUrls.slice() : [];
  const discovered = state.endpoints
    .filter((endpoint) => Number(endpoint.kind) === KIND_EVM_HTTP && !endpoint.unreliable)
    .filter((endpoint) => stateOf(endpoint, metricsFor(endpoint.id)) === "ok")
    .filter((endpoint) => endpoint.url.startsWith("https://"))
    .map((endpoint) => endpoint.url);
  return Array.from(new Set(configured.concat(discovered)));
}

function addressesConfigured() {
  return ethers.isAddress(CFG.registryAddress) && CFG.registryAddress !== ethers.ZeroAddress
    && ethers.isAddress(CFG.lensAddress) && CFG.lensAddress !== ethers.ZeroAddress;
}

/* ------------------------------------------------------------- telemetry */

/* A feed is data from a stranger. It counts only if its detached signature
 * recovers to a checker the registry currently authorizes, it describes this
 * registry, and it is recent. */
async function loadTelemetry(authorized) {
  const feeds = [];
  const problems = [];

  await Promise.all((CFG.telemetryUrls || []).map(async (url) => {
    let raw;
    try {
      const response = await fetch(bust(url), { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      raw = await response.text();
    } catch (error) {
      problems.push(`${url} is unreachable (${explain(error)})`);
      return;
    }

    let data;
    try { data = JSON.parse(raw); } catch { problems.push(`${url} is not valid JSON`); return; }
    if (!Array.isArray(data.endpoints)) { problems.push(`${url} has no endpoint list`); return; }

    if (data.registry && CFG.registryAddress
        && data.registry.toLowerCase() !== CFG.registryAddress.toLowerCase()) {
      problems.push(`${url} describes a different registry`);
      return;
    }
    if (data.chain_id && Number(data.chain_id) !== Number(CFG.chainId)) {
      problems.push(`${url} describes chain ${data.chain_id}`);
      return;
    }

    let signer = null;
    try {
      const response = await fetch(bust(url + ".sig"), { cache: "no-store" });
      if (response.ok) {
        const document_ = await response.json();
        signer = ethers.verifyMessage(raw, document_.signature);
      }
    } catch (error) {
      signer = null;
    }

    if (CFG.requireSignedTelemetry !== false) {
      if (!signer) { problems.push(`${url} is unsigned and was discarded`); return; }
      if (!authorized.has(signer.toLowerCase())) {
        problems.push(`${url} is signed by ${shortAddress(signer)}, which the registry does not authorize`);
        return;
      }
    }

    const age = Math.floor(Date.now() / 1000) - Number(data.generated_at || 0);
    if (age > STALE_FEED_SECONDS) {
      problems.push(`${url} was last written ${ago(data.generated_at)} and was discarded as stale`);
      return;
    }

    feeds.push({ url, data, signer, byId: indexById(data.endpoints) });
  }));

  state.feeds = feeds;
  state.feedProblems = problems;
}

const bust = (url) => url + (url.includes("?") ? "&" : "?") + "t=" + Date.now();

function indexById(list) {
  const map = new Map();
  for (const entry of list) map.set(String(entry.id), entry);
  return map;
}

/* Combine feeds by median rather than mean: one checker with a bad network
 * path should not drag a shared verdict with it. */
function metricsFor(endpointId) {
  const rows = [];
  for (const feed of state.feeds) {
    const entry = feed.byId.get(String(endpointId));
    const window_ = entry?.stats_24h;
    if (window_ && window_.counted) rows.push({ feed, entry, window: window_ });
  }
  if (!rows.length) return null;

  const uptimes = rows.map((row) => row.window.uptime_bps);
  return {
    feedCount: rows.length,
    uptime_bps: median(uptimes),
    latency_p95_ms: median(rows.map((row) => row.window.latency_p95_ms)),
    latency_p50_ms: median(rows.map((row) => row.window.latency_p50_ms)),
    samples: rows.reduce((total, row) => total + Number(row.window.counted || 0), 0),
    throttled: rows.reduce((total, row) => total + Number(row.window.throttled || 0), 0),
    last_checked: Math.max(...rows.map((row) => Number(row.window.last_checked || 0))),
    last_error: rows.map((row) => row.window.last_error).find(Boolean) || "",
    spread: uptimes.length > 1 ? Math.max(...uptimes) - Math.min(...uptimes) : 0,
    perFeed: rows.map((row) => ({
      name: row.feed.data?.checker?.name || shortAddress(row.feed.signer),
      signer: row.feed.signer,
      uptime_bps: row.window.uptime_bps
    })),
    buckets: mergeBuckets(rows.map((row) => row.entry.buckets_24h).filter(Array.isArray))
  };
}

function mergeBuckets(series) {
  if (!series.length) return [];
  const length = Math.max(...series.map((list) => list.length));
  const merged = [];
  for (let index = 0; index < length; index += 1) {
    const cells = series.map((list) => list[index]).filter(Boolean);
    const scored = cells.filter((cell) => cell.uptime_bps !== null && cell.uptime_bps !== undefined);
    merged.push({
      t: cells[0]?.t ?? null,
      samples: cells.reduce((total, cell) => total + Number(cell.samples || 0), 0),
      uptime_bps: scored.length ? median(scored.map((cell) => cell.uptime_bps)) : null,
      latency_p95_ms: median(cells.map((cell) => cell.latency_p95_ms))
    });
  }
  return merged;
}

function stateOf(endpoint, metrics) {
  if (endpoint.unreliable) return "down";
  if (!metrics || metrics.uptime_bps === null) return "idle";
  return metrics.uptime_bps >= state.threshold ? "ok" : "warn";
}

const STATE_LABEL = {
  ok: "At target",
  warn: "Below target",
  down: "Flagged unreliable",
  idle: "Awaiting measurement"
};

/* ------------------------------------------------------------- the trace */

function traceCells(buckets) {
  const cells = [];
  for (let index = 0; index < 24; index += 1) {
    const cell = buckets && buckets[index];
    const uptime = cell && cell.uptime_bps !== null && cell.uptime_bps !== undefined ? Number(cell.uptime_bps) : null;
    const samples = Number(cell?.samples || 0);
    let kind = "idle";
    if (uptime !== null) kind = uptime >= state.threshold ? "ok" : uptime >= 9000 ? "warn" : "down";
    /* Height carries coverage, colour carries availability, so a thin bar
     * reads as "little evidence" rather than "bad". */
    const height = uptime === null ? 22 : Math.max(30, Math.min(100, 30 + Math.log10(1 + samples) * 34));
    const hour = cell?.t ? new Date(cell.t * 1000).toISOString().slice(11, 16) + "Z" : "no data";
    const title = uptime === null
      ? `${hour} · no measurements`
      : `${hour} · ${percent(uptime)} · ${millis(cell.latency_p95_ms)} p95 · ${count(samples)} probes`;
    cells.push(`<span class="trace-cell" data-state="${kind}" style="height:${height}%" title="${esc(title)}"></span>`);
  }
  return cells.join("");
}

const traceHtml = (buckets, extraClass) =>
  `<div class="trace ${extraClass || ""}">${traceCells(buckets)}</div>`;

function networkTrace() {
  const series = [];
  for (const endpoint of state.endpoints) {
    const metrics = metricsFor(endpoint.id);
    if (metrics?.buckets?.length) series.push(metrics.buckets);
  }
  return mergeBuckets(series);
}

/* ------------------------------------------------------------- rendering */

function render() {
  const query = $("#search").value.trim().toLowerCase();
  const typeFilter = $("#typeFilter").value;
  const healthFilter = $("#healthFilter").value;
  const sortBy = $("#sortBy").value;

  renderReadout();

  const cards = [];
  const ranked = state.instances.slice();

  const scoreOf = (instance) => {
    const own = state.endpoints.filter((endpoint) => String(endpoint.instanceId) === String(instance.id));
    const metrics = own.map((endpoint) => metricsFor(endpoint.id)).filter(Boolean);
    if (!metrics.length) return { uptime: -1, latency: Number.MAX_SAFE_INTEGER };
    return {
      uptime: median(metrics.map((m) => m.uptime_bps)) ?? -1,
      latency: median(metrics.map((m) => m.latency_p95_ms)) ?? Number.MAX_SAFE_INTEGER
    };
  };

  ranked.sort((a, b) => {
    if (sortBy === "name") return String(a.name).localeCompare(String(b.name));
    if (sortBy === "newest") return Number(b.addedAt) - Number(a.addedAt);
    const left = scoreOf(a);
    const right = scoreOf(b);
    if (sortBy === "latency") return left.latency - right.latency;
    return right.uptime - left.uptime;
  });

  let shown = 0;
  for (const instance of ranked) {
    let own = state.endpoints.filter((endpoint) => String(endpoint.instanceId) === String(instance.id));
    const haystack = [instance.name, instance.region, instance.owner, instance.operatorInfo]
      .concat(own.map((endpoint) => endpoint.url)).join(" ").toLowerCase();
    if (query && !haystack.includes(query)) continue;
    if (typeFilter) own = own.filter((endpoint) => String(endpoint.kind) === typeFilter);
    if (healthFilter) own = own.filter((endpoint) => stateOf(endpoint, metricsFor(endpoint.id)) === healthFilter);
    if ((query || typeFilter || healthFilter) && !own.length) continue;

    own.sort((a, b) => Number(a.kind) - Number(b.kind) || Number(a.id) - Number(b.id));
    shown += 1;
    cards.push(instanceHtml(instance, own, shown));
  }

  const container = $("#register");
  if (!cards.length) {
    container.innerHTML = state.instances.length
      ? `<div class="empty"><strong>Nothing matches those filters</strong>Clear the search or choose a different state.</div>`
      : `<div class="empty"><strong>The register is empty</strong>No infrastructure has been registered yet. Yours would be the first.</div>`;
    return;
  }
  container.innerHTML = cards.join("");
  $$("#register .instance").forEach((element, index) => {
    element.style.animationDelay = `${Math.min(index * 45, 400)}ms`;
  });
}

function renderReadout() {
  const healthy = state.endpoints.filter((endpoint) => stateOf(endpoint, metricsFor(endpoint.id)) === "ok").length;
  $("#countInstances").textContent = state.instances.length;
  $("#countEndpoints").textContent = state.endpoints.length;
  $("#countHealthy").textContent = healthy;
  $("#countFeeds").textContent = state.feeds.length;
  $("#networkTrace").innerHTML = traceCells(networkTrace());

  const newest = Math.max(0, ...state.feeds.map((feed) => Number(feed.data.generated_at || 0)));
  $("#readoutStamp").textContent = state.feeds.length ? `measured ${ago(newest)}` : "no verified feed";

  const parts = [];
  if (state.summary) {
    parts.push(`target ${percent(state.threshold)}`);
    parts.push(`quorum ${state.summary.adverseReportQuorum}`);
    parts.push(`fee ${ethers.formatEther(state.fee)} L1`);
  }
  $("#footMeta").textContent = parts.join(" · ") || "—";
}

function instanceHtml(instance, endpoints, index) {
  const mine = state.account && String(instance.owner).toLowerCase() === state.account.toLowerCase();
  const allVerified = endpoints.length > 0 && endpoints.every((endpoint) => endpoint.verified);
  const badges = [];
  if (allVerified) badges.push(`<span class="badge verified" title="Every endpoint here has proven control of its host to a quorum of checkers">✓ control verified</span>`);
  if (mine) badges.push(`<span class="badge mine">yours</span>`);

  const link = /^https?:\/\//.test(instance.operatorInfo || "")
    ? `<a href="${esc(instance.operatorInfo)}" rel="noopener noreferrer nofollow" target="_blank">${esc(instance.operatorInfo)}</a>`
    : esc(instance.operatorInfo || "");

  const restorable = state.inactive.get(String(instance.id)) || [];
  const owner = mine
    ? `<div class="owner-actions">
         <button class="btn mini" data-act="add" data-id="${instance.id}">Add endpoints</button>
         <button class="btn mini" data-act="instance" data-id="${instance.id}">Edit details</button>
         <button class="btn mini danger" data-act="delInstance" data-id="${instance.id}">Delete instance</button>
       </div>`
    : "";

  const restoreRow = mine && restorable.length
    ? `<div class="endpoint"><div class="state idle"><span class="lamp"></span>Removed</div>
        <div><div class="endpoint-note">${restorable.length} endpoint${restorable.length > 1 ? "s" : ""} previously removed from this instance.</div>
        <div class="endpoint-actions">${restorable.map((endpoint) =>
            `<button class="btn mini" data-act="restore" data-id="${endpoint.id}">Restore ${esc(TYPES[Number(endpoint.kind)]?.[0] || "endpoint")} · ${esc(endpoint.url)}</button>`
          ).join("")}</div></div><div></div></div>`
    : "";

  return `<article class="instance">
    <div class="instance-head">
      <div class="instance-index">№ ${String(index).padStart(2, "0")}</div>
      <div class="instance-title">
        <h3>${esc(instance.name)}</h3>
        <div class="instance-meta">
          ${esc(instance.region || "region not stated")} ·
          operator ${esc(shortAddress(instance.owner))} ·
          registered ${day(instance.addedAt)}
          ${Number(instance.updatedAt) > Number(instance.addedAt) ? ` · updated ${day(instance.updatedAt)}` : ""}
          ${link ? `<br>${link}` : ""}
        </div>
        ${badges.length ? `<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap">${badges.join("")}</div>` : ""}
      </div>
      ${owner}
    </div>
    ${endpoints.map((endpoint) => endpointHtml(endpoint, mine)).join("") || `<div class="endpoint"><div class="state idle"><span class="lamp"></span>No endpoints</div><div></div><div></div></div>`}
    ${restoreRow}
  </article>`;
}

function endpointHtml(endpoint, mine) {
  const metrics = metricsFor(endpoint.id);
  const kind = stateOf(endpoint, metrics);
  const type = TYPES[Number(endpoint.kind)] || ["Unknown API", ""];
  const aggregate = state.aggregates.get(String(endpoint.id));
  const attested = Number(state.verifications.get(String(endpoint.id)) || 0);

  const notes = [];
  if (endpoint.unreliable) {
    notes.push(`<span class="flag">Flagged by checker quorum. Cleared when a quorum measures it back at target.</span>`);
  }
  if (Number(endpoint.adverseDayCount) > 0) {
    notes.push(`<span>${endpoint.adverseDayCount} flagged day${Number(endpoint.adverseDayCount) > 1 ? "s" : ""} on record</span>`);
  }
  if (aggregate && Number(aggregate.reportCount) > 0) {
    notes.push(`<span class="chain">${aggregate.reportCount} checker${Number(aggregate.reportCount) > 1 ? "s" : ""} attested ${percent(aggregate.uptimeBpsAverage)} on ${day(Number(aggregate.day) * 86400)}</span>`);
  }
  if (endpoint.verified) {
    notes.push(`<span title="${attested} checkers confirmed the operator controls this host">host control verified</span>`);
  }
  if (metrics?.throttled) {
    notes.push(`<span>${count(metrics.throttled)} probes rate limited, excluded from uptime</span>`);
  }
  if (kind !== "ok" && metrics?.last_error) {
    notes.push(`<span>last error: ${esc(metrics.last_error)}</span>`);
  }
  notes.push(`<span>epoch ${endpoint.epoch} · added ${day(endpoint.addedAt)}</span>`);

  const consensus = metrics && metrics.feedCount
    ? `<div class="consensus ${metrics.spread > 200 ? "split" : ""}" title="${esc(metrics.perFeed.map((f) => `${f.name}: ${percent(f.uptime_bps)}`).join(" · "))}">
         ${metrics.perFeed.map((f) => `<span class="dot ${f.uptime_bps >= state.threshold ? "ok" : f.uptime_bps >= 9000 ? "warn" : "down"}"></span>`).join("")}
         <small>${metrics.feedCount} checker${metrics.feedCount > 1 ? "s" : ""}${metrics.spread > 200 ? " · disagree" : ""}</small>
       </div>`
    : `<div class="consensus"><small>no verified feed</small></div>`;

  const actions = [];
  if (mine) {
    actions.push(`<button class="btn mini" data-act="edit" data-id="${endpoint.id}">Edit</button>`);
    if (endpoint.unreliable) actions.push(`<button class="btn mini" data-act="contest" data-id="${endpoint.id}">Contest flag</button>`);
    actions.push(`<button class="btn mini danger" data-act="delEndpoint" data-id="${endpoint.id}">Remove</button>`);
  }

  return `<div class="endpoint ${endpoint.unreliable ? "flagged" : ""}">
    <div>
      <div class="state ${kind}"><span class="lamp"></span>${STATE_LABEL[kind]}</div>
      <div class="kind">${esc(type[0])}</div>
      <div class="transport">${esc(type[1])}</div>
    </div>
    <div>
      <div class="url-line">
        <div class="url" title="${esc(endpoint.url)}">${esc(endpoint.url)}</div>
        <button class="copy" type="button" data-act="copy" data-id="${endpoint.id}">Copy</button>
      </div>
      <div class="endpoint-trace">${traceHtml(metrics?.buckets)}</div>
      <div class="endpoint-note">${notes.join("")}</div>
      ${actions.length ? `<div class="endpoint-actions">${actions.join("")}</div>` : ""}
    </div>
    <div class="quality">
      <div><b>${metrics ? percent(metrics.uptime_bps) : "—"}</b><span>24h uptime</span></div>
      <div><b>${metrics ? millis(metrics.latency_p95_ms) : "—"}</b><span>p95 latency</span></div>
      <div><b>${metrics ? count(metrics.samples) : "—"}</b><span>probes</span></div>
      <div><b>${metrics ? ago(metrics.last_checked) : "—"}</b><span>last checked</span></div>
      <div style="grid-column:1/-1">${consensus}</div>
    </div>
  </div>`;
}

/* ------------------------------------------------------------ chain reads */

async function load() {
  if (state.loading) return;
  if (!addressesConfigured()) {
    notice(`Set <code>registryAddress</code> and <code>lensAddress</code> in <code>web/config.js</code>. Everything else is ready.`);
    $("#register").innerHTML = `<div class="empty"><strong>Not pointed at a deployment yet</strong>Add your contract addresses to config.js.</div>`;
    return;
  }
  state.loading = true;
  try {
    state.provider = await connectProvider(rpcCandidates());
    state.registry = new ethers.Contract(CFG.registryAddress, ABI.registry, state.provider);
    state.lens = new ethers.Contract(CFG.lensAddress, ABI.lens, state.provider);

    const summary = await state.lens.getSummary();
    state.summary = summary;
    state.fee = summary.endpointWriteFee;
    /* The threshold comes from the contract, never from config.js: the page
     * must not disagree with the rule the chain actually applies. */
    state.threshold = Number(summary.reliabilityThresholdBps);

    const checkers = await state.lens.getCheckers();
    const authorized = new Set(
      checkers.filter((checker) => checker.authorized).map((checker) => checker.addr.toLowerCase())
    );

    const instances = [];
    const endpoints = [];
    state.aggregates.clear();
    state.verifications.clear();

    let cursor = 0n;
    let guard = 0;
    for (;;) {
      const page = await state.lens.getDirectory(cursor, CFG.pageSize || 25);
      let seen = 0;
      page.instances.forEach((instance, index) => {
        instances.push(normalizeInstance(instance));
        const size = Number(page.endpointCounts[index]);
        for (let step = 0; step < size; step += 1) {
          const endpoint = page.endpoints[seen];
          endpoints.push(normalizeEndpoint(endpoint));
          state.aggregates.set(String(endpoint.id), page.aggregates[seen]);
          state.verifications.set(String(endpoint.id), Number(page.verifications[seen]));
          seen += 1;
        }
      });
      if (page.nextCursor <= cursor || page.instances.length === 0) break;
      cursor = page.nextCursor;
      if ((guard += 1) > 200) break;
    }

    state.instances = instances;
    state.endpoints = endpoints;

    await loadTelemetry(authorized);
    await loadRestorable();

    const problems = state.feedProblems.slice();
    if (!state.feeds.length && (CFG.telemetryUrls || []).length) {
      problems.unshift("No verified telemetry feed. Uptime and latency are unavailable until a checker publishes a signed snapshot.");
    }
    notice(problems.length ? problems.map(esc).join("<br>") : "");
    render();
  } catch (error) {
    console.error(error);
    notice(`Could not read the register: ${esc(explain(error))}`);
    $("#register").innerHTML = `<div class="empty"><strong>The register could not be read</strong>${esc(explain(error))}</div>`;
  } finally {
    state.loading = false;
  }
}

function normalizeInstance(instance) {
  return {
    id: Number(instance.id),
    owner: instance.owner,
    addedAt: Number(instance.addedAt),
    updatedAt: Number(instance.updatedAt),
    endpointCount: Number(instance.endpointCount),
    name: instance.name,
    region: instance.region,
    operatorInfo: instance.operatorInfo
  };
}

function normalizeEndpoint(endpoint) {
  return {
    id: Number(endpoint.id),
    instanceId: Number(endpoint.instanceId),
    addedAt: Number(endpoint.addedAt),
    updatedAt: Number(endpoint.updatedAt),
    epoch: Number(endpoint.epoch),
    adverseDayCount: Number(endpoint.adverseDayCount),
    kind: Number(endpoint.kind),
    unreliable: Boolean(endpoint.unreliable),
    verified: Boolean(endpoint.verified),
    url: endpoint.url
  };
}

/* Removed endpoints leave the directory, so an owner would otherwise have no
 * way to bring one back without a block explorer. */
async function loadRestorable() {
  state.inactive.clear();
  if (!state.account) return;
  const mine = state.instances.filter((instance) => instance.owner.toLowerCase() === state.account.toLowerCase());
  for (const instance of mine) {
    try {
      const ids = await state.registry.getInstanceEndpointIds(instance.id);
      const active = new Set(
        state.endpoints.filter((endpoint) => endpoint.instanceId === instance.id).map((endpoint) => endpoint.id)
      );
      const missing = ids.map(Number).filter((id) => !active.has(id));
      if (!missing.length) continue;
      const records = await Promise.all(missing.map((id) => state.registry.getEndpoint(id)));
      state.inactive.set(String(instance.id), records.map(normalizeEndpoint));
    } catch (error) {
      console.warn("could not list removed endpoints", error);
    }
  }
}

/* ----------------------------------------------------------------- wallet */

async function connect() {
  if (!window.ethereum) throw new Error("No EVM wallet found in this browser.");
  await window.ethereum.request({ method: "eth_requestAccounts" });
  try {
    await window.ethereum.request({
      method: "wallet_switchEthereumChain",
      params: [{ chainId: CFG.chainHex }]
    });
  } catch (error) {
    if (error.code !== 4902) throw error;
    await window.ethereum.request({
      method: "wallet_addEthereumChain",
      params: [{
        chainId: CFG.chainHex,
        chainName: CFG.chainName,
        nativeCurrency: CFG.nativeCurrency,
        rpcUrls: CFG.rpcUrls,
        blockExplorerUrls: CFG.blockExplorerUrls
      }]
    });
  }
  const browserProvider = new ethers.BrowserProvider(window.ethereum);
  state.signer = await browserProvider.getSigner();
  state.account = await state.signer.getAddress();
  $("#connectBtn").textContent = shortAddress(state.account);
  $("#connectBtn").title = state.account;
  await loadRestorable();
  render();
}

async function writable() {
  if (!state.signer) await connect();
  return new ethers.Contract(CFG.registryAddress, ABI.registry, state.signer);
}

/* Every write funnels through here so submission, confirmation, error
 * translation and reload behave identically everywhere. */
async function submit(label, build) {
  try {
    const contract = await writable();
    const tx = await build(contract);
    toast(`${label} submitted. Waiting for confirmation…`);
    const receipt = await tx.wait();
    if (receipt.status !== 1) throw new Error("The transaction was mined but reverted.");
    toast(`${label} confirmed.`);
    await load();
    return true;
  } catch (error) {
    console.error(error);
    toast(explain(error));
    return false;
  }
}

/* ------------------------------------------------------------ form pieces */

function endpointRow(container, feeLabel) {
  const row = document.createElement("div");
  row.className = "endpoint-row";
  row.innerHTML = `
    <label>API type<select class="row-kind">${TYPES.map((type, index) =>
      `<option value="${index}">${esc(type[0])}</option>`).join("")}</select></label>
    <label>Endpoint URL<input class="row-url" maxlength="256" required placeholder="https://rpc.example.org"></label>
    <button type="button" class="btn mini danger row-remove">Remove</button>`;
  row.querySelector(".row-remove").onclick = () => { row.remove(); updateFee(container, feeLabel); };
  $(container).append(row);
  updateFee(container, feeLabel);
  return row;
}

function updateFee(container, feeLabel) {
  const rows = $$(`${container} .endpoint-row`).length;
  $(feeLabel).textContent = `${ethers.formatEther(state.fee * BigInt(rows))} L1 coin`;
}

function collectRows(container) {
  const rows = $$(`${container} .endpoint-row`);
  const kinds = rows.map((row) => Number(row.querySelector(".row-kind").value));
  const urls = rows.map((row) => row.querySelector(".row-url").value.trim());
  if (!rows.length) throw new Error("Add at least one endpoint.");
  if (urls.some((url) => !url)) throw new Error("Every row needs a URL.");
  const bad = urls.find((url) => !/^(https?|wss?|grpcs?):\/\/[^\s"'<>\\`@]+$/.test(url));
  if (bad) throw new Error(`That URL will be rejected by the registry: ${bad}`);
  return { kinds, urls };
}

/* --------------------------------------------------------------- actions */

const findEndpoint = (id) => state.endpoints.find((endpoint) => String(endpoint.id) === String(id))
  || (Array.from(state.inactive.values()).flat().find((endpoint) => String(endpoint.id) === String(id)));

const ACTIONS = {
  async copy(id, button) {
    const endpoint = findEndpoint(id);
    if (!endpoint) return;
    try {
      await navigator.clipboard.writeText(endpoint.url);
    } catch {
      const field = document.createElement("textarea");
      field.value = endpoint.url;
      document.body.append(field);
      field.select();
      document.execCommand("copy");
      field.remove();
    }
    button.textContent = "Copied";
    button.classList.add("done");
    setTimeout(() => { button.textContent = "Copy"; button.classList.remove("done"); }, 1800);
  },

  edit(id) {
    const endpoint = findEndpoint(id);
    if (!endpoint) return;
    $("#editId").value = id;
    $("#editKind").value = String(endpoint.kind);
    $("#editUrl").value = endpoint.url;
    $("#editFee").textContent = `${ethers.formatEther(state.fee)} L1 coin`;
    openModal("#editModal");
  },

  add(id) {
    $("#addInstanceId").value = id;
    $("#addRows").innerHTML = "";
    endpointRow("#addRows", "#addFee");
    openModal("#addModal");
  },

  instance(id) {
    const instance = state.instances.find((item) => String(item.id) === String(id));
    if (!instance) return;
    $("#instanceId").value = id;
    $("#instanceName").value = instance.name;
    $("#instanceRegion").value = instance.region;
    $("#instanceInfo").value = instance.operatorInfo;
    $("#instanceFee").textContent = `${ethers.formatEther(state.fee)} L1 coin`;
    openModal("#instanceModal");
  },

  contest(id) {
    $("#contestId").value = id;
    $("#contestReason").value = "";
    openModal("#contestModal");
  },

  async delEndpoint(id) {
    const endpoint = findEndpoint(id);
    if (!confirm(`Remove ${endpoint?.url} from the register?\n\nIts measurement history is kept and you can restore it later for one fee.`)) return;
    await submit("Removal", (contract) => contract.deleteEndpoint(id));
  },

  async delInstance(id) {
    if (!confirm("Delete this instance and all of its endpoints?\n\nThis frees one of your four instance slots. The records stay on-chain as history.")) return;
    await submit("Deletion", (contract) => contract.deleteInstance(id));
  },

  async restore(id) {
    await submit("Restore", (contract) => contract.reactivateEndpoint(id, { value: state.fee }));
  }
};

document.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-act]");
  if (!trigger) return;
  const action = ACTIONS[trigger.dataset.act];
  if (!action) return;
  event.preventDefault();
  action(trigger.dataset.id, trigger);
});

/* ------------------------------------------------------------------ wiring */

$("#connectBtn").onclick = () => connect().catch((error) => toast(explain(error)));
$("#refreshBtn").onclick = () => load();
$("#search").oninput = render;
$("#typeFilter").onchange = render;
$("#healthFilter").onchange = render;
$("#sortBy").onchange = render;

$("#openRegister").onclick = () => {
  if (!$("#endpointRows .endpoint-row")) endpointRow("#endpointRows", "#registerFee");
  updateFee("#endpointRows", "#registerFee");
  openModal("#registerModal");
};
$("#addEndpointRow").onclick = () => endpointRow("#endpointRows", "#registerFee");
$("#addMoreRow").onclick = () => endpointRow("#addRows", "#addFee");

$$("[data-close]").forEach((button) => {
  button.onclick = () => closeModal(button.closest(".modal"));
});
$$(".modal").forEach((modal) => {
  modal.onclick = (event) => { if (event.target === modal) closeModal(modal); };
});

$("#themeBtn").onclick = () => {
  const current = document.documentElement.dataset.theme;
  const dark = current
    ? current === "dark"
    : window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "light" : "dark";
};

$("#registerForm").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const { kinds, urls } = collectRows("#endpointRows");
    const data = new FormData(event.target);
    const name = String(data.get("name") || "").trim();
    if (!name) throw new Error("The instance needs a name.");
    const ok = await submit("Registration", (contract) =>
      contract.createInstance(name, String(data.get("region") || "").trim(),
        String(data.get("operatorInfo") || "").trim(), kinds, urls,
        { value: state.fee * BigInt(kinds.length) }));
    if (ok) {
      closeModal("#registerModal");
      event.target.reset();
      $("#endpointRows").innerHTML = "";
    }
  } catch (error) {
    toast(explain(error));
  }
};

$("#addForm").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const { kinds, urls } = collectRows("#addRows");
    const ok = await submit("Endpoints", (contract) =>
      contract.addEndpoints($("#addInstanceId").value, kinds, urls,
        { value: state.fee * BigInt(kinds.length) }));
    if (ok) closeModal("#addModal");
  } catch (error) {
    toast(explain(error));
  }
};

$("#editForm").onsubmit = async (event) => {
  event.preventDefault();
  const url = $("#editUrl").value.trim();
  const ok = await submit("Endpoint update", (contract) =>
    contract.updateEndpoint($("#editId").value, Number($("#editKind").value), url, { value: state.fee }));
  if (ok) closeModal("#editModal");
};

$("#instanceForm").onsubmit = async (event) => {
  event.preventDefault();
  const ok = await submit("Instance update", (contract) =>
    contract.updateInstance($("#instanceId").value, $("#instanceName").value.trim(),
      $("#instanceRegion").value.trim(), $("#instanceInfo").value.trim(), { value: state.fee }));
  if (ok) closeModal("#instanceModal");
};

$("#contestForm").onsubmit = async (event) => {
  event.preventDefault();
  const reason = $("#contestReason").value.trim();
  if (!reason) return toast("Describe what happened before recording a statement.");
  const ok = await submit("Statement", (contract) => contract.contestEndpoint($("#contestId").value, reason));
  if (ok) closeModal("#contestModal");
};

/* ------------------------------------------------------------------- start */

TYPES.forEach((type, index) => {
  $("#typeFilter").insertAdjacentHTML("beforeend", `<option value="${index}">${esc(type[0])}</option>`);
  $("#editKind").insertAdjacentHTML("beforeend", `<option value="${index}">${esc(type[0])} — ${esc(type[1])}</option>`);
});

if (window.ethereum) {
  window.ethereum.on?.("accountsChanged", () => window.location.reload());
  window.ethereum.on?.("chainChanged", () => window.location.reload());
}

window.addEventListener("load", () => {
  load();
  /* Feeds are rewritten once a minute; the page follows without a reload. */
  setInterval(() => { if (!document.hidden && !state.loading) load(); }, 60000);
});
