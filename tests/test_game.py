"""Regression, property and HTTP tests for Space Trader: Odyssey.

Run with:   python3 -m unittest discover -s tests -v
Every test class documents a real defect that was found and fixed.
"""
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import spacetrader as st  # noqa: E402


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


class GameTestCase(unittest.TestCase):
    """Isolated save directory + seeded RNG for every test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="st_test_")
        self._old_env = os.environ.get("ST_SAVE_DIR")
        os.environ["ST_SAVE_DIR"] = self.tmp
        random.seed(1234)
        self.e = self.new_engine()

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("ST_SAVE_DIR", None)
        else:
            os.environ["ST_SAVE_DIR"] = self._old_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def new_engine(diff="normal"):
        e = st.GameEngine(muted=True, difficulty_id=diff)
        e.new_game("Tester", diff)
        return e


class TestInputValidation(GameTestCase):
    """Negative amounts used to PAY the player and push fuel/hull below zero."""

    def test_negative_fuel_rejected(self):
        self.e.player.fuel = 50
        c0 = self.e.player.credits
        ok, _ = self.e.buy_fuel(-100)
        self.assertFalse(ok)
        self.assertEqual((self.e.player.credits, self.e.player.fuel), (c0, 50))

    def test_negative_repair_rejected(self):
        self.e.player.hull = 50
        c0 = self.e.player.credits
        self.assertFalse(self.e.repair_hull(-40)[0])
        self.assertEqual((self.e.player.credits, self.e.player.hull), (c0, 50))

    def test_non_positive_amounts_rejected_everywhere(self):
        e = self.e
        e.player.credits = 10_000
        e.player.savings = 5_000
        e.player.loan = 5_000
        e.player.equipped_weapons.append("missile_rack")
        for call in (e.deposit, e.withdraw, e.borrow, e.repay, e.buy_missiles):
            for bad in (0, -5):
                self.assertFalse(call(bad)[0], f"{call.__name__}({bad})")
        self.assertFalse(e.sell_stock("NXC" if "NXC" in e.stocks else next(iter(e.stocks)), -3)[0])

    def test_to_int_rejects_hostile_values(self):
        for bad in (float("inf"), float("-inf"), float("nan"), "inf", "nan", "abc",
                    True, None, [1], {"a": 1}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                st.to_int(bad)
        self.assertEqual(st.to_int("12"), 12)
        self.assertEqual(st.to_int(7.9), 7)
        self.assertEqual(st.to_int(10 ** 30), st.MAX_INT_INPUT)


class TestEconomy(GameTestCase):
    def test_no_same_station_arbitrage_in_any_reachable_state(self):
        """Trader crew + rank perks used to make buy->sell a risk-free loop."""
        worst = -10 ** 9
        for diff in ("easy", "normal"):
            for rank in range(6):
                for crew in (None, "chen"):
                    e = self.new_engine(diff)
                    e.rank_index = lambda r=rank: r
                    e.player.hired_crew = [crew] if crew else []
                    e.player.credits, e.player.cargo_cap = 10 ** 12, 10 ** 7
                    p = e.current_planet
                    for good, comm in st.COMMODITIES.items():
                        lo = max(2, -(-comm.base_price // 4))
                        hi = int(comm.base_price * 3.2)
                        for mid in sorted({lo, lo + 1, comm.base_price, hi, (lo + hi) // 2}):
                            for qty in (1, 3, 10, 25, 100, 400):
                                p.market[good], p.stock[good] = mid, 10 ** 7
                                c0 = e.player.credits
                                if not e.buy_commodity(good, qty)[0]:
                                    continue
                                e.sell_commodity(good, qty)
                                worst = max(worst, e.player.credits - c0)
        self.assertLess(worst, 0, "a same-station round trip turned a profit")

    def test_sell_price_always_below_buy_price(self):
        for rank in range(6):
            self.e.rank_index = lambda r=rank: r
            self.e.player.hired_crew = ["chen"]
            for g in st.COMMODITIES:
                self.assertLess(self.e.get_sell_price(g), self.e.get_buy_price(g))

    def test_tiny_trades_do_not_pay_phantom_fees(self):
        self.assertEqual(st.GameEngine.market_depth_fee(12, 1), 0)

    def test_savings_interest_is_capped(self):
        e = self.e
        e.player.credits = 400_000
        e.deposit(400_000)
        before = e.player.savings
        e.advance_day()
        self.assertEqual(e.player.savings - before,
                         int(st.SAVINGS_INTEREST_CAP * st.SAVINGS_DAILY_RATE))

    def test_borrowing_to_bank_never_profits(self):
        for diff, d in st.DIFFICULTIES.items():
            self.assertLess(st.SAVINGS_DAILY_RATE, d.loan_interest * 0.85, diff)

    def test_stock_rounding_favours_house(self):
        sym = next(iter(self.e.stocks))
        self.e.stocks[sym].price = 5.99
        self.e.player.credits = 1000
        c0 = self.e.player.credits
        self.e.buy_stock(sym, 3)           # 17.97 -> 18, not 17
        self.assertEqual(c0 - self.e.player.credits, 18)


class TestContracts(GameTestCase):
    def _accept_cargo_contract(self):
        e = self.e
        e.available_missions = st.generate_mission_board(
            e.current_planet, list(e.planets.values()), e.player.day, career_tier=1)
        m = next(m for m in e.available_missions if m.cargo_good and m.cargo_qty > 0)
        self.assertTrue(e.accept_mission(m.id)[0])
        return m

    def test_contract_cargo_cannot_be_sold_or_jettisoned(self):
        m = self._accept_cargo_contract()
        c0 = self.e.player.credits
        self.assertFalse(self.e.sell_commodity(m.cargo_good, 1)[0])
        self.assertFalse(self.e.jettison_cargo(m.cargo_good, 1)[0])
        self.assertEqual(self.e.player.credits, c0)

    def test_own_stock_of_same_good_stays_sellable(self):
        m = self._accept_cargo_contract()
        self.e.player.cargo_cap += 10
        self.e.current_planet.stock[m.cargo_good] = 50
        self.e.player.credits = 50_000
        self.assertTrue(self.e.buy_commodity(m.cargo_good, 2)[0])
        self.assertTrue(self.e.sell_commodity(m.cargo_good, 2)[0])
        self.assertFalse(self.e.sell_commodity(m.cargo_good, 1)[0])

    def test_expired_contract_cargo_is_repossessed(self):
        m = self._accept_cargo_contract()
        m.days_left = 1
        self.e.advance_day()
        self.assertEqual(self.e.player.cargo.get(m.cargo_good, 0), 0)
        self.assertNotIn(m, self.e.player.active_missions)

    def test_abandon_clears_cargo_and_cost_basis(self):
        m = self._accept_cargo_contract()
        self.e.abandon_mission(m.id)
        self.assertNotIn(m.cargo_good, self.e.player.cargo)
        self.assertNotIn(m.cargo_good, self.e.player.cargo_cost_basis)


class TestShipsAndEquipment(GameTestCase):
    def test_downgrade_refunds_the_trade_in_difference(self):
        e = self.e
        e.player.credits = 10 ** 6
        e.buy_ship("sovereign")
        c1 = e.player.credits
        self.assertTrue(e.buy_ship("sparrow")[0])
        self.assertEqual(e.player.credits - c1,
                         int(st.SHIP_TEMPLATES["sovereign"].cost * 0.7) - st.SHIP_TEMPLATES["sparrow"].cost)

    def test_ship_swap_clears_damaged_subsystems(self):
        self.e.player.credits = 10 ** 6
        self.e.player.engines_damaged = True
        self.e.buy_ship("kestrel")
        self.assertFalse(self.e.player.engines_damaged)

    def test_cannot_sell_cargo_expander_with_full_hold(self):
        e = self.e
        e.player.credits = 10 ** 6
        e.buy_ship("kestrel")
        mod = next(k for k, v in st.EQUIPMENT_ITEMS.items() if v.slot_type == "module" and v.cargo_bonus > 0)
        self.assertTrue(e.buy_equipment(mod)[0])
        e.player.cargo = {"water": e.player.cargo_cap}
        self.assertFalse(e.sell_equipment(mod)[0])
        self.assertLessEqual(e.player.cargo_used(), e.player.cargo_cap)

    def test_selling_missile_rack_refunds_missiles(self):
        e = self.e
        e.player.credits = 10 ** 6
        e.buy_ship("valkyrie")
        e.buy_equipment("missile_rack")
        e.buy_missiles(4)
        c0 = e.player.credits
        e.sell_equipment("missile_rack")
        self.assertEqual(e.player.missiles, 0)
        self.assertGreater(e.player.credits - c0, int(st.EQUIPMENT_ITEMS["missile_rack"].cost * 0.75))


class TestEncounterText(GameTestCase):
    def test_forced_asteroid_detour_really_hurts(self):
        self.e.player.fuel = 0
        self.e.player.hull = 50
        self.e.resolve_asteroid_field({}, thread_needle=False)
        self.assertEqual(self.e.player.hull, 46)

    def test_wormhole_counts_as_visit(self):
        random.seed(5)
        self.e.resolve_wormhole({}, enter=True)
        self.assertIn(self.e.player.location, self.e.player.visited)


class TestCombat(GameTestCase):
    def test_drone_kill_does_not_waste_missile_or_volley(self):
        e = self.e
        e.player.equipped_weapons = ["laser_1", "missile_rack"]
        e.player.missiles = 3
        c = st.CombatEncounter(e, "Dummy", "sparrow")
        c.drones_active = True
        c.enemy_hull, c.enemy_shield = 1, 0
        random.seed(1)
        c.player_action("missile")
        self.assertTrue(c.player_won)
        self.assertEqual(e.player.missiles, 3)

    def test_insured_wreck_is_not_a_hall_of_fame_entry(self):
        e = self.e
        e.player.insurance_active = True
        e.player.hull = 1
        c = st.CombatEncounter(e, "Reaper", "behemoth")
        c.enemy_damage_range = (500, 600)
        for _ in range(30):
            if c.is_finished:
                break
            c.player_action("recharge")
        self.assertTrue(c.insurance_used)
        self.assertFalse(e.is_game_over)
        self.assertEqual(e.load_hall_of_fame(), [])
        self.assertFalse(e._hof_recorded)

    def test_true_death_is_recorded(self):
        e = self.e
        e.player.hull = 1
        c = st.CombatEncounter(e, "Reaper", "behemoth")
        c.enemy_damage_range = (500, 600)
        for _ in range(30):
            if c.is_finished:
                break
            c.player_action("recharge")
        self.assertTrue(e.is_game_over)
        self.assertEqual(len(e.load_hall_of_fame()), 1)


class TestPersistence(GameTestCase):
    def test_roundtrip(self):
        e = self.e
        e.player.credits = 4321
        self.assertTrue(e.save_game("1")[0])
        e.player.credits = 1
        self.assertTrue(e.load_game("1")[0])
        self.assertEqual(e.player.credits, 4321)

    def test_corrupt_load_rolls_back_cleanly(self):
        e = self.e
        e.save_game("1")
        path = st.slot_path("1")
        data = read_json(path)
        data["player"]["credits"] = 99999
        data["player"]["active_missions"] = [{"id": "x"}]       # malformed, skipped
        data["player"]["fuel"] = "not-a-number"                  # fatal
        write_json(path, data)
        e.player.credits = 777
        ok, _ = e.load_game("1")
        self.assertFalse(ok)
        self.assertEqual(e.player.credits, 777, "failed load must not half-apply")

    def test_tampered_save_is_sanitised(self):
        e = self.e
        e.save_game("1")
        path = st.slot_path("1")
        data = read_json(path)
        p = data["player"]
        p.update(credits=-500, loan=-9, missiles=99, credit_score=5000,
                 cargo={"water": 9999, "bogus": 5}, hull=10 ** 6,
                 equipped_weapons=["laser_1"] * 9, hired_crew=["vance", "vance", "drake", "aria", "zoe", "chen"])
        write_json(path, data)
        self.assertTrue(e.load_game("1")[0])
        pl = e.player
        self.assertGreaterEqual(pl.credits, 0)
        self.assertGreaterEqual(pl.loan, 0)
        self.assertLessEqual(pl.missiles, st.PLAYER_MISSILE_CAP)
        self.assertLessEqual(pl.credit_score, st.MAX_CREDIT_SCORE)
        self.assertLessEqual(pl.cargo_used(), pl.cargo_cap)
        self.assertLessEqual(pl.hull, pl.max_hull)
        self.assertEqual(len(pl.equipped_weapons), 1)
        self.assertEqual(len(set(pl.hired_crew)), len(pl.hired_crew))
        self.assertLessEqual(len(pl.hired_crew), st.MAX_CREW)

    def test_saves_are_atomic_and_leave_no_temp_files(self):
        self.e.save_game("1")
        self.assertEqual([f for f in os.listdir(self.tmp) if f.startswith(".st_tmp_")], [])

    def test_dead_captain_cannot_save(self):
        self.e.is_game_over = True
        self.assertFalse(self.e.save_game("1")[0])

    def test_invalid_slot_rejected(self):
        self.assertFalse(self.e.save_game("../../etc/passwd")[0])
        self.assertFalse(self.e.load_game("../../etc/passwd")[0])


class TestDispatcher(GameTestCase):
    def setUp(self):
        super().setUp()
        self.s = st.GameSession()

    def act(self, **data):
        with self.s.lock:
            return st.handle_action(self.s, data)

    def test_revision_increments(self):
        r0 = self.s.revision
        self.act(action="odd_jobs")
        self.assertEqual(self.s.revision, r0 + 1)

    def test_unknown_action_and_bad_numbers(self):
        self.assertFalse(self.act(action="nope")["success"])
        with self.assertRaises(st.ActionError):
            self.act(action="buy_fuel", amount=float("inf"))
        with self.assertRaises(st.ActionError):
            self.act(action="buy_commodity", good="water", qty="many")

    def test_encounter_blocks_everything_but_resolution(self):
        self.s.active_encounter = {"type": "solar_flare", "title": "x"}
        r = self.act(action="buy_fuel", amount=5)
        self.assertFalse(r["success"])
        self.assertEqual(r["message"], st.ENCOUNTER_PENDING_MSG)
        self.assertFalse(self.act(action="travel", destination="Mars")["success"])
        self.assertTrue(self.act(action="resolve_encounter", choice=True)["success"])
        self.assertTrue(self.act(action="odd_jobs")["success"])

    def test_cannot_walk_away_from_a_battle(self):
        self.s.active_combat = st.CombatEncounter(self.s.engine, "Raider", "viper")
        day = self.s.engine.player.day
        for act in ({"action": "travel", "destination": "Mars"}, {"action": "odd_jobs"},
                    {"action": "save_game", "slot": "1"}):
            self.assertEqual(self.act(**act)["message"], st.IN_COMBAT_MSG)
        self.assertEqual(self.s.engine.player.day, day)

    def test_finished_combat_auto_closes_on_next_action(self):
        c = st.CombatEncounter(self.s.engine, "Raider", "viper")
        c.is_finished = c.player_won = True
        self.s.active_combat = c
        self.assertTrue(self.act(action="odd_jobs")["success"])
        self.assertIsNone(self.s.active_combat)

    def test_game_over_blocks_actions_but_new_game_revives(self):
        self.s.engine.is_game_over = True
        self.assertFalse(self.act(action="odd_jobs")["success"])
        self.assertTrue(self.act(action="new_game", name="<script>x</script>", difficulty="hard")["success"])
        self.assertFalse(self.s.engine.is_game_over)
        self.assertNotIn("<", self.s.engine.player.name)

    def test_unknown_combat_maneuver_rejected(self):
        self.s.active_combat = st.CombatEncounter(self.s.engine, "Raider", "viper")
        self.assertFalse(self.act(action="combat_action", combat_action="self_destruct")["success"])
        self.assertEqual(self.s.active_combat.turn_count, 0)

    def test_state_is_json_serialisable_and_exposes_sealed_cargo(self):
        state = self.act(action="odd_jobs")["state"]
        json.dumps(state)
        self.assertIn("economy", state)
        self.assertIn("sellable_qty", state["current_planet"]["market"][0])


class TestHttp(GameTestCase):
    def setUp(self):
        super().setUp()
        st.GLOBAL_SESSION = st.GameSession()
        self.server = st.ThreadedHTTPServer(("127.0.0.1", 0), st.SpaceTraderWebHandler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        st.GLOBAL_SESSION = None
        super().tearDown()

    def post(self, body, headers=None, raw=False):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        data = body if raw else json.dumps(body).encode()
        req = urllib.request.Request(self.base + "/api/action", data=data, headers=h)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_infinity_and_nan_are_400_not_a_crash(self):
        for literal in (b'{"action":"buy_fuel","amount":1e999}',
                        b'{"action":"buy_fuel","amount":NaN}',
                        b'{"action":"buy_fuel","amount":-Infinity}'):
            code, _ = self.post(literal, raw=True)
            self.assertEqual(code, 400, literal)
        self.assertEqual(self.post({"action": "odd_jobs"})[0], 200)   # still alive

    def test_negative_fuel_over_http_does_not_pay_out(self):
        s = st.get_session()
        s.engine.player.fuel = 10
        c0 = s.engine.player.credits
        _, body = self.post({"action": "buy_fuel", "amount": -1000})
        self.assertFalse(body["success"])
        self.assertEqual(s.engine.player.credits, c0)

    def test_csrf_defences(self):
        self.assertEqual(self.post({"action": "odd_jobs"}, {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.post({"action": "odd_jobs"}, {"Content-Type": "text/plain"})[0], 415)

    def test_no_cors_headers(self):
        with urllib.request.urlopen(self.base + "/api/state") as r:
            self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))
            self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn("Content-Security-Policy", r.headers)

    def test_malformed_bodies(self):
        self.assertEqual(self.post(b"{not json", raw=True)[0], 400)
        self.assertEqual(self.post(b"[1,2,3]", raw=True)[0], 400)
        self.assertEqual(self.post(b"x" * (st.MAX_BODY_BYTES + 1), raw=True)[0], 413)

    def test_unknown_routes_are_json_404(self):
        for path in ("/nope", "/api/nope"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(self.base + path)
            self.assertEqual(cm.exception.code, 404)

    def test_concurrent_requests_stay_consistent(self):
        errors = []

        def hammer():
            try:
                for _ in range(15):
                    code, _ = self.post({"action": "bank_deposit", "amount": 1})
                    if code != 200:
                        errors.append(code)
            except Exception as exc:     # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        s = st.get_session()
        self.assertEqual(s.engine.player.savings, 90)
        self.assertEqual(s.revision, 90)

    def test_session_creation_is_single_instance(self):
        st.GLOBAL_SESSION = None
        seen = []
        ts = [threading.Thread(target=lambda: seen.append(st.get_session())) for _ in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len({id(x) for x in seen}), 1)


class TestFuzzInvariants(GameTestCase):
    """Random hostile play must never break the game's invariants."""

    def check(self, e, ctx):
        p = e.player
        t = st.SHIP_TEMPLATES[p.ship_id]
        self.assertGreaterEqual(p.credits, 0, ctx)
        self.assertGreaterEqual(p.savings, 0, ctx)
        self.assertGreaterEqual(p.loan, 0, ctx)
        self.assertTrue(0 <= p.fuel <= p.max_fuel, f"{ctx} fuel {p.fuel}")
        self.assertTrue(0 <= p.hull <= p.max_hull, f"{ctx} hull {p.hull}")
        self.assertTrue(0 <= p.shield <= p.max_shield, f"{ctx} shield")
        self.assertLessEqual(p.cargo_used(), p.cargo_cap, ctx)
        self.assertTrue(all(q > 0 for q in p.cargo.values()), ctx)
        self.assertTrue(0 <= p.missiles <= st.PLAYER_MISSILE_CAP, ctx)
        self.assertLessEqual(len(p.equipped_weapons), t.weapon_slots, ctx)
        self.assertLessEqual(len(p.equipped_shields), t.shield_slots, ctx)
        self.assertLessEqual(len(p.equipped_modules), t.module_slots, ctx)
        self.assertLessEqual(len(p.hired_crew), st.MAX_CREW, ctx)
        for plan in e.planets.values():
            self.assertTrue(all(v >= 0 for v in plan.stock.values()), ctx)

    def test_random_play(self):
        weird = [-10 ** 9, -100, -1, 0, 1, 2, 7, 25, 10 ** 6, 10 ** 12]
        for seed in range(40):
            r = random.Random(seed)
            random.seed(seed)
            diff = r.choice(list(st.DIFFICULTIES))
            s = st.GameSession(difficulty=diff)
            s.engine.player.credits += r.choice([0, 5000, 400_000])
            e = s.engine
            for i in range(250):
                if e.is_game_over:
                    break
                ctx = f"seed {seed} step {i}"
                a = r.choice([
                    {"action": "buy_commodity", "good": r.choice(list(st.COMMODITIES)), "qty": r.choice(weird)},
                    {"action": "sell_commodity", "good": r.choice(list(st.COMMODITIES)), "qty": r.choice(weird)},
                    {"action": "buy_fuel", "amount": r.choice(weird)},
                    {"action": "repair_hull", "hp": r.choice(weird)},
                    {"action": "buy_ship", "ship_id": r.choice(list(st.SHIP_TEMPLATES))},
                    {"action": "buy_equipment", "eq_id": r.choice(list(st.EQUIPMENT_ITEMS))},
                    {"action": "sell_equipment", "eq_id": r.choice(list(st.EQUIPMENT_ITEMS))},
                    {"action": "hire_crew", "crew_id": r.choice(list(st.CREW_INDEX))},
                    {"action": "bank_borrow", "amount": r.choice(weird)},
                    {"action": "bank_deposit", "amount": r.choice(weird)},
                    {"action": "buy_missiles", "qty": r.choice(weird)},
                    {"action": "jettison_cargo", "good": r.choice(list(st.COMMODITIES)), "qty": r.choice(weird)},
                    {"action": "odd_jobs"}, {"action": "travel", "destination": r.choice(list(e.planets))},
                    {"action": "travel", "destination": r.choice(list(e.planets))},
                    {"action": "resolve_encounter", "choice": r.random() < .5},
                    {"action": "combat_action", "combat_action": r.choice(list(st.CombatEncounter.ACTIONS))},
                    {"action": "dismiss_combat"},
                ])
                if a["action"] == "accept_mission" or r.random() < 0.08:
                    if e.available_missions:
                        a = {"action": "accept_mission", "mission_id": r.choice(e.available_missions).id}
                with s.lock:
                    resp = st.handle_action(s, a)
                json.dumps(resp)
                self.check(e, ctx)


if __name__ == "__main__":
    unittest.main()
