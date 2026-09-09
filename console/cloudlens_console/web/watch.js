(function(){
"use strict";
/* watch.js: the Watch screen.

   Everything on this screen comes from one of the engine's events. There is
   no timer that advances a step, no phase list typed out here, no address
   guessed from a stack name: a row, a node, a login or a banner exists
   because a frame said so, and says exactly what that frame carried. The
   phases still to come are drawn from the script's own `phases` event
   (deploy-stack.sh emits its PHASE_ORDER once, right after its first hello),
   which is why they can be shown before they run without inventing them.

   Two halves, deliberately separated:

     applyEvent(model, ev)  a pure function of the model and one frame. It
                            MUTATES the model it is given and returns it (the
                            one convention in this file: nothing here copies).
                            No DOM, no network, no globals, so tests/test_watch_model.py
                            can run it under node.
     render(model)          all of the DOM. Called after every frame, it
                            draws from the model alone and holds nothing of
                            its own, except the two things a redraw must not
                            destroy: the open prompt's input, and the log
                            lines already on the page. It coalesces: however
                            many frames arrive, one draw per animation frame,
                            of the newest model.

   READS below is the contract with deploy-stack.sh: the fields this model
   reads out of each event type. test_watch_model.py builds the frames the
   script's own emit_event calls write, runs them through events.from_script,
   and holds this table to them, so a key renamed in the script fails a test
   here instead of quietly emptying a card. A few of the types are the
   console's own and not the script's (log, error, answered); their contract
   is with events.py, and the same test holds them to it. */

/* ------------------------------------------------------------ the model */

var READS={
  /* two hello shapes on one job: the console's (account, arn, region) before
     the engine starts, then the script's (stack, region, dry_run) */
  hello:["stack","region","dry_run","account","arn"],
  phases:["order"],
  phase:["name","status","reason"],
  /* the script's own "id" arrives as resource_id: events.from_script files it
     there, because the console's own id is stamped over "id" */
  resource:["kind","resource_id","role","zone","ip","private_ip","ingress_ip",
            "egress_ip","count","tag","mode","filter","created","cluster"],
  check:["item","status","fix"],
  /* password_in says WHERE the password is. There is no password field in any
     event, and this screen must never grow one. */
  login:["component","url","user","password_in"],
  prompt:["prompt_id","question","default","kind"],
  /* the console's own frame, not the script's: orchestrator.Job.answer emits
     it when the answer reached the engine. `shown` is what may be displayed,
     which for a secret is asterisks and never the value */
  answered:["prompt_id","shown"],
  /* the script's done carries status/phase/reason/code/mode/report/profile;
     the console's own carries summary */
  done:["status","phase","reason","code","mode","report","profile","summary"],
  log:["text"],
  error:["text","fix"]
};

/* the phase whose failure is unambiguously one node's. stack (everything at
   once) and prove (the whole path) are not in it: those only redden the
   timeline row, because reddening a node there would be a guess. */
var PHASE_NODE={wait:"vcontroller",key:"vcontroller",bootstrap:"vpb",vpb:"vpb",path:"vpb",
  license:"kvo",adopt:"kvo",mirror:"kvo",sensors:"workloads",eks:"eks"};

var LOG_MAX=400;         // the drawer keeps the tail, as the instrument does
// A phase list is a dozen short names. Anything longer than this is a frame
// that went wrong, and drawing thousands of rows from it would take the page
// down with it: the cap is what stops one bad `order` at a screenful.
var PHASE_MAX=100;

function txt(v){return v===undefined||v===null?"":String(v);}

function emptyModel(){
  return {
    job:"",              // the job id this model follows
    stream:"idle",       // idle | live | reconnecting | closed
    note:"",             // why the stream is not live, in words for the page
    // what this page last DID to the run, in words: the note is the stream's
    // and is cleared by the next frame, and a stop the operator asked for
    // must outlive the frames that keep arriving while the run winds down
    action:"",
    events:0,            // frames applied
    lastId:0,            // the highest console id seen (EventSource resumes from it)
    ended:false,         // a terminal frame arrived: done, or the console's error
    hello:{source:"",stack:"",region:"",dryRun:false,account:"",arn:""},
    phaseOrder:[],       // the script's PHASE_ORDER, from its phases event
    phaseSeen:[],        // phases that reported, in arrival order
    phases:{},           // name -> {name, status, reason}
    nodes:{},            // key -> the resource frame's fields
    nodeOrder:[],
    logins:[],
    checks:[],
    prompts:[],
    logs:[],
    logCount:0,          // every line, not only the ones still kept
    done:null,
    error:null
  };
}

function copy(into,ev,fields){
  for(var i=0;i<fields.length;i++){
    var f=fields[i];
    // an empty value is the script saying "not known": it never overwrites
    // something a previous frame did know (workloads report count twice)
    if(ev[f]!==undefined&&ev[f]!=="")into[f]=txt(ev[f]);
  }
  return into;
}

function findBy(list,field,value){
  for(var i=0;i<list.length;i++)if(list[i][field]===value)return list[i];
  return null;
}

/* One frame into the model. Mutates and returns it. An unknown type, or a
   frame that is not an object, changes nothing: this screen renders what it
   understands and drops nothing else on the floor, because the raw drawer
   already carries every line the run printed. */
function applyEvent(model,ev){
  if(!ev||typeof ev!=="object"||typeof ev.type!=="string")return model;
  model.events++;
  if(typeof ev.id==="number"&&ev.id>model.lastId)model.lastId=ev.id;
  var t=ev.type;
  if(t==="hello"){
    var h=model.hello;
    if(ev.stack!==undefined){h.source="script";h.stack=txt(ev.stack);h.dryRun=txt(ev.dry_run)==="true";}
    else{h.source="console";h.account=txt(ev.account);h.arn=txt(ev.arn);}
    if(txt(ev.region))h.region=txt(ev.region);
    return model;
  }
  if(t==="phases"){
    // one row per phase, in the script's order: a name repeated in the frame
    // is the same row, and past PHASE_MAX the rest is dropped rather than
    // drawn. The script writes its PHASE_ORDER, which is neither, so this
    // only ever fires on a frame that is already wrong.
    var order=[],seen={};
    txt(ev.order).split(/\s+/).forEach(function(p){
      if(!p||order.length>=PHASE_MAX)return;
      if(Object.prototype.hasOwnProperty.call(seen,p))return;
      seen[p]=true;order.push(p);
    });
    if(order.length)model.phaseOrder=order;
    return model;
  }
  if(t==="phase"){
    var name=txt(ev.name);
    if(!name)return model;
    if(!model.phases[name])model.phaseSeen.push(name);
    model.phases[name]={name:name,status:txt(ev.status),reason:txt(ev.reason)};
    return model;
  }
  if(t==="resource"){
    var kind=txt(ev.kind);
    if(!kind)return model;
    // three subnets share one kind, so the role keys them; a subnet with no
    // role keys on its own id, and one with neither takes its arrival slot
    var key=kind==="subnet"?("subnet:"+(txt(ev.role)||txt(ev.resource_id)||model.nodeOrder.length)):kind;
    var n=model.nodes[key];
    if(!n){n={key:key,kind:kind};model.nodes[key]=n;model.nodeOrder.push(key);}
    copy(n,ev,READS.resource);
    return model;
  }
  if(t==="check"){
    // a check that reports twice (a replayed buffer, a doctor run that
    // re-checks) updates its row, as a login and a resource do; the item is
    // what names it. A check with no item has nothing to be the same as, so
    // it takes a row of its own.
    var item=txt(ev.item);
    var c=item?findBy(model.checks,"item",item):null;
    if(!c){c={item:item};model.checks.push(c);}
    copy(c,ev,READS.check);
    return model;
  }
  if(t==="login"){
    // a component that announces itself twice updates its card, it does not
    // get a second one
    var comp=txt(ev.component);
    var card=findBy(model.logins,"component",comp);
    if(!card){card={component:comp};model.logins.push(card);}
    copy(card,ev,READS.login);
    return model;
  }
  if(t==="prompt"){
    // the same prompt can arrive twice (a replayed buffer after a reconnect):
    // it is the same question, and any answer already given stays on it
    var pid=txt(ev.prompt_id);
    var p=findBy(model.prompts,"prompt_id",pid);
    if(!p){p={prompt_id:pid,answer:null,error:"",sending:false};model.prompts.push(p);}
    p.question=txt(ev.question);
    p.def=txt(ev["default"]);
    p.kind=txt(ev.kind)||"text";
    return model;
  }
  if(t==="answered"){
    // the stream, not this page, is what says a question is settled. Without
    // this frame a reload of a run that had already answered replayed every
    // prompt as unanswered, opened the modal on the newest one, and the only
    // thing the operator could do with it was have it refused. An id no
    // prompt frame introduced is ignored: the prompt is always emitted before
    // the answer to it can be (nothing is pending until it is), so the model
    // has the question by the time this arrives, on a replay and on a resume.
    var ap=findBy(model.prompts,"prompt_id",txt(ev.prompt_id));
    if(ap){ap.answer=txt(ev.shown);ap.error="";ap.sending=false;}
    return model;
  }
  if(t==="log"){
    model.logCount++;
    model.logs.push(txt(ev.text));
    if(model.logs.length>LOG_MAX)model.logs.shift();
    return model;
  }
  if(t==="done"){
    model.done=copy({},ev,READS.done);
    model.ended=true;
    return model;
  }
  if(t==="error"){
    model.error=copy({},ev,READS.error);
    model.ended=true;
    return model;
  }
  return model;
}

/* Instant feedback for the operator who just pressed send: the fetch has
   come back OK, and the `answered` frame for it is a moment behind. It
   records the same `shown` that frame will carry, so the replay agrees with
   what is already on the screen instead of changing it; the stream stays the
   authority, and this only saves the wait. Pure, for the same reason
   applyEvent is. `shown` is what may be displayed: never a secret. */
function noteAnswer(model,promptId,shown,error){
  var p=findBy(model.prompts,"prompt_id",txt(promptId));
  if(!p)return model;
  p.sending=false;
  if(error){p.error=txt(error);return model;}
  p.error="";
  p.answer=txt(shown);
  return model;
}

/* The prompt the run is blocked on: the last one with no answer. */
function openPrompt(model){
  for(var i=model.prompts.length-1;i>=0;i--)if(model.prompts[i].answer===null)return model.prompts[i];
  return null;
}

/* A node's state, derived: live because a resource event said it exists,
   fail when a phase that is unambiguously about it failed. */
function nodeState(model,key){
  var node=model.nodes[key];
  if(!node)return "";
  for(var name in model.phases){
    if(!Object.prototype.hasOwnProperty.call(model.phases,name))continue;
    if(model.phases[name].status==="failed"&&PHASE_NODE[name]===node.kind)return "fail";
  }
  return "live";
}

/* The run's state in one word, for the pill and for a test. */
function verdict(model){
  if(model.error)return "failed";
  if(model.done){
    var s=model.done.status;
    if(s===""||s==="ok"||s==="dry-run")return "complete";
    return s;
  }
  if(!model.job)return "idle";
  if(model.stream==="reconnecting")return "reconnecting";
  if(model.stream==="closed")return "disconnected";
  return "running";
}

/* --------------------------------------------------------------- render */

var $=function(id){return document.getElementById(id);};
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}

var NODE_LABEL={vpc:"VPC",subnet:"subnet",vcontroller:"vController",kvo:"KVO",vpb:"vPB",
  workloads:"workloads",eks:"EKS"};
// the instrument's own icon set, so this screen speaks the diagram's language
var NODE_ICON={vpc:"vpc",subnet:"vpc",vcontroller:"clms",kvo:"kvo",vpb:"vpb",workloads:"vm",eks:"coll"};
/* the mark on a timeline row. A row that is pending, or one a status of
   start/running arrived for, is drawn by CSS from the row's own class (a dot
   and a spinner); deploy-stack.sh reports a phase only when it ENDS, so
   done, failed and skipped are the three it writes today. */
var MARK={done:"✓",failed:"✕",skipped:"–"};
var WORKLOAD_CARDS=8;   // beyond this the stack says "+N" instead of drawing them

function icon(kind){
  var set=window.clConsole&&window.clConsole.icons;
  return (set&&set[NODE_ICON[kind]])||"";
}

function nodeSub(n){
  // only what the frame carried, in the order an operator reads it
  var bits=[];
  if(n.resource_id)bits.push(n.resource_id);
  if(n.ip)bits.push(n.ip);
  else if(n.private_ip)bits.push(n.private_ip);
  if(n.cluster)bits.push(n.cluster);
  if(n.zone)bits.push(n.zone);
  // mode means the tapping mode on an EKS node and the discovery mode on
  // workloads, where the tag is the thing worth reading instead
  if(n.kind==="workloads"){
    if(n.count)bits.push(n.count+" tagged");
    if(n.tag)bits.push(n.tag);
  }else if(n.mode)bits.push(n.mode);
  return bits.join(" · ");
}

function nodeTitle(n){
  var bits=[];
  READS.resource.forEach(function(f){if(n[f])bits.push(f+"="+n[f]);});
  return bits.join("  ");
}

function nodeHtml(model,key){
  var n=model.nodes[key],extra="";
  if(n.kind==="vpb"&&(n.ingress_ip||n.egress_ip)){
    extra='<div class="nics">'+
      nic("mgmt",n.ip)+nic("ingress",n.ingress_ip)+nic("egress",n.egress_ip)+'</div>';
  }
  if(n.kind==="workloads"){
    var total=parseInt(n.count,10);
    if(total>0){
      var drawn=Math.min(total,WORKLOAD_CARDS),cards="";
      for(var i=0;i<drawn;i++)cards+="<i></i>";
      extra='<div class="wstack">'+cards+(total>drawn?'<span>+'+esc(String(total-drawn))+"</span>":"")+"</div>";
    }
  }
  return '<div class="node '+esc(nodeState(model,key))+'" title="'+esc(nodeTitle(n))+'">'+
    '<div class="chip">'+icon(n.kind)+"</div>"+
    '<div class="nlab">'+esc(n.kind==="subnet"?(n.role||"subnet"):(NODE_LABEL[n.kind]||n.kind))+"</div>"+
    '<div class="nsub">'+esc(nodeSub(n))+"</div>"+extra+"</div>";
}

function nic(name,ip){
  return '<i class="'+(ip?"on":"")+'" title="'+esc(name+(ip?" "+ip:" (no address in the event)"))+'"></i>';
}

function renderHead(model){
  var v=verdict(model);
  var cls=v==="running"?"run":v==="complete"?"done":v==="idle"?"":"err";
  if(v==="reconnecting")cls="run";
  $("wPill").className="pill"+(cls?" "+cls:"");
  $("wPillTxt").textContent=v;
  var h=model.hello,chip=[];
  if(h.stack)chip.push("stack <b>"+esc(h.stack)+"</b>");
  if(h.region)chip.push(esc(h.region));
  if(h.account)chip.push("acct <b>"+esc(h.account)+"</b>");
  if(h.dryRun)chip.push("dry run");
  $("wChip").innerHTML=chip.join(" · ");
  $("wChip").hidden=!chip.length;
  $("wRunId").textContent=model.job?"job "+model.job:"";
  $("wConn").textContent=[model.note,model.action].filter(function(s){return !!s;}).join(" · ");
  $("wStop").hidden=!model.job||model.ended;
}

function renderPhases(model){
  var names=model.phaseOrder.slice();
  model.phaseSeen.forEach(function(n){if(names.indexOf(n)<0)names.push(n);});
  if(!names.length){
    $("wPhases").innerHTML='<li class="dim">The phase list arrives with the run: deploy-stack.sh sends it once, at the start.</li>';
    return;
  }
  $("wPhases").innerHTML=names.map(function(name){
    var p=model.phases[name],st=p?p.status:"pending";
    return '<li class="tlrow '+esc(st)+'"><span class="tlmark">'+esc(MARK[st]||"")+"</span>"+
      '<span class="tlname">'+esc(name)+"</span>"+
      '<span class="tlwhy">'+esc(p?(p.reason||(st==="pending"?"":st)):"")+"</span></li>";
  }).join("");
}

function renderTopo(model){
  if(!model.nodeOrder.length){
    $("wTopo").innerHTML='<p class="dim">Nothing yet: a node appears here when the run reports the resource behind it.</p>';
    return;
  }
  var inVpc=[],outside=[];
  model.nodeOrder.forEach(function(k){
    (model.nodes[k].kind==="vpc"||model.nodes[k].kind==="subnet"?inVpc:outside).push(k);
  });
  var html="";
  if(inVpc.length){
    html+='<div class="tvpc">'+inVpc.map(function(k){return nodeHtml(model,k);}).join("")+"</div>";
  }
  if(outside.length){
    html+='<div class="tnodes">'+outside.map(function(k){return nodeHtml(model,k);}).join("")+"</div>";
  }
  $("wTopo").innerHTML=html;
}

function renderLogins(model){
  if(!model.logins.length){
    $("wLogins").innerHTML='<p class="dim">The run announces each login as its appliance comes up.</p>';
    return;
  }
  $("wLogins").innerHTML=model.logins.map(function(l){
    // vPB announces an ssh command line, not a URL: only a real http(s) URL
    // is ever made a link
    var url=/^https?:\/\//i.test(l.url||"")
      ? '<a href="'+esc(l.url)+'" target="_blank" rel="noopener">'+esc(l.url)+"</a>"
      : "<code>"+esc(l.url||"")+"</code>";
    return '<div class="lgn"><b>'+esc(l.component||"")+"</b>"+url+
      "<div>user <code>"+esc(l.user||"")+"</code></div>"+
      '<div class="pwin">password: '+esc(l.password_in||"")+"</div></div>";
  }).join("");
}

function renderChecks(model){
  $("wChecksWrap").hidden=!model.checks.length;
  $("wChecks").innerHTML=model.checks.map(function(c){
    return '<tr><td><span class="st '+esc(c.status||"")+'">'+esc(String(c.status||"").toUpperCase())+"</span></td>"+
      "<td>"+esc(c.item||"")+"</td><td>"+(c.fix?esc(c.fix):'<span class="dim"></span>')+"</td></tr>";
  }).join("");
}

/* Every question the run asked, with the answer the stream recorded for it.
   A secret is recorded as asterisks by whoever answered it: nothing here has
   ever held the value. */
function renderQuestions(model){
  $("wQuestionsWrap").hidden=!model.prompts.length;
  $("wQuestions").innerHTML=model.prompts.map(function(p){
    // one wording for both branches, because both mean the same thing: no
    // `answered` frame carried a reply for this question. "waiting for an
    // answer" was a live-run claim this page could not make - a page that
    // attached after the question was answered would have said the run was
    // blocked on it. The default is still worth naming while the run is
    // going, because it is what pressing Enter alone would send.
    var unanswered='<span class="dim">no answer in the event stream</span>'+
      (!model.ended&&p.def?' <span class="dim">(default '+esc(p.def)+")</span>":"");
    // an empty answer is the script's own contract for "take the default"
    // (the comment above ask() in deploy-stack.sh), so it is named as one
    var given=p.answer===""?("(default)"+(p.def?" "+p.def:"")):p.answer;
    var state=p.answer===null?unanswered:"<code>"+esc(given)+"</code>";
    return '<div class="qcard"><div class="q">'+esc(p.question||"")+"</div>"+
      '<div class="a">'+state+"</div></div>";
  }).join("");
}

function renderBanner(model){
  var b=$("wBanner");
  if(model.error){
    b.hidden=false;b.className="banner bad";
    b.innerHTML="<b>The run ended</b><p>"+esc(model.error.text||"")+"</p>"+
      (model.error.fix?"<p>"+esc(model.error.fix)+"</p>":"");
    return;
  }
  if(!model.done){b.hidden=true;b.innerHTML="";return;}
  var d=model.done,ok=d.status===""||d.status==="ok"||d.status==="dry-run";
  var lines=[];
  if(d.summary)lines.push(d.summary);
  if(!ok){
    var why="Ended as "+(d.status||"?");
    if(d.phase)why+=" in phase "+d.phase;
    if(d.reason)why+=": "+d.reason;
    if(d.code)why+=" (exit "+d.code+")";
    lines.push(why);
  }
  if(d.report)lines.push("Report: "+d.report+" (on the machine running this console, next to the script)");
  if(d.profile)lines.push("Profile: "+d.profile+" (replay this deployment with --profile)");
  b.hidden=false;b.className="banner "+(ok?"good":"bad");
  b.innerHTML="<b>"+esc(ok?(d.status==="dry-run"?"Dry run complete":"Run complete"):"Run failed")+"</b>"+
    lines.map(function(l){return "<p>"+esc(l)+"</p>";}).join("");
}

/* The raw drawer, appended to and not rebuilt. Redrawing all 400 lines on
   every frame threw away the operator's selection in it while output was
   still streaming, and did it once per line of a run that prints thousands.
   Only the lines that arrived since the last draw are appended; the front is
   dropped over LOG_MAX, the way the model drops its own.

   logShown is how many of the run's lines are on the page (model.logCount
   counts every line, including the ones the model has since dropped), and
   logFor is the model those nodes belong to: attach() makes a new model, so
   a different object is a different run and the drawer starts over. */
var logShown=0,logFor=null;
var LOG_PIN=4;           // px of slack that still counts as "at the bottom"

function renderLog(model){
  $("wLogCount").textContent=model.logCount?model.logCount+(model.logCount===1?" line":" lines"):"";
  var box=$("wLog");
  var fresh=logFor!==model||model.logCount<logShown;
  if(fresh){
    box.innerHTML="";logFor=model;
    logShown=model.logCount-model.logs.length;   // what the model no longer holds
  }
  var added=model.logCount-logShown;
  if(added>model.logs.length)added=model.logs.length;  // the front was dropped
  if(added<=0)return;
  // a reader scrolled up in the drawer is reading it: only a reader already
  // at the tail is carried along by the new lines
  var pin=fresh||(box.scrollHeight-box.scrollTop-box.clientHeight)<LOG_PIN;
  for(var i=model.logs.length-added;i<model.logs.length;i++){
    var d=document.createElement("div");
    d.className="cln";
    d.textContent=model.logs[i];      // text, so nothing in a log line is markup
    box.appendChild(d);
  }
  logShown=model.logCount;
  while(box.childNodes.length>LOG_MAX)box.removeChild(box.firstChild);
  if(pin)box.scrollTop=box.scrollHeight;
}

/* The modal is the one piece of DOM a redraw must not rebuild: the operator
   is typing into it. It is filled when the open prompt CHANGES, and left
   alone otherwise. It is a div and not a <dialog> on purpose: Escape closes
   a dialog, and there is nothing to close here. The run is blocked on this
   answer and there is no cancel - which is why it must never open on a
   question that was already answered. It cannot, now: the `answered` frames
   replay with everything else, so a resumed page opens the modal only on a
   question the stream has no answer for.

   What is filled and what is left alone turns on one key, and that key is the
   JOB and the prompt id, never the id alone. Prompt ids are per-run counters
   (p1, p2, ...), so every run has a p4. Keyed on the bare id, attaching to a
   second run left the key from the first one standing: run B's p4 was read
   as the question already on screen, and the modal kept showing run A's
   question, run A's input type and whatever run A's operator had typed into
   it, while Send posted that value as the answer to run B's. A secret
   left in the box that way reached the stream verbatim, because run B's p4
   was a text question and the `answered` frame for a text question carries
   what was sent. The box is also emptied whenever the modal closes, so
   nothing typed outlives the question it was typed for. */
function renderModal(model){
  var host=$("wPrompt"),p=openPrompt(model),input=$("wPromptInput");
  if(!p||model.ended){
    host.hidden=true;
    if(host.dataset.promptId){host.dataset.promptId="";input.value="";}
    return;
  }
  var key=model.job+"/"+p.prompt_id;
  if(host.dataset.promptId!==key){
    host.dataset.promptId=key;
    $("wPromptQ").textContent=p.question||"The engine asks";
    $("wPromptHint").textContent=p.def?"Enter alone takes the default: "+p.def:"";
    input.type=p.kind==="secret"?"password":"text";
    input.value="";
    $("wPromptNote").textContent="";
    $("wPromptSend").disabled=false;
    host.hidden=false;
    input.focus();
    return;
  }
  host.hidden=false;
  $("wPromptNote").textContent=p.error||(p.sending?"sending...":"");
  $("wPromptSend").disabled=!!p.sending;
}

function draw(model){
  renderHead(model);renderPhases(model);renderTopo(model);renderLogins(model);
  renderChecks(model);renderQuestions(model);renderBanner(model);renderLog(model);renderModal(model);
}

/* One draw per animation frame, whatever the stream does.

   Every frame used to redraw all nine panels: thirteen innerHTML assignments,
   and a run that prints two thousand lines did that two thousand times. The
   browser cannot show more than one draw per frame anyway, so the work went
   nowhere, and while it was going nowhere it destroyed any selection the
   operator had made in the panels it rebuilt. render() now records the model
   and asks for a frame; the frames that arrive before it fires collapse into
   that one draw, always of the newest model.

   requestAnimationFrame is the right clock (a hidden tab stops asking for
   frames, and a hidden tab has nothing to draw), and the timeout is for
   where there is none: the node harness, chiefly. */
var frameQueued=false,frameModel=null;

function render(model){
  frameModel=model;
  if(frameQueued)return;
  frameQueued=true;
  var run=function(){
    frameQueued=false;
    var m=frameModel;frameModel=null;
    if(m)draw(m);
  };
  if(typeof requestAnimationFrame==="function")requestAnimationFrame(run);
  else setTimeout(run,16);
}

/* ---------------------------------------------------------- the stream */

var model=emptyModel(),es=null;
// every type this screen listens for. A named SSE event with no listener
// here is simply never delivered, so this list is what makes the console's
// own `answered` reach the page at all.
var TYPES=["hello","phases","phase","resource","check","login","prompt","answered","log","done"];
var JOB_RE=/^[A-Za-z0-9_-]+$/;     // server.py's job_id_ok

function detach(){
  if(es){es.close();es=null;}
}

function onFrame(e){
  var m=null;
  try{m=JSON.parse(e.data);}catch(err){return;}
  model.stream="live";model.note="";
  applyEvent(model,m);
  // the run is over and the server has said its last word: stop, or
  // EventSource reconnects to a stream that will only replay the same buffer
  if(model.ended)detach();
  render(model);
}

/* EventSource fires "error" for two different things: an engine error FRAME
   (it has data) and the transport. CONNECTING is EventSource retrying on its
   own, with Last-Event-ID, so the model is never reset: the replay picks up
   where this one stopped. */
function onError(e){
  if(e&&e.data)return onFrame(e);
  if(!es)return;
  if(es.readyState===EventSource.CLOSED){
    detach();
    model.stream="closed";
    model.note=model.events
      ? "The stream closed. Attach the run id again to pick it up: the run carries on without this page."
      : "This console is following no run with that id. A run started by another console process, or before it restarted, cannot be attached.";
  }else{
    model.stream="reconnecting";
    model.note="reconnecting...";
  }
  render(model);
}

/* Follow one job. A fresh model every time: the stream replays from id 0, so
   what is drawn is the whole run and not the tail of it. */
function attach(jobId){
  var id=txt(jobId).trim();
  detach();
  model=emptyModel();
  // a Stop pressed on the last run left its button disabled; this run has
  // its own, and its own question, whatever the last one's was called
  $("wStop").disabled=false;
  if(!JOB_RE.test(id)){
    model.note="A run id is letters, digits, - or _.";
    render(model);
    return;
  }
  model.job=id;model.stream="live";
  try{localStorage.setItem("cl-job",id);}catch(err){}
  if($("wJob").value!==id)$("wJob").value=id;
  es=new EventSource("/events/"+encodeURIComponent(id));
  TYPES.forEach(function(t){es.addEventListener(t,onFrame);});
  es.addEventListener("error",onError);
  render(model);
}

function send(){
  var p=openPrompt(model);
  if(!p||p.sending)return;
  var input=$("wPromptInput"),value=input.value;
  // exactly what the answered frame will carry, so the stream confirms what
  // the card already shows rather than rewriting it; an empty answer is left
  // empty here and named as the default where it is rendered
  var shown=p.kind==="secret"?"********":value;
  p.sending=true;p.error="";
  render(model);
  fetch("/api/answer/"+encodeURIComponent(model.job),{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({prompt_id:p.prompt_id,text:value})})
   .then(function(r){return r.text().then(function(body){
     var j=null;try{j=JSON.parse(body);}catch(err){}
     return {ok:r.ok,status:r.status,j:j};});})
   .catch(function(){return {err:"Could not reach the console server."};})
   .then(function(x){
     if(x.err||!x.ok){
       // the modal stays open with the reason on it: the run is still
       // blocked, and nothing else can be done from here
       noteAnswer(model,p.prompt_id,null,x.err||(x.j&&x.j.error)||("refused (HTTP "+x.status+")"));
       render(model);
       return;
     }
     input.value="";
     noteAnswer(model,p.prompt_id,shown,null);
     render(model);
   });
}

function init(){
  $("wResume").addEventListener("submit",function(e){e.preventDefault();attach($("wJob").value);});
  /* Stop is not a small button. /stop TERMs the run's whole process group
     and KILLs what is left after three seconds, so one stray click ends a
     deploy where it stands and leaves half a stack in the account: nothing
     is rolled back. It asks first, naming what it is about to stop (the
     teardown route makes the operator type the stack name back for the same
     reason), then says what came of it, because a stop that the server
     refused used to look exactly like one that worked. */
  $("wStop").addEventListener("click",function(){
    if(!model.job)return;
    var btn=this,job=model.job;
    var what=model.hello.stack?("stack "+model.hello.stack):("run "+job);
    if(!window.confirm("Stop "+what+"? The engine is signalled where it stands, and whatever it has already created in AWS stays there."))return;
    btn.disabled=true;
    // into the model's own action, not its note: the note is what the stream
    // is doing, and onFrame clears it on the very next frame the winding-down
    // run sends, which is where this sentence used to disappear
    var say=function(what){
      if(model.job!==job)return;    // the page has moved on to another run
      model.action=what;render(model);
    };
    fetch("/stop/"+encodeURIComponent(job),{method:"POST"})
     .then(function(r){
       if(r.ok){say("Stop sent to "+what+". The run ends after the step it is in; watch for its last event.");return;}
       btn.disabled=false;
       say("The console refused the stop (HTTP "+r.status+"). The run is still going.");
     })
     .catch(function(){
       btn.disabled=false;
       say("Could not reach the console server to stop the run. It is still going.");
     });
  });
  $("wPromptForm").addEventListener("submit",function(e){e.preventDefault();send();});
  var last="";
  try{last=localStorage.getItem("cl-job")||"";}catch(err){}
  if(last&&JOB_RE.test(last)){attach(last);}   // a run started before this page loaded
  else render(model);
}

if(typeof window!=="undefined")window.clWatch={
  applyEvent:applyEvent,noteAnswer:noteAnswer,emptyModel:emptyModel,
  openPrompt:openPrompt,nodeState:nodeState,verdict:verdict,
  READS:READS,PHASE_NODE:PHASE_NODE,LOG_MAX:LOG_MAX,PHASE_MAX:PHASE_MAX,
  render:render,attach:attach,
  model:function(){return model;}
};

// the model half runs anywhere (tests load this file under node); the DOM
// half only where the Watch page is
if(typeof document!=="undefined"&&document.getElementById("wPhases"))init();
})();
