// The page, wired to the bridge.
//
// The bridge decides which of three things the visitor is looking at, and
// everything below is the rendering of that decision:
//
//   replay  - nothing answered on 127.0.0.1. The captured events play locally
//             and the panel quietly offers the real thing. NEVER an error:
//             Chrome's Private Network Access refuses a public page's request
//             to loopback with the same generic failure a closed port gives,
//             so "no console" and "blocked" are indistinguishable from here
//             and neither is the visitor's fault.
//   pairing - a console answered /health and named itself. Ask for the eight
//             characters it printed.
//   live    - paired. Real Server-Sent Events off a real deploy.
//
// No sentence the visitor reads is written in this file. The furniture goes
// through CLC_T directly; anything the machine has to say about the connection
// comes back from CLC_BRIDGE.text(), which is the bridge's only door to the
// string table.
//
// ES5 on purpose: three bare <script> tags, no build step.
(function () {
"use strict";
var B = window.CLC_BRIDGE, T = window.CLC_T;
var reduce = window.matchMedia("(prefers-reduced-motion:reduce)").matches;
var $ = function (id) { return document.getElementById(id); };

/* theme */
$("themeBtn").addEventListener("click", function () {
  var cur = document.documentElement.getAttribute("data-theme") || "light";
  var nxt = cur === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", nxt);
  try { localStorage.setItem("cl-theme", nxt); } catch (e) {}
});

// embed mode (iframed into the main doc): hide the chrome, report our height.
// The console's own page can never be framed - server.py sends
// frame-ancestors 'none' - so this only ever runs on the built public page.
if (/embed/.test(location.search)) {
  document.body.classList.add("embed");
  var ph = function () { try { parent.postMessage({ clc: document.body.scrollHeight }, "*"); } catch (e) {} };
  window.addEventListener("load", ph);
  window.addEventListener("resize", ph);
  setInterval(ph, 900);
}

/* ---- where the events come from ----------------------------------------
 *
 * Flow metadata: the built public page carries it inline because there is no
 * server to ask. A real console hands over its own on pair.ok and that one
 * wins - it is the process that will actually run them, and its list is the
 * one that is true right now.
 *
 * Fixtures are the replay. Only the built page has them; on the console's own
 * page there is nothing to replay and nothing to replay it for. */
var FLOWS = {}, ORDER = [];
var FIXTURES = window.__FIXTURES__ || null;
if (window.__FLOWS__) { FLOWS = window.__FLOWS__.flows; ORDER = window.__FLOWS__.order; }

var bridge = B.create({ base: B.baseFor(location), onState: onState });

/* ---- what the page is doing --------------------------------------------- */
var phase = "replay";      // replay | pairing | live, from B.phase()
var current = null;        // selected flow id
var nodeEls = {};
var timer = null, t0 = 0, conLines = 0;
var running = false;       // a run is in flight, live or replayed
var started = false;       // /run answered with a job id: something IS running
var stopping = false;      // the visitor pressed Stop, so an end is not a fault
var checking = false;      // a pairing attempt is in flight
var drawn = 0;             // how much of the machine's transcript is on screen
var gen = 0;               // cancels an in-flight fixture replay

/* theme-independent demo guard. It is the operator's protection against a
 * click on a paired console spending real AWS money, so it is only shown
 * where it can actually guard something: an inert switch that claims to
 * control something is worse than no switch. */
var demoOn = true;
$("demoSw").addEventListener("click", function () { setDemo(!demoOn); });
$("demoSw").addEventListener("keydown", function (e) {
  if (e.key === " " || e.key === "Enter") { e.preventDefault(); setDemo(!demoOn); }
});
function setDemo(v) {
  demoOn = v;
  $("demoSw").setAttribute("aria-checked", v ? "true" : "false");
  renderPanel(bridge.state());
}

/* icons */
var IC={
 vpc:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 9h18M9 3v18"/></svg>',
 clms:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
 kvo:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4M5 5l3 3M16 16l3 3M19 5l-3 3M8 16l-3 3"/></svg>',
 vpb:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 7V5M12 7V5M17 7V5M7 17v2M12 17v2M17 17v2"/></svg>',
 vm:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="4" y="4" width="16" height="12" rx="2"/><path d="M2 20h20"/></svg>',
 tool:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M14 6l4 4-8 8-4 1 1-4z"/><path d="M4 20h6"/></svg>',
 mirror:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3v18M7 8l-4 4 4 4M17 8l4 4-4 4"/></svg>',
 coll:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3a4 4 0 0 0-4 4H6a3 3 0 0 0 0 6h12a3 3 0 0 0 0-6h-2a4 4 0 0 0-4-4z"/><path d="M9 17l3 4 3-4"/></svg>'
};
var TONE={info:"i",good:"✓",note:"·",warn:"!",err:"✕"};

/* ---- the bridge's only entry point into the page ------------------------ */
function onState(s, ev) {
  phase = B.phase(s);
  if (ev && ev.type === "pair.ok" && ev.flows && ev.flows.order) { adoptFlows(ev.flows); }
  if (ev && ev.type === "job.started") { started = true; }
  // Draw the machine's TRANSCRIPT, never the raw event that just arrived. The
  // machine refuses frames that reach it outside a live state, and rendering
  // the event directly drew the refused ones anyway: the page filled with a
  // deploy the bridge had already decided it was not watching.
  if (s.transcript.length < drawn) { drawn = 0; }   // job.started emptied it
  while (drawn < s.transcript.length) { frame(s.transcript[drawn++]); }
  // The machine's terminal states are the run's, so the instrument follows
  // them rather than watching the stream for a second time.
  if (running && (s.name === "finished" || s.name === "failed" || s.name === "lost")) {
    finish(s.name);
  }
  renderPanel(s);
}

/* ---- the panel: the offer, the code box, or nothing --------------------- */
function noticeText(s) {
  // "Lost the local console. The transcript below is kept." is true only if
  // something was running to keep. A /run that never reached the console
  // started nothing, and there is no transcript under this sentence.
  if (s.name === "lost" && !started) { return T("transportFailed"); }
  return B.text(s);
}

function renderPanel(s) {
  var name = s.name, ph = B.phase(s);
  // The badge states a FACT and is not a mode the visitor picks: real AWS only
  // when a console is driving us AND it was not asked to replay fixtures.
  $("modeBadge").textContent = (ph === "live" && !demoOn) ? T("liveBadge") : T("replayBadge");
  $("demoWrap").hidden = (ph !== "live");

  // The offer, and the command that makes it true. The command is also the
  // remedy for a console that disabled pairing (restart it), and is withheld
  // from a console that refused the request as unsafe - that one is running
  // perfectly well and starting a second changes nothing.
  $("link").hidden = (ph === "live");
  $("linkTitle").textContent = T("goLiveTitle");
  $("linkBody").textContent = T("goLiveBody");
  $("linkCmd").textContent = T("goLiveCommand");
  $("linkCmdWrap").hidden = (name === "refused");
  if (!checking) { $("copyBtn").textContent = T("copyCommand"); }

  // The code box appears only where typing again can work. Once the guess cap
  // has fired the console has discarded its code for good, so retyping the
  // RIGHT one fails forever: leaving the box up is exactly the dead end the
  // disabled state exists to spare the visitor.
  $("pairForm").hidden = (name !== "pairing");
  $("pairLabel").textContent = T("pairPrompt");
  $("pairInput").placeholder = T("pairPlaceholder");
  $("pairHelp").textContent = T("pairHelp");
  if (!checking) { $("pairBtn").textContent = T("pairSubmit"); }

  $("notice").textContent = noticeText(s) || "";
  $("runBtn").disabled = running || !canRun();
}

// Something can be run when a console will run it, or when there are captured
// events to play. Neither: the control is disabled rather than lying.
function canRun() { return ORDER.length > 0 && (phase === "live" || !!FIXTURES); }

/* ---- pairing ------------------------------------------------------------ */
$("pairForm").addEventListener("submit", function (e) {
  e.preventDefault();     // Enter in the field submits, as it should
  submitCode();
});

$("pairInput").addEventListener("input", function () {
  // Upper-cased IN THE FIELD, not on the way out. The console's alphabet is
  // upper case with no O/0 and no I/1, so lower case can never be a code, and
  // the bridge would refuse it locally with "that code did not match" - a
  // sentence that is wrong about a correct code typed with caps lock off.
  // Doing it here rather than in the transport keeps what is sent identical to
  // what the visitor can see, which is the reason bridge.js refuses to
  // normalise anything itself.
  var at = this.selectionStart;
  var v = this.value.replace(/\s+/g, "").toUpperCase();
  if (v !== this.value) {
    this.value = v;
    try { this.setSelectionRange(at, at); } catch (e) {}
  }
});

function submitCode() {
  if (checking) { return; }
  checking = true;
  $("pairInput").disabled = true;
  $("pairBtn").disabled = true;
  // The console sleeps half a second on a wrong code, so the wait is real and
  // has to be visible or the button looks dead.
  $("pairBtn").textContent = T("pairChecking");
  bridge.pair($("pairInput").value).then(function (s) {
    checking = false;
    $("pairInput").disabled = false;
    $("pairBtn").disabled = false;
    $("pairBtn").textContent = T("pairSubmit");
    if (s.name === "pairing") { $("pairInput").select(); $("pairInput").focus(); }
    renderPanel(s);
  });
}

$("copyBtn").addEventListener("click", function () {
  var txt = T("goLiveCommand");
  var ok = function () {
    $("copyBtn").textContent = T("copiedCommand");
    setTimeout(function () { $("copyBtn").textContent = T("copyCommand"); }, 1600);
  };
  var legacy = function () {
    var ta = document.createElement("textarea");
    ta.value = txt; ta.setAttribute("readonly", "");
    ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { if (document.execCommand("copy")) { ok(); } } catch (e) {}
    document.body.removeChild(ta);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(txt).then(ok, legacy);
  } else { legacy(); }
});

/* ---- flows -------------------------------------------------------------- */
function adoptFlows(d) {
  FLOWS = d.flows; ORDER = d.order;
  renderFlows();
}

function renderFlows() {
  var tabs = $("flows"), cards = $("flowCards");
  tabs.innerHTML = ""; cards.innerHTML = "";
  ORDER.forEach(function (id, i) {
    var f = FLOWS[id];
    var b = document.createElement("button");
    b.className = "flow"; b.setAttribute("role", "tab"); b.type = "button";
    b.setAttribute("aria-selected", "false"); b.dataset.flow = id;
    b.innerHTML = '<div class="fn">FLOW 0' + (i + 1) + '</div><div class="ft">' + esc(f.name) + '</div>';
    b.addEventListener("click", function () { selectFlow(id); });
    tabs.appendChild(b);
    var c = document.createElement("div");
    c.className = "card";
    c.innerHTML = '<div class="k">FLOW 0' + (i + 1) + '</div><h3>' + esc(f.name) +
                  '</h3><p>' + esc(f.subtitle) + '</p>';
    cards.appendChild(c);
  });
  // Keep what the visitor had selected if the console still offers it.
  selectFlow(ORDER.indexOf(current) > -1 ? current : ORDER[0]);
}

function selectFlow(id) {
  if (!id) { return; }
  gen++; running = false; stopTimer();
  current = id;
  var f = FLOWS[id];
  document.querySelectorAll(".flow").forEach(function (b) {
    b.setAttribute("aria-selected", b.dataset.flow === id ? "true" : "false");
  });
  $("instName").textContent = f.script;
  $("cfgTitle").textContent = T("inputsTitle");
  $("cfgSub").textContent = "· " + f.name;
  var fl = $("fields"); fl.innerHTML = "";
  f.inputs.forEach(function (fd) {
    var d = document.createElement("div"); d.className = "field";
    var iid = "in-" + fd.key;
    d.innerHTML = '<label for="' + esc(iid) + '">' + esc(fd.label) + '</label>' +
      '<input id="' + esc(iid) + '" data-k="' + esc(fd.key) + '" value="' + esc(fd.default || "") +
      '" placeholder="' + esc(fd.placeholder || "") + '" spellcheck="false">';
    fl.appendChild(d);
  });
  resetInstrument();
  layoutDiagram(f);
  $("runTxt").textContent = T("runStart");
  renderPanel(bridge.state());
}

/* ---- the instrument ----------------------------------------------------- */
function resetInstrument() {
  $("console").innerHTML = ""; conLines = 0; $("conCount").textContent = "";
  $("narr").innerHTML = '<div class="empty">' + esc(T("transcriptEmpty")) + '</div>';
  setPill("idle", T("statusIdle"));
  $("mElapsed").textContent = "0:00"; $("mCreated").textContent = "0";
  $("idChip").hidden = true; $("stopBtn").hidden = true;
  $("stopBtn").textContent = T("stopRun");
}
function setPill(cls, txt) {
  $("statusPill").className = "pill" + (cls && cls !== "idle" ? " " + cls : "");
  $("statusTxt").textContent = txt;
}

function layoutDiagram(f){
  var dg=$("diagram"),sv=$("wires");
  dg.querySelectorAll(".node").forEach(function(n){n.remove();});sv.innerHTML="";nodeEls={};
  var W=dg.clientWidth,H=dg.clientHeight;
  Object.keys(f.nodes).forEach(function(id){
    var n=f.nodes[id],el=document.createElement("div");el.className="node";
    el.style.left=n.x+"%";el.style.top=n.y+"%";
    el.innerHTML='<div class="chip">'+(IC[n.ic]||"")+'</div><div class="nlab">'+esc(n.lab)+'</div><div class="nsub" data-sub>'+esc(n.sub)+'</div>';
    dg.appendChild(el);nodeEls[id]=el;
  });
  f.wires.forEach(function(w){
    var a=f.nodes[w[0]],b=f.nodes[w[1]];
    var l=document.createElementNS("http://www.w3.org/2000/svg","line");
    l.setAttribute("x1",a.x/100*W);l.setAttribute("y1",a.y/100*H);
    l.setAttribute("x2",b.x/100*W);l.setAttribute("y2",b.y/100*H);
    l.setAttribute("class","dwire");l.dataset.pair=w[0]+"-"+w[1];sv.appendChild(l);
  });
  Object.keys(f.nodes).forEach(function(id,i){setTimeout(function(){if(nodeEls[id])nodeEls[id].classList.add("show");},reduce?0:70*i);});
}

function setNode(id,status,label){
  var el=nodeEls[id];if(!el)return;
  el.classList.remove("show","busy","live","fail");
  if(status==="ghost")el.classList.add("show");
  else el.classList.add(status);
  if(label){var s=el.querySelector("[data-sub]");if(s)s.textContent=label;}
  if(status==="live"){
    $("wires").querySelectorAll(".dwire").forEach(function(l){
      var p=l.dataset.pair.split("-");
      if(p.indexOf(id)>-1){var o=p[0]===id?p[1]:p[0];
        if(nodeEls[o]&&nodeEls[o].classList.contains("live"))l.classList.add("on");}
    });
    var n=0;Object.keys(nodeEls).forEach(function(k){if(nodeEls[k].classList.contains("live"))n++;});
    $("mCreated").textContent=n;
  }
}

/* narration + console */
function narrate(text,tone){
  var n=$("narr");var e=n.querySelector(".empty");if(e)e.remove();
  var d=document.createElement("div");d.className="nline "+(tone||"info");
  d.innerHTML='<span class="ni">'+(TONE[tone]||"i")+'</span><div class="nt">'+esc(text)+'</div>';
  n.appendChild(d);n.scrollTop=n.scrollHeight;
}
function card(kind,head,body){
  var n=$("narr");var e=n.querySelector(".empty");if(e)e.remove();
  var d=document.createElement("div");d.className="card-in "+(kind||"");
  d.innerHTML='<div class="h">'+esc(head)+'</div><div class="b">'+esc(body)+'</div>';
  n.appendChild(d);n.scrollTop=n.scrollHeight;
}
function conLine(text){
  var c=$("console");var d=document.createElement("div");d.className="cln";d.textContent=text;
  c.appendChild(d);c.scrollTop=c.scrollHeight;conLines++;
  $("conCount").textContent=conLines+" "+T("linesSuffix");
  while(c.childNodes.length>400)c.removeChild(c.firstChild);
}
function esc(s){return String(s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c];});}

/* timer */
function startTimer(){t0=Date.now();stopTimer();timer=setInterval(function(){
  var s=Math.floor((Date.now()-t0)/1000);$("mElapsed").textContent=Math.floor(s/60)+":"+("0"+(s%60)).slice(-2);
},1000);}
function stopTimer(){if(timer){clearInterval(timer);timer=null;}}

/* ---- one event, from either source --------------------------------------
 *
 * The live stream and the captured replay carry the SAME events (events.py
 * writes both), so they get the same renderer: a divergence here would mean
 * the demo shows something the real deploy cannot. */
function frame(m) {
  if (!m || !m.type) { return; }
  if (m.type === "hello") {
    $("idChip").hidden = false;
    $("idChip").innerHTML = esc(T("acctLabel")) + ' <b>' + esc(m.account) + '</b> · ' + esc(m.region);
  } else if (m.type === "log") {
    conLine(m.text);
  } else if (m.type === "state") {
    setNode(m.node, m.status, m.label);
  } else if (m.type === "narrate") {
    narrate(m.text, m.tone);
  } else if (m.type === "stat") {
    if (m.created != null) { $("mCreated").textContent = m.created; }
    // m.note is the server's own words about a real AWS wait, not a phrase of
    // ours to translate; waitingOnAws is the fallback when it sent none.
    if (m.waiting) { setPill("run", m.note || T("waitingOnAws")); }
  } else if (m.type === "done") {
    narrate(m.summary, "good");
    if (m.outputs && m.outputs.note) { card("", T("cardNext"), m.outputs.note); }
  } else if (m.type === "error") {
    if (m.node) { setNode(m.node, "fail"); narrate(m.text, "err"); }
    else { card("err", T("cardFailed"), m.text); }
    if (m.fix) { card("err", T("cardFix"), m.fix); }
  }
}

/* ---- running ------------------------------------------------------------ */
$("runBtn").addEventListener("click", run);
$("stopBtn").addEventListener("click", function () {
  stopping = true;
  if (phase === "live") { bridge.stop(); }
  else { gen++; finish("stopped"); }
});

function run() {
  if (!current || running || !canRun()) { return; }
  var f = FLOWS[current];
  var inputs = {};
  document.querySelectorAll("#fields input").forEach(function (i) { inputs[i.dataset.k] = i.value; });
  resetInstrument(); layoutDiagram(f); $("narr").innerHTML = "";
  running = true; started = false; stopping = false;
  setPill("run", T("statusRunning"));
  $("runBtn").disabled = true;
  $("runTxt").textContent = T("runBusy");
  $("stopBtn").hidden = false;
  startTimer();
  if (phase === "live") { bridge.run(current, inputs, demoOn); }
  else { playFixture(current); }
}

// The end of a run, from either source.
//
// The console is the authority on how a run ended, and pressing Stop does not
// override it. orchestrator.py emits done() only for a run that really
// finished and "Stopped by operator." on every path it did not, the fixture
// replay included - so a stop that lands in the final instant, after the deploy
// already succeeded, still reads "complete" rather than claiming the visitor
// cut short a run that was over.
//
// What Stop does is RENAME an ending the console already called a failure: a
// terminal error the operator asked for is not the deploy's fault, and
// labelling it "failed" blames it for doing as it was told. Nothing else the
// console can report is touched.
//
// The local fixture playback has no console to ask, so it reports "stopped"
// itself by passing that kind straight in.
var END = {
  finished: ["done", "statusComplete"],
  failed:   ["err", "statusFailed"],
  lost:     ["err", "statusLost"],
  stopped:  ["err", "statusStopped"]
};
function finish(kind) {
  running = false; stopTimer();
  var end = END[(stopping && kind === "failed") ? "stopped" : kind] || END.failed;
  setPill(end[0], T(end[1]));
  $("stopBtn").hidden = true;
  $("runTxt").textContent = T("runAgain");
  $("runBtn").disabled = !canRun();
}

// The captured run, played locally. This is the whole of the replay state: no
// server is involved and nothing is claimed to be live.
function playFixture(id) {
  var frames = (FIXTURES && FIXTURES[id]) || [];
  var mine = ++gen, i = 0;
  // No identity chip: the captured runs carry no hello frame, and the one the
  // static page used to show ("acct your-account") was invented. An empty
  // chip is better than a fabricated account id on a page about proof.
  (function step() {
    if (mine !== gen) { return; }
    if (i >= frames.length) { finish("finished"); return; }
    var fr = frames[i++];
    frame(fr.event);
    if (fr.event && fr.event.type === "done") { finish("finished"); return; }
    setTimeout(step, reduce ? 40 : Math.min((fr._delay || 0.6) * 1000, 2200));
  }());
}

window.addEventListener("resize", function () {
  if (!running && current && FLOWS[current]) { layoutDiagram(FLOWS[current]); }
});

/* ---- boot ---------------------------------------------------------------
 *
 * Render first, probe second. The page must be complete and usable before
 * anything is known about the machine it is running on, because for almost
 * every visitor the answer is "no console" and that answer must cost them
 * nothing. */
if (ORDER.length) { renderFlows(); }
renderPanel(bridge.state());
bridge.probe().then(function (s) {
  // Only the console's own page can attach without a code; the bridge refuses
  // to spend a guess from anywhere else, so this is safe to call blind.
  if (s.name === "pairing") { return bridge.attach(); }
});

// The built page autoplays its captured run so a visitor sees the thing work
// without touching anything. Gated on there being a fixture to play, which is
// what keeps it from ever launching a real deploy on a paired console.
if (FIXTURES && !reduce) {
  setTimeout(function () { if (phase !== "live" && !running) { run(); } }, 900);
}
})();
