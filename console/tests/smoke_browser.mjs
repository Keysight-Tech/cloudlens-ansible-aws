// Browser smoke tests for the live console: the eight things a person can
// actually do, driven in a real browser against a real console process.
//
// The unit suites cover the machine (tests/test_bridge.mjs) and the server
// (tests/test_console.py). Neither can see the thing that matters most here:
// what a visitor's screen SAYS. These do, and only that.
//
//   1  replay        - no console running. The page plays, offers the console,
//                      shows no pairing box, and shows NO error anywhere
//   2  live          - the console's own page attaches with no prompt
//   3  a run streams - lines, narration, nodes lit, a terminal state
//   4  stop          - the run reads "stopped", never "complete"
//   5  lost console  - killed mid-run: the loss is named, the transcript stays
//   6  pairing       - a foreign origin gets the box, a wrong code, then live
//   7  pairDisabled  - the guess cap fires, and says something DIFFERENT
//   8  second run    - a run started after one finished actually streams
//
// Scenarios 6 and 7 need --dev-origin: on the console's own origin pairing is
// exempt, so a wrong code there is accepted anyway, and any other local origin
// is stopped by CORS before the page sees a body. That flag is the only way
// those two screens exist in a browser at all.
//
// Every expected string below is a LITERAL, copied from strings.js by hand. It
// is never read back out of the page or imported from the table: an assertion
// against a value the page just computed passes whatever the page computed.
//
// Run:
//     npm i -g playwright && npx playwright install chromium
//     cd console && node tests/smoke_browser.mjs
//     HEADED=1 node tests/smoke_browser.mjs      # watch it
//
// Nothing is installed into the repo: playwright is resolved from wherever it
// already is on the machine.
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import net from "node:net";
import fs from "node:fs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONSOLE_DIR = path.resolve(HERE, "..");
const REPO = path.resolve(CONSOLE_DIR, "..");
const DOCS = path.join(REPO, "docs");

const CONSOLE_PORT = 8760;    // bridge.js dials this by default; not a free choice
const STATIC_PORT = 8791;

// NOT localhost, and not 127.0.0.1 either. bridge.js treats ANY http page on a
// loopback hostname as having been served BY a console, and dials that page's
// own origin instead of the default port - so a static server on
// http://localhost:8791 gets probed for /health and answers 404, and the page
// never sees the real console at all.
//
// Chrome resolves every *.localhost name to loopback without a hosts entry, so
// dev.localhost is a real foreign origin that still reaches a server bound to
// 127.0.0.1: exactly the situation --dev-origin exists for.
const DEV_HOST = "dev.localhost";
const DEV_ORIGIN = "http://" + DEV_HOST + ":" + STATIC_PORT;
const PAGE_URL = DEV_ORIGIN + "/console.html";
const OWN_URL = "http://localhost:" + CONSOLE_PORT + "/";

// ---- the exact words the visitor must and must not see ---------------------
const S = {
  liveBadge: "LIVE",
  replayBadge: "REPLAY",
  goLiveTitle: "Run this for real",
  goLiveCommand: "python3 -m cloudlens_console",
  pairPrompt: "Pairing code",
  pairBad: "That code did not match. Check the console banner.",
  pairDisabled: "The console stopped accepting codes after too many wrong " +
    "attempts. Restart it on your machine to get a new pairing code.",
  hostRefused: "The console refused the request as unsafe: it only answers " +
    "to its own address on your machine. Open the console UI " +
    "directly at http://localhost:8760.",
  connected: "Connected to your console",
  lostConsole: "Lost the local console. The transcript below is kept.",
  transportFailed: "Could not reach the console on your machine. Nothing was started.",
  blockedPNA: "Your browser blocked the page from reaching 127.0.0.1.",
  statusComplete: "complete",
  statusFailed: "failed",
  statusStopped: "stopped",
  statusLost: "disconnected",
  statusRunning: "running"
};

// Anything in this list appearing on the replay page is a bug: a visitor who
// never runs the console must not be shown a failure.
const NEVER_ON_REPLAY = [
  S.pairBad, S.pairDisabled, S.hostRefused, S.lostConsole,
  S.transportFailed, S.blockedPNA
];

// ---- tiny harness ----------------------------------------------------------
let failures = 0, passes = 0;
const log = (...a) => console.log(...a);

function ok(cond, what) {
  if (cond) { passes++; log("    ok   " + what); }
  else { failures++; log("    FAIL " + what); }
}

function eq(actual, expected, what) {
  const good = actual === expected;
  if (good) { passes++; log("    ok   " + what); }
  else {
    failures++;
    log("    FAIL " + what + "\n         expected " + JSON.stringify(expected) +
        "\n         actual   " + JSON.stringify(actual));
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function requirePlaywright() {
  const req = createRequire(import.meta.url);
  const candidates = [
    "playwright",
    "/opt/homebrew/lib/node_modules/playwright",
    "/usr/local/lib/node_modules/playwright",
    "/opt/homebrew/lib/node_modules/@playwright/mcp/node_modules/playwright"
  ];
  for (const c of candidates) {
    try { return req(c); } catch (e) { /* next */ }
  }
  console.error("playwright not found. Install it once, globally:\n" +
                "  npm i -g playwright && npx playwright install chromium");
  process.exit(2);
}

function portFree(port) {
  return new Promise((res) => {
    const s = net.connect(port, "127.0.0.1");
    s.on("connect", () => { s.destroy(); res(false); });
    s.on("error", () => res(true));
    setTimeout(() => { s.destroy(); res(true); }, 700);
  });
}

async function waitPort(port, up, ms = 8000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    if (await portFree(port) === !up) { return true; }
    await sleep(120);
  }
  return false;
}

// ---- the console under test ------------------------------------------------
//
// Started as a real subprocess, exactly the way an operator starts it, and its
// banner is read for the pairing code. Never handed a code from a fixture: the
// code is minted per process and a hard-coded one would test nothing.
async function startConsole(extraArgs = []) {
  const proc = spawn("python3", ["-m", "cloudlens_console", "--no-open",
                                 "--port", String(CONSOLE_PORT), ...extraArgs],
                     { cwd: CONSOLE_DIR, stdio: ["ignore", "pipe", "pipe"] });
  let banner = "";
  proc.stdout.on("data", (b) => { banner += b.toString(); });
  proc.stderr.on("data", (b) => { banner += b.toString(); });
  const t0 = Date.now();
  while (Date.now() - t0 < 10000) {
    const m = banner.match(/Pairing code:\s+([A-Z2-9]{8})/);
    if (m && await waitPort(CONSOLE_PORT, true, 200)) {
      return { proc, code: m[1], banner: () => banner };
    }
    await sleep(100);
  }
  proc.kill("SIGKILL");
  throw new Error("console did not start. Output:\n" + banner);
}

async function stopConsole(c) {
  if (!c || !c.proc || c.proc.exitCode !== null) { return; }
  c.proc.kill("SIGKILL");
  await waitPort(CONSOLE_PORT, false, 5000);
}

function startStatic() {
  const proc = spawn("python3", ["-m", "http.server", String(STATIC_PORT),
                                 "--bind", "127.0.0.1", "-d", DOCS],
                     { stdio: "ignore" });
  return proc;
}

// Burn the console's cumulative guess budget from OUTSIDE the browser.
//
// The budget is global and cumulative for the life of the process, so it does
// not matter who spends it - which is the point of it being global. Done here
// rather than by typing 200 codes because each wrong guess costs the console
// half a second of a handler thread on purpose.
function burnGuessBudget(origin) {
  return new Promise((res, rej) => {
    const py = `
import json, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
def guess(_):
    req = urllib.request.Request(
        "http://127.0.0.1:${CONSOLE_PORT}/run", method="POST",
        data=json.dumps({"flow": "stack", "replay": True}).encode(),
        headers={"Origin": "${origin}", "Content-Type": "application/json",
                 "X-CloudLens-Pair": "WRONGCOD"})
    try:
        urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError:
        pass
    except Exception:
        pass
with ThreadPoolExecutor(max_workers=50) as ex:
    list(ex.map(guess, range(260)))
print("burned")
`;
    const p = spawn("python3", ["-c", py], { stdio: ["ignore", "pipe", "inherit"] });
    let out = "";
    p.stdout.on("data", (b) => { out += b.toString(); });
    p.on("exit", (c) => (c === 0 && out.includes("burned")) ? res() : rej(new Error("burn failed")));
  });
}

// ---- page helpers ----------------------------------------------------------
const txt = (page, id) => page.evaluate((i) => {
  const e = document.getElementById(i);
  return e ? (e.textContent || "") : null;
}, id);

const hidden = (page, id) => page.evaluate((i) => {
  const e = document.getElementById(i);
  return e ? !!e.hidden : null;
}, id);

const conLines = (page) => page.evaluate(
  () => document.querySelectorAll("#console .cln").length);
const narrLines = (page) => page.evaluate(
  () => document.querySelectorAll("#narr .nline, #narr .card-in").length);
const litNodes = (page) => page.evaluate(
  () => document.querySelectorAll("#diagram .node.live").length);
const bodyText = (page) => page.evaluate(() => document.body.innerText);

async function until(page, fn, ms, what) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) {
    if (await fn()) { return true; }
    await sleep(150);
  }
  log("    (timed out waiting for " + what + ")");
  return false;
}

const isTerminal = (s) => [S.statusComplete, S.statusFailed, S.statusStopped,
                           S.statusLost].includes(s.trim());

// ---- the scenarios ---------------------------------------------------------
async function main() {
  if (!fs.existsSync(path.join(DOCS, "console.html"))) {
    console.error("docs/console.html is missing. Run: cd console && python3 build_site.py");
    process.exit(2);
  }
  // Both ports, and this is not tidiness: a stale server left on the static
  // port serves SOME page, every id lookup answers null, and the run reads as
  // a pile of product failures. Refuse to start rather than test a page nobody
  // built.
  for (const p of [CONSOLE_PORT, STATIC_PORT]) {
    if (!await portFree(p)) {
      console.error("port " + p + " is busy: stop whatever is on it first " +
                    "(lsof -nP -iTCP:" + p + " -sTCP:LISTEN)");
      process.exit(2);
    }
  }

  const { chromium } = requirePlaywright();
  const browser = await chromium.launch({ headless: !process.env.HEADED });
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const stat = startStatic();
  await waitPort(STATIC_PORT, true);
  let con = null;

  try {
    // ---- 1. replay: no console anywhere ------------------------------------
    log("\n1. replay, with no console running");
    await page.goto(PAGE_URL, { waitUntil: "load" });
    // Prove it is the BUILT page before asserting anything about it. Serving
    // some other file makes every id below answer null, and null is not a
    // product failure worth reporting as one.
    ok(await page.evaluate(() => !!document.getElementById("pairForm")),
       "the page under test is the built console page");
    await until(page, async () => await conLines(page) > 3 && await narrLines(page) > 0,
                25000, "the replay to play");

    eq(await txt(page, "modeBadge"), S.replayBadge, "the badge says REPLAY");
    eq(await hidden(page, "pairForm"), true, "no pairing box");
    eq(await hidden(page, "link"), false, "the offer panel is shown");
    eq(await txt(page, "linkTitle"), S.goLiveTitle, "the offer names itself");
    eq(await txt(page, "linkCmd"), S.goLiveCommand, "the command is shown");
    eq(await txt(page, "notice"), "", "no notice at all");
    ok(await conLines(page) > 3, "captured output is playing");
    ok(await narrLines(page) > 0, "narration is playing");

    const seen = await bodyText(page);
    for (const bad of NEVER_ON_REPLAY) {
      ok(!seen.includes(bad),
         "no error text: " + JSON.stringify(bad.slice(0, 34) + "..."));
    }

    // ---- 2. live on the console's own origin --------------------------------
    log("\n2. live on the console's own origin");
    con = await startConsole();
    await page.goto(OWN_URL, { waitUntil: "load" });
    await until(page, async () => await txt(page, "notice") === S.connected,
                8000, "the attach to land");

    eq(await hidden(page, "pairForm"), true, "attached with no prompt");
    eq(await txt(page, "notice"), S.connected, "and says so");
    eq(await hidden(page, "demoWrap"), false, "the demo guard is live-only, and is shown");
    await page.click("#demoSw");        // the badge states real AWS, so demo must be off
    eq(await txt(page, "modeBadge"), S.liveBadge, "the badge reads LIVE");
    await page.click("#demoSw");        // back on: nothing below may spend real money

    // ---- 3. a run streams ---------------------------------------------------
    log("\n3. a run streams, through the real console and real SSE");
    await page.goto(OWN_URL, { waitUntil: "load" });
    await until(page, async () => await txt(page, "notice") === S.connected, 8000, "attach");
    eq(await hidden(page, "demoWrap"), false, "still live");
    await page.click("#runBtn");
    eq((await txt(page, "statusTxt")).trim(), S.statusRunning, "it reports running");
    await until(page, async () => await conLines(page) > 5, 20000, "console lines");
    ok(await conLines(page) > 5, "console lines accumulate");
    ok(await narrLines(page) > 0, "narration accumulates");
    ok(await hidden(page, "idChip") === false, "the identity frame arrived");
    await until(page, async () => await litNodes(page) > 0, 20000, "a node to light");
    ok(await litNodes(page) > 0, "nodes light");
    await until(page, async () => isTerminal(await txt(page, "statusTxt")), 60000, "an ending");
    eq((await txt(page, "statusTxt")).trim(), S.statusComplete, "it reaches a terminal state");

    // ---- 4. stop mid-run ----------------------------------------------------
    log("\n4. stop mid-run");
    await page.goto(OWN_URL, { waitUntil: "load" });
    await until(page, async () => await txt(page, "notice") === S.connected, 8000, "attach");
    await page.click("#runBtn");
    await until(page, async () => await conLines(page) > 2, 20000, "some output first");
    await page.click("#stopBtn");
    await until(page, async () => isTerminal(await txt(page, "statusTxt")), 20000, "the stop");
    // The Part B guard, at the level the visitor sees: a run cut short is
    // stopped, and specifically never "complete".
    eq((await txt(page, "statusTxt")).trim(), S.statusStopped, "the run reads stopped");
    ok((await txt(page, "statusTxt")).trim() !== S.statusComplete,
       "and is never reported as complete");
    ok(await conLines(page) > 2, "the transcript is retained");

    // ---- 5. the console goes away mid-run -----------------------------------
    log("\n5. the console is killed mid-run");
    await page.goto(OWN_URL, { waitUntil: "load" });
    await until(page, async () => await txt(page, "notice") === S.connected, 8000, "attach");
    await page.click("#runBtn");
    await until(page, async () => await conLines(page) > 2, 20000, "some output first");
    const kept = await conLines(page);
    await stopConsole(con); con = null;
    await until(page, async () => await txt(page, "notice") === S.lostConsole,
                20000, "the loss to be noticed");
    eq(await txt(page, "notice"), S.lostConsole, "the loss is named");
    eq((await txt(page, "statusTxt")).trim(), S.statusLost, "and is not called a failure");
    ok(await conLines(page) >= kept, "the transcript is still on screen");

    // ---- 6. pairing, from a foreign origin ----------------------------------
    log("\n6. pairing, from a --dev-origin page");
    con = await startConsole(["--dev-origin", DEV_ORIGIN]);
    ok(con.banner().includes("DEV ORIGIN GRANTED: " + DEV_ORIGIN),
       "the console shouted about the grant");
    await page.goto(PAGE_URL, { waitUntil: "load" });
    await until(page, async () => await hidden(page, "pairForm") === false,
                10000, "the pairing box");
    eq(await hidden(page, "pairForm"), false, "the pairing box appears");
    eq(await txt(page, "pairLabel"), S.pairPrompt, "and asks for the code");

    const wrong = con.code === "AAAA2345" ? "BBBB2345" : "AAAA2345";
    await page.fill("#pairInput", wrong);
    await page.click("#pairBtn");
    await until(page, async () => await txt(page, "notice") === S.pairBad, 15000, "the retry");
    eq(await txt(page, "notice"), S.pairBad, "a wrong code says to check the banner");
    eq(await hidden(page, "pairForm"), false, "and keeps the box up");

    await page.fill("#pairInput", con.code);
    await page.click("#pairBtn");
    await until(page, async () => await txt(page, "notice") === S.connected, 15000, "pairing");
    eq(await txt(page, "notice"), S.connected, "the right code reaches live");
    eq(await hidden(page, "pairForm"), true, "and the box is gone");
    eq(await hidden(page, "demoWrap"), false, "the page is live");
    await page.click("#demoSw");
    eq(await txt(page, "modeBadge"), S.liveBadge, "the badge reads LIVE");
    await page.click("#demoSw");

    // ---- 7. the guess cap ---------------------------------------------------
    log("\n7. the guess cap, on a fresh console");
    await stopConsole(con);
    con = await startConsole(["--dev-origin", DEV_ORIGIN]);
    await page.goto(PAGE_URL, { waitUntil: "load" });
    await until(page, async () => await hidden(page, "pairForm") === false, 10000, "the box");
    await burnGuessBudget(DEV_ORIGIN);
    // The RIGHT code, on purpose: after the cap, retyping a correct code fails
    // forever, and telling the visitor to check their typing is actively wrong.
    await page.fill("#pairInput", con.code);
    await page.click("#pairBtn");
    await until(page, async () => await txt(page, "notice") === S.pairDisabled,
                20000, "the restart message");
    eq(await txt(page, "notice"), S.pairDisabled, "it says to restart the console");
    ok(S.pairDisabled !== S.pairBad,
       "and that is a different sentence from the wrong-code one");
    ok(!(await bodyText(page)).includes(S.pairBad),
       "the wrong-code sentence is nowhere on the page");
    eq(await hidden(page, "pairForm"), true, "and there is nothing left to type");

    // ---- 8. a second run after the first completes --------------------------
    log("\n8. a second run, after one has finished");
    await stopConsole(con);
    con = await startConsole();
    await page.goto(OWN_URL, { waitUntil: "load" });
    await until(page, async () => await txt(page, "notice") === S.connected, 8000, "attach");
    await page.click("#runBtn");
    await until(page, async () => isTerminal(await txt(page, "statusTxt")), 60000, "run one");
    eq((await txt(page, "statusTxt")).trim(), S.statusComplete, "the first run completes");

    await page.click("#runBtn");
    // job.started used to be accepted only from `live`, so the second run's id
    // was dropped and every frame after it refused: the page sat on the FIRST
    // run's ending with nothing arriving. Both halves are asserted.
    await until(page, async () => await conLines(page) > 5, 30000, "the second run's output");
    ok(await conLines(page) > 5, "the second run's frames reach the page");
    await until(page, async () => isTerminal(await txt(page, "statusTxt")), 60000, "run two");
    eq((await txt(page, "statusTxt")).trim(), S.statusComplete, "and it ends on its own");
  } finally {
    await stopConsole(con);
    stat.kill("SIGKILL");
    await browser.close();
  }

  log("\n" + passes + " passed, " + failures + " failed");
  process.exit(failures ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
