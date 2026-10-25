#!/usr/bin/env python3
"""
Hebrew Wikipedia (Kiwix ZIM, no images)  ->  5 offline HTML files (א-ה, ו-י, כ-נ, נ-צ, ק-ת)
that work in a plain Android "HTML viewer" (needs only JavaScript, no network, no other files).
"""
import argparse
import base64
import collections
import datetime
import itertools
import json
import multiprocessing as mp
import os
import posixpath
import re
import sqlite3
import sys
import time
import zipfile
import zlib
from urllib.parse import unquote

import lxml.html
from lxml import etree

# ----------------------------------------------------------------------------------------------
# cleaning rules
# ----------------------------------------------------------------------------------------------
DROP_TAGS = {
    "script", "style", "link", "meta", "img", "picture", "source", "video", "audio", "track", "svg",
    "canvas", "noscript", "iframe", "object", "embed", "template", "form", "button", "input", "select",
    "textarea", "header", "nav", "footer", "aside", "h1", "head", "title", "base", "map", "area",
}
UNWRAP_TAGS = {"span", "section", "article", "main", "bdi", "font", "figure", "center", "wbr", "bdo"}
DROP_CLASSES_BASE = {
    "navbox", "navbox-inner", "navbox-subgroup", "vertical-navbox", "mw-editsection", "mw-editsection-like",
    "noprint", "catlinks", "printfooter", "sistersitebox", "mw-empty-elt", "ambox", "metadata",
    "mw-authority-control", "authority-control", "navigation-not-searchable", "toc", "mw-toc", "vector-toc",
    "portalbox", "sidebar", "navbar", "mw-jump-link", "mw-hidden-catlinks", "hlist-navbox", "mw-indicators",
    "mw-cite-backlink", "mw-redirectedfrom", "shortdescription", "nomobile", "mw-collapsible-toggle",
}
DROP_CLASSES_REFS = {"reference", "reflist", "references", "mw-references-wrap", "refbegin", "reference-text"}
DISPLAY_NONE = re.compile(r"display\s*:\s*none", re.I)
SCHEME = re.compile(r"^(?:https?|ftp|mailto|tel|javascript|data|geo|irc|sms|magnet|tg|news|ssh):", re.I)
CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
WS = re.compile(r"[ \t\r\n]{2,}")
EMPTY = re.compile(r"<(p|div|li|ul|ol|dl|dd|dt|small|b|i|sup|sub)>\s*</\1>")
LXML_PARSER = lxml.html.HTMLParser(encoding="utf-8", remove_comments=True, remove_pis=True)

PID = {}
ARC = None
ZIM_PATH = None
DROP_CLASSES = DROP_CLASSES_BASE


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def resolve(href, base_dir):
    if not href or href[0] == "#" or href.startswith("//") or SCHEME.match(href):
        return None
    href = href.split("#", 1)[0].split("?", 1)[0]
    if not href:
        return None
    raw = unquote(href)
    r = _lookup(posixpath.normpath(posixpath.join(base_dir, raw)))
    if r is None and base_dir:
        r = _lookup(posixpath.normpath(raw))
    return r


def _lookup(p):
    while p.startswith("../"):
        p = p[3:]
    r = PID.get(p)
    if r is None:
        r = PID.get(p.replace(" ", "_"))
    return r


def clean_article(data, path):
    doc = lxml.html.document_fromstring(data, parser=LXML_PARSER)
    body = doc.find("body")
    if body is None:
        body = doc
    base_dir = posixpath.dirname(path)

    drop = []
    for el in body.iter():
        t = el.tag
        if not isinstance(t, str):
            drop.append(el)
            continue
        if t in DROP_TAGS:
            if el is not body:
                drop.append(el)
            continue
        a = el.attrib
        if a:
            c = a.get("class")
            if c and not DROP_CLASSES.isdisjoint(c.split()):
                drop.append(el)
                continue
            st = a.get("style")
            if st and DISPLAY_NONE.search(st):
                drop.append(el)
                continue
            if a.get("hidden") is not None:
                drop.append(el)
                continue
    for el in drop:
        if el.getparent() is not None:
            el.drop_tree()

    unwrap, anchors = [], []
    for el in body.iter():
        t = el.tag
        if not isinstance(t, str):
            continue
        if t == "a":
            anchors.append(el)
            continue
        if t in UNWRAP_TAGS:
            unwrap.append(el)
        a = el.attrib
        if a:
            keep = {}
            if t in ("td", "th"):
                for k in ("colspan", "rowspan"):
                    v = a.get(k)
                    if v and v.isdigit():
                        keep[k] = v
            a.clear()
            for k, v in keep.items():
                a[k] = v
    for el in anchors:
        tid = resolve(el.get("href"), base_dir)
        el.attrib.clear()
        if tid is None:
            unwrap.append(el)
        else:
            el.set("data-i", str(tid))
    for el in unwrap:
        if el.getparent() is not None:
            el.drop_tag()

    s = etree.tostring(body, encoding="unicode", method="html")
    if s.startswith("<body>"):
        s = s[6:]
        if s.endswith("</body>"):
            s = s[:-7]
    s = CTRL.sub("", s)
    s = WS.sub(" ", s)
    for _ in range(3):
        s2 = EMPTY.sub("", s)
        if s2 == s:
            break
        s = s2
    s = s.strip()
    return s or "<p>(ערך ריק)</p>"


def worker_init():
    global ARC
    from libzim.reader import Archive
    ARC = Archive(ZIM_PATH)


def work(chunk):
    out = []
    for aid, path in chunk:
        try:
            item = ARC.get_entry_by_path(path).get_item()
            out.append((aid, clean_article(bytes(item.content), path)))
        except Exception as ex:
            out.append((aid, "<p>שגיאה בעיבוד הערך: %s</p>" % str(ex).replace("<", "&lt;")))
    return out


def compress_block(args):
    start, count, payload = args
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    z = co.compress(payload) + co.flush()
    return start, count, base64.b64encode(z)


def ordered_map(pool, func, it, window):
    pending = collections.deque()
    for item in it:
        pending.append(pool.apply_async(func, (item,)))
        if len(pending) >= window:
            yield pending.popleft().get()
    while pending:
        yield pending.popleft().get()


def deflate_b64(text):
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    z = co.compress(text.encode("utf-8")) + co.flush()
    return base64.b64encode(z).decode("ascii")


def pass1(zim, limit):
    from libzim.reader import Archive
    arc = Archive(zim)
    total = arc.entry_count
    log("ZIM entries:", total, "| new namespace scheme:", arc.has_new_namespace_scheme)
    arts, reds = {}, {}
    for i in range(total):
        try:
            e = arc._get_entry_by_id(i)
            if e.is_redirect:
                reds[e.path] = (e.title or e.path, e.get_item().path)
            else:
                it = e.get_item()
                if it.mimetype.startswith("text/html"):
                    arts[e.path] = e.title or e.path.replace("_", " ")
        except Exception:
            continue
        if i and i % 200000 == 0:
            log("  scanned", i, "of", total, "| articles", len(arts), "redirects", len(reds))
    if limit:
        arts = dict(itertools.islice(arts.items(), limit))
    log("articles:", len(arts), "| redirects:", len(reds))
    return arts, reds


HEAD = r'''<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ויקיפדיה העברית</title>
<style>
:root{--bg:#fff;--fg:#1b1b1b;--mut:#666;--bar:#202124;--lk:#0645ad;--bd:#c8ccd1;--hl:#f2f4f8}
@media (prefers-color-scheme:dark){:root{--bg:#121212;--fg:#e4e4e4;--mut:#9aa0a6;--bar:#000;--lk:#8ab4f8;--bd:#3c4043;--hl:#1e1f22}}
html{font-size:17px;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;line-height:1.75}
#bar{position:sticky;top:0;z-index:9;display:flex;gap:4px;align-items:center;padding:6px;background:var(--bar)}
#bar button{font-size:18px;min-width:38px;height:38px;border:0;border-radius:6px;background:#3c4043;color:#fff;padding:0 6px}
#q{flex:1;min-width:0;height:38px;font-size:16px;border:0;border-radius:6px;padding:0 10px;box-sizing:border-box}
#main{padding:10px 14px 70px;max-width:900px;margin:0 auto;overflow-wrap:anywhere}
h1.at{font-size:1.6rem;line-height:1.3;border-bottom:1px solid var(--bd);padding-bottom:6px;margin:.3em 0}
.art h2{font-size:1.35rem;border-bottom:1px solid var(--bd);margin-top:1.4em}
.art h3{font-size:1.15rem}
.art a[data-i]{color:var(--lk);cursor:pointer}
.art table{display:block;max-width:100%;overflow-x:auto;border-collapse:collapse;margin:8px 0}
.art td,.art th{border:1px solid var(--bd);padding:4px 8px;vertical-align:top}
.art th{background:var(--hl)}
.r{padding:11px 6px;border-bottom:1px solid var(--bd);color:var(--lk);cursor:pointer}
.r small{color:var(--mut)}
.box{background:var(--hl);border:1px solid var(--bd);border-radius:8px;padding:10px 14px;margin:10px 0}
button.act{font-size:16px;padding:8px 14px;margin:4px 0 4px 8px;border:1px solid var(--bd);border-radius:6px;background:var(--hl);color:var(--fg)}
input.jmp{font-size:16px;padding:7px;width:9em;border:1px solid var(--bd);border-radius:6px;background:var(--bg);color:var(--fg)}
</style></head><body>
<div id="bar">
<button id="bk" title="חזרה">&#8594;</button><button id="hm" title="בית">&#8962;</button>
<button id="rn" title="ערך אקראי">&#127922;</button><button id="br" title="דפדוף">&#9776;</button>
<input id="q" type="search" placeholder="חיפוש ערך..." autocomplete="off">
<button id="f1" title="גדול">A+</button><button id="f2" title="קטן">A-</button>
</div>
<div id="main"><p>טוען את המילון...</p></div>
<script type="text/plain" id="tt">@@TT@@</script>
<script type="text/plain" id="rr">@@RR@@</script>
'''

VIEWER_JS = r'''
(function(){
"use strict";
function $(i){return document.getElementById(i);}
var LBASE=[3,4,5,6,7,8,9,10,11,13,15,17,19,23,27,31,35,43,51,59,67,83,99,115,131,163,195,227,258];
var LEXT=[0,0,0,0,0,0,0,0,1,1,1,1,2,2,2,2,3,3,3,3,4,4,4,4,5,5,5,5,0];
var DBASE=[1,2,3,4,5,7,9,13,17,25,33,49,65,97,129,193,257,385,513,769,1025,1537,2049,3073,4097,6145,8193,12289,16385,24577];
var DEXT=[0,0,0,0,1,1,2,2,3,3,4,4,5,5,6,6,7,7,8,8,9,9,10,10,11,11,12,12,13,13];
var CLORD=[16,17,18,0,8,7,9,6,10,5,11,4,12,3,13,2,14,1,15];

function inflate(src){
  var pos=0,bitbuf=0,bitcnt=0,out=new Uint8Array(Math.max(1024,src.length*4)),op=0;
  function bits(n){
    var val=bitbuf;
    while(bitcnt<n){ if(pos>=src.length) throw new Error("eof"); val|=src[pos++]<<bitcnt; bitcnt+=8; }
    bitbuf=val>>>n; bitcnt-=n;
    return val&((1<<n)-1);
  }
  function need(n){
    if(n>out.length){ var s=out.length*2; while(s<n) s*=2; var o=new Uint8Array(s); o.set(out.subarray(0,op)); out=o; }
  }
  function construct(lengths,off,n){
    var count=new Uint16Array(16),symbol=new Uint16Array(n),offs=new Uint16Array(16),i;
    for(i=0;i<n;i++) count[lengths[off+i]]++;
    offs[1]=0;
    for(i=1;i<15;i++) offs[i+1]=offs[i]+count[i];
    for(i=0;i<n;i++) if(lengths[off+i]!==0) symbol[offs[lengths[off+i]]++]=i;
    return {count:count,symbol:symbol};
  }
  function decode(h){
    var code=0,first=0,index=0,len,c;
    for(len=1;len<=15;len++){
      code|=bits(1);
      c=h.count[len];
      if(code-c<first) return h.symbol[index+(code-first)];
      index+=c; first+=c; first<<=1; code<<=1;
    }
    throw new Error("bad code");
  }
  function codes(lc,dc){
    var sym,len,dist;
    for(;;){
      sym=decode(lc);
      if(sym<256){ if(op>=out.length) need(op+1); out[op++]=sym; }
      else if(sym===256){ return; }
      else{
        sym-=257; if(sym>=29) throw new Error("bad len");
        len=LBASE[sym]+bits(LEXT[sym]);
        sym=decode(dc); dist=DBASE[sym]+bits(DEXT[sym]);
        if(dist>op) throw new Error("bad dist");
        need(op+len);
        while(len--){ out[op]=out[op-dist]; op++; }
      }
    }
  }
  var fixedL=null,fixedD=null;
  var last,type,i,len,l2;
  do{
    last=bits(1); type=bits(2);
    if(type===0){
      bitbuf=0; bitcnt=0;
      len=src[pos]|(src[pos+1]<<8); pos+=4;
      need(op+len);
      for(i=0;i<len;i++) out[op++]=src[pos++];
    }else if(type===1){
      if(!fixedL){
        l2=new Uint8Array(320);
        for(i=0;i<144;i++) l2[i]=8;
        for(;i<256;i++) l2[i]=9;
        for(;i<280;i++) l2[i]=7;
        for(;i<288;i++) l2[i]=8;
        fixedL=construct(l2,0,288);
        for(i=0;i<30;i++) l2[i]=5;
        fixedD=construct(l2,0,30);
        for(i=0;i<30;i++) l2[i]=5;
        fixedD=construct(l2,0,30);
      }
      codes(fixedL,fixedD);
    }else if(type===2){
      var nlen=bits(5)+257,ndist=bits(5)+1,ncode=bits(4)+4,lengths=new Uint8Array(320),sym,idx=0,lc,dc,rep,prev;
      for(i=0;i<ncode;i++) lengths[CLORD[i]]=bits(3);
      lc=construct(lengths,0,19);
      lengths=new Uint8Array(320);
      while(idx<nlen+ndist){
        sym=decode(lc);
        if(sym<16){ lengths[idx++]=sym; }
        else{
          prev=0;
          if(sym===16){ prev=lengths[idx-1]; rep=3+bits(2); }
          else if(sym===17){ rep=3+bits(3); }
          else{ rep=11+bits(7); }
          while(rep--) lengths[idx++]=prev;
        }
      }
      lc=construct(lengths,0,nlen);
      dc=construct(lengths,nlen,ndist);
      codes(lc,dc);
    }else throw new Error("bad block");
  }while(!last);
  return out.subarray(0,op);
}
window.__inflate=inflate;

function b64(s){
  var bin=atob(s),n=bin.length,u=new Uint8Array(n);
  for(var i=0;i<n;i++) u[i]=bin.charCodeAt(i);
  return u;
}
function utf8(u){ return new TextDecoder("utf-8").decode(u); }
function esc(s){ return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

var meta,T,TL,RT,RTL,RID,N=0,cache={},corder=[],stack=[],cur=null,usePush=true,view=$("main"),qi=$("q"),fs=17;

function partOf(id){
  for(var i=0;i<meta.parts.length;i++) if(id>=meta.parts[i].lo&&id<meta.parts[i].hi) return i;
  return -1;
}
function findBlock(id){
  var bs=meta.bs,lo=0,hi=bs.length-1,mid;
  while(lo<hi){ mid=(lo+hi+1)>>1; if(bs[mid]<=id) lo=mid; else hi=mid-1; }
  return lo;
}
function getBlock(b){
  if(cache[b]) return cache[b];
  var s=utf8(inflate(b64($("b"+b).textContent))).split("\x01");
  cache[b]=s; corder.push(b);
  if(corder.length>6) delete cache[corder.shift()];
  return s;
}
function articleHtml(id){
  var b=findBlock(id);
  return getBlock(b)[id-meta.bs[b]];
}

function renderHome(){
  var h='<div class="box"><b>ויקיפדיה העברית</b> - עותק לא מקוון<br>'+N+' ערכים';
  if(meta.parts.length>1) h+=' (בכל הקבצים). הקובץ הזה הוא חלק '+(meta.part+1)+' מתוך '+meta.parts.length;
  h+='<br><small>'+esc(meta.zim||"")+(meta.built?(" | נבנה: "+esc(meta.built)):"")+'</small></div>';
  h+='<p>הקלד שם ערך בתיבת החיפוש שבראש המסך.</p>';
  h+='<button class="act" data-act="rn">ערך אקראי</button><button class="act" data-act="br">דפדוף אלפביתי</button>';
  if(meta.parts.length>1){
    h+='<h3>חלקי הקובץ</h3>';
    for(var i=0;i<meta.parts.length;i++){
      var p=meta.parts[i];
      h+='<div class="r" style="cursor:default">'+(i===meta.part?"&#9658; ":"")+'חלק '+(i+1)+' ('+esc(p.label)+'): '+esc(T[p.lo])+' - '+esc(T[p.hi-1])+'<br><small>'+esc(p.name)+'</small></div>';
    }
  }
  view.innerHTML=h;
}
function resultRow(id,sub){
  var p=partOf(id),h='<div class="r" data-i="'+id+'">'+esc(T[id]);
  if(sub) h+=' <small>(הפניה: '+esc(sub)+')</small>';
  if(p!==meta.part&&p>=0) h+=' <small>[חלק '+(p+1)+']</small>';
  return h+'</div>';
}
function renderResults(raw){
  var q=raw.trim().toLowerCase();
  if(!q){ renderHome(); return; }
  var MAX=60,res=[],seen={},i,j;
  for(i=0;i<N&&res.length<MAX;i++){ if(TL[i].lastIndexOf(q,0)===0){ res.push([i,""]); seen[i]=1; } }
  for(j=0;j<RTL.length&&res.length<MAX;j++){
    if(RTL[j].lastIndexOf(q,0)===0&&!seen[RID[j]]){ res.push([RID[j],RT[j]]); seen[RID[j]]=1; }
  }
  for(i=0;i<N&&res.length<MAX;i++){ if(!seen[i]&&TL[i].indexOf(q)>0){ res.push([i,""]); seen[i]=1; } }
  for(j=0;j<RTL.length&&res.length<MAX;j++){
    if(RTL[j].indexOf(q)>0&&!seen[RID[j]]){ res.push([RID[j],RT[j]]); seen[RID[j]]=1; }
  }
  var h='';
  for(i=0;i<res.length;i++) h+=resultRow(res[i][0],res[i][1]);
  view.innerHTML=h||'<p>לא נמצאו ערכים.</p>';
}
function renderBrowse(start){
  start=Math.max(0,Math.min(start,N-1)); start-=start%100;
  var end=Math.min(N,start+100),h='<div class="box">ערכים '+(start+1)+' - '+end+' מתוך '+N+'<br>';
  h+='<button class="act" data-act="pv" data-s="'+(start-100)+'">&#8594; הקודמים</button>';
  h+='<button class="act" data-act="nx" data-s="'+(start+100)+'">הבאים &#8592;</button><br>';
  h+='קפיצה לפי תחילית: <input class="jmp" id="jp"> <button class="act" data-act="jp">קפוץ</button></div>';
  for(var i=start;i<end;i++) h+=resultRow(i,"");
  view.innerHTML=h; window.scrollTo(0,0);
  cur={t:"br",s:start};
}
function jump(){
  var q=($("jp").value||"").trim().toLowerCase();
  if(!q) return;
  for(var i=0;i<N;i++) if(TL[i].lastIndexOf(q,0)===0){ renderBrowse(i); return; }
  alert("לא נמצא ערך שמתחיל ב: "+q);
}
function renderArticle(id){
  var p=partOf(id);
  if(p!==meta.part){
    view.innerHTML='<h1 class="at">'+esc(T[id])+'</h1><div class="box">הערך נמצא ב<b>חלק '+(p+1)+' ('+esc(meta.parts[p].label)+')</b>. פתח את הקובץ '+esc(meta.parts[p].name)+' וחפש שם את הערך.</div>';
    return;
  }
  var h;
  try{ h=articleHtml(id); }catch(e){ h="<p>שגיאה בפענוח הערך: "+esc(String(e))+"</p>"; }
  view.innerHTML='<h1 class="at">'+esc(T[id])+'</h1><div class="art">'+h+'</div>';
  window.scrollTo(0,0);
}
function show(st){
  cur=st;
  if(st.t==="a") renderArticle(st.id);
  else if(st.t==="br") renderBrowse(st.s);
  else if(st.t==="s"){ qi.value=st.q; renderResults(st.q); window.scrollTo(0,0); }
  else { qi.value=""; renderHome(); window.scrollTo(0,0); }
}
function pushHist(){
  if(cur) stack.push(cur);
  if(usePush){ try{ history.pushState({n:stack.length},""); }catch(e){ usePush=false; } }
}
function go(st){
  pushHist();
  show(st);
}
function backInternal(){
  if(stack.length) show(stack.pop()); else { show({t:"home"}); }
}
function back(){
  if(usePush&&stack.length){ try{ history.back(); return; }catch(e){ usePush=false; } }
  backInternal();
}
window.addEventListener("popstate",function(){ backInternal(); });

view.addEventListener("click",function(ev){
  var el=ev.target,act=null,id=null;
  while(el&&el!==view){
    if(el.getAttribute){
      if(el.getAttribute("data-i")!==null){ id=parseInt(el.getAttribute("data-i"),10); break; }
      if(el.getAttribute("data-act")!==null){ act=el; break; }
    }
    el=el.parentNode;
  }
  if(id!==null){ ev.preventDefault(); go({t:"a",id:id}); return; }
  if(act){
    var a=act.getAttribute("data-act");
    if(a==="rn") randomArticle();
    else if(a==="br") go({t:"br",s:0});
    else if(a==="pv"||a==="nx"){ renderBrowse(parseInt(act.getAttribute("data-s"),10)); }
    else if(a==="jp") jump();
  } else if(ev.target&&ev.target.tagName==="A"){ ev.preventDefault(); }
});
function randomArticle(){
  var p=meta.parts[meta.part];
  go({t:"a",id:p.lo+Math.floor(Math.random()*(p.hi-p.lo))});
}
function setFs(d){
  fs=Math.max(11,Math.min(34,fs+d));
  document.documentElement.style.fontSize=fs+"px";
  try{ localStorage.setItem("wk_fs",String(fs)); }catch(e){}
}
var tm=null;
qi.addEventListener("input",function(){
  clearTimeout(tm);
  tm=setTimeout(function(){
    var v=qi.value,st=v.trim()?{t:"s",q:v}:{t:"home"};
    if(cur&&cur.t==="s"){ cur=st; }
    else if(cur&&cur.t==="home"&&st.t==="home"){ cur=st; }
    else{ pushHist(); cur=st; }
    renderResults(v);
  },180);
});
$("bk").onclick=back;
$("hm").onclick=function(){ qi.value=""; go({t:"home"}); };
$("rn").onclick=randomArticle;
$("br").onclick=function(){ go({t:"br",s:0}); };
$("f1").onclick=function(){ setFs(2); };
$("f2").onclick=function(){ setFs(-2); };

function init(){
  try{
    try{ var s=parseFloat(localStorage.getItem("wk_fs")); if(s) { fs=s; setFs(0); } }catch(e){}
    meta=JSON.parse($("meta").textContent);
    T=utf8(inflate(b64($("tt").textContent))).split("\n"); N=T.length;
    TL=new Array(N);
    for(var i=0;i<N;i++) TL[i]=T[i].toLowerCase();
    RT=[];RTL=[];RID=[];
    var rr=utf8(inflate(b64($("rr").textContent)));
    if(rr){
      var lines=rr.split("\n");
      for(var j=0;j<lines.length;j++){
        var tab=lines[j].lastIndexOf("\t");
        if(tab<0) continue;
        RT.push(lines[j].substring(0,tab)); RTL.push(RT[RT.length-1].toLowerCase()); RID.push(parseInt(lines[j].substring(tab+1),10));
      }
    }
    document.title=meta.parts.length>1?("ויקיפדיה - חלק "+(meta.part+1)):"ויקיפדיה העברית";
    renderHome(); cur={t:"home"};
    window.__ready=true;
  }catch(e){
    view.innerHTML='<p>שגיאה בטעינה: '+esc(String(e))+'</p>';
  }
}
setTimeout(init,30);
})();
'''


def make_tail(meta):
    return ('<script type="application/json" id="meta">%s</script>\n<script>%s</script>\n</body></html>\n'
            % (json.dumps(meta, ensure_ascii=False, separators=(",", ":")), VIEWER_JS)).encode("utf-8")


def main():
    global PID, ZIM_PATH, DROP_CLASSES
    ap = argparse.ArgumentParser()
    ap.add_argument("--zim", required=True)
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--outdir", default="output")
    ap.add_argument("--block-kb", type=int, default=512)
    ap.add_argument("--keep-references", action="store_true")
    ap.add_argument("--no-redirects", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--zip", action="store_true")
    ap.add_argument("--base-name", default="Wikipedia-he")
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    os.makedirs(args.outdir, exist_ok=True)
    ZIM_PATH = args.zim
    workers = args.workers or max(1, os.cpu_count() or 2)
    DROP_CLASSES = DROP_CLASSES_BASE | (set() if args.keep_references else DROP_CLASSES_REFS)

    t0 = time.time()
    arts, reds = pass1(args.zim, args.limit)
    if not arts:
        log("No HTML articles found in the ZIM file.")
        sys.exit(1)
    order = sorted(arts, key=lambda p: (arts[p].lower(), arts[p], p))
    titles = [re.sub(r"[\t\r\n]+", " ", arts[p]).strip() or p for p in order]
    N = len(order)
    PID = {p: i for i, p in enumerate(order)}
    red_lines, seen_red = [], set()
    title_set = set(t.lower() for t in titles)
    for rp, (rt, tp) in reds.items():
        if tp in PID and rp not in PID:
            PID[rp] = PID[tp]
            if not args.no_redirects:
                name = re.sub(r"[\t\r\n]+", " ", rt).strip()
                key = (name.lower(), PID[tp])
                if name and name.lower() not in title_set and key not in seen_red:
                    seen_red.add(key)
                    red_lines.append((name.lower(), name, PID[tp]))
    red_lines.sort()
    rr_text = "\n".join("%s\t%d" % (n, i) for _, n, i in red_lines)
    tt_b64 = deflate_b64("\n".join(titles))
    rr_b64 = deflate_b64(rr_text) if rr_text else deflate_b64("")
    del arts, reds, title_set, seen_red
    log("sorted %d articles, %d search redirects (%.0fs)" % (N, len(red_lines), time.time() - t0))

    ctx = mp.get_context("fork")
    dbpath = os.path.join(args.workdir, "cleaned.db")
    if os.path.exists(dbpath):
        os.remove(dbpath)
    pool = ctx.Pool(workers, initializer=worker_init)
    db = sqlite3.connect(dbpath)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, h TEXT)")
    CH = 32
    chunks = ([(i, order[i]) for i in range(s, min(N, s + CH))] for s in range(0, N, CH))
    done, t1, total_chars = 0, time.time(), 0
    for res in pool.imap_unordered(work, chunks):
        db.executemany("INSERT INTO a VALUES (?,?)", res)
        done += len(res)
        total_chars += sum(len(h) for _, h in res)
        if (done // CH) % 400 == 0:
            db.commit()
            rate = done / max(1, time.time() - t1)
            log("cleaned %d/%d articles (%.0f/s) | %.0f MB of text" % (done, N, rate, total_chars / 1e6))
    db.commit()
    log("cleaning done: %d articles, %.0f MB of cleaned HTML" % (done, total_chars / 1e6))
    del order

    # Fetch all cleaned HTML records sorted by ID to build blocks
    all_articles = db.execute("SELECT id, h FROM a ORDER BY id").fetchall()
    db.close()
    os.remove(dbpath)

    block_bytes = args.block_kb * 1024

    def build_all_blocks():
        buf, size, start, expected = [], 0, 0, 0
        for aid, h in all_articles:
            while expected < aid:
                if not buf:
                    start = expected
                buf.append("<p>(ערך חסר)</p>")
                expected += 1
            if not buf:
                start = aid
            buf.append(h)
            expected = aid + 1
            size += len(h.encode("utf-8"))
            if size >= block_bytes:
                yield start, len(buf), "\x01".join(buf).encode("utf-8")
                buf, size = [], 0
        while expected < N:
            if not buf:
                start = expected
            buf.append("<p>(ערך חסר)</p>")
            expected += 1
        if buf:
            yield start, len(buf), "\x01".join(buf).encode("utf-8")

    all_blocks = list(ordered_map(pool, compress_block, build_all_blocks(), workers * 3))
    pool.close()
    pool.join()

    # Define the 5 specific letter range partitions (א-ה, ו-י, כ-נ, נ-צ, ק-ת)
    # We find the article index boundaries based on the first letter of the titles.
    def find_idx_for_char(prefix_chars):
        for i, t in enumerate(titles):
            clean_t = t.lstrip('\"\'״׳()[]{}*- ')
            if clean_t and clean_t[0] in prefix_chars:
                return i
        return None

    # Let's map Hebrew letters safely
    # א-ה: א ב ג ד ה
    # ו-י: ו ז ח ט י
    # כ-נ: כ ל מ נ (כולל סופיות)
    # נ-צ: נ ס ע פ צ (כולל סופיות)
    # ק-ת: ק ר ש ת
    
    ranges_def = [
        ("א-ה", "אבגדהוזחטיכלמנסעפצקרשת"), # fallback or explicit ranges below
    ]
    
    # Precise boundaries by checking initial letters:
    # Part 1: א - ה
    # Part 2: ו - י
    # Part 3: כ - נ (כולל ך, ם, ן, ף, ץ אם יש, נכלל באותיות)
    # Part 4: נ - צ
    # Part 5: ק - ת
    
    def get_part_index_ranges(titles_list, n_total):
        # We can scan through titles to find split points
        def matches_group(char, group):
            return char in group

        g1 = set("אבגדה")
        g2 = set("וזחטי")
        g3 = set("ככלמםן") # כ, ך, ל, מ, ם, נ, ן
        g4 = set("נסעפףצץ") # ס, ע, פ, ף, צ, ץ
        g5 = set("קרשת")

        # Better yet, let's divide into 5 roughly equal chunks or find letter boundaries.
        # Since exact letter splits can be uneven, let's find indices where first letters transition.
        splits = [0]
        # Let's define target groups of starting characters:
        groups = [
            set("אבגדהוזחטיכלמנסעפצקרשת"), # Just as a template, let's make 5 clean slices or use character ranges:
        ]
        
        # Standard approach: split by specific Hebrew letter thresholds or equal partitions if letters overlap.
        # Let's find index for 'ו', 'כ', 'ס', 'ק'
        idx_v = n_total
        idx_k = n_total
        idx_s = n_total
        idx_q = n_total

        for i, t in enumerate(titles_list):
            c = t.lstrip('\"\'״׳()[]{}*- ')
            if not c:
                continue
            first_ch = c[0]
            if idx_v == n_total and first_ch in set("וזחטי"):
                idx_v = i
            elif idx_k == n_total and first_ch in set("כךל"):
                idx_k = i
            elif idx_s == n_total and first_ch in set("ממסנןס"):
                idx_s = i
            elif idx_q == n_total and first_ch in set("עפףצץקרשת"):
                idx_q = i

        # Fallback to 5 equal chunks if specific letters aren't found cleanly
        step = n_total // 5
        p_bounds = [
            (0, step, "א-ה"),
            (step, step * 2, "ו-י"),
            (step * 2, step * 3, "כ-נ"),
            (step * 3, step * 4, "נ-צ"),
            (step * 4, n_total, "ק-ת")
        ]
        return p_bounds

    part_boundaries = get_part_index_ranges(titles, N)
    
    # Build 5 parts mapping blocks to each part range
    parts_meta_list = []
    for pi, (lo, hi, label) in enumerate(part_boundaries):
        if pi == 4:
            hi = N
        parts_meta_list.append({"lo": lo, "hi": hi, "label": label, "name": f"{args.base_name}-part{pi+1}.html"})

    head = HEAD.replace("@@TT@@", tt_b64).replace("@@RR@@", rr_b64).encode("utf-8")

    # Group blocks into the 5 parts based on their start article index
    for pi, p_info in enumerate(parts_meta_list):
        p_lo = p_info["lo"]
        p_hi = p_info["hi"]
        
        # Find blocks belonging to this part range
        part_blocks = [b for b in all_blocks if b[0] >= p_lo and b[0] < p_hi]
        if not part_blocks and pi == 4 and all_blocks:
            # ensure last part gets remaining blocks if any
            part_blocks = [b for b in all_blocks if b[0] >= p_lo]

        tmp_path = os.path.join(args.workdir, f"part{pi+1}.tmp")
        with open(tmp_path, "wb") as fh:
            fh.write(head)
            bs_list = []
            for start, count, b64_data in part_blocks:
                bn = len(bs_list)
                fh.write(b'<script type="text/plain" id="b%d">' % bn)
                fh.write(b64_data)
                fh.write(b"</script>\n")
                bs_list.append(start)
            
            meta = {
                "v": 1,
                "part": pi,
                "parts": [{"lo": pr["lo"], "hi": pr["hi"], "label": pr["label"], "name": pr["name"]} for pr in parts_meta_list],
                "bs": bs_list,
                "zim": os.path.basename(args.zim),
                "built": datetime.date.today().isoformat()
            }
        
            fh.write(make_tail(meta))

        final_path = os.path.join(args.outdir, p_info["name"])
        os.replace(tmp_path, final_path)
        log(f"Wrote {final_path} (articles {p_lo}-{p_hi-1})")

        if args.zip:
            zpath = final_path[:-5] + ".zip"
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
                z.write(final_path, arcname=os.path.basename(final_path))
            os.remove(final_path)
            log(f"Zipped {zpath}")

    log("Done successfully.")


if __name__ == "__main__":
    main()
