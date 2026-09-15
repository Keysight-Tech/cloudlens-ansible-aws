(function(){
"use strict";
/* ui.js: the surface the three operations screens share.

   Operate, Licensing and Teardown each had their own copy of $(), txt(),
   esc(), status() and a fetch envelope. Five near-identical things is
   four chances to fix a bug in one place and leave it in three, and one
   of them mattered: each esc() fell back to String(s) with NO escaping
   when window.clPlan was missing, so a screen that loaded without its
   dependency rendered a KVO's own words as markup. An escaper fails
   closed or it is not an escaper, so there is now one, here, and the
   screens take it from this object rather than carrying a fallback.

   A screen that loads without this file throws at once, on its first
   line, and draws nothing. That is deliberate: nothing half-rendered,
   nothing unescaped, and a message in the browser's console saying which
   object was missing.

   hostOf() and codeTail() live here for the same reason. Both are rules
   two screens have to agree on exactly: Licensing records a release
   against a host and Teardown decides whether that host is this stack's
   KVO, and both show an activation code by its last four characters and
   never whole. Two copies of a comparison are two answers waiting to
   disagree.

   No DOM is touched at load, so tests/test_ops_model.py loads this under
   node ahead of the screen it is testing. */

/* ------------------------------------------------------------ the page */

function $(id){return document.getElementById(id);}

function txt(v){return v===undefined||v===null?"":String(v);}

/* HTML text. The only escaper these screens have: there is no fallback
   path that returns the string unchanged, because the one thing worse
   than no escaping is escaping that quietly stops.

   The apostrophe is in the set. It was not, and an escaper that covers
   three of the four delimiters is one attribute away from useless: the
   screens here build markup by concatenation, and a single-quoted
   attribute written anywhere in any of them would have been open to a
   value carrying one. Escaping it costs nothing and removes the question. */
function esc(s){
  return String(s==null?"":s).replace(/[&<>"']/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
  });
}

/* One status line: the text, and whether it is a refusal. */
function status(id,text,bad){
  var el=$(id);
  if(!el)return;
  el.textContent=text||"";
  el.classList.toggle("err",!!bad);
}

/* ---------------------------------------------------------- the domain */

/* The host in an address a screen holds: a bare IP or name, as the
   Licensing screen took it, or a url. Whole hosts only: 3.1.1.1 is not
   3.1.1.10, and a pair of elastic IPs like that is ordinary. */
function hostOf(v){
  var s=txt(v).trim();
  if(!s)return "";
  var i=s.indexOf("://");
  if(i>=0)s=s.slice(i+3);
  s=s.split("/")[0].split("?")[0].split("#")[0];
  var at=s.lastIndexOf("@");
  if(at>=0)s=s.slice(at+1);
  if(s.charAt(0)==="["){var e=s.indexOf("]");return e<0?"":s.slice(1,e).toLowerCase();}
  return s.split(":")[0].toLowerCase();
}

/* An activation code as a screen is willing to display it: the last four
   characters, and the quantity when the token carries one. A code on a
   shared screen is a code somebody else can spend. */
function codeTail(c){
  var parts=String(c||"").split(",");
  return "****-"+parts[0].slice(-4)+(parts[1]?","+parts[1]:"");
}

/* -------------------------------------------------------- the requests */

/* Every call these screens make comes back in one shape,
   {ok,status,d,err}, and is read by why() below. A body that is not JSON
   leaves d null rather than throwing, and a server that cannot be reached
   is err: neither is ever mistaken for an answer. */
function send(path,opts,cb){
  fetch(path,opts)
   .then(function(r){return r.text().then(function(t){
     var d=null;try{d=JSON.parse(t);}catch(e){}
     return {ok:r.ok,status:r.status,d:d};});})
   .catch(function(){return {err:"Could not reach the console server."};})
   .then(function(x){cb(x);});
}

function post(path,body,cb){
  send(path,{method:"POST",headers:{"Content-Type":"application/json"},
             body:JSON.stringify(body)},cb);
}

function get(url,cb){
  send(url,{headers:{"Accept":"application/json"}},cb);
}

/* Why an answer is not one, or "" when it is. The API refuses in three
   shapes ({error}, {errors}, and a status code), and a screen that reads
   only one of them shows an empty page beside a silent refusal. */
function why(x){
  if(!x)return "The console server said nothing.";
  if(x.err)return x.err;
  var d=x.d||{};
  if(d.error)return String(d.error);
  if(d.errors&&d.errors.length)return [].concat(d.errors).join("; ");
  if(!x.ok)return "The console refused it (HTTP "+x.status+").";
  if(!x.d)return "The console server sent an answer that is not JSON.";
  return "";
}

/* Elapsed seconds on a status line, while a call the operator cannot see
   is running. One licensing POST activates or releases row by row and
   each row is polled to its end, so a static "activate..." is the only
   thing a page shows for what can be minutes. Returns the function that
   stops it. */
function ticking(id,text){
  var t0=Date.now(),timer=null;
  var paint=function(){
    var s=Math.round((Date.now()-t0)/1000);
    status(id,text+(s?" ("+s+"s)":""));
  };
  paint();
  if(typeof setInterval==="function"){
    timer=setInterval(paint,1000);
    return function(){if(timer){clearInterval(timer);timer=null;}};
  }
  return function(){};
}

window.clUi={$:$,txt:txt,esc:esc,status:status,hostOf:hostOf,codeTail:codeTail,
             post:post,get:get,why:why,ticking:ticking};
})();
