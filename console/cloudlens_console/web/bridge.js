// The bridge between the public docs page and a console running on the
// visitor's own machine.
//
// The page ships as a REPLAY of captured real events and must keep working
// that way for every visitor forever. If a console happens to be listening on
// 127.0.0.1, the same UI can instead drive a real deploy on that machine and
// stream its real output. Three states the visitor can be in:
//
//   replay  - no console found. What ships today, and the fallback from every
//             failure path. Never an error.
//   pairing - a console answered /health, and we need the 8 characters it
//             printed in its terminal before it will act for us.
//   live    - paired, streaming real Server-Sent Events off that machine.
//
// This file is two halves, deliberately separated:
//
//   the MACHINE - pure. (state, event) -> state. No fetch, no DOM, no timers,
//                 no clock. Every rule about what the visitor sees lives here,
//                 which is what makes it testable in node with synthetic
//                 events (see tests/test_bridge.mjs).
//   the TRANSPORT - the only part that touches fetch/EventSource. It turns
//                 network outcomes into machine events and nothing else.
//
// No user-facing text appears here. The machine records a KEY, and the one
// place text is produced is text(), which asks window.CLC_T for it.
//
// ES5 on purpose: loaded with a bare <script> tag, no build step. The wrapper
// hands the same object to a browser (window.CLC_BRIDGE) and to node
// (module.exports), so the test imports the exact file the page loads.
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module && module.exports) {
    module.exports = api;
  }
  if (root) { root.CLC_BRIDGE = api; }
}(typeof window !== "undefined" ? window : null, function () {
  "use strict";

  // Every state the visitor can be in. Spelled out rather than inferred, so a
  // typo is a missing key rather than a silent new state.
  var STATES = {
    REPLAY: "replay",       // no live console: play the captured events
    PAIRING: "pairing",     // a console is there, waiting on the code
    LIVE: "live",           // paired, streaming a real deploy
    FINISHED: "finished",   // the deploy ended, transcript kept
    FAILED: "failed",       // the deploy ended badly, transcript kept
    LOST: "lost",           // the console went away mid-job, transcript kept
    DISABLED: "disabled",   // the console stopped accepting codes until restart
    REFUSED: "refused"      // the console refused the request as unsafe
  };

  // ---- identifying OUR console -------------------------------------------
  //
  // /health is the only unauthenticated route, and it answers a closed body:
  //   {"ok": true, "version": "1.0"}
  // and nothing else, by design - it must leak no account, region, hostname or
  // path to an unauthenticated prober. That closed shape is the whole of what
  // we have to recognise ourselves by, so we hold it exactly: both keys, no
  // third key, ok strictly boolean true, and version the wire-contract number
  // (N.N, never a SHA or a build id, for the same fingerprinting reason).
  //
  // The point is the negative case. A random dev server on the same port is
  // reachable, speaks JSON and is not us; treating it as us would put the
  // visitor in front of a pairing prompt that can never succeed. Note the
  // Server header would be a cleaner marker but is not readable cross-origin,
  // so the body is all a public page gets.
  var VERSION_RE = /^\d+\.\d+$/;

  // The major version we know how to talk to. A console announcing 2.x speaks
  // a contract this file has not been written against, so it is not ours to
  // drive: better replay than a half-understood live deploy.
  var WIRE_MAJOR = "1";

  function isOurConsole(body) {
    if (!body || typeof body !== "object" || Object.prototype.toString.call(body) === "[object Array]") {
      return false;
    }
    var keys = [];
    for (var k in body) {
      if (Object.prototype.hasOwnProperty.call(body, k)) { keys.push(k); }
    }
    if (keys.length !== 2) { return false; }
    if (body.ok !== true) { return false; }
    if (typeof body.version !== "string" || !VERSION_RE.test(body.version)) { return false; }
    return body.version.split(".")[0] === WIRE_MAJOR;
  }

  // ---- the pairing code ---------------------------------------------------
  //
  // 8 characters over an alphabet with no O/0 and no I/1, because a human
  // reads it off a terminal and types it into a browser. Checked here only to
  // avoid spending one of a cumulative, never-reset guess budget on input that
  // cannot possibly be the code.
  //
  // NOT normalised, and specifically not upper-cased. The server compares with
  // hmac.compare_digest, which is byte for byte, so anything this function
  // "helpfully" rewrote would be a different code on the wire.
  var CODE_RE = /^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{8}$/;

  function looksLikeCode(s) {
    return typeof s === "string" && CODE_RE.test(s);
  }

  // What the server answers with when it will not act for us. Three distinct
  // situations that must never be shown as one:
  //
  //   401 "pairing required"  - the code did not match. Retry is the remedy,
  //                             and the visitor stays in pairing.
  //   403 "pairing disabled, restart the console" - the guess cap fired and
  //                             the process discarded its code permanently.
  //                             Retyping the RIGHT code now fails forever, so
  //                             telling the visitor to check their typing is
  //                             actively wrong: only a restart helps.
  //   403 "bad host"          - the request was addressed to a name that is
  //                             not the console's own, so it refused it as
  //                             unsafe before pairing was even consulted.
  //
  // Matched on the body, never on the status: the last two are both 403.
  var DENIALS = {
    "pairing disabled, restart the console": { name: STATES.DISABLED, notice: "pairDisabled" },
    "bad host": { name: STATES.REFUSED, notice: "hostRefused" }
  };

  // Anything we do not recognise, including a 500 or an empty body, falls here.
  // The safe default is "not paired": the alternative is to treat an
  // unexplained refusal as success and then show a live badge over a UI that
  // is in fact still replaying.
  var DENIAL_DEFAULT = { name: STATES.PAIRING, notice: "pairBad" };

  // The state a stream event ends the job in, or null when the job carries on.
  //
  // `done` is terminal. `error` is terminal only when it names no node:
  // events.py reports a failed resource with error(node=...) while the deploy
  // continues, so treating every error as the end would black out the rest of
  // a live deploy over a failure the operator can watch recover.
  function endedBy(ev) {
    if (!ev || typeof ev.type !== "string") { return null; }
    if (ev.type === "done") { return STATES.FINISHED; }
    if (ev.type === "error" && ev.node === undefined) { return STATES.FAILED; }
    return null;
  }

  // ---- the machine --------------------------------------------------------

  function initial() {
    return {
      name: STATES.REPLAY,
      notice: null,       // a key for window.CLC_T, never a sentence
      transcript: [],     // every SSE event we accepted, in order
      jobId: null,
      version: null
    };
  }

  // A new state object every time. Never mutate the one handed in: the caller
  // may still be holding it, and a reducer that edits its input cannot be
  // reasoned about from the test alone.
  function next(state, over) {
    var out = {
      name: state.name,
      notice: state.notice,
      transcript: state.transcript,
      jobId: state.jobId,
      version: state.version
    };
    for (var k in over) {
      if (Object.prototype.hasOwnProperty.call(over, k)) { out[k] = over[k]; }
    }
    return out;
  }

  function reduce(state, event) {
    if (!state) { state = initial(); }
    if (!event || typeof event.type !== "string") { return state; }

    switch (event.type) {

      // A console answered. Only ours earns a pairing prompt.
      case "probe.ok":
        if (!isOurConsole(event.body)) { return next(state, {}); }
        if (state.name === STATES.PAIRING || state.name === STATES.LIVE) {
          return next(state, { version: event.body.version });
        }
        return next(state, {
          name: STATES.PAIRING, notice: null, version: event.body.version
        });

      // The probe did not answer. This is NOT evidence that no console is
      // running: Private Network Access blocks the request before it leaves
      // the browser and reports the same generic failure. So it degrades to
      // replay, and only says something when the transport could attribute it.
      case "probe.fail":
        return next(state, {
          name: STATES.REPLAY,
          notice: event.reason === "blocked" ? "blockedPNA" : null,
          jobId: null,
          version: null
        });

      // The code was accepted: an acting route answered 200.
      case "pair.ok":
        if (state.name !== STATES.PAIRING) { return next(state, {}); }
        return next(state, { name: STATES.LIVE, notice: "connected" });

      // The console would not act for us. Which of the three it is decides
      // whether the visitor has anything left to try.
      case "pair.denied":
        if (state.name !== STATES.PAIRING) { return next(state, {}); }
        var d = DENIALS[event.error] || DENIAL_DEFAULT;
        return next(state, { name: d.name, notice: d.notice });

      // /run handed back an id. It is a capability, not a label: /events/<id>
      // has no pairing check because EventSource cannot send headers, so
      // holding the id IS the authority to read that deploy.
      case "job.started":
        if (state.name !== STATES.LIVE) { return next(state, {}); }
        return next(state, { jobId: event.jobId, transcript: [] });

      // One frame off the real stream. Appended to a COPY: a state object
      // already handed to a renderer must not change under it.
      case "sse.event":
        if (state.name !== STATES.LIVE || !event.event) { return next(state, {}); }
        var kept = state.transcript.slice();
        kept.push(event.event);
        var ended = endedBy(event.event);
        // The notice is cleared on the way out. It still says "connected" from
        // pairing, and a finished job carrying a banner about the connection
        // reads as though something is still running.
        return next(state, {
          name: ended || state.name,
          notice: ended ? null : state.notice,
          transcript: kept
        });

      // The stream broke. After the job ended this is the normal close (the
      // server sends Connection: close and stops), so it is only a loss while
      // the job was still running. The transcript is kept either way: the
      // deploy is still running on their machine, and throwing away what we
      // saw of it helps nobody.
      case "sse.error":
        if (state.name !== STATES.LIVE) { return next(state, {}); }
        return next(state, { name: STATES.LOST, notice: "lostConsole" });
    }
    return next(state, {});
  }

  // The ONE place a state turns into words. The machine only ever records a
  // key, so every sentence the visitor reads comes out of the table in
  // strings.js and can be translated by adding a sibling language to it.
  // Returns null rather than "" for a state with nothing to say, so a caller
  // can tell "no message" from "an empty message".
  function text(state) {
    if (!state || !state.notice) { return null; }
    var t = (typeof window !== "undefined" ? window.CLC_T : null) ||
            (typeof globalThis !== "undefined" ? globalThis.CLC_T : null);
    return t ? t(state.notice) : state.notice;
  }

  return {
    STATES: STATES,
    text: text,
    isOurConsole: isOurConsole,
    looksLikeCode: looksLikeCode,
    initial: initial,
    reduce: reduce
  };
}));
