(function(){
"use strict";
/* licences.js: the Licensing screen, over the /api/licences/* routes.

   The domain rule this screen exists for: licences must be RELEASED
   before a KVO is deleted. A KVO torn down with licences still installed
   strands the counts, and they do not come back; the release is
   POST /api/v2/licensing/operations/deactivate, polled to its end, which
   is what /api/licences/release does. The Teardown screen asks this file
   whether a release happened in this session and warns when it did not.

   What this file never does:
     - store the KVO password. It is read out of its field at the moment a
       request is made, posted in the body (never a URL, never a query
       string), and kept nowhere: no localStorage, no module variable, no
       second copy on a model. A reload asks for it again, which is the
       correct amount of friction for a password that activates money.
     - show an activation code whole. The chips and the tables show the
       last four characters, as the wizard's do: a code on a shared screen
       is a code somebody else can spend.

   The pure half (codeRows, licenceRows, releaseRow, noteRelease,
   released) is under tests/test_ops_model.py in node. */

var $=function(id){return document.getElementById(id);};
var CODES_MAX=50;                             // api.MAX_LIST
var CODE_QTY_RE=/^[A-Za-z0-9][A-Za-z0-9-]{3,63}(?:,[0-9]{1,6})?$/;   // api.CODE_QTY

/* ------------------------------------------------------------ the model */

function txt(v){return v===undefined||v===null?"":String(v);}
function num(v){var n=parseInt(v,10);return isNaN(n)?0:n;}

/* A code, as this page is willing to display it. */
function codeTail(c){
  var parts=String(c||"").split(",");
  return "****-"+parts[0].slice(-4)+(parts[1]?","+parts[1]:"");
}

/* The answer to /api/licences/check: one row per code, with what the code
   holds and the quantity the operator would activate. The default quantity
   is what the entitlement says is available, which is what kvo_license.py
   activates when no quantity is given. */
function codeRows(resp){
  return ((resp&&resp.codes)||[]).map(function(c){
    var ents=((c&&c.entitlements)||[]).map(function(e){
      return {product:txt(e.product),available:num(e.available),total:num(e.total)};
    });
    var avail=ents.length?ents[0].available:0;
    return {code:txt(c.code),tail:codeTail(c.code),valid:c.valid===true,state:txt(c.state),
            entitlements:ents,available:avail,quantity:avail,
            summary:ents.length
              ? ents.map(function(e){return e.product+" "+e.available+" of "+e.total;}).join(", ")
              : "the KVO recognised nothing under this code"};
  });
}

/* The answer to list / activate / release: what the KVO now holds. The
   shape is the KVO's own (activationCode, product, quantity); a row that
   does not carry a code is still listed, because it is still installed. */
function licenceRows(resp){
  return ((resp&&resp.licences)||[]).map(function(l){
    var code=txt(l&&(l.activationCode||l.code));
    return {code:code,tail:code?codeTail(code):"(no code on the row)",
            product:txt(l&&(l.product||l.productName)),
            quantity:num(l&&l.quantity)};
  });
}

/* One installed licence as the release route takes it. */
function releaseRow(row){
  return {activationCode:txt(row&&row.code),quantity:num(row&&row.quantity)};
}

/* What this session has released, and from where. In memory only: it is a
   fact about this page's life, not something to remember for the next one,
   and the Teardown screen's warning is deliberately about THIS session's
   evidence rather than a claim it cannot check. */
var record={kvo:"",codes:[],when:0};

function noteRelease(kvo,resp){
  var ok=((resp&&resp.results)||[]).filter(function(r){return r&&r.ok===true;});
  if(!ok.length)return released();
  record.kvo=txt(kvo);
  record.when=Date.now();
  ok.forEach(function(r){
    var tail=codeTail(r.code);
    if(record.codes.indexOf(tail)<0)record.codes.push(tail);
  });
  return released();
}

function released(){
  return record.kvo?{kvo:record.kvo,codes:record.codes.slice(),when:record.when}:null;
}

/* --------------------------------------------------------------- render */

function esc(s){
  var P=window.clPlan;
  return P?P.esc(s):String(s==null?"":s);
}

var codes=[],rows=[],installed=[],busy=false;

function status(id,text,bad){var el=$(id);el.textContent=text||"";el.classList.toggle("err",!!bad);}

function paintCodes(){
  var list=$("licList");list.innerHTML="";
  codes.forEach(function(c,i){
    var chip=document.createElement("span");chip.className="code";
    chip.appendChild(document.createTextNode(codeTail(c)));
    var rm=document.createElement("button");rm.type="button";rm.textContent="×";
    rm.setAttribute("aria-label","Remove the code ending "+c.split(",")[0].slice(-4));
    rm.addEventListener("click",function(){codes.splice(i,1);paintCodes();});
    chip.appendChild(rm);list.appendChild(chip);
  });
  $("licCount").textContent=codes.length?codes.length+" code"+(codes.length===1?"":"s"):"";
  $("licCheck").disabled=busy||!codes.length;
  $("licActivate").disabled=busy||!rows.length;
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
  if(notes.length)status("licCount",$("licCount").textContent+(codes.length?"; ":"")+notes.join("; ")+".",!!(bad||over));
}

function renderCodeRows(){
  var tb=$("licCodes");
  if(!rows.length){
    tb.innerHTML='<tr><td colspan="4"><span class="dim">Add the codes and press Check: the KVO says what each one holds before anything is spent.</span></td></tr>';
    return;
  }
  tb.innerHTML=rows.map(function(r,i){
    return "<tr><td><code>"+esc(r.tail)+"</code></td>"+
      '<td><span class="st '+(r.valid?"pass":"fail")+'">'+esc(r.valid?"VALID":"NO")+"</span></td>"+
      "<td>"+esc(r.summary)+(r.state?' <span class="dim">'+esc(r.state)+"</span>":"")+"</td>"+
      "<td>"+(r.valid&&r.available
        ? '<label class="vh" for="licQty'+i+'">Quantity for the code ending '+esc(r.tail.slice(-4))+"</label>"+
          '<input class="qty" id="licQty'+i+'" type="number" min="1" max="999999" value="'+esc(String(r.quantity))+'">'
        : '<span class="dim">nothing to activate</span>')+"</td></tr>";
  }).join("");
  rows.forEach(function(r,i){
    var el=$("licQty"+i);
    if(el)el.addEventListener("input",function(){r.quantity=num(el.value);});
  });
  $("licActivate").disabled=busy||!rows.length;
}

function renderInstalled(){
  var tb=$("licRows");
  if(!installed.length){
    tb.innerHTML='<tr><td colspan="4"><span class="dim">Nothing listed yet. Press "List what is installed": that is also the check that the login works.</span></td></tr>';
    return;
  }
  tb.innerHTML=installed.map(function(r,i){
    return "<tr><td><code>"+esc(r.tail)+"</code></td><td>"+esc(r.product)+"</td><td>"+esc(String(r.quantity))+"</td>"+
      '<td><button type="button" class="chip" id="licRel'+i+'"'+(r.code&&r.quantity?"":" disabled")+
      ">Release</button></td></tr>";
  }).join("");
  installed.forEach(function(r,i){
    var b=$("licRel"+i);
    if(b&&!b.disabled)b.addEventListener("click",function(){release(r);});
  });
}

function renderRecord(){
  var rec=released();
  $("licReleased").textContent=rec
    ? ("Released in this session from "+rec.kvo+": "+rec.codes.join(", ")+
       ". The Teardown screen reads this, and will run with --accept-licence-loss.")
    : "Nothing released in this session yet. Do it before the KVO goes: a KVO deleted with licences on it strands the counts.";
}

/* ------------------------------------------------------------ the calls */

/* The body every call shares. The password is read HERE, at the moment of
   the call, and the object is thrown away with the request. */
function body(action,extra){
  var b={action:action,kvo:$("licKvo").value.trim(),user:$("licUser").value.trim()||"admin",
         password:$("licPass").value,verify:$("licVerify").checked===true,
         accept_eula:$("licEula").checked===true};
  if(extra)for(var k in extra)if(Object.prototype.hasOwnProperty.call(extra,k))b[k]=extra[k];
  return b;
}

function call(action,extra,cb){
  if(busy)return;
  if(!$("licKvo").value.trim())return status("licStatus","Name the KVO first.",true);
  busy=true;
  ["licLoad","licCheck","licActivate"].forEach(function(id){$(id).disabled=true;});
  status("licStatus",action+"...");
  fetch("/api/licences/"+encodeURIComponent(action),
        {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body(action,extra))})
   .then(function(r){return r.text().then(function(t){
     var d=null;try{d=JSON.parse(t);}catch(e){}
     return {ok:r.ok,status:r.status,d:d};});})
   .catch(function(){return {err:"Could not reach the console server."};})
   .then(function(x){
     busy=false;
     ["licLoad","licCheck"].forEach(function(id){$(id).disabled=false;});
     paintCodes();
     if(x.err||!x.ok||!x.d||x.d.error){
       var d=x.d||{};
       status("licStatus",x.err||d.error||("The KVO call failed (HTTP "+x.status+")."),true);
       return;
     }
     cb(x.d);
   });
}

function check(){
  call("check",{codes:codes.slice()},function(d){
    rows=codeRows(d);
    renderCodeRows();
    var good=rows.filter(function(r){return r.valid;}).length;
    status("licStatus",good+" of "+rows.length+" code"+(rows.length===1?"":"s")+
      " recognised. Set the quantity per code, then Activate: activating spends entitlement that does not come back.");
  });
}

function activate(){
  var picked=rows.filter(function(r){return r.valid&&r.quantity>0;})
                 .map(function(r){return r.code+","+r.quantity;});
  if(!picked.length)return status("licStatus","No code has a quantity to activate.",true);
  call("activate",{codes:picked},function(d){
    installed=licenceRows(d);
    renderInstalled();
    var ok=(d.activated||0);
    status("licStatus",ok+" of "+picked.length+" activated. "+
      (d.results||[]).filter(function(r){return !r.ok;}).map(function(r){
        return codeTail(r.code)+": "+(r.state||"refused");
      }).join("; "));
  });
}

function load(){
  call("list",null,function(d){
    installed=licenceRows(d);
    renderInstalled();
    status("licStatus",installed.length
      ? (installed.length+" licence"+(installed.length===1?"":"s")+" installed on this KVO.")
      : "This KVO holds no licences.");
  });
}

function release(row){
  var what=releaseRow(row);
  if(!window.confirm("Release "+what.quantity+" of "+(row.product||"this licence")+
      " ("+row.tail+") back to the pool? This is the step that must happen BEFORE the KVO is deleted."))return;
  call("release",{rows:[what]},function(d){
    installed=licenceRows(d);
    renderInstalled();
    noteRelease($("licKvo").value.trim(),d);
    renderRecord();
    status("licStatus",d.released
      ? ("Released. "+(d.clear?"This KVO now holds no licences.":"Some licences are still installed."))
      : "The KVO did not confirm the release; the counts are still with it.",!d.released);
  });
}

function init(){
  $("licAdd").addEventListener("click",function(){
    addCodes($("licEntry").value);$("licEntry").value="";
  });
  $("licEntry").addEventListener("keydown",function(e){
    if(e.key==="Enter"){e.preventDefault();addCodes(this.value);this.value="";}
  });
  $("licEntry").addEventListener("paste",function(e){
    // the clipboard text as it is, before a password input strips newlines
    var text=e.clipboardData&&e.clipboardData.getData("text");
    if(!text)return;
    e.preventDefault();
    addCodes((this.value?this.value+"\n":"")+text);
    this.value="";
  });
  $("licLoad").addEventListener("click",load);
  $("licCheck").addEventListener("click",check);
  $("licActivate").addEventListener("click",activate);
  paintCodes();renderCodeRows();renderInstalled();renderRecord();
}

if(typeof window!=="undefined")window.clLicences={
  codeRows:codeRows,licenceRows:licenceRows,releaseRow:releaseRow,codeTail:codeTail,
  noteRelease:noteRelease,released:released
};

if(typeof document!=="undefined"&&document.getElementById("licRows"))init();
})();
