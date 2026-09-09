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

var U=window.clUi;                            // ui.js, loaded before this file
var $=U.$,txt=U.txt,esc=U.esc,status=U.status,codeTail=U.codeTail;
var enc=encodeURIComponent;
var CODES_MAX=50;                             // api.MAX_LIST
var CODE_QTY_RE=/^[A-Za-z0-9][A-Za-z0-9-]{3,63}(?:,[0-9]{1,6})?$/;   // api.CODE_QTY

/* The phases a re-run SPENDS something on, and what it spends.

   deploy-stack.sh's run_phase tests --only BEFORE the resume skip, on
   purpose, so --only license runs the licensing phase on a stack whose
   licensing the resume state already calls done; and the script says in
   its own comment why that matters: "Activation codes are consumable:
   re-activating one that is already spent burns entitlement quantity."
   That is real money and it does not come back, so this phase is not an
   ordinary option in a dropdown.

   The list is here rather than in the answer because a phase the server
   forgot to mark would arrive as an ordinary option with no warning, and
   a gate that fails open is not a gate. The phase NAMES still come from
   the script alone (PHASE_ORDER, served with the status); this only says
   which of them costs something. */
var SPENDS={license:true};

/* ------------------------------------------------------------ the model */

/* The fields /api/status carries beside the instance table, in reading
   order, with the heading each one wears. */
var CELLS=[
  ["sensors","Sensors registered"],
  ["mirror","Mirror sessions"],
  ["vpb","vPB packet counters"]
];

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
function replayWhy(model,stack,region,busy,reading){
  if(busy)return "starting the engine...";
  if(reading)return "reading the status: the buttons open when the answer lands.";
  if(!stack||!region)return "Name the stack and its region, then read the status.";
  if(model.stack!==stack||model.region!==region)
    return "Read the status for "+stack+" in "+region+" first: the phases come from that answer.";
  if(!model.profile.present)
    return "There is no "+(model.profile.file||"deploy-profile-<stack>.env")+" next to the deploy script, "+
      "so there is nothing to replay. Plan and launch this stack on the Deploy screen: that is what writes it.";
  return "";
}

/* --------------------------------------------------------------- render */

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
    // a phase that spends says so in the option itself, so the warning is
    // read before the dropdown closes and not only after it is picked
    return '<option value="'+esc(p)+'">'+esc(p)+(SPENDS[p]?" (spends entitlement)":"")+"</option>";
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

var model=operateModel(null),busy=false,reading=false,codes=[];

function stack(){return $("opStack").value.trim();}
function region(){return $("opRegion").value.trim();}

/* The activation codes a licensing re-run needs, as chips of their last
   four characters. Same shape and same rule as the Licensing screen's: a
   password input because a textarea shows every code in clear on a
   browser that ignores the masking, and nothing here is stored. */
function paintCodes(){
  var list=$("opCodeList");list.innerHTML="";
  codes.forEach(function(c,i){
    var chip=document.createElement("span");chip.className="code";
    chip.appendChild(document.createTextNode(codeTail(c)));
    var rm=document.createElement("button");rm.type="button";rm.textContent="×";
    rm.setAttribute("aria-label","Remove the code ending "+c.split(",")[0].slice(-4));
    rm.addEventListener("click",function(){codes.splice(i,1);paintCodes();});
    chip.appendChild(rm);list.appendChild(chip);
  });
  status("opCodeCount",codes.length?codes.length+" code"+(codes.length===1?"":"s")+
    " will ride the argv as --kvo-codes":"",false);
}

function addCodes(text){
  var P=window.clPlan,bad=0,dup=0,over=0;
  (P?P.parseCodes(text):[]).forEach(function(c){
    if(!CODE_QTY_RE.test(c)){bad++;return;}
    if(codes.indexOf(c)>=0){dup++;return;}
    if(codes.length>=CODES_MAX){over++;return;}
    codes.push(c);
  });
  paintCodes();
  var notes=[];
  if(bad)notes.push(bad+" entr"+(bad===1?"y is":"ies are")+" not an activation code (CODE or CODE,QTY, a quantity of 1 to 6 digits)");
  if(dup)notes.push(dup+" already added");
  if(over)notes.push(over+" over the limit of "+CODES_MAX);
  if(notes.length)status("opCodeCount",$("opCodeCount").textContent+(codes.length?"; ":"")+notes.join("; ")+".",
    !!(bad||over));
}

function gate(){
  var why=replayWhy(model,stack(),region(),busy,reading);
  var only=$("opPhase").value;
  $("opResume").disabled=!!why;
  $("opRerun").disabled=!!why||!only;
  if(why)return status("opAction",why);
  if(SPENDS[only])
    status("opAction","Re-running "+only+" spends entitlement. --only runs the phase even when the resume "+
      "state calls it done, and re-activating a code that is already spent burns quantity that does not "+
      "come back. The codes above ride the argv as --kvo-codes; without them the phase cannot ask for "+
      "any, because the engine gives the script no terminal.",true);
}

function read(){
  var s=stack(),r=region();
  if(!s||!r)return status("opStatus","Name the stack and its region.",true);
  reading=true;
  gate();
  $("opRead").disabled=true;
  status("opStatus","reading "+s+" in "+r+"... (the sensor count is two vController calls and the vPB "+
    "counters come over SSH, so this can take up to a couple of minutes)");
  U.get("/api/status?stack="+enc(s)+"&region="+enc(r),function(x){
    reading=false;
    $("opRead").disabled=false;
    var why=U.why(x);
    if(why){
      model=operateModel(null);
      render(model);
      return status("opStatus",why,true);
    }
    model=operateModel(x.d);
    render(model);
    status("opStatus",model.error||(model.instanceNote+"."),!!model.error);
  });
}

/* Resume, and Re-run one phase. Both are POST /api/run with the stack name
   and no plan, which is the API's replay path: it refuses without the
   profile file, it takes the same one-engine-per-stack lock as a launch,
   and the run it starts is a run like any other, so the Watch screen
   follows it from here. Activation codes travel exactly as they do on a
   launch: argv, as --kvo-codes, registered with the job so the stream
   redacts them. */
function replay(only){
  if(busy)return;
  if(SPENDS[only]){
    if(!codes.length)
      return status("opAction","Re-running the "+only+" phase needs the activation codes. The engine runs "+
        "the script with no terminal, and kvo_license.py refuses without codes when stdin is not a TTY "+
        "(it prints \"no activation codes supplied and stdin is not a TTY\" and exits 2), so the phase "+
        "would fail rather than ask. Add the codes above first.",true);
    if(!window.confirm("Re-run the "+only+" phase with "+codes.length+" activation code"+
        (codes.length===1?"":"s")+"?\n\ndeploy-stack.sh runs --only "+only+" even when the resume state "+
        "says this phase is already done, and activation codes are consumable: re-activating one that is "+
        "already spent burns entitlement quantity. That is real money, and it does not come back."))return;
  }
  var s=stack(),r=region(),body={stack:s,region:r};
  if(only)body.only=only;
  if(codes.length)body.kvo_codes=codes.slice();
  busy=true;gate();
  status("opAction",only?("re-running "+only+"..."):"resuming...");
  U.post("/api/run",body,function(x){
    busy=false;
    var why=U.why(x);
    if(why){
      gate();
      return status("opAction",why,true);
    }
    // the buttons come back BEFORE the line that stands: gate() writes its
    // own message when it has one, and this is the one to leave on screen
    gate();
    status("opAction","Started job "+x.d.job_id+" on "+x.d.profile_file+
      (x.d.only?" (--only "+x.d.only+")":" (--resume)")+".");
    if(window.clWatch)window.clWatch.attach(x.d.job_id);
    if(window.clNav)window.clNav.show("watch");
  });
}

function init(){
  $("opForm").addEventListener("submit",function(e){e.preventDefault();read();});
  $("opResume").addEventListener("click",function(){replay("");});
  $("opRerun").addEventListener("click",function(){replay($("opPhase").value);});
  $("opPhase").addEventListener("change",gate);
  ["opStack","opRegion"].forEach(function(id){$(id).addEventListener("input",gate);});
  $("opCodeAdd").addEventListener("click",function(){
    addCodes($("opCodes").value);$("opCodes").value="";
  });
  $("opCodes").addEventListener("keydown",function(e){
    if(e.key==="Enter"){e.preventDefault();addCodes(this.value);this.value="";}
  });
  $("opCodes").addEventListener("paste",function(e){
    // the clipboard text as it is, before a password input strips newlines
    var text=e.clipboardData&&e.clipboardData.getData("text");
    if(!text)return;
    e.preventDefault();
    addCodes((this.value?this.value+"\n":"")+text);
    this.value="";
  });
  // the wizard's own plan is the likeliest stack to operate, so the fields
  // open on it; nothing here writes that back
  try{
    var saved=JSON.parse(localStorage.getItem("cl-plan")||"{}");
    if(saved&&typeof saved==="object"){
      if(typeof saved.CLOUDLENS_STACK_NAME==="string")$("opStack").value=saved.CLOUDLENS_STACK_NAME;
      if(typeof saved.CLOUDLENS_REGION==="string")$("opRegion").value=saved.CLOUDLENS_REGION;
    }
  }catch(e){}
  paintCodes();
  render(model);
}

if(typeof window!=="undefined")window.clOperate={
  operateModel:operateModel,replayWhy:replayWhy,say:say,CELLS:CELLS,SPENDS:SPENDS,
  render:render,read:read,model:function(){return model;},reading:function(){return reading;}
};

if(typeof document!=="undefined"&&document.getElementById("opInstances"))init();
})();
