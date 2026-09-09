(function(){
"use strict";
/* app.js: the two things this page needs before any screen loads - the
   theme, and the node icon set.

   It used to be the quick flows: four tabs, a Run button, POST /run, and
   an instrument that drew the diagram and the narration. That whole
   surface is gone. It was a second, unlocked way to start a real deploy
   (no one-engine-per-stack lock, no input validation, no redaction) sitting
   under a badge that said the page was replaying, and three of its four
   commands could not run at all. The operations screens above are the way
   in now, and the replay it offered lives on the published demo page,
   which build_site.py assembles and which replays client-side.

   What is left here is what the rest of the page still reads:
     the theme button, which belongs to no screen
     window.clConsole.icons, the node icon set watch.js draws its topology
       with, so that screen speaks the diagram's language instead of
       inventing a second one
     the four cards under the screens, drawn from GET /flows, which is the
       same data the published page is built from */
var $=function(id){return document.getElementById(id);};

/* theme */
$("themeBtn").addEventListener("click",function(){
  var cur=document.documentElement.getAttribute("data-theme")||"light";
  var nxt=cur==="dark"?"light":"dark";
  document.documentElement.setAttribute("data-theme",nxt);
  try{localStorage.setItem("cl-theme",nxt);}catch(e){}
});

/* icons */
var IC={
 vpc:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M3 9h18M9 3v18"/></svg>',
 clms:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
 kvo:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="3"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4M5 5l3 3M16 16l3 3M19 5l-3 3M8 16l-3 3"/></svg>',
 vpb:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 7V5M12 7V5M17 7V5M7 17v2M12 17v2M17 17v2"/></svg>',
 vm:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="4" y="4" width="16" height="12" rx="2"/><path d="M2 20h20"/></svg>',
 tool:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M14 6l4 4-8 8-4 1 1-4z"/><path d="M4 20h6"/></svg>',
 mirror:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3v18M7 8l-4 4 4 4M17 8l4 4-4 4"/></svg>',
 coll:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M12 3a4 4 0 0 0-4 4H6a3 3 0 0 0 0 6h12a3 3 0 0 0 0-6h-2a4 4 0 0 0-4-4z"/><path d="M9 17l3 4 3-4"/></svg>'
};

/* the four cards, from the server's own flow data. A page that cannot
   reach the server says so where the cards would have been, rather than
   leaving an empty strip that reads as "there are none". */
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,function(c){return{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}

fetch("/flows").then(function(r){return r.json();}).then(function(d){
  var cards=$("flowCards");
  d.order.forEach(function(id,i){
    var f=d.flows[id];
    var c=document.createElement("div");c.className="card";
    c.innerHTML='<div class="k">FLOW 0'+(i+1)+'</div><h3>'+esc(f.name)+'</h3><p>'+esc(f.subtitle)+'</p>';
    cards.appendChild(c);
  });
}).catch(function(){
  $("flowCards").innerHTML='<div class="card"><p>Could not load the flow list. Is the console server running?</p></div>';
});

window.clConsole={icons:IC};
})();
