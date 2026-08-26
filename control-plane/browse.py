# SPDX-License-Identifier: AGPL-3.0-only
"""Element-level browsing built on deskclient's /eval, /exec and /action seams.

Same design as deskclient.navigate: composed in the control plane so it reaches
every computer that exists today on a cased restart — no deskd change, no image
rebuild.

Five capabilities:
  snapshot        numbered visible interactive elements (the token-cheap "what's
                  on the page" that replaces a screenshot for most clicks)
  click_element   re-derive the same numbered list, verify the ref still matches,
                  scroll it into view, then fire a real OS-level click through
                  /action at the computed screen coordinates (isTrusted stays true)
  fill            batch-fill a form via native value setters + input/change events
                  (React-safe); refuses password fields — vault login owns those
  wait_for        server-side 0.4s poll for selector/text/network-idle, so agents
                  stop spending one LLM turn per poll
  tabs            list/activate/new/close via Chromium's CDP HTTP endpoint,
                  reached with curl through /exec (curl is baked into the image)

Determinism contract: snapshot and click_element run the SAME walk, and the walk
emits elements in document order, so a ref from the last snapshot re-derives to
the same element unless the page itself changed — in which case click_element
refuses and returns a fresh snapshot instead of clicking the wrong thing.
"""
import json
import posixpath
import re
import shlex
import time
from urllib.parse import quote

from config import DESK_W as SCREEN_W, DESK_H as SCREEN_H
from errors import ApiError
import base64

from deskclient import desk_bytes, desk_json, eval_js, page_text

MAX_ELS = 150

# The shared walk. Defines __els = [{el, tag, type, name, value, href, where, …}]
# in document order. Pierces open shadow roots and same-origin iframes; includes
# cursor:pointer / onclick widgets; drops occluded nodes and children whose bbox
# sits inside an already-emitted hit target. Password/OTP values are never read.
_WALK = """
const __secretish=el=>el.type==='password'
  ||['one-time-code','current-password','new-password'].includes((el.getAttribute&&el.getAttribute('autocomplete'))||'');
const __sel='a[href],button,input,select,textarea,summary,label,'+
 '[role="button"],[role="link"],[role="tab"],[role="menuitem"],[role="checkbox"],'+
 '[role="radio"],[role="combobox"],[role="option"],[role="switch"],[role="searchbox"],'+
 '[role="textbox"],[onclick],[tabindex],[contenteditable="true"]';
const __seen=new Set(),__els=[],__boxes=[];
const __clickable=el=>{
  try{if(el.matches&&el.matches(__sel))return true;}catch(e){}
  if(el.onclick)return true;
  try{if(getComputedStyle(el).cursor==='pointer')return true;}catch(e){}
  return false;
};
const __vis=el=>{
  if(el.disabled)return false;
  try{if(el.closest&&el.closest('[aria-hidden="true"]'))return false;}catch(e){}
  const r=el.getBoundingClientRect();
  if(r.width<2||r.height<2)return false;
  if(typeof el.checkVisibility==='function'&&!el.checkVisibility())return false;
  return true;
};
const __occluded=el=>{
  const r=el.getBoundingClientRect();
  const x=r.left+r.width/2,y=r.top+r.height/2;
  const root=el.getRootNode();
  let hit=null;
  try{hit=root.elementFromPoint?root.elementFromPoint(x,y):document.elementFromPoint(x,y);}catch(e){return false;}
  if(!hit)return false;
  return hit!==el&&!el.contains(hit)&&!hit.contains(el);
};
const __contained=r=>__boxes.some(b=>r.left>=b.left&&r.right<=b.right&&r.top>=b.top&&r.bottom<=b.bottom);
const __topRect=el=>{
  let r=el.getBoundingClientRect(),x=r.left,y=r.top;
  let w=el.ownerDocument&&el.ownerDocument.defaultView;
  while(w&&w.frameElement){
    const fr=w.frameElement.getBoundingClientRect();
    x+=fr.left;y+=fr.top;
    w=w.frameElement.ownerDocument&&w.frameElement.ownerDocument.defaultView;
  }
  return {x,y,w:r.width,h:r.height};
};
const __push=(el,where)=>{
  if(__seen.has(el)||!__clickable(el)||!__vis(el)||__occluded(el))return;
  const r=el.getBoundingClientRect();
  if(__contained(r))return;
  __seen.add(el);
  __boxes.push({left:r.left,right:r.right,top:r.top,bottom:r.bottom});
  const tag=el.tagName.toLowerCase();
  const name=(el.getAttribute('aria-label')||el.placeholder||
    ((tag==='input'&&(el.type==='submit'||el.type==='button'))?el.value:'')||
    el.innerText||el.title||el.alt||'').trim().replace(/\\s+/g,' ').slice(0,80);
  const tr=__topRect(el);
  const bx=(window.outerWidth-window.innerWidth)/2;
  __els.push({el,tag,type:(el.type||el.getAttribute('role')||''),name,
    value:('value'in el&&!__secretish(el)&&tag!=='button')?String(el.value).slice(0,40):'',
    href:tag==='a'?String(el.getAttribute('href')||'').slice(0,120):'',
    where:where||'',vx:tr.x,vy:tr.y,vw:tr.w,vh:tr.h,
    sx:Math.round(window.screenX+bx+tr.x),
    sy:Math.round(window.screenY+(window.outerHeight-window.innerHeight)-bx+tr.y)});
};
const __walk=(root,where)=>{
  let nodes=[];
  try{nodes=root.querySelectorAll('*');}catch(e){return;}
  for(const el of nodes){
    __push(el,where);
    if(el.shadowRoot)__walk(el.shadowRoot,'shadow');
    if((el.tagName||'')==='IFRAME'){
      try{if(el.contentDocument)__walk(el.contentDocument,'iframe');}catch(e){}
    }
  }
};
__walk(document,'');
"""


def _iife(body):
    return "(()=>{" + _WALK + body + "})()"


def _fmt(i, e, star=False):
    """One element, one compact line: [12] button 'Save changes'."""
    t = e["tag"] + ("(" + e["type"] + ")" if e["type"] and e["type"] != e["tag"] else "")
    mark = f"|{e['where']}| " if e.get("where") else ""
    pref = "*" if star else ""
    line = f'{pref}[{i}] {mark}{t} "{e["name"]}"'
    if e.get("value"):
        line += f' ={e["value"]!r}'
    if e.get("href"):
        line += f' -> {e["href"]}'
    return line


def snapshot(row, timeout_s=15):
    """Numbered visible interactive elements of the active tab, document order."""
    body = """
const keys=__els.map(e=>e.tag+'\\0'+e.name+'\\0'+(e.href||''));
const prev=(window.__caseEls&&window.__caseEls.keys)||[];
window.__caseEls={els:__els.map(e=>e.el),keys:keys};
return {url:location.href,title:document.title,count:__els.length,
  els:__els.slice(0,%d).map(({el,...r})=>r),keys:keys.slice(0,%d),prev:prev.slice(0,%d)};
""" % (MAX_ELS, MAX_ELS, MAX_ELS)
    r = eval_js(row, _iife(body), timeout_s)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "snapshot failed"}
    v = r.get("value")
    if not isinstance(v, dict):   # >64KB truncates to a string; should not happen at 150 els
        return {"ok": False, "error": "snapshot too large — page has an extreme DOM"}
    els = v.get("els") or []
    keys = v.get("keys") or []
    prev = set(v.get("prev") or [])
    starred = bool(prev)
    return {"ok": True, "url": v.get("url"), "title": v.get("title"),
            "count": v.get("count", len(els)),
            "truncated": (v.get("count", 0) > MAX_ELS),
            "elements": [_fmt(i, e, star=starred and i < len(keys) and keys[i] not in prev)
                         for i, e in enumerate(els)]}


def _locate(row, ref, name, timeout_s=15):
    """Re-derive the walk, verify ref+name, scroll into view, return screen coords."""
    checks = f"const __i={int(ref)};const __n={json.dumps(name) if name else 'null'};"
    body = checks + """
const __nameOk=(nm)=>__n===null||nm===__n||(nm&&nm.includes(__n));
const __coords=(el,nm,tag,healed,oldI,newI)=>{
  el.scrollIntoView({block:'center',inline:'center'});
  const tr=__topRect(el);
  const bx=(window.outerWidth-window.innerWidth)/2;
  return {ok:true,name:nm,tag:tag,type:(el.type||''),healed:!!healed,old_ref:oldI,new_ref:newI,
    x:Math.round(window.screenX+bx+tr.x+tr.w/2),
    y:Math.round(window.screenY+(window.outerHeight-window.innerHeight)-bx+tr.y+tr.h/2)};
};
const stored=window.__caseEls&&window.__caseEls.els&&window.__caseEls.els[__i];
if(stored&&stored.isConnected){
  const tag=stored.tagName.toLowerCase();
  const nm=(stored.getAttribute('aria-label')||stored.placeholder||stored.innerText||'').trim().replace(/\\s+/g,' ').slice(0,80);
  if(__nameOk(nm))return __coords(stored,nm,tag,false,__i,__i);
}
if(__i>=0&&__i<__els.length){
  const e=__els[__i];
  if(__nameOk(e.name))return __coords(e.el,e.name,e.tag,false,__i,__i);
  if(__n!==null){
    const hits=__els.map((x,i)=>({x,i})).filter(h=>h.x.name===__n||h.x.name.includes(__n));
    if(hits.length===1)return __coords(hits[0].x.el,hits[0].x.name,hits[0].x.tag,true,__i,hits[0].i);
    return {ok:false,stale:true,found:e.name,count:__els.length};
  }
}
if(__n!==null){
  const hits=__els.map((x,i)=>({x,i})).filter(h=>h.x.name===__n||h.x.name.includes(__n));
  if(hits.length===1)return __coords(hits[0].x.el,hits[0].x.name,hits[0].x.tag,true,__i,hits[0].i);
}
return {ok:false,stale:true,count:__els.length};
"""
    r = eval_js(row, _iife(body), timeout_s)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "locate failed"}
    return r.get("value") if isinstance(r.get("value"), dict) else {"ok": False, "error": "bad locate result"}


# Same sentinel as deskclient.navigate: a click that navigates leaves the old
# document reporting readyState complete for a beat. Stamp first so a document
# without the stamp is the one the action produced.
_STAMP = "window.__case_act"


def _stamp(row):
    try:
        return bool(eval_js(row, f"{_STAMP}=1", 3).get("ok"))
    except ApiError:
        return False


def _settled_snapshot(row, stamped, settle_s=1.0, budget_s=8.0):
    """The page as it stands once the action stops moving it."""
    if not stamped:
        time.sleep(settle_s)
        deadline = time.time() + budget_s
        while time.time() < deadline:
            time.sleep(0.25)
            try:
                r = eval_js(row, "document.readyState", 3)
            except ApiError as e:
                if e.status != 502:
                    return None
                continue
            if r.get("value") == "complete":
                break
        try:
            return snapshot(row)
        except ApiError:
            return None
    deadline = time.time() + budget_s
    grace = time.time() + settle_s
    while time.time() < deadline:
        time.sleep(0.25)
        try:
            r = eval_js(row, f"[{_STAMP}===undefined,document.readyState]", 3)
        except ApiError as e:
            if e.status != 502:
                return None
            continue
        v = r.get("value")
        if not isinstance(v, list):
            break
        navigated, ready = v[0], v[1]
        if navigated and ready == "complete":
            return snapshot(row)
        if not navigated and time.time() >= grace:
            fresh = snapshot(row)
            try:
                eval_js(row, f"delete {_STAMP}", 3)
            except ApiError:
                pass
            return fresh
    try:
        return snapshot(row)
    except ApiError:
        return None


def _attach_snapshot(row, res, want, stamped):
    if not want:
        return res
    fresh = _settled_snapshot(row, stamped)
    if fresh and fresh.get("ok"):
        res["snapshot"] = fresh
    return res


def click_element(row, ref, name=None, text=None, screenshot=False, snapshot_after=True):
    """Verify + scroll + real OS click, then hand back the page it produced. On stale
    ref: refuse and hand back a fresh snapshot (a wrong click is worse than a slow
    click). text, when given, is typed after the click (the click focuses the field)."""
    loc = _locate(row, ref, name)
    if not loc.get("ok"):
        if loc.get("stale"):
            fresh = snapshot(row)
            return {"ok": False, "stale": True,
                    "error": ("element list changed"
                              + (f" — [{ref}] is now \"{loc['found']}\"" if loc.get("found") else "")),
                    "snapshot": fresh}
        return loc
    x, y = loc["x"], loc["y"]
    if not (0 <= x < SCREEN_W and 0 <= y < SCREEN_H):
        return {"ok": False, "error": f"element resolves off-screen ({x},{y})"}
    before = _page_ids(row) if loc.get("tag") in ("a", "button") and text is None else []
    stamped = snapshot_after and _stamp(row)
    out = desk_json(row, "POST", "/action",
                    json={"type": "click", "x": x, "y": y,
                          "screenshot": bool(screenshot) and not text},
                    timeout=30)
    if text is not None:
        out = desk_json(row, "POST", "/action",
                        json={"type": "type", "text": str(text),
                              "screenshot": bool(screenshot)},
                        timeout=30)
    res = {"ok": True, "clicked": loc.get("name"), "tag": loc.get("tag"), "x": x, "y": y}
    if loc.get("healed"):
        res["healed"] = True
        res["old_ref"] = loc.get("old_ref")
        res["new_ref"] = loc.get("new_ref")
    if before:
        switched = _activate_new_tab(row, before)
        if switched:
            res["switched_tab"] = switched
    if isinstance(out, dict) and out.get("screenshot_png_b64"):
        res["screenshot_png_b64"] = out["screenshot_png_b64"]
    t = page_text(row, eval_js)
    if t:
        res["text"] = t
    return _attach_snapshot(row, res, snapshot_after, stamped)


def _page_ids(row):
    try:
        return [t["id"] for t in tabs(row).get("tabs") or [] if t.get("id")]
    except Exception:
        return []


def _activate_new_tab(row, before):
    try:
        after = tabs(row).get("tabs") or []
    except Exception:
        return None
    known = set(before)
    fresh = [t for t in after if t.get("id") and t["id"] not in known]
    if len(fresh) != 1:
        return None
    t = fresh[0]
    try:
        tabs(row, action="activate", target_id=t["id"])
    except Exception:
        return None
    return {"id": t["id"], "url": t.get("url"), "title": t.get("title")}


def overlay_marks(png, rects):
    """Draw numbered boxes on a desktop PNG. Never injects into the live DOM.
    Returns the original bytes if Pillow cannot read the image."""
    try:
        from io import BytesIO
        from PIL import Image, ImageDraw
        im = Image.open(BytesIO(png)).convert("RGB")
        dr = ImageDraw.Draw(im)
        for i, e in enumerate(rects or []):
            x, y, w, h = e.get("sx"), e.get("sy"), e.get("vw"), e.get("vh")
            if None in (x, y, w, h):
                continue
            x, y, w, h = int(x), int(y), int(w), int(h)
            dr.rectangle([x, y, x + w, y + h], outline=(220, 40, 40), width=2)
            dr.text((x + 2, max(0, y - 12)), str(i), fill=(220, 40, 40))
        buf = BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return png


def element_rects(row, timeout_s=15):
    body = ("return __els.slice(0,%d).map((e,i)=>({i:i,sx:e.sx,sy:e.sy,vw:e.vw,vh:e.vh}));"
            % MAX_ELS)
    r = eval_js(row, _iife(body), timeout_s)
    if not r.get("ok") or not isinstance(r.get("value"), list):
        return []
    return r["value"]


def hover(row, ref, name=None):
    """Move the OS pointer over [ref] without clicking."""
    loc = _locate(row, ref, name)
    if not loc.get("ok"):
        if loc.get("stale"):
            return {"ok": False, "stale": True, "error": "element list changed",
                    "snapshot": snapshot(row)}
        return loc
    x, y = loc["x"], loc["y"]
    if not (0 <= x < SCREEN_W and 0 <= y < SCREEN_H):
        return {"ok": False, "error": f"element resolves off-screen ({x},{y})"}
    desk_json(row, "POST", "/action", json={"type": "move", "x": x, "y": y}, timeout=30)
    return {"ok": True, "hovered": loc.get("name"), "tag": loc.get("tag"), "x": x, "y": y}


UPLOAD_MAX = 5 * 1024 * 1024
_UPLOAD_CHUNK = 6000


def upload(row, ref, path, name=None):
    """Assign a file already on the computer to input[type=file] [ref]."""
    p = str(path or "")
    if "\n" in p or not p.startswith("/home/agent/") or any(part == ".." for part in p.split("/")):
        raise ApiError(400, "bad_request", "path must be under /home/agent/")
    p = posixpath.normpath(p)
    if not p.startswith("/home/agent/"):
        raise ApiError(400, "bad_request", "path must be under /home/agent/")
    st = desk_json(row, "POST", "/exec",
                   json={"command": f"test -f {shlex.quote(p)} && wc -c < {shlex.quote(p)}",
                         "timeout_s": 10}, timeout=20)
    raw = (st.get("stdout") or "").strip().split()[0] if st.get("exit_code") == 0 else ""
    try:
        size = int(raw)
    except ValueError:
        raise ApiError(400, "bad_request", "file not found on the computer")
    if size > UPLOAD_MAX:
        raise ApiError(400, "bad_request", f"file larger than {UPLOAD_MAX} bytes")
    loc = _locate(row, ref, name)
    if not loc.get("ok"):
        if loc.get("stale"):
            return {"ok": False, "stale": True, "error": "element list changed",
                    "snapshot": snapshot(row)}
        return loc
    use_ref = int(loc["new_ref"]) if loc.get("new_ref") is not None else int(ref)
    if loc.get("type") != "file":
        return {"ok": False, "error": "ref is not input[type=file] — snapshot again"}
    fname = p.rsplit("/", 1)[-1]
    raw = desk_bytes(row, "GET", "/file", params={"path": p}, timeout=120)
    if len(raw) != size:
        return {"ok": False, "error": "file size changed during read"}
    data = base64.b64encode(raw).decode("ascii")
    eval_js(row, "window.__caseUp=''", 5)
    for i in range(0, len(data), _UPLOAD_CHUNK):
        eval_js(row, "window.__caseUp+=%s" % json.dumps(data[i:i + _UPLOAD_CHUNK]), 10)
    done = eval_js(row, _iife(f"""
const __i={use_ref};
const stored=window.__caseEls&&window.__caseEls.els&&window.__caseEls.els[__i];
const e=(__els[__i])||(stored?{{el:stored,type:stored.type}}:null);
if(!e||e.type!=='file')return {{ok:false,error:'not a file input'}};
const raw=atob(window.__caseUp||'');delete window.__caseUp;
if(raw.length!=={int(size)})return {{ok:false,error:'decoded length mismatch'}};
const u=new Uint8Array(raw.length);
for(let i=0;i<raw.length;i++)u[i]=raw.charCodeAt(i);
const f=new File([u],{json.dumps(fname)});
const dt=new DataTransfer();dt.items.add(f);e.el.files=dt.files;
e.el.dispatchEvent(new Event('change',{{bubbles:true}}));
return {{ok:true,name:{json.dumps(fname)},bytes:raw.length}};
"""), 15)
    val = done.get("value") if done.get("ok") else None
    return val if isinstance(val, dict) else {"ok": False, "error": "upload eval failed"}


def fill(row, fields, submit=False, timeout_s=20, snapshot_after=True):
    """Batch form fill. fields=[{ref, value}]. Native setters + input/change events
    so React/Vue see the change. Password and OTP inputs are refused inside the
    page — vaulted computer_login owns credentials, always."""
    clean = []
    for f in fields or []:
        if not isinstance(f, dict) or "ref" not in f or "value" not in f:
            raise ApiError(400, "bad_request", "each field needs {ref, value}")
        clean.append({"ref": int(f["ref"]), "value": f["value"]})
    if not clean:
        raise ApiError(400, "bad_request", "fields is empty")
    body = f"const __fields={json.dumps(clean)};const __submit={json.dumps(bool(submit))};" + """
const out=[];
for(const f of __fields){
  const e=__els[f.ref];
  if(!e){out.push({ref:f.ref,ok:false,error:'no such ref — re-snapshot'});continue;}
  const el=e.el;
  if(__secretish(el)){out.push({ref:f.ref,ok:false,error:'secret fields are vault-only (computer_login)'});continue;}
  try{
    if(el.type==='checkbox'||el.type==='radio'){
      el.checked=(f.value===true||f.value==='true'||f.value===1||f.value==='1');
    }else if(e.tag==='select'){
      const opt=[...el.options].find(o=>o.value===String(f.value)||o.text.trim()===String(f.value));
      if(!opt){out.push({ref:f.ref,ok:false,error:'no matching option'});continue;}
      el.value=opt.value;
    }else if(el.isContentEditable){
      el.focus();el.textContent=String(f.value);
    }else{
      const proto=e.tag==='textarea'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
      const d=Object.getOwnPropertyDescriptor(proto,'value');
      el.focus();
      if(d&&d.set)d.set.call(el,String(f.value));else el.value=String(f.value);
    }
    el.dispatchEvent(new Event('input',{bubbles:true}));
    el.dispatchEvent(new Event('change',{bubbles:true}));
    let actual='';
    if(el.type==='checkbox'||el.type==='radio')actual=el.checked;
    else if(el.isContentEditable)actual=el.textContent;
    else actual=el.value;
    const want=f.value;
    const match=(el.type==='checkbox'||el.type==='radio')
      ?(actual===true||actual==='true')=== (want===true||want==='true'||want===1||want==='1')
      :String(actual)===String(want);
    if(!match){out.push({ref:f.ref,ok:false,name:e.name,actual:actual,error:'page reformatted value'});continue;}
    out.push({ref:f.ref,ok:true,name:e.name,actual:actual});
  }catch(err){out.push({ref:f.ref,ok:false,error:String(err).slice(0,80)});}
}
if(__submit&&out.length&&out.every(o=>o.ok)){
  const first=__els[__fields[0].ref];
  const form=first&&first.el.form;
  if(form){form.requestSubmit?form.requestSubmit():form.submit();}
  else out.push({ref:__fields[0].ref,ok:false,error:'submit requested but field has no form'});
}
return {ok:out.every(o=>o.ok),fields:out};
"""
    want = bool(snapshot_after and submit)
    stamped = want and _stamp(row)
    r = eval_js(row, _iife(body), timeout_s)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "fill failed"}
    out = r.get("value") if isinstance(r.get("value"), dict) else {"ok": False, "error": "bad fill result"}
    return _attach_snapshot(row, out, want and out.get("ok"), stamped)


def wait_for(row, selector=None, text=None, gone=False, network_idle=False, timeout_s=30):
    """Block server-side until the condition holds. One MCP call instead of one
    LLM turn per poll — the same trade navigate() already makes."""
    if not (selector or text or network_idle):
        raise ApiError(400, "bad_request", "need selector, text or network_idle")
    if selector:
        cond = f"!!document.querySelector({json.dumps(selector)})"
    elif text:
        cond = f"(document.body?document.body.innerText:'').includes({json.dumps(text)})"
    else:
        cond = "performance.getEntriesByType('resource').length"
    if gone and not network_idle:
        cond = "!(" + cond + ")"
    t0 = time.time()
    deadline = t0 + max(1, min(int(timeout_s), 120))
    last_count = -1
    stable = 0
    while time.time() < deadline:
        try:
            r = eval_js(row, cond, 5)
        except ApiError as e:
            if e.status != 502:   # asleep/injecting/wedged — surface, don't wait out
                raise
            time.sleep(0.4)
            continue
        v = r.get("value")
        if network_idle:
            if v == last_count and r.get("ok"):
                stable += 1
                if stable >= 2:   # ~0.8s with no new resources
                    return {"ok": True, "waited_ms": int((time.time() - t0) * 1000)}
            else:
                stable = 0
            last_count = v
        elif r.get("ok") and v:
            return {"ok": True, "waited_ms": int((time.time() - t0) * 1000)}
        time.sleep(0.4)
    what = selector or text or "network idle"
    return {"ok": False, "error": f"'{what}' not {'gone' if gone else 'satisfied'} within {timeout_s}s",
            "waited_ms": int((time.time() - t0) * 1000)}


# ---------- teach-a-task recorder ----------
# One stateless tick: install capture-phase listeners into the page (idempotent),
# drain the event buffer, report href + password-field presence. REDACTION HAPPENS
# IN-PAGE at capture time: password/OTP fields, and every field of any form that
# contains a password field, record {k:'secret'} with no name and no value — the
# human may log in mid-demo and their keystrokes must never leave the page. During
# vault credential injection deskd 423s /eval, so ticks are structurally blind
# then, same discipline as screenshots. The caller (UI/agent) accumulates ticks
# and diffs href for navigation events; cased holds no recorder state.
_TEACH_JS = """
if(!window.__caseTeach){
  window.__caseTeach={q:[]};
  const nm=el=>{try{return ((el.getAttribute&&(el.getAttribute('aria-label')||el.placeholder||''))
    ||el.innerText||el.value||el.title||'').trim().replace(/\\s+/g,' ').slice(0,80);}catch(e){return '';}};
  const push=e=>{if(window.__caseTeach.q.length<300)window.__caseTeach.q.push(e);};
  const secretish=el=>el.type==='password'
    ||['one-time-code','current-password','new-password'].includes(el.getAttribute&&el.getAttribute('autocomplete'))
    ||!!(el.form&&el.form.querySelector('input[type=password]'));
  addEventListener('click',ev=>{
    const el=(ev.target.closest&&ev.target.closest('a,button,input,select,textarea,summary,label,[role],[onclick]'))||ev.target;
    push({k:'click',tag:(el.tagName||'').toLowerCase(),name:nm(el)});
  },true);
  addEventListener('change',ev=>{
    const el=ev.target;
    if(secretish(el)){push({k:'secret'});return;}
    push({k:'input',tag:(el.tagName||'').toLowerCase(),type:el.type||'',name:nm(el),
      value:String(el.value===undefined?'':el.value).slice(0,80)});
  },true);
  addEventListener('submit',()=>push({k:'submit'}),true);
}
const q=window.__caseTeach.q.splice(0);
return {href:location.href,pw:!!document.querySelector('input[type=password]'),events:q};
"""


def teach_tick(row):
    """Install-if-absent + drain the in-page demo recorder. 502 (nav churn) is a
    quiet empty tick; 423 (credential injection) likewise — the gap is the point."""
    try:
        r = eval_js(row, "(()=>{" + _TEACH_JS + "})()", 8)
    except ApiError as e:
        if e.status in (502, 423):
            return {"ok": True, "href": "", "pw": False, "events": [], "gap": e.status}
        raise
    v = r.get("value")
    if not r.get("ok") or not isinstance(v, dict):
        return {"ok": True, "href": "", "pw": False, "events": [], "gap": "eval"}
    return {"ok": True, "href": v.get("href", ""), "pw": bool(v.get("pw")),
            "events": v.get("events") or []}


_TARGET_RE = re.compile(r"^[A-Za-z0-9-]{4,64}$")
_CDP = "http://127.0.0.1:9222"


def _cdp_curl(row, path, method="GET", timeout_s=10):
    cmd = f"curl -s -m 8 {'-X PUT ' if method == 'PUT' else ''}{shlex.quote(_CDP + path)}"
    out = desk_json(row, "POST", "/exec",
                    json={"command": cmd, "timeout_s": timeout_s}, timeout=timeout_s + 15)
    return out.get("stdout", "")


def tabs(row, action="list", target_id=None, url=None):
    """Tab management via Chromium's CDP HTTP endpoint (container loopback :9222),
    reached with curl through the existing /exec route. eval/capture bind to the
    ACTIVE tab, so after activate/new the next eval talks to that tab."""
    if action in ("activate", "close"):
        if not target_id or not _TARGET_RE.match(str(target_id)):
            raise ApiError(400, "bad_request", "need a valid target_id from tabs list")
        _cdp_curl(row, f"/json/{action}/{target_id}")
        return {"ok": True, "action": action, "target_id": target_id, "tabs": tabs(row)["tabs"]}
    if action == "new":
        if not url or not re.match(r"^https?://", str(url)):
            raise ApiError(400, "bad_request", "need an http(s) url")
        raw = _cdp_curl(row, "/json/new?" + quote(str(url), safe=""), method="PUT")
        made = None
        try:
            made = json.loads(raw)
        except ValueError:
            pass
        return {"ok": bool(made), "action": "new",
                "target_id": (made or {}).get("id"), "tabs": tabs(row)["tabs"]}
    # list
    raw = _cdp_curl(row, "/json/list")
    try:
        pages = [t for t in json.loads(raw) if t.get("type") == "page"]
    except ValueError:
        raise ApiError(502, "cdp_unreachable", "chromium CDP endpoint did not answer")
    return {"ok": True, "tabs": [
        {"id": t.get("id"), "title": (t.get("title") or "")[:80],
         "url": (t.get("url") or "")[:200], "active": i == 0}
        for i, t in enumerate(pages)]}
