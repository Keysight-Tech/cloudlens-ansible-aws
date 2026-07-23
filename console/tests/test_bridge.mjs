// Drives the bridge state machine directly, with synthetic events: no browser,
// no server, no timers. Run it with node's own runner, no dependencies:
//
//     node --test console/tests/test_bridge.mjs
//
// Every expectation here is a LITERAL: a state name spelled out, a string key
// spelled out. Nothing is read back out of the machine and then asserted
// against itself, because a test that does that passes for any implementation,
// including a wrong one.
import test from "node:test";
import assert from "node:assert/strict";

import B from "../cloudlens_console/web/bridge.js";

// What the real /health answers with. Kept as a literal rather than imported
// from anywhere: if the server's body changes, this file is meant to fail.
const HEALTH = { ok: true, service: "cloudlens-console", version: "1.0" };

test("the machine starts in replay: the public page ships working, always", () => {
  const s = B.initial();
  assert.equal(s.name, "replay");
  assert.equal(s.notice, null);
  assert.deepEqual(s.transcript, []);
});

test("a probe that answers as OUR console moves to pairing", () => {
  const s = B.reduce(B.initial(), { type: "probe.ok", body: HEALTH });
  assert.equal(s.name, "pairing");
  assert.equal(s.notice, null);
});

test("a probe answered by something that is NOT our console stays in replay", () => {
  // A random dev server on the same port. It is listening, it speaks JSON, and
  // it is not us. Reaching pairing here would ask the visitor to type a code
  // into a page that will never accept one.
  const S = "cloudlens-console";
  const impostors = [
    { ok: true },                                  // no version
    { ok: true, service: S, version: "1.0", extra: "x" },  // not the closed key set
    { version: "1.0" },                            // no ok
    { ok: "true", service: S, version: "1.0" },    // ok not a boolean
    { ok: true, service: S, version: "v1.0" },     // not a wire-contract number
    { ok: true, service: S, version: "1.0.3" },    // ditto: N.N only
    { ok: true, service: S, version: 1.0 },        // not a string
    { ok: false, service: S, version: "1.0" },
    // Right shape, wrong software. This is the case the service key exists
    // for: before it, a body of exactly {ok:true, version:"1.0"} from anything
    // at all was indistinguishable from our console, and the visitor was put
    // in front of a pairing prompt a stranger can never accept.
    { ok: true, version: "1.0" },                  // no service at all
    { ok: true, service: "", version: "1.0" },
    { ok: true, service: "cloudlens", version: "1.0" },       // not the full name
    { ok: true, service: "Cloudlens-Console", version: "1.0" },  // compared byte for byte
    { ok: true, service: "cloudlens-console-dev", version: "1.0" },
    { ok: true, service: true, version: "1.0" },   // not a string
    {},
    null,
    "OK",
    [1, 2, 3]
  ];
  for (const body of impostors) {
    const s = B.reduce(B.initial(), { type: "probe.ok", body: body });
    assert.equal(s.name, "replay", "body " + JSON.stringify(body) + " is not our console");
  }
});

test("the service name is what decides it, not the shape around it", () => {
  // Hold everything else constant and change only "service". If this pair ever
  // agrees, identification has quietly gone back to being an inference from
  // the shape, and any dev server answering that shape is us again.
  const ours = { ok: true, service: "cloudlens-console", version: "1.0" };
  const theirs = { ok: true, service: "some-other-server", version: "1.0" };
  assert.equal(B.reduce(B.initial(), { type: "probe.ok", body: ours }).name, "pairing");
  assert.equal(B.reduce(B.initial(), { type: "probe.ok", body: theirs }).name, "replay");
});

test("a probe that fails degrades to replay, never to an error state", () => {
  // Chrome's Private Network Access blocks a public HTTPS page from reaching
  // 127.0.0.1 and the failure is a generic network error, indistinguishable
  // from nothing listening. So this can never be reported as a fault.
  const s = B.reduce(B.initial(), { type: "probe.fail", reason: "network" });
  assert.equal(s.name, "replay");
  assert.equal(s.notice, null);
});

test("a probe we can attribute to the private-network block advises the visitor", () => {
  const s = B.reduce(B.initial(), { type: "probe.fail", reason: "blocked" });
  assert.equal(s.name, "replay");
  assert.equal(s.notice, "blockedPNA");
});

test("a probe failing after we were already paired drops back to replay", () => {
  const paired = B.reduce(B.reduce(B.initial(), { type: "probe.ok", body: HEALTH }),
                          { type: "pair.ok" });
  assert.equal(paired.name, "live");
  const s = B.reduce(paired, { type: "probe.fail", reason: "network" });
  assert.equal(s.name, "replay");
});

// ---- pairing outcomes ----------------------------------------------------
//
// The three refusals below are three different situations for the visitor and
// must never look alike: one is "you mistyped, try again", one is a dead end
// that needs a restart on their machine, and one says the request itself was
// refused as unsafe. Statuses and bodies are the server's, verified against
// server.py: a mismatch is 401 "pairing required" (NOT 403), the cap is 403
// "pairing disabled, restart the console", and a rebound Host is 403
// "bad host".

test("the code the console printed is accepted: pairing -> live", () => {
  const s = B.reduce(B.reduce(B.initial(), { type: "probe.ok", body: HEALTH }),
                     { type: "pair.ok" });
  assert.equal(s.name, "live");
  assert.equal(s.notice, "connected");
});

test("a wrong code stays in pairing and offers the retry string", () => {
  const paired = B.reduce(B.initial(), { type: "probe.ok", body: HEALTH });
  const s = B.reduce(paired, {
    type: "pair.denied", status: 401, error: "pairing required"
  });
  assert.equal(s.name, "pairing", "the visitor can just type it again");
  assert.equal(s.notice, "pairBad");
});

test("pairing disabled is a dead end, distinct from a wrong code", () => {
  const paired = B.reduce(B.initial(), { type: "probe.ok", body: HEALTH });
  const s = B.reduce(paired, {
    type: "pair.denied", status: 403, error: "pairing disabled, restart the console"
  });
  assert.equal(s.name, "disabled");
  assert.notEqual(s.name, "pairing");
  assert.equal(s.notice, "pairDisabled",
    "retyping a correct code will fail forever: say restart the console");
});

test("a bad host is its own state and shows hostRefused", () => {
  const paired = B.reduce(B.initial(), { type: "probe.ok", body: HEALTH });
  const s = B.reduce(paired, { type: "pair.denied", status: 403, error: "bad host" });
  assert.equal(s.name, "refused");
  assert.equal(s.notice, "hostRefused");
});

test("the dead-end refusals are keyed on the body, not on the status", () => {
  // Both are 403. Keying on the status alone would collapse two situations
  // with different remedies into one message.
  const paired = B.reduce(B.initial(), { type: "probe.ok", body: HEALTH });
  const disabled = B.reduce(paired, {
    type: "pair.denied", status: 403, error: "pairing disabled, restart the console"
  });
  const badHost = B.reduce(paired, {
    type: "pair.denied", status: 403, error: "bad host" });
  assert.equal(disabled.name, "disabled");
  assert.equal(badHost.name, "refused");
});

test("an unrecognised refusal is treated as a wrong code, not as success", () => {
  const paired = B.reduce(B.initial(), { type: "probe.ok", body: HEALTH });
  const s = B.reduce(paired, { type: "pair.denied", status: 500, error: "" });
  assert.equal(s.name, "pairing");
  assert.equal(s.notice, "pairBad");
});

test("the code is checked without being case-folded", () => {
  // hmac.compare_digest is byte for byte, and the alphabet is upper case with
  // no O/0 or I/1. Upper-casing what the visitor typed would turn a paste of
  // the right code into a wrong one for any console that ever widens it, and
  // silently spend a guess against a cap that never resets.
  assert.equal(B.looksLikeCode("ABCD2345"), true);
  assert.equal(B.looksLikeCode("abcd2345"), false, "lower case is not the code");
  assert.equal(B.looksLikeCode("ABCD234"), false, "seven characters");
  assert.equal(B.looksLikeCode("ABCD23456"), false, "nine characters");
  assert.equal(B.looksLikeCode("ABCD0I1O"), false, "0, I, 1 and O are not in the alphabet");
  assert.equal(B.looksLikeCode(""), false);
  assert.equal(B.looksLikeCode(null), false);
});

// ---- the live stream -----------------------------------------------------
//
// Event shapes are the server's, from events.py: hello, log, state, narrate,
// stat, done, error. done is terminal; error is terminal only when it names no
// node, since a per-node error is reported mid-deploy and the job continues.

function live() {
  return B.reduce(B.reduce(B.initial(), { type: "probe.ok", body: HEALTH }),
                  { type: "pair.ok" });
}

test("a job id is the capability, and the machine holds it", () => {
  const s = B.reduce(live(), {
    type: "job.started", jobId: "0123456789abcdef0123456789abcdef"
  });
  assert.equal(s.name, "live");
  assert.equal(s.jobId, "0123456789abcdef0123456789abcdef");
});

test("a second deploy can start from wherever the first one ended", () => {
  // "Run again" is the ordinary next thing a visitor does, and the first run
  // always leaves the machine in one of these three. While job.started was
  // accepted from live alone, the new id was dropped on the floor, every frame
  // after it was refused for arriving outside a live state, and the page went
  // on showing the previous run's outcome over a deploy that was under way.
  const enders = [
    [{ type: "sse.event", event: { id: 1, type: "done", summary: "up" } }, "finished"],
    [{ type: "sse.event", event: { id: 1, type: "error", text: "rolled back" } }, "failed"],
    [{ type: "sse.error" }, "lost"]
  ];
  for (const [ending, name] of enders) {
    const ended = B.reduce(live(), ending);
    assert.equal(ended.name, name);
    const again = B.reduce(ended, {
      type: "job.started", jobId: "ffffffffffffffffffffffffffffffff"
    });
    assert.equal(again.name, "live", "a console that answered /run is a console we are paired to");
    assert.equal(again.jobId, "ffffffffffffffffffffffffffffffff");
    assert.deepEqual(again.transcript, [], "the new deploy starts on a clean transcript");
    assert.equal(again.notice, null, "the last run's banner is a sentence about nothing now");
    // and the stream is heard again
    const framed = B.reduce(again, { type: "sse.event", event: { id: 9, type: "log", text: "creating" } });
    assert.equal(framed.transcript.length, 1);
  }
});

test("a job id from a state that never paired is not accepted", () => {
  // /run from replay, pairing, disabled or refused has nothing to answer it,
  // so a job.started arriving there is not a deploy of ours to watch.
  for (const build of [
    () => B.initial(),
    () => B.reduce(B.initial(), { type: "probe.ok", body: HEALTH }),
    () => B.reduce(B.reduce(B.initial(), { type: "probe.ok", body: HEALTH }),
                   { type: "pair.denied", status: 403, error: "pairing disabled, restart the console" }),
    () => B.reduce(B.reduce(B.initial(), { type: "probe.ok", body: HEALTH }),
                   { type: "pair.denied", status: 403, error: "bad host" })
  ]) {
    const before = build();
    const after = B.reduce(before, { type: "job.started", jobId: "aa" });
    assert.equal(after.name, before.name);
    assert.equal(after.jobId, null);
  }
});

test("stream events accumulate in order", () => {
  let s = live();
  s = B.reduce(s, { type: "sse.event", event: { id: 1, type: "log", text: "one" } });
  s = B.reduce(s, { type: "sse.event", event: { id: 2, type: "log", text: "two" } });
  assert.equal(s.name, "live");
  assert.equal(s.transcript.length, 2);
  assert.equal(s.transcript[0].text, "one");
  assert.equal(s.transcript[1].text, "two");
});

test("a per-node error does not end the job", () => {
  // events.py sends error(node=...) for one failed resource while the deploy
  // carries on. Ending the stream here would black out the rest of a live
  // deploy over a failure the operator can watch recover.
  let s = B.reduce(live(), {
    type: "sse.event", event: { id: 1, type: "error", text: "subnet failed", node: "vpc" }
  });
  assert.equal(s.name, "live");
  assert.equal(s.transcript.length, 1);
});

test("the terminal done event finishes the job and keeps the transcript", () => {
  let s = live();
  s = B.reduce(s, { type: "sse.event", event: { id: 1, type: "log", text: "creating" } });
  s = B.reduce(s, { type: "sse.event", event: { id: 2, type: "done", summary: "stack up" } });
  assert.equal(s.name, "finished");
  assert.equal(s.transcript.length, 2, "the transcript is what the visitor reads afterwards");
  assert.equal(s.transcript[1].type, "done");
});

test("a terminal error finishes the job as failed, transcript kept", () => {
  let s = live();
  s = B.reduce(s, { type: "sse.event", event: { id: 1, type: "log", text: "creating" } });
  s = B.reduce(s, { type: "sse.event", event: { id: 2, type: "error", text: "rolled back" } });
  assert.equal(s.name, "failed");
  assert.equal(s.transcript.length, 2);
});

test("the console going away mid-job is lostConsole, transcript kept", () => {
  let s = live();
  s = B.reduce(s, { type: "sse.event", event: { id: 1, type: "log", text: "creating" } });
  s = B.reduce(s, { type: "sse.error" });
  assert.equal(s.name, "lost");
  assert.equal(s.notice, "lostConsole");
  assert.equal(s.transcript.length, 1,
    "the deploy is still running on their machine: never discard what we saw");
  assert.equal(s.transcript[0].text, "creating");
});

test("the stream closing after the job ended is not a loss", () => {
  // The server sets Connection: close and ends the response when the job is
  // done, so a drop right after the terminal event is the normal path.
  let s = B.reduce(live(), { type: "sse.event", event: { id: 1, type: "done", summary: "up" } });
  s = B.reduce(s, { type: "sse.error" });
  assert.equal(s.name, "finished");
  assert.equal(s.notice, null);
});

// ---- events that cannot be accepted where they arrive --------------------

test("an event a state cannot accept leaves that state untouched", () => {
  const cases = [
    // [state builder, event, the state name that must survive]
    [() => B.initial(), { type: "pair.ok" }, "replay"],
    [() => B.initial(), { type: "pair.denied", status: 401, error: "pairing required" }, "replay"],
    [() => B.initial(), { type: "sse.event", event: { id: 1, type: "log", text: "x" } }, "replay"],
    [() => B.initial(), { type: "sse.error" }, "replay"],
    [() => B.reduce(B.initial(), { type: "probe.ok", body: HEALTH }),
     { type: "sse.event", event: { id: 1, type: "done", summary: "x" } }, "pairing"],
    [() => B.reduce(B.initial(), { type: "probe.ok", body: HEALTH }),
     { type: "sse.error" }, "pairing"],
    [live, { type: "pair.denied", status: 401, error: "pairing required" }, "live"],
    [live, { type: "nonsense" }, "live"],
    [live, {}, "live"],
    [live, null, "live"]
  ];
  for (const [build, event, expected] of cases) {
    const before = build();
    const after = B.reduce(before, event);
    assert.equal(after.name, expected,
      JSON.stringify(event) + " must not move a " + expected + " state");
    assert.deepEqual(after.transcript, before.transcript, "nor add to the transcript");
  }
});

test("reduce never mutates the state it was handed", () => {
  const before = live();
  B.reduce(before, { type: "sse.event", event: { id: 1, type: "log", text: "x" } });
  B.reduce(before, { type: "sse.error" });
  B.reduce(before, { type: "probe.fail", reason: "network" });
  assert.equal(before.name, "live");
  assert.deepEqual(before.transcript, []);
});

// ---- text ----------------------------------------------------------------

test("the machine records string KEYS and text() is the only place they resolve", () => {
  // Nothing user-facing is written in bridge.js: the state carries a key and
  // the table in strings.js owns the sentence.
  const s = B.reduce(B.reduce(B.initial(), { type: "probe.ok", body: HEALTH }),
                     { type: "pair.denied", status: 401, error: "pairing required" });
  assert.equal(s.notice, "pairBad");
  globalThis.CLC_T = (key) => "<<" + key + ">>";
  assert.equal(B.text(s), "<<pairBad>>");
  assert.equal(B.text(B.initial()), null, "no notice is no text, not the empty string");
  delete globalThis.CLC_T;
});

test("a dead-end refusal reaches the visitor even after we were live", () => {
  // A console can disable pairing while we hold a live session: the guess cap
  // is global and cumulative, so a hostile tab on the same machine can spend
  // it. The next acting request then fails forever, and a retry banner would
  // be a lie. A plain wrong-code refusal is different: there is nothing for
  // the visitor to do about it here, and it must not tear down a live view.
  const s = B.reduce(live(), {
    type: "pair.denied", status: 403, error: "pairing disabled, restart the console"
  });
  assert.equal(s.name, "disabled");
  assert.equal(s.notice, "pairDisabled");

  const still = B.reduce(live(), {
    type: "pair.denied", status: 401, error: "pairing required" });
  assert.equal(still.name, "live");
});

// ---- the phase a state puts the page in ----------------------------------
//
// Eight states, three panels. Every pair below is spelled out: reading the
// table out of the implementation and asserting it against itself would pass
// for any mapping at all, including one that showed a pairing box over a
// finished deploy.

test("each state names the panel the page shows, and nothing lands in live by accident", () => {
  const expected = [
    ["replay", "replay"],
    ["pairing", "pairing"],
    // A dead end still belongs on the pairing panel: that is where the reason
    // and the remedy are. The input itself is the caller's branch, not this.
    ["disabled", "pairing"],
    ["refused", "pairing"],
    ["live", "live"],
    // The transcript is what the visitor is reading once a deploy ends.
    // Swapping it for a pairing box would throw away the thing they came for.
    ["finished", "live"],
    ["failed", "live"],
    ["lost", "live"]
  ];
  for (const [name, want] of expected) {
    assert.equal(B.phase({ name: name }), want, name + " belongs on the " + want + " panel");
  }
  assert.equal(B.phase({ name: "something-new" }), "replay",
    "an unknown state falls to the panel that is never wrong");
  assert.equal(B.phase(null), "replay");
});

// ---- which console, and whether this page is one --------------------------

test("a page served over http from loopback was served BY a console", () => {
  assert.equal(B.isSelfHosted({ protocol: "http:", hostname: "127.0.0.1", host: "127.0.0.1:8760" }), true);
  assert.equal(B.isSelfHosted({ protocol: "http:", hostname: "localhost", host: "localhost:8890" }), true);
  assert.equal(B.isSelfHosted({ protocol: "http:", hostname: "[::1]", host: "[::1]:8760" }), true);
  assert.equal(B.isSelfHosted({ protocol: "https:", hostname: "keysight-tech.github.io", host: "keysight-tech.github.io" }), false);
  assert.equal(B.isSelfHosted({ protocol: "https:", hostname: "127.0.0.1", host: "127.0.0.1" }), false);
  assert.equal(B.isSelfHosted({ protocol: "http:", hostname: "example.com", host: "example.com" }), false);
  assert.equal(B.isSelfHosted(null), false);
});

test("the console's own page talks to the port it was actually served on", () => {
  // --port 8890 is a supported bind, and a page that assumed 8760 would probe
  // a port nothing is listening on and report replay over a live console.
  assert.equal(B.baseFor({ protocol: "http:", hostname: "localhost", host: "localhost:8890" }),
               "http://localhost:8890");
  assert.equal(B.baseFor({ protocol: "https:", hostname: "keysight-tech.github.io", host: "keysight-tech.github.io" }),
               "http://127.0.0.1:8760", "a public page can only try the default");
  assert.equal(B.baseFor(null), "http://127.0.0.1:8760");
});

// ---- the transport -------------------------------------------------------
//
// Thin by design, but it is the half that can wire the machine up wrongly, so
// it is driven here with a fake fetch and a fake EventSource. Everything
// asserted below is a literal: the URL, the header name, the state name.

function fakeFetch(routes) {
  const calls = [];
  const fn = (url, init) => {
    calls.push({ url, init: init || {} });
    const r = routes[url.replace("http://127.0.0.1:8760", "")];
    if (!r) { return Promise.reject(new TypeError("Failed to fetch")); }
    return Promise.resolve({
      ok: r.status === 200,
      status: r.status,
      json: () => Promise.resolve(r.body)
    });
  };
  fn.calls = calls;
  return fn;
}

class FakeEventSource {
  constructor(url) { this.url = url; this.listeners = {}; this.closed = false; FakeEventSource.last = this; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  close() { this.closed = true; }
  emit(name, data) { this.listeners[name]({ data: JSON.stringify(data) }); }
}

test("probe hits /health and lands in pairing", async () => {
  const f = fakeFetch({ "/health": { status: 200, body: HEALTH } });
  const b = B.create({ fetch: f, EventSource: FakeEventSource });
  await b.probe();
  assert.equal(f.calls[0].url, "http://127.0.0.1:8760/health");
  assert.equal(b.state().name, "pairing");
});

test("a probe against a dead port lands in replay, and says nothing about it", async () => {
  const b = B.create({ fetch: fakeFetch({}), EventSource: FakeEventSource,
                       location: { protocol: "http:", hostname: "localhost" } });
  await b.probe();
  assert.equal(b.state().name, "replay");
  assert.equal(b.state().notice, null);
});

test("the same failure from a public https page offers the private-network advice", async () => {
  const b = B.create({ fetch: fakeFetch({}), EventSource: FakeEventSource,
                       location: { protocol: "https:", hostname: "keysight-tech.github.io" } });
  await b.probe();
  assert.equal(b.state().name, "replay", "advice or not, it still degrades to replay");
  assert.equal(b.state().notice, "blockedPNA");
});

test("pairing sends the code on X-CloudLens-Pair, byte for byte", async () => {
  const f = fakeFetch({
    "/health": { status: 200, body: HEALTH },
    "/flows": { status: 200, body: { order: [], flows: {} } }
  });
  const b = B.create({ fetch: f, EventSource: FakeEventSource });
  await b.probe();
  await b.pair("ABCD2345");
  const flows = f.calls[1];
  assert.equal(flows.url, "http://127.0.0.1:8760/flows");
  assert.equal(flows.init.headers["X-CloudLens-Pair"], "ABCD2345");
  assert.equal(b.state().name, "live");
});

test("a refused code is read out of the body, not guessed from the status", async () => {
  const f = fakeFetch({
    "/health": { status: 200, body: HEALTH },
    "/flows": { status: 403, body: { error: "pairing disabled, restart the console" } }
  });
  const b = B.create({ fetch: f, EventSource: FakeEventSource });
  await b.probe();
  await b.pair("ABCD2345");
  assert.equal(b.state().name, "disabled");
});

test("input that cannot be the code never reaches the server", async () => {
  // The guess budget is cumulative and never resets, so a stray keystroke must
  // not spend one.
  const f = fakeFetch({ "/health": { status: 200, body: HEALTH } });
  const b = B.create({ fetch: f, EventSource: FakeEventSource });
  await b.probe();
  await b.pair("abc");
  assert.equal(f.calls.length, 1, "only the probe was sent");
  assert.equal(b.state().name, "pairing");
  assert.equal(b.state().notice, "pairBad");
});

// ---- attaching without a code, on the console's own page ------------------

const SELF = { protocol: "http:", hostname: "127.0.0.1", host: "127.0.0.1:8760" };
const PAGES = { protocol: "https:", hostname: "keysight-tech.github.io",
                host: "keysight-tech.github.io" };

test("the console's own page attaches with no code and no pair header", async () => {
  const f = fakeFetch({
    "/health": { status: 200, body: HEALTH },
    "/flows": { status: 200, body: { order: [], flows: {} } }
  });
  const b = B.create({ fetch: f, EventSource: FakeEventSource, location: SELF });
  await b.probe();
  await b.attach();
  assert.equal(f.calls[1].url, "http://127.0.0.1:8760/flows");
  assert.equal(f.calls[1].init.headers, undefined,
    "there is no code to send: server.py exempts its own origin");
  assert.equal(b.state().name, "live");
});

test("an attach the console refuses says nothing about a code the visitor never typed", async () => {
  const f = fakeFetch({
    "/health": { status: 200, body: HEALTH },
    "/flows": { status: 401, body: { error: "pairing required" } }
  });
  const b = B.create({ fetch: f, EventSource: FakeEventSource, location: SELF });
  await b.probe();
  await b.attach();
  assert.equal(b.state().name, "pairing", "the code box stays up and waits");
  assert.equal(b.state().notice, null, "'that code did not match' would be about nothing");
});

test("a public page never attaches: that request would be a guess", async () => {
  // The guess budget is cumulative and never resets, so an automatic attempt
  // from a page the pairing exemption does not cover would spend one of the
  // visitor's 200 on every single page load.
  const f = fakeFetch({
    "/health": { status: 200, body: HEALTH },
    "/flows": { status: 200, body: { order: [], flows: {} } }
  });
  const b = B.create({ fetch: f, EventSource: FakeEventSource, location: PAGES });
  await b.probe();
  await b.attach();
  assert.equal(f.calls.length, 1, "only the probe was sent");
  assert.equal(b.state().name, "pairing");
});

test("the console's fixture replay is asked for explicitly on every run", async () => {
  // server.py replays fixtures/<flow>.json instead of calling AWS when this is
  // truthy. It is the operator's guard against a demo click spending real
  // money, so a run that omitted it would silently be the expensive one.
  const routes = {
    "/health": { status: 200, body: HEALTH },
    "/flows": { status: 200, body: { order: [], flows: {} } },
    "/run": { status: 200, body: { job_id: "0123456789abcdef0123456789abcdef", replay: false } }
  };
  for (const [asked, wanted] of [[true, true], [false, false], [undefined, false]]) {
    const f = fakeFetch(routes);
    const b = B.create({ fetch: f, EventSource: FakeEventSource });
    await b.probe();
    await b.pair("ABCD2345");
    await b.run("stack", { region: "us-east-1" }, asked);
    const body = JSON.parse(f.calls[2].init.body);
    assert.equal(body.replay, wanted);
    assert.equal(body.flow, "stack");
    assert.deepEqual(body.inputs, { region: "us-east-1" });
  }
});

test("run streams /events/<job_id> with no header on it at all", async () => {
  const f = fakeFetch({
    "/health": { status: 200, body: HEALTH },
    "/flows": { status: 200, body: { order: [], flows: {} } },
    "/run": { status: 200, body: { job_id: "0123456789abcdef0123456789abcdef", replay: false } }
  });
  const b = B.create({ fetch: f, EventSource: FakeEventSource });
  await b.probe();
  await b.pair("ABCD2345");
  await b.run("stack", {});
  const es = FakeEventSource.last;
  assert.equal(es.url, "http://127.0.0.1:8760/events/0123456789abcdef0123456789abcdef",
    "the id is the capability: EventSource cannot send a header");
  // Every frame the console sends names its type, so "message" alone is deaf.
  for (const name of ["hello", "log", "state", "narrate", "stat", "done", "error"]) {
    assert.equal(typeof es.listeners[name], "function", name + " needs its own listener");
  }
  es.emit("log", { id: 1, type: "log", text: "creating" });
  assert.equal(b.state().transcript.length, 1);
  es.emit("done", { id: 2, type: "done", summary: "up" });
  assert.equal(b.state().name, "finished");
  assert.equal(es.closed, true,
    "EventSource reconnects on its own, and a reconnect would replay the buffer");
});
