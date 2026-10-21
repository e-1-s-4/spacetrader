# Contributing to Space Trader: Odyssey

Thanks for taking a look. This project is deliberately small: **the entire game — engine,
economy, combat, persistence, HTTP API and browser client — lives in `spacetrader.py`.**
There are no build steps and no runtime dependencies.

## Getting set up

```bash
python3 spacetrader.py          # play at http://127.0.0.1:3000
python3 spacetrader.py --help   # all flags
```

Python 3.8 or newer. No packages to install for the game itself.

To run the browser UI smoke test you need Node 18+ (it uses `jsdom`):

```bash
npm ci
```

## Running the tests

```bash
npm test                 # everything below, in order
npm run test:engine      # python3 spacetrader.py --test    — engine invariants
npm run test:web         # python3 spacetrader.py --web-test — real HTTP handler
npm run test:regression  # python3 -m unittest discover -s tests
npm run test:ui          # node tests/ui/smoke.js           — real browser client
```

CI runs all four suites on Python 3.8 through 3.13 for every push and pull request.

## House rules

- **Keep the single-file architecture.** Splitting `spacetrader.py` is not a refactor we
  want; the "clone it and run it" experience is the product.
- **No runtime dependencies.** `jsdom` is a dev-only dependency for the UI test.
- **Every bug fix gets a regression test.** `tests/test_game.py` is organised so that each
  test class documents a real defect that was found and fixed — please keep that pattern
  and name the defect in the docstring.
- **Never trust client input.** The API is untrusted input: use `to_int()` and the `_arg()`
  helper rather than casting raw values.
- **Keep the local-server threat model.** No CORS, no auth, no TLS. If you add an endpoint,
  it must reject cross-site writes, require `application/json`, and cap the body size.

## Style

- Python: 4-space indent, ~88-column lines, type hints on public functions.
- The browser client is plain JavaScript with no framework; keep it dependency-free.
- Comment *why*, not *what* — the codebase leans on its comments explaining the reasoning
  behind a rule (see the station-spread and market-depth comments for the tone).
