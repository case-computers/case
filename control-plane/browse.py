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
import re
import shlex
import time

from errors import ApiError
from deskclient import desk_json, eval_js

MAX_ELS = 150
SCREEN_W, SCREEN_H = 1280, 800

# The shared walk. Defines __els = [{el, tag, type, name, value, href}] in
# document order, visible interactive elements only. Password values never read.
_WALK = """
const __sel='a[href],button,input,select,textarea,summary,label,'+
 '[role="button"],[role="link"],[role="tab"],[role="menuitem"],[role="checkbox"],'+
 '[role="radio"],[role="combobox"],[role="option"],[role="switch"],[role="searchbox"],'+
 '[role="textbox"],[onclick],[tabindex],[contenteditable="true"]';
const __seen=new Set(),__els=[];
for(const el of document.querySelectorAll(__sel)){
  if(__seen.has(el))continue;__seen.add(el);
  if(el.closest('[aria-hidden="true"]'))continue;
  if(el.disabled)continue;
  const r=el.getBoundingClientRect();
  if(r.width<2||r.height<2)continue;
  if(typeof el.checkVisibility==='function'&&!el.checkVisibility())continue;
  const tag=el.tagName.toLowerCase();
  const name=(el.getAttribute('aria-label')||el.placeholder||
    ((tag==='input'&&(el.type==='submit'||el.type==='button'))?el.value:'')||
    el.innerText||el.title||el.alt||'').trim().replace(/\\s+/g,' ').slice(0,80);
  __els.push({el,tag,type:(el.type||el.getAttribute('role')||''),name,
    value:('value'in el&&el.type!=='password'&&tag!=='button')?String(el.value).slice(0,40):'',
    href:tag==='a'?String(el.getAttribute('href')||'').slice(0,120):''});
}
"""


def _iife(body):
    return "(()=>{" + _WALK + body + "})()"


def _fmt(i, e):
    """One element, one compact line: [12] button 'Save changes'."""
    t = e["tag"] + ("(" + e["type"] + ")" if e["type"] and e["type"] != e["tag"] else "")
    line = f'[{i}] {t} "{e["name"]}"'
    if e.get("value"):
        line += f' ={e["value"]!r}'
    if e.get("href"):
        line += f' -> {e["href"]}'
    return line


def snapshot(row, timeout_s=15):
    """Numbered visible interactive elements of the active tab, document order."""
    body = ("return {url:location.href,title:document.title,count:__els.length,"
            "els:__els.slice(0,%d).map(({el,...r})=>r)};" % MAX_ELS)
    r = eval_js(row, _iife(body), timeout_s)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "snapshot failed"}
    v = r.get("value")
    if not isinstance(v, dict):   # >64KB truncates to a string; should not happen at 150 els
        return {"ok": False, "error": "snapshot too large — page has an extreme DOM"}
    els = v.get("els") or []
    return {"ok": True, "url": v.get("url"), "title": v.get("title"),
            "count": v.get("count", len(els)),
            "truncated": (v.get("count", 0) > MAX_ELS),
            "elements": [_fmt(i, e) for i, e in enumerate(els)]}


def _locate(row, ref, name, timeout_s=15):
    """Re-derive the walk, verify ref+name, scroll into view, return screen coords."""
    checks = f"const __i={int(ref)};const __n={json.dumps(name) if name else 'null'};"
    body = checks + """
if(__i<0||__i>=__els.length)return {ok:false,stale:true,count:__els.length};
const e=__els[__i];
if(__n!==null&&e.name!==__n&&!e.name.includes(__n))
  return {ok:false,stale:true,found:e.name,count:__els.length};
e.el.scrollIntoView({block:'center',inline:'center'});
const r=e.el.getBoundingClientRect();
const bx=(window.outerWidth-window.innerWidth)/2;
return {ok:true,name:e.name,tag:e.tag,
  x:Math.round(window.screenX+bx+r.left+r.width/2),
  y:Math.round(window.screenY+(window.outerHeight-window.innerHeight)-bx+r.top+r.height/2)};
"""
    r = eval_js(row, _iife(body), timeout_s)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "locate failed"}
    return r.get("value") if isinstance(r.get("value"), dict) else {"ok": False, "error": "bad locate result"}


def click_element(row, ref, name=None, text=None, screenshot=False):
    """Verify + scroll + real OS click. On stale ref: refuse and hand back a fresh
    snapshot (a wrong click is worse than a slow click). text, when given, is typed
    after the click (the click focuses the field)."""
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
    if isinstance(out, dict) and out.get("screenshot_png_b64"):
        res["screenshot_png_b64"] = out["screenshot_png_b64"]
    return res


def fill(row, fields, submit=False, timeout_s=20):
    """Batch form fill. fields=[{ref, value}]. Native setters + input/change events
    so React/Vue see the change. Password inputs are refused inside the page —
    vaulted computer_login owns credentials, always."""
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
  if(el.type==='password'){out.push({ref:f.ref,ok:false,error:'password fields are vault-only (computer_login)'});continue;}
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
    out.push({ref:f.ref,ok:true,name:e.name});
  }catch(err){out.push({ref:f.ref,ok:false,error:String(err).slice(0,80)});}
}
if(__submit&&out.some(o=>o.ok)){
  const first=__els[__fields[0].ref];
  const form=first&&first.el.form;
  if(form){form.requestSubmit?form.requestSubmit():form.submit();}
  else out.push({ref:__fields[0].ref,ok:false,error:'submit requested but field has no form'});
}
return {ok:out.every(o=>o.ok),fields:out};
"""
    r = eval_js(row, _iife(body), timeout_s)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "fill failed"}
    return r.get("value") if isinstance(r.get("value"), dict) else {"ok": False, "error": "bad fill result"}


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
        raw = _cdp_curl(row, "/json/new?" + url, method="PUT")
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
