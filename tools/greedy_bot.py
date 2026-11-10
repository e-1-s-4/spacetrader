import os, sys, tempfile, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["ST_SAVE_DIR"] = tempfile.mkdtemp()
import spacetrader as s
def play(seed, diff, maxdays=700):
    random.seed(seed); s.random.seed(seed)
    sess = s.GameSession(difficulty=diff); e = sess.engine; A = lambda d: s.handle_action(sess, d)
    for it in range(4000):
        p = e.player
        if e.is_game_over: return ("dead", p.day, e.calculate_net_worth())
        if p.day > maxdays: break
        if e.calculate_net_worth() >= s.TARGET_NET_WORTH: return ("won", p.day, e.calculate_net_worth())
        if sess.active_combat and not sess.active_combat.is_finished:
            A({"action":"combat_action","combat_action": random.choice(["fire","fire","fire","recharge","flee"]) }); continue
        if sess.active_combat: A({"action":"dismiss_combat"}); continue
        if sess.active_encounter: A({"action":"resolve_encounter","choice":False}); continue
        for g, q in list(p.cargo.items()):
            if p.sellable_cargo(g) > 0: A({"action":"sell_commodity","good":g,"qty":p.sellable_cargo(g)})
        if p.credits > 0 and p.hull < p.max_hull: A({"action":"repair_hull","hp":p.max_hull-p.hull})
        if p.credits > 5000 and not p.insurance_active: A({"action":"buy_insurance"})
        # upgrade ship when rich
        order = list(s.SHIP_TEMPLATES)
        i = order.index(p.ship_id)
        if i+1 < len(order):
            t = s.SHIP_TEMPLATES[order[i+1]]
            if p.credits > t.cost*1.5 + 3000: A({"action":"buy_ship","ship_id":order[i+1]})
        routes = e.compute_best_trade_routes(from_current_only=True)
        if p.fuel < 60: A({"action":"buy_fuel","amount":p.max_fuel-p.fuel})
        done = False
        for r in routes[:3]:
            good = r.get("good_id")
            dest = r.get("dst")
            if not good or not dest: print(r.keys()); return
            q = min(p.cargo_free(), e.planets[p.location].stock.get(good,0), int(p.credits // max(1,e.get_buy_price(good))))
            if q > 0:
                A({"action":"buy_commodity","good":good,"qty":q})
                if p.fuel < 60: A({"action":"buy_fuel","amount":p.max_fuel-p.fuel})
                rr = A({"action":"travel","destination":dest}); done = rr["success"]
                if done: break
        if not done:
            # wander
            if p.credits < 200: A({"action":"odd_jobs"})
            A({"action":"buy_fuel","amount":p.max_fuel-p.fuel})
            near = sorted([n for n in e.planets if n != p.location], key=lambda n: e.calculate_distance(e.current_planet, e.planets[n]))
            for n in near[:3]:
                if A({"action":"travel","destination":n})["success"]: break
    return ("timeout", e.player.day, e.calculate_net_worth())
if __name__ == "__main__":
    for diff in ["easy","normal","hard","nightmare"]:
        res = [play(sd, diff) for sd in range(4)]
        print(diff, res)
