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
const HEALTH = { ok: true, version: "1.0" };

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
  const impostors = [
    { ok: true },                                  // no version
    { ok: true, version: "1.0", extra: "x" },      // not the closed key set
    { version: "1.0" },                            // no ok
    { ok: "true", version: "1.0" },                // ok not a boolean
    { ok: true, version: "v1.0" },                 // not a wire-contract number
    { ok: true, version: "1.0.3" },                // ditto: N.N only
    { ok: true, version: 1.0 },                    // not a string
    { ok: false, version: "1.0" },
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
