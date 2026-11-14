import os, sys, json, random, tempfile, traceback, collections
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ST_SAVE_DIR"] = tempfile.mkdtemp()
import spacetrader as s

LOADED=[False]
def inv(sess, tag, prev):
    e = sess.engine; p = e.player; errs = []
    def chk(c, m):
        if not c: errs.append(m)
    chk(p.credits >= 0, f"credits {p.credits}")
    chk(p.savings >= 0, f"savings {p.savings}")
    chk(p.loan >= 0, f"loan {p.loan}")
    chk(0 <= p.hull <= p.max_hull, f"hull {p.hull}/{p.max_hull}")
    chk(0 <= p.shield <= p.max_shield, f"shield {p.shield}/{p.max_shield}")
    chk(0 <= p.fuel <= p.max_fuel, f"fuel {p.fuel}/{p.max_fuel}")
    chk(p.cargo_used() <= p.cargo_cap, f"cargo {p.cargo_used()}>{p.cargo_cap}")
    for g, q in p.cargo.items():
        chk(q >= 0, f"neg cargo {g}")
        chk(g in s.COMMODITIES, f"bad cargo {g}")
    for m in p.active_missions:
        if not (m.completed or m.failed) and m.cargo_good:
            chk(p.cargo.get(m.cargo_good, 0) >= m.cargo_qty, f"reserved cargo missing {m.cargo_good}")
    chk(p.missiles >= 0 and p.missiles <= s.PLAYER_MISSILE_CAP + 50, f"missiles {p.missiles}")
    chk(all(v >= 0 for v in p.stocks_owned.values()), "neg stock")
    chk(s.MIN_CREDIT_SCORE <= p.credit_score <= s.MAX_CREDIT_SCORE, f"score {p.credit_score}")
    chk(len(p.hired_crew) <= s.MAX_CREW, "crew over cap")
    chk(len([m for m in p.active_missions if not (m.completed or m.failed)]) <= s.MAX_ACTIVE_MISSIONS, "missions over cap")
    chk(len(set(p.hired_crew)) == len(p.hired_crew), "dup crew")
    chk(p.location in e.planets, "bad loc")
    chk(all(s.REPUTATION_MIN <= v <= s.REPUTATION_MAX for v in p.reputation.values()), f"rep {p.reputation}")
    chk(all(v >= 0 for k,v in p.stats.items() if k!="trading_profit"), f"neg stat {[k for k,v in p.stats.items() if v<0]}")
    if prev is not None and not LOADED[0]: chk(p.day >= prev, f"day went back {prev}->{p.day}")
    for pl in e.planets.values():
        for g, q in pl.stock.items():
            chk(q >= 0, f"neg planet stock {pl.name} {g} {q}")
        for g, pr in pl.prices.items() if hasattr(pl, 'prices') else []:
            chk(pr > 0, f"nonpositive price {pl.name} {g} {pr}")
    for g in s.COMMODITIES:
        try:
            b, sl = e.get_buy_price(g), e.get_sell_price(g)
            chk(b > 0 and sl > 0, f"price<=0 {g} {b} {sl}")
            chk(sl <= b, f"sell>buy {g} {sl}>{b}")
        except Exception as ex:
            errs.append(f"price exc {g} {ex}")
    nw = e.calculate_net_worth()
    chk(isinstance(nw, int) or isinstance(nw, float), "nw type")
    try: json.dumps(s.serialize_game_state(sess))
    except Exception as ex: errs.append(f"serialize: {ex!r}")
    return errs

def run(seed, steps):
    random.seed(seed)
    diff = random.choice(["easy", "normal", "hard", "nightmare"])
    s.random.seed(seed)
    sess = s.GameSession(difficulty=diff, player_name=f"Bot{seed}")
    e = sess.engine
    problems = collections.Counter(); samples = {}
    prev = e.player.day; counts = collections.Counter()
    names = list(e.planets)
    def act(d):
        try:
            r = s.handle_action(sess, d)
            counts[d["action"]] += 1
            return r
        except s.ActionError:
            return None
        except Exception:
            problems["EXC "+d["action"]] += 1
            samples.setdefault("EXC "+d["action"], (seed, d, traceback.format_exc()))
            return None
    for i in range(steps):
        p = e.player
        R = random.random
        if sess.active_combat is not None and not sess.active_combat.is_finished:
            act({"action": "combat_action", "combat_action": random.choice(s.CombatEncounter.ACTIONS)})
        elif sess.active_combat is not None:
            act({"action": "dismiss_combat"})
        elif sess.active_encounter:
            act({"action": "resolve_encounter", "choice": R() < .5})
        elif e.is_game_over:
            LOADED[0] = True
            act({"action": "new_game", "name": "Bot", "difficulty": random.choice(["easy","normal","hard","nightmare"])})
        else:
            c = random.random()
            g = random.choice(list(s.COMMODITIES)); q = random.choice([1, 2, 5, 10, 50, 0, -3, 10**7])
            if c < .22:
                if p.fuel < 40 and R() < .8: act({"action": "buy_fuel", "amount": 100})
                else:
                    near = sorted(names, key=lambda n: e.calculate_distance(e.current_planet, e.planets[n]) if n != p.location else 99999)[:5]
                    act({"action": "travel", "destination": random.choice(near)})
            elif c < .37: act({"action": "buy_commodity", "good": g, "qty": q})
            elif c < .52: act({"action": "sell_commodity", "good": g, "qty": q})
            elif c < .56: act({"action": "buy_fuel", "amount": random.choice([5, 20, 1000, -1])})
            elif c < .59: act({"action": "repair_hull", "hp": random.choice([5, 50, 1000, -1])})
            elif c < .60: act({"action": "repair_subsystems"})
            elif c < .61: act({"action": "buy_insurance"})
            elif c < .63: act({"action": "buy_missiles", "qty": random.choice([1, 5, 100])})
            elif c < .65: act({"action": "buy_ship", "ship_id": random.choice(list(s.SHIP_TEMPLATES))})
            elif c < .70: act({"action": random.choice(["buy_equipment", "sell_equipment"]), "eq_id": random.choice(list(s.EQUIPMENT_ITEMS))})
            elif c < .72: act({"action": random.choice(["hire_crew", "dismiss_crew"]), "crew_id": random.choice(list(s.CREW_INDEX))})
            elif c < .78:
                ms = [m for m in e.available_missions if m.origin == p.location and not m.completed and not m.failed]
                am = p.active_missions
                bounties = [m for m in am if m.m_type == "bounty" and m.destination == p.location]
                if bounties and R() < .5: act({"action": "engage_bounty", "mission_id": random.choice(bounties).id})
                elif ms and R() < .7: act({"action": "accept_mission", "mission_id": random.choice(ms).id})
                elif am: act({"action": "abandon_mission", "mission_id": random.choice(am).id})
                else: act({"action": "accept_mission", "mission_id": "x"})
            elif c < .84: act({"action": random.choice(["bank_deposit","bank_withdraw","bank_borrow","bank_repay"]), "amount": random.choice([100, 1000, 50000, -5, 0, 10**9])})
            elif c < .89: act({"action": random.choice(["buy_stock","sell_stock"]), "symbol": random.choice(list(s.DEFAULT_STOCKS)), "qty": random.choice([1, 10, 100, -1])})
            elif c < .91: act({"action": "odd_jobs"})
            elif c < .93: act({"action": "jettison_cargo", "good": g, "qty": random.choice([1, 5, 100])})
            elif c < .95: act({"action": "faction_gift", "faction": random.choice(s.FACTIONS), "amount": random.choice([100, 1000, 10**6])})
            elif c < .97: act({"action": "save_game", "slot": str(random.choice([1, 2, 3, 99]))})
            else:
                LOADED[0]=True
                act({"action": "load_game", "slot": random.choice(["1", "2", "auto", "zzz"])})
        errs = inv(sess, i, prev); LOADED[0]=False
        prev = sess.engine.player.day if not False else prev
        for er in errs:
            k = er.split(" ")[0] + " " + er.split(" ")[1] if " " in er else er
            problems["INV " + k] += 1
            samples.setdefault("INV " + k, (seed, i, er, [x for x in counts.most_common(3)]))
        e = sess.engine
        prev = e.player.day
        names = list(e.planets)
    return problems, samples, counts

if __name__ == "__main__":
    n = int(sys.argv[1]); steps = int(sys.argv[2]); start = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    P = collections.Counter(); S = {}; C = collections.Counter()
    for sd in range(start, start + n):
        p, sm, c = run(sd, steps); P.update(p); C.update(c)
        for k, v in sm.items(): S.setdefault(k, v)
    print("actions:", dict(C))
    print("PROBLEMS:", dict(P))
    for k, v in S.items():
        print("=====", k); print(v[0], v[1], v[2] if isinstance(v[2], str) else v[2:])
