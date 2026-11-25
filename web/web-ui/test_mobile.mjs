import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const page=fs.readFileSync(new URL('./index.html',import.meta.url),'utf8');
const between=(start,end)=>page.slice(page.indexOf(start),page.indexOf(end,page.indexOf(start)));
const frame={style:{},contentWindow:{}};
const settings=[];
const context=vm.createContext({
  mobile:true,takeover:false,frame,
  MOBILE:()=>context.mobile,
  $:()=>frame,
  fetch:async()=>({}),
  Date,
});
vm.runInContext(between('function applyViewOnly(){',"addEventListener('case-desk-ready'"),context);
context.applyViewOnly();
assert.equal(frame.inert,true,'block input even before noVNC loads');
assert.equal(frame.style.pointerEvents,'none');
assert.equal(frame.tabIndex,-1);
frame.contentWindow.UI={updateSetting:(...v)=>settings.push(v),rfb:{viewOnly:false}};
context.applyViewOnly();
assert.equal(frame.contentWindow.UI.rfb.viewOnly,true);
assert.deepEqual(settings.at(-1),['view_only',true]);
context.takeover=true;
context.applyViewOnly();
assert.equal(frame.inert,false);
assert.equal(frame.style.pointerEvents,'');
assert.equal(frame.contentWindow.UI.rfb.viewOnly,false);
context.takeover=false;
context.applyViewOnly();
assert.equal(frame.contentWindow.UI.rfb.viewOnly,true,'return to view-only');
context.mobile=false;
context.applyViewOnly();
assert.equal(frame.inert,false,'desktop remains interactive after resizing');
assert.equal(frame.contentWindow.UI.rfb.viewOnly,false);

const pending=new Map();
const inner={innerHTML:'',appendChild(){}};
const chat=vm.createContext({
  activeTid:'',loadingTid:'',threadGen:0,promptQ:[],inner,log:{scrollTop:0,scrollHeight:100},
  THREAD_KEY:'case.drive.thread',
  closeNav(){chat.closed=true;},MOBILE:()=>true,
  goTab:n=>{chat.tab=n;},paint(){},esc:s=>s,md:s=>s,shotFig:()=>({}),
  orphanQueued(){},holdQueued(){},drainQ(){},queuedThread:()=>'',
  fetch:url=>new Promise(resolve=>pending.set(new URL(url,'http://test').searchParams.get('id'),resolve)),
});
vm.runInContext(between('async function openThread(tid,opts){','let followTid'),chat);
const first=chat.openThread('first');
assert.equal(chat.closed,true);
assert.equal(chat.tab,'chat');
const second=chat.openThread('second');
pending.get('second')({ok:true,json:async()=>({turns:[]})});
await second;
inner.innerHTML='second conversation';
pending.get('first')({ok:false,json:async()=>({error:'old request failed'})});
await first;
assert.equal(inner.innerHTML,'second conversation','late responses cannot replace selected chat');
chat.closed=false;chat.tab='more';
await chat.openThread('second');
assert.equal(chat.closed,true,'reselecting active chat still closes drawer');
assert.equal(chat.tab,'chat');
chat.closed=false;chat.tab='screen';
await chat.openThread('second',{stay:true});
assert.equal(chat.tab,'screen','resume must not steal the screen tab');
console.log('mobile interaction regressions: all checks pass');
