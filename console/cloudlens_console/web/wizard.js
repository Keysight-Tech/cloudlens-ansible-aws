(function(){
"use strict";
/* wizard.js: the operations pages and the six-screen deploy wizard.

   One `plan` object, keyed by the profile keys (deploy/profile-keys.txt),
   is the whole state: every screen writes into it, POST /api/plan renders
   it as the profile deploy-stack.sh replays, POST /api/run starts the
   engine on it. A conditional part of a screen (the management plane, the
   collector placement, the EKS mode) is shown with `hidden` and its keys
   are DELETED from the plan when it is hidden, so the profile never
   carries a stale key. Secrets never enter the plan or localStorage: they
   are read from their password fields at Launch and cleared. */
var $=function(id){return document.getElementById(id);};
var P=window.clPlan, C=window.clConsole, W=window.clWatch, esc=P.esc;
// codeTail is a rule three screens have to agree on exactly, so there is one
// of it, in ui.js, and this file takes it from there rather than keeping a
// second copy that can drift from the other two
var codeTail=window.clUi.codeTail;
var enc=encodeURIComponent;

/* The words the script accepts for each choice key. test_web_static holds
   these equal to deploy-stack.sh's own messages, so a renamed value there
   fails a test here. */
var VALUES={
  CLOUDLENS_INFRA:["new","existing"],
  CLOUDLENS_TAPPING:["sensors","mirror","both","none"],
  CLOUDLENS_SENSOR_MODE:["standalone","kvo"],
  CLOUDLENS_WORKLOAD_CHOICE:["existing","test","later"],
  CLOUDLENS_EKS_MODE:["daemonset","sidecar"],
  CLOUDLENS_VCONTROLLER_TYPE:["t3.xlarge","m5.xlarge"]
};
// the API's rules, re-applied here so a bad value is said beside its field
var REGION_RE=/^[a-z]{2}(-[a-z]+)+-\d$/;
var STACK_RE=/^[A-Za-z][A-Za-z0-9-]*$/, STACK_MAX=128;   // deploy-stack.sh valid_stack_name()
var DEBOUNCE=400;
var PAGES=["preflight","deploy","watch","operate","licensing","teardown"];
/* The pages the quick-flow instrument shows under. Watch is not one of them
   any more: watch.js owns that screen, and a run launched here streams into
   it. The instrument stays on the Deploy page, where the quick flows it
   belongs to live. */
var RUN_PAGES={deploy:true};
var OS=["ubuntu","rhel","windows"];

/* ------------------------------------------------------------- state */
/* The keys this file writes, and the only ones a restored plan keeps.
   localStorage is not ours: a key retired from deploy/profile-keys.txt, or
   anything else planted under cl-plan, would otherwise ride into /api/plan
   and come back as "<KEY>: not a profile key" for a key no screen owns,
   with Launch off and nothing on the page able to clear it. Only these
   keys survive the load, and only with string values. test_web_static
   holds this list equal to every CLOUDLENS_ literal in this file. */
var OWN_KEYS=["CLOUDLENS_REGION","CLOUDLENS_STACK_NAME","CLOUDLENS_INFRA","CLOUDLENS_KEY_NAME",
  "CLOUDLENS_EXISTING_VPC_ID","CLOUDLENS_EXISTING_SUBNET_ID","CLOUDLENS_DEPLOY_KVO","CLOUDLENS_DEPLOY_VPB",
  "CLOUDLENS_VCONTROLLER_TYPE","CLOUDLENS_TAPPING","CLOUDLENS_SENSOR_MODE","CLOUDLENS_WORKLOAD_CHOICE",
  "CLOUDLENS_DISCOVERY_TAG_KEY","CLOUDLENS_DISCOVERY_TAG_VALUE","CLOUDLENS_SOURCE_VPCS","CLOUDLENS_TEST_VMS",
  "CLOUDLENS_COLLECTOR_ZONE","CLOUDLENS_COLLECTOR_MGMT_SUBNET","CLOUDLENS_COLLECTOR_INGRESS_SUBNET",
  "CLOUDLENS_COLLECTOR_EGRESS_SUBNET","CLOUDLENS_DEPLOY_EKS","CLOUDLENS_EKS_CLUSTER","CLOUDLENS_EKS_SAMPLE",
  "CLOUDLENS_EKS_MODE"];
var plan={};
try{
  var saved=JSON.parse(localStorage.getItem("cl-plan")||"{}");
  if(saved&&typeof saved==="object"&&!Array.isArray(saved))
    OWN_KEYS.forEach(function(k){if(typeof saved[k]==="string")plan[k]=saved[k];});
}catch(e){}
function seed(k,v){if(!(k in plan))plan[k]=v;}
// the interview's own defaults, so an untouched wizard plans what an
// all-Enter interview would
seed("CLOUDLENS_REGION","us-east-1");seed("CLOUDLENS_STACK_NAME","cloudlens-stack");
seed("CLOUDLENS_INFRA","new");seed("CLOUDLENS_DEPLOY_KVO","false");seed("CLOUDLENS_DEPLOY_VPB","false");
seed("CLOUDLENS_VCONTROLLER_TYPE","t3.xlarge");seed("CLOUDLENS_TAPPING","sensors");
seed("CLOUDLENS_WORKLOAD_CHOICE","test");seed("CLOUDLENS_DEPLOY_EKS","false");

function save(){try{localStorage.setItem("cl-plan",JSON.stringify(plan));}catch(e){}}
function set(k,v){if(v===undefined||v===null)delete plan[k];else plan[k]=String(v);}
function unset(){for(var i=0;i<arguments.length;i++)delete plan[arguments[i]];}
function has(k){return Object.prototype.hasOwnProperty.call(plan,k);}
function tapping(){return VALUES.CLOUDLENS_TAPPING.indexOf(plan.CLOUDLENS_TAPPING)>-1?plan.CLOUDLENS_TAPPING:"sensors";}
function hasSensors(){var t=tapping();return t==="sensors"||t==="both";}
function hasMirror(){var t=tapping();return t==="mirror"||t==="both";}
function kvo(){return plan.CLOUDLENS_DEPLOY_KVO==="true";}
function vpb(){return plan.CLOUDLENS_DEPLOY_VPB==="true";}
function existing(){return plan.CLOUDLENS_INFRA==="existing";}
function eks(){return plan.CLOUDLENS_DEPLOY_EKS==="true";}
function eksSample(){return eks()&&plan.CLOUDLENS_EKS_SAMPLE==="true";}
function workload(){return VALUES.CLOUDLENS_WORKLOAD_CHOICE.indexOf(plan.CLOUDLENS_WORKLOAD_CHOICE)>-1?plan.CLOUDLENS_WORKLOAD_CHOICE:"test";}
function collectorShown(){return hasMirror()&&existing();}
function sourceVpcs(){return (plan.CLOUDLENS_SOURCE_VPCS||"").split(",").map(function(s){return s.trim();}).filter(function(s){return s;});}

/* derive(): the conditional keys. Runs after every change, before every
   paint and every POST. A part that is hidden has its keys deleted; a
   part that is shown and has no answer yet gets the interview's default. */
function derive(){
  if(!existing())unset("CLOUDLENS_EXISTING_VPC_ID","CLOUDLENS_EXISTING_SUBNET_ID");
  if(!(plan.CLOUDLENS_KEY_NAME||"").trim())unset("CLOUDLENS_KEY_NAME");
  if(kvo()&&hasSensors()){if(VALUES.CLOUDLENS_SENSOR_MODE.indexOf(plan.CLOUDLENS_SENSOR_MODE)<0)plan.CLOUDLENS_SENSOR_MODE="standalone";}
  else unset("CLOUDLENS_SENSOR_MODE");
  if(tapping()==="none"){
    unset("CLOUDLENS_WORKLOAD_CHOICE","CLOUDLENS_DISCOVERY_TAG_KEY","CLOUDLENS_DISCOVERY_TAG_VALUE","CLOUDLENS_TEST_VMS","CLOUDLENS_SOURCE_VPCS");
  }else{
    plan.CLOUDLENS_WORKLOAD_CHOICE=workload();
    var w=workload();
    if(w==="existing"){seed("CLOUDLENS_DISCOVERY_TAG_KEY","cloudlens");seed("CLOUDLENS_DISCOVERY_TAG_VALUE","yes");
      if(!sourceVpcs().length)unset("CLOUDLENS_SOURCE_VPCS");}
    else unset("CLOUDLENS_DISCOVERY_TAG_KEY","CLOUDLENS_DISCOVERY_TAG_VALUE","CLOUDLENS_SOURCE_VPCS");
    if(w==="test")seed("CLOUDLENS_TEST_VMS","ubuntu:1,rhel:1,windows:1");
    else unset("CLOUDLENS_TEST_VMS");
  }
  if(!collectorShown())unset("CLOUDLENS_COLLECTOR_ZONE","CLOUDLENS_COLLECTOR_MGMT_SUBNET","CLOUDLENS_COLLECTOR_INGRESS_SUBNET","CLOUDLENS_COLLECTOR_EGRESS_SUBNET");
  if(!eks())unset("CLOUDLENS_EKS_CLUSTER","CLOUDLENS_EKS_SAMPLE","CLOUDLENS_EKS_MODE");
  else{
    seed("CLOUDLENS_EKS_SAMPLE","false");
    if(VALUES.CLOUDLENS_EKS_MODE.indexOf(plan.CLOUDLENS_EKS_MODE)<0)plan.CLOUDLENS_EKS_MODE="daemonset";
    if(eksSample())unset("CLOUDLENS_EKS_CLUSTER");
  }
  save();
}

/* ------------------------------------------------------------- helpers */
function status(id,text,bad){var el=$(id);el.textContent=text||"";el.classList.toggle("err",!!bad);}
function setVal(id,v){var el=$(id);v=v==null?"":String(v);if(el.value!==v)el.value=v;}
function setSwitch(el,on){el.setAttribute("aria-checked",on?"true":"false");}
/* Show or hide a part of the secrets block. A part that no longer applies
   is EMPTIED as it hides: a mirror access key typed under "both" would
   otherwise sit in the DOM after the choice became "sensors", where
   nothing on the page shows it and secretsNow() no longer reads it. */
function hideSecrets(sec,hide){
  sec.hidden=hide;
  if(hide)sec.querySelectorAll("input").forEach(function(i){i.value="";});
}
function region(){return plan.CLOUDLENS_REGION||"";}
function regionOk(){return REGION_RE.test(region());}

/* GET url -> cb(error, data). An API error ({error}) and a 403 or 500 come
   back as their message text; a body that is not JSON as its status.

   Both of these resolve to {err,d} and call cb exactly once, in a final
   then with nothing after it. A catch placed after the then that calls cb
   would swallow whatever the callback threw and then call cb AGAIN with
   "Could not reach the console server.": at Launch that read as the server
   being unreachable while the secrets were already cleared and the run
   already going. This way a callback that throws becomes an unhandled
   rejection (the console's own error), never a second answer. */
function api(url,cb){
  fetch(url,{headers:{"Accept":"application/json"}}).then(function(r){
    return r.text().then(function(t){
      var d=null;try{d=JSON.parse(t);}catch(e){}
      if(!r.ok)return {err:(d&&d.error)||("HTTP "+r.status+(t?": "+t.slice(0,160):""))};
      if(d&&d.error)return {err:d.error};
      return {d:d};
    });
  }).catch(function(){
    return {err:"Could not reach the console server."};
  }).then(function(x){cb(x.err||null,x.d);});
}
function post(url,body,cb){
  fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}).then(function(r){
    return r.text().then(function(t){
      var d=null;try{d=JSON.parse(t);}catch(e){}
      return {d:{ok:r.ok,status:r.status,d:d||{error:"HTTP "+r.status+(t?": "+t.slice(0,160):"")}}};
    });
  }).catch(function(){
    return {err:"Could not reach the console server."};
  }).then(function(x){cb(x.err||null,x.d);});
}
/* A row of a pick table. The choice is a real <button> in the first cell:
   a <tr role="button"> replaces the row's own semantics, and a table whose
   rows are buttons stops being a table to a screen reader. The row keeps
   the hover, the picked background and the mouse click, so the visual
   language is still the row; the keyboard reaches the button. */
function pickRow(tr,on,onPick){
  tr.className="pickrow"+(on?" on":"");
  var b=tr.querySelector("button.pickb");
  b.setAttribute("aria-pressed",on?"true":"false");
  b.addEventListener("click",onPick);
  tr.addEventListener("click",function(e){if(e.target!==b)onPick();});
}

/* ------------------------------------------------------------- pages */
function showPage(name){
  if(PAGES.indexOf(name)<0)name="deploy";
  document.querySelectorAll("#opsNav [data-page]").forEach(function(b){b.setAttribute("aria-selected",b.dataset.page===name?"true":"false");b.tabIndex=b.dataset.page===name?0:-1;});
  document.querySelectorAll("[data-page-body]").forEach(function(s){s.hidden=s.dataset.pageBody!==name;});
  var rv=$("runView"),was=rv.hidden;
  rv.hidden=!RUN_PAGES[name];
  if(was&&!rv.hidden)C.relayout();
  if(name==="preflight"&&!$("pfRegion").value)$("pfRegion").value=region();
  // the screens that live in their own files hear which page is showing:
  // teardown.js holds an EventSource open while its audit streams, and a
  // stream nobody is reading is a client the server keeps for nothing
  try{document.dispatchEvent(new CustomEvent("cl-page",{detail:name}));}catch(e){}
  try{localStorage.setItem("cl-page",name);}catch(e){}
}
document.querySelectorAll("#opsNav [data-page]").forEach(function(b){
  b.addEventListener("click",function(){showPage(b.dataset.page);});
  b.addEventListener("keydown",function(e){
    var i=PAGES.indexOf(b.dataset.page);
    if(e.key==="ArrowRight"||e.key==="ArrowLeft"){e.preventDefault();var n=PAGES[(i+(e.key==="ArrowRight"?1:PAGES.length-1))%PAGES.length];showPage(n);document.querySelector('#opsNav [data-page="'+n+'"]').focus();}
  });
});

/* ------------------------------------------------------------- discovery */
var disco={vpcs:{},subnets:{},eks:{}};    // by region, region+vpc, region

function loadVpcs(cb,force){
  var r=region();
  if(!regionOk())return cb("Set a valid region first (screen 1).");
  if(disco.vpcs[r]&&!force)return cb(null,disco.vpcs[r]);
  api("/api/discover/vpcs?region="+enc(r),function(err,d){
    if(region()!==r)return;               // the region changed while this was out
    if(err)return cb(err);
    disco.vpcs[r]=Array.isArray(d)?d:[];cb(null,disco.vpcs[r]);
  });
}
function loadSubnets(vpc,cb,force){
  var r=region(),k=r+" "+vpc;
  if(!regionOk())return cb("Set a valid region first (screen 1).");
  if(disco.subnets[k]&&!force)return cb(null,disco.subnets[k]);
  api("/api/discover/subnets?region="+enc(r)+"&vpc="+enc(vpc),function(err,d){
    if(region()!==r)return;
    if(err)return cb(err);
    disco.subnets[k]=Array.isArray(d)?d:[];cb(null,disco.subnets[k]);
  });
}
function loadEks(cb,force){
  var r=region();
  if(!regionOk())return cb("Set a valid region first (screen 1).");
  if(disco.eks[r]&&!force)return cb(null,disco.eks[r]);
  api("/api/discover/eks?region="+enc(r),function(err,d){
    if(region()!==r)return;
    if(err)return cb(err);
    disco.eks[r]=Array.isArray(d)?d:[];cb(null,disco.eks[r]);
  });
}

/* ------------------------------------------------------------- screen 1 */
function renderVpcs(rows){
  var tb=$("vpcRows");tb.innerHTML="";
  if(!rows.length){tb.innerHTML='<tr><td colspan="3" class="dim">No VPC in '+esc(region())+'.</td></tr>';return;}
  rows.forEach(function(v){
    var tr=document.createElement("tr");
    tr.innerHTML='<td><button type="button" class="pickb">'+esc(v.id)+'</button></td><td>'+esc(v.name||"")+'</td><td>'+esc(v.cidr)+'</td>';
    pickRow(tr,plan.CLOUDLENS_EXISTING_VPC_ID===v.id,function(){
      if(plan.CLOUDLENS_EXISTING_VPC_ID!==v.id){
        set("CLOUDLENS_EXISTING_VPC_ID",v.id);
        // a new VPC: its subnets are other subnets
        unset("CLOUDLENS_EXISTING_SUBNET_ID","CLOUDLENS_COLLECTOR_ZONE","CLOUDLENS_COLLECTOR_MGMT_SUBNET","CLOUDLENS_COLLECTOR_INGRESS_SUBNET","CLOUDLENS_COLLECTOR_EGRESS_SUBNET");
      }
      // re-rendered from the cache, so the picked row reads as picked:
      // the pick is drawn at render time, and this table was the one that
      // never redrew itself after a choice (showSubnets is showVpcs's own
      // last step)
      sync();showVpcs();
    });
    tb.appendChild(tr);
  });
}
function showVpcs(force){
  status("vpcStatus","looking...");
  loadVpcs(function(err,rows){
    if(err){status("vpcStatus",err,true);$("vpcRows").innerHTML="";return;}
    status("vpcStatus",rows.length+" VPC"+(rows.length===1?"":"s")+" in "+region()+". Pick the one the appliances land in.");
    renderVpcs(rows);showSubnets();
  },force);
}
function subnetLabel(s){return s.id+(s.name?"  "+s.name:"")+"  "+s.az+"  "+s.cidr+(s.public?"  public":"  private");}
function renderSubnets(rows){
  var tb=$("subnetRows");tb.innerHTML="";
  if(!rows.length){tb.innerHTML='<tr><td colspan="5" class="dim">No subnet in this VPC.</td></tr>';return;}
  rows.forEach(function(s){
    var tr=document.createElement("tr");
    tr.innerHTML='<td><button type="button" class="pickb">'+esc(s.id)+'</button></td><td>'+esc(s.name||"")+'</td><td>'+esc(s.az)+'</td><td>'+esc(s.cidr)+'</td>'+
      '<td><span class="tag'+(s.public?" on":"")+'">'+(s.public?"public IPs":"private")+'</span> <span class="tag'+(s.igw_route?" on":"")+'">'+(s.igw_route?"IGW route":"no IGW route")+'</span></td>';
    pickRow(tr,plan.CLOUDLENS_EXISTING_SUBNET_ID===s.id,function(){set("CLOUDLENS_EXISTING_SUBNET_ID",s.id);sync();renderSubnets(rows);});
    tb.appendChild(tr);
  });
}
function showSubnets(){
  var vpc=plan.CLOUDLENS_EXISTING_VPC_ID;
  $("subnetBlock").hidden=!vpc;
  if(!vpc)return;
  status("subnetStatus","looking...");
  loadSubnets(vpc,function(err,rows){
    if(plan.CLOUDLENS_EXISTING_VPC_ID!==vpc)return;
    if(err){status("subnetStatus",err,true);$("subnetRows").innerHTML="";return;}
    status("subnetStatus",rows.length+" subnet"+(rows.length===1?"":"s")+" in "+vpc+". The management subnet: the appliances live here; the console reaches them on it.");
    renderSubnets(rows);
  });
}

/* ------------------------------------------------------------- screen 4 */
var wlTimer=null, wlSeq=0;
function scheduleWorkloads(){clearTimeout(wlTimer);wlTimer=setTimeout(loadWorkloads,DEBOUNCE);}
function loadWorkloads(){
  var k=plan.CLOUDLENS_DISCOVERY_TAG_KEY||"", v=plan.CLOUDLENS_DISCOVERY_TAG_VALUE||"";
  $("wlRows").innerHTML="";
  if(!regionOk())return status("wlStatus","Set a valid region first (screen 1).",true);
  if(!k.trim())return status("wlStatus","Name the tag key.",true);
  var seq=++wlSeq, r=region();
  status("wlStatus","looking...");
  var url="/api/discover/workloads?region="+enc(r)+"&tag="+enc(k+"="+v)+(sourceVpcs().length?"&vpcs="+enc(sourceVpcs().join(",")):"");
  api(url,function(err,d){
    if(seq!==wlSeq)return;               // a later query is the one that counts
    if(err)return status("wlStatus",err,true);
    status("wlStatus",P.workloadSummary(d)+(d.count?"":". The deploy continues, but nothing gets a sensor until instances carry that tag."),d.count===0);
    var tb=$("wlRows");
    (d.rows||[]).forEach(function(i){
      var tr=document.createElement("tr");
      tr.innerHTML='<td>'+esc(i.id)+'</td><td>'+esc(i.name)+'</td><td>'+esc(i.vpc)+'</td><td>'+esc(i.az)+'</td><td>'+esc(i.type)+'</td><td>'+esc(i.platform)+'</td><td>'+esc(i.private_ip)+'</td>';
      tb.appendChild(tr);
    });
  });
}
function showChips(){
  var box=$("vpcChips");
  loadVpcs(function(err,rows){
    box.innerHTML="";
    if(err){box.innerHTML='<span class="status err">'+esc(err)+'</span>';return;}
    var picked=sourceVpcs();
    rows.forEach(function(v){
      var b=document.createElement("button");b.type="button";b.className="chip";
      b.textContent=v.id+(v.name?" "+v.name:"")+" "+v.cidr;
      b.setAttribute("aria-pressed",picked.indexOf(v.id)>-1?"true":"false");
      b.addEventListener("click",function(){
        var now=sourceVpcs(),i=now.indexOf(v.id);
        if(i>-1)now.splice(i,1);else now.push(v.id);
        if(now.length)set("CLOUDLENS_SOURCE_VPCS",now.join(","));else unset("CLOUDLENS_SOURCE_VPCS");
        b.setAttribute("aria-pressed",i>-1?"false":"true");
        sync();scheduleWorkloads();
      });
      box.appendChild(b);
    });
    $("chipNote").textContent=picked.length?"":(existing()?"None picked: the script taps the VPC the appliances land in ("+(plan.CLOUDLENS_EXISTING_VPC_ID||"not chosen yet")+")."
      :"Pick at least one: the VPC this deploy builds holds nothing to tap.");
  });
}
function showCollector(){
  var vpc=plan.CLOUDLENS_EXISTING_VPC_ID;
  if(!vpc){status("colStatus","Pick the existing VPC on screen 1 first.",true);return;}
  status("colStatus","looking...");
  loadSubnets(vpc,function(err,rows){
    if(plan.CLOUDLENS_EXISTING_VPC_ID!==vpc)return;   // another VPC was picked while this was out
    if(err){status("colStatus",err,true);return;}
    status("colStatus",rows.length+" subnet"+(rows.length===1?"":"s")+" in "+vpc+". Three DISTINCT subnets in ONE availability zone (KVO UG, AWS Cloud Configs); leave all three empty and the mirror step is skipped with instructions.");
    [["colMgmt","CLOUDLENS_COLLECTOR_MGMT_SUBNET"],["colIngress","CLOUDLENS_COLLECTOR_INGRESS_SUBNET"],["colEgress","CLOUDLENS_COLLECTOR_EGRESS_SUBNET"]].forEach(function(pair){
      var sel=$(pair[0]);sel.innerHTML='<option value="">(none)</option>';
      rows.forEach(function(s){var o=document.createElement("option");o.value=s.id;o.textContent=subnetLabel(s);sel.appendChild(o);});
      sel.value=plan[pair[1]]||"";
      if(sel.value!==(plan[pair[1]]||""))unset(pair[1]);   // a subnet that is no longer there
    });
    sync();
  });
}
function collectorZone(){
  var rows=disco.subnets[region()+" "+plan.CLOUDLENS_EXISTING_VPC_ID]||[];
  var m=plan.CLOUDLENS_COLLECTOR_MGMT_SUBNET;
  for(var i=0;i<rows.length;i++)if(rows[i].id===m)return rows[i].az;
  return "";
}
function collectorAzs(){
  var rows=disco.subnets[region()+" "+plan.CLOUDLENS_EXISTING_VPC_ID]||[],out={};
  ["CLOUDLENS_COLLECTOR_MGMT_SUBNET","CLOUDLENS_COLLECTOR_INGRESS_SUBNET","CLOUDLENS_COLLECTOR_EGRESS_SUBNET"].forEach(function(k){
    rows.forEach(function(s){if(s.id===plan[k])out[s.az]=true;});
  });
  return Object.keys(out);
}

/* ------------------------------------------------------------- screen 5 */
function showEks(){
  status("eksStatus","looking...");
  loadEks(function(err,rows){
    var tb=$("eksRows");tb.innerHTML="";
    if(err){status("eksStatus",err,true);return;}
    status("eksStatus",rows.length?rows.length+" cluster"+(rows.length===1?"":"s")+" in "+region()+". The one you pick needs kubectl rights from this machine.":"No EKS cluster in "+region()+".",!rows.length);
    rows.forEach(function(c){
      var tr=document.createElement("tr");tr.innerHTML='<td><button type="button" class="pickb">'+esc(c.name)+'</button></td>';
      pickRow(tr,plan.CLOUDLENS_EKS_CLUSTER===c.name,function(){set("CLOUDLENS_EKS_CLUSTER",c.name);sync();showEks();});
      tb.appendChild(tr);
    });
  });
}

/* ------------------------------------------------------------- paint */
function paint(){
  // 1 where
  setVal("wStack",plan.CLOUDLENS_STACK_NAME);setVal("wRegion",plan.CLOUDLENS_REGION);setVal("wKey",plan.CLOUDLENS_KEY_NAME||"");
  $("infraNew").checked=!existing();$("infraExisting").checked=existing();
  $("existingBlock").hidden=!existing();
  $("s1Pick").textContent=existing()?("VPC "+(plan.CLOUDLENS_EXISTING_VPC_ID||"not chosen")+", management subnet "+(plan.CLOUDLENS_EXISTING_SUBNET_ID||"not chosen")):"";
  // 2 components
  setSwitch($("swKvo"),kvo());setSwitch($("swVpb"),vpb());
  $("vcT3").checked=plan.CLOUDLENS_VCONTROLLER_TYPE!=="m5.xlarge";$("vcM5").checked=plan.CLOUDLENS_VCONTROLLER_TYPE==="m5.xlarge";
  $("fixedKvo").hidden=!kvo();$("fixedVpb").hidden=!vpb();
  // 3 tapping
  document.querySelectorAll("#tapCards [data-tap]").forEach(function(b){b.setAttribute("aria-checked",b.dataset.tap===tapping()?"true":"false");});
  $("mgmtPlane").hidden=!(kvo()&&hasSensors());
  $("smStandalone").checked=plan.CLOUDLENS_SENSOR_MODE!=="kvo";$("smKvo").checked=plan.CLOUDLENS_SENSOR_MODE==="kvo";
  $("mirrorNeedsKvo").hidden=!(hasMirror()&&!kvo());
  // 4 workloads
  var none=tapping()==="none",w=workload();
  $("wlNone").hidden=!none;$("wlChoices").hidden=none;
  $("wlExisting").checked=w==="existing";$("wlTest").checked=w==="test";$("wlLater").checked=w==="later";
  $("wlExistingBlock").hidden=none||w!=="existing";$("wlTestBlock").hidden=none||w!=="test";$("wlLaterNote").hidden=none||w!=="later";
  if(w==="existing"){setVal("tagKey",plan.CLOUDLENS_DISCOVERY_TAG_KEY);setVal("tagValue",plan.CLOUDLENS_DISCOVERY_TAG_VALUE);}
  var counts=P.parseTestVms(plan.CLOUDLENS_TEST_VMS||"");
  OS.forEach(function(os){setVal("n-"+os,counts[os]);});
  $("collectorBlock").hidden=!collectorShown();
  // 5 kubernetes
  $("eksNone").checked=!eks();$("eksExisting").checked=eks()&&!eksSample();$("eksSample").checked=eksSample();
  $("eksClusterBlock").hidden=!(eks()&&!eksSample());$("eksModeBlock").hidden=!eks();
  $("modeDaemon").checked=plan.CLOUDLENS_EKS_MODE!=="sidecar";$("modeSidecar").checked=plan.CLOUDLENS_EKS_MODE==="sidecar";
  $("eksPicked").textContent=eks()&&!eksSample()?("Cluster: "+(plan.CLOUDLENS_EKS_CLUSTER||"not chosen")):"";
  // 6 plan: the secrets that apply
  hideSecrets($("secMirror"),!hasMirror());hideSecrets($("secKvo"),!kvo());
  paintCli();
}
function sync(){derive();paint();}

/* ------------------------------------------------------------- inputs */
$("wStack").addEventListener("input",function(){set("CLOUDLENS_STACK_NAME",this.value.trim());sync();});
$("wRegion").addEventListener("input",function(){
  var v=this.value.trim();
  if(v!==region()){
    set("CLOUDLENS_REGION",v);
    // another region: every discovered id belongs to the old one
    unset("CLOUDLENS_EXISTING_VPC_ID","CLOUDLENS_EXISTING_SUBNET_ID","CLOUDLENS_SOURCE_VPCS","CLOUDLENS_EKS_CLUSTER",
      "CLOUDLENS_COLLECTOR_ZONE","CLOUDLENS_COLLECTOR_MGMT_SUBNET","CLOUDLENS_COLLECTOR_INGRESS_SUBNET","CLOUDLENS_COLLECTOR_EGRESS_SUBNET");
    $("vpcRows").innerHTML="";$("subnetRows").innerHTML="";status("vpcStatus","");
    sync();
    if(existing()&&regionOk())showVpcs();
  }
});
$("wKey").addEventListener("input",function(){set("CLOUDLENS_KEY_NAME",this.value.trim());sync();});
$("infraNew").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_INFRA","new");sync();}});
$("infraExisting").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_INFRA","existing");sync();showVpcs();}});
$("vpcRefresh").addEventListener("click",function(){showVpcs(true);});

function bindSwitch(el,key,after){
  function flip(){set(key,el.getAttribute("aria-checked")==="true"?"false":"true");sync();if(after)after();}
  el.addEventListener("click",flip);
  el.addEventListener("keydown",function(e){if(e.key===" "||e.key==="Enter"){e.preventDefault();flip();}});
}
bindSwitch($("swKvo"),"CLOUDLENS_DEPLOY_KVO");
bindSwitch($("swVpb"),"CLOUDLENS_DEPLOY_VPB");
$("vcT3").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_VCONTROLLER_TYPE","t3.xlarge");sync();}});
$("vcM5").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_VCONTROLLER_TYPE","m5.xlarge");sync();}});

document.querySelectorAll("#tapCards [data-tap]").forEach(function(b){
  b.addEventListener("click",function(){set("CLOUDLENS_TAPPING",b.dataset.tap);sync();});
});
$("smStandalone").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_SENSOR_MODE","standalone");sync();}});
$("smKvo").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_SENSOR_MODE","kvo");sync();}});

$("wlExisting").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_WORKLOAD_CHOICE","existing");sync();enterScreen4();}});
$("wlTest").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_WORKLOAD_CHOICE","test");sync();}});
$("wlLater").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_WORKLOAD_CHOICE","later");sync();}});
$("tagKey").addEventListener("input",function(){set("CLOUDLENS_DISCOVERY_TAG_KEY",this.value.trim());save();scheduleWorkloads();});
$("tagValue").addEventListener("input",function(){set("CLOUDLENS_DISCOVERY_TAG_VALUE",this.value.trim());save();scheduleWorkloads();});
OS.forEach(function(os){
  function bump(d){
    var counts=P.parseTestVms(plan.CLOUDLENS_TEST_VMS||"");
    counts[os]=Math.max(0,Math.min(10,(parseInt($("n-"+os).value,10)||0)+d));
    set("CLOUDLENS_TEST_VMS",P.testVms(counts));sync();
  }
  $("dec-"+os).addEventListener("click",function(){bump(-1);});
  $("inc-"+os).addEventListener("click",function(){bump(1);});
  $("n-"+os).addEventListener("change",function(){bump(0);});
});
[["colMgmt","CLOUDLENS_COLLECTOR_MGMT_SUBNET"],["colIngress","CLOUDLENS_COLLECTOR_INGRESS_SUBNET"],["colEgress","CLOUDLENS_COLLECTOR_EGRESS_SUBNET"]].forEach(function(pair){
  $(pair[0]).addEventListener("change",function(){
    set(pair[1],this.value||null);
    // the zone comes from the management subnet, as the interview derives it
    set("CLOUDLENS_COLLECTOR_ZONE",collectorZone()||null);
    sync();
  });
});

$("eksNone").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_DEPLOY_EKS","false");sync();}});
$("eksExisting").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_DEPLOY_EKS","true");set("CLOUDLENS_EKS_SAMPLE","false");sync();showEks();}});
$("eksSample").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_DEPLOY_EKS","true");set("CLOUDLENS_EKS_SAMPLE","true");sync();}});
$("eksRefresh").addEventListener("click",function(){disco.eks={};showEks();});
$("modeDaemon").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_EKS_MODE","daemonset");sync();}});
$("modeSidecar").addEventListener("change",function(){if(this.checked){set("CLOUDLENS_EKS_MODE","sidecar");sync();}});

/* The refusal under Next belongs to the answer that was refused: changing
   any answer clears it, not only moving to another screen. Captured, so
   Next's own click clears the old text before its handler writes the new. */
["input","change","click"].forEach(function(type){
  $("wizard").addEventListener(type,function(){if($("wErr").textContent)status("wErr","");},true);
});

/* ------------------------------------------------------------- screens */
var screen=1;
function validate(n){
  if(n===1){
    var s=plan.CLOUDLENS_STACK_NAME||"";
    if(!s)return "Name the stack.";
    if(!STACK_RE.test(s)||s.length>STACK_MAX)return "A stack name starts with a letter and uses only letters, digits and hyphens (at most "+STACK_MAX+" characters): the script's own rule.";
    if(!regionOk())return "The region must be an AWS region like us-east-1.";
    if(existing()&&!plan.CLOUDLENS_EXISTING_VPC_ID)return "Pick the VPC the appliances land in.";
    if(existing()&&!plan.CLOUDLENS_EXISTING_SUBNET_ID)return "Pick the management subnet (an existing VPC needs one; the script falls back to a new VPC without it).";
  }
  if(n===4&&tapping()!=="none"){
    var w=workload();
    if(w==="existing"){
      if(!(plan.CLOUDLENS_DISCOVERY_TAG_KEY||"").trim())return "Name the tag key that marks the workloads.";
      if(!existing()&&!sourceVpcs().length)return "Pick at least one VPC the workloads live in: the VPC this deploy builds holds nothing to tap.";
    }
    if(w==="test"&&!(plan.CLOUDLENS_TEST_VMS||""))return "Ask for at least one test VM, or choose \"decide later\".";
    if(collectorShown()){
      var c=[plan.CLOUDLENS_COLLECTOR_MGMT_SUBNET,plan.CLOUDLENS_COLLECTOR_INGRESS_SUBNET,plan.CLOUDLENS_COLLECTOR_EGRESS_SUBNET].filter(function(x){return x;});
      if(c.length&&c.length<3)return "The collector needs all three subnets (management, ingress, egress), or none to skip the mirror step.";
      if(c.length===3&&(c[0]===c[1]||c[1]===c[2]||c[0]===c[2]))return "The three collector subnets must be DISTINCT subnets (KVO UG).";
      if(c.length===3&&collectorAzs().length>1)return "The three collector subnets must be in ONE availability zone (they span "+collectorAzs().join(", ")+").";
    }
  }
  if(n===5&&eks()&&!eksSample()&&!plan.CLOUDLENS_EKS_CLUSTER)return "Pick the EKS cluster to tap.";
  return "";
}
function enterScreen4(){
  if(tapping()==="none")return;
  if(workload()==="existing"){showChips();scheduleWorkloads();}
  if(collectorShown())showCollector();
}
function showScreen(n){
  n=Math.max(1,Math.min(6,n));
  screen=n;
  document.querySelectorAll("#wizard [data-screen]").forEach(function(s){s.hidden=parseInt(s.dataset.screen,10)!==n;});
  document.querySelectorAll("#wSteps [data-step]").forEach(function(b){
    var k=parseInt(b.dataset.step,10);
    if(k===n)b.setAttribute("aria-current","step");else b.removeAttribute("aria-current");
    b.classList.toggle("done",k<n);
  });
  status("wErr","");
  $("backBtn").disabled=n===1;
  $("nextBtn").hidden=n===6;
  sync();
  if(n===1&&existing()&&regionOk())showVpcs();
  if(n===4)enterScreen4();
  if(n===5&&eks()&&!eksSample())showEks();
  if(n===6)postPlan();
  try{localStorage.setItem("cl-screen",String(n));}catch(e){}
}
$("nextBtn").addEventListener("click",function(){
  sync();
  var err=validate(screen);
  if(err){status("wErr",err,true);return;}
  showScreen(screen+1);
});
$("backBtn").addEventListener("click",function(){showScreen(screen-1);});
document.querySelectorAll("#wSteps [data-step]").forEach(function(b){
  b.addEventListener("click",function(){
    var k=parseInt(b.dataset.step,10);
    // forward only through Next, so every screen on the way was validated
    for(var i=screen;i<k;i++){var err=validate(i);if(err){showScreen(i);status("wErr",err,true);return;}}
    showScreen(k);
  });
});

/* ------------------------------------------------------------- screen 6 */
var planResp=null, planOk=false, launching=false, planSeq=0;
/* api.py's word for a key that is not in deploy/profile-keys.txt. No screen
   owns such a key, so no screen can clear it: the plan drops it and asks
   once more, rather than showing an error nobody can act on. */
var NOT_A_KEY=/^([A-Za-z_][A-Za-z0-9_]*): not a profile key/;
function postPlan(retried){
  derive();planOk=false;planResp=null;
  var seq=++planSeq;
  status("planStatus","checking the plan...");
  $("planErrors").innerHTML="";$("resolvedWrap").innerHTML="";$("profileText").textContent="";
  $("profileName").textContent="";gate();
  post("/api/plan",{plan:plan},function(err,x){
    if(seq!==planSeq)return;              // a later plan is the one that counts
    if(err)return status("planStatus",err,true);
    var d=x.d;
    if(!x.ok||d.errors||d.error){
      var list=d.errors||[d.error||("HTTP "+x.status)];
      // one pass, never a loop: the retry drops the stray keys and asks
      // again, and a key derive() writes straight back is one it cannot
      // clear. That needs a CLOUDLENS_* literal in this file that is not in
      // deploy/profile-keys.txt, which test_web_static forbids, so in a
      // consistent build it is unreachable. The window it is written for is
      // a stale cache: a page held from before a key was renamed, talking to
      // the new engine. There the second refusal is shown, not retried.
      if(!retried){
        var stray=[];
        list.forEach(function(e){var m=NOT_A_KEY.exec(String(e));if(m&&has(m[1]))stray.push(m[1]);});
        // repainted, so the earlier screens stop showing an answer the
        // plan no longer carries (a key field with a dropped key in it)
        if(stray.length){unset.apply(null,stray);sync();return postPlan(true);}
      }
      $("planErrors").innerHTML=P.renderErrors(list);
      status("planStatus","The plan has "+list.length+" problem"+(list.length===1?"":"s")+": fix "+(list.length===1?"it":"them")+" on the earlier screens.",true);
      gate();return;
    }
    planResp=d;planOk=true;
    $("resolvedWrap").innerHTML=P.renderResolved(d.resolved);
    $("profileText").textContent=d.profile_text;
    $("profileName").textContent=d.profile_file;
    status("planStatus","Stack "+d.stack+" in "+d.region+": the profile below is what the script replays, question for question.");
    paintCli();gate();
  });
}
function secretsNow(){
  var out={};
  document.querySelectorAll("#secrets [data-secret]").forEach(function(i){
    if(i.closest("[hidden]"))return;           // a field of a part that does not apply
    if(i.value)out[i.name]=i.value;
  });
  return out;
}
/* Activation codes never appear whole on the page. Each one is typed or
   pasted into a password input (masked by every browser: a textarea under
   -webkit-text-security showed them in clear on Firefox, which ignores
   that property) and moves into this array, closed over here and nowhere
   else: not the plan, not localStorage. The page shows a chip of the last
   four characters. Launch sends the array as kvo_codes. */
var codes=[];
// api.CODE_QTY, quantity digits and all. test_web_static extracts both
// patterns and holds the quantity parts equal: a five or six digit quantity
// the engine takes is not one this page turns away.
var CODE_QTY_RE=/^[A-Za-z0-9][A-Za-z0-9-]{3,63}(?:,[0-9]{1,6})?$/;
var CODES_MAX=50;                                                    // api.MAX_LIST
function codesNow(){return kvo()?codes.slice():[];}
function paintCodes(){
  var list=$("codeList");list.innerHTML="";
  codes.forEach(function(c,i){
    var chip=document.createElement("span");chip.className="code";
    chip.appendChild(document.createTextNode(codeTail(c)));
    var rm=document.createElement("button");rm.type="button";rm.textContent="\u00d7";
    rm.setAttribute("aria-label","Remove the code ending "+c.split(",")[0].slice(-4));
    rm.addEventListener("click",function(){codes.splice(i,1);paintCodes();});
    chip.appendChild(rm);list.appendChild(chip);
  });
  $("codeCount").textContent=codes.length?codes.length+" code"+(codes.length===1?"":"s"):"";
  paintCli();
}
function addCodes(text){
  var bad=0,dup=0,over=0;
  P.parseCodes(text).forEach(function(c){
    if(!CODE_QTY_RE.test(c)){bad++;return;}          // the API's rule, said here instead of at Launch
    if(codes.indexOf(c)>=0){dup++;return;}
    if(codes.length>=CODES_MAX){over++;return;}
    codes.push(c);
  });
  paintCodes();
  var notes=[];
  if(bad)notes.push(bad+" entr"+(bad===1?"y is":"ies are")+" not an activation code (CODE or CODE,QTY, a quantity of 1 to 6 digits)");
  if(dup)notes.push(dup+" already added");
  if(over)notes.push(over+" over the limit of "+CODES_MAX);
  if(notes.length)status("codeCount",$("codeCount").textContent+(codes.length?"; ":"")+notes.join("; ")+".",!!(bad||over));
  else $("codeCount").classList.remove("err");
}
function takeCodeEntry(){addCodes($("codeEntry").value);$("codeEntry").value="";$("codeEntry").focus();}
$("codeAdd").addEventListener("click",takeCodeEntry);
$("codeEntry").addEventListener("keydown",function(e){if(e.key==="Enter"){e.preventDefault();takeCodeEntry();}});
$("codeEntry").addEventListener("paste",function(e){
  // the clipboard text as it is, read before the password input strips
  // its newlines: a pasted list adds every code in it, plus whatever was
  // already typed
  var text=e.clipboardData&&e.clipboardData.getData("text");
  if(!text)return;
  e.preventDefault();
  addCodes(($("codeEntry").value?$("codeEntry").value+"\n":"")+text);
  $("codeEntry").value="";
});
function paintCli(){
  $("cliLine").textContent=P.cliLine(planResp,Object.keys(secretsNow()).sort(),codesNow().length);
}
document.querySelectorAll("#secrets [data-secret]").forEach(function(i){i.addEventListener("input",paintCli);});

/* Launch: disabled while the plan has errors, a run is being started, or
   the doctor's last verdict for this region carries a FAIL; the note
   says which. */
function gate(){
  var why="";
  if(launching)why="starting the engine...";
  else if(!planOk)why="The plan has to pass /api/plan first.";
  else if(doctor.region===region()&&doctor.fails)why="Pre-flight found "+doctor.fails+" blocker"+(doctor.fails===1?"":"s")+" in "+doctor.region+": fix "+(doctor.fails===1?"it":"them")+" on the Pre-flight page and re-check.";
  else if(doctor.region!==region())why="Pre-flight has not been run for "+region()+" yet (recommended, not required).";
  $("launchBtn").disabled=!!why&&(launching||!planOk||(doctor.region===region()&&doctor.fails>0));
  $("launchNote").textContent=why;
}
$("launchBtn").addEventListener("click",function(){
  if($("launchBtn").disabled)return;
  derive();
  var body={plan:plan,secrets:secretsNow(),kvo_codes:codesNow()};
  launching=true;gate();status("launchStatus","");
  post("/api/run",body,function(err,x){
    launching=false;
    if(err){gate();return status("launchStatus",err,true);}
    var d=x.d;
    if(!x.ok||d.error||d.errors){
      gate();
      if(d.errors)$("planErrors").innerHTML=P.renderErrors(d.errors);
      return status("launchStatus",d.error||(x.status===409?"That stack already has a run in progress.":"The engine refused the plan; see above."),true);
    }
    // the secrets have gone to the engine's environment; nothing keeps them here
    document.querySelectorAll("#secrets [data-secret]").forEach(function(i){i.value="";});
    codes.length=0;$("codeEntry").value="";paintCodes();
    status("launchStatus","Started job "+d.job_id+" on "+d.profile_file+".");
    // the Watch screen follows it from here, and remembers the id, so a
    // reload (or a tab opened later) can attach to the same run
    W.attach(d.job_id);
    showPage("watch");
    gate();
  });
});

/* ------------------------------------------------------------- pre-flight */
var doctor={region:null,checks:null,fails:0,warns:0};
function renderChecks(d){
  var tb=$("pfRows");tb.innerHTML="";
  d.checks.forEach(function(c){
    var tr=document.createElement("tr");
    tr.innerHTML='<td><span class="st '+esc(c.status)+'">'+esc(String(c.status).toUpperCase())+'</span></td><td>'+esc(c.item)+'</td><td>'+(c.fix?esc(c.fix):'<span class="dim">'+(c.status==="pass"?"":"no fix given")+'</span>')+'</td>';
    tb.appendChild(tr);
  });
}
$("pfBtn").addEventListener("click",function(){
  var r=$("pfRegion").value.trim();
  if(!REGION_RE.test(r))return status("pfStatus","The region must be an AWS region like us-east-1.",true);
  $("pfBtn").disabled=true;$("pfRows").innerHTML="";
  status("pfStatus","checking "+r+"... (deploy-stack.sh --doctor probes GitHub, the template bucket and AWS: up to two minutes)");
  api("/api/doctor?region="+enc(r),function(err,d){
    $("pfBtn").disabled=false;$("pfBtn").innerHTML='<span class="tri"></span> Re-check';
    if(err)return status("pfStatus",err,true);
    doctor.region=r;doctor.checks=d.checks;
    doctor.fails=d.checks.filter(function(c){return c.status==="fail";}).length;
    doctor.warns=d.checks.filter(function(c){return c.status==="warn";}).length;
    renderChecks(d);
    status("pfStatus",d.ok?("All "+d.checks.length+" checks passed in "+r+(doctor.warns?" ("+doctor.warns+" warning"+(doctor.warns===1?"":"s")+")":"")+". Launch is open.")
      :(doctor.fails+" blocker"+(doctor.fails===1?"":"s")+" in "+r+": each row says the fix. Launch stays off until a re-check passes."),!d.ok);
    gate();
  });
});

/* The page switch, for the screens that live in their own files: a run
   started on Operate or Teardown belongs on Watch, and only this file
   knows how a page is shown. */
if(typeof window!=="undefined")window.clNav={show:showPage,pages:PAGES};

/* ------------------------------------------------------------- start */
derive();paint();
var page="deploy",first=1;
try{page=localStorage.getItem("cl-page")||"deploy";first=parseInt(localStorage.getItem("cl-screen")||"1",10)||1;}catch(e){}
// every step is reachable: a saved plan resumes on any screen, and a jump
// forward validates every screen on the way (the stepper's own handler)
showScreen(first);
showPage(page);
})();
