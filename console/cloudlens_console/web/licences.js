(function(){
"use strict";
/* licences.js: the Licensing screen, over the /api/licences/* routes.

   The domain rule this screen exists for: licences must be RELEASED
   before a KVO is deleted. A KVO torn down with licences still installed
   strands the counts, and they do not come back; the release is
   POST /api/v2/licensing/operations/deactivate, polled to its end, which
   is what /api/licences/release does. The Teardown screen asks this file
   whether a release happened in this session and warns when it did not.

   A release is recorded PER KVO, and only as far as the KVO's own answer
   goes. Two things were one before and were both wrong:

     - the record was a single bucket. `kvo` was overwritten per release
       while `codes` accumulated, so releasing on KVO A and then on KVO B
       left one record saying both codes came from B. The address the
       Teardown screen matches was right; the sentence an operator reads
       and acts on was a lie. The record is a map now, host to codes.
     - the record said only THAT something was released, never that the
       KVO was then clear. /api/licences/release answers `clear` ("the
       KVO holds no licence now") and this screen already printed "Some
       licences are still installed" when it was false, but what the
       Teardown screen read was a green banner and --accept-licence-loss
       on the argv of a run that deletes the KVO with those licences
       still on it. `clear` is carried into the record and the teardown
       gate turns on it.
     - the record was written only when something was RELEASED, so it
       aged into a lie. Release everything on a KVO, activate a new code
       on the same KVO a minute later, and the banner was still green off
       the older fact. Anything that puts licences back on an appliance
       (an activation, or a list that names one) marks that appliance's
       record stale, and a stale record no longer arms the teardown.

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
   noteHolds, released, latestRelease) is under tests/test_ops_model.py in
   node. */

var U=window.clUi;                            // ui.js, loaded before this file
var $=U.$,txt=U.txt,esc=U.esc,status=U.status,codeTail=U.codeTail,hostOf=U.hostOf;
var CODES_MAX=50;                             // api.MAX_LIST
var OPS_MAX=10;                               // api.MAX_OPS: codes in one polled call
var CHECK_MAX=25;                             // api.MAX_CHECK: codes in one polled check
var CODE_QTY_RE=/^[A-Za-z0-9][A-Za-z0-9-]{3,63}(?:,[0-9]{1,6})?$/;   // api.CODE_QTY

/* ------------------------------------------------------------ the model */

function num(v){var n=parseInt(v,10);return isNaN(n)?0:n;}

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
    // a lookup whose poll ran out of the request's budget has no answer:
    // reporting it as a code the KVO did not recognise is how a good
    // activation code gets thrown away
    var running=!!(c&&c.running===true&&!ents.length);
    return {code:txt(c.code),tail:codeTail(c.code),valid:c.valid===true,running:running,
            state:txt(c.state),entitlements:ents,available:avail,quantity:avail,
            summary:ents.length
              ? ents.map(function(e){return e.product+" "+e.available+" of "+e.total;}).join(", ")
              : running
                ? "the KVO was still looking this code up when the console's polling budget ran out: "+
                  "outcome unknown, check it again on its own"
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

/* What this session has released, per KVO. In memory only: it is a fact
   about this page's life, not something to remember for the next one, and
   the Teardown screen's warning is deliberately about THIS session's
   evidence rather than a claim it cannot check.

   host -> {kvo (as it was typed), codes (tails), clear, unknown, stale,
   when}.

   `clear` is the KVO's own answer to "do you still hold licences", read
   from the release response and never inferred: a release that succeeded
   on the rows it was given still leaves the KVO holding whatever nobody
   asked about, and that is the case this record exists to make visible.
   It is true only when the KVO ANSWERED that it holds none: /api/licences
   answers clear:null when the list could not be read at all, and a null
   is not a yes.

   `stale` is the other half of the same rule, in time rather than in
   rows. The record was written only by noteRelease, so a release followed
   by an activation on the same KVO in the same session still showed the
   green banner: the release did happen, and it stopped being TRUE of the
   appliance the moment something was put back on it. Anything that says
   the KVO holds licences again (an activation against it, or a list that
   comes back non-empty) marks the record stale. The codes and the
   timestamp stay, because "released ****-BBBB from 10.1.2.3" is still a
   fact worth printing; what it stops being is evidence. */
var record={},seq=0;

function noteRelease(kvo,resp){
  var host=hostOf(kvo);
  var ok=((resp&&resp.results)||[]).filter(function(r){return r&&r.ok===true;});
  if(!host||!ok.length)return released(kvo);
  var rec=record[host]||(record[host]={kvo:txt(kvo),codes:[],clear:false,unknown:false,stale:false,
                                       when:0,seq:0});
  rec.kvo=txt(kvo);
  rec.when=Date.now();
  // the order releases were made in, which Date.now() does not give: two in
  // the same millisecond is ordinary, and "the most recent" has to be an
  // answer and not a coin toss
  rec.seq=++seq;
  rec.clear=resp.clear===true;
  // the API answers clear:null when it could not read the KVO's list at
  // all. That is not "still holds licences" and it is not "holds none":
  // the record says which, so the Teardown banner can say it too
  rec.unknown=resp.clear!==true&&resp.clear!==false;
  rec.stale=false;                  // this release is the newest word on this KVO
  ok.forEach(function(r){
    var tail=codeTail(r.code);
    if(rec.codes.indexOf(tail)<0)rec.codes.push(tail);
  });
  return released(kvo);
}

/* This KVO holds licences again, or still: an activation landed on it, or
   it listed something. The record keeps its sentence and loses its force. */
function noteHolds(kvo){
  var host=hostOf(kvo),rec=host&&record[host];
  if(!rec)return released(kvo);
  rec.clear=false;
  rec.unknown=false;                // this is not an unread list: it is a licence
  rec.stale=true;
  return released(kvo);
}

function _copy(rec){
  return rec?{kvo:rec.kvo,codes:rec.codes.slice(),clear:rec.clear===true,
              unknown:rec.unknown===true,stale:rec.stale===true,when:rec.when}:null;
}

/* The release made against one appliance, or null. An address in any of
   the forms a screen holds it in: the host is what is compared. */
function released(kvo){
  return _copy(record[hostOf(kvo)]);
}

/* The most recent release made against ANY appliance, or null. The
   Teardown screen falls back to this when it has no record of its own
   KVO: a release from somewhere else is not evidence, and naming the
   appliance it was actually made against is the whole warning. */
function latestRelease(){
  var best=null;
  Object.keys(record).forEach(function(h){
    if(!best||record[h].seq>best.seq)best=record[h];
  });
  return _copy(best);
}

/* --------------------------------------------------------------- render */

var codes=[],rows=[],installed=[],busy=false;

/* Whether Activate has anything to activate: a checked row the KVO
   recognised, with a quantity still on it. The test was `rows.length`, so
   a check that came back with nothing the KVO knew left the button live
   over a table whose every row reads "nothing to activate", and pressing
   it could only produce activate()'s own refusal. This is the one button
   on the screen that spends entitlement that does not come back, so it is
   offered when there is something to spend and not before. The quantity
   counts because setting a row to 0 is how this screen is told to leave
   that code for another batch: activate() picks rows by exactly this
   rule, and the button now says what that rule already decided. */
function activatable(){
  for(var i=0;i<rows.length;i++)if(rows[i].valid&&rows[i].quantity>0)return true;
  return false;
}

function paintActivate(){
  $("licActivate").disabled=busy||!activatable();
}

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
  // status(), not textContent: addCodes marks this line as a refusal, and a
  // line rewritten without clearing that stays red after the bad entry that
  // earned it is gone
  status("licCount",codes.length?codes.length+" code"+(codes.length===1?"":"s"):"",false);
  $("licCheck").disabled=busy||!codes.length;
  paintActivate();
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
  paintActivate();          // before the early return: no rows is nothing to activate
  if(!rows.length){
    tb.innerHTML='<tr><td colspan="4"><span class="dim">Add the codes and press Check: the KVO says what each one holds before anything is spent.</span></td></tr>';
    return;
  }
  tb.innerHTML=rows.map(function(r,i){
    return "<tr><td><code>"+esc(r.tail)+"</code></td>"+
      '<td><span class="st '+(r.valid?"pass":r.running?"warn":"fail")+'">'+
      esc(r.valid?"VALID":r.running?"UNKNOWN":"NO")+"</span></td>"+
      "<td>"+esc(r.summary)+(r.state?' <span class="dim">'+esc(r.state)+"</span>":"")+"</td>"+
      "<td>"+(r.valid&&r.available
        ? '<label class="vh" for="licQty'+i+'">Quantity for the code ending '+esc(r.tail.slice(-4))+"</label>"+
          '<input class="qty" id="licQty'+i+'" type="number" min="1" max="999999" value="'+esc(String(r.quantity))+'">'
        : '<span class="dim">nothing to activate</span>')+"</td></tr>";
  }).join("");
  rows.forEach(function(r,i){
    var el=$("licQty"+i);
    // the quantity decides whether this row is one Activate would
    // send, so the button is repainted with it
    if(el)el.addEventListener("input",function(){r.quantity=num(el.value);paintActivate();});
  });
  paintActivate();
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
  var hosts=Object.keys(record);
  if(!hosts.length){
    $("licReleased").textContent="Nothing released in this session yet. Do it before the KVO goes: a KVO "+
      "deleted with licences on it strands the counts.";
    return;
  }
  hosts.sort(function(a,b){return record[b].seq-record[a].seq;});
  $("licReleased").textContent=hosts.map(function(h){
    var r=record[h];
    return "Released from "+r.kvo+": "+r.codes.join(", ")+". "+
      (r.stale?"Licences have been activated on that KVO, or it has listed licences it still holds, since "+
               "that release: it is not safe to delete yet."
             :r.clear?"That KVO now holds no licences."
                     :r.unknown?"That KVO's licence list could not be read afterwards, so whether it still "+
                                "holds licences is unknown; it is not safe to delete on that."
                               :"That KVO STILL holds licences, so it is not safe to delete yet.");
  }).join(" ")+" The Teardown screen reads this per KVO, and runs with --accept-licence-loss only for a "+
    "KVO that is this stack's own AND clear.";
}

/* ------------------------------------------------------------ the calls */

/* The body every call shares. The password is read HERE, at the moment of
   the call, and the object is thrown away with the request. The address is
   NOT read here: call() reads it once and passes it in, so the request and
   the answer to it name the same appliance. */
function body(action,kvo,extra){
  var b={action:action,kvo:kvo,user:$("licUser").value.trim()||"admin",
         password:$("licPass").value,verify:$("licVerify").checked===true,
         accept_eula:$("licEula").checked===true};
  if(extra)for(var k in extra)if(Object.prototype.hasOwnProperty.call(extra,k))b[k]=extra[k];
  return b;
}

/* One licensing call. activate and release POST one operation per row and
   poll each to its end, so a call with several codes is minutes, not
   seconds: the status line counts the seconds while it runs rather than
   showing one word and looking hung. The API bounds the whole request
   (api.OP_BUDGET) and refuses more than api.MAX_OPS rows in one call, so
   the wait has a ceiling on both sides. */
function call(action,extra,cb){
  if(busy)return;
  // The appliance this call is ADDRESSED to, read once, here, and carried
  // to the callback. The buttons are disabled while a call runs; the
  // address field is not, and it cannot be, because a call is minutes:
  // activate and release POST one operation per row and poll each to its
  // end. Every handler used to re-read the field when the answer came
  // back, so an operator who retyped the address mid-flight had the answer
  // filed against whatever was in the box by then. Activate against
  // 10.1.2.3, retype the field, and the callback marked 10.9.9.9 stale,
  // which is a no-op because nothing was released there, while 10.1.2.3
  // kept a record saying clear:true, stale:false. The Teardown screen
  // reads that record, and a green one puts --accept-licence-loss on the
  // argv of a run that deletes a KVO holding a licence activated a minute
  // earlier: the exact loss the stale rule exists to close.
  var kvo=$("licKvo").value.trim();
  if(!kvo)return status("licStatus","Name the KVO first.",true);
  busy=true;
  ["licLoad","licCheck","licActivate"].forEach(function(id){$(id).disabled=true;});
  var stop=U.ticking("licStatus",action+"...");
  U.post("/api/licences/"+encodeURIComponent(action),body(action,kvo,extra),function(x){
    busy=false;
    stop();
    ["licLoad","licCheck"].forEach(function(id){$(id).disabled=false;});
    paintCodes();
    var why=U.why(x);
    if(why)return status("licStatus",why,true);
    cb(x.d,kvo);
  });
}

function check(){
  if(codes.length>CHECK_MAX)
    return status("licStatus","That is "+codes.length+" codes in one call, and each one is a lookup the "+
      "console POSTs to the KVO and polls to its end inside a single request: it takes at most "+CHECK_MAX+
      " at a time, and they share one polling budget, so a longer list only gives each code less time to "+
      "answer. Remove some and check them in batches.",true);
  call("check",{codes:codes.slice()},function(d){
    rows=codeRows(d);
    renderCodeRows();
    var good=rows.filter(function(r){return r.valid;}).length;
    var open=rows.filter(function(r){return r.running;});
    status("licStatus",good+" of "+rows.length+" code"+(rows.length===1?"":"s")+
      " recognised. Set the quantity per code, then Activate: activating spends entitlement that does not come back."+
      (open.length?" "+open.length+" code"+(open.length===1?" was":"s were")+" still being looked up when the "+
        "console's budget ran out, so nothing is known about "+(open.length===1?"it":"them")+" either way ("+
        open.map(function(r){return r.tail;}).join(", ")+"): check "+(open.length===1?"it":"them")+
        " again on "+(open.length===1?"its":"their")+" own.":""),!!open.length);
  });
}

function activate(){
  var picked=rows.filter(function(r){return r.valid&&r.quantity>0;})
                 .map(function(r){return r.code+","+r.quantity;});
  if(!picked.length)return status("licStatus","No code has a quantity to activate.",true);
  if(picked.length>OPS_MAX)
    return status("licStatus","That is "+picked.length+" codes in one call, and each one is a licensing "+
      "operation polled to its end: the console takes at most "+OPS_MAX+" at a time so a single request "+
      "cannot run for the rest of the afternoon. Set the quantity to 0 on the ones to leave for the next "+
      "batch.",true);
  call("activate",{codes:picked},function(d,kvo){
    installed=licenceRows(d);
    renderInstalled();
    // whatever this KVO's release record said before, something has just
    // been put back ON it: the record keeps its sentence and stops being
    // the evidence the Teardown screen may arm on
    noteHolds(kvo);
    renderRecord();
    var ok=(d.activated||0);
    // a row still running is not a row that failed and not a row that
    // worked: the console stopped polling before the KVO finished, and
    // the entitlement may or may not have been spent
    var open=(d.results||[]).filter(function(r){return r&&r.running===true;});
    var no=(d.results||[]).filter(function(r){return r&&r.ok!==true&&r.running!==true;});
    // a partial or total failure is a failure: the refused codes are named
    // here and the line is styled as the refusal it is
    status("licStatus",ok+" of "+picked.length+" activated."+
      (no.length?" Refused: "+no.map(function(r){
        return codeTail(r.code)+": "+(r.state||"refused");
      }).join("; ")+".":"")+
      (open.length?" "+open.length+" still running, outcome unknown ("+
        open.map(function(r){return codeTail(r.code);}).join(", ")+"): the console's polling budget ran out "+
        "before the KVO finished, so "+(open.length===1?"that code":"those codes")+" may or may not have "+
        "been spent. Press \"List what is installed\" to see what actually landed.":""),
      ok<picked.length);
  });
}

function load(){
  call("list",null,function(d,kvo){
    installed=licenceRows(d);
    renderInstalled();
    // a list that names anything is the KVO saying it still holds
    // something, whatever this session released earlier
    if(installed.length){noteHolds(kvo);renderRecord();}
    // clear is tri-state: the API answers null when it could not read the
    // list at all, and "this KVO holds no licences" is not a thing to say
    // about an answer nobody got
    status("licStatus",installed.length
      ? (installed.length+" licence"+(installed.length===1?"":"s")+" installed on this KVO.")
      : d.clear===true
        ? "This KVO holds no licences."
        : "The KVO's licence list could not be read"+(d.unreadable?" ("+d.unreadable+")":"")+
          ", so what it holds is unknown. That is not the same as holding none: try again before "+
          "tearing anything down.",
      installed.length?false:d.clear!==true);
  });
}

function release(row){
  var what=releaseRow(row);
  if(!window.confirm("Release "+what.quantity+" of "+(row.product||"this licence")+
      " ("+row.tail+") back to the pool? This is the step that must happen BEFORE the KVO is deleted."))return;
  call("release",{rows:[what]},function(d,kvo){
    installed=licenceRows(d);
    renderInstalled();
    noteRelease(kvo,d);
    renderRecord();
    var open=(d.results||[]).filter(function(r){return r&&r.running===true;});
    status("licStatus",d.released
      ? ("Released. "+(d.clear===true
          ? "This KVO now holds no licences."
          : d.clear===false
            ? "Some licences are still installed, so this KVO is still not safe to delete: the Teardown "+
              "screen will keep warning until it is clear."
            : "The KVO's licence list could not be read afterwards"+(d.unreadable?" ("+d.unreadable+")":"")+
              ", so whether it still holds licences is UNKNOWN. It is not clear until it says so: list "+
              "what is installed again before tearing anything down."))
      : open.length
        ? ("The KVO had not finished the release when the console's polling budget ran out, so the outcome "+
           "is unknown: the counts may or may not be back. List what is installed and look before releasing "+
           "again.")
        : "The KVO did not confirm the release; the counts are still with it.",
      !d.released||d.clear!==true);
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
  noteRelease:noteRelease,noteHolds:noteHolds,released:released,latestRelease:latestRelease
};

if(typeof document!=="undefined"&&document.getElementById("licRows"))init();
})();
