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
- **Banking, Loans & Stock Market**: Interstellar exchange stocks, savings accounts earning daily interest, loans, and credit score tracking.
- **Synthesized Audio**: Built-in Web Audio API synthesizer for laser fire, shield hits, warp transitions, alarms, and coin chimes without any external sound assets.
- **Saves & Autosaves**: Multi-slot persistent save game system with autosaves on jump and pre-combat checkpoints.

## Running the Game

### Start the Web Server
```bash
python3 spacetrader.py --port 3000
```
Then open `http://localhost:3000` in your web browser.

### Headless Verification Tests
```bash
# Run the 16-suite engine self-test
python3 spacetrader.py --test

# Run the web server and API smoke test
python3 spacetrader.py --web-test
```

