import os, sys, json, random, tempfile, copy, traceback, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
d = tempfile.mkdtemp(); os.environ["ST_SAVE_DIR"] = d
import spacetrader as s
random.seed(5)
e = s.GameEngine(muted=True); e.new_game("Fz","normal")
for _ in range(40):
    e.buy_commodity("water", 1); e.advance_day() if hasattr(e,'advance_day') else None
ok, m = e.save_game("1"); print("save", ok, m)
f = [x for x in os.listdir(d) if x.startswith("st_odyssey_save")][0]
orig = json.load(open(os.path.join(d, f)))
def mutate(o, depth=0):
    o = copy.deepcopy(o)
    paths = []
    def walk(x, path):
        paths.append(path)
        if isinstance(x, dict):
            for k, v in x.items(): walk(v, path + [k])
        elif isinstance(x, list):
            for i, v in enumerate(x): walk(v, path + [i])
    walk(o, [])
    for _ in range(random.randint(1, 3)):
        paths = []
        walk(o, [])
        path = random.choice(paths)
        if not path: continue
        cur = o
        for k in path[:-1]: cur = cur[k]
        bad = random.choice([None, "x", -1, 10**12, 1.5, [], {}, True, float("nan"), -10**9, 0])
        if random.random() < .15 and isinstance(cur, dict): del cur[path[-1]]
        else: cur[path[-1]] = bad
    return o
bad = collections.Counter(); samples = {}
for i in range(1500):
    m = mutate(orig)
    open(os.path.join(d, f), "w").write(json.dumps(m))
    e2 = s.GameEngine(muted=True); e2.new_game("Y","normal"); before = e2.calculate_net_worth()
    try:
        ok, msg = e2.load_game("1")
        if ok:
            # loaded; then play a few things
            sess = s.GameSession(); sess.engine = e2
            for a in [{"action":"buy_commodity","good":"water","qty":1},{"action":"travel","destination":random.choice(list(e2.planets))},{"action":"sell_commodity","good":"water","qty":1}]:
                try: s.handle_action(sess, a)
                except s.ActionError: pass
            json.dumps(s.serialize_game_state(sess))
            p = e2.player
            if p.credits < 0 or p.hull > p.max_hull or p.fuel > p.max_fuel or p.cargo_used() > p.cargo_cap or p.loan < 0:
                bad["loaded-insane"] += 1; samples.setdefault("loaded-insane", (p.credits,p.hull,p.max_hull,p.fuel,p.max_fuel,p.cargo_used(),p.cargo_cap,p.loan))
        else:
            if e2.calculate_net_worth() != before: bad["failed-load-mutated-state"] += 1
    except Exception:
        k = traceback.format_exc().strip().splitlines()[-1][:100]
        bad["EXC " + k] += 1; samples.setdefault("EXC " + k, traceback.format_exc()[-700:])
print(dict(bad))
for k, v in samples.items(): print("=====", k); print(v)
