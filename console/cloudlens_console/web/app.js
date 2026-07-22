(function(){
"use strict";
var reduce=window.matchMedia("(prefers-reduced-motion:reduce)").matches;
var $=function(id){return document.getElementById(id);};

/* theme */
$("themeBtn").addEventListener("click",function(){
  var cur=document.documentElement.getAttribute("data-theme")||"light";
  var nxt=cur==="dark"?"light":"dark";
  document.documentElement.setAttribute("data-theme",nxt);
  try{localStorage.setItem("cl-theme",nxt);}catch(e){}
});

/* demo toggle */
var demoOn=true, demoSw=$("demoSw");
function setDemo(v){demoOn=v;demoSw.setAttribute("aria-checked",v?"true":"false");
  $("modeBadge").textContent=v?"DEMO · REPLAYING REAL EVENTS":"LIVE · YOUR AWS ACCOUNT";}
demoSw.addEventListener("click",function(){setDemo(!demoOn);});
demoSw.addEventListener("keydown",function(e){if(e.key===" "||e.key==="Enter"){e.preventDefault();setDemo(!demoOn);}});

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

var FLOWS={}, ORDER=[], current=null, nodeEls={}, es=null, timer=null, t0=0, conLines=0;

/* fetch flows */
fetch("/flows").then(function(r){return r.json();}).then(function(d){
  FLOWS=d.flows; ORDER=d.order;
  var tabs=$("flows"), cards=$("flowCards");
  ORDER.forEach(function(id,i){
    var f=FLOWS[id];
    var b=document.createElement("button");b.className="flow";b.setAttribute("role","tab");
    b.setAttribute("aria-selected",i===0?"true":"false");b.dataset.flow=id;
    b.innerHTML='<div class="fn">FLOW 0'+(i+1)+'</div><div class="ft">'+f.name+'</div>';
    b.addEventListener("click",function(){selectFlow(id);});
    tabs.appendChild(b);
    var c=document.createElement("div");c.className="card";
    c.innerHTML='<div class="k">FLOW 0'+(i+1)+'</div><h3>'+f.name+'</h3><p>'+f.subtitle+'</p>';
    cards.appendChild(c);
  });
  selectFlow(ORDER[0]);
}).catch(function(){$("narr").innerHTML='<div class="empty">Could not load flows. Is the console server running?</div>';});

function selectFlow(id){
  if(es){es.close();es=null;} stopTimer();
  current=id; var f=FLOWS[id];
  document.querySelectorAll(".flow").forEach(function(b){b.setAttribute("aria-selected",b.dataset.flow===id?"true":"false");});
  $("instName").textContent=f.script;
  $("cfgTitle").textContent="Inputs"; $("cfgSub").textContent="· "+f.name;
  var fl=$("fields");fl.innerHTML="";
  f.inputs.forEach(function(fd){
    var d=document.createElement("div");d.className="field";
    d.innerHTML='<label>'+fd.label+'</label><input data-k="'+fd.key+'" value="'+(fd.default||"")+'" placeholder="'+(fd.placeholder||"")+'" spellcheck="false">';
    fl.appendChild(d);
  });
  resetInstrument();
  layoutDiagram(f);
  $("narr").innerHTML='<div class="empty">Press ▸ Run — the narration explains each step as it happens.</div>';
  $("runBtn").disabled=false;$("runBtn").innerHTML='<span class="tri"></span> Run this flow';
}

function resetInstrument(){
  $("console").innerHTML="";conLines=0;$("conCount").textContent="";
  setPill("idle","");$("mElapsed").textContent="0:00";$("mCreated").textContent="0";
  $("idChip").hidden=true;$("stopBtn").hidden=true;
}
function setPill(cls,txt){var p=$("statusPill");p.className="pill"+(cls&&cls!=="idle"?" "+cls:"");$("statusTxt").textContent=txt||cls;}

function layoutDiagram(f){
  var dg=$("diagram"),sv=$("wires");
  dg.querySelectorAll(".node").forEach(function(n){n.remove();});sv.innerHTML="";nodeEls={};
  var W=dg.clientWidth,H=dg.clientHeight;
  Object.keys(f.nodes).forEach(function(id){
    var n=f.nodes[id],el=document.createElement("div");el.className="node";
    el.style.left=n.x+"%";el.style.top=n.y+"%";
    el.innerHTML='<div class="chip">'+(IC[n.ic]||"")+'</div><div class="nlab">'+n.lab+'</div><div class="nsub" data-sub>'+n.sub+'</div>';
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
  var n=$("narr");
  var d=document.createElement("div");d.className="card-in "+(kind||"");
  d.innerHTML='<div class="h">'+esc(head)+'</div><div class="b">'+esc(body)+'</div>';
  n.appendChild(d);n.scrollTop=n.scrollHeight;
}
function conLine(text){
  var c=$("console");var d=document.createElement("div");d.className="cln";d.textContent=text;
  c.appendChild(d);c.scrollTop=c.scrollHeight;conLines++;$("conCount").textContent=conLines+" lines";
  while(c.childNodes.length>400)c.removeChild(c.firstChild);
}
function esc(s){return String(s).replace(/[&<>]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;"}[c];});}

/* timer */
function startTimer(){t0=Date.now();stopTimer();timer=setInterval(function(){
  var s=Math.floor((Date.now()-t0)/1000);$("mElapsed").textContent=Math.floor(s/60)+":"+("0"+(s%60)).slice(-2);
},1000);}
function stopTimer(){if(timer){clearInterval(timer);timer=null;}}

/* run */
$("runBtn").addEventListener("click",run);
$("stopBtn").addEventListener("click",function(){ if(window._job) fetch("/stop/"+window._job,{method:"POST"}); });

function run(){
  var f=FLOWS[current];
  var inputs={};document.querySelectorAll("#fields input").forEach(function(i){inputs[i.dataset.k]=i.value;});
  resetInstrument();layoutDiagram(f);$("narr").innerHTML="";
  setPill("run","running");$("runBtn").disabled=true;$("runBtn").innerHTML='<span class="tri"></span> Running…';
  $("stopBtn").hidden=false;startTimer();
  fetch("/run",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({flow:current,inputs:inputs,replay:demoOn})})
   .then(function(r){return r.json();})
   .then(function(d){
     if(d.error){finish("err","error");narrate(d.error,"err");return;}
     window._job=d.job_id;
     es=new EventSource("/events/"+d.job_id);
     es.addEventListener("hello",function(e){var m=JSON.parse(e.data);
       $("idChip").hidden=false;$("idChip").innerHTML='acct <b>'+m.account+'</b> · '+m.region;});
     es.addEventListener("log",function(e){conLine(JSON.parse(e.data).text);});
     es.addEventListener("state",function(e){var m=JSON.parse(e.data);setNode(m.node,m.status,m.label);
       if(m.status==="live"){var n=0;Object.keys(nodeEls).forEach(function(k){if(nodeEls[k].classList.contains("live"))n++;});$("mCreated").textContent=n;}});
     es.addEventListener("narrate",function(e){var m=JSON.parse(e.data);narrate(m.text,m.tone);});
     es.addEventListener("stat",function(e){var m=JSON.parse(e.data);
       if(m.created!=null)$("mCreated").textContent=m.created;
       if(m.waiting){setPill("run",m.note||"waiting on AWS");}});
     es.addEventListener("done",function(e){var m=JSON.parse(e.data);
       finish("done","complete");narrate(m.summary,"good");
       if(m.outputs&&m.outputs.note)card("","Next",m.outputs.note);});
     es.addEventListener("error",function(e){
       if(!e.data){return;} var m=JSON.parse(e.data);
       if(m.node){setNode(m.node,"fail");narrate(m.text,"err");}
       else{finish("err","error");card("err","Failed",m.text);}
       if(m.fix)card("err","How to fix",m.fix);});
   })
   .catch(function(){finish("err","error");narrate("Could not reach the console server.","err");});
}

function finish(cls,txt){
  stopTimer();setPill(cls,txt);$("stopBtn").hidden=true;
  $("runBtn").disabled=false;$("runBtn").innerHTML='<span class="tri"></span> Run again';
  if(es){es.close();es=null;}
}

window.addEventListener("resize",function(){if(!timer&&current)layoutDiagram(FLOWS[current]);});
})();
