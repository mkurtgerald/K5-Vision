/* Port-free served-script state tests; no native or browser qualification. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '../..');
const script = fs.readFileSync(path.join(root, 'src/k5vision/operator_web/app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'src/k5vision/operator_web/index.html'), 'utf8');
const turn = () => new Promise(resolve => setImmediate(resolve));
const ticket = 'a'.repeat(32);
const reply = (data, status = 200) => {
  if(data?.status !== 'ready') return {ok:status < 400,status,headers:{get:()=>null},json:async()=>data};
  const fields={'content-type':'application/octet-stream','content-length':String(data.pixels.length),
    'x-k5-sample-width':String(data.width),'x-k5-sample-height':String(data.height),
    'x-k5-sample-stride':String(data.stride_bytes),'x-k5-sample-format':data.pixel_format,
    'x-k5-sample-boxes':String(data.rendered_boxes),'x-k5-sample-received':data.received_at,
    'x-k5-sample-expires':String(data.expires_in_seconds),'x-k5-sample-ticket':ticket};
  let sent=false;
  return {ok:true,status,headers:{get:name=>fields[name]||null},body:{getReader:()=>({
    async read(){if(sent)return {done:true};sent=true;return {done:false,value:data.pixels};},
    async cancel(){},releaseLock(){}})}};
};
const ready = () => ({status:'ready',expires_in_seconds:40,width:1,height:1,stride_bytes:8,
  pixels:new Uint8Array([10,20,30,0,90,90,90,90]),pixel_format:'BGRx',
  rendered_boxes:0,received_at:'2026-10-12T12:00:00+00:00'});
function harness() {
  const elements = new Map(), timers = new Map(), requests = [], pending = [];
  let next = 0, painted = null, now = 0;
  function element() {return {value:'',textContent:'',hidden:false,disabled:false,
    classList:{toggle(){},add(){},remove(){}},addEventListener(){},setAttribute(){},removeAttribute(){},
    pause(){},load(){},close(){},reset(){},replaceChildren(){},append(){},
    getContext(){return {createImageData(w,h){return {data:new Uint8ClampedArray(w*h*4)};},
      putImageData(image){painted = Array.from(image.data);}}}};}
  for(const match of html.matchAll(/\bid="([^"]+)"/g)) elements.set(match[1], element());
  const get = id => { assert.ok(elements.has(id),id); return elements.get(id); };
  const context = {document:{getElementById:get,createElement:element,createDocumentFragment:element},
    window:{addEventListener(){}},location:{origin:'http://127.0.0.1:8080'},performance:{now:()=>now},
    URL,AbortController,AbortSignal,Uint8ClampedArray,Uint8Array,
    setTimeout(fn,ms){const id=++next;timers.set(id,{fn,ms});return id;},clearTimeout(id){timers.delete(id);},
    fetch(url,options){requests.push({url,options});return new Promise(resolve => pending.push(resolve));}};
  vm.runInNewContext(script.replace(/\}\)\(\);\s*$/, 'globalThis.testing={state,requestSample,clearSample,paintSample,clearWorkspace,setView};})();'),context);
  const api = context.testing;
  Object.assign(api.state,{token:'private-session',account:{},selected:'camera',
    cameras:[{device_id:'camera',name:'Camera',analytics_status:'sample_available',analytics_generation:1}]});
  return {api,get,timers,requests,pending,painted:()=>painted,advance:ms=>{now+=ms;}};
}

test('uses server BGRx stride and opaque alpha on a separate canvas',()=>{
  const h=harness();h.api.paintSample(ready());
  assert.deepEqual(h.painted(),[30,20,10,255]);
  assert.equal(h.get('sample-canvas').hidden,false);
  assert.match(h.get('sample-message').textContent,/Server received/);
  assert.equal(h.get('live-video').width,undefined);
});

for(const bad of [{width:0},{width:16385},{stride_bytes:3},{height:10000000},
  {pixels:new Uint8Array([0])},{pixel_format:'RGBA'},{rendered_boxes:129},{received_at:'invalid'}]) {
  test(`rejects malformed bounded frame ${JSON.stringify(bad)}`,()=>{
    const h=harness();assert.throws(()=>h.api.paintSample({...ready(),...bad}));
    assert.equal(h.painted(),null);
  });
}

test('one explicit POST, no pixels or URL supplied, repeated clicks do not queue',async()=>{
  const h=harness();const run=h.api.requestSample();void h.api.requestSample();
  assert.equal(h.requests.length,1);assert.equal(h.requests[0].options.method,'POST');
  assert.equal(h.requests[0].options.body,undefined);
  h.pending.shift()(reply({ticket,status:'pending',expires_in_seconds:60},202));await turn();
  assert.equal(h.requests.length,2);assert.ok(h.requests[1].url.endsWith('/'+ticket));
  h.pending.shift()(reply(ready()));await run;
  assert.deepEqual(h.painted(),[30,20,10,255]);
});

for(const action of ['clear','logout','selection','source','history']) {
  test(`${action} prevents a late sample from painting`,async()=>{
    const h=harness();const run=h.api.requestSample();
    h.pending.shift()(reply({ticket,status:'pending',expires_in_seconds:60},202));await turn();
    if(action==='logout')h.api.clearWorkspace();
    else if(action==='selection'){h.api.state.selectionEpoch++;h.api.clearSample();}
    else if(action==='history')h.api.setView('history');
    else h.api.clearSample();
    h.pending.shift()(reply(ready()));await run;
    assert.equal(h.painted(),null);assert.equal(h.get('sample-canvas').width,0);
  });
}

test('expiry clears retained pixels without automatically requesting another sample',async()=>{
  const h=harness();const run=h.api.requestSample();
  h.pending.shift()(reply({ticket,status:'pending',expires_in_seconds:60},202));await turn();
  h.pending.shift()(reply(ready()));await run;
  const timer=[...h.timers.values()].find(t=>t.ms===40000);assert.ok(timer);timer.fn();
  assert.equal(h.get('sample-canvas').hidden,true);assert.equal(h.get('sample-canvas').width,0);
  assert.equal(h.requests.filter(r=>r.options.method==='POST').length,1);
});

test('pending poll stays bounded and errors never retry POST',async()=>{
  const h=harness();const run=h.api.requestSample();
  h.pending.shift()(reply({ticket,status:'pending',expires_in_seconds:60},202));await turn();
  h.pending.shift()(reply({status:'pending',expires_in_seconds:55}));await run;
  const timer=[...h.timers.values()].find(t=>t.ms===1000);assert.ok(timer);timer.fn();await turn();
  h.pending.shift()(reply({},503));await turn();
  assert.equal(h.get('sample-canvas').hidden,true);
  assert.equal(h.requests.filter(r=>r.options.method==='POST').length,1);
  assert.equal(h.timers.size,0);
});

for(const kind of ['oversize-header','wrong-ticket','truncated-body','extra-body','bad-integer']) {
  test(`binary sample refuses ${kind} before painting`,async()=>{
    const h=harness();const run=h.api.requestSample();
    h.pending.shift()(reply({ticket,status:'pending',expires_in_seconds:60},202));await turn();
    const data=ready(), response=reply(data), previous=response.headers.get;
    if(kind==='oversize-header')response.headers.get=name=>name==='content-length'?'16777217':previous(name);
    if(kind==='wrong-ticket')response.headers.get=name=>name==='x-k5-sample-ticket'?'b'.repeat(32):previous(name);
    if(kind==='bad-integer')response.headers.get=name=>name==='x-k5-sample-width'?'1.0':previous(name);
    if(kind==='truncated-body')data.pixels=new Uint8Array(4);
    if(kind==='extra-body')data.pixels=new Uint8Array(12);
    h.pending.shift()(response);await run;
    assert.equal(h.painted(),null);assert.equal(h.get('sample-canvas').hidden,true);
  });
}

test('already-consumed sample never triggers another capture',async()=>{
  const h=harness();const run=h.api.requestSample();
  h.pending.shift()(reply({ticket,status:'pending',expires_in_seconds:60},202));await turn();
  h.pending.shift()(reply({status:'consumed',expires_in_seconds:40}));await run;
  assert.equal(h.painted(),null);assert.equal(h.requests.filter(r=>r.options.method==='POST').length,1);
  assert.equal(h.timers.size,0);
});

test('delayed POST cannot extend the original monotonic click deadline',async()=>{
  const h=harness();const run=h.api.requestSample();
  assert.ok([...h.timers.values()].some(t=>t.ms===60000));
  h.advance(20000);
  h.pending.shift()(reply({ticket,status:'pending',expires_in_seconds:60},202));await turn();
  assert.ok([...h.timers.values()].some(t=>t.ms===40000));
  const data=ready();data.expires_in_seconds=55;
  h.advance(15000);h.pending.shift()(reply(data));await run;
  assert.ok([...h.timers.values()].some(t=>t.ms===25000));
  assert.deepEqual(h.painted(),[30,20,10,255]);
});

test('binary body completing past original deadline never paints',async()=>{
  const h=harness();const run=h.api.requestSample();
  h.advance(10000);h.pending.shift()(reply({ticket,status:'pending',expires_in_seconds:60},202));await turn();
  const response=reply(ready()), previous=response.body.getReader;
  response.body.getReader=()=>{
    const reader=previous(), read=reader.read;
    reader.read=async()=>{h.advance(30000);return read();};
    return reader;
  };
  h.pending.shift()(response);await run;
  assert.equal(h.painted(),null);assert.equal(h.get('sample-canvas').hidden,true);
  assert.equal(h.timers.size,0);
});
