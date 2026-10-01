# Space Trader: Odyssey (Nebula Edition)

A deep, atmospheric sci-fi space trading, exploration, and tactical turn-based combat RPG game coded entirely in Python in a single file (`spacetrader.py`).

## Features
- **100% Single-File Python Architecture**: The entire game engine, persistent save system, market simulation, event generator, combat system, and HTTP server with modern browser interface live in `spacetrader.py`.
- **Modern Browser HTML/CSS/JS Interface**: Migrated from Tkinter to a sleek, responsive sci-fi glassmorphism interface accessible directly in any web browser.
- **Interactive Starmap & Celestial Navigation**: Dynamic Canvas galaxy map featuring 16 unique planetary systems, animated orbits, jump range radius indicators, and faction borders.
- **Dynamic Living Sector Economy**: 18 trade commodities with dynamic price discovery, supply/demand shifts, planetary event disruptions (famines, tech booms, embargoes), and interactive historical trend sparklines.
- **Automated Trade Computer**: Route-finding algorithm calculating best profit-per-day routes across all connected star systems.
- **Fleet & Outfitting**: 10 distinct starship classes (from nimble Sparrow couriers to behemoth Dreadnoughts) with configurable weapon hardpoints, shield capacitors, cargo expanders, and specialized modules.
- **Crew Roster**: Hire navigators, engineers, diplomats, tacticians, and mercenaries each granting unique bonuses and daily wages.
- **Dynamic Turn-Based Tactical Combat**: Subsystem targeting, shield management, guided missiles, combat drones, boarding actions on crippled hulls, and automated distress signals.
- **Bounty & Contract Board**: High-paying delivery runs, covert smuggling operations, and pirate bounty hunting missions.
- **Banking, Loans & Stock Market**: Interstellar exchange stocks, savings (0.2%/day on the first 60,000 CR), loans, and credit score tracking.
- **Synthesized Audio**: Built-in Web Audio API synthesizer for laser fire, shield hits, warp transitions, alarms, and coin chimes without any external sound assets.
- **Saves & Autosaves**: Multi-slot persistent save game system with autosaves on jump and pre-combat checkpoints.

## Running the Game

### Start the Web Server
```bash
python3 spacetrader.py --port 3000
```
Then open `http://localhost:3000` in your web browser.

The server listens on **localhost only** by default. Use `--host 0.0.0.0` to expose it on your LAN
(it is automatically `0.0.0.0` when a hosting platform sets `$PORT`).

### Tests
```bash
python3 spacetrader.py --test        # 29-section engine self-test
python3 spacetrader.py --web-test    # HTTP API smoke test
python3 -m unittest discover -s tests -v   # regression, property, fuzz & HTTP-hardening tests
npm install --no-save jsdom && node tests/ui/smoke.js   # drives the real UI headlessly
```

## Game Rules Worth Knowing
- **Station spreads**: stations buy below and sell above the mid-market price. Trader officers, rank perks and
  faction goodwill *narrow* that spread (never below 1%), so buying and reselling at one station always loses money.
  Large orders also pay a small market-depth fee (shown live in the trade dialog).
- **Contract cargo is sealed**: goods loaded for a contract can't be sold or jettisoned; abandon the contract to
  hand them back. Expired contracts repossess their cargo.
- **Trade-ins**: your ship is worth 70% of its price; buying a cheaper hull pays you the difference.
- **Encounters & battles are modal**: you can't trade, travel or save until the contact is resolved.

## Architecture
Everything stays in one file by design: engine (`GameEngine`), combat (`CombatEncounter`), the pure
`handle_action()` dispatcher, a hardened stdlib HTTP server, and the embedded browser client. `handle_action`
has no HTTP dependency, which is what makes the game testable without sockets.

Security posture: no CORS, same-origin JSON-only writes, `Sec-Fetch-Site` checks, request-size limits, a
Content-Security-Policy, atomic save writes and rollback-safe loading.

