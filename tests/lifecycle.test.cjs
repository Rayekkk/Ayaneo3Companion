// The overview and device pages (index.tsx) with controlled RPC completion and timers: polling, mutations, the RGB
// queue (controller_queue.ts), native dropdowns, About with the shared updater, the Fan overview row and error UI.
const test = require('node:test');
const assert = require('node:assert/strict');
const { harness: panel, all, settle } = require('./frontend_harness.cjs');

// Pages are kept as elements: a test reads their props or renders them itself (page.type(page.props)).
const KEEP = ['SectionLink', 'PageHeader', 'StackedAction', 'SlowSliderField', 'TdpPage', 'FanPage', 'AboutPage',
  'VibrationPage', 'BatteryPage', 'MagicModulesPage', 'AudioPage', 'ButtonsPage', 'DisplayPage'];
const harness = ({ visible = true } = {}) => {
  const h = panel({ keep: KEEP, ui: { Router: { MainRunningApp: null }, showModal() {} },
    globals: { SteamClient: { GameSessions: { RegisterForAppLifetimeNotifications() { return { unregister() {} }; } } } } });
  if (!visible) h.hide();
  return h;
};
/** QAM opens or closes, and the panel renders again. */
const setVisible = (h, value, props) => { if (value) h.show(); else h.hide(); return h.render(props); };
const find = (node, key, value) => all(node).find(n => n.props?.[key] === value);
const findComponent = (node, name) => all(node).find(n => n.type?.name === name);
const ok = state => ({ ok: true, state });
const refused = (error = 'RPC failed') => ({ ok: false, error });
// TDP Control has its own page and tests (tdp.test.cjs).
const state = { supported: true, device: 'AYANEO 3', version: '1.0.2',
  controller: { vibration: 'medium', ff_gain: 60, rgb_mode: 'solid', color: 'ff0000', brightness: 70 },
  module_left: { label: 'Gamepad' }, module_right: { label: 'Gamepad' }, modules_connected: true,
};
const withController = controller => ok({ ...state, controller: { ...state.controller, ...controller } });
const count = (h, name) => h.calls.filter(c => c.name === name).length;
// A page stays open across QAM visibility changes; return to the overview explicitly, like the user.
const open = (h, title) => {
  findComponent(h.render(), 'PageHeader')?.props.onBack();
  const field = find(h.render(), 'title', title); assert.ok(field?.props.onOpen, title); field.props.onOpen(); return h.render();
};
const ready = async () => { const h = harness(); h.render(); h.respond('get_state', ok(state)); await settle(); h.render(); return h; };
const page = (h, name) => { const element = findComponent(h.render(), name); return element.type(element.props); };

test('battery reads: single flight, discarded when obsolete, errors shown and cleared', async () => {
  const slowBattery = await ready(); open(slowBattery, 'Battery');
  for (let reopen = 0; reopen < 3; reopen++) {
    setVisible(slowBattery, false); setVisible(slowBattery, true); open(slowBattery, 'Battery');
    slowBattery.tick(10000);
  }
  assert.equal(count(slowBattery, 'get_battery_status'), 1,
    'closing and reopening Battery must not accumulate concurrent slow RPC reads');
  slowBattery.respond('get_battery_status', ok({ available: true, percent: 10, status: 'Discharging', seconds_to_full: null })); await settle();
  assert.equal(count(slowBattery, 'get_battery_status'), 2,
    'the reopened page needs one fresh read once the obsolete request finishes');
  let slowBatteryPage = findComponent(slowBattery.render(), 'BatteryPage');
  assert.equal(slowBatteryPage.props.battery, null, 'an obsolete battery result must stay discarded');
  slowBattery.respond('get_battery_status', ok({ available: true, percent: 60, status: 'Charging', seconds_to_full: 3600 })); await settle();
  slowBatteryPage = findComponent(slowBattery.render(), 'BatteryPage');
  assert.equal(slowBatteryPage.props.battery.percent, 60);
  slowBattery.tick(10000);
  setVisible(slowBattery, false); slowBattery.unmount();
  const batteryCallsAtUnmount = count(slowBattery, 'get_battery_status');
  const batteryWritesAtUnmount = slowBattery.writes;
  slowBattery.fail('get_battery_status'); await settle();
  assert.equal(count(slowBattery, 'get_battery_status'), batteryCallsAtUnmount, 'unmount must not restart a battery read');
  assert.equal(slowBattery.writes, batteryWritesAtUnmount, 'unmount must suppress late battery errors');

  const failedBattery = await ready(); open(failedBattery, 'Battery');
  failedBattery.respond('get_battery_status', refused()); await settle();
  assert.ok(find(page(failedBattery, 'BatteryPage'), 'label', 'Battery status unavailable'),
    'an initial battery read failure must replace the indefinite spinner');
  failedBattery.tick(10000);
  failedBattery.respond('get_battery_status', ok({ available: true, percent: 65, status: 'Charging', seconds_to_full: 3600, power_w: 15, source: 'sysfs' })); await settle();
  assert.equal(find(page(failedBattery, 'BatteryPage'), 'label', 'Battery status unavailable'), undefined);
  failedBattery.tick(10000); failedBattery.fail('get_battery_status'); await settle();
  assert.ok(find(page(failedBattery, 'BatteryPage'), 'label', 'Battery refresh failed'),
    'cached battery values must be marked after their refresh fails');
  assert.ok(find(page(failedBattery, 'BatteryPage'), 'label', '65% · Charging'));
  failedBattery.tick(10000);
  failedBattery.respond('get_battery_status', ok({ available: true, percent: 70, status: 'Charging', seconds_to_full: 3000, power_w: 15, source: 'sysfs' })); await settle();
  assert.equal(find(page(failedBattery, 'BatteryPage'), 'label', 'Battery refresh failed'), undefined);
  failedBattery.unmount();

  const obsoleteBattery = await ready(); open(obsoleteBattery, 'Battery');
  setVisible(obsoleteBattery, false); setVisible(obsoleteBattery, true); open(obsoleteBattery, 'Battery');
  obsoleteBattery.respond('get_battery_status', refused()); await settle();
  assert.equal(find(page(obsoleteBattery, 'BatteryPage'), 'label', 'Battery status unavailable'), undefined,
    'a late failure from the closed battery page must not overwrite its new read');
  const reopenedBatteryRead = obsoleteBattery.calls.filter(call => call.name === 'get_battery_status' && !call.settled).at(-1);
  reopenedBatteryRead.settled = true;
  reopenedBatteryRead.resolve(ok({ available: true, percent: 50, status: 'Discharging', seconds_to_full: null, power_w: 10, source: 'none' })); await settle();
  assert.ok(find(page(obsoleteBattery, 'BatteryPage'), 'label', '50% · Discharging'));
  obsoleteBattery.unmount();
});

test('a failed status poll marks cached values; an obsolete failure is ignored', async () => {
  const failedStatus = await ready();
  failedStatus.tick(1500);
  failedStatus.respond('get_state', refused()); await settle();
  assert.match(find(failedStatus.render(), 'title', 'Vibration').props.note, /^Older data · Last read \d+s ago · Retrying…$/,
    'a failed poll must identify cached values as unverified on every overview row');
  assert.ok(find(failedStatus.render(), 'title', 'Vibration').props.onOpen, 'cached controls remain reachable during a transient status failure');
  open(failedStatus, 'Vibration');
  assert.ok(find(failedStatus.render(), 'label', 'Status refresh failed'), 'status failure remains visible on control pages');
  failedStatus.tick(1500);
  failedStatus.respond('get_state', ok(state)); await settle();
  assert.equal(find(failedStatus.render(), 'label', 'Status refresh failed'), undefined, 'a fresh successful poll clears the read error');
  failedStatus.unmount();

  const obsoleteStatus = await ready();
  obsoleteStatus.tick(1500);
  setVisible(obsoleteStatus, false); setVisible(obsoleteStatus, true);
  obsoleteStatus.fail('get_state'); await settle();
  assert.equal(find(obsoleteStatus.render(), 'label', 'Status refresh failed'), undefined, 'an obsolete poll failure cannot describe the reopened panel');
  obsoleteStatus.respond('get_state', ok(state)); await settle(); obsoleteStatus.unmount();
});

test('vibration edits: the last choice wins and audio waits for its readback', async () => {
  const returningGain = await ready(); open(returningGain, 'Vibration');
  const gainCallbacks = findComponent(returningGain.render(), 'VibrationPage').props;
  gainCallbacks.onGain(40); gainCallbacks.onGain(60); returningGain.fire(); await settle();
  assert.equal(returningGain.calls.find(call => call.name === 'set_vibration_gain').args[0], 60,
    'returning to the original gain in one frame cancels the intermediate choice');
  returningGain.respond('set_vibration_gain', ok(state)); await settle(); returningGain.unmount();

  const returningStrength = await ready(); open(returningStrength, 'Vibration');
  const strengthCallbacks = findComponent(returningStrength.render(), 'VibrationPage').props;
  strengthCallbacks.onLevel(3); strengthCallbacks.onLevel(2); returningStrength.fire(); await settle();
  assert.equal(returningStrength.calls.find(call => call.name === 'set_controller_with_vibration_feedback').args[0].vibration, 'medium',
    'returning to the original strength in one frame cancels the intermediate choice');
  returningStrength.respond('set_controller_with_vibration_feedback', ok(state)); await settle(); returningStrength.unmount();

  const checkingAudio = harness(); checkingAudio.render();
  checkingAudio.respond('get_state', ok({ ...state, audio_fix_enabled: true, audio_fix_installed: true,
    audio_fix_supported: false, audio_calibration_available: false, audio_profile: 'Pending' })); await settle(); open(checkingAudio, 'Audio');
  const checkingTree = page(checkingAudio, 'AudioPage');
  assert.ok(find(checkingTree, 'description', 'Waiting to verify the speaker profile.'), 'installed files alone do not confirm the actual speaker profile');
  assert.equal(find(checkingTree, 'title', 'Speaker calibration'), undefined, 'calibration remains unavailable until DSP readback');
  checkingAudio.unmount();
});

test('polling is single flight and hidden or unmounted answers are ignored; a remount shows cached summaries', async () => {
  const polling = harness({ visible: false }); polling.render(); assert.equal(polling.calls.length, 0, 'hidden content does not read hardware');
  setVisible(polling, true); polling.tick(); polling.tick(); assert.equal(count(polling, 'get_state'), 1, 'polling is single flight');
  setVisible(polling, false); let writes = polling.writes; polling.respond('get_state', ok(state)); await settle();
  assert.equal(polling.writes, writes, 'hidden response ignored');
  setVisible(polling, true); polling.unmount(); writes = polling.writes; polling.respond('get_state', ok(state)); await settle();
  assert.equal(polling.writes, writes, 'unmounted response ignored');

  const cached = await ready(); cached.unmount();
  assert.ok(find(cached.remount(), 'title', 'Vibration'), 'remount immediately shows confirmed summaries'); cached.unmount();
});

test('a vibration test waits for confirmed settings and never plays a failed one', async () => {
  const pendingTest = await ready(); open(pendingTest, 'Vibration');
  findComponent(pendingTest.render(), 'VibrationPage').props.onLevel(3);
  findComponent(pendingTest.render(), 'VibrationPage').props.onTest(); await settle();
  assert.equal(count(pendingTest, 'test_vibration'), 0, 'a vibration test waits for the debounced firmware setting');
  assert.equal(count(pendingTest, 'set_controller_with_vibration_feedback'), 1, 'test flushes the pending setting immediately');
  pendingTest.respond('set_controller_with_vibration_feedback', withController({ vibration: 'high' })); await settle();
  assert.equal(count(pendingTest, 'test_vibration'), 1, 'a test starts after the selected strength has been confirmed');
  const pendingVibration = page(pendingTest, 'VibrationPage');
  assert.equal(find(pendingVibration, 'label', 'FF Gain').props.disabled, true,
    'FF Gain cannot queue changes during the test');
  pendingTest.respond('test_vibration', { ok: true }); await settle(); pendingTest.unmount();

  const failedTest = await ready(); open(failedTest, 'Vibration');
  findComponent(failedTest.render(), 'VibrationPage').props.onGain(30);
  findComponent(failedTest.render(), 'VibrationPage').props.onTest(); await settle();
  assert.equal(count(failedTest, 'set_vibration_gain'), 1, 'test flushes pending gain');
  failedTest.respond('set_vibration_gain', refused()); await settle();
  assert.equal(count(failedTest, 'test_vibration'), 0, 'a failed setting must not play using the previous gain');
  assert.equal(findComponent(failedTest.render(), 'VibrationPage').props.testing, false);
  assert.equal(count(failedTest, 'get_state'), 2, 'failure immediately refreshes the restored controls after the test releases its action guard');
  failedTest.respond('get_state', ok(state)); await settle();
  assert.equal(findComponent(failedTest.render(), 'VibrationPage').props.state.controller.ff_gain, 60);
  failedTest.unmount();

  const queuedTest = await ready(); open(queuedTest, 'Vibration');
  findComponent(queuedTest.render(), 'VibrationPage').props.onGain(40); queuedTest.fire(); await settle();
  findComponent(queuedTest.render(), 'VibrationPage').props.onLevel(3);
  findComponent(queuedTest.render(), 'VibrationPage').props.onTest(); await settle();
  assert.equal(count(queuedTest, 'test_vibration'), 0, 'test waits for an in-flight write and its queued successor');
  queuedTest.respond('set_vibration_gain', withController({ ff_gain: 40 })); await settle();
  assert.equal(count(queuedTest, 'test_vibration'), 0, 'finishing the first write cannot bypass the queued latest strength');
  queuedTest.respond('set_controller_with_vibration_feedback', withController({ ff_gain: 40, vibration: 'high' })); await settle();
  assert.equal(count(queuedTest, 'test_vibration'), 1);
  queuedTest.respond('test_vibration', { ok: true }); await settle(); queuedTest.unmount();

  const failedStrength = await ready(); open(failedStrength, 'Vibration');
  findComponent(failedStrength.render(), 'VibrationPage').props.onLevel(3); failedStrength.fire(); await settle();
  findComponent(failedStrength.render(), 'VibrationPage').props.onGain(30);
  findComponent(failedStrength.render(), 'VibrationPage').props.onTest(); await settle();
  failedStrength.fail('set_controller_with_vibration_feedback'); await settle();
  failedStrength.respond('set_vibration_gain', withController({ ff_gain: 30 })); await settle();
  assert.equal(count(failedStrength, 'test_vibration'), 0, 'a successful gain patch cannot confirm a failed strength in the same drain');
  assert.equal(findComponent(failedStrength.render(), 'VibrationPage').props.testing, false);
  failedStrength.respond('get_state', withController({ ff_gain: 30 })); await settle(); failedStrength.unmount();

  const failedGain = await ready(); open(failedGain, 'Vibration');
  findComponent(failedGain.render(), 'VibrationPage').props.onGain(30); failedGain.fire(); await settle();
  findComponent(failedGain.render(), 'VibrationPage').props.onLevel(3);
  findComponent(failedGain.render(), 'VibrationPage').props.onTest(); await settle();
  failedGain.respond('set_vibration_gain', refused()); await settle();
  failedGain.respond('set_controller_with_vibration_feedback', withController({ vibration: 'high' })); await settle();
  assert.equal(count(failedGain, 'test_vibration'), 0, 'a successful strength patch cannot confirm a failed gain');
  failedGain.respond('get_state', withController({ vibration: 'high' })); await settle(); failedGain.unmount();

  const supersededStrength = await ready(); open(supersededStrength, 'Vibration');
  findComponent(supersededStrength.render(), 'VibrationPage').props.onLevel(3); supersededStrength.fire(); await settle();
  findComponent(supersededStrength.render(), 'VibrationPage').props.onLevel(1);
  findComponent(supersededStrength.render(), 'VibrationPage').props.onTest(); await settle();
  supersededStrength.respond('set_controller_with_vibration_feedback', refused()); await settle();
  supersededStrength.respond('set_controller_with_vibration_feedback', withController({ vibration: 'low' })); await settle();
  assert.equal(count(supersededStrength, 'test_vibration'), 1, 'a confirmed newer edit of the failed field permits the test');
  supersededStrength.respond('test_vibration', { ok: true }); await settle(); supersededStrength.unmount();

  const closingTest = await ready(); open(closingTest, 'Vibration');
  findComponent(closingTest.render(), 'VibrationPage').props.onGain(40);
  findComponent(closingTest.render(), 'VibrationPage').props.onTest(); await settle(); closingTest.unmount();
  const beforeClosingAck = closingTest.writes;
  closingTest.respond('set_vibration_gain', withController({ ff_gain: 40 })); await settle();
  assert.equal(count(closingTest, 'test_vibration'), 0, 'leaving an unmounted panel cannot start a delayed test');
  assert.equal(closingTest.writes, beforeClosingAck);
});

test('RGB edits are debounced, merged, queued and survive unmount and native dropdowns', async () => {
  const rgb = await ready(); const plugin = rgb.exports.default(); assert.equal(plugin.alwaysRender, true, 'Decky preserves content behind native dropdown');
  open(rgb, 'Lighting'); const hue = find(rgb.render(), 'label', 'Hue'), brightness = find(rgb.render(), 'label', 'Brightness');
  hue.props.onChange(120); hue.props.onChangeEnd(120); brightness.props.onChange(42); brightness.props.onChangeEnd(42);
  assert.equal(count(rgb, 'set_controller'), 0, 'slider movement is debounced');
  setVisible(rgb, false); assert.equal(count(rgb, 'set_controller'), 1, 'closing QAM flushes the latest RGB edit');
  const rgbCall = rgb.calls.find(c => c.name === 'set_controller');
  assert.equal(rgbCall.args[0].color, '00ff00'); assert.equal(rgbCall.args[0].brightness, 42, 'same-frame sibling slider edits merge');
  rgb.unmount(); let writes = rgb.writes; rgb.respond('set_controller', withController(rgbCall.args[0])); await settle();
  assert.equal(rgb.writes, writes, 'pending controller save completes without touching an unmounted view'); plugin.onDismount();

  const queue = await ready(); open(queue, 'Lighting'); find(queue.render(), 'label', 'LED mode').props.onChange({ data: 'pulse' }); queue.fire();
  find(queue.render(), 'label', 'LED mode').props.onChange({ data: 'rainbow' }); queue.unmount();
  assert.equal(count(queue, 'set_controller'), 1, 'controller writes do not overlap');
  writes = queue.writes; queue.respond('set_controller', withController({ rgb_mode: 'pulse' })); await settle();
  assert.equal(count(queue, 'set_controller'), 2, 'queued latest edit survives unmount during an earlier write');
  queue.respond('set_controller', withController({ rgb_mode: 'rainbow' })); await settle(); assert.equal(queue.writes, writes);

  const remounted = await ready(); open(remounted, 'Lighting');
  find(remounted.render(), 'label', 'Hue').props.onChange(120);
  find(remounted.render(), 'label', 'Hue').props.onChangeEnd(120);
  remounted.unmount(); await settle();
  const oldEdit = remounted.calls.find(call => call.name === 'set_controller');
  assert.deepEqual({ ...oldEdit.args[0] }, { color: '00ff00' }, 'color edit omits unrelated fields');
  remounted.remount(); remounted.respond('get_state', ok(state)); await settle(); open(remounted, 'Lighting');
  find(remounted.render(), 'label', 'Brightness').props.onChange(40);
  find(remounted.render(), 'label', 'Brightness').props.onChangeEnd(40);
  remounted.fire(); await settle();
  const newEdit = remounted.calls.filter(call => call.name === 'set_controller')[1];
  assert.deepEqual({ ...newEdit.args[0] }, { brightness: 40 }, 'a stale remounted view cannot overwrite the earlier pending color');
  writes = remounted.writes;
  remounted.respond('set_controller', withController({ color: '00ff00' })); await settle();
  assert.equal(remounted.writes, writes, 'earlier unmounted view cannot render its acknowledgement');
  remounted.respond('set_controller', withController({ color: '00ff00', brightness: 40 })); await settle();
  assert.ok(find(remounted.render(), 'label', '#00FF00'), 'merged backend acknowledgement retains both edits'); remounted.unmount();

  const saturation = await ready(); open(saturation, 'Lighting');
  find(saturation.render(), 'label', 'Saturation').props.onChange(50);
  find(saturation.render(), 'label', 'Saturation').props.onChangeEnd(50);
  saturation.fire(); await settle();
  const saturationEdit = saturation.calls.find(call => call.name === 'set_controller');
  assert.deepEqual(Object.keys(saturationEdit.args[0]), ['color'], 'saturation cannot replace a newer brightness or gain');
  saturation.respond('set_controller', withController(saturationEdit.args[0])); await settle(); saturation.unmount();

  const dropdown = await ready(); open(dropdown, 'Lighting'); const menu = find(dropdown.render(), 'label', 'LED mode');
  // The native menu hides Quick Access while it is open.
  setVisible(dropdown, false); menu.props.onChange({ data: 'pulse' }); dropdown.fire();
  setVisible(dropdown, true); assert.ok(find(dropdown.render(), 'label', 'LED mode'), 'native dropdown returns to the same section'); dropdown.unmount();

  const combined = await ready(); open(combined, 'Lighting');
  find(combined.render(), 'label', 'Hue').props.onChange(120);
  find(combined.render(), 'label', 'Hue').props.onChangeEnd(120);
  findComponent(combined.render(), 'PageHeader').props.onBack(); open(combined, 'Vibration');
  findComponent(combined.render(), 'VibrationPage').props.onGain(20); combined.fire();
  const controllerCommit = combined.calls.find(call => call.name === 'set_controller');
  assert.ok(controllerCommit); assert.equal(controllerCommit.args[0].ff_gain, 20);
  combined.respond('set_controller', withController(controllerCommit.args[0])); await settle();
  assert.equal(count(combined, 'set_vibration_gain'), 0, 'RGB plus gain uses one complete backend transaction');
  findComponent(combined.render(), 'VibrationPage').props.onGain(30); combined.fire();
  assert.equal(count(combined, 'set_vibration_gain'), 1, 'gain-only edit retains the evdev-only RPC');
  combined.respond('set_vibration_gain', withController({ ...controllerCommit.args[0], ff_gain: 30 })); await settle(); combined.unmount();
});

test('backend diagnostics, missing capabilities and an unavailable backend replace the controls', async () => {
  for (const diagnostic of [
    { extra: { settings_error: 'Invalid settings JSON; restore backup.' }, label: 'Settings require attention' },
    { extra: { startup_error: 'Backend startup failed' }, label: 'Settings require attention' },
    { extra: { initializing: true }, label: 'Starting hardware controls' },
    { extra: { supported: false }, title: 'Unsupported device' },
  ]) {
    const h = harness(); h.render();
    h.respond('get_state', ok({ ...state, ...diagnostic.extra })); await settle();
    const plugin = h.exports.default();  // its updater; its own Content element is not rendered here
    assert.ok(find(h.render(), diagnostic.label ? 'label' : 'title', diagnostic.label ?? diagnostic.title), 'backend diagnostics replace the controls');
    assert.equal(Boolean(find(h.render(), 'title', 'Updates')), Boolean(diagnostic.extra.settings_error || diagnostic.extra.startup_error),
      'a plugin that did not start keeps its updater');
    h.unmount(); plugin.onDismount();
  }

  const capabilities = harness(); capabilities.render();
  capabilities.respond('get_state', ok({ ...state, controller_supported: false, controller_error: 'kernel hid-ayaneo is active',
    module_eject_supported: false, module_reset_supported: false })); await settle();
  open(capabilities, 'Lighting');
  assert.equal(find(capabilities.render(), 'label', 'Enable LED control').props.disabled, true);
  assert.equal(find(capabilities.render(), 'label', 'Hue').props.disabled, true);
  assert.ok(find(capabilities.render(), 'label', 'Controller control unavailable'));
  findComponent(capabilities.render(), 'PageHeader').props.onBack(); open(capabilities, 'Magic Modules');
  const modulesTree = page(capabilities, 'MagicModulesPage');
  assert.equal(find(modulesTree, 'title', 'Eject left').props.disabled, true);
  assert.equal(find(modulesTree, 'title', 'Reset Magic Modules').props.disabled, true);
  assert.ok(find(modulesTree, 'description', 'kernel hid-ayaneo is active'));
  findComponent(capabilities.render(), 'PageHeader').props.onBack(); open(capabilities, 'Vibration');
  const vibrationTree = page(capabilities, 'VibrationPage');
  assert.equal(find(vibrationTree, 'label', 'Firmware strength · Medium').props.disabled, true);
  assert.ok(!find(vibrationTree, 'label', 'FF Gain').props.disabled, 'evdev gain remains independent of vendor HID');
  findComponent(capabilities.render(), 'PageHeader').props.onBack(); open(capabilities, 'Key binding');
  const buttonsTree = page(capabilities, 'ButtonsPage');
  assert.equal(find(buttonsTree, 'label', 'Fix key binding').props.disabled, true);
  assert.equal(find(buttonsTree, 'label', 'TM Guard').props.disabled, false, 'saved policy can still be disabled while HID is unavailable');
  capabilities.unmount();

  for (const failure of ['rejected', 'refused']) {
    const errors = harness(); errors.render();
    if (failure === 'rejected') errors.fail('get_state'); else errors.respond('get_state', refused('Backend is not ready'));
    await settle(); assert.ok(find(errors.render(), 'label', 'Backend unavailable'), `a ${failure} first read says why`);
    errors.tick(); errors.respond('get_state', ok({ ...state, settings_error: 'Invalid settings JSON; restore backup.' })); await settle();
    assert.ok(find(errors.render(), 'label', 'Settings require attention')); assert.equal(find(errors.render(), 'title', 'Hardware controls'), undefined); errors.unmount();
  }
});

test('About: the shared page with Updates last, on the plugin update RPCs, a ~/ path and its install text', async () => {
  const h = await ready(); const plugin = h.exports.default();
  open(h, 'About');
  assert.equal(count(h, 'get_version'), 1, 'About reads the home folder once');
  h.respond('get_version', { ok: true, version: '1.0.2', home: '/home/deck' }); await settle();
  const about = findComponent(h.render(), 'AboutPage').props;
  assert.equal(about.name, 'AYANEO 3 Companion'); assert.equal(about.version, '1.0.2'); assert.equal(about.home, '/home/deck');
  assert.match(about.install, /Install plugin from ZIP\. Your settings and per-game profiles are kept\.$/);
  void about.updates.run('check'); void about.updates.run('check');
  assert.equal(count(h, 'check_for_updates'), 1, 'one check at a time');
  h.respond('check_for_updates', { ok: true, current_version: '1.0.2', update_available: true, download_available: true,
    latest_version: '1.0.3', size: 1048576 }); await settle();
  void about.updates.run('download', '1.0.3');
  assert.deepEqual(h.calls.find(call => call.name === 'perform_update').args, ['1.0.3'], 'the download is bound to the shown version');
  h.respond('perform_update', { ok: true, version: '1.0.3', path: '/home/deck/Downloads/Ayaneo3Companion-1.0.3.zip', sha256: 'a' }); await settle();
  const tree = all(page(h, 'AboutPage'));
  assert.ok(tree.some(n => [].concat(n.props?.children ?? []).includes('~/Downloads/Ayaneo3Companion-1.0.3.zip')), 'the saved ZIP shows as ~/…');
  const sections = tree.filter(n => n.type === 'PanelSection').map(n => n.props.title);
  assert.equal(sections.at(-1), 'Updates', 'Updates is the last section of About');
  h.unmount(); plugin.onDismount();
  assert.equal(about.updates.getSnapshot().busy, null);
});

test('the Fan overview row reads Fan Control every second while shown', async () => {
  // Fan Control (fan.tsx, its own tests in fan.test.cjs).
  const fanState = { supported: true, error: '', active_curve: 'global',
    settings: { enabled: false, revision: 0, assign_tdp_presets: false, global_curve: [], presets: {} },
    telemetry: { active: false, automatic: true },
    readings: { valid: true, rpm: 2100, apu_temperature: 45, age_seconds: 0, automatic: true, error: '' } };
  const fanRow = await ready(); await settle();
  assert.equal(count(fanRow, 'fan_get_state'), 1, 'the overview reads Fan Control once it is shown');
  fanRow.respond('fan_get_state', ok(fanState)); await settle();
  assert.equal(find(fanRow.render(), 'title', 'Fan Control').props.description, 'APU 45.0°C · 2100 RPM · Automatic');
  fanRow.tick(1000); await settle();
  assert.equal(count(fanRow, 'fan_get_state'), 2, 'measured numbers are read again every second');
  fanRow.respond('fan_get_state', ok({ ...fanState, supported: false, error: 'Fan Control supports only the AYANEO 3.' })); await settle();
  assert.equal(find(fanRow.render(), 'title', 'Fan Control').props.description, 'APU 45.0°C · 2100 RPM · Not available on this console',
    'an unsupported fan keeps its row and says why');
  setVisible(fanRow, false); fanRow.tick(1000); await settle();
  assert.equal(count(fanRow, 'fan_get_state'), 2, 'a hidden overview reads nothing');
  fanRow.unmount();
});
