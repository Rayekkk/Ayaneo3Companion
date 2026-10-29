// Execute the actual FanPage with controlled React hooks/RPC timing.
// Ported from LegionGo2Companion: the curve is the AYANEO 3 EC's duty in percent, the tach is RPM only.
const test = require('node:test');
const assert = require('node:assert/strict');
const { harness: panel, all: walk, settle } = require('./frontend_harness.cjs');
const curve=[[50,60],[57,62],[64,70],[71,78],[78,88],[85,100],[93,100],[100,100]];
const limits={min_pct:10,last_min_pct:40,max_pct:100,step:1,hard_max:100,rpm_at_min:712,rpm_per_pct:(5540-712)/90};
const initial={supported:true,error:'',active_curve:'global',settings:{enabled:false,assign_tdp_presets:true,revision:0,global_curve:curve,presets:{}},
  templates:{silent:curve.map(([t,p],i)=>[t,i<7?0:p]),balanced:curve,aggressive:curve.map(([t])=>[t,100])},
  presets:['silent','balanced','performance','custom'],limits,telemetry:{active:false,automatic:true},
  readings:{valid:true,rpm:1600,apu_temperature:44.5,age_seconds:0,automatic:true,error:''}};
const json=x=>JSON.parse(JSON.stringify(x));
const flush=()=>settle();
const find=(tree,predicate)=>walk(tree).find(predicate);
const label=(tree,name)=>find(tree,n=>n.props?.label===name);
const buttonText=n=>Array.isArray(n.props?.children)?n.props.children.join(''):n.props?.children;
const button=(tree,name)=>find(tree,n=>n.type==='ButtonItem'&&buttonText(n)===name);
// A fake fan backend behind every RPC, answering in the D2 shape: {ok, state} or {ok: false, error}.
function harness(initialState=initial,component='FanPage'){
  let state=json(initialState), read=null, failSave=false,modal=null;
  const h=panel({entry:'src/fan.tsx',component,keep:['CurveGraph','FullEditor'],
    ui:{GamepadButton:{OK:1,CANCEL:2,BUMPER_LEFT:5,BUMPER_RIGHT:6,DIR_UP:9,DIR_DOWN:10,DIR_LEFT:11,DIR_RIGHT:12},
      showModal:(node,parent,props)=>{modal={node,props,Close(){props.fnOnClose?.();}};return modal;}},
    api:{callable:name=>async(...args)=>{
        h.calls.push({name,args:json(args)});
        // A failed read rejects, as Decky does when the backend is gone.
        if(name==='fan_get_state')return read?read().then(s=>({ok:true,state:s})):{ok:true,state:json(state)};
        try{
          if(name==='fan_set_enabled'){state.settings.enabled=args[0];state.settings.revision++;state.telemetry.active=args[0];state.telemetry.automatic=!args[0];}
          if(name==='fan_save_assignment_mode'){assert.equal(args[1],state.settings.revision);state.settings.assign_tdp_presets=args[0];state.settings.revision++;}
          if(name==='fan_save_curve'){
            if(failSave)throw Error('save rejected');assert.equal(args[2],state.settings.revision,'CAS revision');
            if(args[0]==='global')state.settings.global_curve=json(args[1]);else if(args[1])state.settings.presets[args[0]]=json(args[1]);else delete state.settings.presets[args[0]];
            state.settings.revision++;
            state.needs_curve_review=false;
          }
        }catch(error){return{ok:false,error:error.message};}
        return{ok:true,state:json(state)};
      }}});
  h.advance(100000);
  const live=()=>[...h.timers,...h.intervals].filter(t=>!t.done);
  return{exports:h.exports,calls:h.calls,live,render:(props={})=>h.render(props),
    hide(){h.hide();return h.render();},show(){h.show();return h.render();},unmount:()=>h.unmount(),
    poll(){const t=h.timers.find(t=>!t.done);t.done=true;t.fn();},
    advance(ms){h.advance(ms);},
    setRead(fn){read=fn;},failSave(v){failSave=v;},getState:()=>json(state),foreign(fn){fn(state);},getModal:()=>modal,toasts:h.toasts};
}
const graphHarness=()=>harness(initial,'CurveGraph');
const text=node=>node==null||node===false?'':typeof node!=='object'?String(node):
  (Array.isArray(node)?node:[node.props?.children]).map(text).join(' ');
const summaryText=tree=>find(tree,n=>n.props?.['data-fan-summary']).props.children[0].props.children;

test('eight points in percent, stop/deadband, dirty scopes, CAS/failures/late reads, graph buttons, full-screen parent pause, conflict block, expiry and hidden cleanup',async()=>{
  const h=harness();h.render();await flush();let tree=h.render();
  assert.equal(h.calls.filter(c=>c.name==='fan_get_state').length,1);assert.equal(h.live().length,2);
  const summary=find(tree,n=>n.props?.['data-fan-summary']);
  const telemetry=find(summary,n=>n!==summary && n.props && text(n)==='APU 44.5°C · 1600 RPM');
  assert.ok(telemetry,'The summary shows measured temperature and tachometer values');
  assert.equal(label(tree,'Enable Fan Control').props.checked,false);
  assert.equal(label(tree,'Show fan speed as percentage'),undefined,'Percent is the only unit: no unit toggle');
  assert.equal(find(tree,n=>n.type==='SliderField'),undefined,'No slider editor');
  assert.equal(label(tree,'Speed edit step').props.selectedOption,2,'Default edit step is 2 %');
  assert.deepEqual(json(label(tree,'Speed edit step').props.rgOptions.map(o=>o.label)),['1%','2%','5%','10%']);
  assert.ok(label(tree,'Curve').props.rgOptions.some(o=>o.data==='custom'),'Custom remains an independent curve assignment');
  const simple=harness({...initial,settings:{...initial.settings,assign_tdp_presets:false}});
  simple.render();await flush();let st=simple.render();
  assert.equal(label(st,'Curve'),undefined,'Default global mode hides TDP scope selection');
  assert.equal(label(st,'Assign curves to TDP presets').props.checked,false);
  assert.ok(find(st,n=>n.type==='PanelSection'&&n.props.title==='Settings'));
  const callsBefore=simple.calls.length,untouched=simple.getState();
  label(st,'Starting curve').props.onChange({data:'aggressive'});st=simple.render();
  assert.equal(simple.calls.length,callsBefore,'Selecting a template performs no RPC');
  assert.deepEqual(simple.getState(),untouched,'Selecting a template does not save or arm');
  assert.equal(label(st,'Starting curve').props.selectedOption,'aggressive','Template label is derived from exact draft points');
  find(st,n=>n.type===simple.exports.CurveGraph).props.edit(1,99,0);st=simple.render();
  assert.equal(label(st,'Starting curve').props.selectedOption,'','An edited curve is never labelled as the former template');
  assert.equal(button(st,'Save global curve').props.disabled,false);
  const globalDraft=json(find(st,n=>n.type===simple.exports.CurveGraph).props.curve);
  label(st,'Assign curves to TDP presets').props.onChange(true);await flush();st=simple.render();
  assert.equal(simple.getState().settings.assign_tdp_presets,false,'Assignments wait for the draft to be saved or discarded');
  assert.equal(label(st,'Assign curves to TDP presets').props.description,'Save or discard your curve changes first.');
  button(st,'Save global curve').props.onClick();await flush();st=simple.render();
  assert.deepEqual(simple.getState().settings.global_curve,globalDraft);
  label(st,'Assign curves to TDP presets').props.onChange(true);await flush();st=simple.render();
  assert.ok(label(st,'Curve'));label(st,'Curve').props.onChange({data:'silent'});st=simple.render();
  label(st,'Starting curve').props.onChange({data:'silent'});st=simple.render();
  const silentDraft=json(find(st,n=>n.type===simple.exports.CurveGraph).props.curve);
  button(st,'Assign curve to Silent').props.onClick();await flush();st=simple.render();
  assert.deepEqual(simple.getState().settings.presets.silent,silentDraft);
  assert.deepEqual(simple.getState().templates,untouched.templates,'Drafts never alias hardcoded template arrays');
  assert.equal(simple.getState().settings.enabled,false);
  assert.equal(simple.getState().settings.template,undefined);
  label(st,'Curve').props.onChange({data:'global'});st=simple.render();
  button(st,'Full screen curve editor').props.onClick();st=simple.render();
  const fm=simple.getModal(),editor=harness(simple.getState());
  const fullPropsTemplates={fullscreen:true,initialEditor:fm.node.props.initial,onEditorChange:fm.node.props.onResult};
  editor.render(fullPropsTemplates);await flush();let et=editor.render(fullPropsTemplates);
  find(et,n=>n.type==='Dropdown'&&n.props.menuLabel==='Starting curve').props.onChange({data:'balanced'});et=editor.render(fullPropsTemplates);
  find(et,n=>n.type===editor.exports.CurveGraph).props.edit(1,59,0);et=editor.render(fullPropsTemplates);
  const editedFull=json(find(et,n=>n.type===editor.exports.CurveGraph).props.curve);
  find(et,n=>n.type==='DialogButton'&&n.props.children==='Save global curve').props.onClick();await flush();et=editor.render(fullPropsTemplates);
  assert.deepEqual(editor.getState().settings.global_curve,editedFull,'Fullscreen template edits save through ordinary curve RPC');
  simple.setRead(async()=>editor.getState());fm.Close();simple.render();await flush();st=simple.render();
  assert.deepEqual(json(find(st,n=>n.type===simple.exports.CurveGraph).props.curve),editedFull,'Saved fullscreen draft synchronizes with parent');
  assert.equal(button(st,'Save global curve').props.disabled,true);
  editor.unmount();simple.unmount();
  for(const [width,height] of [[1040,266],[900,220],[1600,480]]){
    const m=h.exports.graphLayout(true,width,height),scale=Math.min(width/m.width,height/m.height);
    assert.ok(Object.values(m).every(value=>Number.isFinite(value) && value>0),'Fullscreen transforms remain finite');
    assert.ok(m.left+m.span<=m.width && m.rise<=m.bottom && m.bottom<=m.height,'Plot coordinates stay inside the graph');
    assert.ok(m.span*scale<=width && m.rise*scale<=height,'Scaled plot fits its viewport');
    // Readability and Steam overlay clearance require rendered visual review;
    // model coordinates do not establish a font size or a screen layout.
  }
  const {editPoint,nudgeSpeed}=h.exports;
  assert.equal(editPoint(curve,0,0,51,limits)[0][0],51);
  assert.equal(editPoint(curve,1,0,52,limits)[1][0],52);
  assert.equal(editPoint(curve,7,0,99,limits)[7][0],100);
  assert.equal(editPoint(curve,7,1,0,limits)[7][1],100,'Point 8 cannot drop below point 7');
  const low=[[0,0],[10,0],[20,0],[30,0],[40,0],[50,0],[95,0],[100,100]];
  assert.equal(editPoint(low,7,1,40,limits)[7][1],40,'Point 8 moves down to its own floor');
  for(const v of [0,1,10,39,-100])assert.equal(editPoint(low,7,1,v,limits)[7][1],40,'Point 8 can never stop or go below 40 %');
  assert.equal(editPoint(low,6,1,10,limits)[6][1],10,'Other points keep the 10 % running minimum');
  assert.equal(editPoint(low,7,1,999,limits)[7][1],100);
  assert.equal(editPoint(low,7,0,50,limits)[7][0],100,'Point 8 stays at 100°C');
  const lowLast=[[0,0],[45,10],[55,40],[65,50],[75,55],[85,58],[95,60],[100,62]];
  assert.equal(editPoint(lowLast,6,1,80,limits)[6][1],62,'Point 7 cannot rise above point 8');
  assert.equal(editPoint(lowLast,7,1,40,limits)[7][1],60,'Point 8 cannot drop below point 7');
  assert.equal(editPoint(curve,4,1,11,limits)[4][1],78,'Lower neighbour bounds the speed');
  assert.equal(editPoint(curve,0,1,2,limits)[0][1],0,'Below half the running minimum snaps to stop');
  const stopped=json(curve);stopped[0][1]=0;
  assert.equal(editPoint(stopped,0,1,4,limits)[0][1],0,'Below half the running minimum snaps to stop');
  assert.equal(editPoint(stopped,0,1,5,limits)[0][1],10,'Half the running minimum snaps to the minimum');
  assert.equal(editPoint(curve,0,0,-50,limits)[0][0],0);
  assert.equal(editPoint(curve,0,0,100,limits)[0][0],56);
  assert.equal(editPoint(curve,4,1,999,limits)[4][1],100);
  assert.equal(editPoint(curve,2,1,999,limits)[2][1],78);
  const zeroPair=json(curve);zeroPair[0][1]=zeroPair[1][1]=0;
  assert.equal(editPoint(zeroPair,0,1,20,limits)[0][1],0,'A following stopped point prevents a rising deadband snap');
  assert.equal(editPoint(curve,2,1,0,limits)[2][1],62,'Cannot stop after a running neighbour');
  assert.equal(nudgeSpeed(0,2,10),10,'A step up from a stopped fan starts at the running minimum');
  assert.equal(nudgeSpeed(10,-2,10),0,'A step down below the running minimum stops the fan');
  assert.equal(nudgeSpeed(10,2,10),12);
  assert.equal(nudgeSpeed(0,-2,10),0);
  for(const delta of [-200,-50,-10,-5,-1,1,5,10,50,200])for(const base of [curve,low,lowLast])for(let i=0;i<8;i++){
    const result=editPoint(base,i,1,base[i][1]+delta,limits);
    assert.ok(result.every((p,j)=>j===0||p[1]>=result[j-1][1]));
    assert.ok(result.every(p=>p[1]===0||p[1]>=10));
    assert.ok(result[7][0]===100&&result[7][1]>=40);
  }
  const steps=harness();steps.render();await flush();let stepTree=steps.render();
  for(const step of [1,2,5,10]){
    label(stepTree,'Speed edit step').props.onChange({data:step});stepTree=steps.render();
    assert.equal(label(stepTree,'Speed edit step').props.selectedOption,step);
    assert.equal(find(stepTree,n=>n.type===steps.exports.CurveGraph).props.step,step,'Graph edits use the displayed step');
  }
  assert.deepEqual(steps.getState().settings.global_curve,curve,'Changing edit steps never changes the curve');steps.unmount();

  let graph=find(tree,n=>n.type===h.exports.CurveGraph);graph.props.edit(0,51,0);tree=h.render();
  assert.equal(button(tree,'Save global curve').props.disabled,false);
  label(tree,'Curve').props.onChange({data:'silent'});tree=h.render();
  graph=find(tree,n=>n.type===h.exports.CurveGraph);graph.props.edit(1,11,2);tree=h.render();
  label(tree,'Curve').props.onChange({data:'global'});tree=h.render();
  assert.equal(find(tree,n=>n.type===h.exports.CurveGraph).props.curve[0][0],51,'Changing scope retains unsaved drafts');
  let finishRead;const old=h.getState();h.setRead(()=>new Promise(r=>finishRead=r));h.poll();await flush();
  button(tree,'Save global curve').props.onClick();await flush();tree=h.render();
  assert.equal(h.getState().settings.global_curve[0][0],51);
  finishRead(old);await flush();tree=h.render();
  assert.equal(find(tree,n=>n.type===h.exports.CurveGraph).props.curve[0][0],51,'Old read cannot revert confirmed save');
  h.setRead(null);
  label(tree,'Curve').props.onChange({data:'silent'});tree=h.render();
  h.failSave(true);button(tree,'Assign curve to Silent').props.onClick();await flush();tree=h.render();
  assert.match(button(tree,'Assign curve to Silent').props.description,/save rejected/,'the error stays on the failed control');
  assert.equal(h.toasts.at(-1).title,'Fan Control');assert.match(h.toasts.at(-1).body,/save rejected/);
  assert.equal(find(tree,n=>n.type===h.exports.CurveGraph).props.curve[2][1],62,'Failed save retains draft');
  h.failSave(false);button(tree,'Assign curve to Silent').props.onClick();await flush();tree=h.render();
  assert.equal(h.getState().settings.presets.silent[2][1],62);
  assert.equal(h.toasts.at(-1).body,'Curve saved.');
  button(tree,'Use global curve for Silent').props.onClick();await flush();assert.equal(h.getState().settings.presets.silent,undefined);
  tree=h.render();button(tree,'Full screen curve editor').props.onClick();tree=h.render();
  assert.equal(h.live().length,0,'Parent does no polling behind full-screen modal');
  assert.ok(h.getModal());h.getModal().Close();h.render();await flush();h.render();assert.equal(h.live().length,2);
  h.hide();assert.equal(h.live().length,0);h.unmount();

  const g=graphHarness();let selected=0,edited=[];
  const props={curve,status:{...initial,receivedAt:100000},selected,disabled:false,step:1,select:i=>selected=i,edit:(...args)=>edited.push(args)};
  let graphics=g.render(props),svg=find(graphics,n=>n.type==='svg');
  assert.equal(graphics.props.tabIndex,0,'The graph must be an eligible native keyboard/gamepad focus target');
  assert.equal(find(svg,n=>n.props?.['data-fan-point']===8).type,'circle');
  const marker=find(svg,n=>n.props?.['data-fan-live-marker']);
  assert.ok(marker,'Fresh actual tach marker shown');
  const at=pct=>find(svg,n=>n.props?.['data-fan-point']===1).props.cy+(pct-curve[0][1])*(find(svg,n=>n.props?.['data-fan-point']===8).props.cy-find(svg,n=>n.props?.['data-fan-point']===1).props.cy)/(100-curve[0][1]);
  assert.ok(Math.abs(marker.props.children[0].props.cy-at(10+(1600-712)/limits.rpm_per_pct))<1e-9,'The tach marker sits at the duty a healthy fan needs for its RPM');
  const event=button=>({detail:{button},preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;}});
  assert.equal(graphics.props.onGamepadDirection,undefined,'Normal navigation remains available outside edit mode');
  graphics.props.onOKButton(event(1));graphics=g.render(props);
  graphics.props.onActivate(event(1));graphics=g.render(props);
  assert.equal(graphics.props['data-fan-editing'],'true','Activate and OK both enter idempotently');
  graphics.props.onButtonDown(event(6));assert.equal(selected,1);
  props.selected=1;graphics=g.render(props);
  graphics.props.onButtonDown(event(5));assert.equal(selected,0,'LB selects the previous point');
  props.selected=0;graphics=g.render(props);
  graphics.props.onButtonDown(event(5));assert.equal(selected,7,'LB wraps from the first point to the last point');
  props.selected=7;graphics=g.render(props);
  graphics.props.onButtonDown(event(6));assert.equal(selected,0,'RB wraps from the last point to the first');
  props.selected=1;graphics=g.render(props);
  graphics.props.onButtonDown(event(12));assert.equal(edited.length,0,'ButtonDown must not duplicate the native direction path');
  graphics.props.onGamepadDirection(event(12));graphics.props.onGamepadDirection(event(9));
  assert.deepEqual(json(edited),[[0,58],[1,63]],'D-pad edits temperature by one degree and selected speed step');
  const pointerSvg=find(graphics,n=>n.type==='svg');
  pointerSvg.props.ref.current={getScreenCTM:()=>({inverse:()=>({})}),createSVGPoint:()=>({matrixTransform(){return{x:this.x,y:this.y};}}),setPointerCapture(){}};
  const hit=find(pointerSvg,n=>n.type==='g'&&n.props.onPointerDown);
  hit.props.onPointerDown({pointerId:1,preventDefault(){}});
  // Drag to the independently specified 52°C / 60 % using the rendered
  // first/last point locations. This survives plot-margin/layout changes.
  const firstPoint=find(pointerSvg,n=>n.props?.['data-fan-point']===1).props;
  const lastPoint=find(pointerSvg,n=>n.props?.['data-fan-point']===8).props;
  const clientX=firstPoint.cx+(52-curve[0][0])/(curve[7][0]-curve[0][0])*(lastPoint.cx-firstPoint.cx);
  edited=[];pointerSvg.props.onPointerMove({pointerId:1,clientX,clientY:firstPoint.cy});
  assert.deepEqual(json(edited).map(([axis,,point])=>[axis,point]),[[0,0],[1,0]],'Pointer drag keeps the chosen point and updates both coordinates');
  assert.ok(Math.abs(edited[0][1]-52)<1e-9,'Pointer temperature follows the rendered position');
  assert.equal(edited[1][1],60,'Pointer speed follows the rendered position');
  find(pointerSvg,n=>n.type==='g'&&n.props.children?.[0]?.props?.['data-fan-point']===8).props.onPointerDown({pointerId:2,preventDefault(){}});
  edited=[];pointerSvg.props.onPointerMove({pointerId:2,clientX:lastPoint.cx,clientY:firstPoint.cy});
  assert.deepEqual(json(edited).map(([axis,,point])=>[axis,point]),[[0,7],[1,7]],'Point 8 can be dragged; editPoint keeps it at 100°C');
  pointerSvg.props.onLostPointerCapture();edited=[];
  pointerSvg.props.onPointerMove({pointerId:1,clientX:100,clientY:100});
  assert.equal(edited.length,0,'Losing capture must end a drag before mouse hover can edit again');
  const b=event(2);graphics.props.onCancelButton(b);assert.ok(b.prevented && b.stopped,'B exits steering without leaving module');
  graphics=g.render(props);assert.equal(graphics.props.onCancelButton,undefined,'Next B follows native navigation');
  assert.equal(graphics.props.onGamepadFocus,undefined,'Focus alone does not enter edit mode');
  assert.equal(graphics.props['data-fan-editing'],'false');
  graphics.props.onOKButton(event(1));graphics=g.render(props);
  assert.equal(graphics.props['data-fan-editing'],'true','A explicitly enters controller editing');
  graphics.props.onGamepadBlur(event(0));graphics=g.render(props);assert.equal(graphics.props.onGamepadDirection,undefined,'Blur exits edit mode');
  props.status={...props.status,receivedAt:96000};graphics=g.render(props);
  assert.equal(find(graphics,n=>n.props?.['data-fan-live-marker']),undefined,'Stale sample removes marker');g.unmount();

  const wide=graphHarness();let wideEdits=[];
  const wideProps={...props,status:{...initial,receivedAt:100000},fullscreen:true,selected:1,edit:(...args)=>wideEdits.push(args)};
  const wideTree=wide.render(wideProps),wideSvg=find(wideTree,n=>n.type==='svg');
  const metrics=wide.exports.graphLayout(true);
  wideSvg.props.ref.current={getScreenCTM:()=>({inverse:()=>({})}),createSVGPoint:()=>({matrixTransform(){return{x:(this.x-40)/1.5,y:(this.y-60)/1.5};}}),setPointerCapture(){}};
  const wideHit=find(wideSvg,n=>n.type==='g'&&n.props.children[0].props['data-fan-point']===2);
  wideHit.props.onPointerDown({pointerId:7,preventDefault(){}});
  wideSvg.props.onPointerMove({pointerId:7,clientX:40+1.5*(metrics.left+56*metrics.span/100),clientY:60+1.5*(metrics.bottom-59/100*metrics.rise)});
  assert.ok(Math.abs(wideEdits[0][1]-56)<1e-9,'Fullscreen inverse transform uses the whole 0–100°C span');
  assert.deepEqual(json(wideEdits).map(([axis,value,point])=>[axis,Math.round(value),point]),[[0,56,1],[1,59,1]],'Fullscreen pointer maps scaled screen coordinates to the chosen one-percent edit before neighbour clamping');
  wideSvg.props.onPointerCancel();wideEdits=[];
  wideSvg.props.onPointerMove({pointerId:7,clientX:100,clientY:100});assert.equal(wideEdits.length,0,'Cancelled fullscreen drag cannot edit');wide.unmount();
  const full=harness();let closed=0;const fullProps={fullscreen:true,onClose:()=>closed++};
  let fullTree=full.render(fullProps);
  await flush();fullTree=full.render(fullProps);
  assert.ok(find(fullTree,n=>n.type==='DialogBody'));assert.ok(find(fullTree,n=>n.type==='DialogFooter'));
  for(const attr of ['data-fan-controls','data-fan-footer'])assert.equal(find(fullTree,n=>n.props?.[attr]).props['flow-children'],'row','Horizontal controls use native horizontal focus flow');
  assert.equal(find(fullTree,n=>n.type==='Dropdown'&&n.props.menuLabel==='Speed unit'),undefined,'No unit choice in the dialog either');
  assert.equal(find(fullTree,n=>n.type===full.exports.CurveGraph).props.fullscreen,true,'The fullscreen view keeps the same editable graph');
  find(fullTree,n=>n.type==='DialogButton'&&n.props.children==='Back to Fan Control').props.onClick();assert.equal(closed,1);full.unmount();

  const blocked=harness({...initial,ownership:{blocked:true,state:'unavailable',reason:'Another app is controlling the fan.'}});
  blocked.render();await flush();tree=blocked.render();assert.equal(label(tree,'Enable Fan Control').props.disabled,true);
  assert.match(label(tree,'Enable Fan Control').props.description,/Another app/);blocked.unmount();
  {
    const draft=harness();draft.render();await flush();let view=draft.render();
    find(view,n=>n.type===draft.exports.CurveGraph).props.edit(1,50,0);view=draft.render();
    assert.equal(label(view,'Enable Fan Control').props.disabled,true,'turning on waits for the curve draft');
    assert.equal(label(view,'Enable Fan Control').props.description,'Save or discard your curve changes first.');
    draft.unmount();
    const review=harness({...initial,needs_curve_review:true});review.render();await flush();view=review.render();
    assert.equal(label(view,'Enable Fan Control').props.description,'Review and save the curve first.');review.unmount();
    const last=harness();last.render();await flush();view=last.render();
    const rows=walk(view).filter(n=>['ButtonItem','ToggleField','DropdownItem','Field'].includes(n.type));
    assert.equal(rows.at(-1).props.label,'Assign curves to TDP presets','the page ends with an always-enabled row');
    assert.equal(rows.at(-1).props.disabled,undefined);last.unmount();
  }
  const recovered=harness();recovered.render();await flush();recovered.render();
  recovered.setRead(()=>Promise.reject(Error('transient read reset')));recovered.poll();await flush();
  tree=recovered.render();assert.match(summaryText(tree),/APU —/,'a failed read shows no stale numbers');
  recovered.setRead(null);recovered.poll();await flush();tree=recovered.render();
  assert.match(summaryText(tree),/APU 44.5/);
  label(tree,'Curve').props.onChange({data:'silent'});tree=recovered.render();
  recovered.failSave(true);button(tree,'Assign curve to Silent').props.onClick();await flush();tree=recovered.render();
  assert.match(button(tree,'Assign curve to Silent').props.description,/save rejected/);
  recovered.poll();await flush();tree=recovered.render();
  assert.match(button(tree,'Assign curve to Silent').props.description,/save rejected/,'Background read recovery does not erase a rejected user mutation');
  recovered.unmount();
  const expire=harness();expire.render();await flush();tree=expire.render();
  let pendingResolve;expire.setRead(()=>new Promise(r=>pendingResolve=r));expire.poll();await flush();
  expire.advance(3000);tree=expire.render();assert.match(summaryText(tree),/APU —/);
  expire.hide();const count=expire.calls.length;pendingResolve(initial);await flush();assert.equal(expire.calls.length,count);expire.unmount();
  const late=harness();late.render();await flush();let end;late.setRead(()=>new Promise(r=>end=r));const pending=late.exports.getFanState();await flush();
  late.advance(3000);end(json(initial));assert.match(late.exports.fanSummary(await pending),/APU —/);late.unmount();
  const queued=harness();queued.render();await flush();const callbacks=queued.live().map(t=>t.fn),countQ=queued.calls.length;
  queued.hide();callbacks.forEach(fn=>fn());await flush();assert.equal(queued.calls.length,countQ);
  await assert.rejects(queued.exports.getFanState(()=>false),/no longer visible/);queued.unmount();
  const paused=json(initial);paused.settings.enabled=true;paused.telemetry={active:false,automatic:true};paused.error='Fan Control paused; resuming automatically.';
  assert.match(late.exports.fanSummary(paused),/Paused, resuming automatically$/,'a paused or retrying session never claims a custom curve');
  paused.telemetry.active=true;paused.error='';assert.match(late.exports.fanSummary(paused),/Global curve$/);
  // The status table, top-down, from fields the backend already returns.
  const status=(change,startingFor)=>{const s=json(initial);change(s);return late.exports.fanStatus(s,startingFor);};
  const cases=[
    [s=>{s.supported=false;s.error='Fan Control supports only the AYANEO 3.';},['Not available on this console','Fan Control supports only the AYANEO 3.']],
    [s=>{s.settings.enabled=true;s.error='Fan busy.';},['Paused, resuming automatically','No action needed.']],
    [s=>{s.settings.enabled=true;},['Starting your curve…','No action needed.']],
    [s=>{s.settings.enabled=true;},['Starting your curve…','Still starting. Turn Fan Control off and on to retry.'],30000],
    [s=>{s.error='Fan stalled.';},['Turned off after an error','Fan stalled. Turn on to try again.']],
    [s=>{s.ownership={blocked:true,state:'x',reason:'Another app is controlling the fan.'};},['Another app controls the fan','Another app is controlling the fan.']],
    [s=>{s.settings.enabled=true;s.telemetry.active=true;s.active_curve='performance';},['Performance TDP curve','Turn off to return the fan to automatic.']],
    [s=>{s.readings.automatic=false;},['Another app controls the fan',undefined]],
    [s=>{s.telemetry.automatic=false;},['Returning fan to automatic…','No action needed.']],
    [s=>{s.settings.assign_tdp_presets=false;},['Automatic','Uses your saved global curve.']],
  ];
  for(const [change,[headline,detail],startingFor] of cases){const r=status(change,startingFor);assert.deepEqual([r.headline,r.detail||undefined],[headline,detail],headline);}
  assert.equal(late.exports.fanSummary(undefined),'APU — · — RPM · Reading fan…');
  // Own ON/OFF bumps the revision without CAS; unsaved drafts (current and other scopes) must stay saveable.
  const on=json(initial);on.settings.enabled=true;on.telemetry={active:true,automatic:false};
  const own=harness(on);own.render();await flush();let ot=own.render();
  find(ot,n=>n.type===own.exports.CurveGraph).props.edit(1,30,0);ot=own.render();
  label(ot,'Curve').props.onChange({data:'silent'});ot=own.render();
  find(ot,n=>n.type===own.exports.CurveGraph).props.edit(1,35,0);ot=own.render();
  assert.equal(label(ot,'Enable Fan Control').props.disabled,false,'Turning off is never blocked by an unsaved curve');
  label(ot,'Enable Fan Control').props.onChange(false);await flush();ot=own.render();
  assert.equal(own.getState().settings.enabled,false);
  button(ot,'Assign curve to Silent').props.onClick();await flush();ot=own.render();
  assert.equal(own.getState().settings.presets.silent?.[0][1],35,'Own disable must not make the current draft unsaveable');
  label(ot,'Curve').props.onChange({data:'global'});ot=own.render();
  button(ot,'Save global curve').props.onClick();await flush();ot=own.render();
  assert.equal(own.getState().settings.global_curve[0][1],30,'Own disable must not make other scope drafts unsaveable');own.unmount();
  // A foreign curve save the panel has not polled yet is still refused after the toggle.
  const raced=harness(on);raced.render();await flush();let rt=raced.render();
  find(rt,n=>n.type===raced.exports.CurveGraph).props.edit(1,30,0);rt=raced.render();
  raced.foreign(s=>{s.settings.global_curve[0][1]=55;s.settings.revision++;});
  label(rt,'Enable Fan Control').props.onChange(false);await flush();rt=raced.render();
  button(rt,'Save global curve').props.onClick();await flush();rt=raced.render();
  assert.match(button(rt,'Save global curve').props.description,/CAS revision/,'A concurrent curve change is never overwritten');
  assert.equal(raced.getState().settings.global_curve[0][1],55);
  assert.equal(find(rt,n=>n.type===raced.exports.CurveGraph).props.curve[0][1],30,'The refused draft is preserved');raced.unmount();
  // A fault turns Fan Control off (revision +1, curves untouched) while the user edits: the draft stays saveable.
  const faulted=harness(on);faulted.render();await flush();let ft=faulted.render();
  find(ft,n=>n.type===faulted.exports.CurveGraph).props.edit(1,30,0);ft=faulted.render();
  faulted.foreign(s=>{s.settings.enabled=false;s.settings.revision++;});
  faulted.poll();await flush();ft=faulted.render();
  button(ft,'Save global curve').props.onClick();await flush();ft=faulted.render();
  assert.equal(faulted.getState().settings.global_curve[0][1],30,'A non-curve revision bump never strands the edit');
  assert.equal(button(ft,'Save global curve').props.description,undefined);faulted.unmount();
});
