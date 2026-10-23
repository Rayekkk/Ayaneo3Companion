// Overview navigation in LegionGo2Companion's style: pages survive QAM overlays,
// back restores the opened row, and polling follows the tab's real visibility.
const { assert, run, mount, find, settle } = require('./frontend_harness.cjs');

const state = {
  supported: true, device: 'AYANEO 3', version: '1.2.0',
  controller: { vibration: 'medium', ff_gain: 60, rgb_mode: 'solid', color: 'ff0000', brightness: 70 },
  module_left: { label: 'Gamepad', layout: '', code: null }, module_right: { label: 'Gamepad', layout: '', code: null },
  modules_connected: true, charge_bypass: false, audio_fix_enabled: false, button_fix_installed: false,
  tm_guard_enabled: true, screen_installed: false, screen_conflict: false, edid_game_nits: 0,
};
const component = name => name;
let intersecting = true, observed = null;
class FakeObserver {
  constructor(callback) { this.callback = callback; observed = this; }
  observe() {} disconnect() { if (observed === this) observed = null; }
}
const element = { ownerDocument: { defaultView: { IntersectionObserver: FakeObserver } },
  getClientRects: () => intersecting ? [{}] : [] };
const h = mount('index.tsx', {
  component: 'TestedContent', append: 'export { Content as TestedContent };',
  responses: { get_state: () => structuredClone(state), set_active_app: null,
    get_settings: { enabled: true, running: { spl: 15, sppt: 25, fppt: 30, preset: 'balanced', game: false } },
    get_battery_status: { available: true, percent: 80, status: 'Discharging', seconds_to_full: null, power_w: 8, source: 'none' } },
  modules: name => ({
    './updates': { resetUpdates() {} },
    './tdp': { TdpPage: component('TdpPage'), startTdpWatcher() {}, stopTdpWatcher() {} },
    './about': { AboutPage: component('AboutPage') },
    './device_pages': new Proxy({}, { get: (_, key) => key }),
    './fan': { FanPage: 'FanPage', fanSummary: () => 'APU 45.0°C · 2000 RPM · Automatic', getFanState: async () => ({ supported: true }) },
    './model': {},
  })[name],
  onRender: tree => { const root = find(tree, n => n.props?.rootRef); if (root) root.props.rootRef.current = element; },
});
const polling = () => h.intervals().some(timer => timer.delay === 1500);

run(async () => {
  h.render(); await settle(); h.render(); await settle();
  const overview = h.render();
  const titles = ['TDP', 'Fan Control', 'Vibration', 'Lighting', 'Key Binding', 'Magic Modules', 'Battery', 'Audio', 'OLED Display'];
  for (const title of titles) {
    const row = find(overview, n => n.props?.title === title && n.props?.onClick);
    assert.ok(row, `overview row: ${title}`);
    assert.ok(row.props.section, `${title} carries its section icon`);
  }
  for (const title of [...titles, 'About']) {
    find(h.render(), n => n.props?.title === title && n.props?.onClick).props.onClick();
    for (let cycle = 0; cycle < 3; cycle++) {
      h.hide();
      assert.ok(!polling(), 'hidden QAM does not poll');
      h.show();
      assert.ok(find(h.render(), n => n.props?.title === title && n.props?.onBack),
        `${title} stays open after a dropdown, confirmation or QAM reopening`);
    }
    find(h.render(), n => n.props?.onBack).props.onBack();
    const row = find(h.render(), n => n.props?.title === title && n.props?.onClick);
    assert.equal(row.props.restoreFocus, true, `back restores focus to ${title}`);
    assert.ok(polling(), 'the visible overview polls');
  }
  assert.ok(observed, 'the panel watches whether Steam shows the Decky tab');
  observed.callback([{ isIntersecting: false }]); h.render();
  assert.ok(!polling(), 'a tab hidden behind another Quick Access tab stops polling');
  observed.callback([{ isIntersecting: true }]); h.render();
  assert.ok(polling(), 'polling resumes when the tab is shown again');
  console.log('Frontend navigation: PASS (pages survive overlays, focus returns, polling follows tab visibility)');
});
