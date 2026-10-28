const test=require('node:test');
const assert=require('node:assert/strict');
const world=require('./world3d.js');

class Surface{
  constructor(){this.listeners=new Map();}
  addEventListener(type,fn){if(!this.listeners.has(type))this.listeners.set(type,new Set());this.listeners.get(type).add(fn);}
  removeEventListener(type,fn){this.listeners.get(type)?.delete(fn);}
  emit(type,event={}){for(const fn of this.listeners.get(type)||[])fn(event);}
  get listenerCount(){return [...this.listeners.values()].reduce((n,set)=>n+set.size,0);}
}
function harness(options={}){
  let serial=0,disconnected=0,intersectionCallback=null,intersectionDisconnected=0;
  const live={buffers:new Set(),arrays:new Set(),programs:new Set(),shaders:new Set()},made={buffers:0,arrays:0,programs:0,shaders:0};
  const create=type=>{const id={type,id:++serial};live[type].add(id);made[type]++;return id;},remove=(type,id)=>live[type].delete(id);
  const gl={};for(const name of ['VERTEX_SHADER','FRAGMENT_SHADER','COMPILE_STATUS','LINK_STATUS','ARRAY_BUFFER','ELEMENT_ARRAY_BUFFER','STATIC_DRAW','FLOAT','DEPTH_TEST','LEQUAL','COLOR_BUFFER_BIT','DEPTH_BUFFER_BIT','TRIANGLES','UNSIGNED_SHORT'])gl[name]=++serial;gl.NO_ERROR=0;
  Object.assign(gl,{createShader:()=>create('shaders'),deleteShader:id=>remove('shaders',id),getShaderParameter:()=>!options.shaderFail,createProgram:()=>create('programs'),deleteProgram:id=>remove('programs',id),getProgramParameter:()=>!options.linkFail,createBuffer:()=>create('buffers'),deleteBuffer:id=>remove('buffers',id),createVertexArray:()=>create('arrays'),deleteVertexArray:id=>remove('arrays',id),getUniformLocation:()=>({}),getError:()=>options.drawFail?1:0});
  for(const name of ['shaderSource','compileShader','attachShader','linkProgram','bindVertexArray','bindBuffer','bufferData','enableVertexAttribArray','vertexAttribPointer','enable','depthFunc','clearColor','viewport','clear','useProgram','uniformMatrix4fv','uniform1f','drawElements'])gl[name]=()=>{};
  const canvas=new Surface();Object.assign(canvas,{hidden:false,width:0,height:0,clientHeight:380,setAttribute(){},getContext:type=>{assert.equal(type,'webgl2');return options.unavailable?null:gl;},getBoundingClientRect:()=>({left:0,top:0,width:700,height:380}),remove(){host.children.splice(host.children.indexOf(canvas),1);}});
  const doc=new Surface(),win=new Surface(),queue=new Map();let rafSerial=0;Object.assign(doc,{hidden:false,createElement:()=>canvas,defaultView:win});Object.assign(win,{devicePixelRatio:2,performance:{now:()=>1}});
  const host={ownerDocument:doc,children:[],firstChild:null,insertBefore(node){this.children.unshift(node);},getBoundingClientRect:()=>({width:700,height:380})};
  class Resize{observe(){}disconnect(){disconnected++;}}
  class Intersection{constructor(callback){intersectionCallback=callback;}observe(){}disconnect(){intersectionDisconnected++;}}
  const env={document:doc,window:win,ResizeObserver:Resize,IntersectionObserver:Intersection,requestAnimationFrame:fn=>{const id=++rafSerial;queue.set(id,fn);return id;},cancelAnimationFrame:id=>queue.delete(id)};
  const calls={ready:0,fallback:[],agents:[],evidence:0},renderer=world.createWorldRenderer(host,{onReady:()=>calls.ready++,onFallback:reason=>calls.fallback.push(reason),onSelectAgent:id=>calls.agents.push(id),onSelectEvidence:()=>calls.evidence++},env);
  const flush=(time=1000)=>{const work=[...queue.values()];queue.clear();for(const fn of work)fn(time);};
  return {renderer,canvas,doc,win,host,gl,queue,live,made,calls,flush,setIntersecting:value=>intersectionCallback([{isIntersecting:value}]),get intersectionDisconnected(){return intersectionDisconnected;},get disconnected(){return disconnected;}};
}
const agents=()=>[{id:'codex',type:'codex',active:false},{id:'claude',type:'claude',active:false},{id:'antigravity',type:'gemini',active:false}];

test('indexed vec3 meshes stay finite, have depth, and remain below all scene budgets',()=>{
  const scene=world.sceneGeometry(Array.from({length:300},(_,i)=>'completion-'+i));
  assert.ok(scene.cuboids<=world.LIMITS.cuboids);assert.ok(scene.triangles<=world.LIMITS.triangles);assert.ok(scene.drawCalls<=world.LIMITS.drawCalls);
  for(const mesh of [scene.ground,scene.outputs,...Object.values(scene.characters).flatMap(Object.values)]){
    assert.ok(mesh.indices instanceof Uint16Array);assert.equal(mesh.vertices.length%9,0);assert.equal(mesh.indices.length%3,0);assert.ok([...mesh.vertices].every(Number.isFinite));assert.ok([...mesh.indices].every(i=>i<mesh.vertices.length/9));
  }
  for(const parts of Object.values(scene.characters)){const positions=[...parts.body.vertices].filter((_,i)=>i%9<3),z=[...parts.body.vertices].filter((_,i)=>i%9===2);assert.ok(Math.max(...z)-Math.min(...z)>.6);assert.ok(positions.length>300);}
  assert.equal(scene.outputs.total,300);assert.equal(scene.outputs.visibleKeys.length,256);assert.equal(scene.outputs.overflow,44);
});

test('completion-key projection deduplicates without mutating the source list',()=>{
  const keys=['a','a','b'],before=JSON.stringify(keys),result=world.outputGeometry(keys);assert.equal(result.total,2);assert.equal(result.cuboids,2);assert.equal(JSON.stringify(keys),before);
});

test('orthographic camera presets change projected side/depth relationships',()=>{
  const left=world.cameraMatrix(1.75,'left'),center=world.cameraMatrix(1.75,'center'),right=world.cameraMatrix(1.75,'right');assert.notDeepEqual([...left],[...right]);assert.notDeepEqual(world.project([2,1,-2],left,700,400),world.project([2,1,-2],right,700,400));assert.ok([...center].every(Number.isFinite));
});

test('DPR and backing pixel budget cap large and small canvases',()=>{
  assert.equal(world.pixelSize(700,380,3).dpr,1.5);for(const [w,h]of [[700,380],[4000,3000],[4000,500],[400,4000],[0,0]]){const size=world.pixelSize(w,h,4);assert.ok(size.width*size.height<=1600000);assert.ok(size.width<=1600&&size.height<=1000);assert.ok(size.width>=1&&size.height>=1);}
});

test('20 toggles, repeated state and resizes retain one canvas and stable GPU allocations',()=>{
  const h=harness(),snapshot={agents:agents(),completionKeys:['one','two']};h.renderer.update(snapshot);const allocated={...h.made};
  for(let i=0;i<20;i++){h.renderer.setVisible(true);h.renderer.resize(700+i,380,3);h.renderer.update(snapshot);h.flush(1000+i*40);h.renderer.setVisible(false);assert.equal(h.queue.size,0);}
  assert.equal(h.host.children.length,1);assert.deepEqual(h.made,allocated);assert.equal(h.renderer.getDiagnostics().confirmedCount,2);assert.equal(snapshot.completionKeys.length,2);h.renderer.dispose();assert.equal(h.host.children.length,0);
});

test('idle, hidden, reduced-motion and chat-hidden states stop recurring frames',()=>{
  const h=harness();h.renderer.update({agents:agents(),completionKeys:[]});h.renderer.setVisible(true);h.flush();assert.equal(h.queue.size,0);
  const moving=agents();moving[2].active=true;h.renderer.update({agents:moving,completionKeys:[]});h.flush(1040);assert.equal(h.queue.size,1);
  h.doc.hidden=true;h.doc.emit('visibilitychange');assert.equal(h.queue.size,0);h.doc.hidden=false;h.doc.emit('visibilitychange');h.flush(1080);assert.equal(h.queue.size,1);
  h.setIntersecting(false);assert.equal(h.queue.size,0);assert.equal(h.renderer.getDiagnostics().viewportVisible,false);h.setIntersecting(true);h.flush(90000);assert.equal(h.queue.size,1);h.renderer.setReducedMotion(true);h.flush(90040);assert.equal(h.queue.size,0);h.renderer.setVisible(false);assert.equal(h.queue.size,0);h.renderer.dispose();
});

test('canvas picking calls only the existing selection callbacks',()=>{
  const h=harness();h.renderer.update({agents:agents(),completionKeys:[]});h.renderer.setVisible(true);h.flush();const p=world.project([world.POSITIONS.claude[0],1.1,world.POSITIONS.claude[2]],world.cameraMatrix(700/380),700,380);h.canvas.emit('pointerup',{clientX:p[0],clientY:p[1]});assert.deepEqual(h.calls.agents,['claude']);const evidence=world.project([1.1,.6,2.2],world.cameraMatrix(700/380),700,380);h.canvas.emit('pointerup',{clientX:evidence[0],clientY:evidence[1]});assert.equal(h.calls.evidence,1);assert.equal(h.renderer.getDiagnostics().confirmedCount,0);h.renderer.dispose();
});

test('unsupported WebGL2 and compile/link failures provide a non-retrying SVG fallback',()=>{
  for(const failure of [{unavailable:true},{shaderFail:true},{linkFail:true}]){const h=harness(failure);h.renderer.setVisible(true);h.renderer.update({agents:agents(),completionKeys:[]});assert.equal(h.renderer.getDiagnostics().available,false);assert.equal(h.queue.size,0);assert.equal(h.canvas.hidden,true);assert.equal(h.calls.fallback.length,1);h.renderer.dispose();for(const resources of Object.values(h.live))assert.equal(resources.size,0);}
});

test('context loss preserves projection and permits one bounded rebuild',()=>{
  const h=harness();h.renderer.update({agents:agents(),completionKeys:['verified']});h.renderer.setVisible(true);h.flush();h.canvas.emit('webglcontextlost',{preventDefault(){}});assert.equal(h.queue.size,0);assert.equal(h.canvas.hidden,true);assert.equal(h.renderer.getDiagnostics().confirmedCount,1);h.canvas.emit('webglcontextrestored');h.flush(1040);assert.equal(h.renderer.getDiagnostics().ready,true);assert.equal(h.renderer.getDiagnostics().confirmedCount,1);h.canvas.emit('webglcontextlost',{preventDefault(){}});h.canvas.emit('webglcontextrestored');assert.equal(h.queue.size,0);h.renderer.dispose();for(const resources of Object.values(h.live))assert.equal(resources.size,0);
});

test('draw failure stops retries and dispose releases every resource/listener',()=>{
  const h=harness({drawFail:true});h.renderer.setVisible(true);h.flush();h.renderer.update({agents:agents(),completionKeys:[]});assert.equal(h.queue.size,0);assert.equal(h.canvas.hidden,true);h.renderer.dispose();h.renderer.dispose();for(const resources of Object.values(h.live))assert.equal(resources.size,0);assert.equal(h.canvas.listenerCount,0);assert.equal(h.doc.listenerCount,0);assert.equal(h.win.listenerCount,0);assert.equal(h.disconnected,1);assert.equal(h.intersectionDisconnected,1);assert.equal(h.renderer.getDiagnostics().disposed,true);
});
