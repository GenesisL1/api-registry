/* End-to-end test: load the real page against a real registry.
 *
 * Starts test/e2e_fixture.py (in-process EVM behind an HTTP JSON-RPC bridge,
 * plus a static server for web/), renders index.html in jsdom with the actual
 * scripts, and asserts on the rendered DOM. This is the test that catches an
 * ABI mismatch, a signature that will not verify in a browser, or a render
 * that throws on real contract data.
 *
 *   npm install jsdom            # the only dev dependency
 *   node test/e2e.js
 */
"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const path = require("path");
const { JSDOM, VirtualConsole } = require("jsdom");

const ROOT = path.resolve(__dirname, "..");
const PASSED = [];
const FAILED = [];

function check(name, condition, detail = "") {
  if (condition) {
    PASSED.push(name);
    console.log(`  \x1b[32mpass\x1b[0m  ${name}`);
  } else {
    FAILED.push([name, detail]);
    console.log(`  \x1b[31mFAIL\x1b[0m  ${name}  ${detail}`);
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(predicate, { timeout = 30000, interval = 200, label = "condition" } = {}) {
  const deadline = Date.now() + timeout;
  for (;;) {
    let value;
    try { value = predicate(); } catch { value = false; }
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
    await sleep(interval);
  }
}

function startFixture() {
  return new Promise((resolve, reject) => {
    const solc = process.env.SOLC || "solc";
    const child = spawn("python3", [path.join("test", "e2e_fixture.py"), "--solc", solc, "--hold"], {
      cwd: ROOT, stdio: ["ignore", "pipe", "pipe"]
    });
    let buffer = "";
    let settled = false;
    child.stdout.on("data", (chunk) => {
      buffer += chunk.toString();
      const start = buffer.indexOf("{");
      const end = buffer.indexOf("\n}");
      if (!settled && start >= 0 && end > start) {
        try {
          const manifest = JSON.parse(buffer.slice(start, end + 2));
          settled = true;
          resolve({ child, manifest });
        } catch { /* keep buffering */ }
      }
    });
    child.stderr.on("data", (chunk) => process.env.VERBOSE && process.stderr.write(chunk));
    child.on("exit", (code) => { if (!settled) reject(new Error(`fixture exited with ${code}\n${buffer}`)); });
    setTimeout(() => { if (!settled) reject(new Error(`fixture did not start\n${buffer}`)); }, 180000);
  });
}

async function main() {
  console.log("starting the fixture chain…");
  const { child, manifest } = await startFixture();
  const configPath = path.join(ROOT, "web", "config.js");
  const originalConfig = fs.readFileSync(configPath, "utf8");

  try {
    // Point the page at the fixture. Both feeds are listed, and one bogus feed
    // is included to prove an unauthorized signer is rejected rather than shown.
    fs.writeFileSync(configPath, `window.GENESIS_API_CONFIG = ${JSON.stringify({
      chainId: manifest.chainId,
      chainHex: "0x" + manifest.chainId.toString(16),
      chainName: "GenesisL1 fixture",
      nativeCurrency: { name: "L1 coin", symbol: "L1", decimals: 18 },
      blockExplorerUrls: [],
      rpcUrls: [manifest.rpcUrl],
      registryAddress: manifest.registryAddress,
      lensAddress: manifest.lensAddress,
      telemetryUrls: ["data/status-1.json", "data/status-2.json", "data/status-forged.json"],
      requireSignedTelemetry: true,
      pageSize: 25
    }, null, 2)};\n`);

    // A forged feed: valid JSON, valid signature, signer nobody authorized.
    const forged = JSON.parse(fs.readFileSync(path.join(ROOT, "web", "data", "status-1.json"), "utf8"));
    forged.endpoints.forEach((entry) => { entry.stats_24h.uptime_bps = 10000; });
    fs.writeFileSync(path.join(ROOT, "web", "data", "status-forged.json"), JSON.stringify(forged));
    const { ethers } = require(path.join(ROOT, "web", "vendor", "ethers-6.13.4.umd.min.js"));
    const impostor = new ethers.Wallet("0x" + "ab".repeat(32));
    const forgedRaw = fs.readFileSync(path.join(ROOT, "web", "data", "status-forged.json"));
    fs.writeFileSync(path.join(ROOT, "web", "data", "status-forged.json.sig"), JSON.stringify({
      address: impostor.address,
      signature: await impostor.signMessage(forgedRaw),
      signed_at: forged.generated_at,
      algorithm: "eip191-personal-sign/status.json-bytes"
    }));

    const virtualConsole = new VirtualConsole();
    const errors = [];
    virtualConsole.on("jsdomError", (error) => errors.push(error.message));
    virtualConsole.on("error", (...args) => errors.push(args.join(" ")));

    console.log("rendering the page…\n");
    const dom = await JSDOM.fromURL(`${manifest.webUrl}/index.html`, {
      runScripts: "dangerously",
      resources: "usable",
      pretendToBeVisual: true,
      virtualConsole,
      // jsdom ships no fetch and no matchMedia. Both exist in every browser the
      // page targets, so the shims restore the platform rather than the page.
      beforeParse(window) {
        window.fetch = (input, init) => {
          const url = typeof input === "string" ? new URL(input, window.location.href).href : input;
          // Browsers silently drop forbidden request headers; undici rejects
          // them. Strip them so the harness behaves like the real platform.
          const options = { ...(init || {}) };
          if (options.headers) {
            const headers = {};
            const entries = options.headers instanceof globalThis.Headers
              ? Array.from(options.headers.entries())
              : Object.entries(options.headers);
            for (const [key, value] of entries) {
              if (!/^(content-length|connection|host|origin)$/i.test(key)) headers[key] = value;
            }
            options.headers = headers;
          }
          return globalThis.fetch(url, options);
        };
        window.Headers = globalThis.Headers;
        window.Request = globalThis.Request;
        window.Response = globalThis.Response;
        window.matchMedia = window.matchMedia || ((query) => ({
          matches: false, media: query, onchange: null,
          addEventListener() {}, removeEventListener() {},
          addListener() {}, removeListener() {}, dispatchEvent() { return false; }
        }));
      }
    });
    const { window } = dom;
    const $ = (selector) => window.document.querySelector(selector);
    const $$ = (selector) => Array.from(window.document.querySelectorAll(selector));

    try {
      await waitFor(() => $$("#register .instance").length > 0, { label: "the register to render" });
    } catch (error) {
      console.log("\ndiagnostics");
      console.log("  notice:  ", $("#notice").textContent.slice(0, 300));
      console.log("  register:", $("#register").textContent.slice(0, 300));
      console.log("  ethers:  ", typeof window.ethers, "| abi:", typeof window.GENESIS_ABI,
                  "| cfg:", typeof window.GENESIS_API_CONFIG);
      console.log("  errors:  ", errors.slice(0, 4).join(" | ").slice(0, 900));
      throw error;
    }
    await sleep(1500);

    console.log("register");
    check("every seeded instance renders", $$("#register .instance").length === manifest.instances,
      `${$$("#register .instance").length} of ${manifest.instances}`);
    check("every endpoint renders", $$("#register .endpoint").length === manifest.endpoints,
      `${$$("#register .endpoint").length} of ${manifest.endpoints}`);
    check("instance counter matches the chain", $("#countInstances").textContent === String(manifest.instances),
      $("#countInstances").textContent);
    check("endpoint counter matches the chain", $("#countEndpoints").textContent === String(manifest.endpoints),
      $("#countEndpoints").textContent);
    check("no page script threw", errors.length === 0, errors.slice(0, 2).join(" | "));

    console.log("\ntelemetry trust");
    check("both authorized feeds were accepted", $("#countFeeds").textContent === "2", $("#countFeeds").textContent);
    const noticeText = $("#notice").textContent;
    check("the forged feed was rejected", /does not authorize/.test(noticeText), noticeText.slice(0, 120));
    check("the rejection names the impostor", noticeText.includes(impostor.address.slice(0, 6)),
      noticeText.slice(0, 120));
    check("no forged 100% reading is displayed",
      !$$(".quality b").some((element) => element.textContent === "100.00%"));

    console.log("\nmeasurements");
    const uptimes = $$(".endpoint .quality b").map((element) => element.textContent).filter((text) => /%$/.test(text));
    check("uptime is shown for every endpoint", uptimes.length === manifest.endpoints,
      `${uptimes.length} of ${manifest.endpoints}`);
    check("uptime values are plausible", uptimes.every((text) => parseFloat(text) > 80 && parseFloat(text) <= 100));
    check("latency is shown in milliseconds", $$(".quality b").some((element) => /\d+ ms$/.test(element.textContent)));
    check("the 24 hour trace renders 24 cells per endpoint",
      $$(".endpoint .trace").every((trace) => trace.children.length === 24));
    check("the network trace renders", $("#networkTrace").children.length === 24,
      String($("#networkTrace").children.length));
    check("trace cells carry an hourly tooltip",
      $$(".endpoint .trace-cell").some((cell) => /\d\d:\d\dZ/.test(cell.getAttribute("title"))));
    check("checker consensus is shown per endpoint",
      $$(".consensus small").every((element) => /2 checkers/.test(element.textContent)));

    console.log("\non-chain state");
    const flagged = $$(".endpoint.flagged");
    check("the quorum-flagged endpoint is marked", flagged.length === 1, `${flagged.length} flagged`);
    check("the flag explains how it clears", /quorum measures it back at target/.test(flagged[0]?.textContent || ""));
    check("the on-chain attestation is displayed",
      /checkers attested/.test(window.document.body.textContent));
    check("the lifetime flagged-day count is displayed",
      /flagged day/.test(window.document.body.textContent));
    check("host control verification is displayed",
      /host control verified/.test(window.document.body.textContent));
    check("the fee is read from the contract", /fee 1\.0 L1/.test($("#footMeta").textContent),
      $("#footMeta").textContent);
    check("the reliability target is read from the contract", /target 99\.00%/.test($("#footMeta").textContent),
      $("#footMeta").textContent);

    console.log("\ninteraction");
    const searchBox = $("#search");
    searchBox.value = "zurich";
    searchBox.dispatchEvent(new window.Event("input"));
    check("search narrows the register", $$("#register .instance").length === 1,
      `${$$("#register .instance").length} instances`);
    searchBox.value = "";
    searchBox.dispatchEvent(new window.Event("input"));

    const healthFilter = $("#healthFilter");
    healthFilter.value = "down";
    healthFilter.dispatchEvent(new window.Event("change"));
    check("the flagged filter isolates flagged endpoints",
      $$("#register .endpoint").length === 1, `${$$("#register .endpoint").length} endpoints`);
    healthFilter.value = "";
    healthFilter.dispatchEvent(new window.Event("change"));

    const sortBy = $("#sortBy");
    sortBy.value = "name";
    sortBy.dispatchEvent(new window.Event("change"));
    const names = $$("#register .instance h3").map((element) => element.textContent);
    check("sorting by name orders the register",
      JSON.stringify(names) === JSON.stringify(names.slice().sort()), names.join(", "));

    searchBox.value = "nothing matches this";
    searchBox.dispatchEvent(new window.Event("input"));
    check("an empty result explains itself", /Clear the search/.test($("#register").textContent));
    searchBox.value = "";
    searchBox.dispatchEvent(new window.Event("input"));

    $("#openRegister").dispatchEvent(new window.Event("click"));
    check("the registration dialog opens", $("#registerModal").classList.contains("open"));
    check("the dialog is announced to assistive technology",
      $("#registerModal").getAttribute("aria-hidden") === "false");
    check("the fee shown in the dialog comes from the chain",
      $("#registerFee").textContent === "1.0 L1 coin", $("#registerFee").textContent);
    $("#addEndpointRow").dispatchEvent(new window.Event("click"));
    check("adding a row updates the total", $("#addEndpointRow") && $("#registerFee").textContent === "2.0 L1 coin",
      $("#registerFee").textContent);
    $("#registerModal [data-close]").dispatchEvent(new window.Event("click"));
    check("the dialog closes", !$("#registerModal").classList.contains("open"));

    console.log("\nescaping and safety");
    check("no inline event handler is emitted",
      !/ on(click|error|load)=/i.test($("#register").innerHTML));
    check("URLs render inside a text node, not an attribute sink",
      $$(".url").every((element) => element.children.length === 0));
    check("operator links carry noopener", $$(".instance-meta a").every((element) =>
      (element.getAttribute("rel") || "").includes("noopener")));

    check("no page script threw during interaction", errors.length === 0, errors.slice(0, 2).join(" | "));
    dom.window.close();
  } finally {
    fs.writeFileSync(configPath, originalConfig);
    for (const name of ["status-forged.json", "status-forged.json.sig",
                        "status-1.json", "status-1.json.sig",
                        "status-2.json", "status-2.json.sig"]) {
      fs.rmSync(path.join(ROOT, "web", "data", name), { force: true });
    }
    fs.rmSync(path.join(ROOT, "test", "e2e-manifest.json"), { force: true });
    child.kill("SIGKILL");
  }

  console.log(`\n${"-".repeat(62)}`);
  console.log(`${PASSED.length} passed, ${FAILED.length} failed`);
  FAILED.forEach(([name, detail]) => console.log(`  FAILED: ${name} — ${detail}`));
  process.exit(FAILED.length ? 1 : 0);
}

main().catch((error) => { console.error(error); process.exit(1); });
