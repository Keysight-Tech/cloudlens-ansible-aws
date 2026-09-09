"""Generate the self-contained static site (docs/index.html) from the console.

The public site is static GitHub Pages, so it can't run the Python backend. This
bundles the flow metadata + the real captured fixtures inline and drives the whole
premium experience CLIENT-SIDE (no server, no AWS). It is the "watch it happen"
demo anyone can open. The Python console (`python3 -m cloudlens_console`) remains
the tool that runs REAL deploys with live SSE.

Run:  cd console && python3 build_site.py
"""
import os, re, json, sys
sys.path.insert(0, os.path.dirname(__file__))
from cloudlens_console import flows as F

HERE = os.path.dirname(__file__)
WEB = os.path.join(HERE, "cloudlens_console", "web")
FX = os.path.join(HERE, "fixtures")
# OUT=/some/path in the environment writes elsewhere (a build to prove the
# page still assembles, without touching docs/).
OUT = os.environ.get("OUT") or os.path.abspath(os.path.join(HERE, "..", "docs", "console.html"))

# 1. flow metadata (same shape the /flows endpoint returns)
FLOWS = {"order": F.ORDER, "flows": {
    fid: {k: F.FLOWS[fid][k] for k in ("id", "name", "script", "subtitle", "inputs", "nodes", "wires")}
    for fid in F.ORDER}}

# 2. the real captured fixtures
FIXTURES = {fid: json.load(open(os.path.join(FX, fid + ".json"))) for fid in F.ORDER}

# 3. take the CSS + body from the console UI, swap the script for a client-side one
src = open(os.path.join(WEB, "index.html")).read()
style = re.search(r"<style>.*?</style>", src, re.S).group(0)

# The instrument and the flow picker this page IS. They used to live in
# index.html, where the console served them too: a Run button beside the
# real deploy wizard, posting to a route that started a real run with no
# one-engine-per-stack lock and no redaction, under a badge that said the
# page was replaying. The route and the markup are gone from the console;
# this page keeps both, because here the replay is the truth - CLIENT_APP
# below drives it from the captured fixtures and makes no request at all.
#
# The rules that dress it are still in the console's stylesheet, which this
# build takes whole, and most of them are shared with the Watch screen.
DEMO_MARKUP = """\
<div class="wrap" id="runView">
  <div class="instrument">
    <div class="inst-bar">
      <div class="traffic"><i></i><i></i><i></i></div>
      <div class="inst-title"><b id="instName">deploy-stack.sh</b></div>
      <span class="id-chip" id="idChip" hidden>acct <b>-</b></span>
      <div class="inst-right">
        <div class="metric">elapsed<b id="mElapsed">0:00</b></div>
        <div class="metric">created<b id="mCreated">0</b></div>
        <span class="pill" id="statusPill"><span class="d"></span><span id="statusTxt">idle</span></span>
        <button class="stopbtn" id="stopBtn" hidden>Stop</button>
      </div>
    </div>
    <div class="inst-body">
      <div class="diagram" id="diagram"><div class="dgrid"></div><svg id="wires"></svg></div>
      <div class="rightcol">
        <div class="narr" id="narr"><div class="empty">Narration will appear here as each step runs, with the why behind it.</div></div>
        <div class="console-head"><span>Raw output</span><span id="conCount"></span></div>
        <div class="console" id="console"></div>
      </div>
    </div>
  </div>

  <details class="quick" id="quickFlows" open>
    <summary>Quick flows (advanced)</summary>
    <div class="rig">
      <div class="flows" id="flows" role="tablist" aria-label="Deployment flows"></div>
      <button class="runbtn" id="runBtn"><span class="tri"></span> Run this flow</button>
    </div>

    <div class="cfg">
      <h4><span id="cfgTitle">Inputs</span><span class="sub" id="cfgSub"></span></h4>
      <div class="fields" id="fields"></div>
    </div>
  </details>
</div>
"""

CLIENT_APP = r"""
(function(){
"use strict";
var reduce=window.matchMedia("(prefers-reduced-motion:reduce)").matches;
var $=function(id){return document.getElementById(id);};
var FLOWS=window.__FLOWS__.flows, ORDER=window.__FLOWS__.order, FX=window.__FIXTURES__;

// embed mode (iframed into the main doc): hide chrome, report height to the parent
if(/embed/.test(location.search)){document.body.classList.add("embed");
  var ph=function(){try{parent.postMessage({clc:document.body.scrollHeight},"*");}catch(e){}};
  window.addEventListener("load",ph);window.addEventListener("resize",ph);setInterval(ph,900);}

$("themeBtn").addEventListener("click",function(){
  var cur=document.documentElement.getAttribute("data-theme")||"light";
  var nxt=cur==="dark"?"light":"dark";document.documentElement.setAttribute("data-theme",nxt);
  try{localStorage.setItem("cl-theme",nxt);}catch(e){}});

var IC={
 vpc:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 9h18M9 3v18"/></svg>',
 clms:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
 kvo:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4M5 5l3 3M16 16l3 3M19 5l-3 3M8 16l-3 3"/></svg>',
 vpb:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 7V5M12 7V5M17 7V5M7 17v2M12 17v2M17 17v2"/></svg>',
 vm:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="4" y="4" width="16" height="12" rx="2"/><path d="M2 20h20"/></svg>',
 tool:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M14 6l4 4-8 8-4 1 1-4z"/><path d="M4 20h6"/></svg>',
 mirror:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3v18M7 8l-4 4 4 4M17 8l4 4-4 4"/></svg>',
 coll:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3a4 4 0 0 0-4 4H6a3 3 0 0 0 0 6h12a3 3 0 0 0 0-6h-2a4 4 0 0 0-4-4z"/><path d="M9 17l3 4 3-4"/></svg>'};
var TONE={info:"i",good:"✓",note:"·",warn:"!",err:"✕"};
var current=null,nodeEls={},timer=null,t0=0,conLines=0,playing=false,gen=0;

var tabs=$("flows"),cards=$("flowCards");
ORDER.forEach(function(id,i){var f=FLOWS[id];
  var b=document.createElement("button");b.className="flow";b.setAttribute("role","tab");
  b.setAttribute("aria-selected",i===0?"true":"false");b.dataset.flow=id;
  b.innerHTML='<div class="fn">FLOW 0'+(i+1)+'</div><div class="ft">'+f.name+'</div>';
  b.addEventListener("click",function(){selectFlow(id);});tabs.appendChild(b);
  var c=document.createElement("div");c.className="card";
  c.innerHTML='<div class="k">FLOW 0'+(i+1)+'</div><h3>'+f.name+'</h3><p>'+f.subtitle+'</p>';cards.appendChild(c);});

function selectFlow(id){stop();current=id;var f=FLOWS[id];
  document.querySelectorAll(".flow").forEach(function(b){b.setAttribute("aria-selected",b.dataset.flow===id?"true":"false");});
  $("instName").textContent=f.script;$("cfgTitle").textContent="Inputs";$("cfgSub").textContent="· "+f.name;
  var fl=$("fields");fl.innerHTML="";
  f.inputs.forEach(function(fd){var d=document.createElement("div");d.className="field";
    d.innerHTML='<label>'+fd.label+'</label><input value="'+(fd.default||"")+'" placeholder="'+(fd.placeholder||"")+'" spellcheck="false">';fl.appendChild(d);});
  reset();layoutDiagram(f);
  $("narr").innerHTML='<div class="empty">Press ▸ Run: the narration explains each step as it happens.</div>';
  $("runBtn").disabled=false;$("runBtn").innerHTML='<span class="tri"></span> Run this flow';}

function reset(){$("console").innerHTML="";conLines=0;$("conCount").textContent="";
  setPill("idle","");$("mElapsed").textContent="0:00";$("mCreated").textContent="0";
  $("idChip").hidden=true;$("stopBtn").hidden=true;}
function setPill(cls,txt){var p=$("statusPill");p.className="pill"+(cls&&cls!=="idle"?" "+cls:"");$("statusTxt").textContent=txt||cls;}

function layoutDiagram(f){var dg=$("diagram"),sv=$("wires");
  dg.querySelectorAll(".node").forEach(function(n){n.remove();});sv.innerHTML="";nodeEls={};
  var W=dg.clientWidth,H=dg.clientHeight;
  Object.keys(f.nodes).forEach(function(id){var n=f.nodes[id],el=document.createElement("div");el.className="node";
    el.style.left=n.x+"%";el.style.top=n.y+"%";
    el.innerHTML='<div class="chip">'+(IC[n.ic]||"")+'</div><div class="nlab">'+n.lab+'</div><div class="nsub" data-sub>'+n.sub+'</div>';
    dg.appendChild(el);nodeEls[id]=el;});
  f.wires.forEach(function(w){var a=f.nodes[w[0]],b=f.nodes[w[1]];
    var l=document.createElementNS("http://www.w3.org/2000/svg","line");
    l.setAttribute("x1",a.x/100*W);l.setAttribute("y1",a.y/100*H);
    l.setAttribute("x2",b.x/100*W);l.setAttribute("y2",b.y/100*H);
    l.setAttribute("class","dwire");l.dataset.pair=w[0]+"-"+w[1];sv.appendChild(l);});
  Object.keys(f.nodes).forEach(function(id,i){setTimeout(function(){if(nodeEls[id])nodeEls[id].classList.add("show");},reduce?0:70*i);});}

function setNode(id,status,label){var el=nodeEls[id];if(!el)return;
  el.classList.remove("show","busy","live","fail");
  if(status==="ghost")el.classList.add("show");else el.classList.add(status);
  if(label){var s=el.querySelector("[data-sub]");if(s)s.textContent=label;}
  if(status==="live"){$("wires").querySelectorAll(".dwire").forEach(function(l){var p=l.dataset.pair.split("-");
    if(p.indexOf(id)>-1){var o=p[0]===id?p[1]:p[0];if(nodeEls[o]&&nodeEls[o].classList.contains("live"))l.classList.add("on");}});
    var n=0;Object.keys(nodeEls).forEach(function(k){if(nodeEls[k].classList.contains("live"))n++;});$("mCreated").textContent=n;}}

function narrate(text,tone){var n=$("narr");var e=n.querySelector(".empty");if(e)e.remove();
  var d=document.createElement("div");d.className="nline "+(tone||"info");
  d.innerHTML='<span class="ni">'+(TONE[tone]||"i")+'</span><div class="nt">'+esc(text)+'</div>';n.appendChild(d);n.scrollTop=n.scrollHeight;}
function card(kind,head,body){var n=$("narr");var d=document.createElement("div");d.className="card-in "+(kind||"");
  d.innerHTML='<div class="h">'+esc(head)+'</div><div class="b">'+esc(body)+'</div>';n.appendChild(d);n.scrollTop=n.scrollHeight;}
function conLine(t){var c=$("console");var d=document.createElement("div");d.className="cln";d.textContent=t;
  c.appendChild(d);c.scrollTop=c.scrollHeight;conLines++;$("conCount").textContent=conLines+" lines";while(c.childNodes.length>400)c.removeChild(c.firstChild);}
function esc(s){return String(s).replace(/[&<>]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;"}[c];});}
function startTimer(){t0=Date.now();stopTimer();timer=setInterval(function(){var s=Math.floor((Date.now()-t0)/1000);$("mElapsed").textContent=Math.floor(s/60)+":"+("0"+(s%60)).slice(-2);},1000);}
function stopTimer(){if(timer){clearInterval(timer);timer=null;}}

$("runBtn").addEventListener("click",run);
$("stopBtn").addEventListener("click",stop);
function stop(){gen++;playing=false;stopTimer();if($("statusTxt").textContent==="running")setPill("idle","stopped");
  $("stopBtn").hidden=true;$("runBtn").disabled=false;$("runBtn").innerHTML='<span class="tri"></span> Run this flow';}

function run(){var myGen=++gen;var f=FLOWS[current];reset();layoutDiagram(f);$("narr").innerHTML="";
  setPill("run","running");$("runBtn").disabled=true;$("runBtn").innerHTML='<span class="tri"></span> Running…';
  $("stopBtn").hidden=false;$("idChip").hidden=false;$("idChip").innerHTML='acct <b>your-account</b> · us-east-1';
  startTimer();playing=true;
  var frames=FX[current],i=0;
  (function step(){
    if(myGen!==gen)return;
    if(i>=frames.length){finish("done","complete");return;}
    var fr=frames[i++];var ev=fr.event;
    if(ev.type==="log")conLine(ev.text);
    else if(ev.type==="state")setNode(ev.node,ev.status,ev.label);
    else if(ev.type==="narrate")narrate(ev.text,ev.tone);
    else if(ev.type==="stat"){if(ev.created!=null)$("mCreated").textContent=ev.created;}
    else if(ev.type==="done"){narrate(ev.summary,"good");if(ev.outputs&&ev.outputs.note)card("","Next",ev.outputs.note);finish("done","complete");return;}
    setTimeout(step,reduce?40:Math.min((fr._delay||0.6)*1000,2200));
  })();}
function finish(cls,txt){playing=false;stopTimer();setPill(cls,txt);$("stopBtn").hidden=true;
  $("runBtn").disabled=false;$("runBtn").innerHTML='<span class="tri"></span> Run again';}

window.addEventListener("resize",function(){if(!playing&&current)layoutDiagram(FLOWS[current]);});
selectFlow(ORDER[0]);
setTimeout(function(){if(!reduce)run();},900);
})();
"""

data = ('<script>window.__FLOWS__=' + json.dumps(FLOWS, separators=(",", ":")) +
        ';window.__FIXTURES__=' + json.dumps(FIXTURES, separators=(",", ":")) + ';</script>')

body = re.search(r"<body>.*?</body>", src, re.S).group(0)

# The operations console (pre-flight, the deploy wizard, Watch, Operate,
# Licensing, Teardown) drives the API and has nothing to do on a static
# page: the block between its two markers goes, and so do the scripts that
# only it uses. DEMO_MARKUP takes its place, so the instrument this page is
# about stands exactly where the screens it replaces stood.
#
# The cut is an unanchored substitution over the whole body, so it is held
# to its markers at both ends: a marker that was renamed or duplicated (or
# a body with none) would otherwise leave the wizard, its API calls and its
# secret fields on a public page that cannot serve them, or eat the page
# between the wrong pair. Counted here, checked in the result below.
for marker in ("<!-- ops:start -->", "<!-- ops:end -->"):
    found = body.count(marker)
    assert found == 1, "index.html carries %d of %s, expected exactly one" % (found, marker)
body = re.sub(r"<!-- ops:start -->.*?<!-- ops:end -->", DEMO_MARKUP.replace("\\", "\\\\"), body, flags=re.S)
body = re.sub(r'\s*<script src="/web/(?:ui|plan|wizard|watch|operate|licences|teardown)\.js"></script>', "", body)
# The console's hero describes the console: every screen there runs against
# the real account. This page runs against nothing, so it says so, in the
# badge and in the sentence under it. Both are held by test_build_site.py:
# a page that replays must never be dressed as one that deploys, and the
# console must never be dressed as one that replays.
body = body.replace('LIVE · YOUR AWS ACCOUNT', 'WATCH IT DEPLOY · REAL CAPTURED RUN')
body = body.replace(
    "Plan a deploy, watch the engine run it, and operate what it built. Every screen here runs "
    "against your own AWS account and your own appliances: the phases are the deploy script's own, "
    "the addresses are the ones it reported, and its raw output is underneath.",
    "Pick a flow and press Run. This page replays real captured events from live deploys, in the "
    "browser and with no AWS calls: the diagram wires itself up, the narration explains each step, "
    "and the console shows the raw output underneath. The tool that runs these for real is the "
    "console you install, and it is the one thing on this page that is not here.")
body = body.replace('<script src="/web/app.js"></script>', data + '<script>' + CLIENT_APP + '</script>')

html = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        "<title>CloudLens Autopilot for AWS · Deployment Console</title>\n"
        "<script>try{document.documentElement.setAttribute('data-theme',localStorage.getItem('cl-theme')||'light');}catch(e){document.documentElement.setAttribute('data-theme','light');}</script>\n"
        + style + "\n</head>\n" + body + "\n</html>\n")

# What the static page must not carry: the wizard's screens, the scripts
# that only it uses, any API path, the secret fields and the activation
# code entry. And the one thing it must: the quick flows, opened.
for gone in ("data-screen", "ui.js", "wizard.js", "plan.js", "watch.js", "operate.js", "licences.js",
             "teardown.js", "/api/", "/events/", "data-secret", "codeEntry", "licEntry", "opCodes"):
    assert gone not in html, "the static page still carries %r: the ops block survived the cut" % gone
assert 'quickFlows" open' in html, "the quick flows fold does not open on the static page"
# and the markup this build supplies is all there, since index.html no
# longer carries any of it: a DEMO_MARKUP that lost an element would be a
# page whose script throws on its first line and draws nothing
for needed in ('id="diagram"', 'id="narr"', 'id="console"', 'id="flows"', 'id="runBtn"',
               'id="fields"', 'id="instName"', 'id="statusTxt"', 'id="mElapsed"'):
    assert needed in html, "the demo instrument is missing %s" % needed
assert "WATCH IT DEPLOY" in html and "DEMO" not in html.replace("DEMO_MARKUP", ""), \
    "the badge on a page that replays must say what it is"

os.makedirs(os.path.dirname(OUT), exist_ok=True)
open(OUT, "w").write(html)
print("wrote", OUT, "(", len(html), "bytes,", len(F.ORDER), "flows,",
      sum(len(v) for v in FIXTURES.values()), "fixture frames )")
