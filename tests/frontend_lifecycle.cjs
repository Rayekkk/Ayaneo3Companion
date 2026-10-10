// Execute the actual TSX with controlled hooks, RPC completion and timers.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), ts = require(path.join(root, 'node_modules/typescript'));
const settle = async () => { for (let i = 0; i < 24; i++) await Promise.resolve(); };
function harness(component = 'Content', initialVisible = true) {
  const slots = [], effects = new Map(), pending = [], timers = new Map(), calls = [], toasts = [], callbacks = {};
  let cursor = 0, visible = initialVisible, nextTimer = 0, writes = 0, props = {}, backendApp = '';
  const Router = { MainRunningApp: null };
  const same = (a, b) => a && b && a.length === b.length && a.every((v, i) => v === b[i]);
  const hooks = {
    useState(value) { const i = cursor++; if (!(i in slots)) slots[i] = typeof value === 'function' ? value() : value;
      return [slots[i], value => { writes++; slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
    useRef(value) { const i = cursor++; return slots[i] ||= { current: value }; },
    useCallback(fn, deps) { const i = cursor++; if (!slots[i] || !same(slots[i].deps, deps)) slots[i] = { fn, deps }; return slots[i].fn; },
    useEffect(fn, deps) { const i = cursor++, old = effects.get(i); if (!old || !same(old.deps, deps)) pending.push(() => {
      old?.cleanup?.(); effects.set(i, { deps, cleanup: fn() });
    }); },
  };
  const jsx = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
  const callable = name => (...args) => new Promise((resolve, reject) => calls.push({ name, args, resolve, reject, settled: false }));
  const timer = (kind, fn, delay) => { const id = ++nextTimer; timers.set(id, { kind, fn, delay }); return id; };
  const modules = new Map();
  const load = file => {
    const fullPath = path.join(root, 'src', file);
    if (modules.has(fullPath)) return modules.get(fullPath).exports;
    const mod = { exports: {} };
    modules.set(fullPath, mod);
    const source = fs.readFileSync(fullPath, 'utf8') + (file === 'index.tsx' ? '\nexport { Content, AppWatcher };' : '');
    const output = ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX,
    } }).outputText;
    vm.runInNewContext(output, {
      module: mod, exports: mod.exports, console,
      setTimeout: (fn, delay) => timer('timeout', fn, delay), clearTimeout: id => timers.delete(id),
      setInterval: (fn, delay) => timer('interval', fn, delay), clearInterval: id => timers.delete(id),
      window: { SteamClient: { GameSessions: { RegisterForAppLifetimeNotifications(fn) {
        callbacks.lifetime = fn; return { unregister() {} };
      } } } },
      require(name) {
        if (name.startsWith('./')) return load(`${name.slice(2)}.tsx`);
        if (name === 'react') return hooks;
        if (name === 'react/jsx-runtime') return jsx;
        if (name === '@decky/api') return { callable, definePlugin: fn => fn, useQuickAccessVisible: () => visible, toaster: { toast: value => toasts.push(value) } };
        if (name === '@decky/ui') return new Proxy({ Router, showModal() {} }, { get: (object, key) => key in object ? object[key] : key });
        throw Error(name);
      },
    });
    return mod.exports;
  };
  const plugin = load('index.tsx');
  const target = component === 'UpdateSection' ? load('updates.tsx') : component === 'FanPage' ? load('fan.tsx') : plugin;
  const render = (nextProps = props) => { props = nextProps; cursor = 0; const tree = target[component](props); pending.splice(0).forEach(fn => fn()); return tree; };
  return { render, calls, timers, callbacks, toasts, Router, watcher: plugin.AppWatcher,
    initialize: () => plugin.default(), get writes() { return writes; },
    visible(value) { visible = value; return render(); },
    fire(kind = 'interval', delay) { for (const [id, timer] of [...timers]) if (timer.kind === kind && (delay == null || timer.delay === delay)) {
      if (kind === 'timeout') timers.delete(id); timer.fn();
    } },
    respond(name, value) {
      const call = calls.find(c => c.name === name && !c.settled); assert.ok(call, `pending ${name}`);
      if (name === 'set_active_app') backendApp = call.args[0];
      if (value?.tdp && value.active_app_id === undefined) value = { ...value, active_app_id: backendApp };
      call.settled = true; call.resolve(value); return call;
    },
    reject(name) { const call = calls.find(c => c.name === name && !c.settled); assert.ok(call, `pending ${name}`); call.settled = true; call.reject(new Error('RPC failed')); },
    unmount() { for (const effect of effects.values()) effect.cleanup?.(); effects.clear(); },
    remount() { slots.length = 0; pending.length = 0; return render(); },
  };
}
function find(node, key, value) {
  if (!node || typeof node !== 'object') return;
  if (node.props?.[key] === value) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) { const result = find(child, key, value); if (result) return result; }
}
function findComponent(node, name) {
  if (!node || typeof node !== 'object') return;
  if (node.type?.name === name) return node;
  for (const child of Array.isArray(node) ? node : [node.props?.children]) {
    const result = findComponent(child, name); if (result) return result;
  }
}
const state = { supported: true, device: 'AYANEO 3', version: '1.0.2', tdp_backend: 'ryzenadj',
  tdp: { spl: 15, sppt: 17, fppt: 20 }, tdp_preset: 'Balanced', presets: {
    Balanced: { spl: 15, sppt: 17, fppt: 20 }, Performance: { spl: 25, sppt: 28, fppt: 30 }, Max: { spl: 30, sppt: 32, fppt: 35 },
  }, cpu_boost_supported: true, cpu_boost: false, controller: { vibration: 'medium', ff_gain: 60, rgb_mode: 'solid', color: 'ff0000', brightness: 70 },
  module_left: { label: 'Gamepad' }, module_right: { label: 'Gamepad' }, modules_connected: true,
};
const count = (h, name) => h.calls.filter(c => c.name === name).length;
const open = (h, title) => { const field = find(h.render(), 'title', title); assert.ok(field, title); field.props.onClick(); return h.render(); };
const ready = async () => { const h = harness(); h.render(); h.respond('get_state', state); await settle(); h.render(); return h; };
(async () => {
  for (const operation of ['save', 'delete']) for (const failed of [false, true]) {
    const profileWrite = harness(); profileWrite.Router.MainRunningApp = { appid: 111, display_name: 'A' };
    profileWrite.watcher.start(); profileWrite.respond('set_active_app'); await settle(); profileWrite.render();
    profileWrite.respond('get_state', state);
    profileWrite.respond('get_game_profile', { exists: true, profile: state.tdp, preset: 'Balanced' }); await settle(); open(profileWrite, 'TDP');
    if (operation === 'save') find(profileWrite.render(), 'children', 'Performance').props.onClick();
    else find(profileWrite.render(), 'label', 'Per Game Profile').props.onChange(false);
    await settle();
    profileWrite.visible(false); profileWrite.visible(true); profileWrite.render();
    assert.equal(count(profileWrite, 'get_game_profile'), 1,
      `${operation}: reopening QAM must not read the pre-commit profile during a pending write`);
    const writeName = operation === 'save' ? 'set_game_profile' : 'delete_game_profile';
    const saved = operation === 'save' && !failed;
    const finalState = { ...state, tdp: saved ? state.presets.Performance : state.tdp, tdp_preset: saved ? 'Performance' : 'Balanced' };
    if (failed) profileWrite.reject(writeName); else profileWrite.respond(writeName, finalState);
    await settle();
    assert.equal(count(profileWrite, 'get_game_profile'), 2,
      `${operation}: refresh the reopened profile after success or rollback without waiting for another menu open`);
    const exists = operation !== 'delete' || failed;
    profileWrite.respond('get_game_profile', { exists, profile: exists ? finalState.tdp : {}, preset: finalState.tdp_preset });
    profileWrite.respond('get_state', finalState); await settle(); open(profileWrite, 'TDP');
    assert.equal(find(profileWrite.render(), 'label', 'Per Game Profile').props.checked, exists);
    assert.equal(find(profileWrite.render(), 'label', 'Per Game Profile').props.disabled, false);
    profileWrite.unmount(); profileWrite.watcher.stop();
  }

  const slowProfile = harness(); slowProfile.Router.MainRunningApp = { appid: 111, display_name: 'A' };
  slowProfile.watcher.start(); slowProfile.render(); slowProfile.respond('get_state', { ...state, active_app_id: '111' }); await settle();
  for (let reopen = 0; reopen < 3; reopen++) { slowProfile.visible(false); slowProfile.visible(true); }
  assert.equal(count(slowProfile, 'get_game_profile'), 1,
    'QAM navigation must not accumulate concurrent slow game-profile reads');
  slowProfile.Router.MainRunningApp = { appid: 222, display_name: 'B' }; slowProfile.fire('interval', 2000); slowProfile.render();
  slowProfile.Router.MainRunningApp = { appid: 333, display_name: 'C' }; slowProfile.fire('interval', 2000); slowProfile.render();
  assert.equal(count(slowProfile, 'get_game_profile'), 1, 'game switches wait for the pending read without adding workers');
  slowProfile.reject('get_game_profile'); await settle(); slowProfile.render();
  assert.equal(count(slowProfile, 'get_game_profile'), 2, 'the latest game is read immediately after an obsolete failure');
  assert.equal(slowProfile.calls.filter(c => c.name === 'get_game_profile')[1].args[0], '333');
  assert.equal(find(slowProfile.render(), 'label', 'Action failed'), undefined, 'obsolete profile errors stay hidden');
  assert.equal(find(slowProfile.render(), 'label', 'Game profile lookup failed'), undefined, 'obsolete reads cannot set the dedicated profile error');
  slowProfile.respond('get_game_profile', { exists: true, profile: state.tdp, preset: 'Balanced' }); await settle();
  slowProfile.visible(false); slowProfile.visible(true);
  assert.equal(count(slowProfile, 'get_game_profile'), 3);
  slowProfile.unmount(); const profileWritesAfterUnmount = slowProfile.writes;
  slowProfile.reject('get_game_profile'); await settle(); slowProfile.fire('timeout', 3000);
  assert.equal(count(slowProfile, 'get_game_profile'), 3, 'unmounted profile lookups cannot retry');
  assert.equal(slowProfile.writes, profileWritesAfterUnmount);
  slowProfile.watcher.stop();

  const slowBattery = await ready(); open(slowBattery, 'Battery');
  for (let reopen = 0; reopen < 3; reopen++) {
    slowBattery.visible(false); slowBattery.visible(true); open(slowBattery, 'Battery');
    slowBattery.fire('interval', 10000);
  }
  assert.equal(count(slowBattery, 'get_battery_status'), 1,
    'closing and reopening Battery must not accumulate concurrent slow RPC reads');
  slowBattery.respond('get_battery_status', { available: true, percent: 10, status: 'Discharging', seconds_to_full: null }); await settle();
  assert.equal(count(slowBattery, 'get_battery_status'), 2,
    'the reopened page needs one fresh read once the obsolete request finishes');
  let slowBatteryPage = findComponent(slowBattery.render(), 'BatteryPage');
  assert.equal(slowBatteryPage.props.battery, null, 'an obsolete battery result must stay discarded');
  slowBattery.respond('get_battery_status', { available: true, percent: 60, status: 'Charging', seconds_to_full: 3600 }); await settle();
  slowBatteryPage = findComponent(slowBattery.render(), 'BatteryPage');
  assert.equal(slowBatteryPage.props.battery.percent, 60);
  slowBattery.fire('interval', 10000);
  slowBattery.visible(false); slowBattery.unmount();
  const batteryCallsAtUnmount = count(slowBattery, 'get_battery_status');
  const batteryWritesAtUnmount = slowBattery.writes;
  slowBattery.reject('get_battery_status'); await settle();
  assert.equal(count(slowBattery, 'get_battery_status'), batteryCallsAtUnmount, 'unmount must not restart a battery read');
  assert.equal(slowBattery.writes, batteryWritesAtUnmount, 'unmount must suppress late battery errors');

  const failedBattery = await ready(); open(failedBattery, 'Battery');
  const batteryTree = h => { const page = findComponent(h.render(), 'BatteryPage'); return page.type(page.props); };
  failedBattery.reject('get_battery_status'); await settle();
  assert.ok(find(batteryTree(failedBattery), 'label', 'Battery status unavailable'),
    'an initial battery read failure must replace the indefinite spinner');
  failedBattery.fire('interval', 10000);
  failedBattery.respond('get_battery_status', { available: true, percent: 65, status: 'Charging', seconds_to_full: 3600, power_w: 15, source: 'sysfs' }); await settle();
  assert.equal(find(batteryTree(failedBattery), 'label', 'Battery status unavailable'), undefined);
  failedBattery.fire('interval', 10000); failedBattery.reject('get_battery_status'); await settle();
  assert.ok(find(batteryTree(failedBattery), 'label', 'Battery refresh failed'),
    'cached battery values must be marked after their refresh fails');
  assert.ok(find(batteryTree(failedBattery), 'label', '65% · Charging'));
  failedBattery.fire('interval', 10000);
  failedBattery.respond('get_battery_status', { available: true, percent: 70, status: 'Charging', seconds_to_full: 3000, power_w: 15, source: 'sysfs' }); await settle();
  assert.equal(find(batteryTree(failedBattery), 'label', 'Battery refresh failed'), undefined);
  failedBattery.unmount();

  const obsoleteBattery = await ready(); open(obsoleteBattery, 'Battery');
  obsoleteBattery.visible(false); obsoleteBattery.visible(true); open(obsoleteBattery, 'Battery');
  obsoleteBattery.reject('get_battery_status'); await settle();
  assert.equal(find(batteryTree(obsoleteBattery), 'label', 'Battery status unavailable'), undefined,
    'a late failure from the closed battery page must not overwrite its new read');
  const reopenedBatteryRead = obsoleteBattery.calls.filter(call => call.name === 'get_battery_status' && !call.settled).at(-1);
  reopenedBatteryRead.settled = true;
  reopenedBatteryRead.resolve({ available: true, percent: 50, status: 'Discharging', seconds_to_full: null, power_w: 10, source: 'none' }); await settle();
  assert.ok(find(batteryTree(obsoleteBattery), 'label', '50% · Discharging'));
  obsoleteBattery.unmount();

  const failedStatus = await ready();
  failedStatus.fire('interval', 1500);
  failedStatus.reject('get_state'); await settle();
  assert.ok(find(failedStatus.render(), 'label', 'Status refresh failed'), 'a failed poll must identify cached values as unverified');
  assert.ok(find(failedStatus.render(), 'title', 'TDP'), 'cached controls remain reachable during a transient status failure');
  open(failedStatus, 'Vibration');
  assert.ok(find(failedStatus.render(), 'label', 'Status refresh failed'), 'status failure remains visible on control pages');
  failedStatus.fire('interval', 1500);
  failedStatus.respond('get_state', state); await settle();
  assert.equal(find(failedStatus.render(), 'label', 'Status refresh failed'), undefined, 'a fresh successful poll clears the read error');
  failedStatus.unmount();

  const obsoleteStatus = await ready();
  obsoleteStatus.fire('interval', 1500);
  obsoleteStatus.visible(false); obsoleteStatus.visible(true);
  obsoleteStatus.reject('get_state'); await settle();
  assert.equal(find(obsoleteStatus.render(), 'label', 'Status refresh failed'), undefined, 'an obsolete poll failure cannot describe the reopened panel');
  obsoleteStatus.respond('get_state', state); await settle(); obsoleteStatus.unmount();

  const batchedTdp = await ready(); open(batchedTdp, 'TDP');
  find(batchedTdp.render(), 'children', 'Custom').props.onClick();
  const tdpControls = batchedTdp.render();
  find(tdpControls, 'label', 'SPL (TDP) - 15 W').props.onChange(20);
  find(tdpControls, 'label', 'SPPT +2 W  =  17 W').props.onChange(4);
  find(tdpControls, 'label', 'FPPT +5 W  =  20 W').props.onChange(7);
  assert.ok(find(batchedTdp.render(), 'label', 'SPL (TDP) - 20 W'), 'batched sibling edits preserve the latest SPL');
  find(tdpControls, 'children', 'Apply TDP').props.onClick(); await settle();
  assert.deepEqual({ ...batchedTdp.calls.find(call => call.name === 'set_tdp').args[0] },
    { spl: 20, sppt: 24, fppt: 27 }, 'Apply uses all edits even when its callback came from an earlier render');
  batchedTdp.respond('set_tdp', { ...state, tdp: { spl: 20, sppt: 24, fppt: 27 }, tdp_preset: 'Custom' }); await settle();
  batchedTdp.unmount();

  const tdpHeadroom = await ready(); open(tdpHeadroom, 'TDP');
  find(tdpHeadroom.render(), 'children', 'Custom').props.onClick();
  const headroomControls = tdpHeadroom.render();
  find(headroomControls, 'label', 'SPL (TDP) - 15 W').props.onChange(35);
  find(headroomControls, 'label', 'SPPT +2 W  =  17 W').props.onChange(10);
  find(headroomControls, 'label', 'FPPT +5 W  =  20 W').props.onChange(1);
  find(headroomControls, 'children', 'Apply TDP').props.onClick(); await settle();
  assert.deepEqual({ ...tdpHeadroom.calls.find(call => call.name === 'set_tdp').args[0] },
    { spl: 35, sppt: 36, fppt: 36 }, 'batched edits use current headroom and preserve SPL <= SPPT <= FPPT <= 37');
  tdpHeadroom.respond('set_tdp', { ...state, tdp: { spl: 35, sppt: 36, fppt: 36 }, tdp_preset: 'Custom' }); await settle();
  tdpHeadroom.unmount();

  const failedDraftPreset = await ready(); open(failedDraftPreset, 'TDP');
  find(failedDraftPreset.render(), 'children', 'Custom').props.onClick();
  find(failedDraftPreset.render(), 'label', 'SPL (TDP) - 15 W').props.onChange(21);
  find(failedDraftPreset.render(), 'children', 'Max').props.onClick(); await settle();
  failedDraftPreset.reject('set_tdp'); await settle();
  failedDraftPreset.respond('get_state', state); await settle();
  assert.ok(find(failedDraftPreset.render(), 'label', 'SPL (TDP) - 21 W'), 'failed preset and subsequent poll preserve the previous unapplied draft');
  find(failedDraftPreset.render(), 'children', 'Apply TDP').props.onClick(); await settle();
  assert.equal(failedDraftPreset.calls.filter(call => call.name === 'set_tdp')[1].args[0].spl, 21);
  failedDraftPreset.respond('set_tdp', { ...state, tdp: { spl: 21, sppt: 23, fppt: 26 }, tdp_preset: 'Custom' }); await settle();
  failedDraftPreset.unmount();

  const editAfterApply = await ready(); open(editAfterApply, 'TDP');
  find(editAfterApply.render(), 'children', 'Custom').props.onClick();
  const applyingControls = editAfterApply.render();
  find(applyingControls, 'label', 'SPL (TDP) - 15 W').props.onChange(20);
  find(applyingControls, 'children', 'Apply TDP').props.onClick();
  find(applyingControls, 'label', 'SPL (TDP) - 15 W').props.onChange(30); await settle();
  assert.ok(find(editAfterApply.render(), 'label', 'SPL (TDP) - 20 W'), 'an old enabled callback cannot edit a draft while Apply is pending');
  editAfterApply.respond('set_tdp', { ...state, tdp: { spl: 20, sppt: 22, fppt: 25 }, tdp_preset: 'Custom' }); await settle();
  editAfterApply.unmount();

  const returningGain = await ready(); open(returningGain, 'Vibration');
  const gainCallbacks = findComponent(returningGain.render(), 'VibrationPage').props;
  gainCallbacks.onGain(40); gainCallbacks.onGain(60); returningGain.fire('timeout'); await settle();
  assert.equal(returningGain.calls.find(call => call.name === 'set_vibration_gain').args[0], 60,
    'returning to the original gain in one frame cancels the intermediate choice');
  returningGain.respond('set_vibration_gain', state); await settle(); returningGain.unmount();

  const returningStrength = await ready(); open(returningStrength, 'Vibration');
  const strengthCallbacks = findComponent(returningStrength.render(), 'VibrationPage').props;
  strengthCallbacks.onLevel(3); strengthCallbacks.onLevel(2); returningStrength.fire('timeout'); await settle();
  assert.equal(returningStrength.calls.find(call => call.name === 'set_controller_with_vibration_feedback').args[0].vibration, 'medium',
    'returning to the original strength in one frame cancels the intermediate choice');
  returningStrength.respond('set_controller_with_vibration_feedback', state); await settle(); returningStrength.unmount();

  const instantFanApply = harness('FanPage');
  const manualSpeeds = [];
  const fanControls = instantFanApply.render({ status: { supported: true, mode: 'auto', percent: null, rpm: 2500 },
    available: true, error: '', busy: false, onApply: (mode, speed) => manualSpeeds.push([mode, speed]) });
  find(fanControls, 'label', 'Manual speed').props.onChange(75);
  find(fanControls, 'title', 'Enable Manual Control').props.onClick();
  assert.deepEqual(manualSpeeds, [['manual', 75]], 'Apply Manual Speed uses the slider edit made before the next render');
  instantFanApply.unmount();

  const rapidLimits = await ready(); open(rapidLimits, 'TDP');
  find(rapidLimits.render(), 'children', 'Custom').props.onClick();
  const rapidControls = rapidLimits.render();
  const moveSpl = find(rapidControls, 'label', 'SPL (TDP) - 15 W').props.onChange;
  const moveSlow = find(rapidControls, 'label', 'SPPT +2 W  =  17 W').props.onChange;
  const moveFast = find(rapidControls, 'label', 'FPPT +5 W  =  20 W').props.onChange;
  let seed = 12345, latestSpl = 15;
  for (let i = 0; i < 300; i++) {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    const kind = seed % 3, value = (seed >>> 8) % 50;
    if (kind === 0) { moveSpl(value); latestSpl = Math.max(5, Math.min(35, value)); }
    else if (kind === 1) moveSlow(value);
    else moveFast(value);
    const limits = find(rapidLimits.render(), 'title', 'Current TDP').props.children.props.children.props.label;
    const [spl, sppt, fppt] = limits.match(/\d+/g).map(Number);
    assert.equal(spl, latestSpl, 'offset changes in any order cannot change the last SPL choice');
    assert.ok(5 <= spl && spl <= 35 && spl <= sppt && sppt <= fppt && fppt <= 37, 'every interleaving keeps the three hardware limits ordered and capped');
  }
  moveSpl(NaN); moveSlow(Infinity); moveFast(-Infinity);
  const beforeApply = find(rapidLimits.render(), 'title', 'Current TDP').props.children.props.children.props.label;
  find(rapidControls, 'children', 'Apply TDP').props.onClick(); await settle();
  const rapidValue = rapidLimits.calls.find(call => call.name === 'set_tdp').args[0];
  assert.equal(`${rapidValue.spl} / ${rapidValue.sppt} / ${rapidValue.fppt} W`, beforeApply, 'invalid events and a stale Apply callback cannot change the displayed draft');
  assert.equal(count(rapidLimits, 'set_tdp'), 1, '300 draft changes produce only the explicitly requested hardware write');
  rapidLimits.respond('set_tdp', { ...state, tdp: rapidValue, tdp_preset: 'Custom' }); await settle(); rapidLimits.unmount();

  const checkingAudio = harness(); checkingAudio.render();
  checkingAudio.respond('get_state', { ...state, audio_fix_enabled: true, audio_fix_installed: true,
    audio_fix_supported: false, audio_calibration_available: false, audio_profile: 'Pending' }); await settle(); open(checkingAudio, 'Audio');
  const checkingPage = findComponent(checkingAudio.render(), 'AudioPage');
  const checkingTree = checkingPage.type(checkingPage.props);
  assert.ok(find(checkingTree, 'description', 'Waiting to verify the speaker profile.'), 'installed files alone do not confirm the actual speaker profile');
  assert.equal(find(checkingTree, 'title', 'Speaker Calibration'), undefined, 'calibration remains unavailable until DSP readback');
  checkingAudio.unmount();

  const polling = harness('Content', false); polling.render(); assert.equal(polling.calls.length, 0, 'hidden content does not read hardware');
  polling.visible(true); polling.fire(); polling.fire(); assert.equal(count(polling, 'get_state'), 1, 'polling is single flight');
  polling.visible(false); let writes = polling.writes; polling.respond('get_state', state); await settle();
  assert.equal(polling.writes, writes, 'hidden response ignored');
  polling.visible(true); polling.unmount(); writes = polling.writes; polling.respond('get_state', state); await settle();
  assert.equal(polling.writes, writes, 'unmounted response ignored');

  const cached = await ready(); cached.unmount();
  assert.ok(find(cached.remount(), 'title', 'TDP'), 'remount immediately shows confirmed summaries'); cached.unmount();

  const mutation = await ready(); open(mutation, 'TDP'); mutation.fire();
  find(mutation.render(), 'label', 'CPU Boost').props.onChange(true); await settle();
  mutation.respond('set_cpu_boost', { ...state, cpu_boost: true }); await settle();
  mutation.respond('get_state', state); await settle();
  assert.equal(find(mutation.render(), 'label', 'CPU Boost').props.checked, true, 'older poll cannot revert a saved toggle');
  mutation.respond('get_state', { ...state, cpu_boost: true }); await settle();
  find(mutation.render(), 'children', 'Custom').props.onClick(); await settle(); mutation.fire();
  mutation.respond('get_state', { ...state, cpu_boost: true }); await settle();
  assert.ok(find(mutation.render(), 'label', 'SPL (TDP) - 15 W'), 'polling preserves chosen Custom editor');
  find(mutation.render(), 'label', 'SPL (TDP) - 15 W').props.onChange(21); mutation.render();
  find(mutation.render(), 'label', 'CPU Boost').props.onChange(false); await settle(); mutation.respond('set_cpu_boost', state); await settle();
  assert.ok(find(mutation.render(), 'label', 'SPL (TDP) - 21 W'), 'unrelated action preserves unapplied TDP draft');
  mutation.unmount();

  const partialAck = harness(); partialAck.render();
  partialAck.respond('get_state', { ...state, cpu_boost: true, charge_bypass: false,
    charge_bypass_supported: true, tdp_backend: 'PowerStation', tdp_supported: false,
    tdp_error: 'PowerStation is active' }); await settle();
  open(partialAck, 'Battery');
  findComponent(partialAck.render(), 'BatteryPage').props.onBypass(true); await settle();
  partialAck.respond('set_charge_bypass', { ...state, cpu_boost: false, charge_bypass: true,
    charge_bypass_supported: true, tdp_backend: 'RyzenAdj', tdp_supported: true }); await settle();
  assert.equal(findComponent(partialAck.render(), 'BatteryPage').props.state.charge_bypass, true, 'acknowledged control updates immediately');
  findComponent(partialAck.render(), 'SectionHeader').props.onBack(); open(partialAck, 'TDP');
  assert.equal(find(partialAck.render(), 'label', 'CPU Boost').props.checked, true, 'a fast unrelated acknowledgement cannot replace a fresher CPU observation');
  assert.ok(find(partialAck.render(), 'label', 'TDP control unavailable'), 'an acknowledgement cannot replace live diagnostic capabilities');
  partialAck.respond('get_state', { ...state, cpu_boost: false, charge_bypass: true,
    charge_bypass_supported: true, tdp_backend: 'PowerStation', tdp_supported: false }); await settle();
  assert.equal(find(partialAck.render(), 'label', 'CPU Boost').props.checked, false, 'the next actual status read can still report external drift');
  partialAck.unmount();

  const changingGameAck = harness(); changingGameAck.Router.MainRunningApp = { appid: 111, display_name: 'A' };
  changingGameAck.watcher.start(); changingGameAck.respond('set_active_app'); await settle(); changingGameAck.render();
  changingGameAck.respond('get_state', state);
  changingGameAck.respond('get_game_profile', { exists: false, profile: {} }); await settle();
  open(changingGameAck, 'TDP'); find(changingGameAck.render(), 'label', 'CPU Boost').props.onChange(true); await settle();
  changingGameAck.Router.MainRunningApp = { appid: 222, display_name: 'B' };
  changingGameAck.watcher.check(); changingGameAck.render();
  changingGameAck.respond('set_cpu_boost', { ...state, active_app_id: '111', cpu_boost: true }); await settle();
  assert.equal(find(changingGameAck.render(), 'label', 'CPU Boost').props.checked, true,
    'an independent write acknowledgement stays visible when the running game changes');
  assert.equal(find(changingGameAck.render(), 'label', 'Per Game Profile').props.disabled, true,
    'accepting an independent acknowledgement must not unlock an unconfirmed new game');
  assert.deepEqual(changingGameAck.calls.find(call => call.name === 'set_cpu_boost').args, [true]);
  changingGameAck.unmount(); changingGameAck.watcher.stop();

  const pendingTest = await ready(); open(pendingTest, 'Vibration');
  findComponent(pendingTest.render(), 'VibrationPage').props.onLevel(3);
  findComponent(pendingTest.render(), 'VibrationPage').props.onTest(); await settle();
  assert.equal(count(pendingTest, 'test_vibration'), 0, 'a vibration test waits for the debounced firmware setting');
  assert.equal(count(pendingTest, 'set_controller_with_vibration_feedback'), 1, 'test flushes the pending setting immediately');
  pendingTest.respond('set_controller_with_vibration_feedback', { ...state, controller: { ...state.controller, vibration: 'high' } }); await settle();
  assert.equal(count(pendingTest, 'test_vibration'), 1, 'a test starts after the selected strength has been confirmed');
  let pendingVibration = findComponent(pendingTest.render(), 'VibrationPage');
  assert.equal(find(pendingVibration.type(pendingVibration.props), 'label', 'FF Gain').props.disabled, true,
    'FF Gain cannot queue changes during the test');
  pendingTest.respond('test_vibration', { success: true }); await settle(); pendingTest.unmount();

  const failedTest = await ready(); open(failedTest, 'Vibration');
  findComponent(failedTest.render(), 'VibrationPage').props.onGain(30);
  findComponent(failedTest.render(), 'VibrationPage').props.onTest(); await settle();
  assert.equal(count(failedTest, 'set_vibration_gain'), 1, 'test flushes pending gain');
  failedTest.reject('set_vibration_gain'); await settle();
  assert.equal(count(failedTest, 'test_vibration'), 0, 'a failed setting must not play using the previous gain');
  assert.equal(findComponent(failedTest.render(), 'VibrationPage').props.testing, false);
  assert.equal(count(failedTest, 'get_state'), 2, 'failure immediately refreshes the restored controls after the test releases its action guard');
  failedTest.respond('get_state', state); await settle();
  assert.equal(findComponent(failedTest.render(), 'VibrationPage').props.state.controller.ff_gain, 60);
  failedTest.unmount();

  const queuedTest = await ready(); open(queuedTest, 'Vibration');
  findComponent(queuedTest.render(), 'VibrationPage').props.onGain(40); queuedTest.fire('timeout'); await settle();
  findComponent(queuedTest.render(), 'VibrationPage').props.onLevel(3);
  findComponent(queuedTest.render(), 'VibrationPage').props.onTest(); await settle();
  assert.equal(count(queuedTest, 'test_vibration'), 0, 'test waits for an in-flight write and its queued successor');
  queuedTest.respond('set_vibration_gain', { ...state, controller: { ...state.controller, ff_gain: 40 } }); await settle();
  assert.equal(count(queuedTest, 'test_vibration'), 0, 'finishing the first write cannot bypass the queued latest strength');
  queuedTest.respond('set_controller_with_vibration_feedback', { ...state, controller: { ...state.controller, ff_gain: 40, vibration: 'high' } }); await settle();
  assert.equal(count(queuedTest, 'test_vibration'), 1);
  queuedTest.respond('test_vibration', { success: true }); await settle(); queuedTest.unmount();

  const failedStrength = await ready(); open(failedStrength, 'Vibration');
  findComponent(failedStrength.render(), 'VibrationPage').props.onLevel(3); failedStrength.fire('timeout'); await settle();
  findComponent(failedStrength.render(), 'VibrationPage').props.onGain(30);
  findComponent(failedStrength.render(), 'VibrationPage').props.onTest(); await settle();
  failedStrength.reject('set_controller_with_vibration_feedback'); await settle();
  failedStrength.respond('set_vibration_gain', { ...state, controller: { ...state.controller, ff_gain: 30 } }); await settle();
  assert.equal(count(failedStrength, 'test_vibration'), 0, 'a successful gain patch cannot confirm a failed strength in the same drain');
  assert.equal(findComponent(failedStrength.render(), 'VibrationPage').props.testing, false);
  failedStrength.respond('get_state', { ...state, controller: { ...state.controller, ff_gain: 30 } }); await settle(); failedStrength.unmount();

  const failedGain = await ready(); open(failedGain, 'Vibration');
  findComponent(failedGain.render(), 'VibrationPage').props.onGain(30); failedGain.fire('timeout'); await settle();
  findComponent(failedGain.render(), 'VibrationPage').props.onLevel(3);
  findComponent(failedGain.render(), 'VibrationPage').props.onTest(); await settle();
  failedGain.reject('set_vibration_gain'); await settle();
  failedGain.respond('set_controller_with_vibration_feedback', { ...state, controller: { ...state.controller, vibration: 'high' } }); await settle();
  assert.equal(count(failedGain, 'test_vibration'), 0, 'a successful strength patch cannot confirm a failed gain');
  failedGain.respond('get_state', { ...state, controller: { ...state.controller, vibration: 'high' } }); await settle(); failedGain.unmount();

  const supersededStrength = await ready(); open(supersededStrength, 'Vibration');
  findComponent(supersededStrength.render(), 'VibrationPage').props.onLevel(3); supersededStrength.fire('timeout'); await settle();
  findComponent(supersededStrength.render(), 'VibrationPage').props.onLevel(1);
  findComponent(supersededStrength.render(), 'VibrationPage').props.onTest(); await settle();
  supersededStrength.reject('set_controller_with_vibration_feedback'); await settle();
  supersededStrength.respond('set_controller_with_vibration_feedback', { ...state, controller: { ...state.controller, vibration: 'low' } }); await settle();
  assert.equal(count(supersededStrength, 'test_vibration'), 1, 'a confirmed newer edit of the failed field permits the test');
  supersededStrength.respond('test_vibration', { success: true }); await settle(); supersededStrength.unmount();

  const independentFan = await ready();
  independentFan.respond('get_fan_status', { supported: true, mode: 'manual', percent: 80, rpm: 3000 }); await settle();
  open(independentFan, 'TDP'); find(independentFan.render(), 'children', 'Max').props.onClick(); await settle();
  findComponent(independentFan.render(), 'SectionHeader').props.onBack(); open(independentFan, 'Fan Control');
  const availableFan = findComponent(independentFan.render(), 'FanPage');
  assert.equal(availableFan.props.busy, false, 'an unrelated pending TDP write does not disable the fan page');
  availableFan.props.onApply('auto'); await settle();
  assert.equal(count(independentFan, 'set_fan'), 1, 'the panel can request Auto while TDP is pending');
  independentFan.respond('set_fan', { supported: true, mode: 'auto', percent: null, rpm: 3000 }); await settle();
  independentFan.respond('set_tdp', { ...state, tdp: state.presets.Max, tdp_preset: 'Max' }); await settle(); independentFan.unmount();

  const closingTest = await ready(); open(closingTest, 'Vibration');
  findComponent(closingTest.render(), 'VibrationPage').props.onGain(40);
  findComponent(closingTest.render(), 'VibrationPage').props.onTest(); await settle(); closingTest.unmount();
  const beforeClosingAck = closingTest.writes;
  closingTest.respond('set_vibration_gain', { ...state, controller: { ...state.controller, ff_gain: 40 } }); await settle();
  assert.equal(count(closingTest, 'test_vibration'), 0, 'leaving an unmounted panel cannot start a delayed test');
  assert.equal(closingTest.writes, beforeClosingAck);

  const rgb = await ready(); const plugin = rgb.initialize(); assert.equal(plugin.alwaysRender, true, 'Decky preserves content behind native dropdown');
  open(rgb, 'RGB'); const hue = find(rgb.render(), 'label', 'Hue'), brightness = find(rgb.render(), 'label', 'Brightness');
  hue.props.onChange(120); hue.props.onChangeEnd(120); brightness.props.onChange(42); brightness.props.onChangeEnd(42);
  assert.equal(count(rgb, 'set_controller'), 0, 'slider movement is debounced');
  rgb.visible(false); assert.equal(count(rgb, 'set_controller'), 1, 'closing QAM flushes the latest RGB edit');
  const rgbCall = rgb.calls.find(c => c.name === 'set_controller');
  assert.equal(rgbCall.args[0].color, '00ff00'); assert.equal(rgbCall.args[0].brightness, 42, 'same-frame sibling slider edits merge');
  rgb.unmount(); writes = rgb.writes; rgb.respond('set_controller', { ...state, controller: { ...state.controller, ...rgbCall.args[0] } }); await settle();
  assert.equal(rgb.writes, writes, 'pending controller save completes without touching an unmounted view'); plugin.onDismount();

  const queue = await ready(); open(queue, 'RGB'); find(queue.render(), 'label', 'LED Mode').props.onChange({ data: 'pulse' }); queue.fire('timeout');
  find(queue.render(), 'label', 'LED Mode').props.onChange({ data: 'rainbow' }); queue.unmount();
  assert.equal(count(queue, 'set_controller'), 1, 'controller writes do not overlap');
  writes = queue.writes; queue.respond('set_controller', { ...state, controller: { ...state.controller, rgb_mode: 'pulse' } }); await settle();
  assert.equal(count(queue, 'set_controller'), 2, 'queued latest edit survives unmount during an earlier write');
  queue.respond('set_controller', { ...state, controller: { ...state.controller, rgb_mode: 'rainbow' } }); await settle(); assert.equal(queue.writes, writes);

  const remounted = await ready(); open(remounted, 'RGB');
  find(remounted.render(), 'label', 'Hue').props.onChange(120);
  find(remounted.render(), 'label', 'Hue').props.onChangeEnd(120);
  remounted.unmount(); await settle();
  const oldEdit = remounted.calls.find(call => call.name === 'set_controller');
  assert.deepEqual({ ...oldEdit.args[0] }, { color: '00ff00' }, 'color edit omits unrelated fields');
  remounted.remount(); remounted.respond('get_state', state); await settle(); open(remounted, 'RGB');
  find(remounted.render(), 'label', 'Brightness').props.onChange(40);
  find(remounted.render(), 'label', 'Brightness').props.onChangeEnd(40);
  remounted.fire('timeout'); await settle();
  const newEdit = remounted.calls.filter(call => call.name === 'set_controller')[1];
  assert.deepEqual({ ...newEdit.args[0] }, { brightness: 40 }, 'a stale remounted view cannot overwrite the earlier pending color');
  writes = remounted.writes;
  remounted.respond('set_controller', { ...state, controller: { ...state.controller, color: '00ff00' } }); await settle();
  assert.equal(remounted.writes, writes, 'earlier unmounted view cannot render its acknowledgement');
  remounted.respond('set_controller', { ...state, controller: { ...state.controller, color: '00ff00', brightness: 40 } }); await settle();
  assert.ok(find(remounted.render(), 'label', '#00FF00'), 'merged backend acknowledgement retains both edits'); remounted.unmount();

  const saturation = await ready(); open(saturation, 'RGB');
  find(saturation.render(), 'label', 'Saturation').props.onChange(50);
  find(saturation.render(), 'label', 'Saturation').props.onChangeEnd(50);
  saturation.fire('timeout'); await settle();
  const saturationEdit = saturation.calls.find(call => call.name === 'set_controller');
  assert.deepEqual(Object.keys(saturationEdit.args[0]), ['color'], 'saturation cannot replace a newer brightness or gain');
  saturation.respond('set_controller', { ...state, controller: { ...state.controller, ...saturationEdit.args[0] } }); await settle(); saturation.unmount();

  const dropdown = await ready(); open(dropdown, 'RGB'); const menu = find(dropdown.render(), 'label', 'LED Mode');
  menu.props.onMenuWillOpen(() => dropdown.visible(false)); menu.props.onChange({ data: 'pulse' }); dropdown.fire('timeout');
  dropdown.visible(true); assert.ok(find(dropdown.render(), 'label', 'LED Mode'), 'native dropdown returns to the same section'); dropdown.unmount();

  const watcher = harness(); watcher.Router.MainRunningApp = { appid: 111, display_name: 'A' }; watcher.watcher.start();
  watcher.callbacks.lifetime(); const late = [...watcher.timers.values()].find(t => t.kind === 'timeout').fn;
  watcher.watcher.stop(); assert.equal(watcher.timers.size, 0); late(); assert.equal(count(watcher, 'set_active_app'), 1);
  watcher.watcher.start(); watcher.respond('set_active_app'); await settle(); watcher.fire();
  assert.equal(count(watcher, 'set_active_app'), 2, 'old lifecycle completion cannot clear the new in-flight guard'); watcher.watcher.stop();

  const slowGame = harness(); const observedGames = [];
  slowGame.Router.MainRunningApp = { appid: 111, display_name: 'A' }; slowGame.watcher.start();
  slowGame.watcher.listen(game => observedGames.push(game?.appId ?? ''));
  slowGame.Router.MainRunningApp = { appid: 222, display_name: 'B' }; slowGame.fire('interval', 2000);
  assert.equal(slowGame.watcher.currentGame().appId, '222', 'a slow profile RPC cannot freeze local game detection');
  slowGame.Router.MainRunningApp = { appid: 333, display_name: 'C' }; slowGame.callbacks.lifetime(); slowGame.fire('timeout', 300);
  assert.deepEqual(observedGames, ['222', '333'], 'listeners see every observed game while the previous RPC is pending');
  assert.equal(count(slowGame, 'set_active_app'), 1, 'profile writes remain single flight');
  slowGame.respond('set_active_app'); await settle();
  assert.equal(count(slowGame, 'set_active_app'), 2, 'latest game is sent immediately after the old RPC settles');
  assert.equal(slowGame.calls.filter(call => call.name === 'set_active_app')[1].args[0], '333', 'intermediate profiles are coalesced');
  slowGame.respond('set_active_app'); await settle(); slowGame.watcher.stop();

  const failedGame = harness(); failedGame.Router.MainRunningApp = { appid: 111 }; failedGame.watcher.start();
  failedGame.Router.MainRunningApp = null; failedGame.fire('interval', 2000);
  assert.equal(failedGame.watcher.currentGame(), null, 'game exit is observed during a pending RPC');
  failedGame.reject('set_active_app'); await settle();
  assert.equal(failedGame.calls.filter(call => call.name === 'set_active_app')[1].args[0], '', 'queued exit survives a failed profile RPC');
  failedGame.watcher.stop(); failedGame.respond('set_active_app'); await settle();
  assert.equal(count(failedGame, 'set_active_app'), 2, 'stopped watcher cannot start another queued update');

  const game = harness(); game.Router.MainRunningApp = { appid: 111, display_name: 'A' }; game.watcher.start(); game.respond('set_active_app'); await settle();
  game.render(); game.respond('get_state', state); game.respond('get_game_profile', { exists: true, profile: state.tdp, preset: 'Balanced' }); await settle();
  open(game, 'TDP'); find(game.render(), 'children', 'Performance').props.onClick(); await settle();
  const save = game.calls.find(c => c.name === 'set_game_profile'); assert.equal(save.args[0], '111'); assert.equal(save.args[3], '111', 'save captures expected app');
  game.Router.MainRunningApp = { appid: 222, display_name: 'B' }; game.fire(); game.render();
  game.respond('set_game_profile', { ...state, tdp: { spl: 26, sppt: 29, fppt: 31 }, tdp_preset: 'Performance' }); await settle();
  assert.notEqual(find(game.render(), 'label', '26 / 29 / 31 W')?.props.label, '26 / 29 / 31 W', 'old game save cannot replace the new editor');
  game.respond('set_active_app'); await settle();
  game.respond('get_game_profile', { exists: false, profile: {} }); game.respond('get_state', state); await settle(); game.render();
  find(game.render(), 'children', 'Max').props.onClick(); await settle();
  const global = game.calls.find(c => c.name === 'set_tdp'); assert.equal(global.args[2], '222', 'global edit is also guarded against app changes');
  game.unmount(); game.watcher.stop();

  const retry = harness(); retry.Router.MainRunningApp = { appid: 333, display_name: 'C' }; retry.watcher.start(); retry.respond('set_active_app'); await settle();
  retry.render(); retry.respond('get_state', state); await settle(); open(retry, 'TDP');
  retry.reject('get_game_profile'); await settle();
  assert.ok(find(retry.render(), 'label', 'Game profile lookup failed'), 'profile lookup failure is visible on the open TDP page');
  assert.equal(find(retry.render(), 'label', 'Per Game Profile').props.disabled, true);
  retry.fire('timeout', 3000);
  assert.equal(count(retry, 'get_game_profile'), 2, 'transient profile lookup retries without another menu open');
  retry.respond('get_game_profile', { exists: true, profile: state.tdp, preset: 'Balanced' }); await settle();
  assert.equal(JSON.stringify(retry.render()).includes('RPC failed'), false,
    'successful automatic profile retry clears its error without navigating away');
  assert.equal(find(retry.render(), 'label', 'Per Game Profile').props.checked, true);
  assert.equal(find(retry.render(), 'label', 'Per Game Profile').props.disabled, false);
  retry.unmount(); retry.watcher.stop();

  const profileError = harness(); profileError.Router.MainRunningApp = { appid: 111, display_name: 'A' };
  profileError.watcher.start(); profileError.respond('set_active_app'); await settle(); profileError.render();
  profileError.respond('get_state', state); profileError.reject('get_game_profile'); await settle();
  assert.ok(find(profileError.render(), 'label', 'Game profile lookup failed'), 'lookup failure also appears in the overview');
  open(profileError, 'TDP');
  assert.ok(find(profileError.render(), 'label', 'Game profile lookup failed'), 'navigation cannot dismiss an unresolved read failure');
  find(profileError.render(), 'label', 'CPU Boost').props.onChange(true); await settle();
  profileError.reject('set_cpu_boost'); await settle();
  profileError.fire('timeout', 3000); profileError.respond('get_game_profile', { exists: false, profile: {} }); await settle();
  assert.equal(find(profileError.render(), 'label', 'Game profile lookup failed'), undefined);
  assert.ok(JSON.stringify(profileError.render()).includes('RPC failed'), 'successful profile recovery preserves the separate CPU action failure');
  profileError.Router.MainRunningApp = { appid: 222, display_name: 'B' }; profileError.fire('interval', 2000); profileError.render();
  profileError.reject('get_game_profile'); await settle();
  assert.ok(find(profileError.render(), 'label', 'Game profile lookup failed'));
  profileError.Router.MainRunningApp = null; profileError.fire('interval', 2000); profileError.render(); profileError.render();
  assert.equal(find(profileError.render(), 'label', 'Game profile lookup failed'), undefined, 'game exit removes the obsolete profile error');
  profileError.unmount(); profileError.watcher.stop();

  const transition = harness(); transition.Router.MainRunningApp = { appid: 111, display_name: 'A' };
  transition.watcher.start(); transition.respond('set_active_app'); await settle(); transition.render();
  transition.respond('get_state', state);
  transition.respond('get_game_profile', { exists: true, profile: state.tdp, preset: 'Balanced' }); await settle(); transition.render();
  transition.Router.MainRunningApp = { appid: 222, display_name: 'B' };
  transition.fire('interval', 2000); transition.render(); transition.fire('interval', 1500);
  const maxTdp = { spl: 32, sppt: 35, fppt: 37 };
  transition.respond('get_game_profile', { exists: true, profile: maxTdp, preset: 'Max' }); await settle();
  transition.respond('get_state', { ...state, active_app_id: '111' }); await settle();
  open(transition, 'TDP');
  assert.equal(find(transition.render(), 'label', 'Per Game Profile').props.disabled, true, 'old-app state cannot unlock the new profile');
  transition.respond('set_active_app'); await settle(); transition.fire('interval', 1500);
  transition.respond('get_state', { ...state, active_app_id: '222', tdp: maxTdp, tdp_preset: 'Max' }); await settle();
  assert.equal(find(transition.render(), 'children', '> Max').props.disabled, true, 'matching app state initializes the Max selection');
  assert.equal(find(transition.render(), 'children', 'Balanced').props.disabled, false, 'stale Balanced state cannot initialize the new selection');
  assert.equal(find(transition.render(), 'label', 'Per Game Profile').props.disabled, false, 'only the matching app state unlocks edits');
  findComponent(transition.render(), 'SectionHeader').props.onBack();
  assert.equal(find(transition.render(), 'title', 'TDP').props.description, 'Max · 32 / 35 / 37 W');
  transition.unmount(); transition.watcher.stop();

  const failedContext = harness(); failedContext.Router.MainRunningApp = { appid: 777, display_name: 'Current game' };
  failedContext.watcher.start(); failedContext.reject('set_active_app'); await settle(); failedContext.render();
  failedContext.respond('get_game_profile', { exists: false, profile: {} });
  failedContext.respond('get_state', { ...state, active_app_id: '' }); await settle();
  assert.ok(find(failedContext.render(), 'title', 'Hardware Controls'), 'failed TDP context synchronization must not hide independent hardware controls');
  open(failedContext, 'TDP');
  assert.ok(find(failedContext.render(), 'label', 'Game profile not synchronized'), 'unconfirmed game context has a visible explanation');
  assert.equal(find(failedContext.render(), 'children', 'Max').props.disabled, true);
  find(failedContext.render(), 'children', 'Max').props.onClick(); await settle();
  assert.equal(count(failedContext, 'set_tdp'), 0, 'the action guard also refuses TDP while context is unconfirmed');
  assert.equal(find(failedContext.render(), 'label', 'Per Game Profile').props.disabled, true);
  assert.equal(find(failedContext.render(), 'label', 'CPU Boost').props.disabled, false);
  find(failedContext.render(), 'label', 'CPU Boost').props.onChange(true); await settle();
  failedContext.respond('set_cpu_boost', { ...state, active_app_id: '', cpu_boost: true }); await settle();
  assert.equal(find(failedContext.render(), 'label', 'CPU Boost').props.checked, true, 'independent RPC results remain visible across a TDP context mismatch');
  failedContext.respond('get_state', { ...state, active_app_id: '', cpu_boost: true }); await settle();
  findComponent(failedContext.render(), 'SectionHeader').props.onBack(); open(failedContext, 'RGB');
  assert.equal(find(failedContext.render(), 'label', 'LED Mode').props.disabled, false, 'RGB remains available when only game TDP synchronization failed');
  find(failedContext.render(), 'label', 'LED Mode').props.onChange({ data: 'pulse' }); failedContext.fire('timeout');
  assert.equal(count(failedContext, 'set_controller'), 1);
  failedContext.respond('set_controller', { ...state, active_app_id: '', controller: { ...state.controller, rgb_mode: 'pulse' } }); await settle();
  failedContext.unmount(); failedContext.watcher.stop();

  for (const diagnostic of [
    { extra: { settings_error: 'Invalid settings JSON; restore backup.' }, label: 'Settings require attention' },
    { extra: { startup_error: 'Backend startup failed' }, label: 'Settings require attention' },
    { extra: { initializing: true }, label: 'Starting hardware controls' },
    { extra: { supported: false }, title: 'Unsupported device' },
  ]) {
    const h = harness(); h.Router.MainRunningApp = { appid: 777, display_name: 'Current game' }; h.watcher.start(); h.render();
    h.respond('get_state', { ...state, active_app_id: '', ...diagnostic.extra }); await settle();
    assert.ok(find(h.render(), diagnostic.label ? 'label' : 'title', diagnostic.label ?? diagnostic.title), 'backend diagnostics must survive a mismatched game context');
    h.unmount(); h.watcher.stop();
  }

  const pendingProfile = harness(); pendingProfile.render();
  pendingProfile.respond('get_state', { ...state, tdp_profile_pending: true, tdp_profile_error: 'RyzenAdj is temporarily unavailable' }); await settle();
  open(pendingProfile, 'TDP');
  assert.ok(find(pendingProfile.render(), 'label', 'TDP profile pending'));
  assert.ok(find(pendingProfile.render(), 'description', 'RyzenAdj is temporarily unavailable'));
  assert.equal(find(pendingProfile.render(), 'children', 'Max').props.disabled, true, 'matching app identity alone cannot unlock an unapplied pending profile');
  assert.equal(find(pendingProfile.render(), 'label', 'CPU Boost').props.disabled, false);
  pendingProfile.fire('interval', 1500);
  pendingProfile.respond('get_state', { ...state, tdp_profile_pending: false, tdp_profile_error: '' }); await settle();
  assert.equal(find(pendingProfile.render(), 'children', 'Max').props.disabled, false, 'successful retry unlocks TDP without another game transition');
  assert.equal(find(pendingProfile.render(), 'label', 'TDP profile pending'), undefined);
  pendingProfile.unmount();

  const gameDraft = harness(); gameDraft.Router.MainRunningApp = { appid: 111, display_name: 'A' };
  gameDraft.watcher.start(); gameDraft.respond('set_active_app'); await settle(); gameDraft.render();
  gameDraft.respond('get_state', state);
  gameDraft.respond('get_game_profile', { exists: true, profile: state.tdp, preset: 'Balanced' }); await settle();
  open(gameDraft, 'TDP'); find(gameDraft.render(), 'children', 'Custom').props.onClick(); await settle();
  find(gameDraft.render(), 'label', 'SPL (TDP) - 15 W').props.onChange(21); await settle();
  gameDraft.visible(false); gameDraft.visible(true);
  gameDraft.respond('get_game_profile', { exists: true, profile: state.tdp, preset: 'Balanced' });
  gameDraft.respond('get_state', state); await settle(); open(gameDraft, 'TDP');
  assert.ok(find(gameDraft.render(), 'children', '> Custom'), 'hide/show in the same game preserves Custom together with its dirty values');
  assert.ok(find(gameDraft.render(), 'label', 'SPL (TDP) - 21 W'));
  assert.equal(find(gameDraft.render(), 'children', '> Balanced'), undefined, 'a saved preset must not mislabel an unapplied custom draft');
  assert.equal(count(gameDraft, 'set_game_profile'), 0, 'QAM reopening does not apply a draft');
  const applyDraft = find(gameDraft.render(), 'children', 'Apply & Save for A');
  assert.ok(applyDraft); assert.equal(applyDraft.props.disabled, false); applyDraft.props.onClick(); await settle();
  const draftSave = gameDraft.calls.find(call => call.name === 'set_game_profile');
  assert.deepEqual(JSON.parse(JSON.stringify(draftSave.args)), ['111', { spl: 21, sppt: 23, fppt: 26 }, 'Custom', '111']);
  gameDraft.respond('set_game_profile', { ...state, tdp: { spl: 21, sppt: 23, fppt: 26 }, tdp_preset: 'Custom' }); await settle();
  gameDraft.unmount(); gameDraft.watcher.stop();

  const newProfileDraft = harness(); newProfileDraft.Router.MainRunningApp = { appid: 444, display_name: 'D' };
  newProfileDraft.watcher.start(); newProfileDraft.respond('set_active_app'); await settle(); newProfileDraft.render();
  newProfileDraft.respond('get_state', state);
  newProfileDraft.respond('get_game_profile', { exists: false, profile: {} }); await settle(); open(newProfileDraft, 'TDP');
  find(newProfileDraft.render(), 'label', 'Per Game Profile').props.onChange(true); await settle();
  newProfileDraft.respond('get_game_profile', { exists: false, profile: {} }); await settle();
  newProfileDraft.respond('get_state', state); await settle();
  find(newProfileDraft.render(), 'children', 'Custom').props.onClick(); await settle();
  find(newProfileDraft.render(), 'label', 'SPL (TDP) - 15 W').props.onChange(21); await settle();
  newProfileDraft.visible(false); newProfileDraft.visible(true);
  newProfileDraft.respond('get_game_profile', { exists: false, profile: {} });
  newProfileDraft.respond('get_state', state); await settle(); open(newProfileDraft, 'TDP');
  assert.equal(find(newProfileDraft.render(), 'label', 'Per Game Profile').props.checked, true, 'a dirty draft retains the intended per-game save target even before its first profile exists');
  const createProfile = find(newProfileDraft.render(), 'children', 'Apply & Save for D');
  assert.ok(createProfile); createProfile.props.onClick(); await settle();
  assert.equal(count(newProfileDraft, 'set_tdp'), 0, 'reopening a new game draft cannot silently turn its save into a global TDP write');
  assert.equal(newProfileDraft.calls.find(call => call.name === 'set_game_profile').args[0], '444');
  newProfileDraft.respond('set_game_profile', { ...state, tdp: { spl: 21, sppt: 23, fppt: 26 }, tdp_preset: 'Custom' }); await settle();
  newProfileDraft.unmount(); newProfileDraft.watcher.stop();

  const combined = await ready(); open(combined, 'RGB');
  find(combined.render(), 'label', 'Hue').props.onChange(120);
  find(combined.render(), 'label', 'Hue').props.onChangeEnd(120);
  findComponent(combined.render(), 'SectionHeader').props.onBack(); open(combined, 'Vibration');
  findComponent(combined.render(), 'VibrationPage').props.onGain(20); combined.fire('timeout');
  const controllerCommit = combined.calls.find(call => call.name === 'set_controller');
  assert.ok(controllerCommit); assert.equal(controllerCommit.args[0].ff_gain, 20);
  combined.respond('set_controller', { ...state, controller: { ...state.controller, ...controllerCommit.args[0] } }); await settle();
  assert.equal(count(combined, 'set_vibration_gain'), 0, 'RGB plus gain uses one complete backend transaction');
  findComponent(combined.render(), 'VibrationPage').props.onGain(30); combined.fire('timeout');
  assert.equal(count(combined, 'set_vibration_gain'), 1, 'gain-only edit retains the evdev-only RPC');
  combined.respond('set_vibration_gain', { ...state, controller: { ...state.controller, ...controllerCommit.args[0], ff_gain: 30 } }); await settle(); combined.unmount();

  const capabilities = harness(); capabilities.render();
  capabilities.respond('get_state', { ...state, tdp_backend: 'PowerStation', tdp_supported: false,
    tdp_error: 'Cannot enforce FPPT 37 W', controller_supported: false, controller_error: 'kernel hid-ayaneo is active',
    module_eject_supported: false, module_reset_supported: false }); await settle();
  open(capabilities, 'TDP');
  assert.ok(find(capabilities.render(), 'label', 'TDP control unavailable'));
  assert.equal(find(capabilities.render(), 'children', 'Max').props.disabled, true);
  assert.equal(find(capabilities.render(), 'label', 'CPU Boost').props.disabled, false, 'independent CPU control remains available');
  findComponent(capabilities.render(), 'SectionHeader').props.onBack(); open(capabilities, 'RGB');
  assert.equal(find(capabilities.render(), 'label', 'Enable LED Control').props.disabled, true);
  assert.equal(find(capabilities.render(), 'label', 'Hue').props.disabled, true);
  assert.ok(find(capabilities.render(), 'label', 'Controller control unavailable'));
  findComponent(capabilities.render(), 'SectionHeader').props.onBack(); open(capabilities, 'Magic Modules');
  const modulesPage = findComponent(capabilities.render(), 'MagicModulesPage');
  const modulesTree = modulesPage.type(modulesPage.props);
  assert.equal(find(modulesTree, 'title', 'Eject Left').props.disabled, true);
  assert.equal(find(modulesTree, 'title', 'Reset Magic Modules').props.disabled, true);
  assert.ok(find(modulesTree, 'description', 'kernel hid-ayaneo is active'));
  findComponent(capabilities.render(), 'SectionHeader').props.onBack(); open(capabilities, 'Vibration');
  const blockedVibration = findComponent(capabilities.render(), 'VibrationPage');
  const vibrationTree = blockedVibration.type(blockedVibration.props);
  assert.equal(find(vibrationTree, 'label', 'Firmware Strength · Medium').props.disabled, true);
  assert.ok(!find(vibrationTree, 'label', 'FF Gain').props.disabled, 'evdev gain remains independent of vendor HID');
  findComponent(capabilities.render(), 'SectionHeader').props.onBack(); open(capabilities, 'Key Binding');
  const blockedButtons = findComponent(capabilities.render(), 'ButtonsPage');
  const buttonsTree = blockedButtons.type(blockedButtons.props);
  assert.equal(find(buttonsTree, 'label', 'Fix Key Binding').props.disabled, true);
  assert.equal(find(buttonsTree, 'label', 'TM Guard').props.disabled, false, 'saved policy can still be disabled while HID is unavailable');
  capabilities.unmount();

  const errors = harness(); errors.render(); errors.reject('get_state'); await settle(); assert.ok(find(errors.render(), 'label', 'Backend unavailable'));
  errors.fire(); errors.respond('get_state', { ...state, settings_error: 'Invalid settings JSON; restore backup.' }); await settle();
  assert.ok(find(errors.render(), 'label', 'Settings require attention')); assert.equal(find(errors.render(), 'title', 'Hardware Controls'), undefined); errors.unmount();

  const update = harness('UpdateSection'); update.render({ initialVersion: '1.0.2' });
  const check = find(update.render(), 'title', 'Check for Updates'); check.props.onClick(); check.props.onClick();
  assert.equal(count(update, 'check_for_updates'), 1, 'same-frame double click makes one update request');
  update.unmount(); writes = update.writes; update.respond('check_for_updates', { update_available: true, latest_version: '1.0.3', download_url: 'https://github.com/release.zip', asset_name: 'plugin.zip' }); await settle();
  assert.equal(update.writes, writes, 'unmounted update panel ignores state writes'); update.remount({ initialVersion: '1.0.2' });
  const download = find(update.render(), 'title', 'Version 1.0.3 is available'); assert.ok(download, 'completed update check survives panel remount');
  download.props.onClick(); download.props.onClick(); assert.equal(count(update, 'perform_update'), 1, 'download is single flight');
  assert.equal(update.calls.find(call => call.name === 'perform_update').args[0], '1.0.3', 'download is bound to the displayed version');
  update.reject('perform_update'); await settle(); assert.ok(find(update.render(), 'title', 'Updates')); assert.equal(update.toasts.length, 1); update.unmount();

  const changedRelease = harness('UpdateSection'); changedRelease.render({ initialVersion: '1.2.0' });
  find(changedRelease.render(), 'title', 'Check for Updates').props.onClick();
  changedRelease.respond('check_for_updates', { update_available: true, latest_version: '1.2.1', download_url: 'https://github.com/release.zip', asset_name: 'plugin.zip' }); await settle();
  find(changedRelease.render(), 'title', 'Version 1.2.1 is available').props.onClick();
  changedRelease.respond('perform_update', { success: true, version: '1.3.0', path: '/mock/wrong.zip' }); await settle();
  assert.equal(find(changedRelease.render(), 'label', 'Update downloaded'), undefined, 'a different release cannot be reported as the selected download');
  assert.equal(changedRelease.toasts.length, 1);
  find(changedRelease.render(), 'title', 'Check for Updates').props.onClick();
  changedRelease.respond('check_for_updates', { update_available: true, latest_version: '1.2.1', download_url: 'https://github.com/release.zip', asset_name: 'plugin.zip' }); await settle();
  find(changedRelease.render(), 'title', 'Version 1.2.1 is available').props.onClick();
  changedRelease.respond('perform_update', { success: true, version: '1.2.1', path: '/mock/selected.zip' }); await settle();
  assert.ok(find(changedRelease.render(), 'label', 'Update downloaded')); changedRelease.unmount();

  const expired = harness('UpdateSection'); const oldPlugin = expired.initialize(); expired.render({ initialVersion: '1.0.2' });
  find(expired.render(), 'title', 'Check for Updates').props.onClick(); expired.unmount(); oldPlugin.onDismount();
  expired.remount(); expired.respond('check_for_updates', { update_available: true, latest_version: '9.9.9' }); await settle();
  assert.equal(find(expired.render(), 'title', 'Version 9.9.9 is available'), undefined, 'old plugin lifecycle cannot publish update results'); expired.unmount();

  const unsupportedFan = await ready();
  unsupportedFan.respond('get_fan_status', { supported: false, mode: 'auto', percent: null, rpm: null, reason: 'ayaneo_ec hwmon unavailable' });
  await settle();
  assert.equal(find(unsupportedFan.render(), 'title', 'Fan Control'), undefined, 'unsupported fan control stays hidden');
  unsupportedFan.unmount();

  const fan = await ready();
  fan.respond('get_fan_status', { supported: true, mode: 'auto', percent: 77, rpm: 2100 });
  await settle();
  const fanLink = find(fan.render(), 'title', 'Fan Control');
  assert.ok(fanLink, 'supported fan control appears in the hardware menu');
  assert.ok(!String(fanLink.props.description).includes('77%'), 'Auto mode does not present last PWM as current speed');
  fanLink.props.onClick();
  let fanPage = findComponent(fan.render(), 'FanPage');
  assert.ok(fanPage, 'fan page opens without changing the other sections');
  fanPage.props.onApply('manual', 75); fanPage.props.onApply('manual', 80);
  assert.equal(count(fan, 'set_fan'), 1, 'same-frame fan edits are single flight');
  assert.equal(fan.calls.find(call => call.name === 'set_fan').args[1], 75);
  fan.respond('set_fan', { supported: true, mode: 'manual', percent: 75, rpm: 3000 }); await settle();
  fanPage = findComponent(fan.render(), 'FanPage');
  assert.equal(fanPage.props.status.percent, 75, 'fan page shows verified backend result');
  fanPage.props.onApply('auto');
  const autoCall = fan.calls.filter(call => call.name === 'set_fan')[1];
  assert.equal(autoCall.args.length, 1, 'Auto request omits a stale PWM value');
  fan.respond('set_fan', { supported: true, mode: 'auto', percent: 75, rpm: 2200 }); await settle();
  fan.unmount();

  const slowFan = await ready();
  slowFan.respond('get_fan_status', { supported: true, mode: 'auto', percent: null, rpm: 2200 }); await settle();
  slowFan.fire('interval', 30000); open(slowFan, 'Fan Control');
  assert.equal(count(slowFan, 'get_fan_status'), 2, 'opening the page shares its pending overview read');
  slowFan.respond('get_fan_status', { supported: true, mode: 'manual', percent: 80, rpm: 3400 }); await settle();
  assert.equal(findComponent(slowFan.render(), 'FanPage').props.status.rpm, 3400,
    'opening the fan page cannot discard the latest read and wait another five seconds');
  slowFan.fire('interval', 5000); slowFan.visible(false); slowFan.visible(true);
  slowFan.respond('get_fan_status', { supported: true, mode: 'manual', percent: 90, rpm: 3900 }); await settle();
  assert.equal(count(slowFan, 'get_fan_status'), 4, 'a stale hidden response immediately schedules the reopened view read');
  slowFan.respond('get_fan_status', { supported: true, mode: 'auto', percent: null, rpm: 2400 }); await settle();
  assert.equal(find(slowFan.render(), 'title', 'Fan Control').props.description, 'Auto · 2400 RPM');
  slowFan.unmount();

  const lostFan = await ready();
  lostFan.respond('get_fan_status', { supported: true, mode: 'auto', percent: 70, rpm: 2400 }); await settle();
  open(lostFan, 'Fan Control');
  lostFan.respond('get_fan_status', { supported: true, mode: 'auto', percent: 70, rpm: 2400 }); await settle();
  findComponent(lostFan.render(), 'FanPage').props.onApply('manual', 75);
  lostFan.respond('set_fan', { supported: false, mode: 'auto', percent: null, rpm: null, reason: 'Temporary EC read failure' }); await settle();
  let lostPage = findComponent(lostFan.render(), 'FanPage');
  assert.equal(lostPage.props.available, true, 'a temporary read failure does not erase confirmed fan capability');
  assert.ok(lostPage.props.error.includes('Temporary EC read failure'), 'unconfirmed manual result remains visible');
  const fanView = harness('FanPage');
  let lostTree = fanView.render(lostPage.props);
  assert.equal(find(lostTree, 'title', 'Restore Automatic Control').props.disabled, false, 'Auto recovery is enabled even when the unknown status defaults to auto');
  assert.equal(find(lostTree, 'title', 'Enable Manual Control').props.disabled, true, 'manual writes stay disabled until live status recovers');
  lostPage.props.onApply('manual', 80);
  assert.equal(count(lostFan, 'set_fan'), 1, 'the hook also refuses manual writes while the controller is unreadable');
  lostFan.respond('get_fan_status', { supported: true, mode: 'auto', percent: 70, rpm: 2400 }); await settle();
  lostPage = findComponent(lostFan.render(), 'FanPage');
  assert.ok(lostPage.props.error.includes('Temporary EC read failure'), 'successful polling does not hide a failed manual action');
  findComponent(lostFan.render(), 'SectionHeader').props.onBack();
  assert.equal(find(lostFan.render(), 'title', 'Fan Control').props.description, 'Status unavailable', 'overview does not claim an unconfirmed automatic mode');
  open(lostFan, 'Fan Control');
  lostPage = findComponent(lostFan.render(), 'FanPage');
  lostPage.props.onApply('auto');
  assert.equal(count(lostFan, 'set_fan'), 2, 'Auto remains callable after an unconfirmed manual operation');
  lostFan.respond('set_fan', { supported: true, mode: 'auto', percent: 70, rpm: 2400 }); await settle();
  lostPage = findComponent(lostFan.render(), 'FanPage');
  assert.equal(lostPage.props.error, '', 'verified Auto clears the failed-action message');
  lostFan.unmount(); fanView.unmount();

  const disappearingFan = await ready();
  disappearingFan.respond('get_fan_status', { supported: true, mode: 'manual', percent: 75, rpm: 3000 }); await settle();
  disappearingFan.fire('interval', 30000);
  disappearingFan.respond('get_fan_status', { supported: false, mode: 'auto', percent: null, rpm: null, reason: 'Temporarily unavailable' }); await settle();
  assert.ok(find(disappearingFan.render(), 'title', 'Fan Control'), 'previously supported fan remains reachable after a failed poll');
  disappearingFan.unmount();
  assert.ok(find(disappearingFan.remount(), 'title', 'Fan Control'), 'capability survives a view remount in the same plugin lifecycle');
  disappearingFan.unmount();
  const retiringPlugin = disappearingFan.initialize(); retiringPlugin.onDismount();
  disappearingFan.remount();
  assert.equal(find(disappearingFan.render(), 'title', 'Fan Control'), undefined, 'plugin unload resets the capability proof for a new lifecycle');
  disappearingFan.unmount();

  console.log('Frontend lifecycle regressions: PASS (polling, mutations, RGB queue, dropdown, app lifecycle, profiles, updates, fan safety and error UI)');
})().catch(error => { console.error(error); process.exitCode = 1; });
