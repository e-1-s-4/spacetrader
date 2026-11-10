const { JSDOM, VirtualConsole } = require('jsdom');
const { spawn } = require('child_process');
const ids = JSON.parse(require('child_process').execFileSync('python3', ['-c', "import json,sys,os;sys.path.insert(0,os.path.join(sys.argv[1],'..'));import spacetrader as s;print(json.dumps({'goods':list(s.COMMODITIES),'ships':list(s.SHIP_TEMPLATES),'eq':list(s.EQUIPMENT_ITEMS),'crew':list(s.CREW_INDEX),'stocks':list(s.DEFAULT_STOCKS),'factions':list(s.FACTIONS)}))", __dirname]).toString());
const PORT = 3931;
const srv = spawn('python3', [require('path').join(__dirname, '..', '..', 'spacetrader.py'), '--port', String(PORT), '--seed', process.env.SEED || '3'],
  { env: { ...process.env, ST_SAVE_DIR: require('fs').mkdtempSync('/tmp/stuif_') } });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const errors = new Map();
const note = (k) => errors.set(k, (errors.get(k) || 0) + 1);
(async () => {
  await sleep(1200);
  const vc = new VirtualConsole();
  vc.on('jsdomError', e => note('jsdomError: ' + String(e.detail || e.message).split('\n')[0]));
  vc.on('error', (...e) => note('console.error: ' + e.map(String).join(' ').slice(0, 200)));
  const base = `http://127.0.0.1:${PORT}`;
  const html = await (await fetch(base + '/')).text();
  const dom = new JSDOM(html, { url: base + '/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    beforeParse(w) {
      w.fetch = (u, o) => fetch(new URL(u, base), o);
      w.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: () => () => ({ addColorStop(){}, width: 0 }), set: () => true });
      w.AudioContext = w.webkitAudioContext = function () { return new Proxy({ currentTime: 0, destination: {} }, { get: (t, k) => k in t ? t[k] : () => new Proxy({ frequency: {setValueAtTime(){}, exponentialRampToValueAtTime(){}, linearRampToValueAtTime(){}}, gain: {setValueAtTime(){}, exponentialRampToValueAtTime(){}, linearRampToValueAtTime(){}} }, { get: (t2, k2) => k2 in t2 ? t2[k2] : () => {} , set: () => true }) }); };
      w.scrollTo = () => {};
    } });
  const w = dom.window;
  w.addEventListener('error', e => note('window.onerror: ' + e.message));
  w.addEventListener('unhandledrejection', e => note('unhandledrejection: ' + e.reason));
  await sleep(800);
  const tabs = ['map','market','advisor','shipyard','services','crew','missions','bank','log'];
  const pick = a => a[Math.floor(Math.random() * a.length)];
  const ev = s => { try { return w.eval(s); } catch (e) { note('eval-throw: ' + s.slice(0, 40) + ' -> ' + e.message); } };
  const STEPS = Number(process.env.STEPS || 500);
  for (let i = 0; i < STEPS; i++) {
    ev(`switchTab('${pick(tabs)}')`);
    const gs = ev('gameState') || {};
    if (gs.active_combat && !gs.active_combat.is_finished) {
      ev(`sendCombatAction('${pick(['fire','target_engines','target_weapons','target_shields','missile','drones','recharge','evade','board','flee'])}')`);
    } else if (gs.active_combat) { ev('dismissCombat()'); }
    else if (gs.active_encounter) { ev(`resolveEncounter(${Math.random() < .5})`); }
    else if (gs.is_game_over || (gs.player && gs.player.is_game_over)) { ev(`sendAction('new_game',{name:'Fz${i}',difficulty:'${pick(['easy','normal','hard','nightmare'])}'})`); }
    else {
      const r = Math.random(); const g = pick(ids.goods);
      const act = r < .25 ? (() => { const d = (gs.all_planets || []).filter(p => !p.is_current && p.in_range).map(p => p.name); return d.length ? `sendAction('travel',{destination:${JSON.stringify(pick(d))}})` : `sendAction('buy_fuel',{amount:100})`; })()
        : r < .4 ? `sendAction('buy_commodity',{good:'${g}',qty:${pick([1,3,10])}})`
        : r < .5 ? `sendAction('sell_commodity',{good:'${g}',qty:${pick([1,3,10])}})`
        : r < .54 ? `sendAction('buy_fuel',{amount:100})`
        : r < .57 ? `sendAction('repair_hull',{hp:50})`
        : r < .6 ? `sendAction('buy_ship',{ship_id:'${pick(ids.ships)}'})`
        : r < .66 ? `sendAction('buy_equipment',{eq_id:'${pick(ids.eq)}'})`
        : r < .69 ? `sendAction('sell_equipment',{eq_id:'${pick(ids.eq)}'})`
        : r < .73 ? `sendAction('hire_crew',{crew_id:'${pick(ids.crew)}'})`
        : r < .8 ? (() => { const m = (gs.available_missions || []); return m.length ? `sendAction('accept_mission',{mission_id:'${pick(m).id}'})` : `sendAction('odd_jobs')`; })()
        : r < .85 ? `sendAction('${pick(['bank_deposit','bank_withdraw','bank_borrow','bank_repay'])}',{amount:${pick([100,1000,5000])}})`
        : r < .9 ? `sendAction('${pick(['buy_stock','sell_stock'])}',{symbol:'${pick(ids.stocks)}',qty:${pick([1,10])}})`
        : r < .93 ? `sendAction('buy_insurance')`
        : r < .96 ? `sendAction('faction_gift',{faction:'${pick(ids.factions)}',amount:1000})`
        : `sendAction('odd_jobs')`;
      ev(act);
    }
    await sleep(35);
    if (i % 25 === 0) { ev(`openTradeModal('${pick(ids.goods)}','${pick(['buy','sell'])}')`); ev('closeTradeModal && closeTradeModal()'); }
  }
  console.log('final day', ev('gameState.player.day'), 'steps', STEPS);
  console.log('ERRORS:', JSON.stringify([...errors.entries()], null, 1));
  srv.kill(); process.exit(errors.size ? 1 : 0);
})();
