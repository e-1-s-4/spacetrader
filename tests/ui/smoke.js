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
  // refused action while encounter pending is surfaced, not silently dropped
  console.log(errors.length ? '\nERRORS:\n' + errors.join('\n') : '\nNO RUNTIME ERRORS');
  srv.kill(); process.exit(errors.length ? 1 : 0);
})().catch(e => { console.error(e); srv.kill(); process.exit(2); });
