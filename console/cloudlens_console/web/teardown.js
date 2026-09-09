(function(){
"use strict";
/* teardown.js: the Teardown screen, in the one order that is safe.

     1. the read-only audit    POST /api/teardown {orphans_only:true}
                               (teardown-stack.sh --orphans, which deletes
                               nothing, ever) and its report, read here.
     2. the licence warning    a stack with a KVO whose licences have not
                               been released in this session gets a red
                               banner and a pointer to the Licensing
                               screen. Deleting the KVO first strands the
                               counts and they do not come back.
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

var $=function(id){return document.getElementById(id);};
var enc=encodeURIComponent;
var LOG_MAX=400;

/* ------------------------------------------------------------ the model */

function txt(v){return v===undefined||v===null?"":String(v);}

/* Whether the destructive run may start, and the warning that stands over
   it. `hasKvo` is true, false, or null for "could not tell", and null is
   never treated as false: a stack whose KVO could not be checked gets the
   warning too, because the cost of being wrong that way is a stranded
   licence count and the cost of being wrong the other way is one sentence
   the operator ignores. */
function teardownGate(m){
  m=m||{};
  var stack=txt(m.stack),region=txt(m.region),typed=txt(m.typed);
  var released=m.released||null;
  var warn=null;
  if(m.hasKvo===true&&!released)
    warn={level:"bad",text:"This stack has a KVO"+(m.kvoName?" ("+txt(m.kvoName)+")":"")+
      " and nothing has been released in this session. Release its licences on the Licensing screen "+
      "FIRST: a KVO deleted with licences still installed strands those counts, and they do not come back."};
  else if(m.hasKvo===true&&released)
    warn={level:"good",text:"Licences were released from "+txt(released.kvo)+" in this session ("+
      (released.codes||[]).join(", ")+"), so the teardown will run with --accept-licence-loss."};
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
          // --accept-licence-loss on the script's command line
          licencesReleased:!!released};
}

/* --------------------------------------------------------------- render */

function esc(s){
  var P=window.clPlan;
  return P?P.esc(s):String(s==null?"":s);
}

var model={stack:"",region:"",typed:"",auditFor:"",running:false,hasKvo:false,kvoName:"",kvoWhy:"",released:null};
var auditJob="",auditLines=0,es=null;

function status(id,text,bad){var el=$(id);el.textContent=text||"";el.classList.toggle("err",!!bad);}

function render(){
  model.stack=$("tdStack").value.trim();
  model.region=$("tdRegion").value.trim();
  model.typed=$("tdConfirm").value;
  model.released=window.clLicences?window.clLicences.released():null;
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

function post(path,body,cb){
  fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})
   .then(function(r){return r.text().then(function(t){
     var d=null;try{d=JSON.parse(t);}catch(e){}
     return {ok:r.ok,status:r.status,d:d};});})
   .catch(function(){return {err:"Could not reach the console server."};})
   .then(function(x){cb(x);});
}

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

/* Whether this stack has a KVO, from the one read-only route that knows:
   the instances it named. A failure is null, not false. */
function checkKvo(){
  var s=model.stack,r=model.region;
  fetch("/api/status?stack="+enc(s)+"&region="+enc(r),{headers:{"Accept":"application/json"}})
   .then(function(resp){return resp.json();})
   .then(function(d){
     if(model.stack!==s||model.region!==r)return;      // the operator moved on
     var inst=d&&d.instances;
     if(!inst||!Object.prototype.hasOwnProperty.call(inst,"value")){
       model.hasKvo=null;
       model.kvoWhy=txt((inst&&inst.unavailable)||(d&&d.error)||"the console could not read the instances");
     }else{
       var kvo=null;
       (inst.value.rows||[]).forEach(function(row){if(row.role==="kvo")kvo=row;});
       model.hasKvo=!!kvo;
       model.kvoName=kvo?txt(kvo.name):"";
       model.kvoWhy="";
     }
     render();
   })
   .catch(function(){
     model.hasKvo=null;model.kvoWhy="the console server could not be reached";render();
   });
}

function audit(){
  render();
  if(!model.stack||!model.region)return status("tdAuditStatus","Name the stack and its region.",true);
  $("tdAudit").disabled=true;
  model.auditFor="";
  status("tdAuditStatus","auditing "+model.stack+" in "+model.region+"... (read-only: --orphans deletes nothing)");
  checkKvo();
  post("/api/teardown",{stack:model.stack,region:model.region,orphans_only:true},function(x){
    if(x.err||!x.ok||!x.d||x.d.error){
      $("tdAudit").disabled=false;
      var d=x.d||{};
      return status("tdAuditStatus",x.err||d.error||("The console refused the audit (HTTP "+x.status+")."),true);
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
  post("/api/teardown",{stack:model.stack,region:model.region,confirm_name:model.typed,
                        licences_released:gate.licencesReleased},function(x){
    if(x.err||!x.ok||!x.d||x.d.error){
      var d=x.d||{};
      render();
      return status("tdRunNote",x.err||d.error||("The console refused it (HTTP "+x.status+")."),true);
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
  fetch("/api/verify-empty?region="+enc(r),{headers:{"Accept":"application/json"}})
   .then(function(resp){return resp.text().then(function(t){
     var d=null;try{d=JSON.parse(t);}catch(e){}
     return {ok:resp.ok,status:resp.status,d:d};});})
   .catch(function(){return {err:"Could not reach the console server."};})
   .then(function(x){
     $("tdVerify").disabled=false;
     if(x.err||!x.ok||!x.d||x.d.error){
       var d=x.d||{};
       return status("tdVerifyStatus",x.err||d.error||("HTTP "+x.status),true);
     }
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
  ["tdStack","tdRegion","tdConfirm"].forEach(function(id){$(id).addEventListener("input",render);});
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
  teardownGate:teardownGate,COUNTS:COUNTS,model:function(){return model;}
};

if(typeof document!=="undefined"&&document.getElementById("tdReport"))init();
})();
