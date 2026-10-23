// Execute the actual TSX with controlled hooks, RPC completion and timers.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..'), ts = require(path.join(root, 'node_modules/typescript'));
const settle = async () => { for (let i = 0; i < 24; i++) await Promise.resolve(); };
function harness(component = 'Content', initialVisible = true) {
  const slots = [], effects = new Map(), pending = [], timers = new Map(), calls = [], toasts = [], callbacks = {};
  let cursor = 0, visible = initialVisible, nextTimer = 0, writes = 0, props = {};
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
    const source = fs.readFileSync(fullPath, 'utf8') + (file === 'index.tsx' ? '\nexport { Content };' : '');
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
        if (name.startsWith('./')) return load(fs.existsSync(path.join(root, 'src', `${name.slice(2)}.tsx`))
          ? `${name.slice(2)}.tsx` : `${name.slice(2)}.ts`);
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
  return { render, calls, timers, callbacks, toasts, Router,
    initialize: () => plugin.default(), get writes() { return writes; },
    visible(value) { visible = value; return render(); },
    fire(kind = 'interval', delay) { for (const [id, timer] of [...timers]) if (timer.kind === kind && (delay == null || timer.delay === delay)) {
      if (kind === 'timeout') timers.delete(id); timer.fn();
    } },
    respond(name, value) {
      const call = calls.find(c => c.name === name && !c.settled); assert.ok(call, `pending ${name}`);
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
// TDP Control has its own page and tests (frontend_tdp_recovery.cjs).
const state = { supported: true, device: 'AYANEO 3', version: '1.0.2',
  controller: { vibration: 'medium', ff_gain: 60, rgb_mode: 'solid', color: 'ff0000', brightness: 70 },
  module_left: { label: 'Gamepad' }, module_right: { label: 'Gamepad' }, modules_connected: true,
};
const count = (h, name) => h.calls.filter(c => c.name === name).length;
// A page stays open across QAM visibility changes; return to the overview explicitly, like the user.
const open = (h, title) => {
  findComponent(h.render(), 'SectionHeader')?.props.onBack();
  const field = find(h.render(), 'title', title); assert.ok(field?.props.onClick, title); field.props.onClick(); return h.render();
};
const ready = async () => { const h = harness(); h.render(); h.respond('get_state', state); await settle(); h.render(); return h; };
(async () => {
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
  assert.match(find(failedStatus.render(), 'title', 'Vibration').props.statusNote, /^Older data · Last read \d+s ago · Retrying…$/,
    'a failed poll must identify cached values as unverified on every overview row');
  assert.ok(find(failedStatus.render(), 'title', 'Vibration').props.onClick, 'cached controls remain reachable during a transient status failure');
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
  assert.ok(find(cached.remount(), 'title', 'Vibration'), 'remount immediately shows confirmed summaries'); cached.unmount();

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

  const closingTest = await ready(); open(closingTest, 'Vibration');
  findComponent(closingTest.render(), 'VibrationPage').props.onGain(40);
  findComponent(closingTest.render(), 'VibrationPage').props.onTest(); await settle(); closingTest.unmount();
  const beforeClosingAck = closingTest.writes;
  closingTest.respond('set_vibration_gain', { ...state, controller: { ...state.controller, ff_gain: 40 } }); await settle();
  assert.equal(count(closingTest, 'test_vibration'), 0, 'leaving an unmounted panel cannot start a delayed test');
  assert.equal(closingTest.writes, beforeClosingAck);

  const rgb = await ready(); const plugin = rgb.initialize(); assert.equal(plugin.alwaysRender, true, 'Decky preserves content behind native dropdown');
  open(rgb, 'Lighting'); const hue = find(rgb.render(), 'label', 'Hue'), brightness = find(rgb.render(), 'label', 'Brightness');
  hue.props.onChange(120); hue.props.onChangeEnd(120); brightness.props.onChange(42); brightness.props.onChangeEnd(42);
  assert.equal(count(rgb, 'set_controller'), 0, 'slider movement is debounced');
  rgb.visible(false); assert.equal(count(rgb, 'set_controller'), 1, 'closing QAM flushes the latest RGB edit');
  const rgbCall = rgb.calls.find(c => c.name === 'set_controller');
  assert.equal(rgbCall.args[0].color, '00ff00'); assert.equal(rgbCall.args[0].brightness, 42, 'same-frame sibling slider edits merge');
  rgb.unmount(); writes = rgb.writes; rgb.respond('set_controller', { ...state, controller: { ...state.controller, ...rgbCall.args[0] } }); await settle();
  assert.equal(rgb.writes, writes, 'pending controller save completes without touching an unmounted view'); plugin.onDismount();

  const queue = await ready(); open(queue, 'Lighting'); find(queue.render(), 'label', 'LED Mode').props.onChange({ data: 'pulse' }); queue.fire('timeout');
  find(queue.render(), 'label', 'LED Mode').props.onChange({ data: 'rainbow' }); queue.unmount();
  assert.equal(count(queue, 'set_controller'), 1, 'controller writes do not overlap');
  writes = queue.writes; queue.respond('set_controller', { ...state, controller: { ...state.controller, rgb_mode: 'pulse' } }); await settle();
  assert.equal(count(queue, 'set_controller'), 2, 'queued latest edit survives unmount during an earlier write');
  queue.respond('set_controller', { ...state, controller: { ...state.controller, rgb_mode: 'rainbow' } }); await settle(); assert.equal(queue.writes, writes);

  const remounted = await ready(); open(remounted, 'Lighting');
  find(remounted.render(), 'label', 'Hue').props.onChange(120);
  find(remounted.render(), 'label', 'Hue').props.onChangeEnd(120);
  remounted.unmount(); await settle();
  const oldEdit = remounted.calls.find(call => call.name === 'set_controller');
  assert.deepEqual({ ...oldEdit.args[0] }, { color: '00ff00' }, 'color edit omits unrelated fields');
  remounted.remount(); remounted.respond('get_state', state); await settle(); open(remounted, 'Lighting');
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

  const saturation = await ready(); open(saturation, 'Lighting');
  find(saturation.render(), 'label', 'Saturation').props.onChange(50);
  find(saturation.render(), 'label', 'Saturation').props.onChangeEnd(50);
  saturation.fire('timeout'); await settle();
  const saturationEdit = saturation.calls.find(call => call.name === 'set_controller');
  assert.deepEqual(Object.keys(saturationEdit.args[0]), ['color'], 'saturation cannot replace a newer brightness or gain');
  saturation.respond('set_controller', { ...state, controller: { ...state.controller, ...saturationEdit.args[0] } }); await settle(); saturation.unmount();

  const dropdown = await ready(); open(dropdown, 'Lighting'); const menu = find(dropdown.render(), 'label', 'LED Mode');
  // The native menu hides Quick Access while it is open.
  dropdown.visible(false); menu.props.onChange({ data: 'pulse' }); dropdown.fire('timeout');
  dropdown.visible(true); assert.ok(find(dropdown.render(), 'label', 'LED Mode'), 'native dropdown returns to the same section'); dropdown.unmount();

  for (const diagnostic of [
    { extra: { settings_error: 'Invalid settings JSON; restore backup.' }, label: 'Settings require attention' },
    { extra: { startup_error: 'Backend startup failed' }, label: 'Settings require attention' },
    { extra: { initializing: true }, label: 'Starting hardware controls' },
    { extra: { supported: false }, title: 'Unsupported device' },
  ]) {
    const h = harness(); h.render();
    h.respond('get_state', { ...state, ...diagnostic.extra }); await settle();
    assert.ok(find(h.render(), diagnostic.label ? 'label' : 'title', diagnostic.label ?? diagnostic.title), 'backend diagnostics replace the controls');
    h.unmount();
  }

  const combined = await ready(); open(combined, 'Lighting');
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
  capabilities.respond('get_state', { ...state, controller_supported: false, controller_error: 'kernel hid-ayaneo is active',
    module_eject_supported: false, module_reset_supported: false }); await settle();
  open(capabilities, 'Lighting');
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
  findComponent(slowFan.render(), 'SectionHeader').props.onBack();
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

  console.log('Frontend lifecycle regressions: PASS (polling, mutations, RGB queue, dropdown, updates, fan safety and error UI)');
})().catch(error => { console.error(error); process.exitCode = 1; });
