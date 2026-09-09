(function(){
"use strict";
/* plan.js: the plan page's rendering, as pure functions of the /api/plan
   answer. Nothing here touches the DOM or the network; wizard.js calls
   these and puts the HTML where it goes. Exposed as window.clPlan. */

function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}

/* The resolved table: one row per profile key in the file's order, as the
   CLI prints its "Resolved configuration". A row with a value shows it; one
   without shows the note the API gave (the script asks, or applies its own
   default), dimmed. */
function renderResolved(rows){
  var h='<table class="ref resolved"><thead><tr><th>Key</th><th>Value</th></tr></thead><tbody>';
  (rows||[]).forEach(function(r){
    var has=r.value!==null&&r.value!==undefined;
    h+='<tr class="'+(has?"set":"unset")+'"><td><code>'+esc(r.key)+'</code></td><td>'+
      (has?(r.value===""?'<span class="dim">(empty)</span>':esc(r.value)):'<span class="dim">'+esc(r.note||"not in the plan")+'</span>')+
      '</td></tr>';
  });
  return h+'</tbody></table>';
}

function renderErrors(errors){
  var list=Array.isArray(errors)?errors:[errors];
  return '<ul class="errs">'+list.map(function(e){return '<li>'+esc(e)+'</li>';}).join("")+'</ul>';
}

/* The CLI line that does what Launch does. Secrets are named, never shown:
   the env names api.SECRET_ENV reads, and one --kvo-codes per code. */
function cliLine(resp,secretNames,codeCount){
  var parts=[];
  (secretNames||[]).forEach(function(n){parts.push(n+"=<secret>");});
  parts.push("bash deploy/deploy-stack.sh --profile "+(resp&&resp.profile_file?resp.profile_file:"deploy-profile-<stack>.env"));
  for(var i=0;i<(codeCount||0);i++)parts.push("--kvo-codes <code"+(codeCount>1?(i+1):"")+">");
  return parts.join(" ");
}

/* CLOUDLENS_TEST_VMS as write_profile() writes it: os:N per OS with a count,
   comma separated, an OS with 0 left off. */
function testVms(counts){
  var out=[];
  ["ubuntu","rhel","windows"].forEach(function(os){
    var n=parseInt(counts&&counts[os],10);
    if(n>0)out.push(os+":"+Math.min(n,10));
  });
  return out.join(",");
}

/* {ubuntu:N,rhel:N,windows:N} from a CLOUDLENS_TEST_VMS value. */
function parseTestVms(text){
  var counts={ubuntu:0,rhel:0,windows:0};
  String(text||"").split(",").forEach(function(item){
    var p=item.trim().toLowerCase().split(":");
    if(p[0] in counts)counts[p[0]]=p.length>1?(parseInt(p[1],10)||0):1;
  });
  return counts;
}

/* Activation codes as typed or pasted: whitespace, newlines and commas
   separate them, except that a purely numeric piece after a comma is the
   quantity of the code before it, so CODE,QTY stays one token, the shape
   the script's --kvo-codes takes.

   A quantity is 1 to 4 digits (what the field asks for, and what
   wizard.js's CODE_QTY_RE accepts). A longer run of digits is folded into
   its code all the same: it is a mistyped quantity, and joined to the code
   it is reported as the one bad entry it is, where on its own it became a
   chip of its own that named a code nobody typed. */
function parseCodes(text){
  var out=[];
  String(text||"").split(/\s+/).forEach(function(word){
    var last=-1;   // index in out of the last code this word produced
    word.split(",").forEach(function(p){
      if(!p)return;
      if(/^[0-9]+$/.test(p)&&last>=0&&out[last].indexOf(",")<0){out[last]+=","+p;return;}
      out.push(p);last=out.length-1;
    });
  });
  return out;
}

/* The workload count line for the discovery status. */
function workloadSummary(d){
  if(!d||typeof d.count!=="number")return "";
  var s=d.count+" running instance"+(d.count===1?"":"s")+" match "+(d.filter&&d.filter.tag?d.filter.tag:"the tag");
  if(d.truncated)s+=" (first "+d.rows.length+" shown)";
  return s;
}

window.clPlan={esc:esc,renderResolved:renderResolved,renderErrors:renderErrors,cliLine:cliLine,
  testVms:testVms,parseTestVms:parseTestVms,parseCodes:parseCodes,workloadSummary:workloadSummary};
})();
