# Space Trader: Odyssey — Nebula Edition

A single-file science-fiction trading, exploration, and tactical-combat RPG. The game engine, economy simulation, combat, persistence, HTTP API, and responsive browser interface all live in **`spacetrader.py`**. Runtime dependencies are limited to the Python standard library—no package installation, build step, external assets, or separate frontend is required.

## Quick start

Requires Python 3.8 or newer.

```bash
python3 spacetrader.py
```

Open **http://127.0.0.1:3000** in a modern browser. The server binds to localhost by default, so other devices cannot connect unless you explicitly change the bind address.

```bash
# Print the game version
python3 spacetrader.py --version

# Choose a port and captain name
python3 spacetrader.py --port 8080 --player "Nova"

# Start a harder career with repeatable random events
python3 spacetrader.py --difficulty hard --seed 42

# Expose the game to your local network (only on a trusted network)
python3 spacetrader.py --host 0.0.0.0 --port 3000

# Run the engine and HTTP regression checks
python3 spacetrader.py --test
python3 spacetrader.py --web-test
```

The `PORT` environment variable is also supported for hosting platforms. When `PORT` is set, the default bind host becomes `0.0.0.0`; set `--host 127.0.0.1` to override that behavior. Ports are validated to be between 1 and 65535.

## What you can do

- **Explore a 16-world sector:** travel between planets with distinct industrial, agricultural, technological, crime, and security profiles.
- **Trade a living market:** buy and sell 18 commodities, react to 20 planetary events, study price histories, and compare risk-adjusted routes.
- **Build a ship and crew:** progress through 10 hull classes, install weapons, shields, and specialist modules, and recruit officers with different perks.
- **Fight tactically:** target enemy subsystems, manage shields, use missiles and drones, attempt boarding actions, evade, or flee.
- **Take contracts:** haul cargo, smuggle restricted goods, deliver medical supplies, transport passengers, and hunt bounty targets.
- **Manage a career:** earn promotions and achievements, develop faction reputation, use banking and stocks, and compete for a persistent Hall of Fame ranking.
- **Recover from setbacks:** dockside odd jobs offer a way to earn fuel money if you become stranded.

Your main victory goal is to reach **500,000 CR net worth**. Trading, contracts, investing, missions, and combat provide different paths to success.

## Save files and settings

- Save slots, autosaves, and pre-combat recovery points are JSON files named `st_odyssey_save_*.json`.
- The Hall of Fame is stored in `st_odyssey_scores.json`.
- Save files are written alongside the script when possible. Set `ST_SAVE_DIR` to an **existing writable directory** to keep game data elsewhere.
- `--difficulty` accepts `easy`, `normal`, `hard`, or `nightmare`.
- `--seed N` initializes the random-number generator and helps reproduce test runs. It is not a promise that a saved career can be replayed identically after arbitrary actions.

The included `.gitignore` excludes local saves and Python bytecode so personal game progress is not accidentally committed.

## Controls and game rules

The browser interface provides the star map, market, trade advisor, shipyard, station services, crew lounge, mission board, bank/stocks, career log, and save/load dialogs. Browser audio is synthesized with the Web Audio API; if a browser does not support audio or blocks playback, gameplay remains available without sound.

- Stations buy below and sell above the mid-market price. Rank, crew, and faction goodwill can narrow the spread, but it never falls below 1% per side.
- Large trades can incur a market-depth fee, shown in the trade dialog.
- Contract cargo is sealed. Abandon the contract to return its cargo; expired contracts repossess it.
- A traded-in ship is valued at 70% of its listed price.
- Active encounters and battles must be resolved before regular travel, trading, or saving can continue.

## Architecture and safety

The project intentionally keeps the game engine in one Python file:

- `GameEngine` contains the game rules and persistent state.
- `CombatEncounter` owns turn-based tactical combat.
- `handle_action()` dispatches typed actions independently of HTTP, making the engine testable without opening a socket.
- `SpaceTraderWebHandler` serves the embedded HTML/CSS/JavaScript client and JSON API.

The built-in server is intended for local single-player use, **not as an internet-facing production service**. It has no authentication or TLS layer. It does not enable CORS, requires JSON for game-changing requests, rejects cross-site and mismatched-Origin browser writes, rejects ambiguous request framing, caps request-body size, applies browser security headers, limits client socket wait time, and writes save files atomically. Bind to a LAN address only on a trusted network.

## Verification

```bash
python3 -m py_compile spacetrader.py
python3 spacetrader.py --test
python3 spacetrader.py --web-test
```

The engine self-test suite checks data invariants, trade and progression rules, contracts, combat, persistence, and recovery mechanics. The HTTP smoke test exercises the real handler, frontend delivery, JSON endpoints, action requests, malformed inputs, request-size/framing guardrails, and response security headers.

GitHub Actions runs compilation, both built-in suites, and the version check on Python 3.10–3.13 for pushes, pull requests, and manual runs. No third-party runtime dependencies are required.
