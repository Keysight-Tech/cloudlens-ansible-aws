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
  //   {"ok": true, "service": "cloudlens-console", "version": "1.0"}
  // and nothing else, by design - it must leak no account, region, hostname or
  // path to an unauthenticated prober.
  //
  // "service" is the identification, and it is a fact rather than a guess: the
  // console states what it is, and this compares that string exactly. Without
  // it there was nothing to match on but the SHAPE of a two-key body, and any
  // dev server answering {ok:true, version:"1.0"} was indistinguishable from
  // our console - a false positive that puts the visitor in front of a pairing
  // prompt a stranger can never accept.
  //
  // The rest of the shape is still held exactly, because a body that carries
  // our name and then does not look like ours is a mismatch worth refusing:
  // the closed key set, ok strictly boolean true, and version the wire-contract
  // number (N.N, never a SHA or a build id, for the fingerprinting reason).
  // Note the Server header would be a cleaner marker but is not readable
  // cross-origin, so the body is all a public page gets.
  var SERVICE = "cloudlens-console";
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
    if (keys.length !== 3) { return false; }
    if (body.service !== SERVICE) { return false; }
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
        var d = DENIALS[event.error] || DENIAL_DEFAULT;
        // A dead end is a dead end wherever it arrives. The guess cap is
        // global and cumulative, so another tab on the same machine can
        // disable pairing while we hold a live session, and every later
        // acting request fails permanently: the visitor has to be told.
        // The retry case is the opposite - outside pairing there is nothing
        // to retype, and a wrong-code banner must not tear down a live view.
        if (d === DENIAL_DEFAULT && state.name !== STATES.PAIRING) {
          return next(state, {});
        }
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

  // What the PAGE is doing, which is not the same as which of the eight states
  // the machine is in. Three panels, and several states share one:
  //
  //   a finished, failed or lost deploy is still the LIVE view - the transcript
  //   is what the visitor is reading, and swapping it for a pairing box would
  //   throw away the thing they came for.
  //   a disabled or refused console is still the PAIRING panel - that is where
  //   the explanation belongs - but with nothing left to type, which is why the
  //   caller must still branch on the state name for the input itself.
  //
  // Spelled out per state rather than derived, so a new state is a missing key
  // (and falls to replay, the safe panel) instead of silently landing in live.
  var PHASES = {
    replay: "replay",
    pairing: "pairing", disabled: "pairing", refused: "pairing",
    live: "live", finished: "live", failed: "live", lost: "live"
  };

  function phase(state) {
    return (state && PHASES[state.name]) || "replay";
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

  // ---- the transport ------------------------------------------------------
  //
  // The only half that touches the network. It turns outcomes into machine
  // events and decides nothing else: no state name and no string key is
  // chosen down here.

  var DEFAULT_BASE = "http://127.0.0.1:8760";

  // Named events, from events.py. EventSource fires "message" only for frames
  // with no event: line, and every frame the console sends names its type, so
  // each one needs its own listener or the stream looks silent.
  var EVENT_TYPES = ["hello", "log", "state", "narrate", "stat", "done", "error"];

  // Frames that end the job. Kept beside EVENT_TYPES so the transport knows
  // when to close the socket, while endedBy() stays the machine's own rule.
  function isTerminalFrame(ev) { return endedBy(ev) !== null; }

  // Whether a failed probe is worth blaming on Private Network Access.
  //
  // A GUESS, and it is allowed to be one: it only ever picks between saying
  // nothing and offering advice, because both paths land in replay. Chrome
  // gives a public page no way to tell the PNA block from a closed port - both
  // reject with the same TypeError - so the only signal available is that we
  // are the kind of page the block applies to: a secure, non-loopback origin
  // reaching loopback. Never let this read as a diagnosis.
  function looksBlocked(loc) {
    if (!loc) { return false; }
    if (loc.protocol !== "https:") { return false; }
    return loc.hostname !== "localhost" && loc.hostname.indexOf("127.") !== 0;
  }

  // The pairing header. The visitor's code travels on it for every acting
  // request; the SSE stream cannot carry it and does not need to.
  var PAIR_HEADER = "X-CloudLens-Pair";

  // ---- where the console is, and whether this page IS the console ---------
  //
  // The console serves this very file at /web/app.js, so a page loaded over
  // http from a loopback host was served BY a console, and its own origin is
  // that console - whatever --port it was given. Any other page has no way to
  // learn the port and can only try the default.
  var LOOPBACK_HOSTS = { "localhost": 1, "::1": 1, "[::1]": 1 };

  function isLoopbackHost(h) {
    return typeof h === "string" &&
           (LOOPBACK_HOSTS[h] === 1 || h.indexOf("127.") === 0);
  }

  // NOT a permission, and it grants nothing. server.py exempts its own origins
  // from pairing because local code already has the shell and the AWS identity;
  // this only records that the exemption is the one about to apply, so the
  // console's own page can attach instead of asking for a code that would not
  // be compared against anything.
  function isSelfHosted(loc) {
    return !!(loc && loc.protocol === "http:" && isLoopbackHost(loc.hostname));
  }

  function baseFor(loc) {
    return isSelfHosted(loc) ? loc.protocol + "//" + loc.host : DEFAULT_BASE;
  }

  function create(opts) {
    opts = opts || {};
    var base = opts.base || DEFAULT_BASE;
    var fetchFn = opts.fetch || (typeof fetch !== "undefined" ? fetch : null);
    var ES = opts.EventSource || (typeof EventSource !== "undefined" ? EventSource : null);
    var loc = opts.location || (typeof location !== "undefined" ? location : null);
    var state = initial();
    var code = null;
    var stream = null;

    function dispatch(event) {
      state = reduce(state, event);
      if (opts.onState) { opts.onState(state, event); }
      return state;
    }

    function headers() {
      var h = { "Content-Type": "application/json" };
      if (code) { h[PAIR_HEADER] = code; }
      return h;
    }

    // The refusal body, or "" for anything unreadable. The machine falls back
    // to "wrong code" on an unrecognised value, so an unparseable body is safe
    // here: it can only ever be less specific, never wrongly reassuring.
    function denial(r) {
      return r.json().then(function (b) {
        return (b && typeof b.error === "string") ? b.error : "";
      }, function () { return ""; });
    }

    return {
      state: function () { return state; },

      // Is a console there? Answers into the machine, never by throwing: a
      // rejection here is the ordinary case for the overwhelming majority of
      // visitors, who have no console running at all.
      probe: function () {
        if (!fetchFn) {
          dispatch({ type: "probe.fail", reason: "network" });
          return Promise.resolve(state);
        }
        return fetchFn(base + "/health", { mode: "cors", cache: "no-store" })
          .then(function (r) {
            if (!r.ok) { throw new Error("health"); }
            return r.json();
          })
          .then(function (body) { return dispatch({ type: "probe.ok", body: body }); },
                function () {
                  return dispatch({
                    type: "probe.fail",
                    reason: looksBlocked(loc) ? "blocked" : "network"
                  });
                });
      },

      // Pair with no code at all, for the console's OWN page.
      //
      // On that page the code box is theatre: the request carries a loopback
      // origin, server.py exempts it, and any eight characters would be
      // "accepted". Asking for one would teach the visitor that a code they
      // made up works.
      //
      // Refuses to leave a page the exemption does not cover, because there
      // the same request is a GUESS: the budget is cumulative and never resets,
      // so an automatic attempt would spend one of the visitor's 200 on every
      // page load, and buy a half-second delay for it.
      //
      // A refusal dispatches NOTHING. The visitor typed no code, so "that code
      // did not match" would be a sentence about something they never did:
      // pairing simply stays up and waits for one.
      attach: function () {
        if (!isSelfHosted(loc)) { return Promise.resolve(state); }
        return fetchFn(base + "/flows", { mode: "cors", cache: "no-store" })
          .then(function (r) {
            if (!r.ok) { return state; }
            return r.json().then(function (flows) {
              return dispatch({ type: "pair.ok", flows: flows });
            });
          }, function () { return state; });
      },

      // Try the code the visitor typed. /flows is the cheapest paired route
      // and has no side effect, so a wrong guess costs a rejected read rather
      // than a half-started deploy.
      pair: function (typed) {
        if (!looksLikeCode(typed)) {
          return Promise.resolve(dispatch({
            type: "pair.denied", status: 0, error: ""
          }));
        }
        code = typed;
        return fetchFn(base + "/flows", { mode: "cors", cache: "no-store",
                                          headers: headers() })
          .then(function (r) {
            if (r.ok) {
              return r.json().then(function (flows) {
                return dispatch({ type: "pair.ok", flows: flows });
              });
            }
            code = null;
            return denial(r).then(function (err) {
              return dispatch({ type: "pair.denied", status: r.status, error: err });
            });
          }, function () {
            code = null;
            return dispatch({
              type: "probe.fail", reason: looksBlocked(loc) ? "blocked" : "network"
            });
          });
      },

      // Start a real deploy and stream it.
      //
      // `replay` is the CONSOLE's own fixture replay (server.py: /run replays
      // fixtures/<flow>.json instead of calling AWS), and has nothing to do
      // with the page's replay state. It is the operator's guard against a
      // demo click spending real money, so it travels explicitly on every run
      // rather than being defaulted anywhere.
      run: function (flow, inputs, replay) {
        return fetchFn(base + "/run", {
          method: "POST", mode: "cors", headers: headers(),
          body: JSON.stringify({ flow: flow, inputs: inputs || {},
                                 replay: !!replay })
        }).then(function (r) {
          if (!r.ok) {
            return denial(r).then(function (err) {
              return dispatch({ type: "pair.denied", status: r.status, error: err });
            });
          }
          return r.json().then(function (b) {
            dispatch({ type: "job.started", jobId: b.job_id });
            open(b.job_id);
            return state;
          });
        }, function () {
          return dispatch({ type: "sse.error" });
        });
      },

      stop: function () {
        if (!state.jobId) { return Promise.resolve(state); }
        return fetchFn(base + "/stop/" + state.jobId, {
          method: "POST", mode: "cors", headers: headers()
        }).then(function () { return state; }, function () { return state; });
      },

      close: close
    };

    // The job id IS the authorisation to read this stream, which is why no
    // header is set here and none can be: EventSource cannot send one.
    function open(jobId) {
      close();
      if (!ES) { dispatch({ type: "sse.error" }); return; }
      stream = new ES(base + "/events/" + jobId);
      EVENT_TYPES.forEach(function (name) {
        stream.addEventListener(name, function (m) {
          var ev;
          try { ev = JSON.parse(m.data); } catch (e) { return; }
          dispatch({ type: "sse.event", event: ev });
          // Close on the last frame rather than waiting for the server's
          // close: EventSource reconnects on its own, and a reconnect after a
          // finished job would replay the whole buffer into the transcript.
          if (isTerminalFrame(ev)) { close(); }
        });
      });
      stream.onerror = function () {
        // Also fired on a retryable hiccup, but the browser reconnects on its
        // own and would deliver the same frames twice into the transcript.
        // One reader, one connection: report the drop and let the visitor
        // reconnect deliberately.
        close();
        dispatch({ type: "sse.error" });
      };
    }

    function close() {
      if (stream) { stream.close(); stream = null; }
    }
  }

  return {
    STATES: STATES,
    text: text,
    phase: phase,
    create: create,
    isOurConsole: isOurConsole,
    looksLikeCode: looksLikeCode,
    isSelfHosted: isSelfHosted,
    baseFor: baseFor,
    initial: initial,
    reduce: reduce
  };
}));
