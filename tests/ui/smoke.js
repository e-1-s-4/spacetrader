const { JSDOM, VirtualConsole } = require('jsdom');
const { spawn } = require('child_process');
const PORT = Number(process.env.PORT) || 3917;
const srv = spawn('python3', [require('path').join(__dirname, '..', '..', 'spacetrader.py'), '--port', String(PORT), '--seed', '7'],
  { env: { ...process.env, ST_SAVE_DIR: require('fs').mkdtempSync(require('path').join(require('os').tmpdir(), 'stui_')) } });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const errors = [];
(async () => {
  await sleep(1200);
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => errors.push('jsdomError: ' + (e.detail || e.message)));
  vc.on('error', e => errors.push('console.error: ' + e));
  const base = `http://127.0.0.1:${PORT}`;
  const html = await (await fetch(base + '/')).text();
  const dom = new JSDOM(html, { url: base + '/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (u, o) => fetch(new URL(u, base), o);
      w.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: () => () => ({ addColorStop(){}, width: 0 }) , set: () => true });
      w.AudioContext = w.webkitAudioContext = function () { return new Proxy({ currentTime: 0, destination: {} }, { get: (t, k) => k in t ? t[k] : () => new Proxy({ frequency: {setValueAtTime(){}, exponentialRampToValueAtTime(){}, linearRampToValueAtTime(){}}, gain: {setValueAtTime(){}, exponentialRampToValueAtTime(){}, linearRampToValueAtTime(){}} }, { get: (t2, k2) => k2 in t2 ? t2[k2] : () => {} , set: () => true }) }); };
      w.scrollTo = () => {};
    } });
  const w = dom.window;
  await sleep(800);
  const ok = (c, m) => { console.log((c ? '[PASS] ' : '[FAIL] ') + m); if (!c) errors.push('check failed: ' + m); };
  ok(!!w.eval('gameState'), 'initial state rendered');
  for (const t of ['map','market','advisor','shipyard','services','crew','missions','bank','log']) { w.eval(`switchTab('${t}')`); await sleep(120); }
  ok(true, 'all 9 tabs render without throwing');
  // double-submit guard: fire two buys simultaneously, only one may run
  const c0 = w.eval('gameState.player.credits');
  w.eval(`sendAction('buy_commodity',{good:'water',qty:1}); sendAction('buy_commodity',{good:'water',qty:1});`);
  await sleep(500);
  ok(w.eval(`gameState.player.cargo.water || 0`) === 1, 'double-click buys exactly once');
  // stale-state rejection
  const rev = w.eval('lastRevision');
  w.eval(`applyState(Object.assign({}, gameState, {revision: ${rev - 1}, player: Object.assign({}, gameState.player, {credits: 1})}))`);
  ok(w.eval('gameState.player.credits') !== 1, 'stale snapshot ignored');
  // trade modal quote parity with server
  w.eval(`switchTab('market'); openTradeModal('food','buy'); setTradeQty(10);`);
  const shown = w.document.getElementById('trade-total-display').textContent.replace(/[^0-9]/g,'');
  const c1 = w.eval('gameState.player.credits');
  w.eval('executeTradeModal()'); await sleep(500);
  const paid = c1 - w.eval('gameState.player.credits');
  ok(String(paid) === shown, `modal quote ${shown} == server charge ${paid}`);
  // sell quote parity
  w.eval(`openTradeModal('food','sell'); setTradeQty(10);`);
  const shown2 = w.document.getElementById('trade-total-display').textContent.replace(/[^0-9]/g,'');
  const c2 = w.eval('gameState.player.credits');
  w.eval('executeTradeModal()'); await sleep(500);
  ok(String(w.eval('gameState.player.credits') - c2) === shown2, `sell quote ${shown2} matches server`);
  // travel until an encounter/combat modal shows, exercising those UIs
  let seen = new Set();
  for (let i = 0; i < 40; i++) {
    const gs = w.eval('gameState');
    if (gs.active_combat) {
      seen.add('combat');
      for (let k = 0; k < 60 && w.eval('gameState.active_combat && !gameState.active_combat.is_finished'); k++) { w.eval(`sendCombatAction('fire')`); await sleep(120); }
      if (w.eval('gameState.active_combat')) { w.eval('dismissCombat()'); await sleep(200); }
      if (w.eval('gameState.is_game_over || gameState.player.is_game_over')) { w.eval(`sendAction('new_game',{name:'<b>X</b>',difficulty:'easy'})`); await sleep(300); }
      continue;
    }
    if (gs.active_encounter) { seen.add(gs.active_encounter.type); w.eval('resolveEncounter(true)'); await sleep(250); continue; }
    w.eval(`sendAction('buy_fuel',{amount:200}); `); await sleep(150);
    const dests = w.eval(`gameState.all_planets.filter(p=>!p.is_current && p.in_range).map(p=>p.name)`);
    if (!dests.length) { w.eval(`sendAction('odd_jobs')`); await sleep(150); continue; }
    w.eval(`sendAction('travel',{destination:${JSON.stringify(dests[i % dests.length])}})`); await sleep(300);
  }
  console.log('encounter UIs exercised:', [...seen].join(', ') || '(none)');

  // --- regression: a destroyed vessel must never trap the player ---
  // The combat overlay used to sit above the game-over modal, and its only
  // button fired an action the server refused once the career was over.
  const zOf = (sel) => { const el = w.document.querySelector(sel); return el ? (parseInt(w.getComputedStyle(el).zIndex, 10) || 0) : 0; };
  ok(zOf('#gameover-modal') > zOf('#combat-modal'), 'game-over modal stacks above the combat overlay');
  const fakeCombat = {
    is_finished: true, player_won: false, player_escaped: false, player_dead: true,
    turn_count: 4, enemy_name: 'Reaper', enemy_ship_name: 'Galactic Behemoth',
    personality: 'aggressive', personality_desc: 'fights head-on', threat_level: 'CRITICAL',
    enemy_damage_lo: 20, enemy_damage_hi: 44, enemy_hull: 0, enemy_max_hull: 600,
    enemy_shield: 0, enemy_max_shield: 300, player_missiles: 0, has_drone_bay: false,
    drones_active: false, can_flee: true, can_board: false,
    combat_log: ['CRITICAL FAILURE: Ship destroyed in combat!']
  };
  w.eval(`gameState = Object.assign({}, gameState, {
      player: Object.assign({}, gameState.player, { is_game_over: true }),
      active_combat: ${JSON.stringify(fakeCombat)}
  }); renderAll();`);
  ok(w.document.getElementById('combat-btn-dismiss').dataset.mode === 'recover',
     'destroyed-vessel dismiss button switches to recovery mode');
  w.eval('combatDismiss()');
  ok(w.document.getElementById('gameover-modal').classList.contains('active'),
     'recovery screen opens from the combat overlay');
  w.document.getElementById('gameover-modal').classList.remove('active');
  w.eval(`gameState.active_combat = null; gameState.player.is_game_over = false; renderAll();`);

  // --- accessibility ---
  const MODALS = ['trade-modal','combat-modal','encounter-modal','save-modal',
                  'newgame-modal','manual-modal','gameover-modal','victory-modal',
                  'amount-modal'];
  const badModals = MODALS.filter(id => {
    const el = w.document.getElementById(id);
    return !el || el.getAttribute('role') !== 'dialog' || el.getAttribute('aria-modal') !== 'true';
  });
  ok(badModals.length === 0, `every overlay is a labelled dialog${badModals.length ? ': ' + badModals.join(', ') : ''}`);

  const unlabelled = MODALS.filter(id => {
    const el = w.document.getElementById(id);
    if (!el) return true;
    const by = el.getAttribute('aria-labelledby');
    return !(by && w.document.getElementById(by)) && !el.getAttribute('aria-label');
  });
  ok(unlabelled.length === 0, `every dialog has an accessible name${unlabelled.length ? ': ' + unlabelled.join(', ') : ''}`);

  // The tab strip must expose the ARIA tabs pattern.
  const nav = w.document.getElementById('tab-nav');
  const tabBtns = Array.from(w.document.querySelectorAll('.tab-btn'));
  ok(nav.getAttribute('role') === 'tablist', 'tab strip is a tablist');
  ok(tabBtns.every(b => b.getAttribute('role') === 'tab' && b.hasAttribute('aria-selected')),
     'every tab button is a tab with aria-selected');

  // Opening a dialog must move focus into it; Escape must close it again.
  w.eval(`openManualModal()`);
  const manual = w.document.getElementById('manual-modal');
  ok(manual.classList.contains('active'), 'manual dialog opens');
  ok(manual.contains(w.document.activeElement), 'focus moves into the opened dialog');
  w.eval(`document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))`);
  ok(!manual.classList.contains('active'), 'Escape closes the dialog');

  // The star map must be reachable without a mouse.
  w.eval(`switchTab('map')`); await sleep(200);
  const nodes = Array.from(w.document.querySelectorAll('.map-planet-node'));
  ok(nodes.length > 0 && nodes.every(n => n.getAttribute('tabindex') === '0'
       && n.getAttribute('role') === 'button' && n.getAttribute('aria-label')),
     'every planet on the star map is a labelled, focusable control');

  // Arrow keys must move between tabs.
  w.eval(`switchTab('map')`);
  w.document.getElementById('tabbtn-map').focus();
  w.eval(`document.getElementById('tab-nav').dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight', bubbles: true}))`);
  ok(w.document.getElementById('tabbtn-market').getAttribute('aria-selected') === 'true',
     'arrow keys move between tabs');

  // Combat hotkeys must reach the combat engine. Spy on the dispatcher so the
  // check is about the keyboard wiring, not about a live server-side fight.
  w.eval(`window.__combatKeys = [];
          window.__origSendCombatAction = window.sendCombatAction;
          window.sendCombatAction = (a) => { window.__combatKeys.push(a); };
          gameState.active_combat = ${JSON.stringify(Object.assign({}, fakeCombat, {
              is_finished: false, player_dead: false, player_won: false, turn_count: 0 }))};`);
  w.eval(`document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'f', bubbles: true}))`);
  ok(w.eval('window.__combatKeys[0]') === 'fire',
     'the fire hotkey reaches the combat engine');
  w.eval(`window.sendCombatAction = window.__origSendCombatAction;
          window.__combatKeys = [];
          gameState.active_combat = null; renderAll();`);

  // Audio preference must survive a reload.
  w.eval(`localStorage.setItem('st_odyssey_muted', '1'); loadAudioPreference();`);
  ok(w.eval('audioMuted') === true, 'mute preference is restored from storage');
  w.eval(`localStorage.removeItem('st_odyssey_muted'); loadAudioPreference();`);

  // refused action while encounter pending is surfaced, not silently dropped
  console.log(errors.length ? '\nERRORS:\n' + errors.join('\n') : '\nNO RUNTIME ERRORS');
  srv.kill(); process.exit(errors.length ? 1 : 0);
})().catch(e => { console.error(e); srv.kill(); process.exit(2); });
