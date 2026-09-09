(function(){
"use strict";
/* operate.js: the Operate screen, for a stack that is already up.

   Two halves, the way watch.js is split:

     operateModel(resp)  a pure function of one /api/status answer. No DOM,
                         no network, no globals, so tests/test_ops_model.py
                         runs it under node.
     render(model)       all of the DOM, coalesced behind one animation
                         frame, drawn from the model alone.

   Everything on this screen is one field of that answer, and every field
   carries its own state: a value, or the reason there is none and the
   command that would get one. This page NEVER turns a missing answer into
   a zero. An operator who reads "0 sensors" concludes the deploy failed;
   an operator who reads "no vController credentials file: log in at
   https://... to see" knows exactly what is true and what is not.

   The two buttons are replays, not launches. Both post to /api/run with
   the stack name and no plan, so both need deploy-profile-<stack>.env to
   already exist: the API refuses them otherwise and says to plan the
   deploy first. Resume is the CLI's own resume; Re-run adds --only PHASE,
   and the phases offered are the ones /api/status served out of
   deploy-stack.sh's own PHASE_ORDER. There is no copy of that list here:
   an empty answer offers nothing rather than inventing a phase. */

var $=function(id){return document.getElementById(id);};
var enc=encodeURIComponent;

/* ------------------------------------------------------------ the model */

/* The fields /api/status carries beside the instance table, in reading
   order, with the heading each one wears. */
var CELLS=[
  ["sensors","Sensors registered"],
  ["mirror","Mirror sessions"],
  ["vpb","vPB packet counters"]
];

function txt(v){return v===undefined||v===null?"":String(v);}
function has(o,k){return !!o&&Object.prototype.hasOwnProperty.call(o,k);}
function plural(n,word){return n+" "+word+(n===1?"":"s");}

/* One cell's value in words. Each is what the API measured and nothing
   more: the sensor count names the project it came from, the mirror count
   names its scope (the region, because KVO's sessions carry no stack tag),
   and the vPB's is the appliance's own output. */
function say(key,v){
  if(!v||typeof v!=="object")return txt(v);
  if(key==="sensors")
    return plural(v.sensors,"sensor")+" registered"+
      (v.project?" in project "+v.project:"")+(v.vcontroller?" (vController "+v.vcontroller+")":"");
  if(key==="mirror")
    return plural(v.sessions,"mirror session")+" "+txt(v.scope);
  if(key==="vpb")
    return txt(v.text)||"the vPB answered, and said nothing";
  return txt(v);
}

function cell(resp,key,title){
  var c=resp?resp[key]:null;
  if(!c||typeof c!=="object")
    return {key:key,title:title,state:"missing",text:"this console did not read that field",command:""};
  if(has(c,"value"))
    return {key:key,title:title,state:"ok",text:say(key,c.value),
            command:txt(c.value&&c.value.command)};
  return {key:key,title:title,state:"blind",text:txt(c.unavailable),command:txt(c.command)};
}

/* One /api/status answer as the screen's whole state. An error answer
   becomes `error` and nothing else: a half-drawn screen beside a refusal
   is how a stale instance table gets read as this stack's. */
function operateModel(resp){
  var m={stack:"",region:"",error:"",profile:{file:"",present:false},phases:[],
         instances:[],instanceNote:"",instanceState:"missing",instanceCommand:"",cells:[]};
  if(!resp||typeof resp!=="object")return m;
  if(resp.error||resp.errors){
    m.error=txt(resp.error||(resp.errors||[]).join("; "));
    return m;
  }
  m.stack=txt(resp.stack);m.region=txt(resp.region);
  if(resp.profile&&typeof resp.profile==="object")
    m.profile={file:txt(resp.profile.file),present:resp.profile.present===true};
  if(Array.isArray(resp.phases))
    m.phases=resp.phases.filter(function(p){return typeof p==="string"&&!!p;});
  var inst=resp.instances;
  if(inst&&has(inst,"value")){
    var v=inst.value||{};
    m.instanceState="ok";
    m.instances=(v.rows||[]).map(function(r){
      return {id:txt(r.id),name:txt(r.name),role:txt(r.role)||"(no role in the name)",
              state:txt(r.state),type:txt(r.type),key:txt(r.key),
              address:txt(r.public_ip)||txt(r.private_ip),
              addressKind:r.public_ip?"public":(r.private_ip?"private":"")};
    });
    m.instanceNote=plural(v.count||0,"instance")+" named "+(m.stack?m.stack+"-*":"after the stack")+
      (v.truncated?" (the first "+m.instances.length+" are listed)":"");
  }else if(inst&&typeof inst==="object"){
    m.instanceState="blind";
    m.instanceNote=txt(inst.unavailable);
    m.instanceCommand=txt(inst.command);
  }
  CELLS.forEach(function(c){m.cells.push(cell(resp,c[0],c[1]));});
  return m;
}

/* Why a replay cannot be started, in words, or "" when it can. The profile
   file is the whole gate: this screen replays a deploy somebody planned,
   it never composes one. */
function replayWhy(model,stack,region,busy){
  if(busy)return "starting the engine...";
  if(!stack||!region)return "Name the stack and its region, then read the status.";
  if(model.stack!==stack||model.region!==region)
    return "Read the status for "+stack+" in "+region+" first: the phases come from that answer.";
  if(!model.profile.present)
    return "There is no "+(model.profile.file||"deploy-profile-<stack>.env")+" next to the deploy script, "+
      "so there is nothing to replay. Plan and launch this stack on the Deploy screen: that is what writes it.";
  return "";
}

/* --------------------------------------------------------------- render */

function esc(s){
  var P=window.clPlan;
  return P?P.esc(s):String(s==null?"":s);
}

function renderInstances(model){
  var tb=$("opInstances");
  if(model.instanceState!=="ok"||!model.instances.length){
    tb.innerHTML='<tr><td colspan="5"><span class="dim">'+
      esc(model.instanceNote||"Nothing read yet: name the stack and its region above.")+"</span>"+
      (model.instanceCommand?'<div class="status">'+esc(model.instanceCommand)+"</div>":"")+"</td></tr>";
    return;
  }
  tb.innerHTML=model.instances.map(function(r){
    return "<tr><td>"+esc(r.role)+"</td><td><code>"+esc(r.name)+"</code></td>"+
      '<td><span class="st '+(r.state==="running"?"pass":"warn")+'">'+esc(r.state.toUpperCase())+"</span></td>"+
      "<td>"+(r.address?"<code>"+esc(r.address)+"</code> <span class=\"dim\">"+esc(r.addressKind)+"</span>":
        '<span class="dim">no address</span>')+"</td>"+
      "<td>"+esc(r.type)+"</td></tr>";
  }).join("");
}

function renderCells(model){
  var host=$("opCards");
  if(!model.cells.length){
    host.innerHTML='<p class="dim">Read the status to fill these in.</p>';
    return;
  }
  host.innerHTML=model.cells.map(function(c){
    var body=c.state==="ok"
      ? "<b>"+esc(c.text)+"</b>"
      : '<span class="dim">'+esc(c.text)+"</span>";
    return '<div class="lgn"><b>'+esc(c.title)+"</b><div>"+body+"</div>"+
      (c.command?'<div class="pwin">'+esc(c.command)+"</div>":"")+"</div>";
  }).join("");
}

function renderPhases(model){
  var sel=$("opPhase"),was=sel.value;
  if(!model.phases.length){
    sel.innerHTML='<option value="">no phase list yet</option>';
    sel.disabled=true;
    return;
  }
  sel.disabled=false;
  sel.innerHTML=model.phases.map(function(p){
    return '<option value="'+esc(p)+'">'+esc(p)+"</option>";
  }).join("");
  if(model.phases.indexOf(was)>-1)sel.value=was;
}

var frameQueued=false,frameModel=null;

function draw(model){
  renderInstances(model);
  renderCells(model);
  renderPhases(model);
  $("opProfile").textContent=model.profile.file
    ? (model.profile.present
        ? "Replaying "+model.profile.file+", the profile this stack was deployed from."
        : model.profile.file+" is not next to the deploy script: there is nothing to replay yet.")
    : "";
  gate();
}

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

/* ------------------------------------------------------------ the page */

var model=operateModel(null),busy=false,reading=false;

function stack(){return $("opStack").value.trim();}
function region(){return $("opRegion").value.trim();}
function status(id,text,bad){var el=$(id);el.textContent=text||"";el.classList.toggle("err",!!bad);}

function gate(){
  var why=replayWhy(model,stack(),region(),busy);
  $("opResume").disabled=!!why;
  $("opRerun").disabled=!!why||!$("opPhase").value;
  if(why)status("opAction",why);
}

function get(url,cb){
  fetch(url,{headers:{"Accept":"application/json"}}).then(function(r){
    return r.text().then(function(t){
      var d=null;try{d=JSON.parse(t);}catch(e){}
      if(!r.ok)return {err:(d&&d.error)||("HTTP "+r.status)};
      if(d&&d.error)return {err:d.error};
      return {d:d};
    });
  }).catch(function(){
    return {err:"Could not reach the console server."};
  }).then(function(x){cb(x.err||null,x.d);});
}

function read(){
  var s=stack(),r=region();
  if(!s||!r)return status("opStatus","Name the stack and its region.",true);
  reading=true;
  $("opRead").disabled=true;
  status("opStatus","reading "+s+" in "+r+"... (the vPB counters come over SSH, so this can take a few seconds)");
  get("/api/status?stack="+enc(s)+"&region="+enc(r),function(err,d){
    reading=false;
    $("opRead").disabled=false;
    if(err){
      model=operateModel(null);
      render(model);
      return status("opStatus",err,true);
    }
    model=operateModel(d);
    render(model);
    status("opStatus",model.error||(model.instanceNote+"."),!!model.error);
  });
}

/* Resume, and Re-run one phase. Both are POST /api/run with the stack name
   and no plan, which is the API's replay path: it refuses without the
   profile file, it takes the same one-engine-per-stack lock as a launch,
   and the run it starts is a run like any other, so the Watch screen
   follows it from here. */
function replay(only){
  if(busy)return;
  var s=stack(),r=region(),body={stack:s,region:r};
  if(only)body.only=only;
  busy=true;gate();
  status("opAction",only?("re-running "+only+"..."):"resuming...");
  fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})
   .then(function(resp){return resp.text().then(function(t){
     var d=null;try{d=JSON.parse(t);}catch(e){}
     return {ok:resp.ok,status:resp.status,d:d};});})
   .catch(function(){return {err:"Could not reach the console server."};})
   .then(function(x){
     busy=false;
     if(x.err||!x.ok||!x.d||x.d.error||x.d.errors){
       gate();
       var d=x.d||{};
       return status("opAction",x.err||d.error||(d.errors||[]).join("; ")||("The console refused it (HTTP "+x.status+")."),true);
     }
     status("opAction","Started job "+x.d.job_id+" on "+x.d.profile_file+
       (x.d.only?" (--only "+x.d.only+")":" (--resume)")+".");
     if(window.clWatch)window.clWatch.attach(x.d.job_id);
     if(window.clNav)window.clNav.show("watch");
     gate();
   });
}

function init(){
  $("opForm").addEventListener("submit",function(e){e.preventDefault();read();});
  $("opResume").addEventListener("click",function(){replay("");});
  $("opRerun").addEventListener("click",function(){replay($("opPhase").value);});
  $("opPhase").addEventListener("change",gate);
  ["opStack","opRegion"].forEach(function(id){$(id).addEventListener("input",gate);});
  // the wizard's own plan is the likeliest stack to operate, so the fields
  // open on it; nothing here writes that back
  try{
    var saved=JSON.parse(localStorage.getItem("cl-plan")||"{}");
    if(saved&&typeof saved==="object"){
      if(typeof saved.CLOUDLENS_STACK_NAME==="string")$("opStack").value=saved.CLOUDLENS_STACK_NAME;
      if(typeof saved.CLOUDLENS_REGION==="string")$("opRegion").value=saved.CLOUDLENS_REGION;
    }
  }catch(e){}
  render(model);
}

if(typeof window!=="undefined")window.clOperate={
  operateModel:operateModel,replayWhy:replayWhy,say:say,CELLS:CELLS,
  render:render,read:read,model:function(){return model;},reading:function(){return reading;}
};

if(typeof document!=="undefined"&&document.getElementById("opInstances"))init();
})();
