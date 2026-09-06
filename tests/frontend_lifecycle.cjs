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
  const mod = { exports: {} }, jsx = { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }) };
  const callable = name => (...args) => new Promise((resolve, reject) => calls.push({ name, args, resolve, reject, settled: false }));
  const timer = (kind, fn, delay) => { const id = ++nextTimer; timers.set(id, { kind, fn, delay }); return id; };
  const source = fs.readFileSync(path.join(root, 'src/index.tsx'), 'utf8') + '\nexport { Content, AppWatcher, UpdateSection };';
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText, {
    module: mod, exports: mod.exports, console,
    setTimeout: (fn, delay) => timer('timeout', fn, delay), clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => timer('interval', fn, delay), clearInterval: id => timers.delete(id),
    window: { SteamClient: { GameSessions: { RegisterForAppLifetimeNotifications(fn) {
      callbacks.lifetime = fn; return { unregister() {} };
    } } } },
    require(name) {
      if (name === 'react') return hooks;
      if (name === 'react/jsx-runtime') return jsx;
      if (name === 'react-icons/fa') return { FaChevronRight: 'FaChevronRight' };
      if (name === '@decky/api') return { callable, definePlugin: fn => fn, useQuickAccessVisible: () => visible, toaster: { toast: value => toasts.push(value) } };
      if (name === '@decky/ui') return new Proxy({ Router, showModal() {} }, { get: (object, key) => key in object ? object[key] : key });
      throw Error(name);
    },
  });
  const render = (nextProps = props) => { props = nextProps; cursor = 0; const tree = mod.exports[component](props); pending.splice(0).forEach(fn => fn()); return tree; };
  return { render, calls, timers, callbacks, toasts, Router, watcher: mod.exports.AppWatcher,
    initialize: () => mod.exports.default(), get writes() { return writes; },
    visible(value) { visible = value; return render(); },
    fire(kind = 'interval', delay) { for (const [id, timer] of [...timers]) if (timer.kind === kind && (delay == null || timer.delay === delay)) {
      if (kind === 'timeout') timers.delete(id); timer.fn();
    } },
    respond(name, value) { const call = calls.find(c => c.name === name && !c.settled); assert.ok(call, `pending ${name}`); call.settled = true; call.resolve(value); return call; },
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

  const rgb = await ready(); const plugin = rgb.initialize(); assert.equal(plugin.alwaysRender, true, 'Decky preserves content behind native dropdown');
  open(rgb, 'RGB'); const hue = find(rgb.render(), 'label', 'Hue'), brightness = find(rgb.render(), 'label', 'Brightness');
  hue.props.onChange(120); hue.props.onChangeEnd(120); brightness.props.onChange(42); brightness.props.onChangeEnd(42);
  assert.equal(count(rgb, 'set_controller'), 0, 'slider movement is debounced');
  rgb.visible(false); assert.equal(count(rgb, 'set_controller'), 1, 'closing QAM flushes the latest RGB edit');
  const rgbCall = rgb.calls.find(c => c.name === 'set_controller');
  assert.equal(rgbCall.args[0].color, '00ff00'); assert.equal(rgbCall.args[0].brightness, 42, 'same-frame sibling slider edits merge');
  rgb.unmount(); writes = rgb.writes; rgb.respond('set_controller', { ...state, controller: rgbCall.args[0] }); await settle();
  assert.equal(rgb.writes, writes, 'pending controller save completes without touching an unmounted view'); plugin.onDismount();

  const queue = await ready(); open(queue, 'RGB'); find(queue.render(), 'label', 'LED Mode').props.onChange({ data: 'pulse' }); queue.fire('timeout');
  find(queue.render(), 'label', 'LED Mode').props.onChange({ data: 'rainbow' }); queue.unmount();
  assert.equal(count(queue, 'set_controller'), 1, 'controller writes do not overlap');
  writes = queue.writes; queue.respond('set_controller', { ...state, controller: { ...state.controller, rgb_mode: 'pulse' } }); await settle();
  assert.equal(count(queue, 'set_controller'), 2, 'queued latest edit survives unmount during an earlier write');
  queue.respond('set_controller', { ...state, controller: { ...state.controller, rgb_mode: 'rainbow' } }); await settle(); assert.equal(queue.writes, writes);

  const dropdown = await ready(); open(dropdown, 'RGB'); const menu = find(dropdown.render(), 'label', 'LED Mode');
  menu.props.onMenuWillOpen(() => dropdown.visible(false)); menu.props.onChange({ data: 'pulse' }); dropdown.fire('timeout');
  dropdown.visible(true); assert.ok(find(dropdown.render(), 'label', 'LED Mode'), 'native dropdown returns to the same section'); dropdown.unmount();

  const watcher = harness(); watcher.Router.MainRunningApp = { appid: 111, display_name: 'A' }; watcher.watcher.start();
  watcher.callbacks.lifetime(); const late = [...watcher.timers.values()].find(t => t.kind === 'timeout').fn;
  watcher.watcher.stop(); assert.equal(watcher.timers.size, 0); late(); assert.equal(count(watcher, 'set_active_app'), 1);
  watcher.watcher.start(); watcher.respond('set_active_app'); await settle(); watcher.fire();
  assert.equal(count(watcher, 'set_active_app'), 2, 'old lifecycle completion cannot clear the new in-flight guard'); watcher.watcher.stop();

  const game = harness(); game.Router.MainRunningApp = { appid: 111, display_name: 'A' }; game.watcher.start(); game.respond('set_active_app'); await settle();
  game.render(); game.respond('get_state', state); game.respond('get_game_profile', { exists: true, profile: state.tdp, preset: 'Balanced' }); await settle();
  open(game, 'TDP'); find(game.render(), 'children', 'Performance').props.onClick(); await settle();
  const save = game.calls.find(c => c.name === 'set_game_profile'); assert.equal(save.args[0], '111'); assert.equal(save.args[3], '111', 'save captures expected app');
  game.Router.MainRunningApp = { appid: 222, display_name: 'B' }; game.fire(); game.render();
  game.respond('set_game_profile', { ...state, tdp: { spl: 26, sppt: 29, fppt: 31 }, tdp_preset: 'Performance' }); await settle();
  assert.notEqual(find(game.render(), 'label', '26 / 29 / 31 W')?.props.label, '26 / 29 / 31 W', 'old game save cannot replace the new editor');
  game.respond('get_game_profile', { exists: false, profile: {} }); game.respond('get_state', state); await settle(); game.render();
  find(game.render(), 'children', 'Max').props.onClick(); await settle();
  const global = game.calls.find(c => c.name === 'set_tdp'); assert.equal(global.args[2], '222', 'global edit is also guarded against app changes');
  game.unmount(); game.watcher.stop();

  const retry = harness(); retry.Router.MainRunningApp = { appid: 333, display_name: 'C' }; retry.watcher.start(); retry.respond('set_active_app'); await settle();
  retry.render(); retry.respond('get_state', state); retry.reject('get_game_profile'); await settle(); retry.fire('timeout', 3000);
  assert.equal(count(retry, 'get_game_profile'), 2, 'transient profile lookup retries without another menu open');
  retry.respond('get_game_profile', { exists: true, profile: state.tdp, preset: 'Balanced' }); await settle();
  open(retry, 'TDP'); assert.equal(find(retry.render(), 'label', 'Per Game Profile').props.checked, true);
  retry.unmount(); retry.watcher.stop();

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
  update.reject('perform_update'); await settle(); assert.ok(find(update.render(), 'title', 'Updates')); assert.equal(update.toasts.length, 1); update.unmount();

  const expired = harness('UpdateSection'); const oldPlugin = expired.initialize(); expired.render({ initialVersion: '1.0.2' });
  find(expired.render(), 'title', 'Check for Updates').props.onClick(); expired.unmount(); oldPlugin.onDismount();
  expired.remount(); expired.respond('check_for_updates', { update_available: true, latest_version: '9.9.9' }); await settle();
  assert.equal(find(expired.render(), 'title', 'Version 9.9.9 is available'), undefined, 'old plugin lifecycle cannot publish update results'); expired.unmount();

  console.log('Frontend lifecycle regressions: PASS (polling, mutations, RGB queue, dropdown, app lifecycle, profiles, updates and error UI)');
})().catch(error => { console.error(error); process.exitCode = 1; });
