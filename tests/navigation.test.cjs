// Overview navigation in LegionGo2Companion's style: pages survive QAM overlays, B and ‹ All Controls go back one
// page (asking first when a page has unsaved changes), back restores the opened row, and polling follows the tab's
// real visibility.
const test = require('node:test');
const assert = require('node:assert/strict');
const { harness, all, settle } = require('./frontend_harness.cjs');

const state = {
  supported: true, device: 'AYANEO 3', version: '1.2.0',
  controller: { vibration: 'medium', ff_gain: 60, rgb_mode: 'solid', color: 'ff0000', brightness: 70 },
  module_left: { label: 'Gamepad', layout: '', code: null }, module_right: { label: 'Gamepad', layout: '', code: null },
  modules_connected: true, charge_bypass: false, audio_fix_enabled: false, button_fix_installed: false,
  tm_guard_enabled: true, screen_installed: false, screen_conflict: false, edid_game_nits: 0,
};
const find = (tree, predicate) => all(tree).find(predicate);

const panel = () => {
  let intersecting = true;
  const shell = { observed: null, onBack: undefined, dialogs: [] };
  class FakeObserver {
    constructor(callback) { this.callback = callback; shell.observed = this; }
    observe() {} disconnect() { if (shell.observed === this) shell.observed = null; }
  }
  const element = { ownerDocument: { defaultView: { IntersectionObserver: FakeObserver } },
    getClientRects: () => intersecting ? [{}] : [] };
  const h = harness({
    keep: ['SectionLink', 'PageHeader'],
    responses: { get_state: () => ({ ok: true, state: structuredClone(state) }),
      get_settings: { ok: true, state: { enabled: true, running: { spl: 15, sppt: 25, fppt: 30, preset: 'balanced', game: false } } },
      get_battery_status: { ok: true, state: { available: true, percent: 80, status: 'Discharging', seconds_to_full: null, power_w: 8, source: 'none' } } },
    modules: {
      './about': { AboutPage: 'AboutPage', UpdateSection: 'UpdateSection', INSTALL_HINT: '', UpdateController: class { dispose() {} } },
      './tdp': { TdpPage: 'TdpPage', startTdpWatcher() {}, stopTdpWatcher() {} },
      './device_pages': new Proxy({}, { get: (_, key) => key }),
      './fan': { FanPage: 'FanPage', fanSummary: () => 'APU 45.0°C · 2000 RPM · Automatic', getFanState: async () => ({ supported: true }) },
      // The page shell with the element Steam would attach to its ref; it hands the test its B handler.
      './ui': {
        PageShell({ children, rootRef, onBack }) { if (rootRef) rootRef.current = element; shell.onBack = onBack; return children; },
        PageHeader() {}, SectionLink() {},
        confirmAction: (...args) => { shell.dialogs.push(args); },
        infoRow: (label, description) => ({ type: 'Field', props: { label, description } }),
        notify() {},
      },
    },
  });
  return { h, shell, hideTab: value => { intersecting = !value; } };
};

test('pages survive overlays, B goes back with the focus on the row, polling follows tab visibility', async () => {
  const { h, shell } = panel();
  const polling = () => h.intervals.some(timer => !timer.done && timer.ms === 1500);

  h.render(); await settle(); h.render(); await settle();
  const overview = h.render();
  assert.equal(shell.onBack, undefined, 'on the overview B is Steam\'s: it closes the menu');
  const titles = ['TDP', 'Fan Control', 'Vibration', 'Lighting', 'Key binding', 'Magic Modules', 'Battery', 'Audio', 'OLED display'];
  for (const title of titles) {
    const row = find(overview, n => n.props?.title === title && n.props?.onOpen);
    assert.ok(row, `overview row: ${title}`);
    assert.ok(row.props.icon, `${title} carries its section icon`);
  }
  assert.equal(find(overview, n => n.props?.title === 'About').props.description, 'Version 1.2.0 · updates');
  for (const title of [...titles, 'About']) {
    find(h.render(), n => n.props?.title === title && n.props?.onOpen).props.onOpen();
    for (let cycle = 0; cycle < 3; cycle++) {
      h.hide(); h.render();
      assert.ok(!polling(), 'hidden QAM does not poll');
      h.show();
      assert.ok(find(h.render(), n => n.props?.title === title && n.props?.back === 'All Controls'),
        `${title} stays open after a dropdown, confirmation or QAM reopening`);
    }
    assert.equal(typeof shell.onBack, 'function', `B on ${title} goes back one page`);
    shell.onBack();
    const row = find(h.render(), n => n.props?.title === title && n.props?.onOpen);
    assert.equal(row.props.focus, true, `back restores focus to ${title}`);
    assert.ok(polling(), 'the visible overview polls');
  }
  assert.ok(shell.observed, 'the panel watches whether Steam shows the Decky tab');
  shell.observed.callback([{ isIntersecting: false }]); h.render();
  assert.ok(!polling(), 'a tab hidden behind another Quick Access tab stops polling');
  shell.observed.callback([{ isIntersecting: true }]); h.render();
  assert.ok(polling(), 'polling resumes when the tab is shown again');
});

test('leaving a page with unsaved changes asks "Discard changes?" first; Cancel stays, Discard leaves', async () => {
  const { h, shell } = panel();
  h.render(); await settle(); h.render(); await settle();
  for (const [title, page] of [['Fan Control', 'FanPage'], ['TDP', 'TdpPage']]) {
    find(h.render(), n => n.props?.title === title && n.props?.onOpen).props.onOpen();
    const guard = find(h.render(), n => n.type === page).props.guardLeave;
    assert.equal(typeof guard, 'function', `${title} reports its unsaved changes`);
    let lost = 'Your unsaved changes are lost.';
    guard(() => lost);
    shell.onBack();
    assert.equal(shell.dialogs.length, 1, 'B asks first');
    const [question, description, ok, onOK, options] = shell.dialogs.pop();
    assert.deepEqual(JSON.parse(JSON.stringify([question, description, ok, options])), ['Discard changes?', lost, 'Discard', { destructive: true }]);
    assert.ok(find(h.render(), n => n.type === page), 'Cancel (no answer) keeps the page with its edits');
    find(h.render(), n => n.props?.back === 'All Controls').props.onBack();
    assert.equal(shell.dialogs.length, 1, '‹ All Controls asks the same');
    shell.dialogs.pop();
    onOK();
    assert.ok(find(h.render(), n => n.props?.title === title && n.props?.onOpen), 'Discard goes back');
    // Nothing unsaved: back at once, no dialog.
    find(h.render(), n => n.props?.title === title && n.props?.onOpen).props.onOpen(); h.render();
    lost = null;
    shell.onBack();
    assert.equal(shell.dialogs.length, 0);
    assert.ok(find(h.render(), n => n.props?.title === title && n.props?.onOpen));
  }
});
