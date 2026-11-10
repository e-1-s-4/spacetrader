# Space Trader: Odyssey — Nebula Edition

A single-file science-fiction trading, exploration and tactical-combat RPG. The
engine, economy, combat, persistence, HTTP API and browser client all live in
**`spacetrader.py`**. Runtime dependencies: the Python standard library only.

**Version 2.5.0** · MIT · Python 3.8+

```bash
python3 spacetrader.py                      # http://127.0.0.1:3000
python3 spacetrader.py --port 8080 --player "Nova" --difficulty hard --seed 42
python3 spacetrader.py --version
```

Goal: reach **500,000 CR** net worth by trading, contracts, investing and combat.
`--difficulty` is `easy`, `normal`, `hard` or `nightmare`. Saves live next to the
script (or in `$ST_SAVE_DIR`); the Hall of Fame is `st_odyssey_scores.json`.

## Running inside the parent Next.js app

The Next.js app starts `spacetrader.py` on demand (loopback port `38917`,
override with `SPACETRADER_PORT`), proxies `/api/{state,action,routes,saves,scores}`
to it, and serves the game at **`/game`**. Saves go to `.spacetrader-saves/`.
The game is single-player: all visitors share one career.

## Verification

```bash
python3 -m py_compile spacetrader.py
python3 spacetrader.py --test               # engine self-test
python3 spacetrader.py --web-test           # HTTP smoke test
python3 -m unittest discover -s tests -v    # 73 regression tests

# Browser-client tests (need: npm i jsdom)
node tests/ui/smoke.js                      # real client against a real server
node tests/ui/fuzz.js                       # random UI play across every tab

# Bots / fuzzers (no dependencies)
python3 tools/fuzz_engine.py 8 2000         # random play + invariant checks
python3 tools/fuzz_saves.py                 # mutate saves; loader must stay safe
python3 tools/greedy_bot.py                 # advisor-following trader, all difficulties
```

See `CHANGELOG.md` for the 2.5.0 audit (stale news feed, save-dialog XSS,
corrupt-save validation) and `CONTRIBUTING.md` for the house rules.
