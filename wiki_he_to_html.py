#!/usr/bin/env python3
"""
Kiwix ZIM (Hebrew Wikipedia / Wikiquote / Hamichlol, no images)  ->  offline HTML files of ~30MB each
that work in a plain Android "HTML viewer" (needs only JavaScript, no network, no other files).

How the output works
  * every article is cleaned (no images/scripts/navboxes), links are rewritten to article numbers
  * articles are packed (alphabetical order) into blocks of ~512KB, each block is deflate-compressed + base64
  * the HTML contains a small viewer (search, alphabetical browse, random, back, font size) + all blocks
  * a block is decompressed only when needed, so the viewer does not decompress the whole encyclopedia
  * the output is split into parts of ~30MB (soft limit, may reach ~32MB); articles stay in alphabetical order,
    and every file is named after the first and last article it contains
  * every part is a complete standalone viewer with its own search / browse index (only its own articles);
    a link to an article that sits in another part tells you which file to open
  * parts are grouped into ZIPs (ASCII names, so GitHub keeps the name); the Hebrew file names live inside the ZIP
"""
import argparse
import base64
import collections
import datetime
import html as htmllib
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
BAD_FN = re.compile(r'[<>:"/\\|?*\x00-\x1f\u200e\u200f\u202a-\u202e]')
LXML_PARSER = lxml.html.HTMLParser(encoding="utf-8", remove_comments=True, remove_pis=True)

# globals filled before the worker pool is forked
PID = {}
ARC = None
ZIM_PATH = None
DROP_CLASSES = DROP_CLASSES_BASE


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def resolve(href, base_dir):
    """href of a link inside an article -> article number (or None)."""
    if not href or href[0] == "#" or href.startswith("//") or SCHEME.match(href):
        return None
    href = href.split("#", 1)[0].split("?", 1)[0]
    if not href:
        return None
    raw = unquote(href)
    r = _lookup(posixpath.normpath(posixpath.join(base_dir, raw)))
    if r is None and base_dir:          # article path contains "/" and the link is relative to the root
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
        except Exception as ex:  # keep going, never lose an id
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


# ----------------------------------------------------------------------------------------------
# pass 1: titles + redirects
# ----------------------------------------------------------------------------------------------
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


# ----------------------------------------------------------------------------------------------
# viewer (HTML head / tail)
# ----------------------------------------------------------------------------------------------
HEAD = r'''<!doctype html>
<html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@@TITLE@@</title>
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
<div id="main"><p>טוען את המילון... (בקובץ גדול זה יכול לקחת כמה שניות)</p></div>
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

var meta,T,TL,RT,RTL,RID,N=0,LO=0,cache={},corder=[],stack=[],cur=null,usePush=true,view=$("main"),qi=$("q"),fs=17;

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
  var h='<div class="box"><b>'+esc(meta.title)+'</b> - עותק לא מקוון<br>'+N+' ערכים בקובץ הזה';
  if(meta.parts.length>1) h+=' (מתוך '+meta.total+' בכל הקבצים). הקובץ הזה הוא חלק '+(meta.part+1)+' מתוך '+meta.parts.length;
  h+='<br><small>'+esc(meta.zim||"")+(meta.built?(" | נבנה: "+esc(meta.built)):"")+'</small></div>';
  h+='<p>הקלד שם ערך בתיבת החיפוש שבראש המסך.</p>';
  h+='<button class="act" data-act="rn">ערך אקראי</button><button class="act" data-act="br">דפדוף אלפביתי</button>';
  if(meta.parts.length>1){
    h+='<h3>כל הקבצים</h3>';
    for(var i=0;i<meta.parts.length;i++){
      var p=meta.parts[i];
      h+='<div class="r" style="cursor:default">'+(i===meta.part?"&#9658; ":"")+'חלק '+(i+1)+': '+esc(p.a)+' - '+esc(p.z)+'<br><small>'+esc(p.name)+'</small></div>';
    }
  }
  view.innerHTML=h;
}
function resultRow(id,sub){
  var h='<div class="r" data-i="'+id+'">'+esc(T[id-LO]);
  if(sub) h+=' <small>(הפניה: '+esc(sub)+')</small>';
  return h+'</div>';
}
function renderResults(raw){
  var q=raw.trim().toLowerCase();
  if(!q){ renderHome(); return; }
  var MAX=60,res=[],seen={},i,j;
  for(i=0;i<N&&res.length<MAX;i++){ if(TL[i].lastIndexOf(q,0)===0){ res.push([i+LO,""]); seen[i+LO]=1; } }
  for(j=0;j<RTL.length&&res.length<MAX;j++){
    if(RTL[j].lastIndexOf(q,0)===0&&!seen[RID[j]]){ res.push([RID[j],RT[j]]); seen[RID[j]]=1; }
  }
  for(i=0;i<N&&res.length<MAX;i++){ if(!seen[i+LO]&&TL[i].indexOf(q)>0){ res.push([i+LO,""]); seen[i+LO]=1; } }
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
  for(var i=start;i<end;i++) h+=resultRow(i+LO,"");
  view.innerHTML=h; window.scrollTo(0,0);
  cur={t:"br",s:start};
}
function jump(){
  var q=($("jp").value||"").trim().toLowerCase();
  if(!q) return;
  for(var i=0;i<N;i++) if(TL[i].lastIndexOf(q,0)===0){ renderBrowse(i); return; }
  alert("לא נמצא ערך שמתחיל ב: "+q);
}
function renderArticle(id,label){
  var p=partOf(id);
  if(p!==meta.part){
    var lt=(label||"").trim();
    view.innerHTML='<h1 class="at">'+esc(lt||"ערך בקובץ אחר")+'</h1>'+(p>=0?'<div class="box">הערך נמצא ב<b>חלק '+(p+1)+'</b> (ערכים '+esc(meta.parts[p].a)+' - '+esc(meta.parts[p].z)+')<br><small>'+esc(meta.parts[p].name)+'</small><br>פתח את הקובץ הזה וחפש שם את הערך.</div>':'<div class="box">הערך לא נמצא.</div>');
    window.scrollTo(0,0);
    return;
  }
  var h;
  try{ h=articleHtml(id); }catch(e){ h="<p>שגיאה בפענוח הערך: "+esc(String(e))+"</p>"; }
  view.innerHTML='<h1 class="at">'+esc(T[id-LO])+'</h1><div class="art">'+h+'</div>';
  window.scrollTo(0,0);
}
function show(st){
  cur=st;
  if(st.t==="a") renderArticle(st.id,st.l);
  else if(st.t==="br") renderBrowse(st.s);
  else if(st.t==="s"){ qi.value=st.q; renderResults(st.q); window.scrollTo(0,0); }
  else { qi.value=""; renderHome(); window.scrollTo(0,0); }
  cur=st;
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
  if(id!==null){ ev.preventDefault(); go({t:"a",id:id,l:(el.textContent||"")}); return; }
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
    T=utf8(inflate(b64($("tt").textContent))).split("\n"); N=T.length; LO=meta.parts[meta.part].lo;
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
    document.title=meta.parts.length>1?(meta.title+" - חלק "+(meta.part+1)):meta.title;
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


# ----------------------------------------------------------------------------------------------
def main():
    global PID, ZIM_PATH, DROP_CLASSES
    ap = argparse.ArgumentParser()
    ap.add_argument("--zim", required=True)
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--outdir", default="output")
    ap.add_argument("--part-mb", type=int, default=30, help="target size of one HTML file (MB); a file may go a bit over")
    ap.add_argument("--hard-mb", type=int, default=32, help="warn if an HTML file is bigger than this (MB)")
    ap.add_argument("--zip-limit-mb", type=int, default=1800, help="start a new ZIP when the current one reaches about this size (MB)")
    ap.add_argument("--title", default="ויקיפדיה העברית", help="name shown inside the viewer")
    ap.add_argument("--block-kb", type=int, default=512)
    ap.add_argument("--keep-references", action="store_true")
    ap.add_argument("--no-redirects", action="store_true", help="do not add redirect names to the search")
    ap.add_argument("--limit", type=int, default=0, help="only first N articles (testing)")
    ap.add_argument("--workers", type=int, default=0)
    ap.add_argument("--zip", action="store_true", help="group the HTML files into ZIPs (and delete the .html)")
    ap.add_argument("--base-name", default="Wikipedia-he")
    args = ap.parse_args()

    os.makedirs(args.workdir, exist_ok=True)
    os.makedirs(args.outdir, exist_ok=True)
    ZIM_PATH = args.zim
    workers = args.workers or max(1, os.cpu_count() or 2)
    DROP_CLASSES = DROP_CLASSES_BASE | (set() if args.keep_references else DROP_CLASSES_REFS)

    # ---- pass 1
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
    del arts, reds, title_set, seen_red
    log("sorted %d articles, %d search redirects (%.0fs)" % (N, len(red_lines), time.time() - t0))

    # ---- stage A: clean every article in parallel -> sqlite
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
            log("cleaned %d/%d articles (%.0f/s, eta %.0f min) | %.0f MB of text" %
                (done, N, rate, (N - done) / max(rate, 1) / 60, total_chars / 1e6))
    db.commit()
    log("cleaning done: %d articles, %.0f MB of cleaned HTML (%.0f min)" % (done, total_chars / 1e6, (time.time() - t1) / 60))
    del order

    # ---- stage B: blocks -> parts (~part-mb each)
    block_bytes = args.block_kb * 1024
    part_limit = args.part_mb * 1024 * 1024
    hard_limit = args.hard_mb * 1024 * 1024

    # rough size of the per-part title/search index, so the final file lands near part_mb
    idx_cost = [len(t.encode("utf-8")) + 1 for t in titles]
    for _, name, tid in red_lines:
        idx_cost[tid] += len(name.encode("utf-8")) + 8
    INDEX_FACTOR = 0.8                                   # deflate + base64 of the title lists
    BASE_OVERHEAD = len(HEAD) * 2 + len(VIEWER_JS) * 2 + 20000

    def block_iter():
        buf, size, start, expected = [], 0, 0, 0
        for aid, h in db.execute("SELECT id, h FROM a ORDER BY id"):
            while expected < aid:               # safety: never leave a hole
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

    parts = []   # dicts: blk (temp file with the packed blocks), lo, hi, bs, fh, bytes, idx

    def new_part():
        p = {"blk": os.path.join(args.workdir, "part%d.blk" % (len(parts) + 1)), "bs": [], "lo": None, "hi": None,
             "bytes": 0, "idx": 0.0}
        p["fh"] = open(p["blk"], "wb")
        parts.append(p)
        return p

    def part_est(p):
        return BASE_OVERHEAD + p["bytes"] + p["idx"]

    part = new_part()
    t2, nblocks = time.time(), 0
    for start, count, b64 in ordered_map(pool, compress_block, block_iter(), workers * 3):
        blob_len = len(b64) + 64
        add_idx = sum(idx_cost[start:start + count]) * INDEX_FACTOR
        if part["bs"] and part_est(part) + blob_len + add_idx > part_limit:
            part["fh"].close()
            log("part %d complete: %d articles, ~%.1f MB" % (len(parts), part["hi"] - part["lo"], part_est(part) / 1e6))
            part = new_part()
        if part["lo"] is None:
            part["lo"] = start
        bn = len(part["bs"])
        part["fh"].write(b'<script type="text/plain" id="b%d">' % bn)
        part["fh"].write(b64)
        part["fh"].write(b"</script>\n")
        part["bs"].append(start)
        part["hi"] = start + count
        part["bytes"] += blob_len
        part["idx"] += add_idx
        nblocks += 1
        if nblocks % 200 == 0:
            log("packed %d blocks (article %d/%d)" % (nblocks, start + count, N))
    part["fh"].close()
    pool.close()
    pool.join()
    db.close()
    os.remove(dbpath)
    log("packed %d blocks into %d part(s) (%.0f min)" % (nblocks, len(parts), (time.time() - t2) / 60))

    # ---- finalize parts: file names (first - last article), per-part index, viewer
    def fn_piece(t, maxlen=35):
        t = BAD_FN.sub(" ", t)
        t = re.sub(r"\s+", " ", t).strip(" .")
        if len(t) > maxlen:
            t = t[:maxlen].rstrip(" .")
        return t or "_"

    width = max(3, len(str(len(parts))))
    names = []
    for i, p in enumerate(parts):
        first, last = titles[p["lo"]], titles[p["hi"] - 1]
        names.append("%0*d_%s - %s.html" % (width, i + 1, fn_piece(first), fn_piece(last)))
    plist = [{"lo": p["lo"], "hi": p["hi"], "name": names[i], "a": titles[p["lo"]], "z": titles[p["hi"] - 1]}
             for i, p in enumerate(parts)]
    zim_name = os.path.basename(args.zim)
    built = datetime.date.today().isoformat()
    title_html = htmllib.escape(args.title)
    outputs = []
    for i, p in enumerate(parts):
        lo, hi = p["lo"], p["hi"]
        tt_b64 = deflate_b64("\n".join(titles[lo:hi]))
        rr_text = "\n".join("%s\t%d" % (nm, t) for _, nm, t in red_lines if lo <= t < hi)
        rr_b64 = deflate_b64(rr_text)
        head = HEAD.replace("@@TITLE@@", title_html).replace("@@TT@@", tt_b64).replace("@@RR@@", rr_b64).encode("utf-8")
        meta = {"v": 2, "part": i, "parts": plist, "bs": p["bs"], "zim": zim_name, "built": built,
                "title": args.title, "total": N}
        final = os.path.join(args.outdir, names[i])
        with open(final, "wb") as out:
            out.write(head)
            with open(p["blk"], "rb") as src:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    out.write(chunk)
            out.write(make_tail(meta))
        os.remove(p["blk"])
        outputs.append(final)
        size = os.path.getsize(final)
        log("wrote %s  %.1f MB  (articles %d-%d)%s" % (names[i], size / 1e6, lo, hi - 1,
            "  WARNING: bigger than %d MB" % args.hard_mb if size > hard_limit else ""))

    if args.zip:
        zip_limit = args.zip_limit_mb * 1024 * 1024
        zi, z, zpath, zc, last_c = 0, None, None, 0, 0
        zips = []
        for final in outputs:
            if z is not None and zc + last_c * 1.2 > zip_limit:
                z.close()
                z = None
            if z is None:
                zi += 1
                zpath = os.path.join(args.outdir, "%s-zip%02d.zip" % (args.base_name, zi))
                z = zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True)
                zips.append(zpath)
                zc = 0
            z.write(final, arcname=os.path.basename(final))
            last_c = z.infolist()[-1].compress_size
            zc += last_c
            os.remove(final)
        if z is not None:
            z.close()
        for zpath in zips:
            zmb = os.path.getsize(zpath) / 1048576
            log("zipped %s  %.0f MB" % (os.path.basename(zpath), zmb))
            if zmb > 1990:
                log("ERROR: ZIP is bigger than 1990 MB (GitHub Release limit is 2 GiB). Run again with a smaller --zip-limit-mb.")
                sys.exit(2)
    log("done in %.1f min" % ((time.time() - t0) / 60))


if __name__ == "__main__":
    main()
