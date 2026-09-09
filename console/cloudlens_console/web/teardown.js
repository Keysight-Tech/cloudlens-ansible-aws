(function(){
"use strict";
/* teardown.js: the Teardown screen, in the one order that is safe.

     1. the read-only audit    POST /api/teardown {orphans_only:true}
                               (teardown-stack.sh --orphans, which deletes
                               nothing, ever) and its report, read here.
     2. the licence warning    a stack with a KVO that is not KNOWN to be
                               clear of licences gets a red banner and a
                               pointer to the Licensing screen. Deleting
                               the KVO first strands the counts and they do
                               not come back; a release from a DIFFERENT
                               appliance releases nothing of this one's;
                               and a release that left licences behind
                               released only part of them. All three are
                               refusals here.
     3. the typed name         the stack's own name, typed back.
     4. the run                POST /api/teardown, streaming into Watch.
     5. the proof              GET /api/verify-empty.

   Nothing destructive happens before the typed name matches: the button
   is disabled until it does, and the API refuses the request anyway
   (confirm_name has to equal the stack), so the gate exists on both sides
   of the wire.

   Why the two runs are rendered differently. The audit's whole value is
   its report, and the report has to be READ NEXT TO the gate it informs,
   so its log lines are drawn inline here, on this screen, by this file.
   The destructive run is a run like any other, with a verdict, a banner
   and questions it may ask, and the console has one place for that: it is
   handed to watch.js's attach() and the page moves to Watch. Writing a
   second renderer for a run's phases and prompts here would be two things
   to keep true instead of one.

   teardownGate() is pure and lives under tests/test_ops_model.py. */

var U=window.clUi;                            // ui.js, loaded before this file
var $=U.$,txt=U.txt,esc=U.esc,status=U.status,hostOf=U.hostOf;
var enc=encodeURIComponent;
var LOG_MAX=400;

/* ------------------------------------------------------------ the model */

/* Whether the destructive run may start, and the warning that stands over
   it. `hasKvo` is true, false, or null for "could not tell", and null is
   never treated as false: a stack whose KVO could not be checked gets the
   warning too, because the cost of being wrong that way is a stranded
   licence count and the cost of being wrong the other way is one sentence
   the operator ignores.

   A recorded release satisfies the warning only when BOTH of these hold.

     it was made against THIS stack's KVO. The session record carries the
     appliance it was made against (licences.js noteRelease, keyed by
     host), and `kvoAddr` is the address of the kvo-role instance in
     /api/status's own list; the two are compared as hosts, not as
     substrings. Releasing on KVO A and then tearing down a stack whose
     KVO is B released nothing of B's, and telling the API
     licences_released:true would put --accept-licence-loss on the argv
     and satisfy the script's own licence gate for the wrong appliance.

     the KVO is now CLEAR. /api/licences/release answers `clear`, the
     KVO's own reading of whether it still holds a licence, and a release
     that succeeded on the rows it was asked about can still leave others
     installed. A partial release used to turn this banner green and send
     --accept-licence-loss, which is the last gate teardown-stack.sh has
     in a non-interactive run: the stack was then deleted with licences on
     it, which is exactly the loss this screen exists to prevent. The
     script's own words put a previous instance of it at 1500 counts.

     the release is still the LATEST word on that KVO. A record marked
     stale by licences.js (an activation landed on the appliance after the
     release, or it listed a licence it still holds) says what happened
     but no longer says what is true now.

   Either one missing is a `bad` warning, and each says which one, because
   "release the KVO's licences" and "finish releasing this KVO's licences"
   are different jobs. A KVO whose address the console could not read
   cannot be matched at all, and is treated the same way.

   `counts` requires hasKvo === true structurally, rather than leaning on
   every caller resetting kvoAddr whenever it resets hasKvo. It is the one
   value in this file that becomes --accept-licence-loss on a command
   line, so it does not depend on a convention being kept somewhere else:
   an address left over from a previous stack cannot arm a stack whose own
   KVO question came back unknown. */
function teardownGate(m){
  m=m||{};
  var stack=txt(m.stack),region=txt(m.region),typed=txt(m.typed);
  var released=m.released||null;
  var named=m.kvoName?" ("+txt(m.kvoName)+")":"";
  var mine=hostOf(m.kvoAddr);                       // this stack's own KVO
  var from=released?hostOf(released.kvo):"";        // where the release was made
  var ours=!!(released&&mine&&from&&from===mine);   // made against this stack's KVO
  // and it left the KVO clear, and nothing has been put back on it since.
  // `clear` is === true because licences.js writes exactly true or false
  // and the API's own answer is tri-state: a null is "could not read the
  // KVO's list", which is not a yes.
  var counts=!!(m.hasKvo===true&&ours&&released.clear===true&&released.stale!==true);
  var warn=null;
  // an empty form has no stack to have a KVO: the screen opens on one, and
  // a banner about an unchecked KVO over a form nobody has typed in yet is
  // noise that teaches the operator to scroll past this banner
  if(!stack||!region)warn=null;
  else if(m.hasKvo===true&&counts)
    warn={level:"good",text:"Licences were released from "+from+" in this session ("+
      (released.codes||[]).join(", ")+"), which is this stack's KVO"+named+
      ", and the KVO answered that it now holds none, so the teardown will run with "+
      "--accept-licence-loss."};
  else if(m.hasKvo===true&&ours&&released.stale===true)
    warn={level:"bad",text:"Licences were released from this stack's own KVO"+named+" at "+mine+
      " in this session ("+(released.codes||[]).join(", ")+"), but licences have been ACTIVATED on it "+
      "since, or it has listed licences it still holds, so that release is no longer evidence that it is "+
      "clear. The teardown will NOT run with --accept-licence-loss. Go back to the Licensing screen, list "+
      "what is installed and release it: a KVO deleted with licences still installed strands those counts, "+
      "and they do not come back."};
  else if(m.hasKvo===true&&ours&&released.unknown===true)
    warn={level:"bad",text:"A release was made against this stack's own KVO"+named+" at "+mine+
      " in this session ("+(released.codes||[]).join(", ")+"), but the KVO's licence list could not be READ "+
      "afterwards, so whether it still holds licences is unknown. An unread list is not an empty one. The "+
      "teardown will NOT run with --accept-licence-loss. Go back to the Licensing screen and list what is "+
      "installed: a KVO deleted with licences still installed strands those counts, and they do not come back."};
  else if(m.hasKvo===true&&ours)
    warn={level:"bad",text:"A release was made against this stack's own KVO"+named+" at "+mine+
      " in this session ("+(released.codes||[]).join(", ")+"), but the KVO answered that it STILL HOLDS "+
      "licences: that release covered only part of them. The teardown will NOT run with "+
      "--accept-licence-loss. Go back to the Licensing screen, list what is installed and release the "+
      "rest: a KVO deleted with licences still installed strands those counts, and they do not come back."};
  else if(m.hasKvo===true&&released&&!mine)
    warn={level:"bad",text:"Licences were released from "+from+" in this session, but this stack's KVO"+named+
      " has no address in the console's answer, so that release cannot be shown to be this stack's. The "+
      "teardown will NOT run with --accept-licence-loss. Check on the Licensing screen which appliance those "+
      "counts came from: a KVO deleted with licences still installed strands them, and they do not come back."};
  else if(m.hasKvo===true&&released)
    warn={level:"bad",text:"The licences released in this session came from "+from+", not from this stack's "+
      "KVO"+named+" at "+mine+". A release from another appliance releases nothing of this one's, so the "+
      "teardown will NOT run with --accept-licence-loss. Release "+mine+"'s licences on the Licensing screen "+
      "FIRST: a KVO deleted with licences still installed strands those counts, and they do not come back."};
  else if(m.hasKvo===true)
    warn={level:"bad",text:"This stack has a KVO"+named+
      " and nothing has been released in this session. Release its licences on the Licensing screen "+
      "FIRST: a KVO deleted with licences still installed strands those counts, and they do not come back."};
  else if(m.hasKvo===null)
    warn={level:"warn",text:"Whether this stack has a KVO could not be checked"+
      (m.kvoWhy?" ("+txt(m.kvoWhy)+")":"")+". If it has one, release its licences on the Licensing screen "+
      "before tearing it down: the counts do not come back."};
  var why="";
  if(!stack||!region)why="Name the stack and its region.";
  else if(m.running)why="A run is going for this stack; the console allows one engine per stack.";
  else if(m.auditFor!==stack+"/"+region)
    why="Run the audit first: it is read-only and it is what says what this stack leaves behind.";
  else if(typed!==stack)why="Type "+stack+" to arm the teardown.";
  return {armed:!why,why:why,warn:warn,
          // what POST /api/teardown is told, which is what decides
          // --accept-licence-loss on the script's command line. The API
          // trusts this by design, so this page is the only place the
          // release can be tied to the appliance it was made against.
          licencesReleased:counts};
}

/* The session release record this stack's gate should stand on: this
   KVO's own if the Licensing screen has one, and otherwise the most
   recent from anywhere, which is what lets the gate say "that was another
   appliance" instead of the weaker "nothing was released". */
function recordFor(addr,L){
  L=L||(typeof window!=="undefined"?window.clLicences:null);
  if(!L)return null;
  return L.released(addr)||L.latestRelease();
}

/* Whether this stack has a KVO and, when it has one, WHICH: the answer
   /api/status's instances cell gives, as {hasKvo, name, addr, why}.
   hasKvo is true, false, or null for "could not tell", and only a null
   carries a `why`.

   The KVO is read from `by_role`, which api.status computes over EVERY
   instance it found, and not from `rows`, which is only the first
   api.MAX_ROWS (50) of them. This screen read the rows: a stack with more
   than 50 live instances whose KVO sorted past the cut answered "this
   stack has no KVO", and false is the single value that draws no banner
   at all, so the teardown armed in silence over an appliance still
   holding its counts. Where no by_role arrives, a truncated list is
   "could not tell" unless the KVO is among the rows that did come: those
   rows are still evidence OF a KVO, they are just never evidence of its
   absence. */
function kvoAnswer(inst){
  var unknown=function(why){return {hasKvo:null,name:"",addr:"",why:txt(why)};};
  if(!inst||!Object.prototype.hasOwnProperty.call(inst,"value"))
    return unknown(inst&&inst.unavailable);
  var v=inst.value||{};
  var roles=v.by_role&&typeof v.by_role==="object"?v.by_role:null;
  var kvo=roles?(roles.kvo||null):kvoRow(v.rows);
  if(!kvo&&!roles&&v.truncated===true)
    return unknown("the console listed only the first "+((v.rows||[]).length)+" of "+v.count+
                   " instances, and a KVO outside that list would not be among them");
  return {hasKvo:!!kvo,name:kvo?txt(kvo.name):"",
          // the address the Licensing screen would have been pointed at; a
          // KVO with neither is "" and matches nothing, which is the point
          addr:kvo?txt(kvo.public_ip)||txt(kvo.private_ip):"",why:""};
}

/* The kvo-role instance among the rows /api/status listed: the first one,
   except that a running instance beats a stopped one. api.by_role picks
   by the same rule on its side, so the address the console licenses
   against and the address it matches a release to are one instance. This
   is the fallback for an answer that carries no by_role; where there is
   one, the server has seen every row and this has seen 50. */
function kvoRow(rows){
  var pick=null;
  (rows||[]).forEach(function(row){
    if(!row||row.role!=="kvo")return;
    if(!pick||(pick.state!=="running"&&row.state==="running"))pick=row;
  });
  return pick;
}

/* --------------------------------------------------------------- render */

/* hasKvo starts null, not false: until /api/status has answered, whether
   this stack has a KVO is not known, and false is the one value that
   draws no warning at all. The audit (--orphans, a handful of describe
   calls) routinely finishes before /api/status does, because that route
   serially does describe-instances, up to two vController calls and a 25
   second ssh, so a false here armed the run with no banner. It is reset
   on every audit and on every edit of the stack or the region, so a
   previous stack's answer is never read as this one's. */
var model={stack:"",region:"",typed:"",auditFor:"",running:false,hasKvo:null,kvoName:"",kvoAddr:"",
           kvoWhy:"",released:null};
var auditJob="",auditLines=0,es=null;

function forgetKvo(why){
  model.hasKvo=null;model.kvoName="";model.kvoAddr="";model.kvoWhy=txt(why);
}

function render(){
  model.stack=$("tdStack").value.trim();
  model.region=$("tdRegion").value.trim();
  model.typed=$("tdConfirm").value;
  model.released=recordFor(model.kvoAddr);
  var gate=teardownGate(model);
  var banner=$("tdWarn");
  if(gate.warn){
    banner.hidden=false;
    banner.className="banner "+(gate.warn.level==="good"?"good":"bad");
    banner.innerHTML="<b>"+esc(gate.warn.level==="good"?"Licences released":"Licences first")+"</b><p>"+
      esc(gate.warn.text)+"</p>";
    if(gate.warn.level!=="good"){
      // built, not written as markup with an id: the banner is rebuilt on
      // every keystroke, and an id looked up afterwards is an id that has
      // to exist in the page for a button that only exists here
      var go=document.createElement("button");
      go.type="button";go.className="chip";go.textContent="Go to Licensing";
      go.addEventListener("click",function(){if(window.clNav)window.clNav.show("licensing");});
      var wrap=document.createElement("p");wrap.appendChild(go);banner.appendChild(wrap);
    }
  }else{
    banner.hidden=true;banner.innerHTML="";
  }
  $("tdRun").disabled=!gate.armed;
  if(!gate.armed)status("tdRunNote",gate.why);
}

function logLine(text){
  var box=$("tdReport");
  var d=document.createElement("div");
  d.className="cln";
  d.textContent=text;                 // text, so nothing in a log line is markup
  box.appendChild(d);
  auditLines++;
  $("tdReportCount").textContent=auditLines+(auditLines===1?" line":" lines");
  while(box.childNodes.length>LOG_MAX)box.removeChild(box.firstChild);
  box.scrollTop=box.scrollHeight;
}

/* ------------------------------------------------------------- the calls */

/* The audit's own stream, read here. Only log frames matter: the script
   runs unwired (it has no events channel and refuses flags it does not
   know), so its output IS the report. */
function followAudit(jobId){
  if(es){es.close();es=null;}
  auditJob=jobId;auditLines=0;
  $("tdReport").innerHTML="";
  $("tdReportWrap").hidden=false;
  es=new EventSource("/events/"+enc(jobId));
  // narrate carries the one line the console adds: the command it ran, so
  // the report opens by saying what produced it. log carries the script's
  // own output, which is the report itself.
  ["narrate","log"].forEach(function(type){
    es.addEventListener(type,function(e){
      var m=null;try{m=JSON.parse(e.data);}catch(err){return;}
      logLine(txt(m.text));
    });
  });
  var end=function(word){
    if(es){es.close();es=null;}
    model.auditFor=model.stack+"/"+model.region;
    status("tdAuditStatus","The audit "+word+" ("+auditLines+" line"+(auditLines===1?"":"s")+
      "). Read it, then type the stack name below.");
    $("tdAudit").disabled=false;
    render();
  };
  es.addEventListener("done",function(){end("finished");});
  es.addEventListener("error",function(e){
    if(e&&e.data){
      var m=null;try{m=JSON.parse(e.data);}catch(err){m=null;}
      if(m&&m.text)logLine(m.text);
      end("ended with an error");
      return;
    }
    if(es&&es.readyState===EventSource.CLOSED)end("stopped");
  });
}

/* Leaving this screen mid-audit closed nothing: the EventSource stayed
   open for the life of the page, the server kept a client for a report
   nobody was reading, and coming back opened a second one beside it. The
   audit itself is read-only and finishes on its own; what stops here is
   the reading of it, and auditFor is deliberately not set, so the gate
   goes back to asking for an audit. */
function stopAudit(){
  if(!es)return;
  es.close();es=null;
  $("tdAudit").disabled=false;
  status("tdAuditStatus","The audit stream was closed when you left this screen, after "+auditLines+
    " line"+(auditLines===1?"":"s")+". Nothing was deleted (--orphans deletes nothing). Run it again "+
    "before tearing this stack down.");
  render();
}

function checkKvo(){
  var s=model.stack,r=model.region;
  U.get("/api/status?stack="+enc(s)+"&region="+enc(r),function(x){
    if(model.stack!==s||model.region!==r)return;      // the operator moved on
    var a=kvoAnswer(x.d&&x.d.instances);
    if(a.hasKvo===null)forgetKvo(a.why||U.why(x)||"the console could not read the instances");
    else{
      model.hasKvo=a.hasKvo;
      model.kvoName=a.name;
      model.kvoAddr=a.addr;
      model.kvoWhy="";
    }
    render();
  });
}

function audit(){
  // the previous answer is this stack's only while the fields have not
  // moved, and it is not this run's until this run's status call lands
  forgetKvo("the console is still reading the instances");
  render();
  if(!model.stack||!model.region)return status("tdAuditStatus","Name the stack and its region.",true);
  $("tdAudit").disabled=true;
  model.auditFor="";
  status("tdAuditStatus","auditing "+model.stack+" in "+model.region+"... (read-only: --orphans deletes nothing)");
  checkKvo();
  U.post("/api/teardown",{stack:model.stack,region:model.region,orphans_only:true},function(x){
    var why=U.why(x);
    if(why){
      $("tdAudit").disabled=false;
      return status("tdAuditStatus",why,true);
    }
    followAudit(x.d.job_id);
  });
}

function tearDown(){
  var gate=teardownGate(model);
  if(!gate.armed)return status("tdRunNote",gate.why,true);
  if(!window.confirm("Tear down "+model.stack+" in "+model.region+"? The stack is deleted and the volumes, "+
      "security groups and collector auto-scaling groups it left are swept. Nothing is rolled back."))return;
  $("tdRun").disabled=true;
  status("tdRunNote","starting the teardown...");
  U.post("/api/teardown",{stack:model.stack,region:model.region,confirm_name:model.typed,
                          licences_released:gate.licencesReleased},function(x){
    var why=U.why(x);
    if(why){
      render();
      return status("tdRunNote",why,true);
    }
    status("tdRunNote","Started job "+x.d.job_id+". It runs on the Watch screen; come back here and count "+
      "what is left when it ends.");
    if(window.clWatch)window.clWatch.attach(x.d.job_id);
    if(window.clNav)window.clNav.show("watch");
  });
}

var COUNTS=[["instances","instances (not terminated)"],["vpcs","VPCs (not the default one)"],
            ["volumes","EBS volumes"],["enis","network interfaces"],
            ["mirror_sessions","traffic mirror sessions"],["stacks","CloudFormation stacks"]];

function renderVerify(d){
  var host=$("tdVerifyCard");
  host.innerHTML='<div class="tblwrap"><table class="ref" aria-label="What is left in the region">'+
    "<thead><tr><th>What</th><th>Left</th></tr></thead><tbody>"+
    COUNTS.map(function(c){
      var cell=d[c[0]];
      var said=cell&&Object.prototype.hasOwnProperty.call(cell,"value")
        ? "<b>"+esc(String(cell.value))+"</b>"+
          (c[0]==="volumes"&&cell.detail?' <span class="dim">'+esc(cell.detail.available+" available, "+
            cell.detail.gb+" GB in all")+"</span>":"")
        : '<span class="dim">'+esc((cell&&cell.unavailable)||"not read")+"</span>"+
          (cell&&cell.command?'<div class="status">'+esc(cell.command)+"</div>":"");
      return "<tr><td>"+esc(c[1])+"</td><td>"+said+"</td></tr>";
    }).join("")+"</tbody></table></div>";
}

function verify(){
  var r=$("tdRegion").value.trim();
  if(!r)return status("tdVerifyStatus","Name the region.",true);
  $("tdVerify").disabled=true;
  status("tdVerifyStatus","counting what is left in "+r+"...");
  U.get("/api/verify-empty?region="+enc(r),function(x){
    $("tdVerify").disabled=false;
    var why=U.why(x);
    if(why)return status("tdVerifyStatus",why,true);
    renderVerify(x.d);
    status("tdVerifyStatus",x.d.empty===true
      ? ("Nothing of these is left in "+r+".")
      : x.d.empty===false
        ? ("Something is still in "+r+". These counts are the whole region, not only this stack: read the "+
           "rows before concluding the teardown missed anything.")
        : "At least one count could not be read, so this is not a proof either way. The rows say which.");
  });
}

function init(){
  $("tdForm").addEventListener("submit",function(e){e.preventDefault();audit();});
  $("tdRun").addEventListener("click",tearDown);
  $("tdVerify").addEventListener("click",verify);
  $("tdConfirm").addEventListener("input",render);
  // a stack or a region that changed makes the KVO answer somebody else's
  ["tdStack","tdRegion"].forEach(function(id){
    $(id).addEventListener("input",function(){forgetKvo("");render();});
  });
  // wizard.js owns the page switch and says so; the audit stream is the
  // one thing on this screen that outlives leaving it
  document.addEventListener("cl-page",function(e){
    if(!e||e.detail!=="teardown")stopAudit();
  });
  try{
    var saved=JSON.parse(localStorage.getItem("cl-plan")||"{}");
    if(saved&&typeof saved==="object"){
      if(typeof saved.CLOUDLENS_STACK_NAME==="string")$("tdStack").value=saved.CLOUDLENS_STACK_NAME;
      if(typeof saved.CLOUDLENS_REGION==="string")$("tdRegion").value=saved.CLOUDLENS_REGION;
    }
  }catch(e){}
  render();
}

if(typeof window!=="undefined")window.clTeardown={
  teardownGate:teardownGate,recordFor:recordFor,kvoRow:kvoRow,kvoAnswer:kvoAnswer,COUNTS:COUNTS,
  model:function(){return model;}
};

if(typeof document!=="undefined"&&document.getElementById("tdReport"))init();
})();
