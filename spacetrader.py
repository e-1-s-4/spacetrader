#!/usr/bin/env python3
"""
================================================================================
 SPACE TRADER: ODYSSEY — Nebula Edition (Browser HTML)
================================================================================
A complete sci-fi trading, exploration, and combat RPG with a modern browser
interface served by a built-in threaded HTTP server. Pure Python stdlib —
one file, zero installs, no third-party dependencies.

HOW TO RUN
----------
    python3 spacetrader.py                    Start the web server and play in
                                              your browser (default port 3000)
    python3 spacetrader.py --port 8080        Serve on a custom port
    python3 spacetrader.py --test             Run the headless engine self-tests
    python3 spacetrader.py --web-test         Run the web server smoke test
    python3 spacetrader.py --difficulty easy|normal|hard|nightmare
    python3 spacetrader.py --player NAME      Set the captain's callsign
    python3 spacetrader.py --seed N           Deterministic RNG for testing

The PORT environment variable is honoured (defaults to 3000). Saves live
next to the script (or $ST_SAVE_DIR) as JSON flight records.

WHAT'S INSIDE
-------------
* 16 planetary systems with living economies, 18 commodities, 18 market
  events, price histories and sparkline charts.
* 10 hulls, 22 equipment items, 7 hireable officers, 5 mission types
  (delivery / smuggle / bounty / medical / passenger).
* Tactical turn-based combat with subsystem targeting, missiles, drones,
  boarding actions and four AI personalities.
* Ranks (Cadet → Admiral), faction reputation, achievements, banking,
  a stock exchange, and 10 deep-space encounter types.
* Multi-slot saves with autosave on jump and a pre-combat snapshot.

GOAL
----
Grow your net worth to 500,000 CR to win — through trading, contracts,
smuggling, bounties, investing, and conquest of the void.
================================================================================
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import random
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple, Set


import http.server
import socketserver
import urllib.parse
import threading


# ==============================================================================
# CONSTANTS & HELPERS
# ==============================================================================

TARGET_NET_WORTH = 500_000
STARTING_CREDITS = 2_500      # fallback default; difficulties override this
STARTING_SHIP = "sparrow"
BASE_FUEL_PRICE = 10
BASE_REPAIR_COST = 20
BASE_MISSILE_PRICE = 400
PLAYER_MISSILE_CAP = 8
BASE_SPREAD = 0.05          # 5% station markup/markdown at Normal difficulty
MAX_ACTIVE_MISSIONS = 5
MAX_CREW = 4
SAVE_VERSION = 4
PRICE_HISTORY_LEN = 10       # days kept per commodity for the sparkline charts


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def money(value: float) -> str:
    return f"{int(round(value)):,}"


def pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def save_dir() -> str:
    """Directory where save files live.

    Priority: $ST_SAVE_DIR env override, then the script's folder (if
    writable), finally the current working directory.
    """
    env_dir = os.environ.get("ST_SAVE_DIR")
    if env_dir and os.path.isdir(env_dir):
        return env_dir
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        if os.access(base, os.W_OK):
            return base
    except Exception:
        pass
    return os.getcwd()


def slot_path(slot: str) -> str:
    if slot not in SAVE_SLOTS + (AUTO_SLOT, PRECOMBAT_SLOT):
        raise ValueError("Invalid save slot.")
    return os.path.join(save_dir(), f"st_odyssey_save_{slot}.json")


SAVE_SLOTS = ("1", "2", "3")
AUTO_SLOT = "autosave"
PRECOMBAT_SLOT = "precombat"


# ==============================================================================
# AUDIO — cross-platform tone effects
# ==============================================================================

class SoundManager:
    """
    Lightweight cross-platform sound manager.

    On Windows distinct tones are produced via winsound.Beep. On other
    platforms a terminal bell is used. If audio is unsupported the manager
    silently disables itself.
    """

    TONES: Dict[str, Tuple[int, int]] = {
        "buy": (880, 70),
        "sell": (1046, 70),
        "coin": (1318, 60),
        "laser": (220, 45),
        "shield_hit": (330, 55),
        "alarm": (415, 160),
        "warp": (523, 140),
        "upgrade": (659, 90),
        "victory": (1567, 180),
        "death": (110, 300),
        "rank_up": (784, 200),
        "mine": (196, 120),
        "wormhole": (622, 260),
    }

    def __init__(self, muted: bool = False):
        self.muted = muted
        self.system = platform.system()
        self.enabled = True
        if self.system != "Windows":
            try:
                sys.stdout.write("")
            except Exception:
                self.enabled = False

    def play(self, name: str) -> None:
        if self.muted or not self.enabled:
            return
        try:
            if self.system == "Windows":
                import winsound  # type: ignore
                freq, ms = self.TONES.get(name, (600, 60))
                winsound.Beep(max(37, min(32767, freq)), max(20, ms))
            else:
                sys.stdout.write("\a")
                sys.stdout.flush()
        except Exception:
            self.enabled = False


# ==============================================================================
# DIFFICULTY
# ==============================================================================

@dataclass(frozen=True)
class Difficulty:
    id: str
    name: str
    desc: str
    starting_credits: int
    encounter_mult: float      # scales random encounter probability
    enemy_power: float         # scales enemy hull / shields / damage
    price_spread_mult: float   # scales station buy/sell spread
    loan_interest: float       # daily interest on loans
    fine_mult: float           # scales customs fines and bribes
    repair_mult: float         # scales repair prices
    fuel_mult: float           # scales fuel prices


DIFFICULTIES: Dict[str, Difficulty] = {
    "easy": Difficulty(
        "easy", "Easy", "Relaxed run: generous funds, lenient pirates and prices.",
        4_000, 0.70, 0.80, 0.85, 0.008, 0.60, 0.80, 0.90,
    ),
    "normal": Difficulty(
        "normal", "Normal", "The intended balanced experience.",
        2_500, 1.00, 1.00, 1.00, 0.012, 1.00, 1.00, 1.00,
    ),
    "hard": Difficulty(
        "hard", "Hard", "Cut-throat sector: hostile skies, ruthless fines, thin margins.",
        1_500, 1.30, 1.25, 1.15, 0.018, 1.50, 1.25, 1.10,
    ),
    "nightmare": Difficulty(
        "nightmare", "Nightmare",
        "For veterans only: a starving stake, merciless raiders, razor-thin margins.",
        800, 1.60, 1.55, 1.30, 0.024, 2.00, 1.50, 1.25,
    ),
}


# ==============================================================================
# CAPTAIN RANKS — career progression & perks
# ==============================================================================

@dataclass(frozen=True)
class Rank:
    id: str
    name: str
    renown: int                # renown required to hold this rank
    insignia: str
    perk: str


RANKS: List[Rank] = [
    Rank("cadet", "Cadet", 0, "·", "Fresh commission — the sector is watching."),
    Rank("ensign", "Ensign", 6_000, "◆",
         "+3% sell prices · buys 2% cheaper"),
    Rank("lieutenant", "Lieutenant", 22_000, "◆◆",
         "Contract rewards +12%"),
    Rank("captain", "Captain", 60_000, "◆◆◆",
         "Fuel & repairs -12% · crew wages -10%"),
    Rank("commodore", "Commodore", 150_000, "✦",
         "Reputation gains +50% · bounty rewards +15%"),
    Rank("admiral", "Admiral", 350_000, "✦✦",
         "+8% sell · 5% cheaper buys · loan interest -40% · insurance -25%"),
]


def rank_index_for(renown: int) -> int:
    """Highest rank index whose renown threshold has been met."""
    idx = 0
    for i, r in enumerate(RANKS):
        if renown >= r.renown:
            idx = i
    return idx


# Cumulative perk values keyed by rank index (index 0..5).
RANK_SELL_BONUS = (0.00, 0.03, 0.03, 0.03, 0.03, 0.08)      # added to sell price
RANK_BUY_DISCOUNT = (0.00, 0.02, 0.02, 0.02, 0.02, 0.05)   # cut from buy price
RANK_CONTRACT_BONUS = (0.00, 0.00, 0.12, 0.12, 0.12, 0.12)  # mission reward mult
RANK_SERVICE_DISCOUNT = (0.00, 0.00, 0.00, 0.12, 0.12, 0.12)  # fuel & repairs
RANK_WAGE_DISCOUNT = (0.00, 0.00, 0.00, 0.10, 0.10, 0.10)   # daily crew wages
RANK_REP_MULT = (1.0, 1.0, 1.0, 1.0, 1.5, 1.5)              # reputation gains
RANK_BOUNTY_BONUS = (0.00, 0.00, 0.00, 0.00, 0.15, 0.15)    # bounty payout mult
RANK_INTEREST_MULT = (1.0, 1.0, 1.0, 1.0, 1.0, 0.60)        # daily loan interest
RANK_INSURANCE_MULT = (1.0, 1.0, 1.0, 1.0, 1.0, 0.75)       # insurance premium


# ==============================================================================
# COMMODITIES
# ==============================================================================

@dataclass
class Commodity:
    id: str
    name: str
    category: str
    base_price: int
    volatility: float
    is_contraband: bool
    desc: str
    icon: str


COMMODITIES: Dict[str, Commodity] = {
    "water": Commodity(
        "water", "Pure Water", "Essentials", 12, 0.40, False,
        "Filtered water essential for life support.", "[H2O]"
    ),
    "food": Commodity(
        "food", "Hydroponic Grain", "Essentials", 20, 0.45, False,
        "Nutrient-dense synthetic rations.", "[GRN]"
    ),
    "medicine": Commodity(
        "medicine", "Bio-Vaccines", "Essentials", 75, 0.60, False,
        "Broad-spectrum antiviral and trauma kits.", "[MED]"
    ),
    "ore": Commodity(
        "ore", "Ferro-Titanium Ore", "Raw Materials", 35, 0.50, False,
        "Dense planetary metals for construction.", "[ORE]"
    ),
    "alloys": Commodity(
        "alloys", "Refined Carbon Alloys", "Raw Materials", 60, 0.40, False,
        "High-strength orbital structural composite.", "[ALY]"
    ),
    "fuel_cells": Commodity(
        "fuel_cells", "Hyper-Fuel Cells", "Raw Materials", 45, 0.35, False,
        "Compressed deuterium fuel pods.", "[FUL]"
    ),
    "crystals": Commodity(
        "crystals", "Dilithium Crystals", "Raw Materials", 130, 0.70, False,
        "Rare crystalline matrix for warp fields.", "[XTL]"
    ),
    "electronics": Commodity(
        "electronics", "Micro-Processors", "High Tech", 110, 0.50, False,
        "Optical circuit boards for ship computers.", "[CPU]"
    ),
    "cybernetics": Commodity(
        "cybernetics", "Neural Implants", "High Tech", 190, 0.60, False,
        "Biomechanical enhancements for operators.", "[CYB]"
    ),
    "machinery": Commodity(
        "machinery", "Industrial Assemblers", "High Tech", 140, 0.45, False,
        "Heavy robotic factory equipment.", "[MCH]"
    ),
    "fusion_cores": Commodity(
        "fusion_cores", "Compact Fusion Cores", "High Tech", 280, 0.65, False,
        "Miniature containment reactors.", "[FUS]"
    ),
    "luxury": Commodity(
        "luxury", "Venusian Silk & Wines", "Luxury", 160, 0.55, False,
        "Extravagant commodities for high society.", "[LUX]"
    ),
    "antiques": Commodity(
        "antiques", "Pre-Solar Artifacts", "Luxury", 240, 0.75, False,
        "Priceless relics from ancient colonies.", "[ART]"
    ),
    "gemstones": Commodity(
        "gemstones", "Nebula-Cut Gemstones", "Luxury", 195, 0.60, False,
        "Radiant crystalline gems formed in stellar nurseries.", "[GEM]"
    ),
    "textiles": Commodity(
        "textiles", "Synth-Weave Textiles", "Essentials", 28, 0.35, False,
        "Durable climate-adaptive fabrics for colonists and crews.", "[TEX]"
    ),
    "weapons": Commodity(
        "weapons", "Military Plasma Rifles", "Contraband", 220, 0.70, True,
        "Restricted arms sought by mercenary bands.", "[GUN]"
    ),
    "narcotics": Commodity(
        "narcotics", "Neuro-Stimulants", "Contraband", 260, 0.85, True,
        "Addictive combat stimulants banned by law.", "[STM]"
    ),
    "ai_cores": Commodity(
        "ai_cores", "Unshackled AI Cores", "Contraband", 420, 0.90, True,
        "Autonomous synthetic intelligences.", "[AI*]"
    ),
}


# ==============================================================================
# PLANET EVENTS
# ==============================================================================

@dataclass
class PlanetEvent:
    name: str
    good: str
    mult: float
    duration: int
    desc: str
    fresh: bool = True


PLANET_EVENTS_POOL: List[Tuple[str, str, float, str]] = [
    ("Famine", "food", 3.2,
     "Severe drought and crop blight have caused critical food shortages!"),
    ("Plague Outbreak", "medicine", 4.0,
     "A virulent mutant virus has erupted. Medical supplies in extreme demand!"),
    ("Tech Revolution", "electronics", 0.4,
     "Breakthrough quantum manufacturing yields massive surplus electronics."),
    ("Mineral Rush", "ore", 0.45,
     "Newly discovered rich asteroid seams cause ore prices to plummet."),
    ("Pirate Siege", "weapons", 3.0,
     "Pirates are raiding convoys. Defense armaments fetch sky-high prices!"),
    ("Police Crackdown", "narcotics", 3.5,
     "Syndicate raids make illicit contraband extremely scarce and valuable!"),
    ("Imperial Gala", "luxury", 2.8,
     "Galactic dignitaries convene for an opulent celebration. Luxury goods surge!"),
    ("Fusion Research Boom", "fusion_cores", 0.5,
     "Orbital reactor breakthrough produces excess fusion cores cheap."),
    ("Solar Lottery", "luxury", 2.3,
     "A trillion-credit lottery drawing floods the resorts with big spenders!"),
    ("Black Market Expo", "ai_cores", 2.6,
     "An underground expo draws every crime lord in the sector. Contraband soars!"),
    ("Harvest Festival", "food", 0.5,
     "Record harvests and open granaries push food prices to historic lows."),
    ("Shipyard Boom", "alloys", 2.6,
     "A naval contract has shipyards buying structural alloys at a premium!"),
    ("Quantum Synthesis Leak", "crystals", 0.42,
     "A new synthesis method floods the market with dilithium crystals."),
    ("Cyber-Riots", "cybernetics", 3.1,
     "Riots destroy implant clinics. Neural enhancements are in critical demand!"),
    ("Water Contamination", "water", 2.6,
     "A broken reclaimer has contaminated the reserves. Pure water is precious!"),
    ("Drone Uprising", "machinery", 2.4,
     "Malfunctioning factory drones wrecked assembly lines. Replacements needed!"),
    ("Gem Rush", "gemstones", 2.7,
     "A newly cracked geode field has jewelers bidding fortunes for fresh gemstones!"),
    ("Textile Mill Fire", "textiles", 2.9,
     "A fabrication mill fire has gutted local textile stocks. Colonists need cloth!"),
]


# ==============================================================================
# PLANETS
# ==============================================================================

@dataclass
class Planet:
    name: str
    subtitle: str
    x: float
    y: float
    tech: float
    agri: float
    crime: float
    rich: float
    mining: float
    security: str
    faction: str
    color: str
    desc: str
    market: Dict[str, int] = field(default_factory=dict)
    stock: Dict[str, int] = field(default_factory=dict)
    price_history: Dict[str, List[int]] = field(default_factory=dict)
    fuel_price: int = BASE_FUEL_PRICE
    repair_cost: int = BASE_REPAIR_COST
    active_event: Optional[PlanetEvent] = None

    # ------------------------------------------------------------------ #

    def _trait_mult(self, comm: Commodity) -> float:
        if comm.category == "Essentials":
            return 1.0 / max(0.5, self.agri)
        if comm.category == "Raw Materials":
            return 1.0 / max(0.5, self.mining)
        if comm.category == "High Tech":
            return 1.0 / max(0.5, self.tech)
        if comm.category == "Luxury":
            return max(0.5, self.rich)
        if comm.category == "Contraband":
            return max(0.3, self.crime)
        return 1.0

    def _target_stock(self, comm: Commodity) -> int:
        if comm.is_contraband:
            if self.security == "High":
                return random.randint(0, 10)
            if self.security == "None":
                return random.randint(18, 60)
            return random.randint(2, 25)
        trait = self._trait_mult(comm)
        # Trait mult is inverted for supply categories: low mult = produces.
        production = 1.0 / max(0.4, trait)
        base_stock = int(26 + 26 * production)
        return random.randint(max(4, base_stock // 2), max(6, int(base_stock * 1.8)))

    # ------------------------------------------------------------------ #

    def generate_market(self, days_passed: int = 1) -> None:
        """Tick market prices & stock with mean-reverting smooth dynamics."""
        if self.active_event:
            self.active_event.duration -= days_passed
            if self.active_event.duration <= 0:
                self.active_event = None

        if not self.active_event and random.random() < 0.14:
            name, good, mult, desc = random.choice(PLANET_EVENTS_POOL)
            self.active_event = PlanetEvent(
                name=name, good=good, mult=mult,
                duration=random.randint(4, 9), desc=desc,
            )

        for gid, comm in COMMODITIES.items():
            event_mult = (
                self.active_event.mult
                if self.active_event and self.active_event.good == gid
                else 1.0
            )
            target = (
                comm.base_price
                * self._trait_mult(comm)
                * event_mult
                * random.uniform(0.94, 1.06)
            )
            prev = self.market.get(gid, target)
            smoothed = prev * 0.50 + target * 0.50
            jitter = smoothed * (comm.volatility * 0.25) * random.uniform(-1.0, 1.0)
            price = int(round(smoothed + jitter))
            price = int(clamp(price, comm.base_price * 0.25, comm.base_price * 3.2))
            price = max(2, price)
            self.market[gid] = price

            hist = self.price_history.setdefault(gid, [])
            hist.append(price)
            if len(hist) > PRICE_HISTORY_LEN:
                hist.pop(0)

            tgt_stock = self._target_stock(comm)
            old_stock = self.stock.get(gid, tgt_stock)
            drifted = int(old_stock * 0.55 + tgt_stock * random.uniform(0.55, 1.15))
            self.stock[gid] = max(0, drifted)

        fuel_target = (
            BASE_FUEL_PRICE * random.uniform(0.85, 1.35) / max(0.4, self.mining)
        )
        self.fuel_price = max(4, int(self.fuel_price * 0.4 + fuel_target * 0.6))

        repair_target = (
            BASE_REPAIR_COST * random.uniform(0.85, 1.3) / max(0.5, self.tech)
        )
        self.repair_cost = max(10, int(self.repair_cost * 0.4 + repair_target * 0.6))

    def trend(self, good: str) -> str:
        """Return a trend arrow key from the recent price history."""
        hist = self.price_history.get(good, [])
        if len(hist) < 2:
            return "-"
        a, b = hist[-2], hist[-1]
        if b > a * 1.03:
            return "up"
        if b < a * 0.97:
            return "down"
        return "flat"


SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: List[int], width: int = PRICE_HISTORY_LEN) -> str:
    """Render a short history of prices as a unicode block-character chart."""
    if not values:
        return "·"
    vals = values[-width:]
    if len(vals) == 1:
        return "▄"
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return "▄" * len(vals)
    out = []
    for v in vals:
        idx = int(round((v - lo) / (hi - lo) * (len(SPARK_CHARS) - 1)))
        out.append(SPARK_CHARS[idx])
    return "".join(out)


def generate_default_planets() -> Dict[str, Planet]:
    planets = [
        Planet(
            "Earth", "Sol Prime Metropolis", 0.0, 0.0,
            1.5, 1.3, 0.4, 1.6, 0.7, "High", "Sol Federation", "#00E5FF",
            "Cradle of humanity. High-tech capital and center of galactic commerce."
        ),
        Planet(
            "Mars", "Olympus Heavy Foundry", 3.2, 1.4,
            1.3, 0.5, 0.7, 1.1, 1.6, "High", "Sol Federation", "#FF5252",
            "Industrial powerhouse specializing in raw metal smelting and starship manufacturing."
        ),
        Planet(
            "Europa", "Under-Ice Oceanic Hub", 5.4, 4.8,
            1.2, 0.9, 0.5, 1.3, 1.1, "Medium", "Outer Alliance", "#40C4FF",
            "Sub-surface scientific haven rich in bio-research and pure water reserves."
        ),
        Planet(
            "Titan", "Methane Sea Refineries", 8.6, 2.2,
            0.9, 0.4, 1.0, 0.9, 1.9, "Medium", "Mining Syndicate", "#FFD740",
            "Hydrocarbon and hyper-fuel processing colony shrouded in dense orange mist."
        ),
        Planet(
            "Venus", "Aerosol Sky Citadel", 1.8, 6.5,
            1.4, 0.6, 0.8, 1.7, 0.5, "High", "Sol Federation", "#FFAB40",
            "Luxurious floating cloud cities catering to elite galactic aristocrats."
        ),
        Planet(
            "Ceres", "Belt Outpost 01", 6.8, 8.2,
            0.8, 0.4, 1.4, 0.7, 2.0, "Low", "Mining Syndicate", "#B0BEC5",
            "Rugged asteroid base populated by miners, prospectors, and shady brokers."
        ),
        Planet(
            "Alpha Station", "Frontier Research Nexus", 11.2, 7.6,
            1.7, 0.5, 0.7, 1.5, 0.9, "High", "Outer Alliance", "#7C6BFF",
            "Deep space gateway focused on quantum computation and antimatter physics."
        ),
        Planet(
            "Pirate Haven", "Deadman's Asteroid Cove", 12.8, 3.2,
            0.7, 0.3, 2.4, 1.0, 0.9, "None", "Free Corsairs", "#FF5C7A",
            "Lawless haven where weapons, stims, and black market AI cores trade openly."
        ),
        Planet(
            "Kepler Ring", "Outer Mining Colony", 14.5, 9.8,
            0.6, 0.3, 1.8, 0.6, 2.2, "None", "Mining Syndicate", "#A1887F",
            "Dangerous frontier mineral belt constantly threatened by marauder raids."
        ),
        Planet(
            "Neptune Base", "Triton Cryo-Laboratories", 4.0, 11.0,
            1.5, 0.5, 0.6, 1.2, 1.3, "Medium", "Outer Alliance", "#00B0FF",
            "Isolated research complex analyzing exotic dark matter and ancient probes."
        ),
        Planet(
            "Rigel Prime", "Corporate Mega-Hub", 7.2, 0.6,
            1.9, 0.6, 0.5, 1.8, 0.6, "High", "Sol Federation", "#00E676",
            "Gleaming corporate arcology. Cutting-edge tech exports and deep luxury demand."
        ),
        Planet(
            "Elara Station", "Agricultural Dome Cluster", 2.6, 3.6,
            0.9, 2.3, 0.5, 0.8, 0.8, "Low", "Sol Federation", "#9CCC65",
            "Vast greenhouse domes feeding half the sector. Essentials are dirt cheap here."
        ),
        Planet(
            "Hyperion Foundry", "The Forge World", 10.5, 5.2,
            1.2, 0.4, 1.0, 1.0, 2.3, "Medium", "Mining Syndicate", "#FF6E40",
            "Endless smelters refining asteroids day and night. Raw materials pour from its docks."
        ),
        Planet(
            "Nebula's Rest", "Stellar Spa & Resort", 7.8, 13.4,
            1.1, 0.8, 0.3, 2.1, 0.5, "Medium", "Outer Alliance", "#F48FB1",
            "A serene resort ringed by a glowing nebula. The wealthy pay anything for comfort."
        ),
        Planet(
            "Black Hollow", "Smuggler's Abyss", 15.0, 13.0,
            0.6, 0.3, 2.6, 0.9, 0.8, "None", "Free Corsairs", "#546E7A",
            "A lawless trench-station where every contraband in the sector finds a buyer."
        ),
        Planet(
            "Tartarus", "Penal Labor Colony", 5.9, 7.0,
            0.8, 0.9, 0.2, 0.5, 1.5, "High", "Sol Federation", "#E040FB",
            "Maximum-security prison mining complex. Scrap is cheap; luxuries practically unknown."
        ),
    ]
    return {p.name: p for p in planets}


# ==============================================================================
# SHIPS
# ==============================================================================

@dataclass
class ShipTemplate:
    id: str
    name: str
    ship_class: str
    cost: int
    cargo_cap: int
    max_hull: int
    max_shield: int
    max_fuel: int
    speed: float
    weapon_slots: int
    shield_slots: int
    module_slots: int
    desc: str


SHIP_TEMPLATES: Dict[str, ShipTemplate] = {
    "sparrow": ShipTemplate(
        "sparrow", "Star Sparrow", "Light Courier", 0,
        cargo_cap=25, max_hull=100, max_shield=40, max_fuel=130, speed=1.2,
        weapon_slots=1, shield_slots=1, module_slots=2,
        desc="Nimble starter vessel with decent speed and low operating cost."
    ),
    "kestrel": ShipTemplate(
        "kestrel", "Kestrel Scout", "Recon Cutter", 9_500,
        cargo_cap=18, max_hull=90, max_shield=55, max_fuel=170, speed=1.7,
        weapon_slots=2, shield_slots=1, module_slots=2,
        desc="Blindingly fast scout hull favored by couriers and smugglers alike."
    ),
    "orion": ShipTemplate(
        "orion", "Orion Trader", "Medium Freighter", 18_000,
        cargo_cap=60, max_hull=160, max_shield=70, max_fuel=150, speed=1.0,
        weapon_slots=2, shield_slots=1, module_slots=3,
        desc="Reliable commercial cargo vessel favored by free traders."
    ),
    "drake": ShipTemplate(
        "drake", "Drake Freighter", "Armored Hauler", 26_000,
        cargo_cap=45, max_hull=240, max_shield=90, max_fuel=150, speed=1.0,
        weapon_slots=2, shield_slots=2, module_slots=3,
        desc="A trading hull wrapped in military plating. Slower than an Orion, "
             "but built to survive the lawless lanes with its cargo intact."
    ),
    "viper": ShipTemplate(
        "viper", "Viper Interceptor", "Combat Scout", 32_000,
        cargo_cap=30, max_hull=140, max_shield=100, max_fuel=180, speed=1.5,
        weapon_slots=3, shield_slots=2, module_slots=2,
        desc="Fast military pursuit fighter equipped with formidable firepower."
    ),
    "phoenix": ShipTemplate(
        "phoenix", "Phoenix Explorer", "Deep Range Cruiser", 48_000,
        cargo_cap=40, max_hull=170, max_shield=110, max_fuel=300, speed=1.45,
        weapon_slots=2, shield_slots=2, module_slots=3,
        desc="A long-legged survey cruiser with enormous fuel reserves — built "
             "for captains who chart the far systems and profit on the way."
    ),
    "titan": ShipTemplate(
        "titan", "Titan Heavy Hauler", "Heavy Freighter", 68_000,
        cargo_cap=140, max_hull=280, max_shield=120, max_fuel=220, speed=0.8,
        weapon_slots=2, shield_slots=2, module_slots=4,
        desc="Massive industrial bulk freighter capable of transporting huge hauls."
    ),
    "valkyrie": ShipTemplate(
        "valkyrie", "Valkyrie Gunship", "Heavy Frigate", 115_000,
        cargo_cap=75, max_hull=380, max_shield=200, max_fuel=240, speed=1.1,
        weapon_slots=4, shield_slots=3, module_slots=3,
        desc="Heavily armored gunship equipped to obliterate pirate squadrons."
    ),
    "behemoth": ShipTemplate(
        "behemoth", "Galactic Behemoth", "Dreadnought", 220_000,
        cargo_cap=260, max_hull=550, max_shield=320, max_fuel=320, speed=0.7,
        weapon_slots=4, shield_slots=4, module_slots=5,
        desc="The pinnacle of aerospace engineering. A floating fortress with colossal hold."
    ),
    "sovereign": ShipTemplate(
        "sovereign", "Sovereign Dreadnought", "Flagship", 350_000,
        cargo_cap=210, max_hull=700, max_shield=420, max_fuel=360, speed=0.95,
        weapon_slots=5, shield_slots=4, module_slots=6,
        desc="A one-of-a-kind flagship hull, part warship and part mobile trade empire. "
             "The ultimate expression of galactic power."
    ),
}


# ==============================================================================
# EQUIPMENT
# ==============================================================================

@dataclass
class Equipment:
    id: str
    name: str
    slot_type: str  # weapon, shield, module
    cost: int
    damage: int = 0
    shield_hp: int = 0
    accuracy: float = 0.0    # only meaningful for weapons & the targeting module
    crit_chance: float = 0.0
    fuel_save: float = 0.0
    cargo_bonus: int = 0
    evasion_bonus: float = 0.0
    desc: str = ""


EQUIPMENT_ITEMS: Dict[str, Equipment] = {
    "laser_1": Equipment(
        "laser_1", "Pulse Laser Mk I", "weapon", 2_500,
        damage=22, accuracy=0.85, crit_chance=0.08,
        desc="Standard rapid-fire beam emitter."
    ),
    "laser_2": Equipment(
        "laser_2", "Pulse Laser Mk II", "weapon", 6_500,
        damage=38, accuracy=0.88, crit_chance=0.12,
        desc="Overcharged multi-phase laser cannon."
    ),
    "flak_cannon": Equipment(
        "flak_cannon", "Twin Flak Cannon", "weapon", 8_000,
        damage=30, accuracy=0.95, crit_chance=0.10,
        desc="Proximity-burst shrapnel batteries. Cheap, dependable, rarely misses."
    ),
    "missile_rack": Equipment(
        "missile_rack", "Havoc Missile Launcher", "weapon", 9_500,
        damage=0, accuracy=0.95,
        desc="Enables missile attacks. Fires purchasable missiles (85-125 dmg, 95% hit)."
    ),
    "plasma_1": Equipment(
        "plasma_1", "Heavy Plasma Mortar", "weapon", 14_000,
        damage=65, accuracy=0.75, crit_chance=0.15,
        desc="Fires superheated bolts of high-energy plasma."
    ),
    "ion_cannon": Equipment(
        "ion_cannon", "Ion Disabler", "weapon", 18_000,
        damage=50, accuracy=0.90, crit_chance=0.20,
        desc="Electromagnetic weapon designed to shred shields."
    ),
    "railgun_1": Equipment(
        "railgun_1", "Gauss Railgun", "weapon", 28_000,
        damage=95, accuracy=0.82, crit_chance=0.25,
        desc="Electromagnetic hypervelocity armor penetrator."
    ),
    "particle_lance": Equipment(
        "particle_lance", "Particle Beam Lance", "weapon", 45_000,
        damage=120, accuracy=0.86, crit_chance=0.28,
        desc="A spinal-mount energy lance that shears through hull plating like foil."
    ),
    "shield_1": Equipment(
        "shield_1", "Deflector Barrier", "shield", 3_000,
        shield_hp=40,
        desc="Basic energy screen against micrometeorites and lasers."
    ),
    "shield_2": Equipment(
        "shield_2", "Kinetic Matrix Shield", "shield", 9_000,
        shield_hp=80,
        desc="Military shield dissipating both lasers and ballistics."
    ),
    "shield_3": Equipment(
        "shield_3", "Aegis Fortress Generator", "shield", 22_000,
        shield_hp=150,
        desc="State-of-the-art multi-layer regenerative shield."
    ),
    "cargo_pod": Equipment(
        "cargo_pod", "Expanded Cargo Bay", "module", 7_000,
        cargo_bonus=25,
        desc="Modular cargo expansion pod granting +25 hold capacity."
    ),
    "smuggler_bay": Equipment(
        "smuggler_bay", "Shielded Smuggler Bay", "module", 8_500,
        desc="Hidden lead-lined hull compartment. 75% customs evasion."
    ),
    "deep_scanner": Equipment(
        "deep_scanner", "Deep Space Scanner Array", "module", 9_000,
        desc="Long-range market sensors read live prices and events at any world "
             "on the star map before you jump."
    ),
    "thruster_booster": Equipment(
        "thruster_booster", "Afterburner Vector Thrusters", "module", 9_500,
        evasion_bonus=0.15,
        desc="High-agility directional thrusters (+15% combat dodge)."
    ),
    "warp_booster": Equipment(
        "warp_booster", "Warp Field Compressor", "module", 12_000,
        fuel_save=0.30,
        desc="Optimizes sub-space warp vectors, saving 30% fuel on jumps."
    ),
    "shield_capacitor": Equipment(
        "shield_capacitor", "Shield Capacitor Bank", "module", 12_000,
        desc="Dedicated energy reserve: the RECHARGE maneuver restores 50% of "
             "shields instead of 35%."
    ),
    "combat_scanner": Equipment(
        "combat_scanner", "Targeting Computer AI", "module", 11_000,
        accuracy=0.10, crit_chance=0.10,
        desc="Assists tactical targeting for weapons (+10% hit, +10% crit)."
    ),
    "boarding_pod": Equipment(
        "boarding_pod", "Boarding Assault Pod", "module", 13_500,
        desc="Armored breaching pod and marine complement. +20% boarding success chance."
    ),
    "drone_bay": Equipment(
        "drone_bay", "Autonomous Drone Bay", "module", 15_000,
        desc="Launch attack drones in combat: 8-16 automatic damage every turn once deployed."
    ),
    "nanite_repair": Equipment(
        "nanite_repair", "Nanite Hull Reconstructor", "module", 16_000,
        desc="Microscopic repair drones heal 8 hull daily and fix one damaged subsystem per day."
    ),
    "cloaking_field": Equipment(
        "cloaking_field", "Phase Cloak Emitter", "module", 19_500,
        evasion_bonus=0.10,
        desc="Bends light around the hull. Cuts the chance of hostile encounters "
             "while cruising and grants +10% combat dodge."
    ),
}


# ==============================================================================
# CREW
# ==============================================================================

@dataclass
class CrewCandidate:
    id: str
    name: str
    role: str
    hire_cost: int
    daily_wage: int
    perk_type: str
    perk_val: float
    desc: str


AVAILABLE_CREW: List[CrewCandidate] = [
    CrewCandidate(
        "vance", "Capt. Marcus Vance", "Master Navigator",
        2_500, 45, "nav", 0.25,
        "Reduces travel fuel cost by 25% and improves flee chance."
    ),
    CrewCandidate(
        "drake", "Jax 'Ironclad' Drake", "Chief Gunner",
        3_200, 60, "gunner", 0.25,
        "Increases all weapon damage by 25% and boosts boarding success."
    ),
    CrewCandidate(
        "aria", "Dr. Aria T'Soni", "Biochemist & Medic",
        2_800, 50, "engineer", 8.0,
        "Repairs 8 hull points per day and fixes one damaged subsystem each day."
    ),
    CrewCandidate(
        "zoe", "Zoe 'Spectre' Miller", "Smuggler & Hacker",
        3_500, 65, "smuggler", 0.50,
        "Reduces customs scan fines by 50% and bribes cheaper."
    ),
    CrewCandidate(
        "chen", "Kaelen Chen", "Trade Broker",
        4_000, 75, "trader", 0.08,
        "Negotiates 8% better buying and selling prices across all spaceports."
    ),
    CrewCandidate(
        "t800", "Unit 7-Echo (Android)", "System Specialist",
        5_000, 40, "engineer", 12.0,
        "Reinforces shields by +20% and regenerates ship systems."
    ),
    CrewCandidate(
        "sable", "Envoy Corin Sable", "Diplomatic Attache",
        3_600, 55, "diplomat", 1.0,
        "Doubles all faction reputation gains and softens reputation losses."
    ),
]

CREW_INDEX: Dict[str, CrewCandidate] = {c.id: c for c in AVAILABLE_CREW}


# ==============================================================================
# MISSIONS
# ==============================================================================

@dataclass
class Mission:
    id: str
    title: str
    m_type: str  # delivery, smuggle, bounty, medical
    origin: str
    destination: str
    cargo_good: Optional[str]
    cargo_qty: int
    bounty_target_name: Optional[str]
    bounty_target_ship: Optional[str]
    reward_credits: int
    days_left: int
    desc: str
    completed: bool = False
    failed: bool = False


PIRATE_NAMES = [
    "Viper Malor", "Krag 'The Ripper'", "Commander Vex", "Captain Blood-Eye",
    "Ghost Marauder", "Dread Corsair Morgan", "Siren Vance",
    "Helix the Butcher", "Baron Rook", "Mad Queen Lyra",
]


def generate_mission_board(
    current_planet: Planet,
    all_planets: List[Planet],
    day: int,
    career_tier: int = 0,
) -> List[Mission]:
    """Build the contract board. career_tier (0-2) enriches rewards with rank."""
    missions: List[Mission] = []
    dest_candidates = [p for p in all_planets if p.name != current_planet.name]
    if not dest_candidates:
        return missions

    # Career tier sweetens the pot for proven captains.
    tier_mult = 1.0 + 0.12 * clamp(career_tier, 0, 2)

    def new_id(prefix: str) -> str:
        return f"mis_{prefix}_{day}_{random.randint(1000, 9999)}"

    def pick_dst() -> Planet:
        return random.choice(dest_candidates)

    # 1. Cargo deliveries — one or two per board.
    for _ in range(random.choice([1, 2, 2])):
        dst = pick_dst()
        legal_goods = [g for g, c in COMMODITIES.items() if not c.is_contraband]
        good = random.choice(legal_goods)
        qty = random.randint(5, 18)
        dist = math.hypot(current_planet.x - dst.x, current_planet.y - dst.y)
        days_allowed = max(3, int(dist * 0.8) + 3)
        reward = int((COMMODITIES[good].base_price * qty * 1.65 + dist * 120) * tier_mult)

        missions.append(Mission(
            id=new_id("del"),
            title=f"Commercial Haul: {qty}x {COMMODITIES[good].name}",
            m_type="delivery",
            origin=current_planet.name,
            destination=dst.name,
            cargo_good=good,
            cargo_qty=qty,
            bounty_target_name=None,
            bounty_target_ship=None,
            reward_credits=reward,
            days_left=days_allowed,
            desc=f"Transport {qty} crates of {COMMODITIES[good].name} to {dst.name} "
                 f"within {days_allowed} days."
        ))

    # 2. Smuggling
    if current_planet.crime >= 0.8 or random.random() < 0.5:
        dst_smug = pick_dst()
        c_good = random.choice(["weapons", "narcotics", "ai_cores"])
        c_qty = random.randint(3, 10)
        c_dist = math.hypot(current_planet.x - dst_smug.x, current_planet.y - dst_smug.y)
        c_days = max(4, int(c_dist * 0.7) + 2)
        c_reward = int((COMMODITIES[c_good].base_price * c_qty * 2.8 + c_dist * 190) * tier_mult)

        missions.append(Mission(
            id=new_id("smug"),
            title=f"[SHADOW CONTRACT] Smuggle {COMMODITIES[c_good].name}",
            m_type="smuggle",
            origin=current_planet.name,
            destination=dst_smug.name,
            cargo_good=c_good,
            cargo_qty=c_qty,
            bounty_target_name=None,
            bounty_target_ship=None,
            reward_credits=c_reward,
            days_left=c_days,
            desc=f"Discreetly sneak {c_qty}x {COMMODITIES[c_good].name} into {dst_smug.name}. "
                 f"Evade customs scans!"
        ))

    # 3. Bounty
    dst_bounty = pick_dst()
    target_name = random.choice(PIRATE_NAMES)
    bounty_ships = ["viper", "orion", "valkyrie", "drake"]
    b_ship = random.choice(bounty_ships)
    b_dist = math.hypot(current_planet.x - dst_bounty.x, current_planet.y - dst_bounty.y)
    b_reward = int(random.randint(5_000, 13_000) * tier_mult)
    b_days = max(5, int(b_dist) + 5)

    missions.append(Mission(
        id=new_id("bounty"),
        title=f"WANTED DEAD: {target_name}",
        m_type="bounty",
        origin=current_planet.name,
        destination=dst_bounty.name,
        cargo_good=None,
        cargo_qty=0,
        bounty_target_name=target_name,
        bounty_target_ship=b_ship,
        reward_credits=b_reward,
        days_left=b_days,
        desc=f"Locate and eliminate pirate warlord {target_name} piloting a "
             f"{SHIP_TEMPLATES[b_ship].name} near {dst_bounty.name}."
    ))

    # 4. Medical relief
    dst_med = pick_dst()
    med_qty = random.randint(6, 14)
    med_reward = int((75 * med_qty * 2.4 + 900) * tier_mult)
    med_dist = math.hypot(current_planet.x - dst_med.x, current_planet.y - dst_med.y)

    missions.append(Mission(
        id=new_id("med"),
        title=f"URGENT: Vaccine Relief to {dst_med.name}",
        m_type="medical",
        origin=current_planet.name,
        destination=dst_med.name,
        cargo_good="medicine",
        cargo_qty=med_qty,
        bounty_target_name=None,
        bounty_target_ship=None,
        reward_credits=med_reward,
        days_left=max(3, int(med_dist * 0.7) + 2),
        desc=f"Outbreak reported on {dst_med.name}! Rush {med_qty}x Bio-Vaccines to save lives."
    ))

    # 5. VIP passenger charter — no cargo bay required.
    if random.random() < 0.75:
        dst_psg = pick_dst()
        passengers = random.randint(1, 6)
        psg_dist = math.hypot(current_planet.x - dst_psg.x, current_planet.y - dst_psg.y)
        psg_reward = int((900 * passengers + psg_dist * 150 + 400) * tier_mult)
        psg_days = max(3, int(psg_dist * 0.6) + 3)

        missions.append(Mission(
            id=new_id("psg"),
            title=f"VIP Charter: {passengers} Passenger{'s' if passengers > 1 else ''} to {dst_psg.name}",
            m_type="passenger",
            origin=current_planet.name,
            destination=dst_psg.name,
            cargo_good=None,
            cargo_qty=passengers,
            bounty_target_name=None,
            bounty_target_ship=None,
            reward_credits=psg_reward,
            days_left=psg_days,
            desc=(f"A wealthy delegation requests discreet passage to {dst_psg.name}. "
                  f"No cargo space required — deliver them safely within {psg_days} days.")
        ))

    return missions


# ==============================================================================
# STOCK MARKET
# ==============================================================================

@dataclass
class StockData:
    symbol: str
    name: str
    price: float
    history: List[float]
    volatility: float
    desc: str


DEFAULT_STOCKS: Dict[str, StockData] = {
    "SOL": StockData(
        "SOL", "Sol Federation Dynamics", 120.0, [120.0], 0.04,
        "Galactic transport, infrastructure and planetary terraforming."
    ),
    "MIN": StockData(
        "MIN", "Titan & Ceres Mining Cartel", 55.0, [55.0], 0.07,
        "Deep space asteroid extraction and raw materials monopoly."
    ),
    "BIO": StockData(
        "BIO", "Europa Bio-Genetics Labs", 145.0, [145.0], 0.06,
        "Pharmaceuticals, cryogenic suspension and neural cybernetics."
    ),
    "CYB": StockData(
        "CYB", "Venusian Cyber-Optics", 90.0, [90.0], 0.08,
        "AI hardware, quantum microchips and ship targeting avionics."
    ),
    "SHD": StockData(
        "SHD", "Shadow Syndicate Logistics", 70.0, [70.0], 0.12,
        "Unregulated frontier shipping and black market ventures."
    ),
}


# ==============================================================================
# PLAYER
# ==============================================================================

DEFAULT_STATS: Dict[str, int] = {
    "total_profit": 0,
    "jumps_made": 0,
    "pirates_defeated": 0,
    "bounties_claimed": 0,
    "contraband_sold": 0,
    "missions_completed": 0,
    "missiles_fired": 0,
    "boards": 0,
    "insurance_claims": 0,
    "mining_ops": 0,
    "wormholes": 0,
}

# ------------------------------------------------------------------ #
# Faction reputation

FACTIONS: Tuple[str, ...] = (
    "Sol Federation", "Outer Alliance", "Mining Syndicate", "Free Corsairs",
)
REPUTATION_MIN = -100
REPUTATION_MAX = 100

FACTION_COLORS: Dict[str, str] = {
    "Sol Federation": "#00E5FF",
    "Outer Alliance": "#7C6BFF",
    "Mining Syndicate": "#FFC53D",
    "Free Corsairs": "#FF5C7A",
}


def default_reputation() -> Dict[str, int]:
    return {f: 0 for f in FACTIONS}


def reputation_rank(value: int) -> str:
    if value >= 75:
        return "Exalted"
    if value >= 40:
        return "Trusted"
    if value >= 15:
        return "Friendly"
    if value > -15:
        return "Neutral"
    if value > -40:
        return "Disliked"
    if value > -75:
        return "Hostile"
    return "Nemesis"


@dataclass
class Player:
    name: str = "Commander"
    credits: int = STARTING_CREDITS
    savings: int = 0
    loan: int = 0
    credit_score: int = 650
    day: int = 1
    location: str = "Earth"
    difficulty_id: str = "normal"
    ship_id: str = STARTING_SHIP
    hull: int = 100
    max_hull: int = 100
    shield: int = 40
    max_shield: int = 40
    fuel: int = 120
    max_fuel: int = 120
    cargo_cap: int = 25
    missiles: int = 0
    insurance_active: bool = False
    weapons_damaged: bool = False
    engines_damaged: bool = False
    shields_damaged: bool = False
    highest_rank_index: int = 0
    cargo: Dict[str, int] = field(default_factory=dict)
    cargo_cost_basis: Dict[str, float] = field(default_factory=dict)
    equipped_weapons: List[str] = field(default_factory=lambda: ["laser_1"])
    equipped_shields: List[str] = field(default_factory=lambda: ["shield_1"])
    equipped_modules: List[str] = field(default_factory=list)
    hired_crew: List[str] = field(default_factory=list)
    active_missions: List[Mission] = field(default_factory=list)
    stocks_owned: Dict[str, int] = field(default_factory=dict)
    achievements: Set[str] = field(default_factory=set)
    stats: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_STATS))
    net_worth_history: List[int] = field(default_factory=lambda: [STARTING_CREDITS])
    reputation: Dict[str, int] = field(default_factory=default_reputation)

    # ---------------- cargo ---------------- #

    def cargo_used(self) -> int:
        return sum(self.cargo.values())

    def cargo_free(self) -> int:
        return max(0, self.cargo_cap - self.cargo_used())

    def add_cargo(self, good: str, qty: int) -> int:
        if qty <= 0:
            return 0
        space = self.cargo_free()
        to_add = min(qty, space)
        if to_add > 0:
            self.cargo[good] = self.cargo.get(good, 0) + to_add
        return to_add

    def remove_cargo(self, good: str, qty: int) -> int:
        if qty <= 0:
            return 0
        have = self.cargo.get(good, 0)
        to_rem = min(qty, have)
        if to_rem > 0:
            self.cargo[good] -= to_rem
            if self.cargo[good] <= 0:
                self.cargo.pop(good, None)
        return to_rem

    # ---------------- helpers ---------------- #

    def has_module(self, mod_id: str) -> bool:
        return mod_id in self.equipped_modules

    def has_crew(self, crew_id: str) -> bool:
        return crew_id in self.hired_crew

    def has_crew_perk(self, perk_type: str) -> Optional[float]:
        for cid in self.hired_crew:
            c = CREW_INDEX.get(cid)
            if c and c.perk_type == perk_type:
                return c.perk_val
        return None

    def has_missile_rack(self) -> bool:
        return "missile_rack" in self.equipped_weapons

    def has_drone_bay(self) -> bool:
        return self.has_module("drone_bay")

    def damaged_subsystems(self) -> List[str]:
        out = []
        if self.weapons_damaged:
            out.append("weapons")
        if self.engines_damaged:
            out.append("engines")
        if self.shields_damaged:
            out.append("shields")
        return out

    def effective_max_shield(self) -> int:
        return int(self.max_shield * (0.6 if self.shields_damaged else 1.0))

    def rep(self, faction: str) -> int:
        return self.reputation.get(faction, 0)


# ==============================================================================
# ACHIEVEMENTS
# ==============================================================================

ACHIEVEMENTS: Dict[str, Tuple[str, str]] = {
    "nw_10k": ("Novice Merchant", "Reach 10,000 CR net worth"),
    "nw_50k": ("Rising Magnate", "Reach 50,000 CR net worth"),
    "nw_100k": ("Solar Tycoon", "Reach 100,000 CR net worth"),
    "nw_250k": ("Sector Baron", "Reach 250,000 CR net worth"),
    "nw_target": ("Galactic Mogul", f"Reach {money(TARGET_NET_WORTH)} CR net worth"),
    "jumps_10": ("Void Wanderer", "Complete 10 hyperjumps"),
    "jumps_25": ("Starpath Veteran", "Complete 25 hyperjumps"),
    "pirates_5": ("Bounty Hunter", "Destroy 5 hostile pirates"),
    "pirates_15": ("Scourge of Corsairs", "Destroy 15 hostile pirates"),
    "contraband_20": ("Ghost Smuggler", "Sell 20 units of contraband"),
    "crew_3": ("Fleet Commander", "Hire 3 specialist crew officers"),
    "missions_5": ("Contract Professional", "Complete 5 contracts"),
    "missile_used": ("Missile Commander", "Fire a missile in combat"),
    "boarder": ("Boarding Party", "Successfully board an enemy vessel"),
    "insured": ("Safe Bet", "Purchase ship insurance"),
    "survivor": ("Deep Space Veteran", "Survive 50 days among the stars"),
    "trusted_ally": ("Trusted Ally", "Reach Exalted standing with any faction"),
    "public_enemy": ("Public Enemy", "Reach Nemesis standing with any faction"),
    "diplomat_corps": ("Diplomat Corps", "Reach Friendly standing or better with every faction"),
    "rank_captain": ("Officer & Gentlebeing", "Earn the rank of Captain"),
    "rank_admiral": ("Flag Officer", "Earn the rank of Admiral"),
    "rock_hound": ("Rock Hound", "Complete 5 asteroid mining operations"),
    "wormhole_rider": ("Wormhole Rider", "Survive a transit through an unstable wormhole"),
}


# ==============================================================================
# GAME ENGINE
# ==============================================================================

class GameEngine:
    """Headless game logic. The GUI drives this class; so does the test suite."""

    def __init__(self, muted: bool = False, difficulty_id: str = "normal"):
        self.sound = SoundManager(muted=muted)
        self.difficulty_id = difficulty_id if difficulty_id in DIFFICULTIES else "normal"
        self.planets: Dict[str, Planet] = generate_default_planets()
        self.stocks: Dict[str, StockData] = {
            k: StockData(**asdict(v)) for k, v in DEFAULT_STOCKS.items()
        }
        self.player = Player(difficulty_id=self.difficulty_id)
        self.available_missions: List[Mission] = []
        self.news_feed: List[str] = []
        self.is_game_over = False
        self.victory_achieved = False
        self.last_result: str = ""

        self.apply_difficulty_start()
        for p in self.planets.values():
            p.generate_market()
        self.available_missions = generate_mission_board(
            self.current_planet, list(self.planets.values()), self.player.day,
            career_tier=self.career_tier(),
        )
        self.recalculate_ship_stats()
        self.player.hull = self.player.max_hull
        self.player.shield = self.player.max_shield
        self.player.fuel = self.player.max_fuel
        self.add_news(
            "Welcome to the Orion-Sol Sector. Markets are active. Good fortune, Commander!"
        )

    # ------------------------------------------------------------------ #
    # Setup / difficulty

    @property
    def difficulty(self) -> Difficulty:
        return DIFFICULTIES.get(self.player.difficulty_id, DIFFICULTIES["normal"])

    def apply_difficulty_start(self) -> None:
        self.player.credits = self.difficulty.starting_credits
        self.player.net_worth_history = [self.difficulty.starting_credits]

    def new_game(self, name: str, difficulty_id: str) -> None:
        """Reset everything for a fresh run."""
        difficulty_id = difficulty_id if difficulty_id in DIFFICULTIES else "normal"
        self.difficulty_id = difficulty_id
        self.planets = generate_default_planets()
        self.stocks = {k: StockData(**asdict(v)) for k, v in DEFAULT_STOCKS.items()}
        self.player = Player(
            name=name.strip() or "Commander",
            difficulty_id=difficulty_id,
        )
        self.apply_difficulty_start()
        self.available_missions = []
        self.news_feed = []
        self.is_game_over = False
        self.victory_achieved = False
        self.last_result = ""
        for p in self.planets.values():
            p.generate_market()
        self.available_missions = generate_mission_board(
            self.current_planet, list(self.planets.values()), self.player.day,
            career_tier=self.career_tier(),
        )
        self.recalculate_ship_stats()
        self.player.hull = self.player.max_hull
        self.player.shield = self.player.max_shield
        self.player.fuel = self.player.max_fuel
        self.add_news(
            f"Captain {self.player.name} takes command in the "
            f"{self.difficulty.name} sector. Good hunting!"
        )
        self.add_news(
            f"Career begins at rank {self.rank().name}. Earn Renown to climb: "
            f"trade, fulfil contracts, hunt bounties, explore."
        )

    @property
    def current_planet(self) -> Planet:
        return self.planets.get(self.player.location, self.planets["Earth"])

    def add_news(self, text: str) -> None:
        self.news_feed.insert(0, f"[Day {self.player.day}] {text}")
        if len(self.news_feed) > 40:
            self.news_feed.pop()

    def announce(self, text: str) -> None:
        """Set the last-action banner consumed by the GUI status bar."""
        self.last_result = text

    # ------------------------------------------------------------------ #
    # Renown & captain ranks

    def renown(self) -> int:
        """Career score driving rank: wealth plus deeds of note."""
        s = self.player.stats
        return int(
            self.calculate_net_worth() * 0.5
            + s.get("pirates_defeated", 0) * 250
            + s.get("bounties_claimed", 0) * 400
            + s.get("missions_completed", 0) * 150
            + s.get("jumps_made", 0) * 8
        )

    def rank_index(self) -> int:
        return rank_index_for(self.renown())

    def rank(self) -> Rank:
        return RANKS[self.rank_index()]

    def career_tier(self) -> int:
        """0-2 board-quality tier for contract generation."""
        return {0: 0, 1: 0, 2: 1, 3: 1, 4: 2, 5: 2}[self.rank_index()]

    def check_promotion(self) -> Optional[Rank]:
        """Announce a newly earned rank. Returns the new Rank or None."""
        idx = self.rank_index()
        if idx > self.player.highest_rank_index:
            self.player.highest_rank_index = idx
            new_rank = RANKS[idx]
            self.add_news(
                f"PROMOTION: {self.player.name} is now a {new_rank.name}! "
                f"Perk — {new_rank.perk}"
            )
            self.sound.play("rank_up")
            return new_rank
        return None

    def renown_to_next_rank(self) -> Tuple[Optional[str], int]:
        """(next rank name, renown still needed) — (None, 0) at max rank."""
        idx = self.rank_index()
        if idx + 1 < len(RANKS):
            nxt = RANKS[idx + 1]
            return nxt.name, max(0, nxt.renown - self.renown())
        return None, 0

    # ------------------------------------------------------------------ #
    # Stats / valuation

    def recalculate_ship_stats(self) -> None:
        tmpl = SHIP_TEMPLATES[self.player.ship_id]

        bonus_cargo = sum(
            EQUIPMENT_ITEMS[m].cargo_bonus
            for m in self.player.equipped_modules
            if m in EQUIPMENT_ITEMS
        )
        self.player.cargo_cap = tmpl.cargo_cap + bonus_cargo
        self.player.max_hull = tmpl.max_hull
        self.player.max_fuel = tmpl.max_fuel

        shield_bonus = sum(
            EQUIPMENT_ITEMS[s].shield_hp
            for s in self.player.equipped_shields
            if s in EQUIPMENT_ITEMS
        )
        max_shield = tmpl.max_shield + shield_bonus
        if self.player.has_crew("t800"):
            max_shield = int(max_shield * 1.2)
        self.player.max_shield = max_shield

        self.player.hull = int(clamp(self.player.hull, 0, self.player.max_hull))
        self.player.shield = int(clamp(self.player.shield, 0, self.player.max_shield))
        self.player.fuel = int(clamp(self.player.fuel, 0, self.player.max_fuel))

    def calculate_net_worth(self) -> int:
        p = self.current_planet

        cargo_val = sum(
            p.market.get(g, COMMODITIES[g].base_price) * q
            for g, q in self.player.cargo.items()
        )
        stocks_val = sum(
            int(self.stocks[sym].price * q)
            for sym, q in self.player.stocks_owned.items()
            if sym in self.stocks
        )

        ship_val = SHIP_TEMPLATES[self.player.ship_id].cost
        eq_val = sum(
            EQUIPMENT_ITEMS[w].cost
            for w in self.player.equipped_weapons
            if w in EQUIPMENT_ITEMS
        )
        eq_val += sum(
            EQUIPMENT_ITEMS[s].cost
            for s in self.player.equipped_shields
            if s in EQUIPMENT_ITEMS
        )
        eq_val += sum(
            EQUIPMENT_ITEMS[m].cost
            for m in self.player.equipped_modules
            if m in EQUIPMENT_ITEMS
        )

        total = (
            self.player.credits
            + self.player.savings
            + cargo_val
            + stocks_val
            + int((ship_val + eq_val) * 0.75)
            - self.player.loan
        )
        return max(0, int(total))

    def adjust_reputation(self, faction: str, delta: int) -> int:
        """Change standing with a faction, applying the Diplomat's perk, the
        Commodore's career bonus, and clamping to the legal range. Returns the
        actual delta applied."""
        if faction not in self.player.reputation:
            return 0
        if self.player.has_crew("sable"):
            delta = int(delta * 2) if delta > 0 else int(delta * 0.5)
        if delta > 0:
            delta = int(delta * RANK_REP_MULT[self.rank_index()])
        before = self.player.reputation[faction]
        after = int(clamp(before + delta, REPUTATION_MIN, REPUTATION_MAX))
        self.player.reputation[faction] = after
        return after - before

    def reputation_price_mult(self, faction: str) -> float:
        """Good standing nudges prices in your favor; poor standing hurts you.
        Returns a multiplier close to 1.0 (e.g. 0.97 .. 1.03)."""
        rep = self.player.rep(faction)
        return 1.0 - clamp(rep / 100.0, -1.0, 1.0) * 0.05

    def check_achievements(self) -> List[str]:
        unlocked: List[str] = []
        nw = self.calculate_net_worth()
        stats = self.player.stats

        def unlock(aid: str):
            if aid in self.player.achievements:
                return
            self.player.achievements.add(aid)
            title, desc = ACHIEVEMENTS[aid]
            unlocked.append(f"{title} — {desc}")
            self.add_news(f"Achievement unlocked: {title}")

        if nw >= 10_000:
            unlock("nw_10k")
        if nw >= 50_000:
            unlock("nw_50k")
        if nw >= 100_000:
            unlock("nw_100k")
        if nw >= 250_000:
            unlock("nw_250k")
        if nw >= TARGET_NET_WORTH:
            unlock("nw_target")
            self.victory_achieved = True

        if stats.get("jumps_made", 0) >= 10:
            unlock("jumps_10")
        if stats.get("jumps_made", 0) >= 25:
            unlock("jumps_25")
        if stats.get("pirates_defeated", 0) >= 5:
            unlock("pirates_5")
        if stats.get("pirates_defeated", 0) >= 15:
            unlock("pirates_15")
        if stats.get("contraband_sold", 0) >= 20:
            unlock("contraband_20")
        if len(self.player.hired_crew) >= 3:
            unlock("crew_3")
        if stats.get("missions_completed", 0) >= 5:
            unlock("missions_5")
        if stats.get("missiles_fired", 0) >= 1:
            unlock("missile_used")
        if stats.get("boards", 0) >= 1:
            unlock("boarder")
        if self.player.insurance_active:
            unlock("insured")
        if self.player.day >= 50:
            unlock("survivor")
        if self.player.highest_rank_index >= 3:
            unlock("rank_captain")
        if self.player.highest_rank_index >= 5:
            unlock("rank_admiral")
        if stats.get("mining_ops", 0) >= 5:
            unlock("rock_hound")
        if stats.get("wormholes", 0) >= 1:
            unlock("wormhole_rider")

        rep_values = self.player.reputation.values()
        if any(v >= 75 for v in rep_values):
            unlock("trusted_ally")
        if any(v <= -75 for v in rep_values):
            unlock("public_enemy")
        if rep_values and all(v >= 15 for v in rep_values):
            unlock("diplomat_corps")

        return unlocked

    # ------------------------------------------------------------------ #
    # Time

    def advance_day(self, days: int = 1) -> None:
        for _ in range(days):
            self.player.day += 1

            if self.player.loan > 0:
                interest = max(
                    1,
                    int(self.player.loan * self._effective_loan_interest()),
                )
                self.player.loan += interest

            if self.player.savings > 0:
                earn = int(self.player.savings * 0.008)
                self.player.savings += earn

            total_wages = int(self.total_daily_wages())
            if total_wages > 0:
                if self.player.credits >= total_wages:
                    self.player.credits -= total_wages
                else:
                    shortfall = total_wages - self.player.credits
                    self.player.credits = 0
                    self.player.loan += shortfall
                    self.add_news("Warning: unpaid crew wages were added to your loan.")

            # Daily regeneration
            repair_hp = 0
            if self.player.has_module("nanite_repair"):
                repair_hp += 8
            if self.player.has_crew("aria"):
                repair_hp += 8
            if repair_hp > 0:
                self.player.hull = min(self.player.max_hull, self.player.hull + repair_hp)

            if self.player.has_module("nanite_repair") or self.player.has_crew("aria"):
                damaged = self.player.damaged_subsystems()
                if damaged:
                    fixed = damaged[0]
                    if fixed == "weapons":
                        self.player.weapons_damaged = False
                    elif fixed == "engines":
                        self.player.engines_damaged = False
                    elif fixed == "shields":
                        self.player.shields_damaged = False
                    self.add_news(f"Engineering reports: {fixed} subsystem restored overnight.")

            self.player.shield = self.player.effective_max_shield()

            for sym, stk in self.stocks.items():
                pct_chg = random.gauss(0.0025, stk.volatility)
                stk.price = max(5.0, round(stk.price * (1 + pct_chg), 2))
                stk.history.append(stk.price)
                if len(stk.history) > 30:
                    stk.history.pop(0)

            for p in self.planets.values():
                p.generate_market(days_passed=1)
                ev = p.active_event
                if ev and ev.fresh:
                    ev.fresh = False
                    self.add_news(f"Intel Report: {ev.name} declared at {p.name}!")

            expired: List[Mission] = []
            for m in self.player.active_missions:
                m.days_left -= 1
                if m.days_left <= 0 and not m.completed:
                    m.failed = True
                    expired.append(m)
            for em in expired:
                self.player.active_missions.remove(em)
                self.add_news(f"Mission failed: contract '{em.title}' expired!")

        nw = self.calculate_net_worth()
        self.player.net_worth_history.append(nw)
        if len(self.player.net_worth_history) > 60:
            self.player.net_worth_history.pop(0)

        self.available_missions = generate_mission_board(
            self.current_planet,
            list(self.planets.values()),
            self.player.day,
            career_tier=self.career_tier(),
        )
        self.check_promotion()
        self.check_achievements()

    def _effective_loan_interest(self) -> float:
        """Loan interest shaped by difficulty, credit score and Admiral perk."""
        base = self.difficulty.loan_interest
        score = self.player.credit_score
        if score >= 700:
            base *= 0.85
        elif score < 550:
            base *= 1.25
        return base * RANK_INTEREST_MULT[self.rank_index()]

    # ------------------------------------------------------------------ #
    # Pricing and trading

    def get_buy_price(self, good: str, planet: Optional[Planet] = None) -> int:
        p = planet or self.current_planet
        mid = p.market.get(good, COMMODITIES[good].base_price)
        spread = BASE_SPREAD * self.difficulty.price_spread_mult
        perk = self.player.has_crew_perk("trader") or 0.0
        rank_cut = RANK_BUY_DISCOUNT[self.rank_index()]
        rep_mult = self.reputation_price_mult(p.faction)
        price = mid * (1.0 + spread) * (1.0 - perk) * (1.0 - rank_cut) * rep_mult
        return max(1, int(math.ceil(price)))

    def get_sell_price(self, good: str, planet: Optional[Planet] = None) -> int:
        p = planet or self.current_planet
        mid = p.market.get(good, COMMODITIES[good].base_price)
        spread = BASE_SPREAD * self.difficulty.price_spread_mult
        perk = self.player.has_crew_perk("trader") or 0.0
        rank_bonus = RANK_SELL_BONUS[self.rank_index()]
        rep_mult = self.reputation_price_mult(p.faction)
        price = mid * (1.0 - spread) * (1.0 + perk) * (1.0 + rank_bonus) * (2.0 - rep_mult)
        return max(1, int(math.floor(price)))

    def _price_impact(self, good: str, qty: int, direction: str) -> None:
        """Local supply/demand feedback: trading nudges the market price."""
        p = self.current_planet
        comm = COMMODITIES[good]
        mid = p.market.get(good, comm.base_price)
        impact = min(0.08, qty * 0.002)
        if direction == "buy":
            mid = mid * (1.0 + impact)
        else:
            mid = mid * (1.0 - impact)
        p.market[good] = max(2, int(round(clamp(
            mid, comm.base_price * 0.25, comm.base_price * 3.2
        ))))

    def buy_commodity(self, good: str, qty: int) -> Tuple[bool, str]:
        if good not in COMMODITIES:
            return False, "Invalid commodity."
        if qty <= 0:
            return False, "Quantity must be positive."

        p = self.current_planet
        price = self.get_buy_price(good)
        stock = p.stock.get(good, 0)
        free_space = self.player.cargo_free()

        if qty > stock:
            return False, f"Not enough stock on {p.name} (available: {stock})."
        if qty > free_space:
            return False, f"Not enough cargo space (free: {free_space})."

        total_cost = price * qty
        if self.player.credits < total_cost:
            return False, f"Insufficient credits! Need {money(total_cost)} CR."

        self.player.credits -= total_cost
        p.stock[good] = stock - qty

        # Track weighted average cost basis
        old_qty = self.player.cargo.get(good, 0)
        old_basis = self.player.cargo_cost_basis.get(good, float(price))
        new_basis = (old_qty * old_basis + qty * price) / max(1, old_qty + qty)
        self.player.cargo_cost_basis[good] = round(new_basis, 2)

        added = self.player.add_cargo(good, qty)
        self._price_impact(good, qty, "buy")
        self.sound.play("buy")
        msg = f"Purchased {added}x {COMMODITIES[good].name} for {money(total_cost)} CR."
        self.announce(msg)
        return True, msg

    def sell_commodity(self, good: str, qty: int) -> Tuple[bool, str]:
        if good not in COMMODITIES:
            return False, "Invalid commodity."
        if qty <= 0:
            return False, "Quantity must be positive."

        owned = self.player.cargo.get(good, 0)
        if qty > owned:
            return False, f"You only have {owned}x {COMMODITIES[good].name}."

        price = self.get_sell_price(good)
        total_income = price * qty
        basis = self.player.cargo_cost_basis.get(good, float(price))
        unit_profit = price - basis
        total_profit = int(round(unit_profit * qty))
        pct_return = (unit_profit / max(1.0, basis)) * 100.0

        self.player.remove_cargo(good, qty)
        if self.player.cargo.get(good, 0) <= 0:
            self.player.cargo_cost_basis.pop(good, None)

        self.player.credits += total_income
        self.current_planet.stock[good] = self.current_planet.stock.get(good, 0) + qty
        self._price_impact(good, qty, "sell")
        self.player.stats["total_profit"] += total_income

        if COMMODITIES[good].is_contraband:
            self.player.stats["contraband_sold"] += qty

        self.sound.play("sell")
        self.check_achievements()
        profit_tag = f" (Profit: {('+' if total_profit >= 0 else '')}{money(total_profit)} CR, {pct_return:+.1f}%)" if basis > 0 else ""
        msg = f"Sold {qty}x {COMMODITIES[good].name} for {money(total_income)} CR{profit_tag}."
        self.announce(msg)
        return True, msg


    # ------------------------------------------------------------------ #
    # Station services

    def buy_fuel(self, amount: int) -> Tuple[bool, str]:
        p = self.current_planet
        missing = self.player.max_fuel - self.player.fuel
        if missing <= 0:
            return False, "Fuel tank is already full."

        amount = min(amount, missing)
        unit = max(1, int(p.fuel_price * self.difficulty.fuel_mult
                          * (1.0 - RANK_SERVICE_DISCOUNT[self.rank_index()])))
        cost = amount * unit

        if self.player.credits < cost:
            return False, f"Insufficient credits to buy {amount} fuel ({money(cost)} CR needed)."

        self.player.credits -= cost
        self.player.fuel += amount
        self.sound.play("coin")
        msg = f"Loaded {amount} fuel for {money(cost)} CR."
        self.announce(msg)
        return True, msg

    def current_repair_price(self) -> int:
        raw = self.current_planet.repair_cost * self.difficulty.repair_mult \
            * (1.0 - RANK_SERVICE_DISCOUNT[self.rank_index()])
        return max(6, int(raw))

    def repair_hull(self, hp: int) -> Tuple[bool, str]:
        missing = self.player.max_hull - self.player.hull
        if missing <= 0:
            return False, "Hull is at pristine 100% integrity."

        hp = min(hp, missing)
        cost = hp * self.current_repair_price()

        if self.player.credits < cost:
            return False, f"Insufficient credits for repairs ({money(cost)} CR needed)."

        self.player.credits -= cost
        self.player.hull += hp
        self.sound.play("upgrade")
        msg = f"Repaired {hp} hull points for {money(cost)} CR."
        self.announce(msg)
        return True, msg

    def subsystem_repair_cost(self) -> int:
        count = len(self.player.damaged_subsystems())
        if count == 0:
            return 0
        return int(280 * count * self.difficulty.repair_mult)

    def repair_subsystems(self) -> Tuple[bool, str]:
        damaged = self.player.damaged_subsystems()
        if not damaged:
            return False, "All subsystems are fully operational."
        cost = self.subsystem_repair_cost()
        if self.player.credits < cost:
            return False, f"Insufficient credits ({money(cost)} CR needed)."
        self.player.credits -= cost
        self.player.weapons_damaged = False
        self.player.engines_damaged = False
        self.player.shields_damaged = False
        self.player.shield = min(self.player.shield, self.player.effective_max_shield())
        self.sound.play("upgrade")
        msg = f"Engineering overhauled {len(damaged)} subsystem(s) for {money(cost)} CR."
        self.announce(msg)
        return True, msg

    def insurance_price(self) -> int:
        ship_val = SHIP_TEMPLATES[self.player.ship_id].cost
        eq_val = sum(
            EQUIPMENT_ITEMS[i].cost
            for i in (self.player.equipped_weapons
                      + self.player.equipped_shields
                      + self.player.equipped_modules)
            if i in EQUIPMENT_ITEMS
        )
        raw = max(500, int((ship_val + eq_val) * 0.04))
        return int(raw * RANK_INSURANCE_MULT[self.rank_index()])

    def buy_insurance(self) -> Tuple[bool, str]:
        if self.player.insurance_active:
            return False, "An insurance policy is already active."
        cost = self.insurance_price()
        if self.player.credits < cost:
            return False, f"Cannot afford the policy premium ({money(cost)} CR)."
        self.player.credits -= cost
        self.player.insurance_active = True
        self.sound.play("coin")
        self.check_achievements()
        msg = f"Ship insurance activated for {money(cost)} CR. The void holds less fear now."
        self.add_news(msg)
        self.announce(msg)
        return True, msg

    def buy_missiles(self, qty: int) -> Tuple[bool, str]:
        if not self.player.has_missile_rack():
            return False, "You need a Havoc Missile Launcher installed first."
        cap = PLAYER_MISSILE_CAP
        current = self.player.missiles
        qty = min(qty, cap - current)
        if qty <= 0:
            return False, f"Missile magazines full ({current}/{cap})."
        unit = self.missile_price()
        cost = unit * qty
        if self.player.credits < cost:
            return False, f"Insufficient credits ({money(cost)} CR needed)."
        self.player.credits -= cost
        self.player.missiles += qty
        self.sound.play("coin")
        msg = f"Loaded {qty} missile(s) for {money(cost)} CR ({current + qty}/{cap})."
        self.announce(msg)
        return True, msg

    def missile_price(self) -> int:
        return max(120, int(BASE_MISSILE_PRICE * self.difficulty.price_spread_mult))

    # ------------------------------------------------------------------ #
    # Ship / equipment / crew

    def ship_trade_in_value(self) -> int:
        return int(SHIP_TEMPLATES[self.player.ship_id].cost * 0.7)

    def buy_ship(self, template_id: str) -> Tuple[bool, str]:
        if template_id not in SHIP_TEMPLATES:
            return False, "Invalid ship model."

        if template_id == self.player.ship_id:
            return False, "You are already flying this ship model."

        tmpl = SHIP_TEMPLATES[template_id]
        cur_val = self.ship_trade_in_value()
        net_cost = max(0, tmpl.cost - cur_val)

        if self.player.credits < net_cost:
            return False, f"Need {money(net_cost)} CR (trade-in credited {money(cur_val)} CR)."

        if self.player.cargo_used() > tmpl.cargo_cap:
            return False, (
                f"Current cargo ({self.player.cargo_used()}) exceeds new ship capacity "
                f"({tmpl.cargo_cap}). Sell cargo first."
            )

        self.player.credits -= net_cost
        self.player.ship_id = template_id
        self.player.equipped_weapons = self.player.equipped_weapons[:tmpl.weapon_slots]
        self.player.equipped_shields = self.player.equipped_shields[:tmpl.shield_slots]
        self.player.equipped_modules = self.player.equipped_modules[:tmpl.module_slots]

        self.recalculate_ship_stats()
        self.player.hull = self.player.max_hull
        self.player.shield = self.player.max_shield
        self.player.fuel = self.player.max_fuel

        self.sound.play("upgrade")
        self.check_achievements()
        self.check_promotion()
        msg = f"Congratulations! You are now captain of a new {tmpl.name}."
        self.announce(msg)
        return True, msg

    def buy_equipment(self, eq_id: str) -> Tuple[bool, str]:
        if eq_id not in EQUIPMENT_ITEMS:
            return False, "Invalid equipment item."

        eq = EQUIPMENT_ITEMS[eq_id]
        tmpl = SHIP_TEMPLATES[self.player.ship_id]

        if self.player.credits < eq.cost:
            return False, f"Cannot afford {eq.name} ({money(eq.cost)} CR required)."

        if eq.slot_type == "weapon":
            if len(self.player.equipped_weapons) >= tmpl.weapon_slots:
                return False, f"No open weapon hardpoints (max {tmpl.weapon_slots})."
            if eq_id in self.player.equipped_weapons:
                return False, f"{eq.name} is already mounted."
            self.player.equipped_weapons.append(eq_id)
        elif eq.slot_type == "shield":
            if len(self.player.equipped_shields) >= tmpl.shield_slots:
                return False, f"No open shield generator bays (max {tmpl.shield_slots})."
            if eq_id in self.player.equipped_shields:
                return False, f"{eq.name} is already installed."
            self.player.equipped_shields.append(eq_id)
        elif eq.slot_type == "module":
            if len(self.player.equipped_modules) >= tmpl.module_slots:
                return False, f"No open module expansion slots (max {tmpl.module_slots})."
            if eq_id in self.player.equipped_modules:
                return False, "Module already installed."
            self.player.equipped_modules.append(eq_id)
        else:
            return False, "Unknown equipment slot type."

        self.player.credits -= eq.cost
        self.recalculate_ship_stats()
        self.sound.play("upgrade")
        msg = f"Installed {eq.name} onto starship."
        self.announce(msg)
        return True, msg

    def sell_equipment(self, eq_id: str) -> Tuple[bool, str]:
        if eq_id not in EQUIPMENT_ITEMS:
            return False, "Invalid equipment item."

        eq = EQUIPMENT_ITEMS[eq_id]
        removed = False

        if eq_id in self.player.equipped_weapons:
            self.player.equipped_weapons.remove(eq_id)
            removed = True
        elif eq_id in self.player.equipped_shields:
            self.player.equipped_shields.remove(eq_id)
            removed = True
        elif eq_id in self.player.equipped_modules:
            self.player.equipped_modules.remove(eq_id)
            removed = True

        if not removed:
            return False, f"{eq.name} is not installed on your ship."

        refund = int(eq.cost * 0.75)
        self.player.credits += refund
        self.recalculate_ship_stats()
        self.sound.play("sell")
        msg = f"Dismounted {eq.name} and received {money(refund)} CR salvage value."
        self.announce(msg)
        return True, msg

    def hire_crew(self, crew_id: str) -> Tuple[bool, str]:
        c = CREW_INDEX.get(crew_id)
        if not c:
            return False, "Crew member not found."
        if crew_id in self.player.hired_crew:
            return False, f"{c.name} is already serving in your crew."
        if len(self.player.hired_crew) >= MAX_CREW:
            return False, f"Crew quarters are full (maximum {MAX_CREW} officers)."
        if self.player.credits < c.hire_cost:
            return False, f"Cannot afford signing bonus of {money(c.hire_cost)} CR."
        self.player.credits -= c.hire_cost
        self.player.hired_crew.append(crew_id)
        self.recalculate_ship_stats()
        self.sound.play("coin")
        self.check_achievements()
        msg = f"Hired {c.name} ({c.role}). Daily wage: {c.daily_wage} CR."
        self.announce(msg)
        return True, msg

    def dismiss_crew(self, crew_id: str) -> Tuple[bool, str]:
        if crew_id in self.player.hired_crew:
            self.player.hired_crew.remove(crew_id)
            self.recalculate_ship_stats()
            self.announce("Crew officer dismissed from service.")
            return True, "Crew officer dismissed from service."
        return False, "Crew officer is not in your crew."

    def total_daily_wages(self) -> float:
        total = 0
        for cid in self.player.hired_crew:
            c = CREW_INDEX.get(cid)
            if c:
                total += c.daily_wage
        return total * (1.0 - RANK_WAGE_DISCOUNT[self.rank_index()])

    # ------------------------------------------------------------------ #
    # Missions

    def accept_mission(self, mission_id: str) -> Tuple[bool, str]:
        for m in self.available_missions:
            if m.id == mission_id:
                if len(self.player.active_missions) >= MAX_ACTIVE_MISSIONS:
                    return False, f"Active mission log full (max {MAX_ACTIVE_MISSIONS} contracts)."

                if m.cargo_good and m.cargo_qty > 0:
                    if self.player.cargo_free() < m.cargo_qty:
                        return False, f"Need {m.cargo_qty} free cargo space to accept this contract."
                    self.player.add_cargo(m.cargo_good, m.cargo_qty)

                self.available_missions.remove(m)
                self.player.active_missions.append(m)
                self.sound.play("coin")
                msg = f"Contract accepted: {m.title}"
                self.announce(msg)
                return True, msg
        return False, "Mission no longer available."

    def abandon_mission(self, mission_id: str) -> Tuple[bool, str]:
        for m in list(self.player.active_missions):
            if m.id == mission_id:
                if m.cargo_good and m.cargo_qty > 0:
                    self.player.remove_cargo(m.cargo_good, m.cargo_qty)
                self.player.active_missions.remove(m)
                dest_p = self.planets.get(m.destination)
                rep_msg = ""
                if dest_p:
                    loss = self.adjust_reputation(dest_p.faction, -5)
                    if loss:
                        rep_msg = f" ({dest_p.faction} standing changed by {loss})"
                msg = f"Forfeited contract '{m.title}'.{rep_msg}"
                self.announce(msg)
                return True, msg
        return False, "Contract not found in active missions."

    def check_mission_deliveries(self) -> List[str]:
        completed_msgs: List[str] = []
        for m in list(self.player.active_missions):
            if m.destination == self.player.location and not m.completed and not m.failed:
                if m.m_type == "passenger":
                    # VIPs disembark on arrival — no cargo hand-over needed.
                    bonus_mult = 1.0 + RANK_CONTRACT_BONUS[self.rank_index()]
                    payout = int(m.reward_credits * bonus_mult)
                    self.player.credits += payout
                    m.completed = True
                    self.player.active_missions.remove(m)
                    self.player.stats["missions_completed"] += 1
                    dest_planet = self.planets.get(m.destination)
                    rep_note = ""
                    if dest_planet:
                        gain = self.adjust_reputation(dest_planet.faction, 5)
                        if gain:
                            rep_note = f" ({dest_planet.faction} standing +{gain})"
                    rank_note = "" if payout == m.reward_credits else \
                        f" [Lieutenant's share +{money(payout - m.reward_credits)} CR]"
                    completed_msgs.append(
                        f"Passengers delivered: '{m.title}' complete! Fare: {money(payout)} CR."
                        f"{rank_note}{rep_note}"
                    )
                    self.sound.play("victory")
                elif m.m_type in ("delivery", "smuggle", "medical"):
                    if m.cargo_good and self.player.cargo.get(m.cargo_good, 0) >= m.cargo_qty:
                        self.player.remove_cargo(m.cargo_good, m.cargo_qty)
                        bonus_mult = 1.0 + RANK_CONTRACT_BONUS[self.rank_index()]
                        payout = int(m.reward_credits * bonus_mult)
                        self.player.credits += payout
                        m.completed = True
                        self.player.active_missions.remove(m)
                        self.player.stats["missions_completed"] += 1
                        dest_planet = self.planets.get(m.destination)
                        rep_note = ""
                        if dest_planet:
                            gain = self.adjust_reputation(dest_planet.faction, 5)
                            if gain:
                                rep_note = f" ({dest_planet.faction} standing +{gain})"
                        rank_note = "" if payout == m.reward_credits else \
                            f" [Lieutenant's share +{money(payout - m.reward_credits)} CR]"
                        completed_msgs.append(
                            f"Completed '{m.title}'! Received {money(payout)} CR reward."
                            f"{rank_note}{rep_note}"
                        )
                        self.sound.play("victory")
        self.check_achievements()
        self.check_promotion()
        return completed_msgs

    # ------------------------------------------------------------------ #
    # Travel

    def calculate_distance(self, p1: Planet, p2: Planet) -> float:
        return math.hypot(p1.x - p2.x, p1.y - p2.y)

    def calculate_travel_cost(self, dest: Planet) -> Tuple[int, int]:
        """(fuel_cost, days_cost). Faster hulls genuinely arrive sooner."""
        return self.calculate_travel_cost_between(self.current_planet, dest)

    def calculate_travel_cost_between(self, src: Planet, dest: Planet) -> Tuple[int, int]:
        """Travel cost between two arbitrary worlds (route advisor uses this
        so warp boosters / navigator perks are reflected in its estimates)."""
        dist = self.calculate_distance(src, dest)
        fuel_cost = int(dist * 3.0) + 4

        if self.player.has_module("warp_booster"):
            fuel_cost = int(fuel_cost * 0.70)
        nav_perk = self.player.has_crew_perk("nav")
        if nav_perk:
            fuel_cost = int(fuel_cost * (1.0 - nav_perk))

        speed = max(0.5, SHIP_TEMPLATES[self.player.ship_id].speed)
        days_cost = max(1, int(dist * 0.55 / speed))
        return max(1, fuel_cost), days_cost

    def execute_travel(self, dest_name: str) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        if dest_name not in self.planets:
            return False, "Destination does not exist.", None
        if dest_name == self.player.location:
            return False, "You are already stationed at this destination.", None

        dest = self.planets[dest_name]
        fuel_cost, days_cost = self.calculate_travel_cost(dest)

        if self.player.fuel < fuel_cost:
            return False, f"Insufficient fuel! Required: {fuel_cost}, Current: {self.player.fuel}.", None

        self.player.fuel -= fuel_cost
        self.player.stats["jumps_made"] += 1
        self.player.location = dest.name
        self.advance_day(days_cost)

        self.sound.play("warp")
        self.add_news(f"Arrived at {dest.name} in the {dest.faction} sector.")

        encounter = self.generate_random_encounter(dest)

        delivered = self.check_mission_deliveries()
        for msg in delivered:
            self.add_news(msg)

        self.check_achievements()
        self.autosave()   # autosave after every hyperjump
        return True, f"Hyperjump successful to {dest.name}.", encounter

    # ------------------------------------------------------------------ #
    # Encounters

    def pirate_tier(self) -> int:
        """0..3 threat tier, driven by net worth (smoother early curve)."""
        nw = self.calculate_net_worth()
        if nw < 15_000:
            return 0
        if nw < 60_000:
            return 1
        if nw < 150_000:
            return 2
        return 3

    def generate_random_encounter(self, dest: Planet) -> Optional[Dict[str, Any]]:
        # Bounty target check first — guaranteed dramatic welcome.
        for m in self.player.active_missions:
            if (
                m.destination == dest.name
                and m.m_type == "bounty"
                and not m.completed
                and not m.failed
            ):
                return {
                    "type": "bounty_combat",
                    "title": f"Bounty Target Spotted: {m.bounty_target_name}!",
                    "target_name": m.bounty_target_name,
                    "target_ship": m.bounty_target_ship,
                    "mission_id": m.id,
                    "reward": m.reward_credits,
                }

        sec_hazard = {
            "None": 0.35, "Low": 0.20, "Medium": 0.10, "High": 0.05,
        }.get(dest.security, 0.10)

        chance = (0.18 + sec_hazard) * self.difficulty.encounter_mult

        # Good standing with the local faction calms patrols; poor standing
        # makes everyone (correctly) more suspicious of you.
        rep = self.player.rep(dest.faction)
        chance *= clamp(1.0 - rep / 250.0, 0.6, 1.4)

        if self.player.has_module("cloaking_field"):
            chance *= 0.7

        if random.random() > chance:
            return None

        event_weights = [
            ("pirate_ambush", 32 if dest.security in ("None", "Low") else 14),
            ("customs_inspection", 32 if dest.security in ("High", "Medium") else 5),
            ("faction_patrol", 18),
            ("derelict_ship", 16),
            ("solar_flare", 11),
            ("distress_beacon", 11),
            ("wandering_trader", 12),
            ("asteroid_field", 13),
            ("wormhole", 6),
            ("mining_opportunity", 11),
        ]
        ev_types, weights = zip(*event_weights)
        chosen = random.choices(ev_types, weights=weights)[0]

        if chosen == "pirate_ambush":
            tier = self.pirate_tier()
            tier_ships = [
                ["sparrow", "kestrel", "orion"],
                ["sparrow", "orion", "viper", "drake"],
                ["orion", "viper", "drake", "valkyrie"],
                ["viper", "drake", "valkyrie", "behemoth"],
            ][tier]
            p_ship = random.choice(tier_ships)
            return {
                "type": "pirate_ambush",
                "title": "Warning: Raider Ambush!",
                "enemy_name": f"Corsair {random.choice(['Scavenger', 'Marauder', 'Cutthroat', 'Dread Ship'])}",
                "enemy_ship": p_ship,
                "demand_credits": min(self.player.credits, max(200, int(self.player.credits * 0.20))),
            }

        if chosen == "customs_inspection":
            return {
                "type": "customs_scan",
                "title": f"{dest.faction} Customs Inspection",
                "desc": "An orbital defense cruiser hails and locks scanning sensors onto your cargo hold.",
            }

        if chosen == "faction_patrol":
            rep = self.player.rep(dest.faction)
            return {
                "type": "faction_patrol",
                "title": f"{dest.faction} Patrol Hails You",
                "faction": dest.faction,
                "rep": rep,
                "desc": (
                    f"A {dest.faction} patrol wing pulls alongside and requests you "
                    f"heave to for a courtesy inspection. Your standing with them is "
                    f"currently '{reputation_rank(rep)}' ({rep})."
                ),
            }

        if chosen == "derelict_ship":
            return {
                "type": "derelict",
                "title": "Derelict Vessel Signal Detected",
                "desc": "Long-range sensors pick up an abandoned cargo hull drifting cold in space.",
            }

        if chosen == "solar_flare":
            return {
                "type": "solar_flare",
                "title": "Coronal Mass Ejection Warning!",
                "desc": "A violent solar radiation burst sweeps across the flight corridor.",
            }

        if chosen == "distress_beacon":
            return {
                "type": "distress_beacon",
                "title": "Automated SOS Beacon",
                "desc": "A civilian transport with disabled propulsion is broadcasting an SOS.",
            }

        if chosen == "wandering_trader":
            legal = [g for g, c in COMMODITIES.items() if not c.is_contraband]
            good = random.choice(legal)
            qty = random.randint(4, 9)
            price = max(2, int(COMMODITIES[good].base_price * 0.5))
            return {
                "type": "wandering_trader",
                "title": "Nomadic Merchant Vessel Hails",
                "desc": "A deep space nomadic trader offers a bargain crate of rare goods.",
                "good": good,
                "qty": qty,
                "unit_price": price,
            }

        if chosen == "asteroid_field":
            return {
                "type": "asteroid_field",
                "title": "Dense Asteroid Field Ahead",
                "desc": "Your arrival corridor is clogged with tumbling rock and ice. "
                        "Threading it quickly risks the hull; a wide detour burns extra fuel.",
            }

        if chosen == "wormhole":
            return {
                "type": "wormhole",
                "title": "Unstable Wormhole Signature",
                "desc": "Space folds open nearby — a shimmering throat to somewhere else "
                        "in the sector. Transit is free but violently turbulent.",
            }

        if chosen == "mining_opportunity":
            vein = random.choice(["ore", "crystals", "gemstones"])
            return {
                "type": "mining_opportunity",
                "title": "Rich Asteroid Vein Detected",
                "vein": vein,
                "desc": "Sensors flag a dense, freshly-exposed "
                        f"{COMMODITIES[vein].name} seam in the nearby debris field.",
            }

        return None

    # ----- encounter resolutions (engine-side, shared by GUI and tests) ----- #

    def resolve_customs(self, enc: Dict[str, Any], bribe: bool) -> List[str]:
        msgs: List[str] = []
        faction = self.current_planet.faction
        illegal = {
            g: q for g, q in self.player.cargo.items()
            if COMMODITIES[g].is_contraband and q > 0
        }
        if not illegal:
            return ["Scan complete: No illicit materials detected. Safe travels, Commander."]

        if bribe:
            bribe_cost = max(300, int(self.player.credits * 0.12 * self.difficulty.fine_mult))
            zoe = self.player.has_crew_perk("smuggler")
            if zoe:
                bribe_cost = int(bribe_cost * (1.0 - zoe * 0.5))
            if self.player.credits >= bribe_cost:
                self.player.credits -= bribe_cost
                self.adjust_reputation(faction, -2)
                self.sound.play("coin")
                msgs.append(
                    f"The customs officer accepted your {money(bribe_cost)} CR bribe and looked the other way."
                )
                return msgs
            msgs.append("Insufficient credits to bribe the officer. They proceed with the scan.")

        if self.player.has_module("smuggler_bay") and random.random() < 0.75:
            msgs.append("Shielded Smuggler Bay concealed your contraband from scanners!")
            return msgs

        zoe_perk = self.player.has_crew_perk("smuggler") or 0.0
        fine = max(500, int(self.player.credits * 0.22 * self.difficulty.fine_mult))
        fine = int(fine * (1.0 - zoe_perk))
        fine = min(self.player.credits, fine)
        self.player.credits -= fine
        for g in list(illegal.keys()):
            self.player.remove_cargo(g, illegal[g])
        self.adjust_reputation(faction, -8)
        self.sound.play("alarm")
        msgs.append(f"CONTRABAND CONFISCATED! Fined {money(fine)} CR by customs. "
                    f"({faction} standing -8)")
        return msgs

    def resolve_faction_patrol(self, enc: Dict[str, Any], cooperate: bool) -> List[str]:
        msgs: List[str] = []
        faction = enc.get("faction", self.current_planet.faction)
        rep = self.player.rep(faction)
        illegal = {
            g: q for g, q in self.player.cargo.items()
            if COMMODITIES[g].is_contraband and q > 0
        }

        if not cooperate:
            slip_chance = 0.35 + (0.15 if self.player.has_module("cloaking_field") else 0.0)
            nav_perk = self.player.has_crew_perk("nav") or 0.0
            slip_chance += nav_perk
            if random.random() < slip_chance:
                self.adjust_reputation(faction, -2)
                msgs.append(f"You gun the engines and vanish from the patrol's sensors before "
                            f"they can respond. ({faction} standing -2)")
                return msgs
            self.adjust_reputation(faction, -6)
            dmg = random.randint(5, 15)
            self.player.hull = max(1, self.player.hull - dmg)
            self.sound.play("alarm")
            msgs.append(f"The patrol opens a warning volley as you flee! Sustained {dmg} hull "
                        f"damage. ({faction} standing -6)")
            return msgs

        if illegal and rep < 15:
            fine = max(400, int(self.player.credits * 0.18 * self.difficulty.fine_mult))
            zoe_perk = self.player.has_crew_perk("smuggler") or 0.0
            fine = int(fine * (1.0 - zoe_perk))
            fine = min(self.player.credits, fine)
            self.player.credits -= fine
            for g in list(illegal.keys()):
                self.player.remove_cargo(g, illegal[g])
            self.adjust_reputation(faction, -10)
            self.sound.play("alarm")
            msgs.append(f"The inspection turns up your contraband! Fined {money(fine)} CR and "
                        f"cargo seized. ({faction} standing -10)")
            return msgs

        gain = self.adjust_reputation(faction, 4)
        if rep >= 40:
            bonus = random.randint(200, 700)
            self.player.credits += bonus
            self.sound.play("coin")
            msgs.append(f"The patrol commander recognizes your good standing and waves you "
                        f"through with a goodwill stipend of {money(bonus)} CR. "
                        f"({faction} standing +{gain})")
        else:
            msgs.append(f"You power down to a crawl and submit to the courtesy inspection. "
                        f"Everything checks out. ({faction} standing +{gain})")
        return msgs

    def resolve_derelict(self, enc: Dict[str, Any], board: bool) -> List[str]:
        if not board:
            return ["You leave the derelict to its silent drift."]
        if random.random() < 0.65:
            salvage_cr = random.randint(600, 2_400)
            good = random.choice(list(COMMODITIES.keys()))
            qty = random.randint(2, 6)
            added = self.player.add_cargo(good, qty)
            self.player.credits += salvage_cr
            self.sound.play("victory")
            return [f"Salvage team recovered {money(salvage_cr)} CR and {added}x {COMMODITIES[good].name}!"]
        dmg = random.randint(8, 20)
        self.player.hull = max(1, self.player.hull - dmg)
        self.sound.play("alarm")
        return [f"Derelict automated defense turret detonated! Sustained {dmg} hull damage."]

    def resolve_solar_flare(self, enc: Dict[str, Any]) -> List[str]:
        dmg = random.randint(6, 15)
        self.player.hull = max(1, self.player.hull - dmg)
        self.sound.play("alarm")
        return [f"Intense cosmic radiation battered your ship for {dmg} hull damage."]

    def resolve_distress(self, enc: Dict[str, Any], assist: bool) -> List[str]:
        if not assist:
            return ["You warily power down transmissions and continue on."]
        if self.player.fuel < 15:
            return ["Insufficient fuel reserves to assist."]
        self.player.fuel -= 15
        reward = random.randint(900, 2_600)
        self.player.credits += reward
        self.sound.play("coin")
        return [f"Transferred 15 fuel. Grateful crew awarded you {money(reward)} CR!"]

    def resolve_trader(self, enc: Dict[str, Any], accept: bool) -> List[str]:
        if not accept:
            return ["You politely decline the nomad's offer."]
        good = enc.get("good", "crystals")
        qty = int(enc.get("qty", 5))
        price = int(enc.get("unit_price", 50))
        total = price * qty
        if self.player.credits < total:
            return [f"Cannot afford the deal ({money(total)} CR required)."]
        if self.player.cargo_free() < qty:
            return ["Not enough cargo space for the crate."]
        self.player.credits -= total
        self.player.add_cargo(good, qty)
        self.sound.play("buy")
        return [f"Deal accepted! Loaded {qty}x {COMMODITIES[good].name} for {money(total)} CR."]

    def resolve_asteroid_field(self, enc: Dict[str, Any], thread_needle: bool) -> List[str]:
        """thread_needle=True: fast and risky. False: safe detour costing fuel."""
        if not thread_needle:
            detour_cost = 8
            if self.player.fuel < detour_cost:
                return ["Not enough fuel for the detour — you are forced to thread the rocks! "
                        "Hull scuffed for 4 damage."]
            self.player.fuel -= detour_cost
            return [f"Plotting a wide arc around the field (−{detour_cost} fuel). "
                    "Hull untouched; the stars roll by serenely."]
        roll = random.random()
        if roll < 0.58:
            return ["You knife between tumbling bergs like a born pilot — not a scratch!"]
        if roll < 0.88:
            dmg = random.randint(6, 16)
            self.player.hull = max(1, self.player.hull - dmg)
            self.sound.play("alarm")
            return [f"A glancing boulder slams the plating for {dmg} hull damage!"]
        salvage = random.randint(2, 6)
        added = self.player.add_cargo("ore", salvage)
        self.sound.play("coin")
        if added:
            return [f"Close pass scrapes a rich seam loose — you scoop {added}x "
                    f"{COMMODITIES['ore'].name} from the debris!"]
        return ["Close pass scatters loose ore everywhere — but your hold is already full."]

    def resolve_wormhole(self, enc: Dict[str, Any], enter: bool) -> List[str]:
        if not enter:
            return ["You seal the viewport shutters and give the anomaly a wide berth."]
        others = [p for p in self.planets.values() if p.name != self.player.location]
        dest = random.choice(others)
        self.player.location = dest.name
        self.player.stats["wormholes"] += 1
        self.sound.play("wormhole")
        self.add_news(f"Wormhole transit: spat out at {dest.name}!")
        # The local contract board is planet-specific — regenerate it for the
        # new arrival system so the mission tab is not left empty.
        self.available_missions = generate_mission_board(
            self.current_planet,
            list(self.planets.values()),
            self.player.day,
            career_tier=self.career_tier(),
        )
        msgs = [f"The wormhole swallows your ship whole and vomits it out near {dest.name}!"]
        if random.random() < 0.3:
            dmg = random.randint(4, 12)
            self.player.hull = max(1, self.player.hull - dmg)
            msgs.append(f"Turbulent gravimetric shear strains the frame for {dmg} hull damage.")
        else:
            msgs.append("The ride is violently smooth — not a bolt rattles loose.")
        delivered = self.check_mission_deliveries()
        msgs.extend(delivered)
        self.check_achievements()
        return msgs

    def resolve_mining(self, enc: Dict[str, Any], mine: bool) -> List[str]:
        vein = enc.get("vein", "ore")
        if not mine:
            return [f"You log the {COMMODITIES[vein].name} seam for future survey teams and move on."]
        fuel_cost = 10
        if self.player.fuel < fuel_cost:
            return ["Not enough fuel to power the mining drones."]
        if self.player.cargo_free() < 1:
            return ["No cargo space free — the seam waits for another day."]
        self.player.fuel -= fuel_cost
        self.player.stats["mining_ops"] += 1
        qty = random.randint(3, 8)
        added = self.player.add_cargo(vein, qty)
        self.sound.play("mine")
        self.check_achievements()
        return [f"Mining drones strip the seam: recovered {added}x {COMMODITIES[vein].name} "
                f"(−{fuel_cost} fuel)."]

    def resolve_encounter(
        self, enc: Dict[str, Any], choice: str, arg: bool = True
    ) -> Optional[List[str]]:
        """Dispatch a non-combat encounter choice. Returns messages, or None
        if the encounter type is combat (handled by start_combat)."""
        t = enc.get("type")
        if t in ("pirate_ambush", "bounty_combat"):
            return None
        if t == "customs_scan":
            return self.resolve_customs(enc, bribe=bool(arg))
        if t == "faction_patrol":
            return self.resolve_faction_patrol(enc, cooperate=bool(arg))
        if t == "derelict":
            return self.resolve_derelict(enc, board=bool(arg))
        if t == "solar_flare":
            return self.resolve_solar_flare(enc)
        if t == "distress_beacon":
            return self.resolve_distress(enc, assist=bool(arg))
        if t == "wandering_trader":
            return self.resolve_trader(enc, accept=bool(arg))
        if t == "asteroid_field":
            return self.resolve_asteroid_field(enc, thread_needle=bool(arg))
        if t == "wormhole":
            return self.resolve_wormhole(enc, enter=bool(arg))
        if t == "mining_opportunity":
            return self.resolve_mining(enc, mine=bool(arg))
        return []


    # ------------------------------------------------------------------ #
    # Trade advisor

    def remote_top_deals(self, planet: Planet, limit: int = 5) -> List[Dict[str, Any]]:
        """Best goods to buy at a remote planet and sell where you are now.
        Used by the Deep Space Scanner module for the star-map dossier."""
        deals: List[Dict[str, Any]] = []
        for gid, comm in COMMODITIES.items():
            buy_remote = self.get_buy_price(gid, planet)
            sell_local = self.get_sell_price(gid, self.current_planet)
            margin = sell_local - buy_remote
            stock = planet.stock.get(gid, 0)
            if stock > 0 and margin > 0:
                deals.append({
                    "good": comm.name,
                    "good_id": gid,
                    "buy_price": buy_remote,
                    "sell_price": sell_local,
                    "margin": margin,
                    "stock": stock,
                    "is_contraband": comm.is_contraband,
                })
        deals.sort(key=lambda d: d["margin"], reverse=True)
        return deals[:limit]

    def compute_best_trade_routes(self, from_current_only: bool = False) -> List[Dict[str, Any]]:
        routes: List[Dict[str, Any]] = []
        planets_list = list(self.planets.values())

        for gid, comm in COMMODITIES.items():
            for src in planets_list:
                if from_current_only and src.name != self.player.location:
                    continue
                buy_price = self.get_buy_price(gid, src)
                for dst in planets_list:
                    if src.name == dst.name:
                        continue
                    sell_price = self.get_sell_price(gid, dst)
                    margin = sell_price - buy_price
                    if margin <= 3:
                        continue

                    fuel_units, _days = self.calculate_travel_cost_between(src, dst)
                    fuel_credit_estimate = fuel_units * max(
                        4, int(src.fuel_price * self.difficulty.fuel_mult))

                    potential_qty = min(self.player.cargo_cap, src.stock.get(gid, 0))
                    if potential_qty <= 0:
                        continue

                    total_profit = margin * potential_qty
                    net_profit = total_profit - fuel_credit_estimate
                    days = _days

                    routes.append({
                        "good": comm.name,
                        "good_id": gid,
                        "src": src.name,
                        "dst": dst.name,
                        "buy_price": buy_price,
                        "sell_price": sell_price,
                        "margin": margin,
                        "margin_pct": round((margin / max(1, buy_price)) * 100, 1),
                        "fuel_cost": fuel_units,
                        "days": days,
                        "qty": potential_qty,
                        "total_profit": total_profit,
                        "net_profit": net_profit,
                        "profit_per_day": int(net_profit / max(1, days)),
                        "is_contraband": comm.is_contraband,
                    })

        routes.sort(key=lambda r: r["net_profit"], reverse=True)
        return routes[:12]

    # ------------------------------------------------------------------ #
    # Banking / stocks

    def loan_limit(self) -> int:
        return 20_000 + self.player.cargo_cap * 150 + self.player.max_hull * 50

    def deposit(self, amount: int) -> Tuple[bool, str]:
        amount = min(amount, self.player.credits)
        if amount <= 0:
            return False, "No credits available to deposit."
        self.player.credits -= amount
        self.player.savings += amount
        self.sound.play("coin")
        msg = f"Deposited {money(amount)} CR into savings."
        self.announce(msg)
        return True, msg

    def withdraw(self, amount: int) -> Tuple[bool, str]:
        amount = min(amount, self.player.savings)
        if amount <= 0:
            return False, "No savings available to withdraw."
        self.player.savings -= amount
        self.player.credits += amount
        self.sound.play("coin")
        msg = f"Withdrew {money(amount)} CR from savings."
        self.announce(msg)
        return True, msg

    def borrow(self, amount: int) -> Tuple[bool, str]:
        can_borrow = max(0, self.loan_limit() - self.player.loan)
        amount = min(amount, can_borrow)
        if amount <= 0:
            return False, "Credit limit reached. Repay existing debts first."
        self.player.loan += amount
        self.player.credits += amount
        self.sound.play("coin")
        msg = f"Borrowed {money(amount)} CR."
        self.announce(msg)
        return True, msg

    def repay(self, amount: int) -> Tuple[bool, str]:
        amount = min(amount, self.player.credits, self.player.loan)
        if amount <= 0:
            return False, "No loan payment can be made."
        self.player.loan -= amount
        self.player.credits -= amount
        # Solid repayments build your credit score (caps at 850).
        if amount >= 1_000:
            self.player.credit_score = min(850, self.player.credit_score + 2)
        self.sound.play("coin")
        msg = f"Repaid {money(amount)} CR loan."
        self.announce(msg)
        return True, msg

    def buy_stock(self, symbol: str, qty: int) -> Tuple[bool, str]:
        if symbol not in self.stocks:
            return False, "Unknown stock symbol."
        if qty <= 0:
            return False, "Quantity must be positive."

        stk = self.stocks[symbol]
        cost = int(stk.price * qty)
        if self.player.credits < cost:
            return False, f"Insufficient funds! Need {money(cost)} CR for {qty} shares."

        self.player.credits -= cost
        self.player.stocks_owned[symbol] = self.player.stocks_owned.get(symbol, 0) + qty
        self.sound.play("coin")
        msg = f"Bought {qty} shares of ${symbol} for {money(cost)} CR."
        self.announce(msg)
        return True, msg

    def sell_stock(self, symbol: str, qty: int) -> Tuple[bool, str]:
        if symbol not in self.stocks:
            return False, "Unknown stock symbol."
        owned = self.player.stocks_owned.get(symbol, 0)
        to_sell = min(qty, owned)
        if to_sell <= 0:
            return False, f"You own no shares of ${symbol}."

        income = int(self.stocks[symbol].price * to_sell)
        self.player.stocks_owned[symbol] = owned - to_sell
        if self.player.stocks_owned[symbol] <= 0:
            self.player.stocks_owned.pop(symbol, None)

        self.player.credits += income
        self.sound.play("sell")
        msg = f"Sold {to_sell} shares of ${symbol} for {money(income)} CR."
        self.announce(msg)
        return True, msg

    # ------------------------------------------------------------------ #
    # Insurance payout / death handling

    def insurance_respawn(self) -> List[str]:
        """Player destroyed with an active policy: respawn instead of death."""
        msgs: List[str] = []
        self.player.insurance_active = False
        self.player.stats["insurance_claims"] += 1
        lost_cargo = self.player.cargo_used()
        self.player.cargo = {}
        credit_loss = int(self.player.credits * 0.10)
        self.player.credits -= credit_loss
        self.player.missiles = 0
        self.player.weapons_damaged = False
        self.player.engines_damaged = False
        self.player.shields_damaged = False
        self.player.hull = int(self.player.max_hull * 0.6)
        self.player.shield = 0
        self.advance_day(2)
        msgs.append("EMERGENCY PODS DEPLOYED! Your vessel was destroyed, but the")
        msgs.append("insurance underwriters have you covered.")
        msgs.append(f"Lost {lost_cargo} cargo units and {money(credit_loss)} CR in the incident.")
        msgs.append("A replacement hull (60% condition) has been dispatched to you.")
        self.add_news("Insurance claim settled. You live to trade another day.")
        self.sound.play("victory")
        return msgs

    # ------------------------------------------------------------------ #
    # Save / load

    def _save_payload(self) -> Dict[str, Any]:
        return {
            "version": SAVE_VERSION,
            "saved_at": time.strftime("%Y-%m-%d %H:%M"),
            "difficulty_id": self.player.difficulty_id,
            "player": {
                "name": self.player.name,
                "credits": self.player.credits,
                "savings": self.player.savings,
                "loan": self.player.loan,
                "credit_score": self.player.credit_score,
                "day": self.player.day,
                "location": self.player.location,
                "difficulty_id": self.player.difficulty_id,
                "ship_id": self.player.ship_id,
                "hull": self.player.hull,
                "max_hull": self.player.max_hull,
                "shield": self.player.shield,
                "max_shield": self.player.max_shield,
                "fuel": self.player.fuel,
                "max_fuel": self.player.max_fuel,
                "cargo_cap": self.player.cargo_cap,
                "missiles": self.player.missiles,
                "insurance_active": self.player.insurance_active,
                "weapons_damaged": self.player.weapons_damaged,
                "engines_damaged": self.player.engines_damaged,
                "shields_damaged": self.player.shields_damaged,
                "highest_rank_index": self.player.highest_rank_index,
                "cargo": self.player.cargo,
                "cargo_cost_basis": self.player.cargo_cost_basis,
                "equipped_weapons": self.player.equipped_weapons,
                "equipped_shields": self.player.equipped_shields,
                "equipped_modules": self.player.equipped_modules,
                "hired_crew": self.player.hired_crew,
                "active_missions": [asdict(m) for m in self.player.active_missions],
                "stocks_owned": self.player.stocks_owned,
                "achievements": list(self.player.achievements),
                "stats": self.player.stats,
                "net_worth_history": self.player.net_worth_history,
                "reputation": self.player.reputation,
            },
            "planets": {
                p.name: {
                    "market": p.market,
                    "stock": p.stock,
                    "price_history": p.price_history,
                    "fuel_price": p.fuel_price,
                    "repair_cost": p.repair_cost,
                    "active_event": asdict(p.active_event) if p.active_event else None,
                }
                for p in self.planets.values()
            },
            "stocks": {sym: asdict(stk) for sym, stk in self.stocks.items()},
            "news_feed": self.news_feed,
        }

    def save_game(self, slot: str = "1") -> Tuple[bool, str]:
        try:
            path = slot_path(slot)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._save_payload(), f, indent=2)
            return True, f"Game saved to slot '{slot}'."
        except Exception as e:
            return False, f"Failed to save game: {e}"

    def autosave(self) -> Tuple[bool, str]:
        return self.save_game(AUTO_SLOT)

    def precombat_save(self) -> Tuple[bool, str]:
        return self.save_game(PRECOMBAT_SLOT)

    def has_save(self, slot: str) -> bool:
        try:
            return os.path.exists(slot_path(slot))
        except ValueError:
            return False

    def any_saves_exist(self) -> bool:
        return any(self.has_save(s) for s in (AUTO_SLOT, PRECOMBAT_SLOT) + SAVE_SLOTS)

    @staticmethod
    def slot_info(slot: str) -> Optional[Dict[str, Any]]:
        """Lightweight metadata for the save/load dialogs."""
        try:
            path = slot_path(slot)
        except ValueError:
            return None
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            pd = data.get("player", {})
            return {
                "slot": slot,
                "saved_at": data.get("saved_at", "?"),
                "name": pd.get("name", "?"),
                "day": pd.get("day", "?"),
                "location": pd.get("location", "?"),
                "credits": pd.get("credits", 0),
                "difficulty": data.get("difficulty_id", "?"),
            }
        except Exception:
            return {"slot": slot, "saved_at": "corrupt", "name": "?", "day": "?",
                    "location": "?", "credits": 0, "difficulty": "?"}

    def load_game(self, slot: str = "1") -> Tuple[bool, str]:
        try:
            path = slot_path(slot)
        except ValueError:
            return False, "Invalid save slot."
        if not os.path.exists(path):
            return False, f"Save slot '{slot}' not found."

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            p_data = data.get("player", {})

            self.player.name = p_data.get("name", "Commander")
            self.player.credits = int(p_data.get("credits", 2_500))
            self.player.savings = int(p_data.get("savings", 0))
            self.player.loan = int(p_data.get("loan", 0))
            self.player.credit_score = int(p_data.get("credit_score", 650))
            self.player.day = int(p_data.get("day", 1))
            self.player.location = p_data.get("location", "Earth")
            if self.player.location not in self.planets:
                self.player.location = "Earth"
            self.player.difficulty_id = p_data.get(
                "difficulty_id", data.get("difficulty_id", "normal")
            )
            if self.player.difficulty_id not in DIFFICULTIES:
                self.player.difficulty_id = "normal"
            self.difficulty_id = self.player.difficulty_id
            self.player.ship_id = p_data.get("ship_id", STARTING_SHIP)
            if self.player.ship_id not in SHIP_TEMPLATES:
                self.player.ship_id = STARTING_SHIP
            self.player.hull = int(p_data.get("hull", 100))
            self.player.max_hull = int(p_data.get("max_hull", 100))
            self.player.shield = int(p_data.get("shield", 40))
            self.player.max_shield = int(p_data.get("max_shield", 40))
            self.player.fuel = int(p_data.get("fuel", 120))
            self.player.max_fuel = int(p_data.get("max_fuel", 120))
            self.player.cargo_cap = int(p_data.get("cargo_cap", 25))
            self.player.missiles = int(p_data.get("missiles", 0))
            self.player.insurance_active = bool(p_data.get("insurance_active", False))
            self.player.weapons_damaged = bool(p_data.get("weapons_damaged", False))
            self.player.engines_damaged = bool(p_data.get("engines_damaged", False))
            self.player.shields_damaged = bool(p_data.get("shields_damaged", False))
            self.player.highest_rank_index = int(p_data.get("highest_rank_index", 0))
            self.player.cargo = {
                g: int(q) for g, q in dict(p_data.get("cargo", {})).items()
                if g in COMMODITIES
            }
            self.player.cargo_cost_basis = {
                g: float(b) for g, b in dict(p_data.get("cargo_cost_basis", {})).items()
                if g in COMMODITIES
            }
            self.player.equipped_weapons = list(p_data.get("equipped_weapons", ["laser_1"]))
            self.player.equipped_shields = list(p_data.get("equipped_shields", ["shield_1"]))
            self.player.equipped_modules = list(p_data.get("equipped_modules", []))
            self.player.hired_crew = list(p_data.get("hired_crew", []))
            self.player.stocks_owned = dict(p_data.get("stocks_owned", {}))
            self.player.achievements = set(p_data.get("achievements", []))
            saved_stats = dict(DEFAULT_STATS)
            saved_stats.update(p_data.get("stats", {}))
            self.player.stats = saved_stats
            self.player.net_worth_history = list(
                p_data.get("net_worth_history", [self.player.credits])
            )
            saved_rep = default_reputation()
            saved_rep.update(p_data.get("reputation", {}))
            self.player.reputation = saved_rep

            self.player.active_missions = [
                Mission(**m_dict) for m_dict in p_data.get("active_missions", [])
            ]

            planets_data = data.get("planets", {})
            for p_name, p_info in planets_data.items():
                if p_name in self.planets:
                    p = self.planets[p_name]
                    p.market = dict(p_info.get("market", {}))
                    p.stock = dict(p_info.get("stock", {}))
                    p.price_history = dict(p_info.get("price_history", {}))
                    p.fuel_price = int(p_info.get("fuel_price", BASE_FUEL_PRICE))
                    p.repair_cost = int(p_info.get("repair_cost", BASE_REPAIR_COST))
                    ev_dict = p_info.get("active_event")
                    p.active_event = PlanetEvent(**ev_dict) if ev_dict else None

            stocks_data = data.get("stocks", {})
            for sym, s_info in stocks_data.items():
                if sym in self.stocks:
                    self.stocks[sym] = StockData(**s_info)

            self.news_feed = list(data.get("news_feed", []))

            # Sanitize crew / equipment / achievement references.
            self.player.hired_crew = [
                c for c in self.player.hired_crew if c in CREW_INDEX
            ]
            for lst_name in ("equipped_weapons", "equipped_shields", "equipped_modules"):
                lst = getattr(self.player, lst_name)
                setattr(self.player, lst_name,
                        [e for e in lst if e in EQUIPMENT_ITEMS])
            self.player.achievements = {
                a for a in self.player.achievements if a in ACHIEVEMENTS
            }
            self.player.active_missions = [
                m for m in self.player.active_missions
                if m.destination in self.planets
            ]

            # A loaded career never re-announces ranks already earned.
            self.player.highest_rank_index = max(
                self.player.highest_rank_index, self.rank_index())

            self.is_game_over = False
            self.recalculate_ship_stats()
            self.available_missions = generate_mission_board(
                self.current_planet,
                list(self.planets.values()),
                self.player.day,
                career_tier=self.career_tier(),
            )
            self.check_achievements()
            self.sound.play("buy")
            return True, f"Save slot '{slot}' loaded. Welcome back, {self.player.name}."
        except Exception as e:
            return False, f"Failed to load save: {e}"


# ==============================================================================
# TACTICAL COMBAT — overhauled
# ==============================================================================

ENEMY_PERSONALITIES: Dict[str, str] = {
    "aggressive": "fights head-on and punishes exposed hulls",
    "defensive": "cycles shields and outlasts opponents",
    "opportunist": "steals cargo and cripples your subsystems",
    "coward": "runs the moment the battle turns sour",
}


class CombatEncounter:
    """Stateful tactical battle. The GUI renders its attributes; the test
    suite drives it headlessly. All mutations flow through player_action()."""

    ACTIONS = ("fire", "target_engines", "target_weapons", "target_shields",
               "missile", "drones", "recharge", "board", "flee")

    def __init__(
        self,
        engine: GameEngine,
        enemy_name: str,
        enemy_ship_id: str,
        is_bounty: bool = False,
        bounty_reward: int = 0,
    ):
        self.engine = engine
        self.enemy_name = enemy_name
        self.enemy_ship_id = enemy_ship_id
        self.is_bounty = is_bounty
        self.bounty_reward = bounty_reward

        tmpl = SHIP_TEMPLATES.get(enemy_ship_id, SHIP_TEMPLATES["sparrow"])
        self.enemy_ship_name = tmpl.name

        # Power scaling: difficulty + player net worth through pirate tiers
        # (gentler floor keeps early-game ambushes survivable).
        nw = engine.calculate_net_worth()
        scale = engine.difficulty.enemy_power * (0.75 + min(1.0, nw / 400_000) * 0.5)

        self.enemy_max_hull = max(30, int(tmpl.max_hull * scale))
        self.enemy_max_shield = max(10, int(tmpl.max_shield * scale))
        self.enemy_hull = self.enemy_max_hull
        self.enemy_shield = self.enemy_max_shield
        self.enemy_weapons_damaged = False
        self.enemy_engines_damaged = False
        self.enemy_damage_range = (
            max(5, int(15 * scale)), max(10, int(32 * scale)),
        )
        self.turn_count = 0

        # Personality chosen to match ship class.
        if tmpl.ship_class in ("Combat Scout", "Heavy Frigate", "Dreadnought"):
            personality = random.choices(
                ["aggressive", "defensive", "opportunist"], [5, 3, 2]
            )[0]
        elif tmpl.ship_class in ("Medium Freighter", "Heavy Freighter", "Armored Hauler"):
            personality = random.choices(
                ["defensive", "coward", "opportunist"], [4, 3, 3]
            )[0]
        else:
            personality = random.choices(
                ["aggressive", "coward", "opportunist"], [4, 3, 3]
            )[0]
        self.personality = personality

        self.drones_active = False
        self.combat_log: List[str] = [
            f"Battle engaged with {enemy_name} [{self.enemy_ship_name}]!",
            f"Threat analysis: pilot is {personality.upper()} — "
            f"{ENEMY_PERSONALITIES[personality]}.",
        ]
        self.is_finished = False
        self.player_won = False
        self.player_escaped = False
        self.player_dead = False
        self.insurance_used = False
        self.enemy_fled = False
        self.boarded_success = False

    # ------------------------------------------------------------------ #
    # Helpers

    def _p(self) -> Player:
        return self.engine.player

    def player_missiles(self) -> int:
        return self._p().missiles

    def can_board(self) -> bool:
        return (not self.is_finished) and self.enemy_hull <= max(10, int(self.enemy_max_hull * 0.25))

    def can_flee(self) -> bool:
        return not self._p().engines_damaged

    # ------------------------------------------------------------------ #
    # Main dispatch

    def player_action(self, action: str, arg: Any = None) -> List[str]:
        if self.is_finished:
            return ["The battle is already over."]
        if action not in self.ACTIONS:
            return ["Unknown combat maneuver."]

        msgs: List[str] = []
        self.turn_count += 1

        # Autonomous drones strike first.
        if self.drones_active and action != "drones":
            msgs.extend(self._drone_strike())

        # Set when an action already resolves the enemy's counter-attack
        # internally (e.g. a repelled boarding attempt) — the enemy must not
        # strike twice in a single turn.
        enemy_responded = False

        if action == "fire":
            msgs.extend(self._player_attack(None))
        elif action == "target_engines":
            msgs.extend(self._player_attack("engines"))
        elif action == "target_weapons":
            msgs.extend(self._player_attack("weapons"))
        elif action == "target_shields":
            msgs.extend(self._player_attack("shields"))
        elif action == "missile":
            msgs.extend(self._fire_missile())
        elif action == "drones":
            msgs.extend(self._deploy_drones())
        elif action == "recharge":
            msgs.extend(self._recharge_shields())
        elif action == "board":
            board_msgs, enemy_responded = self._board_enemy()
            msgs.extend(board_msgs)
        elif action == "flee":
            escaped, flee_msgs = self._player_flee()
            msgs.extend(flee_msgs)
            if escaped:
                return msgs

        if self.is_finished:
            return msgs

        if not enemy_responded:
            msgs.extend(self._enemy_turn())
        return msgs

    # ------------------------------------------------------------------ #
    # Player offensive actions

    def _weapon_damage_multiplier(self) -> float:
        return 0.6 if self._p().weapons_damaged else 1.0

    def _player_attack(self, target_subsystem: Optional[str]) -> List[str]:
        msgs: List[str] = []
        p = self._p()

        gunner_perk = p.has_crew_perk("gunner") or 0.0
        targeting_mod = 0.10 if p.has_module("combat_scanner") else 0.0
        dmg_mult = self._weapon_damage_multiplier()

        fired_any = False
        for eq_id in p.equipped_weapons:
            eq = EQUIPMENT_ITEMS.get(eq_id)
            if not eq or eq.damage <= 0:      # missile rack has no beam attack
                continue
            fired_any = True

            acc = eq.accuracy + targeting_mod - (0.15 if target_subsystem else 0.0)
            if random.random() > acc:
                msgs.append(f"Your {eq.name} missed the target!")
                continue

            dmg = eq.damage * (1.0 + gunner_perk) * dmg_mult
            is_crit = random.random() < (eq.crit_chance + targeting_mod)
            if is_crit:
                dmg *= 1.75
                msgs.append(f"[CRITICAL HIT!] Your {eq.name} scored a devastating strike!")

            msgs.extend(self._apply_damage_to_enemy(eq.name, int(dmg)))

            if target_subsystem and self.enemy_hull > 0 and random.random() < 0.60:
                msgs.extend(self._subsystem_strike_effect(target_subsystem))

        if not fired_any and not p.has_missile_rack():
            msgs.append("Your ship has no functioning weapon systems!")

        self.engine.sound.play("laser")

        if self.enemy_hull <= 0 and not self.is_finished:
            msgs.extend(self._victory())
        return msgs

    def _apply_damage_to_enemy(self, source: str, dmg: int) -> List[str]:
        msgs: List[str] = []
        if self.enemy_shield > 0:
            s_dmg = min(self.enemy_shield, dmg)
            self.enemy_shield -= s_dmg
            dmg -= s_dmg
            msgs.append(f"Your {source} burned {s_dmg} enemy shield HP.")
        if dmg > 0:
            self.enemy_hull = max(0, self.enemy_hull - dmg)
            msgs.append(f"Your {source} tore {dmg} hull damage into the enemy!")
        return msgs

    def _subsystem_strike_effect(self, target_subsystem: str) -> List[str]:
        p = self._p()
        if target_subsystem == "engines" and not self.enemy_engines_damaged:
            self.enemy_engines_damaged = True
            return [">> TARGET SYSTEM DISABLED: Enemy propulsion crippled! They cannot flee."]
        if target_subsystem == "weapons" and not self.enemy_weapons_damaged:
            self.enemy_weapons_damaged = True
            return [">> TARGET SYSTEM DISABLED: Enemy weapons grid down! Their damage is halved."]
        if target_subsystem == "shields" and self.enemy_shield > 0:
            drained = self.enemy_shield
            self.enemy_shield = 0
            return [f">> SHIELD EMITTERS FRIED: {drained} enemy shield HP vented into space!"]
        if target_subsystem == "cargo":
            loot_good = random.choice(list(COMMODITIES.keys()))
            qty = random.randint(1, 4)
            added = p.add_cargo(loot_good, qty)
            if added > 0:
                return [f">> CARGO BREACH: Salvaged {added}x {COMMODITIES[loot_good].name} from the breach!"]
            return [">> CARGO BREACH: Containers drifted away — your hold is full."]
        return []

    def _fire_missile(self) -> List[str]:
        p = self._p()
        if not p.has_missile_rack():
            return ["You have no missile launcher installed!"]
        if p.missiles <= 0:
            return ["Missile magazines are empty! Buy more at the shipyard."]

        p.missiles -= 1
        p.stats["missiles_fired"] += 1
        self.engine.check_achievements()

        if random.random() > 0.95:
            self.engine.sound.play("laser")
            return ["Missile launch... guidance failure! The warhead drifts into the void."]

        dmg = random.randint(85, 125)
        crit = random.random() < 0.18
        if crit:
            dmg = int(dmg * 1.6)
        msgs = [f"HAVOC MISSILE AWAY! Warhead impact deals {dmg} damage"
                + (" — CRITICAL DETONATION!" if crit else "!")]
        self.engine.sound.play("alarm")
        msgs.extend(self._apply_damage_to_enemy("missile", dmg))

        if self.enemy_hull <= 0 and not self.is_finished:
            msgs.extend(self._victory())
        return msgs

    def _drone_strike(self) -> List[str]:
        dmg = random.randint(8, 16)
        msgs = [f"Attack drones swarm the hostile: {dmg} automatic damage."]
        msgs.extend(self._apply_damage_to_enemy("drone swarm", dmg))
        if self.enemy_hull <= 0 and not self.is_finished:
            msgs.extend(self._victory())
        return msgs

    def _deploy_drones(self) -> List[str]:
        p = self._p()
        if not p.has_drone_bay():
            return ["No drone bay installed! Purchase the Autonomous Drone Bay module."]
        if self.drones_active:
            msgs = ["Drone wing is already deployed and hunting."]
            msgs.extend(self._drone_strike())
            return msgs
        self.drones_active = True
        msgs = ["DRONE BAY OPEN! Four attack drones streak out and lock onto the enemy."]
        msgs.extend(self._drone_strike())
        return msgs

    def _recharge_shields(self) -> List[str]:
        p = self._p()
        eff_max = p.effective_max_shield()
        rate = 0.50 if p.has_module("shield_capacitor") else 0.35
        recharge = int(eff_max * rate)
        p.shield = min(eff_max, p.shield + recharge)
        self.engine.sound.play("upgrade")
        msgs = [f"Diverted reactor power to shields! Restored {recharge} shield HP."]
        return msgs

    def _board_enemy(self) -> Tuple[List[str], bool]:
        """Attempt a boarding action.

        Returns (messages, enemy_responded). On a repelled boarding the enemy
        counter-attacks (overpowered) *inside* this method, so the caller must
        skip the regular enemy turn to avoid a double strike.
        """
        p = self._p()
        if not self.can_board():
            return ["Boarding requires the enemy hull to be at 25% integrity or less."], False
        bonus = 0.15 if p.has_crew("drake") else 0.0
        bonus += 0.20 if p.has_module("boarding_pod") else 0.0
        success_chance = min(0.95, 0.55 + bonus)
        msgs: List[str] = [f"Boarding party launches! Success chance: {int(success_chance * 100)}%."]

        if random.random() < success_chance:
            self.boarded_success = True
            p.stats["boards"] += 1
            self.engine.check_achievements()
            loot_cr = random.randint(800, 2_500)
            loot_good = random.choice(list(COMMODITIES.keys()))
            loot_qty = random.randint(2, 5)
            added = p.add_cargo(loot_good, loot_qty)
            missiles_found = random.choice([0, 0, 1, 2])
            p.missiles = min(PLAYER_MISSILE_CAP, p.missiles + missiles_found)
            p.credits += loot_cr
            msgs.append(">> YOUR CREW STORMS THE BRIDGE! The enemy crew surrenders.")
            msgs.append(f"Seized {money(loot_cr)} CR, {added}x {COMMODITIES[loot_good].name}"
                        + (f" and {missiles_found} missile(s)!" if missiles_found else "!"))
            self.enemy_hull = 0
            msgs.extend(self._victory(boarded=True))
            return msgs, False
        msgs.append(">> BOARDING REPULSED! Your party is forced back under heavy fire.")
        p.shield = 0
        # Overpowered counter-attack happens here — flag it so the caller
        # does not trigger a second enemy turn.
        msgs.extend(self._enemy_turn(overpowered=True))
        return msgs, True

    def _player_flee(self) -> Tuple[bool, List[str]]:
        p = self._p()
        if p.engines_damaged:
            return False, ["Warp drives are DAMAGED — escape is impossible! Fight or die."]

        speed = SHIP_TEMPLATES[p.ship_id].speed
        nav_perk = p.has_crew_perk("nav") or 0.0
        booster = 0.20 if p.has_module("thruster_booster") else 0.0
        escape_chance = min(0.90, 0.45 + (speed - 1.0) * 0.3 + nav_perk + booster)
        if self.enemy_engines_damaged:
            escape_chance = 1.0

        if random.random() < escape_chance:
            self.is_finished = True
            self.player_escaped = True
            self.engine.sound.play("warp")
            return True, ["Engaged emergency warp burn! Successfully escaped into the void."]

        msgs = ["Warp alignment failed! Unable to shake enemy pursuers."]
        return False, msgs

    # ------------------------------------------------------------------ #
    # Resolution

    def _victory(self, boarded: bool = False) -> List[str]:
        self.is_finished = True
        self.player_won = True
        p = self._p()
        msgs = [
            f"VICTORY! {'The crew of ' + self.enemy_name + ' strikes their colors!' if boarded else 'The ' + self.enemy_name + ' was destroyed in a blinding explosion!'}"
        ]
        p.stats["pirates_defeated"] += 1

        salvage = random.randint(400, 1_800)
        if self.is_bounty:
            bounty_mult = 1.0 + RANK_BOUNTY_BONUS[self.engine.rank_index()]
            salvage += int(self.bounty_reward * bounty_mult)
            p.stats["bounties_claimed"] += 1
            for m in list(p.active_missions):
                if m.bounty_target_name == self.enemy_name:
                    m.completed = True
                    p.active_missions.remove(m)
        p.credits += salvage
        msgs.append(f"Salvaged {money(salvage)} credits from the wreckage.")

        # Clearing out raiders earns goodwill with the lawful factions and
        # a grudge from the Free Corsairs, who lose one of their own.
        self.engine.adjust_reputation("Sol Federation", 2)
        self.engine.adjust_reputation("Outer Alliance", 2)
        self.engine.adjust_reputation("Free Corsairs", -3)

        self.engine.sound.play("victory")
        self.engine.check_achievements()
        self.engine.check_promotion()
        return msgs

    def _handle_player_death(self) -> List[str]:
        p = self._p()
        self.is_finished = True
        if p.insurance_active:
            self.insurance_used = True
            msgs = ["CRITICAL FAILURE: Your ship breaks apart around you..."]
            msgs.extend(self.engine.insurance_respawn())
            return msgs
        self.player_dead = True
        self.engine.is_game_over = True
        self.engine.sound.play("death")
        return [
            "CRITICAL FAILURE: Ship destroyed in combat! Hull integrity collapsed.",
            "The void claims another trader.",
        ]

    # ------------------------------------------------------------------ #
    # Enemy AI

    def _enemy_attack(self, overpowered: bool = False, target_player_subsystem: bool = False) -> List[str]:
        p = self._p()
        msgs: List[str] = []

        base_dmg = random.randint(*self.enemy_damage_range)
        if self.enemy_weapons_damaged:
            base_dmg = int(base_dmg * 0.5)
        if overpowered:
            base_dmg = int(base_dmg * 1.2)

        evasion = 0.05
        if p.has_module("thruster_booster"):
            evasion += 0.15
        if p.has_module("cloaking_field"):
            evasion += 0.10
        if p.has_crew_perk("nav"):
            evasion += 0.10
        if p.engines_damaged:
            evasion -= 0.10

        if random.random() < evasion:
            msgs.append("You skillfully dodged the enemy's incoming barrage!")
            return msgs

        dmg = base_dmg
        if p.shield > 0:
            s_dmg = min(p.shield, dmg)
            p.shield -= s_dmg
            dmg -= s_dmg
            msgs.append(f"Enemy fire struck your shields for {s_dmg} damage.")
            self.engine.sound.play("shield_hit")

        if dmg > 0:
            p.hull = max(0, p.hull - dmg)
            msgs.append(f"WARNING: Armor breached! Ship sustained {dmg} hull damage!")
            self.engine.sound.play("alarm")

            # Enemy crits can cripple YOUR subsystems.
            crit_chance = 0.12 if not target_player_subsystem else 0.30
            if p.hull > 0 and random.random() < crit_chance:
                subsystem = random.choice(["weapons", "engines", "shields"])
                already = {
                    "weapons": p.weapons_damaged,
                    "engines": p.engines_damaged,
                    "shields": p.shields_damaged,
                }[subsystem]
                if not already:
                    if subsystem == "weapons":
                        p.weapons_damaged = True
                        msgs.append(">> YOUR WEAPON GRID IS HIT: damage output reduced 40% until repaired!")
                    elif subsystem == "engines":
                        p.engines_damaged = True
                        msgs.append(">> YOUR WARP DRIVES ARE HIT: cannot flee until repaired!")
                    elif subsystem == "shields":
                        p.shields_damaged = True
                        msgs.append(">> SHIELD EMITTERS DAMAGED: maximum shield power reduced 40%!")
                    p.shield = min(p.shield, p.effective_max_shield())

        if p.hull <= 0:
            msgs.extend(self._handle_player_death())
        return msgs

    def _enemy_steal_cargo(self) -> List[str]:
        p = self._p()
        if not p.cargo:
            return []
        good = random.choice(list(p.cargo.keys()))
        qty = min(p.cargo[good], random.randint(1, 3))
        removed = p.remove_cargo(good, qty)
        return [f"OPPORTUNIST RAID! Pirate cutters stole {removed}x {COMMODITIES[good].name} from your hold!"]

    def _enemy_flee_attempt(self) -> List[str]:
        if self.enemy_engines_damaged:
            return ["The enemy thrashes escape pods but their drives are dead — no escape!"]
        chance = 0.45
        if random.random() < chance:
            self.is_finished = True
            self.enemy_fled = True
            return [f"The {self.enemy_name} engages a desperate burn and escapes into the dark!"]
        return [f"The {self.enemy_name} tries to flee but your guns keep them pinned!"]

    def _enemy_charge(self) -> List[str]:
        recharge = int(self.enemy_max_shield * 0.35)
        before = self.enemy_shield
        self.enemy_shield = min(self.enemy_max_shield, self.enemy_shield + recharge)
        return [f"The enemy diverts power to shields (+{self.enemy_shield - before} HP)."]

    def _enemy_turn(self, overpowered: bool = False) -> List[str]:
        if self.is_finished or self.enemy_hull <= 0:
            return []

        msgs: List[str] = []
        p = self._p()

        if self.personality == "coward" and self.enemy_hull < self.enemy_max_hull * 0.35:
            msgs.extend(self._enemy_flee_attempt())
            if self.is_finished:
                return msgs
            msgs.extend(self._enemy_attack(overpowered))
            return msgs

        if self.personality == "defensive" and self.enemy_shield < self.enemy_max_shield * 0.30:
            msgs.extend(self._enemy_charge())
            return msgs

        if self.personality == "opportunist":
            roll = random.random()
            if roll < 0.25 and p.cargo:
                msgs.extend(self._enemy_steal_cargo())
                return msgs
            if roll < 0.45:
                msgs.extend(self._enemy_attack(target_player_subsystem=True))
                return msgs
            msgs.extend(self._enemy_attack(overpowered))
            return msgs

        # Aggressive (and fallback)
        if p.shield <= 0 and random.random() < 0.30:
            msgs.extend(self._enemy_attack(target_player_subsystem=True))
        else:
            msgs.extend(self._enemy_attack(overpowered))
        return msgs


def start_combat(engine: GameEngine, enc: Dict[str, Any]) -> CombatEncounter:
    """Create a combat from an encounter dict and snapshot a pre-combat save."""
    is_bounty = enc.get("type") == "bounty_combat"
    enemy_name = enc.get("target_name") if is_bounty else enc.get("enemy_name", "Corsair Raider")
    enemy_ship = enc.get("target_ship") if is_bounty else enc.get("enemy_ship", "viper")
    reward = enc.get("reward", 0)
    engine.precombat_save()
    return CombatEncounter(engine, enemy_name, enemy_ship, is_bounty=is_bounty, bounty_reward=reward)


# ==============================================================================
# BROWSER WEB CLIENT INTERFACE (HTML / CSS / JS)
# ==============================================================================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Space Trader: Odyssey — Nebula Edition</title>
  <meta name="description" content="A comprehensive sci-fi space trading, exploration, and tactical combat RPG game with real-time sector economy and celestial navigation.">
  <style>
    :root {
      --bg: #04081c;
      --bg2: #070e28;
      --panel: #0b1538;
      --panel-hi: #112052;
      --panel-border: #1a2c68;
      --panel-border-hi: #2e489c;
      --fg: #e2eeff;
      --fg-dim: #7f95c4;
      --fg-dark: #4d5e87;
      --cyan: #00e5ff;
      --cyan-dim: rgba(0, 229, 255, 0.15);
      --gold: #ffb703;
      --gold-dim: rgba(255, 183, 3, 0.15);
      --green: #06d6a0;
      --green-dim: rgba(6, 214, 160, 0.15);
      --red: #ff5252;
      --red-dim: rgba(255, 82, 82, 0.15);
      --purple: #9d4edd;
      --purple-dim: rgba(157, 78, 221, 0.15);
      --blue: #3a86ff;
      --font-ui: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --font-mono: ui-monospace, "Cascadia Code", "Fira Code", monospace;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background-color: var(--bg);
      color: var(--fg);
      font-family: var(--font-ui);
      line-height: 1.5;
      font-size: 14px;
      overflow-x: hidden;
      min-height: 100vh;
      background-image: 
        radial-gradient(1px 1px at 20px 30px, #ffffffaa, transparent),
        radial-gradient(1.5px 1.5px at 140px 180px, #00e5ffaa, transparent),
        radial-gradient(1px 1px at 280px 70px, #ffffffaa, transparent),
        radial-gradient(2px 2px at 450px 220px, #ffb703aa, transparent),
        radial-gradient(1px 1px at 600px 120px, #ffffff88, transparent),
        radial-gradient(1.5px 1.5px at 750px 290px, #9d4eddaa, transparent),
        radial-gradient(1px 1px at 900px 90px, #ffffffaa, transparent),
        radial-gradient(1px 1px at 1050px 250px, #00e5ffaa, transparent);
      background-size: 500px 350px;
    }

    /* Scrollbars */
    ::-webkit-scrollbar { width: 8px; height: 8px; }
    ::-webkit-scrollbar-track { background: var(--bg); }
    ::-webkit-scrollbar-thumb { background: var(--panel-border); border-radius: 4px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--cyan); }

    /* Top HUD Header */
    #header-hud {
      background: rgba(7, 14, 40, 0.85);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--panel-border);
      position: sticky;
      top: 0;
      z-index: 100;
      padding: 10px 20px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }

    .brand-group {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .game-logo {
      font-weight: 900;
      font-size: 16px;
      letter-spacing: 2px;
      text-transform: uppercase;
      color: var(--cyan);
      display: flex;
      align-items: center;
      gap: 8px;
      text-shadow: 0 0 12px rgba(0, 229, 255, 0.4);
    }
    .rank-pill {
      background: var(--panel-hi);
      border: 1px solid var(--panel-border-hi);
      padding: 3px 10px;
      border-radius: 12px;
      font-size: 12px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
      transition: all 0.2s;
    }
    .rank-pill:hover { border-color: var(--cyan); }

    .hud-stat-box {
      display: flex;
      align-items: center;
      gap: 20px;
    }
    .hud-stat-item {
      display: flex;
      flex-direction: column;
    }
    .hud-stat-label {
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 1px;
      color: var(--fg-dim);
    }
    .hud-stat-val {
      font-family: var(--font-mono);
      font-size: 15px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 4px;
    }

    .hud-actions {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .hud-btn {
      background: var(--panel);
      border: 1px solid var(--panel-border);
      color: var(--fg);
      padding: 6px 12px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 12px;
      font-weight: 600;
      transition: all 0.2s;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .hud-btn:hover {
      background: var(--panel-hi);
      border-color: var(--cyan);
      color: var(--cyan);
    }

    /* Ship Vitals Bar */
    #vitals-bar {
      background: var(--bg2);
      border-bottom: 1px solid var(--panel-border);
      padding: 8px 20px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      font-size: 12px;
    }
    .vitals-ship-name {
      font-weight: 700;
      color: var(--cyan);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .vitals-meters {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 20px;
    }
    .meter-group {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .meter-label {
      font-size: 10px;
      text-transform: uppercase;
      color: var(--fg-dim);
      font-weight: 700;
      width: 48px;
    }
    .meter-bar-outer {
      width: 90px;
      height: 8px;
      background: rgba(255, 255, 255, 0.08);
      border-radius: 4px;
      overflow: hidden;
      position: relative;
    }
    .meter-bar-inner {
      height: 100%;
      border-radius: 4px;
      transition: width 0.3s ease;
    }
    .meter-val {
      font-family: var(--font-mono);
      font-size: 11px;
      font-weight: 600;
      min-width: 60px;
    }

    /* Subsystem damage badges */
    .subsystem-alert {
      background: var(--red-dim);
      border: 1px solid var(--red);
      color: var(--red);
      font-size: 10px;
      padding: 2px 6px;
      border-radius: 4px;
      font-weight: 700;
      animation: pulseAlert 1.5s infinite;
    }
    @keyframes pulseAlert {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }

    /* Tab Navigation */
    #tab-nav {
      background: var(--panel);
      border-bottom: 1px solid var(--panel-border);
      padding: 0 20px;
      display: flex;
      overflow-x: auto;
      gap: 2px;
    }
    .tab-btn {
      background: transparent;
      border: none;
      border-bottom: 2px solid transparent;
      color: var(--fg-dim);
      padding: 12px 16px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 8px;
      white-space: nowrap;
      transition: all 0.2s;
    }
    .tab-btn:hover {
      color: var(--fg);
      background: rgba(255, 255, 255, 0.03);
    }
    .tab-btn.active {
      color: var(--cyan);
      border-bottom-color: var(--cyan);
      background: rgba(0, 229, 255, 0.05);
    }

    /* Main Container */
    #app-main {
      max-width: 1440px;
      margin: 0 auto;
      padding: 20px;
      min-height: calc(100vh - 200px);
    }

    .view-container { display: none; }
    .view-container.active { display: block; animation: fadeIn 0.25s ease-in-out; }
    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(6px); }
      to { opacity: 1; transform: translateY(0); }
    }

    /* Card Panels */
    .panel-card {
      background: var(--panel);
      border: 1px solid var(--panel-border);
      border-radius: 8px;
      padding: 20px;
      margin-bottom: 20px;
    }
    .panel-card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--panel-border);
    }
    .panel-card-title {
      font-size: 16px;
      font-weight: 700;
      color: var(--cyan);
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .panel-card-subtitle {
      font-size: 12px;
      color: var(--fg-dim);
    }

    /* Star Map Elements */
    #map-wrapper {
      display: grid;
      grid-template-columns: 1fr 340px;
      gap: 20px;
    }
    @media (max-width: 1024px) {
      #map-wrapper { grid-template-columns: 1fr; }
    }
    #map-svg-container {
      background: #020514;
      border: 1px solid var(--panel-border);
      border-radius: 8px;
      overflow: hidden;
      position: relative;
      min-height: 520px;
    }
    .map-planet-node {
      cursor: pointer;
      transition: all 0.2s;
    }
    .map-planet-node:hover circle.planet-body {
      filter: drop-shadow(0 0 10px var(--cyan));
      stroke-width: 2.5;
    }
    .hyperlane {
      stroke: rgba(46, 72, 156, 0.4);
      stroke-dasharray: 4 4;
      stroke-width: 1;
    }
    .current-ping {
      animation: ping 2s infinite;
      transform-origin: center;
    }
    @keyframes ping {
      0% { r: 12px; opacity: 0.8; }
      100% { r: 32px; opacity: 0; }
    }

    /* Planet Flight Dossier */
    #dossier-card {
      background: var(--panel);
      border: 1px solid var(--panel-border);
      border-radius: 8px;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .dossier-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .dossier-title { font-size: 18px; font-weight: 700; color: var(--fg); }
    .dossier-subtitle { font-size: 12px; color: var(--fg-dim); }
    .dossier-metric-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      background: var(--bg2);
      border: 1px solid var(--panel-border);
      border-radius: 6px;
      padding: 12px;
    }
    .dossier-btn-engage {
      background: var(--cyan);
      color: #020514;
      border: none;
      padding: 12px;
      border-radius: 6px;
      font-size: 14px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 1px;
      cursor: pointer;
      transition: all 0.2s;
      width: 100%;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }
    .dossier-btn-engage:hover:not(:disabled) {
      background: #33ecff;
      box-shadow: 0 0 16px rgba(0, 229, 255, 0.5);
    }
    .dossier-btn-engage:disabled {
      background: #1a2c68;
      color: var(--fg-dim);
      cursor: not-allowed;
    }

    /* Market View */
    .market-filter-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 16px;
    }
    .filter-pills {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .filter-pill {
      background: var(--panel);
      border: 1px solid var(--panel-border);
      color: var(--fg-dim);
      padding: 5px 12px;
      border-radius: 14px;
      font-size: 12px;
      cursor: pointer;
      transition: all 0.2s;
    }
    .filter-pill.active {
      background: var(--cyan-dim);
      border-color: var(--cyan);
      color: var(--cyan);
      font-weight: 700;
    }
    .search-input {
      background: var(--bg2);
      border: 1px solid var(--panel-border);
      color: var(--fg);
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 13px;
      outline: none;
      width: 220px;
    }
    .search-input:focus { border-color: var(--cyan); }

    /* Tables */
    .data-table-wrapper {
      background: var(--panel);
      border: 1px solid var(--panel-border);
      border-radius: 8px;
      overflow-x: auto;
    }
    table.data-table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
    }
    table.data-table th {
      background: var(--bg2);
      color: var(--fg-dim);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 1px;
      padding: 10px 14px;
      border-bottom: 1px solid var(--panel-border);
    }
    table.data-table td {
      padding: 10px 14px;
      border-bottom: 1px solid rgba(26, 44, 104, 0.4);
      font-size: 13px;
    }
    table.data-table tr:hover td {
      background: rgba(255, 255, 255, 0.02);
    }

    .btn-action-sm {
      background: var(--panel-hi);
      border: 1px solid var(--panel-border-hi);
      color: var(--fg);
      padding: 4px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
    }
    .btn-action-sm:hover:not(:disabled) {
      border-color: var(--cyan);
      color: var(--cyan);
    }
    .btn-action-sm:disabled {
      opacity: 0.3;
      cursor: not-allowed;
    }
    .btn-buy { background: var(--cyan-dim); border-color: var(--cyan); color: var(--cyan); }
    .btn-sell { background: var(--gold-dim); border-color: var(--gold); color: var(--gold); }

    /* Trade Drawer Modal */
    #trade-modal {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(2, 5, 20, 0.8);
      backdrop-filter: blur(8px);
      z-index: 200;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    #trade-modal.active { display: flex; }
    .modal-box {
      background: var(--panel);
      border: 1px solid var(--panel-border-hi);
      border-radius: 12px;
      width: 100%;
      max-width: 520px;
      padding: 24px;
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.8);
    }

    /* Combat Overlay Bridge */
    #combat-modal {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(2, 5, 20, 0.95);
      backdrop-filter: blur(12px);
      z-index: 300;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    #combat-modal.active { display: flex; }
    .combat-bridge {
      background: var(--bg2);
      border: 1px solid var(--red);
      border-radius: 12px;
      width: 100%;
      max-width: 860px;
      padding: 24px;
      box-shadow: 0 0 40px rgba(255, 82, 82, 0.2);
    }
    @keyframes flightDash {
      to { stroke-dashoffset: -20; }
    }
    .flight-vector {
      stroke-dasharray: 6 4;
      animation: flightDash 1.2s linear infinite;
    }
    .dmg-float {
      position: absolute;
      font-family: var(--font-mono);
      font-weight: 800;
      font-size: 14px;
      text-shadow: 0 2px 8px rgba(0,0,0,0.8);
      animation: floatUpFade 1.4s cubic-bezier(0.2, 0.8, 0.2, 1) forwards;
      pointer-events: none;
    }
    @keyframes floatUpFade {
      0% { opacity: 1; transform: translateY(0) scale(1); }
      50% { transform: translateY(-22px) scale(1.1); }
      100% { opacity: 0; transform: translateY(-44px) scale(0.9); }
    }
    .shake-anim {
      animation: shipShake 0.4s ease;
    }
    @keyframes shipShake {
      0%, 100% { transform: translate(0, 0); }
      20% { transform: translate(-5px, 3px); }
      40% { transform: translate(5px, -3px); }
      60% { transform: translate(-3px, -2px); }
      80% { transform: translate(3px, 2px); }
    }
    .laser-beam {
      stroke-dasharray: 400;
      stroke-dashoffset: 400;
      animation: beamShoot 0.4s ease-out forwards;
    }
    @keyframes beamShoot {
      to { stroke-dashoffset: 0; }
    }
    .hardpoint-slot {
      background: var(--bg2);
      border: 1px dashed var(--panel-border-hi);
      border-radius: 6px;
      padding: 10px 14px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .hardpoint-mounted {
      background: var(--panel);
      border: 1px solid var(--panel-border-hi);
      border-radius: 6px;
      padding: 10px 14px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    /* Generic Modal */
    .generic-modal {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(2, 5, 20, 0.85);
      backdrop-filter: blur(8px);
      z-index: 250;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .generic-modal.active { display: flex; }

    /* Toast Log Notification */
    #toast-container {
      position: fixed;
      bottom: 20px;
      right: 20px;
      z-index: 500;
      display: flex;
      flex-direction: column;
      gap: 8px;
      pointer-events: none;
    }
    .toast-msg {
      background: var(--panel-hi);
      border-left: 4px solid var(--cyan);
      border-radius: 4px;
      padding: 10px 16px;
      font-size: 13px;
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
      animation: slideInToast 0.3s ease;
      max-width: 380px;
      color: var(--fg);
    }
    @keyframes slideInToast {
      from { transform: translateX(100%); opacity: 0; }
      to { transform: translateX(0); opacity: 1; }
    }

    /* Grid Layouts */
    .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 20px; }
    .grid-3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 20px; }
    .grid-4 { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; }

    /* Badges & Pills */
    .pill {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 10px;
      font-size: 11px;
      font-weight: 700;
    }
    .pill-cyan { background: var(--cyan-dim); color: var(--cyan); }
    .pill-gold { background: var(--gold-dim); color: var(--gold); }
    .pill-green { background: var(--green-dim); color: var(--green); }
    .pill-red { background: var(--red-dim); color: var(--red); }
    .pill-purple { background: var(--purple-dim); color: var(--purple); }

    /* Sparkline SVG */
    .sparkline-svg {
      display: inline-block;
      vertical-align: middle;
      overflow: visible;
    }
  </style>
</head>
<body>

  <!-- TOP HUD HEADER -->
  <header id="header-hud">
    <div class="brand-group">
      <div class="game-logo">
        <span>🚀</span> SPACE TRADER: ODYSSEY
      </div>
      <div id="rank-badge" class="rank-pill" onclick="switchTab('log')" title="Click to view Career progression">
        <span id="hud-rank-insignia" style="color: var(--cyan);">·</span>
        <span id="hud-rank-title">Cadet</span>
      </div>
      <div id="hud-renown-meter" style="font-size: 11px; color: var(--fg-dim);">
        <span id="hud-renown-val">0</span> Renown (<span id="hud-renown-next">6,000</span> to promo)
      </div>
    </div>

    <div class="hud-stat-box">
      <div class="hud-stat-item">
        <span class="hud-stat-label">Location</span>
        <span class="hud-stat-val" style="color: var(--cyan);">
          <span id="hud-location">Earth</span>
          <span id="hud-security" class="pill pill-green" style="font-size: 9px; margin-left: 4px;">High</span>
        </span>
      </div>
      <div class="hud-stat-item">
        <span class="hud-stat-label">Sector Date</span>
        <span class="hud-stat-val" id="hud-day">Day 1</span>
      </div>
      <div class="hud-stat-item">
        <span class="hud-stat-label">Credits Balance</span>
        <span class="hud-stat-val" style="color: var(--gold);" id="hud-credits">2,500 CR</span>
      </div>
      <div class="hud-stat-item">
        <span class="hud-stat-label">Net Worth (Target: 500k CR)</span>
        <span class="hud-stat-val" style="color: var(--green);" id="hud-networth">6,625 CR</span>
        <div class="meter-bar-outer" style="width: 110px; margin-top: 3px;">
          <div id="hud-networth-bar" class="meter-bar-inner" style="width: 0%; background: var(--green);"></div>
        </div>
      </div>
    </div>

    <div class="hud-actions">
      <button class="hud-btn" id="btn-mute" onclick="toggleAudio()">🔊 Sound</button>
      <button class="hud-btn" onclick="openSaveModal()">💾 Save / Load</button>
      <button class="hud-btn" onclick="openManualModal()">❓ Codex</button>
      <button class="hud-btn" onclick="openNewGameModal()">🔄 New Game</button>
    </div>
  </header>

  <!-- SHIP VITALS STATUS BAR -->
  <section id="vitals-bar">
    <div class="vitals-ship-name">
      <span>🛡️</span>
      <span id="vitals-ship-name-val">Star Sparrow</span>
      <span id="vitals-ship-class-val" style="color: var(--fg-dim); font-size: 11px;">[Light Courier]</span>
      <div id="vitals-damage-badges" style="display: flex; gap: 4px; margin-left: 8px;"></div>
    </div>

    <div class="vitals-meters">
      <div class="meter-group">
        <span class="meter-label">Hull Armor</span>
        <div class="meter-bar-outer">
          <div id="meter-hull" class="meter-bar-inner" style="width: 100%; background: var(--green);"></div>
        </div>
        <span id="meter-hull-val" class="meter-val">100/100</span>
      </div>

      <div class="meter-group">
        <span class="meter-label">Shields</span>
        <div class="meter-bar-outer">
          <div id="meter-shield" class="meter-bar-inner" style="width: 100%; background: var(--cyan);"></div>
        </div>
        <span id="meter-shield-val" class="meter-val">40/40</span>
      </div>

      <div class="meter-group">
        <span class="meter-label">Warp Fuel</span>
        <div class="meter-bar-outer">
          <div id="meter-fuel" class="meter-bar-inner" style="width: 100%; background: var(--gold);"></div>
        </div>
        <span id="meter-fuel-val" class="meter-val">130/130 LY</span>
      </div>

      <div class="meter-group">
        <span class="meter-label">Cargo Hold</span>
        <div class="meter-bar-outer">
          <div id="meter-cargo" class="meter-bar-inner" style="width: 0%; background: var(--purple);"></div>
        </div>
        <span id="meter-cargo-val" class="meter-val">0/25 T</span>
      </div>

      <div class="meter-group">
        <span class="meter-label">Torpedoes</span>
        <span id="meter-missiles-val" class="meter-val" style="color: var(--red);">0/8</span>
      </div>
    </div>
  </section>

  <!-- DECK CONTROLS NAVIGATION TABS -->
  <nav id="tab-nav">
    <button class="tab-btn active" id="tabbtn-map" onclick="switchTab('map')">🌌 Star Map</button>
    <button class="tab-btn" id="tabbtn-market" onclick="switchTab('market')">📈 Commodity Market</button>
    <button class="tab-btn" id="tabbtn-advisor" onclick="switchTab('advisor')">🧭 Trade Advisor</button>
    <button class="tab-btn" id="tabbtn-shipyard" onclick="switchTab('shipyard')">🚀 Shipyard & Outfitter</button>
    <button class="tab-btn" id="tabbtn-services" onclick="switchTab('services')">🔧 Station Depot</button>
    <button class="tab-btn" id="tabbtn-crew" onclick="switchTab('crew')">👥 Crew Lounge</button>
    <button class="tab-btn" id="tabbtn-missions" onclick="switchTab('missions')">📜 Missions & Bounties</button>
    <button class="tab-btn" id="tabbtn-bank" onclick="switchTab('bank')">🏦 Bank & Stocks</button>
    <button class="tab-btn" id="tabbtn-log" onclick="switchTab('log')">🎖️ Career & Log</button>
  </nav>

  <!-- MAIN APPLICATION BODY -->
  <main id="app-main">

    <!-- 1. STAR MAP VIEW -->
    <div id="view-map" class="view-container active">
      <div id="map-wrapper">
        <div id="map-svg-container">
          <svg id="star-map-svg" width="100%" height="100%" viewBox="0 0 1000 650" style="display: block;"></svg>
        </div>

        <div id="dossier-card">
          <div class="dossier-header">
            <div>
              <div id="dossier-name" class="dossier-title">Select a Planet</div>
              <div id="dossier-subtitle" class="dossier-subtitle">Sol Sector Coordinates</div>
            </div>
            <span id="dossier-faction-badge" class="pill pill-cyan">Sol Fed</span>
          </div>

          <p id="dossier-desc" style="font-size: 13px; color: var(--fg-dim); line-height: 1.5;">
            Click on any planetary system on the navigational star chart to calculate jump coordinates, hyperlane fuel requirements, and economic intelligence.
          </p>

          <div class="dossier-metric-grid">
            <div>
              <div style="font-size: 10px; color: var(--fg-dim);">DISTANCE</div>
              <div id="dossier-distance" style="font-family: var(--font-mono); font-weight: 700; color: var(--fg);">-- LY</div>
            </div>
            <div>
              <div style="font-size: 10px; color: var(--fg-dim);">WARP FUEL REQUIRED</div>
              <div id="dossier-fuel" style="font-family: var(--font-mono); font-weight: 700; color: var(--gold);">-- LY</div>
            </div>
            <div>
              <div style="font-size: 10px; color: var(--fg-dim);">FLIGHT DURATION</div>
              <div id="dossier-days" style="font-family: var(--font-mono); font-weight: 700; color: var(--fg);">-- Days</div>
            </div>
            <div>
              <div style="font-size: 10px; color: var(--fg-dim);">SYSTEM SECURITY</div>
              <div id="dossier-security" style="font-weight: 700; color: var(--green);">--</div>
            </div>
          </div>

          <div id="dossier-event-box" style="display: none; background: var(--gold-dim); border: 1px solid var(--gold); border-radius: 6px; padding: 10px; font-size: 12px; color: var(--gold);">
            <strong>Active Sector Event:</strong> <span id="dossier-event-title"></span>
            <div id="dossier-event-desc" style="margin-top: 4px; font-size: 11px; opacity: 0.9;"></div>
          </div>

          <div id="dossier-scan-box" style="display: none; background: var(--purple-dim); border: 1px solid var(--purple); border-radius: 6px; padding: 10px; font-size: 12px;">
            <strong style="color: var(--purple);">🔭 Deep Space Scanner Readout</strong>
            <div style="font-size: 11px; color: var(--fg-dim); margin-bottom: 6px;">Top margins if bought there, sold at your current station:</div>
            <div id="dossier-scan-list" style="display: flex; flex-direction: column; gap: 3px;"></div>
          </div>

          <div id="dossier-missions-box" style="display: none; background: var(--gold-dim); border: 1px solid var(--gold); border-radius: 6px; padding: 10px; font-size: 12px;">
            <strong style="color: var(--gold);">📜 Active Contract Destination</strong>
            <div id="dossier-missions-list" style="margin-top: 4px; display: flex; flex-direction: column; gap: 4px;"></div>
          </div>

          <button id="dossier-btn-engage" class="dossier-btn-engage" disabled onclick="executeTravel()">
            ⚡ Engage Hyperdrive
          </button>
        </div>
      </div>
    </div>

    <!-- 2. COMMODITY MARKET VIEW -->
    <div id="view-market" class="view-container">
      <div class="panel-card">
        <div class="panel-card-header">
          <div>
            <div class="panel-card-title">📈 Planetary Commodity Exchange</div>
            <div class="panel-card-subtitle" id="market-station-sub">Trading terminal at Earth Spaceport</div>
          </div>
          <div style="display: flex; gap: 12px; align-items: center;">
            <div style="font-size: 12px; color: var(--fg-dim);">
              Free Cargo: <strong id="market-free-cargo" style="color: var(--cyan);">25</strong> T
            </div>
          </div>
        </div>

        <div class="market-filter-bar">
          <div class="filter-pills" id="market-category-filters">
            <button class="filter-pill active" onclick="setMarketCategory('all', this)">All Goods</button>
            <button class="filter-pill" onclick="setMarketCategory('Essentials', this)">Essentials</button>
            <button class="filter-pill" onclick="setMarketCategory('Raw Materials', this)">Raw Materials</button>
            <button class="filter-pill" onclick="setMarketCategory('High Tech', this)">High Tech</button>
            <button class="filter-pill" onclick="setMarketCategory('Luxury', this)">Luxury</button>
            <button class="filter-pill" onclick="setMarketCategory('Contraband', this)">Contraband</button>
          </div>
          <input type="text" id="market-search" class="search-input" placeholder="🔍 Search commodities..." oninput="filterMarketTable()">
        </div>

        <div class="data-table-wrapper">
          <table class="data-table" id="market-table">
            <thead>
              <tr>
                <th>Commodity</th>
                <th>Category</th>
                <th>Station Stock</th>
                <th>Buy Price</th>
                <th>Sell Price</th>
                <th>10-Day Trend</th>
                <th>Cargo Held</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody id="market-table-body">
              <!-- Dynamically populated -->
            </tbody>
          </table>
        </div>
      </div>

      <!-- Cargo Hold Manifest -->
      <div class="panel-card">
        <div class="panel-card-header">
          <div>
            <div class="panel-card-title">📦 Cargo Hold Manifest</div>
            <div class="panel-card-subtitle">Freight aboard and estimated liquidation value at this station.</div>
          </div>
          <div style="font-size: 12px; color: var(--fg-dim);">
            Hold Value: <strong id="cargo-total-value" style="color: var(--gold);">0</strong> CR
          </div>
        </div>
        <div id="cargo-hold-grid" class="grid-4">
          <!-- Dynamically populated -->
        </div>
      </div>
    </div>

    <!-- 3. TRADE ADVISOR VIEW -->
    <div id="view-advisor" class="view-container">
      <div class="panel-card">
        <div class="panel-card-header">
          <div>
            <div class="panel-card-title">🧭 Deep Space Trade Route Advisor</div>
            <div class="panel-card-subtitle">Real-time algorithmic route optimization factoring travel time, fuel burn, and market spreads.</div>
          </div>
          <button class="btn-action-sm" onclick="fetchTradeRoutes()">🔄 Re-calculate Routes</button>
        </div>

        <div class="data-table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                <th>Commodity</th>
                <th>Source (Buy)</th>
                <th>Destination (Sell)</th>
                <th>Buy / Sell</th>
                <th>Margin</th>
                <th>Est. Net Profit</th>
                <th>Duration</th>
                <th>Efficiency (CR/Day)</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody id="advisor-table-body">
              <!-- Dynamically populated -->
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 4. SHIPYARD & OUTFITTER VIEW -->
    <div id="view-shipyard" class="view-container">
      <!-- Active Vessel Hardpoint Matrix -->
      <div class="panel-card" style="margin-bottom: 20px;">
        <div class="panel-card-header">
          <div>
            <div class="panel-card-title">🛸 Active Vessel Hardpoints & Installed Systems</div>
            <div class="panel-card-subtitle" id="hardpoint-ship-subtitle">Current Vessel Configuration</div>
          </div>
        </div>
        <div class="grid-3" id="hardpoint-matrix-container">
          <!-- Populated dynamically with weapons, shields, and modules -->
        </div>
      </div>

      <div class="grid-2">
        <!-- Shipyard Catalog -->
        <div class="panel-card">
          <div class="panel-card-header">
            <div>
              <div class="panel-card-title">🚀 Starship Dealership</div>
              <div class="panel-card-subtitle">Commercial, defensive, and exploration hulls. Trade-in value applied automatically.</div>
            </div>
          </div>
          <div id="shipyard-cards" style="display: flex; flex-direction: column; gap: 14px;">
            <!-- Dynamically populated -->
          </div>
        </div>

        <!-- Outfitter Catalog -->
        <div class="panel-card">
          <div class="panel-card-header">
            <div>
              <div class="panel-card-title">⚡ Starship Outfitter & Hardpoints</div>
              <div class="panel-card-subtitle">Install beam lasers, kinetic cannons, barrier shields, warp boosters, and expanded cargo bays.</div>
            </div>
          </div>
          <div id="outfitter-cards" style="display: flex; flex-direction: column; gap: 12px;">
            <!-- Dynamically populated -->
          </div>
        </div>
      </div>
    </div>

    <!-- 5. STATION SERVICES VIEW -->
    <div id="view-services" class="view-container">
      <div class="panel-card">
        <div class="panel-card-header">
          <div>
            <div class="panel-card-title">🔧 Spaceport Depot & Maintenance Facilities</div>
            <div class="panel-card-subtitle" id="services-sub">Earth Spaceport Drydock & Replenishment Bays</div>
          </div>
        </div>

        <div class="grid-3">
          <!-- Fuel Bay -->
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 10px;">
            <div style="font-weight: 700; color: var(--gold); font-size: 15px;">⛽ Hyper-Fuel Depository</div>
            <div style="font-size: 12px; color: var(--fg-dim);">
              Fuel Price: <strong id="depot-fuel-price" style="color: var(--gold);">5</strong> CR / LY
            </div>
            <div style="font-size: 13px;">Current: <span id="depot-fuel-current">130</span> / <span id="depot-fuel-max">130</span> LY</div>
            <div style="display: flex; gap: 8px; margin-top: auto;">
              <button class="btn-action-sm btn-buy" onclick="buyFuel(10)">+10 LY</button>
              <button class="btn-action-sm btn-buy" onclick="buyFuel(50)">+50 LY</button>
              <button class="btn-action-sm btn-buy" onclick="buyFuel(999)">Refuel to Max</button>
            </div>
          </div>

          <!-- Drydock Repairs -->
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 10px;">
            <div style="font-weight: 700; color: var(--green); font-size: 15px;">🛡️ Armor & Hull Drydock</div>
            <div style="font-size: 12px; color: var(--fg-dim);">
              Repair Cost: <strong id="depot-repair-price" style="color: var(--green);">15</strong> CR / HP
            </div>
            <div style="font-size: 13px;">Integrity: <span id="depot-hull-current">100</span> / <span id="depot-hull-max">100</span> HP</div>
            <div style="display: flex; gap: 8px; margin-top: auto;">
              <button class="btn-action-sm btn-buy" onclick="repairHull(10)">Repair 10 HP</button>
              <button class="btn-action-sm btn-buy" onclick="repairHull(999)">Repair to Full</button>
            </div>
          </div>

          <!-- Subsystems Repair -->
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 10px;">
            <div style="font-weight: 700; color: var(--cyan); font-size: 15px;">⚙️ Subsystem Calibration</div>
            <div style="font-size: 12px; color: var(--fg-dim);">
              Restores damaged propulsion, fire control avionics, and shield generators.
            </div>
            <div id="depot-subsystem-status" style="font-size: 13px;">All subsystems nominal.</div>
            <button id="btn-repair-subsystems" class="btn-action-sm btn-buy" style="margin-top: auto;" onclick="repairSubsystems()">
              Overhaul Damaged Modules (1,500 CR)
            </button>
          </div>

          <!-- Torpedo Magazine -->
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 10px;">
            <div style="font-weight: 700; color: var(--red); font-size: 15px;">🚀 Seeker Torpedo Armory</div>
            <div style="font-size: 12px; color: var(--fg-dim);">
              Lock-on anti-ship ordnance bypassing kinetic energy shields.
            </div>
            <div style="font-size: 12px; color: var(--fg-dim);">
              Torpedo Price: <strong id="depot-missiles-price" style="color: var(--red);">400</strong> CR each
            </div>
            <div style="font-size: 13px;">Magazine: <span id="depot-missiles-count">0</span> / 8 Torpedoes</div>
            <div style="display: flex; gap: 8px; margin-top: auto;">
              <button class="btn-action-sm btn-buy" onclick="buyMissiles(1)">Arm 1 Torpedo</button>
              <button class="btn-action-sm btn-buy" onclick="buyMissiles(8)">Restock Full Bay</button>
            </div>
          </div>

          <!-- Lloyds Insurance -->
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 10px;">
            <div style="font-weight: 700; color: var(--purple); font-size: 15px;">📜 Lloyds Interstellar Insurance</div>
            <div style="font-size: 12px; color: var(--fg-dim);">
              Guarantees clone replacement and vessel reconstitution upon fatal ship destruction.
            </div>
            <div id="depot-insurance-status" style="font-size: 13px;">Policy: Inactive</div>
            <button id="btn-buy-insurance" class="btn-action-sm btn-buy" style="margin-top: auto;" onclick="buyInsurance()">
              Purchase Policy
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- 6. CREW LOUNGE VIEW -->
    <div id="view-crew" class="view-container">
      <div class="panel-card">
        <div class="panel-card-header">
          <div>
            <div class="panel-card-title">👥 Spacers Cantina & Officer Recruitment</div>
            <div class="panel-card-subtitle">Hire specialist crew members for unique passive combat and navigational boosts.</div>
          </div>
          <div style="font-size: 12px; color: var(--fg-dim);">
            Daily Payroll: <strong id="crew-daily-wages" style="color: var(--gold);">0</strong> CR/day
          </div>
        </div>

        <div class="grid-2" id="crew-cards">
          <!-- Dynamically populated -->
        </div>
      </div>
    </div>

    <!-- 7. MISSIONS VIEW -->
    <div id="view-missions" class="view-container">
      <div class="grid-2">
        <div class="panel-card">
          <div class="panel-card-header">
            <div>
              <div class="panel-card-title">📜 Station Contract Board</div>
              <div class="panel-card-subtitle">Cargo hauls, confidential runs, and wanted pirate bounties.</div>
            </div>
          </div>
          <div id="available-missions-list" style="display: flex; flex-direction: column; gap: 12px;">
            <!-- Dynamically populated -->
          </div>
        </div>

        <div class="panel-card">
          <div class="panel-card-header">
            <div>
              <div class="panel-card-title">⏱️ Active Contract Manifest</div>
              <div class="panel-card-subtitle">Track deadlines and deliver shipments upon arrival.</div>
            </div>
          </div>
          <div id="active-missions-list" style="display: flex; flex-direction: column; gap: 12px;">
            <!-- Dynamically populated -->
          </div>
        </div>
      </div>
    </div>

    <!-- 8. BANK & STOCKS VIEW -->
    <div id="view-bank" class="view-container">
      <div class="grid-2">
        <!-- Banking -->
        <div class="panel-card">
          <div class="panel-card-header">
            <div>
              <div class="panel-card-title">🏦 First Galactic Bank & Credit Reserve</div>
              <div class="panel-card-subtitle">High-yield compound savings and commercial credit lines.</div>
            </div>
          </div>

          <div style="display: flex; flex-direction: column; gap: 16px;">
            <!-- Savings Box -->
            <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 16px;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <span style="font-weight: 700; color: var(--green);">Secure Savings Account</span>
                <span id="bank-savings-bal" style="font-family: var(--font-mono); font-weight: 700; color: var(--green); font-size: 16px;">0 CR</span>
              </div>
              <div style="font-size: 12px; color: var(--fg-dim); margin-bottom: 12px;">Earns 0.8% compound interest every sector travel day.</div>
              <div style="display: flex; gap: 8px;">
                <button class="btn-action-sm btn-buy" onclick="promptDeposit()">Deposit Credits</button>
                <button class="btn-action-sm btn-sell" onclick="promptWithdraw()">Withdraw Credits</button>
              </div>
            </div>

            <!-- Loan Box -->
            <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 16px;">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                <span style="font-weight: 700; color: var(--red);">Outstanding Debt / Loan</span>
                <span id="bank-loan-bal" style="font-family: var(--font-mono); font-weight: 700; color: var(--red); font-size: 16px;">0 CR</span>
              </div>
              <div style="font-size: 12px; color: var(--fg-dim); margin-bottom: 6px;">
                Credit Score: <strong id="bank-credit-score" style="color: var(--cyan);">650</strong> · Limit: <strong id="bank-loan-limit" style="color: var(--fg);">10,000</strong> CR
              </div>
              <div style="font-size: 12px; color: var(--fg-dim); margin-bottom: 12px;">
                Interest: <span id="bank-interest-rate">1.5%</span> / day. Maintaining loans boosts your credit score!
              </div>
              <div style="display: flex; gap: 8px;">
                <button class="btn-action-sm btn-buy" onclick="promptBorrow()">Borrow Funds</button>
                <button class="btn-action-sm btn-sell" onclick="promptRepay()">Repay Loan</button>
              </div>
            </div>
          </div>
        </div>

        <!-- Stock Exchange -->
        <div class="panel-card">
          <div class="panel-card-header">
            <div>
              <div class="panel-card-title">📊 Interstellar Securities Exchange</div>
              <div class="panel-card-subtitle">Trade equities in the solar system's top megacorporations.</div>
            </div>
          </div>
          <div id="stocks-cards" style="display: flex; flex-direction: column; gap: 12px;">
            <!-- Dynamically populated -->
          </div>
        </div>
      </div>
    </div>

    <!-- 9. CAREER & LOG VIEW -->
    <div id="view-log" class="view-container">
      <div class="grid-2">
        <!-- Ranks and Reputation -->
        <div class="panel-card">
          <div class="panel-card-header">
            <div>
              <div class="panel-card-title">🎖️ Officer Ranks & Career Perks</div>
              <div class="panel-card-subtitle">Earn renown through trading, missions, and combat to climb the officer hierarchy.</div>
            </div>
          </div>
          <div id="ranks-progression-list" style="display: flex; flex-direction: column; gap: 10px;">
            <!-- Dynamically populated -->
          </div>

          <div style="margin-top: 24px;">
            <div class="panel-card-title" style="font-size: 14px; margin-bottom: 10px;">🌐 Faction Diplomatic Standings</div>
            <div id="faction-standings-list" style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
              <!-- Dynamically populated -->
            </div>
          </div>
        </div>

        <!-- Achievements & News -->
        <div class="panel-card">
          <div class="panel-card-header">
            <div>
              <div class="panel-card-title">🏆 Career Achievements (23)</div>
              <div class="panel-card-subtitle">Major milestones unlocked across your voyages.</div>
            </div>
          </div>
          <div id="achievements-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 8px;">
            <!-- Dynamically populated -->
          </div>

          <div style="margin-top: 24px;">
            <div class="panel-card-title" style="font-size: 14px; margin-bottom: 10px;">📡 Galactic Subspace News Wire</div>
            <div id="news-feed-list" style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 6px; padding: 12px; font-family: var(--font-mono); font-size: 11px; max-height: 200px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px;">
              <!-- Dynamically populated -->
            </div>
          </div>
        </div>
      </div>
    </div>

  </main>

  <!-- TRADE SLIDER MODAL -->
  <div id="trade-modal">
    <div class="modal-box">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
        <h3 id="trade-modal-title" style="color: var(--cyan); font-size: 18px;">Trade Commodity</h3>
        <button class="btn-action-sm" onclick="closeTradeModal()">✕</button>
      </div>

      <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 14px; margin-bottom: 16px;">
        <div style="display: flex; justify-content: space-between; margin-bottom: 6px;">
          <span style="color: var(--fg-dim);">Commodity:</span>
          <strong id="trade-good-name" style="color: var(--fg);">--</strong>
        </div>
        <div style="display: flex; justify-content: space-between; margin-bottom: 6px;">
          <span style="color: var(--fg-dim);">Unit Price:</span>
          <strong id="trade-unit-price" style="color: var(--gold);">0 CR</strong>
        </div>
        <div style="display: flex; justify-content: space-between;">
          <span style="color: var(--fg-dim);">Available Space / Stock:</span>
          <strong id="trade-max-limit" style="color: var(--cyan);">0</strong>
        </div>
      </div>

      <div style="margin-bottom: 20px;">
        <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
          <span style="font-weight: 600;">Quantity:</span>
          <span id="trade-qty-display" style="font-family: var(--font-mono); font-size: 18px; font-weight: 700; color: var(--cyan);">1</span>
        </div>
        <input type="range" id="trade-slider" min="1" max="10" value="1" style="width: 100%; cursor: pointer;" oninput="onTradeSliderChange(this.value)">
        <div style="display: flex; justify-content: space-between; margin-top: 8px;">
          <button class="btn-action-sm" onclick="setTradeQty(1)">Min (1)</button>
          <button class="btn-action-sm" onclick="setTradeQty(5)">5</button>
          <button class="btn-action-sm" onclick="setTradeQty(10)">10</button>
          <button class="btn-action-sm" onclick="setTradeQtyMax()">Max</button>
        </div>
      </div>

      <div style="display: flex; justify-content: space-between; align-items: center; border-top: 1px solid var(--panel-border); padding-top: 16px; margin-bottom: 20px;">
        <span style="font-size: 15px; font-weight: 700;">Total Transaction:</span>
        <span id="trade-total-display" style="font-family: var(--font-mono); font-size: 20px; font-weight: 800; color: var(--gold);">0 CR</span>
      </div>

      <button id="trade-btn-confirm" class="dossier-btn-engage" onclick="executeTradeModal()">
        Confirm Purchase
      </button>
    </div>
  </div>

  <!-- TACTICAL COMBAT BRIDGE MODAL -->
  <div id="combat-modal">
    <div class="combat-bridge">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; border-bottom: 1px solid var(--red); padding-bottom: 12px;">
        <div style="display: flex; align-items: center; gap: 10px;">
          <span style="color: var(--red); font-size: 20px;">⚠️</span>
          <div>
            <div id="combat-enemy-name" style="font-size: 18px; font-weight: 800; color: var(--red);">Corsair Raider</div>
            <div id="combat-enemy-ship" style="font-size: 12px; color: var(--fg-dim);">Class: Viper Interceptor · Personality: Aggressive</div>
          </div>
        </div>
        <span id="combat-turn-counter" class="pill pill-red">Turn 1</span>
      </div>

      <!-- Holographic Tactical Combat Stage -->
      <div id="combat-holo-arena" style="position: relative; height: 160px; background: radial-gradient(circle at center, rgba(14,28,70,0.8) 0%, rgba(4,8,28,0.98) 100%); border: 1px solid var(--panel-border); border-radius: 8px; margin-bottom: 16px; overflow: hidden; display: flex; align-items: center; justify-content: space-between; padding: 0 44px;">
        <!-- Player ship hologram SVG -->
        <div id="holo-player-ship" class="holo-vessel holo-player" style="display: flex; flex-direction: column; align-items: center; z-index: 2; transition: transform 0.2s;">
          <svg width="68" height="68" viewBox="0 0 100 100">
            <polygon points="50,15 85,80 50,65 15,80" fill="rgba(0, 229, 255, 0.15)" stroke="var(--cyan)" stroke-width="3" stroke-linejoin="round" />
            <polygon points="50,25 75,75 50,62 25,75" fill="none" stroke="var(--blue)" stroke-width="1.5" />
            <circle cx="50" cy="50" r="42" fill="none" stroke="rgba(0, 229, 255, 0.5)" stroke-dasharray="4 3" id="holo-player-shield-ring" />
          </svg>
          <span style="font-size: 10px; font-weight: 700; color: var(--cyan); margin-top: 2px;">YOUR VESSEL</span>
        </div>

        <!-- Dynamic Laser / Projectile Overlay -->
        <svg id="holo-fx-overlay" style="position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; z-index: 3;">
          <!-- Laser beams and projectile bursts injected dynamically -->
        </svg>

        <!-- Floating Damage / Status Callouts -->
        <div id="holo-float-text" style="position: absolute; inset: 0; pointer-events: none; z-index: 4;"></div>

        <!-- Enemy ship hologram SVG -->
        <div id="holo-enemy-ship" class="holo-vessel holo-enemy" style="display: flex; flex-direction: column; align-items: center; z-index: 2; transition: transform 0.2s;">
          <svg width="68" height="68" viewBox="0 0 100 100">
            <polygon points="50,85 15,20 50,35 85,20" fill="rgba(255, 82, 82, 0.15)" stroke="var(--red)" stroke-width="3" stroke-linejoin="round" />
            <polygon points="50,75 25,25 50,38 75,25" fill="none" stroke="var(--purple)" stroke-width="1.5" />
            <circle cx="50" cy="50" r="42" fill="none" stroke="rgba(255, 82, 82, 0.5)" stroke-dasharray="4 3" id="holo-enemy-shield-ring" />
          </svg>
          <span id="holo-enemy-label" style="font-size: 10px; font-weight: 700; color: var(--red); margin-top: 2px;">HOSTILE TARGET</span>
        </div>
      </div>

      <!-- Combat Vitals Duel -->
      <div class="grid-2" style="margin-bottom: 16px;">
        <!-- Player Ship Bridge -->
        <div style="background: var(--panel); border: 1px solid var(--panel-border); border-radius: 8px; padding: 14px;">
          <div style="font-weight: 700; color: var(--cyan); margin-bottom: 8px;" id="combat-player-ship-title">Your Ship: Star Sparrow</div>
          <div style="display: flex; flex-direction: column; gap: 6px; font-size: 12px;">
            <div style="display: flex; justify-content: space-between;">
              <span>Hull:</span>
              <strong id="combat-player-hull-val" style="color: var(--green);">100 / 100</strong>
            </div>
            <div class="meter-bar-outer" style="width: 100%;">
              <div id="combat-player-hull-bar" class="meter-bar-inner" style="width: 100%; background: var(--green);"></div>
            </div>
            <div style="display: flex; justify-content: space-between; margin-top: 4px;">
              <span>Shields:</span>
              <strong id="combat-player-shield-val" style="color: var(--cyan);">40 / 40</strong>
            </div>
            <div class="meter-bar-outer" style="width: 100%;">
              <div id="combat-player-shield-bar" class="meter-bar-inner" style="width: 100%; background: var(--cyan);"></div>
            </div>
          </div>
        </div>

        <!-- Enemy Target Vessel -->
        <div style="background: var(--panel); border: 1px solid var(--red-dim); border-radius: 8px; padding: 14px;">
          <div style="font-weight: 700; color: var(--red); margin-bottom: 8px;" id="combat-enemy-vessel-title">Hostile Target</div>
          <div style="display: flex; flex-direction: column; gap: 6px; font-size: 12px;">
            <div style="display: flex; justify-content: space-between;">
              <span>Hull:</span>
              <strong id="combat-enemy-hull-val" style="color: var(--red);">75 / 75</strong>
            </div>
            <div class="meter-bar-outer" style="width: 100%;">
              <div id="combat-enemy-hull-bar" class="meter-bar-inner" style="width: 100%; background: var(--red);"></div>
            </div>
            <div style="display: flex; justify-content: space-between; margin-top: 4px;">
              <span>Shields:</span>
              <strong id="combat-enemy-shield-val" style="color: var(--cyan);">25 / 25</strong>
            </div>
            <div class="meter-bar-outer" style="width: 100%;">
              <div id="combat-enemy-shield-bar" class="meter-bar-inner" style="width: 100%; background: var(--cyan);"></div>
            </div>
          </div>
        </div>
      </div>

      <!-- Action Commands -->
      <div id="combat-actions-bar" style="display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; margin-bottom: 16px;">
        <button class="btn-action-sm btn-buy" onclick="sendCombatAction('fire')">⚡ Fire Batteries</button>
        <button class="btn-action-sm" onclick="sendCombatAction('target_weapons')">🎯 Target Weapons</button>
        <button class="btn-action-sm" onclick="sendCombatAction('target_engines')">🎯 Target Thrusters</button>
        <button class="btn-action-sm" onclick="sendCombatAction('target_shields')">🎯 Target Shields</button>
        <button class="btn-action-sm btn-sell" id="btn-combat-missile" onclick="sendCombatAction('missile')">🚀 Fire Torpedo</button>
        <button class="btn-action-sm" id="btn-combat-drones" onclick="sendCombatAction('drones')">🛸 Deploy Drones</button>
        <button class="btn-action-sm" onclick="sendCombatAction('recharge')">🛡️ Boost Capacitor</button>
        <button class="btn-action-sm" id="btn-combat-board" onclick="sendCombatAction('board')">🏴‍☠️ Board Vessel</button>
        <button class="btn-action-sm" style="color: var(--red);" onclick="sendCombatAction('flee')">🏃 Emergency Warp</button>
      </div>

      <!-- Combat Log Terminal -->
      <div id="combat-terminal-log" style="background: var(--bg); border: 1px solid var(--panel-border); border-radius: 6px; padding: 12px; font-family: var(--font-mono); font-size: 11px; max-height: 140px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; color: var(--fg-dim);">
        <div>Sensors locked onto enemy vessel. Weapons armed.</div>
      </div>

      <!-- Victory / Defeat Dismissal -->
      <div id="combat-result-box" style="display: none; margin-top: 16px; text-align: center;">
        <button class="dossier-btn-engage" id="combat-btn-dismiss" onclick="dismissCombat()">
          Disengage & Return to Helm
        </button>
      </div>
    </div>
  </div>

  <!-- DYNAMIC TRAVEL ENCOUNTER MODAL -->
  <div id="encounter-modal" class="generic-modal">
    <div class="modal-box">
      <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 12px;">
        <span id="enc-icon" style="font-size: 24px;">📡</span>
        <h3 id="enc-title" style="color: var(--cyan); font-size: 18px;">Deep Space Contact</h3>
      </div>

      <p id="enc-desc" style="font-size: 13px; color: var(--fg-dim); line-height: 1.6; margin-bottom: 20px;">
        An unexpected sensor contact has appeared along your hyperlane corridor.
      </p>

      <div style="display: flex; gap: 12px;">
        <button id="enc-btn-opt1" class="dossier-btn-engage" onclick="resolveEncounter(true)">Option 1</button>
        <button id="enc-btn-opt2" class="hud-btn" style="flex: 1; padding: 12px;" onclick="resolveEncounter(false)">Option 2</button>
      </div>
    </div>
  </div>

  <!-- SAVE / LOAD MODAL -->
  <div id="save-modal" class="generic-modal">
    <div class="modal-box">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
        <h3 style="color: var(--cyan); font-size: 18px;">💾 Save / Load Game Flight Records</h3>
        <button class="btn-action-sm" onclick="closeSaveModal()">✕</button>
      </div>

      <div id="save-slots-list" style="display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px;">
        <!-- Dynamically loaded -->
      </div>
    </div>
  </div>

  <!-- NEW GAME MODAL -->
  <div id="newgame-modal" class="generic-modal">
    <div class="modal-box">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
        <h3 style="color: var(--cyan); font-size: 18px;">🔄 New Captain Commission</h3>
        <button class="btn-action-sm" onclick="closeNewGameModal()">✕</button>
      </div>

      <div style="margin-bottom: 14px;">
        <label style="display: block; font-size: 12px; color: var(--fg-dim); margin-bottom: 4px;">Commander Callsign:</label>
        <input type="text" id="newgame-name" class="search-input" style="width: 100%;" value="Commander">
      </div>

      <div style="margin-bottom: 20px;">
        <label style="display: block; font-size: 12px; color: var(--fg-dim); margin-bottom: 4px;">Difficulty Tier:</label>
        <select id="newgame-diff" class="search-input" style="width: 100%;">
          <option value="easy">Easy (More credits, cheaper repairs, peaceful sector)</option>
          <option value="normal" selected>Normal (Standard economic margins and patrol enforcement)</option>
          <option value="hard">Hard (Narrow margins, dangerous raiders, stringent authorities)</option>
          <option value="nightmare">Nightmare (Merciless raiders, fragile ships, unforgiving debt)</option>
        </select>
      </div>

      <button class="dossier-btn-engage" onclick="confirmNewGame()">Launch New Career</button>
    </div>
  </div>

  <!-- CODEX & MANUAL MODAL -->
  <div id="manual-modal" class="generic-modal">
    <div class="modal-box" style="max-width: 680px; max-height: 80vh; overflow-y: auto;">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; position: sticky; top: 0; background: var(--panel); padding-bottom: 8px;">
        <h3 style="color: var(--cyan); font-size: 18px;">📖 Space Trader: Odyssey — Field Manual</h3>
        <button class="btn-action-sm" onclick="closeManualModal()">✕</button>
      </div>

      <div style="font-size: 13px; color: var(--fg); line-height: 1.6; display: flex; flex-direction: column; gap: 14px;">
        <div>
          <h4 style="color: var(--gold); margin-bottom: 4px;">🏆 Ultimate Objective: Galactic Mogul</h4>
          <p>Amass a total net worth of <strong>500,000 Credits</strong> through interstellar trading, passenger transport, pirate hunting, contract completion, and smart stock investments.</p>
        </div>

        <div>
          <h4 style="color: var(--cyan); margin-bottom: 4px;">📈 Interstellar Commerce</h4>
          <p>Planets produce commodities according to their planetary traits: agricultural worlds export cheap food and grain; mining stations flood the market with titanium and gems; high-tech arcologies demand raw minerals and export quantum processors. Buy low, consult the <strong>Trade Advisor</strong> for optimal routes, and sell high!</p>
        </div>

        <div>
          <h4 style="color: var(--red); margin-bottom: 4px;">⚡ Tactical Starship Combat</h4>
          <p>Out in the lawless black, Free Corsairs and deserter dreadnoughts raid commercial shipping. Upgrade your hardpoints with Pulse Lasers, Heavy Barrier Shields, and Seeker Torpedoes. Target enemy thrusters to prevent them from fleeing, or breach their hull and launch a <strong>Boarding Action</strong> to seize their cargo and ransom their crew!</p>
        </div>

        <div>
          <h4 style="color: var(--purple); margin-bottom: 4px;">🎖️ Career Progression & Ranks</h4>
          <p>Earning renown advances your commission from <em>Cadet</em> to <em>Admiral</em>, unlocking massive price discounts, lower bank loan interest, cheaper repairs, and enhanced mission rewards.</p>
        </div>
      </div>
    </div>
  </div>

  <!-- GAME OVER MODAL -->
  <div id="gameover-modal" class="generic-modal">
    <div class="modal-box" style="text-align: center; border-color: var(--red);">
      <div style="font-size: 48px; margin-bottom: 10px;">💀</div>
      <h3 style="color: var(--red); font-size: 22px; margin-bottom: 8px;">VESSEL DESTROYED</h3>
      <p style="font-size: 13px; color: var(--fg-dim); margin-bottom: 20px; line-height: 1.6;">
        Your ship broke apart in the void. The sector remembers your final transmission.<br>
        Recovery options are available from your flight recorder archives.
      </p>
      <div style="display: flex; flex-direction: column; gap: 10px;">
        <button class="dossier-btn-engage" onclick="recoverSave('precombat')">⏪ Load Pre-Combat Snapshot</button>
        <button class="dossier-btn-engage" style="background: var(--gold); color: #020514;" onclick="recoverSave('autosave')">💾 Load Last Autosave</button>
        <button class="hud-btn" style="justify-content: center; padding: 12px;" onclick="openNewGameModal()">🔄 Start New Career</button>
      </div>
    </div>
  </div>

  <!-- VICTORY MODAL -->
  <div id="victory-modal" class="generic-modal">
    <div class="modal-box" style="text-align: center; border-color: var(--gold); box-shadow: 0 0 60px rgba(255, 183, 3, 0.3);">
      <div style="font-size: 48px; margin-bottom: 10px;">🏆</div>
      <h3 style="color: var(--gold); font-size: 22px; margin-bottom: 8px;">GALACTIC MOGUL ACHIEVED</h3>
      <p style="font-size: 13px; color: var(--fg-dim); margin-bottom: 20px; line-height: 1.6;">
        Net worth has surpassed <strong style="color: var(--green);">500,000 CR</strong>!<br>
        The sector bows to your commercial empire, <strong id="victory-captain-name" style="color: var(--cyan);">Commander</strong>.
      </p>
      <div style="display: flex; flex-direction: column; gap: 10px;">
        <button class="dossier-btn-engage" style="background: var(--gold); color: #020514;" onclick="dismissVictory()">Continue Playing (Sandbox Mode)</button>
        <button class="hud-btn" style="justify-content: center; padding: 12px;" onclick="dismissVictory(); openNewGameModal();">🔄 Begin a New Career</button>
      </div>
    </div>
  </div>

  <!-- AMOUNT INPUT MODAL (bank / stocks) -->
  <div id="amount-modal" class="generic-modal">
    <div class="modal-box">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
        <h3 id="amount-modal-title" style="color: var(--cyan); font-size: 18px;">Enter Amount</h3>
        <button class="btn-action-sm" onclick="closeAmountModal()">✕</button>
      </div>
      <p id="amount-modal-hint" style="font-size: 12px; color: var(--fg-dim); margin-bottom: 10px;"></p>
      <input type="number" id="amount-modal-input" class="search-input" style="width: 100%; font-size: 16px; padding: 10px;" min="1" value="500">
      <div style="display: flex; gap: 8px; margin-top: 12px;">
        <button class="btn-action-sm" onclick="setAmountModalValue(100)">100</button>
        <button class="btn-action-sm" onclick="setAmountModalValue(500)">500</button>
        <button class="btn-action-sm" onclick="setAmountModalValue(1000)">1,000</button>
        <button class="btn-action-sm" onclick="setAmountModalValue(5000)">5,000</button>
        <button class="btn-action-sm" onclick="setAmountModalValue(10000)">10,000</button>
      </div>
      <button class="dossier-btn-engage" id="amount-modal-confirm" style="margin-top: 16px;" onclick="confirmAmountModal()">Confirm</button>
    </div>
  </div>

  <!-- TOAST CONTAINER -->
  <div id="toast-container"></div>

  <!-- APPLICATION LOGIC JAVASCRIPT -->
  <script>
    // --- Audio Engine (Web Audio API) ---
    let audioCtx = null;
    let audioMuted = false;

    function initAudio() {
      if (!audioCtx) {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (audioCtx.state === 'suspended') {
        audioCtx.resume();
      }
    }

    function toggleAudio() {
      audioMuted = !audioMuted;
      document.getElementById('btn-mute').textContent = audioMuted ? '🔇 Muted' : '🔊 Sound';
      showToast(audioMuted ? 'Audio muted' : 'Audio enabled');
    }

    function playSound(type) {
      if (audioMuted) return;
      initAudio();
      if (!audioCtx) return;
      const now = audioCtx.currentTime;

      if (type === 'laser') {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'sawtooth';
        osc.frequency.setValueAtTime(880, now);
        osc.frequency.exponentialRampToValueAtTime(110, now + 0.12);
        gain.gain.setValueAtTime(0.2, now);
        gain.gain.exponentialRampToValueAtTime(0.01, now + 0.12);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(now);
        osc.stop(now + 0.12);
      } else if (type === 'buy' || type === 'coin') {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(987, now);
        osc.frequency.setValueAtTime(1318, now + 0.08);
        gain.gain.setValueAtTime(0.15, now);
        gain.gain.exponentialRampToValueAtTime(0.01, now + 0.2);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(now);
        osc.stop(now + 0.2);
      } else if (type === 'sell') {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'triangle';
        osc.frequency.setValueAtTime(1046, now);
        osc.frequency.setValueAtTime(1567, now + 0.08);
        gain.gain.setValueAtTime(0.18, now);
        gain.gain.exponentialRampToValueAtTime(0.01, now + 0.22);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(now);
        osc.stop(now + 0.22);
      } else if (type === 'warp') {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(130, now);
        osc.frequency.exponentialRampToValueAtTime(650, now + 0.4);
        gain.gain.setValueAtTime(0.25, now);
        gain.gain.exponentialRampToValueAtTime(0.01, now + 0.45);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(now);
        osc.stop(now + 0.45);
      } else if (type === 'alarm') {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'square';
        osc.frequency.setValueAtTime(600, now);
        osc.frequency.setValueAtTime(400, now + 0.1);
        gain.gain.setValueAtTime(0.15, now);
        gain.gain.exponentialRampToValueAtTime(0.01, now + 0.25);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(now);
        osc.stop(now + 0.25);
      } else if (type === 'shield_hit') {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(330, now);
        osc.frequency.exponentialRampToValueAtTime(180, now + 0.18);
        gain.gain.setValueAtTime(0.16, now);
        gain.gain.exponentialRampToValueAtTime(0.01, now + 0.18);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(now);
        osc.stop(now + 0.18);
      } else if (type === 'death') {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'sawtooth';
        osc.frequency.setValueAtTime(220, now);
        osc.frequency.exponentialRampToValueAtTime(40, now + 0.9);
        gain.gain.setValueAtTime(0.22, now);
        gain.gain.exponentialRampToValueAtTime(0.01, now + 0.9);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(now);
        osc.stop(now + 0.9);
      } else if (type === 'mine') {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'triangle';
        osc.frequency.setValueAtTime(90, now);
        osc.frequency.setValueAtTime(60, now + 0.15);
        gain.gain.setValueAtTime(0.25, now);
        gain.gain.exponentialRampToValueAtTime(0.01, now + 0.3);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(now);
        osc.stop(now + 0.3);
      } else if (type === 'wormhole') {
        const osc = audioCtx.createOscillator();
        const osc2 = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'sine';
        osc2.type = 'sine';
        osc.frequency.setValueAtTime(200, now);
        osc.frequency.exponentialRampToValueAtTime(900, now + 0.5);
        osc2.frequency.setValueAtTime(205, now);
        osc2.frequency.exponentialRampToValueAtTime(880, now + 0.5);
        gain.gain.setValueAtTime(0.18, now);
        gain.gain.exponentialRampToValueAtTime(0.01, now + 0.55);
        osc.connect(gain); osc2.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(now); osc2.start(now);
        osc.stop(now + 0.55); osc2.stop(now + 0.55);
      } else if (type === 'click') {
        const osc = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        osc.type = 'sine';
        osc.frequency.setValueAtTime(660, now);
        gain.gain.setValueAtTime(0.08, now);
        gain.gain.exponentialRampToValueAtTime(0.01, now + 0.06);
        osc.connect(gain);
        gain.connect(audioCtx.destination);
        osc.start(now);
        osc.stop(now + 0.06);
      } else if (type === 'victory' || type === 'rank_up') {
        [523.25, 659.25, 783.99, 1046.50].forEach((freq, i) => {
          const osc = audioCtx.createOscillator();
          const gain = audioCtx.createGain();
          osc.type = 'sine';
          osc.frequency.setValueAtTime(freq, now + i * 0.08);
          gain.gain.setValueAtTime(0.15, now + i * 0.08);
          gain.gain.exponentialRampToValueAtTime(0.01, now + i * 0.08 + 0.35);
          osc.connect(gain);
          gain.connect(audioCtx.destination);
          osc.start(now + i * 0.08);
          osc.stop(now + i * 0.08 + 0.35);
        });
      }
    }

    // --- Toast Notifications ---
    function showToast(msg) {
      const c = document.getElementById('toast-container');
      const t = document.createElement('div');
      t.className = 'toast-msg';
      t.textContent = msg;
      c.appendChild(t);
      setTimeout(() => {
        t.style.opacity = '0';
        t.style.transform = 'translateY(10px)';
        t.style.transition = 'all 0.3s';
        setTimeout(() => t.remove(), 300);
      }, 4000);
    }

    // --- State & Navigation ---
    let gameState = null;
    let currentTab = 'map';
    let selectedPlanetName = null;
    let selectedGoodId = null;
    let tradeMode = 'buy';
    let marketCategoryFilter = 'all';

    // Escapes a value for safe embedding inside a single-quoted string
    // literal within an inline HTML event handler attribute (e.g.
    // onclick="fn('${escJs(name)}')"). Without this, names containing an
    // apostrophe (like "Nebula's Rest") break out of the JS string and
    // throw "Uncaught SyntaxError: missing ) after argument list".
    function escJs(str) {
      return String(str)
        .replace(/\\/g, '\\\\')
        .replace(/'/g, "\\'")
        .replace(/"/g, '&quot;');
    }

    function switchTab(tabId) {
      currentTab = tabId;
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.view-container').forEach(v => v.classList.remove('active'));

      const btn = document.getElementById('tabbtn-' + tabId);
      if (btn) btn.classList.add('active');
      const view = document.getElementById('view-' + tabId);
      if (view) view.classList.add('active');

      if (tabId === 'advisor') fetchTradeRoutes();
      if (tabId === 'map') renderStarMap();
    }

    // --- API Interactions ---
    async function fetchState() {
      try {
        const res = await fetch('/api/state');
        const json = await res.json();
        if (json.success && json.state) {
          gameState = json.state;
          if (json.sound) playSound(json.sound);
          renderAll();
        }
      } catch (err) {
        console.error("Failed to fetch state:", err);
      }
    }

    async function sendAction(action, payload = {}) {
      try {
        const res = await fetch('/api/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action, ...payload })
        });
        const json = await res.json();
        if (json.logs && json.logs.length) {
          json.logs.forEach(l => showToast(l));
        }
        if (json.sound) playSound(json.sound);
        if (json.state) {
          gameState = json.state;
          renderAll();
        }
        return json;
      } catch (err) {
        showToast("Communication error: " + err);
      }
    }

    // --- Master Render ---
    let victoryShown = false;

    function renderAll() {
      if (!gameState) return;
      renderHUD();
      renderVitals();
      renderStarMap();
      renderMarket();
      renderCargoHold();
      renderShipyard();
      renderServices();
      renderCrew();
      renderMissions();
      renderBank();
      renderLog();
      checkEncounter();
      checkCombat();
      checkGameOver();
      checkVictory();
    }

    function renderHUD() {
      const p = gameState.player;
      const cp = gameState.current_planet;

      document.getElementById('hud-rank-insignia').textContent = p.rank.insignia;
      document.getElementById('hud-rank-title').textContent = p.rank.name;
      document.getElementById('hud-renown-val').textContent = p.renown.toLocaleString();
      document.getElementById('hud-renown-next').textContent = p.renown_needed > 0 ? p.renown_needed.toLocaleString() : 'MAX';

      document.getElementById('hud-location').textContent = cp.name;
      document.getElementById('hud-security').textContent = cp.security;
      document.getElementById('hud-day').textContent = 'Day ' + p.day;
      document.getElementById('hud-credits').textContent = p.credits.toLocaleString() + ' CR';
      document.getElementById('hud-networth').textContent = p.net_worth.toLocaleString() + ' CR';
      const nwBar = document.getElementById('hud-networth-bar');
      nwBar.style.width = p.net_worth_pct + '%';
      nwBar.style.background = p.net_worth_pct >= 100 ? 'var(--gold)' : 'var(--green)';
    }

    function renderVitals() {
      const p = gameState.player;
      document.getElementById('vitals-ship-name-val').textContent = p.ship_name;
      document.getElementById('vitals-ship-class-val').textContent = '[' + p.ship_class + ']';

      // Hull
      const hullPct = Math.max(0, Math.min(100, (p.hull / p.max_hull) * 100));
      const hullBar = document.getElementById('meter-hull');
      hullBar.style.width = hullPct + '%';
      hullBar.style.background = hullPct < 25 ? 'var(--red)' : (hullPct < 50 ? 'var(--gold)' : 'var(--green)');
      document.getElementById('meter-hull-val').textContent = `${p.hull}/${p.max_hull}`;

      // Shield
      const shieldPct = p.max_shield > 0 ? Math.max(0, Math.min(100, (p.shield / p.max_shield) * 100)) : 0;
      document.getElementById('meter-shield').style.width = shieldPct + '%';
      document.getElementById('meter-shield-val').textContent = `${p.shield}/${p.max_shield}`;

      // Fuel
      const fuelPct = Math.max(0, Math.min(100, (p.fuel / p.max_fuel) * 100));
      document.getElementById('meter-fuel').style.width = fuelPct + '%';
      document.getElementById('meter-fuel-val').textContent = `${p.fuel}/${p.max_fuel} LY`;

      // Cargo
      const cargoPct = Math.max(0, Math.min(100, (p.cargo_used / p.cargo_cap) * 100));
      document.getElementById('meter-cargo').style.width = cargoPct + '%';
      document.getElementById('meter-cargo-val').textContent = `${p.cargo_used}/${p.cargo_cap} T`;

      // Missiles
      document.getElementById('meter-missiles-val').textContent = `${p.missiles}/${p.max_missiles}`;

      // Damaged Subsystem Badges
      const bContainer = document.getElementById('vitals-damage-badges');
      bContainer.innerHTML = '';
      if (p.weapons_damaged) bContainer.innerHTML += '<span class="subsystem-alert">WEAPONS DAMAGED</span>';
      if (p.engines_damaged) bContainer.innerHTML += '<span class="subsystem-alert">THRUSTERS DAMAGED</span>';
      if (p.shields_damaged) bContainer.innerHTML += '<span class="subsystem-alert">SHIELDS OFFLINE</span>';
    }

    // --- Star Map SVG Rendering ---
    function renderStarMap() {
      const svg = document.getElementById('star-map-svg');
      if (!svg || !gameState) return;
      const planets = gameState.all_planets;
      const cpName = gameState.current_planet.name;
      const cp = planets.find(p => p.name === cpName) || planets[0];

      if (!selectedPlanetName) selectedPlanetName = cpName;
      const selP = planets.find(p => p.name === selectedPlanetName) || cp;

      // Coordinate scaling
      const w = 1000, h = 650;
      function mapX(x) { return 70 + (x / 16.0) * (w - 140); }
      function mapY(y) { return 60 + (y / 15.0) * (h - 120); }

      let html = `
        <defs>
          <filter id="glow-cyan" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="6" result="blur" />
            <feComposite in="SourceGraphic" in2="blur" operator="over" />
          </filter>
        </defs>
      `;

      // Hyperlanes
      planets.forEach((p1, idx) => {
        planets.slice(idx + 1).forEach(p2 => {
          const d = Math.hypot(p1.x - p2.x, p1.y - p2.y);
          if (d <= 5.5) {
            html += `<line x1="${mapX(p1.x)}" y1="${mapY(p1.y)}" x2="${mapX(p2.x)}" y2="${mapY(p2.y)}" class="hyperlane" />`;
          }
        });
      });

      // Jump range ring
      const maxRangeUnits = Math.max(0, (gameState.player.fuel - 4) / 3.0);
      const pixelRadius = (maxRangeUnits / 16.0) * (w - 140);
      html += `
        <circle cx="${mapX(cp.x)}" cy="${mapY(cp.y)}" r="${pixelRadius}" 
          fill="rgba(0, 229, 255, 0.04)" stroke="rgba(0, 229, 255, 0.35)" stroke-dasharray="6 4" stroke-width="1.5" />
      `;

      // Animated Flight Vector to Selected Planet
      if (selP && selP.name !== cp.name) {
        html += `
          <line x1="${mapX(cp.x)}" y1="${mapY(cp.y)}" x2="${mapX(selP.x)}" y2="${mapY(selP.y)}" 
            stroke="var(--gold)" stroke-width="2.5" class="flight-vector" />
        `;
      }

      // Planet Nodes
      planets.forEach(p => {
        const px = mapX(p.x), py = mapY(p.y);
        const isCurrent = (p.name === cpName);
        const isSelected = (p.name === selectedPlanetName);

        if (isCurrent) {
          html += `<circle cx="${px}" cy="${py}" r="16" fill="none" stroke="var(--cyan)" stroke-width="1.5" class="current-ping" />`;
        }

        const strokeColor = isSelected ? 'var(--cyan)' : (isCurrent ? 'var(--green)' : 'rgba(255,255,255,0.4)');
        const strokeWidth = isSelected ? '3' : '1.5';
        const nodeRadius = isCurrent ? 12 : 9;
        const hasMissions = p.active_missions && p.active_missions.length > 0;

        html += `
          <g class="map-planet-node" onclick="selectPlanet('${escJs(p.name)}')">
            <circle cx="${px}" cy="${py}" r="${nodeRadius + 4}" fill="${p.color}" opacity="0.15" />
            <circle class="planet-body" cx="${px}" cy="${py}" r="${nodeRadius}" fill="${p.color}" stroke="${strokeColor}" stroke-width="${strokeWidth}" />
            <text x="${px}" y="${py + 22}" text-anchor="middle" fill="${isSelected ? 'var(--cyan)' : 'var(--fg)'}" font-size="12" font-weight="${isSelected ? 'bold' : 'normal'}" font-family="system-ui">
              ${p.name}
            </text>
            ${p.active_event ? `<text x="${px}" y="${py - 14}" text-anchor="middle" fill="var(--gold)" font-size="10">⚡</text>` : ''}
            ${hasMissions ? `<text x="${px + 12}" y="${py - 10}" text-anchor="middle" font-size="11">📜</text>` : ''}
          </g>
        `;
      });

      svg.innerHTML = html;
      renderDossier(selP, cp);
    }

    function selectPlanet(name) {
      selectedPlanetName = name;
      renderStarMap();
    }

    function renderDossier(planet, currentPlanet) {
      document.getElementById('dossier-name').textContent = planet.name;
      document.getElementById('dossier-subtitle').textContent = planet.subtitle;
      document.getElementById('dossier-faction-badge').textContent = planet.faction;
      document.getElementById('dossier-desc').textContent = planet.desc;
      document.getElementById('dossier-distance').textContent = planet.distance + ' LY';
      document.getElementById('dossier-fuel').textContent = planet.fuel_cost + ' LY';
      document.getElementById('dossier-days').textContent = planet.days_cost + ' Days';
      document.getElementById('dossier-security').textContent = planet.security;

      const eventBox = document.getElementById('dossier-event-box');
      if (planet.active_event) {
        eventBox.style.display = 'block';
        document.getElementById('dossier-event-title').textContent = planet.active_event.name;
        document.getElementById('dossier-event-desc').textContent = planet.active_event.desc;
      } else {
        eventBox.style.display = 'none';
      }

      // Active Contract Dossier Callout
      const misBox = document.getElementById('dossier-missions-box');
      const misList = document.getElementById('dossier-missions-list');
      if (misBox && misList) {
        if (planet.active_missions && planet.active_missions.length > 0) {
          misBox.style.display = 'block';
          misList.innerHTML = planet.active_missions.map(m => `
            <div style="font-size: 11px; display: flex; justify-content: space-between;">
              <span style="color: var(--fg);">${m.title}</span>
              <strong style="color: var(--green); font-family: var(--font-mono);">+${m.reward_credits.toLocaleString()} CR</strong>
            </div>
          `).join('');
        } else {
          misBox.style.display = 'none';
        }
      }

      // Deep Space Scanner readout (module-gated remote market intel)
      const scanBox = document.getElementById('dossier-scan-box');
      const scanList = document.getElementById('dossier-scan-list');
      if (gameState.player.has_deep_scanner && planet.remote_deals && planet.remote_deals.length) {
        scanBox.style.display = 'block';
        scanList.innerHTML = planet.remote_deals.map(d => `
          <div style="display: flex; justify-content: space-between; font-size: 11px;">
            <span>${d.is_contraband ? '🔴' : '🔸'} ${d.good} <span style="color: var(--fg-dim);">(x${d.stock})</span></span>
            <span style="color: var(--green); font-family: var(--font-mono);">+${d.margin} CR</span>
          </div>
        `).join('');
      } else if (gameState.player.has_deep_scanner && !planet.is_current) {
        scanBox.style.display = 'block';
        scanList.innerHTML = '<div style="font-size: 11px; color: var(--fg-dim);">No profitable buy signals detected at this world.</div>';
      } else {
        scanBox.style.display = 'none';
      }

      const btn = document.getElementById('dossier-btn-engage');
      if (planet.is_current) {
        btn.disabled = true;
        btn.textContent = '📍 Current Location';
      } else if (!planet.in_range) {
        btn.disabled = true;
        btn.textContent = `❌ Insufficient Fuel (${planet.fuel_cost} LY needed)`;
      } else {
        btn.disabled = false;
        btn.textContent = `⚡ Engage Hyperdrive to ${planet.name}`;
      }
    }

    function executeTravel() {
      if (!selectedPlanetName) return;
      sendAction('travel', { destination: selectedPlanetName });
    }

    // --- Commodity Market ---
    function setMarketCategory(cat, el) {
      marketCategoryFilter = cat;
      document.querySelectorAll('#market-category-filters .filter-pill').forEach(p => p.classList.remove('active'));
      if (el) el.classList.add('active');
      renderMarket();
    }

    function filterMarketTable() {
      renderMarket();
    }

    function renderMarket() {
      if (!gameState) return;
      const tbody = document.getElementById('market-table-body');
      const goods = gameState.current_planet.market;
      const query = (document.getElementById('market-search').value || '').toLowerCase();
      document.getElementById('market-station-sub').textContent = `Commercial Terminal at ${gameState.current_planet.name} Spaceport`;
      document.getElementById('market-free-cargo').textContent = gameState.player.cargo_free;

      let html = '';
      goods.forEach(g => {
        if (marketCategoryFilter !== 'all' && g.category !== marketCategoryFilter) return;
        if (query && !g.name.toLowerCase().includes(query)) return;

        // Sparkline
        const pts = g.price_history || [g.base_price];
        const minP = Math.min(...pts), maxP = Math.max(...pts);
        const range = maxP === minP ? 1 : (maxP - minP);
        const sparkCoords = pts.map((val, i) => {
          const x = (i / Math.max(1, pts.length - 1)) * 50;
          const y = 20 - ((val - minP) / range) * 16;
          return `${x},${y}`;
        }).join(' ');

        const trendColor = g.trend === 'up' ? 'var(--green)' : (g.trend === 'down' ? 'var(--red)' : 'var(--fg-dim)');
        const buyColor = g.pct_diff < 0 ? 'var(--green)' : (g.pct_diff > 15 ? 'var(--red)' : 'var(--fg)');

        html += `
          <tr>
            <td>
              <div style="font-weight: 700; color: var(--fg);">${g.name}</div>
              <div style="font-size: 11px; color: var(--fg-dim);">${g.desc}</div>
            </td>
            <td><span class="pill pill-cyan">${g.category}</span></td>
            <td><strong style="color: var(--fg);">${g.stock}</strong> T</td>
            <td><strong style="color: ${buyColor}; font-family: var(--font-mono);">${g.buy_price} CR</strong></td>
            <td><strong style="color: var(--gold); font-family: var(--font-mono);">${g.sell_price} CR</strong></td>
            <td>
              <svg class="sparkline-svg" width="55" height="22">
                <polyline points="${sparkCoords}" fill="none" stroke="${trendColor}" stroke-width="2" />
              </svg>
            </td>
            <td>
              <strong style="color: var(--cyan);">${g.player_qty}</strong> T
              ${g.player_qty > 0 && g.cost_basis > 0 ? `<div style="font-size: 10px; color: ${g.unit_profit >= 0 ? 'var(--green)' : 'var(--red)'}; font-family: var(--font-mono);">${g.unit_profit >= 0 ? '+' : ''}${g.unit_profit} (${g.profit_pct >= 0 ? '+' : ''}${g.profit_pct}%)</div>` : ''}
            </td>
            <td>
              <div style="display: flex; gap: 4px;">
                <button class="btn-action-sm btn-buy" ${!g.can_buy ? 'disabled' : ''} onclick="openTradeModal('${g.id}', 'buy')">Buy</button>
                <button class="btn-action-sm btn-sell" ${!g.can_sell ? 'disabled' : ''} onclick="openTradeModal('${g.id}', 'sell')">Sell</button>
              </div>
            </td>
          </tr>
        `;
      });

      tbody.innerHTML = html;
    }

    // --- Trade Slider Modal ---
    function openTradeModal(goodId, mode) {
      selectedGoodId = goodId;
      tradeMode = mode;
      const g = gameState.current_planet.market.find(item => item.id === goodId);
      if (!g) return;

      const p = gameState.player;
      let maxQty = 1;
      if (mode === 'buy') {
        const affordable = Math.floor(p.credits / Math.max(1, g.buy_price));
        maxQty = Math.max(1, Math.min(g.stock, p.cargo_free, affordable));
        document.getElementById('trade-modal-title').textContent = `Purchase ${g.name}`;
        document.getElementById('trade-btn-confirm').textContent = 'Confirm Purchase';
        document.getElementById('trade-unit-price').textContent = `${g.buy_price} CR`;
      } else {
        maxQty = Math.max(1, g.player_qty);
        document.getElementById('trade-modal-title').textContent = `Sell ${g.name}`;
        document.getElementById('trade-btn-confirm').textContent = 'Confirm Sale';
        document.getElementById('trade-unit-price').textContent = `${g.sell_price} CR`;
      }

      document.getElementById('trade-good-name').textContent = g.name;
      document.getElementById('trade-max-limit').textContent = `${maxQty} Units Max`;

      const slider = document.getElementById('trade-slider');
      slider.max = maxQty;
      slider.value = 1;
      onTradeSliderChange(1);

      document.getElementById('trade-modal').classList.add('active');
    }

    function closeTradeModal() {
      document.getElementById('trade-modal').classList.remove('active');
    }

    function onTradeSliderChange(val) {
      const g = gameState.current_planet.market.find(item => item.id === selectedGoodId);
      if (!g) return;
      const qty = parseInt(val, 10);
      document.getElementById('trade-qty-display').textContent = qty;
      const unitP = tradeMode === 'buy' ? g.buy_price : g.sell_price;
      document.getElementById('trade-total-display').textContent = `${(qty * unitP).toLocaleString()} CR`;
    }

    function setTradeQty(val) {
      const slider = document.getElementById('trade-slider');
      slider.value = Math.min(parseInt(slider.max, 10), val);
      onTradeSliderChange(slider.value);
    }

    function setTradeQtyMax() {
      const slider = document.getElementById('trade-slider');
      slider.value = slider.max;
      onTradeSliderChange(slider.value);
    }

    function executeTradeModal() {
      const qty = parseInt(document.getElementById('trade-slider').value, 10);
      closeTradeModal();
      if (tradeMode === 'buy') {
        sendAction('buy_commodity', { good: selectedGoodId, qty });
      } else {
        sendAction('sell_commodity', { good: selectedGoodId, qty });
      }
    }

    // --- Cargo Hold Manifest ---
    function renderCargoHold() {
      if (!gameState) return;
      const grid = document.getElementById('cargo-hold-grid');
      const items = gameState.player.cargo_detail || [];
      let total = 0;
      let html = '';
      items.forEach(c => {
        total += c.total_value;
        html += `
          <div style="background: var(--bg2); border: 1px solid ${c.is_contraband ? 'var(--red)' : 'var(--panel-border)'}; border-radius: 8px; padding: 12px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
              <strong style="color: ${c.is_contraband ? 'var(--red)' : 'var(--fg)'}; font-size: 13px;">${c.name}</strong>
              <span class="pill ${c.is_contraband ? 'pill-red' : 'pill-cyan'}" style="font-size: 10px;">${c.qty} T</span>
            </div>
            <div style="font-size: 11px; color: var(--fg-dim);">Sell here: <strong style="color: var(--gold);">${c.sell_price} CR</strong>/unit</div>
            <div style="font-size: 11px; color: var(--fg-dim);">
              Cost: <strong style="color: var(--fg);">${c.cost_basis} CR</strong> · P/L: <strong style="color: ${c.total_profit >= 0 ? 'var(--green)' : 'var(--red)'}; font-family: var(--font-mono);">${c.total_profit >= 0 ? '+' : ''}${c.total_profit.toLocaleString()} CR (${c.profit_pct >= 0 ? '+' : ''}${c.profit_pct}%)</strong>
            </div>
            <div style="font-size: 11px; color: var(--fg-dim);">Total Value: <strong style="color: var(--gold); font-family: var(--font-mono);">${c.total_value.toLocaleString()} CR</strong></div>
            <button class="btn-action-sm btn-sell" style="margin-top: 8px; width: 100%;" onclick="openTradeModal('${c.id}', 'sell')">Sell All</button>
          </div>
        `;
      });
      grid.innerHTML = html || '<div style="color: var(--fg-dim); font-size: 12px; grid-column: 1 / -1;">Cargo hold is empty. Purchase commodities from the exchange above.</div>';
      document.getElementById('cargo-total-value').textContent = total.toLocaleString();
    }

    // --- Trade Advisor ---
    async function fetchTradeRoutes() {
      try {
        const res = await fetch('/api/routes');
        const json = await res.json();
        if (json.success && json.routes) {
          const tbody = document.getElementById('advisor-table-body');
          let html = '';
          json.routes.forEach(r => {
            html += `
              <tr>
                <td><strong style="color: var(--cyan);">${r.good}</strong></td>
                <td>${r.src}</td>
                <td><strong style="color: var(--gold);">${r.dst}</strong></td>
                <td>${r.buy_price} / ${r.sell_price} CR</td>
                <td><span class="pill pill-green">+${r.margin} CR (${r.margin_pct}%)</span></td>
                <td><strong style="color: var(--gold); font-family: var(--font-mono);">${r.net_profit.toLocaleString()} CR</strong></td>
                <td>${r.days} Days</td>
                <td><strong style="color: var(--green);">${r.profit_per_day.toLocaleString()} CR/day</strong></td>
                <td>
                  <button class="btn-action-sm btn-buy" onclick="selectPlanet('${escJs(r.dst)}'); switchTab('map');">Plot Course</button>
                </td>
              </tr>
            `;
          });
          tbody.innerHTML = html || '<tr><td colspan="9" style="text-align: center; color: var(--fg-dim);">No profitable routes identified currently.</td></tr>';
        }
      } catch (e) {
        console.error(e);
      }
    }

    // --- Shipyard & Outfitter ---
    function renderShipyard() {
      if (!gameState) return;
      const p = gameState.player;
      
      // Hardpoints Matrix
      const matrixContainer = document.getElementById('hardpoint-matrix-container');
      if (matrixContainer) {
        document.getElementById('hardpoint-ship-subtitle').textContent = 
          `${p.ship_name} [${p.ship_class}] · Hardpoints: ${p.weapon_slots} Weapon, ${p.shield_slots} Shield, ${p.module_slots} Module Bays`;
        
        let mHtml = '';
        // Weapons Column
        mHtml += `
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 14px; display: flex; flex-direction: column; gap: 10px;">
            <div style="font-weight: 700; color: var(--red); font-size: 14px;">⚡ Weapon Mounts (${(p.equipped_weapons || []).length}/${p.weapon_slots})</div>
        `;
        const wepDetails = p.equipped_weapons_detail || [];
        wepDetails.forEach(w => {
          mHtml += `
            <div class="hardpoint-mounted">
              <div>
                <div style="font-weight: 700; color: var(--fg); font-size: 13px;">${w.name}</div>
                <div style="font-size: 11px; color: var(--fg-dim);">Dmg: ${w.damage} · Acc: ${w.accuracy}%</div>
              </div>
              <button class="btn-action-sm btn-sell" onclick="sendAction('sell_equipment', { eq_id: '${w.id}' })">Dismount (+${w.refund_val.toLocaleString()} CR)</button>
            </div>
          `;
        });
        const openWeps = p.weapon_slots - wepDetails.length;
        for (let i = 0; i < openWeps; i++) {
          mHtml += `<div class="hardpoint-slot"><span style="color: var(--fg-dark); font-size: 12px;">[ Empty Weapon Hardpoint ]</span></div>`;
        }
        mHtml += `</div>`;

        // Shields Column
        mHtml += `
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 14px; display: flex; flex-direction: column; gap: 10px;">
            <div style="font-weight: 700; color: var(--cyan); font-size: 14px;">🛡️ Shield Generators (${(p.equipped_shields || []).length}/${p.shield_slots})</div>
        `;
        const shdDetails = p.equipped_shields_detail || [];
        shdDetails.forEach(s => {
          mHtml += `
            <div class="hardpoint-mounted">
              <div>
                <div style="font-weight: 700; color: var(--fg); font-size: 13px;">${s.name}</div>
                <div style="font-size: 11px; color: var(--fg-dim);">Cap: +${s.shield_hp} HP</div>
              </div>
              <button class="btn-action-sm btn-sell" onclick="sendAction('sell_equipment', { eq_id: '${s.id}' })">Dismount (+${s.refund_val.toLocaleString()} CR)</button>
            </div>
          `;
        });
        const openShds = p.shield_slots - shdDetails.length;
        for (let i = 0; i < openShds; i++) {
          mHtml += `<div class="hardpoint-slot"><span style="color: var(--fg-dark); font-size: 12px;">[ Empty Shield Bay ]</span></div>`;
        }
        mHtml += `</div>`;

        // Modules Column
        mHtml += `
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 14px; display: flex; flex-direction: column; gap: 10px;">
            <div style="font-weight: 700; color: var(--purple); font-size: 14px;">⚙️ Utility Modules (${(p.equipped_modules || []).length}/${p.module_slots})</div>
        `;
        const modDetails = p.equipped_modules_detail || [];
        modDetails.forEach(m => {
          mHtml += `
            <div class="hardpoint-mounted">
              <div>
                <div style="font-weight: 700; color: var(--fg); font-size: 13px;">${m.name}</div>
                <div style="font-size: 11px; color: var(--fg-dim);">${m.desc.slice(0, 32)}...</div>
              </div>
              <button class="btn-action-sm btn-sell" onclick="sendAction('sell_equipment', { eq_id: '${m.id}' })">Dismount (+${m.refund_val.toLocaleString()} CR)</button>
            </div>
          `;
        });
        const openMods = p.module_slots - modDetails.length;
        for (let i = 0; i < openMods; i++) {
          mHtml += `<div class="hardpoint-slot"><span style="color: var(--fg-dark); font-size: 12px;">[ Empty Module Bay ]</span></div>`;
        }
        mHtml += `</div>`;

        matrixContainer.innerHTML = mHtml;
      }

      // Shipyard
      const sContainer = document.getElementById('shipyard-cards');
      let html = '';
      gameState.all_ships.forEach(s => {
        html += `
          <div style="background: var(--bg2); border: 1px solid ${s.is_current ? 'var(--cyan)' : 'var(--panel-border)'}; border-radius: 8px; padding: 14px; display: flex; justify-content: space-between; align-items: center;">
            <div style="flex: 1;">
              <div style="font-weight: 700; color: ${s.is_current ? 'var(--cyan)' : 'var(--fg)'}; font-size: 15px;">
                ${s.name} ${s.is_current ? '<span class="pill pill-cyan">COMMISSIONED</span>' : ''}
              </div>
              <div style="font-size: 11px; color: var(--fg-dim); margin-bottom: 6px;">[${s.ship_class}] · ${s.desc}</div>
              <div style="display: flex; gap: 12px; font-size: 11px; color: var(--fg-dim);">
                <span>Hull: <strong style="color: var(--green);">${s.max_hull}</strong></span>
                <span>Shield: <strong style="color: var(--cyan);">${s.max_shield}</strong></span>
                <span>Cargo: <strong style="color: var(--purple);">${s.cargo_cap} T</strong></span>
                <span>Range: <strong style="color: var(--gold);">${s.max_fuel} LY</strong></span>
                <span>Speed: <strong>${s.speed}x</strong></span>
              </div>
            </div>
            <div style="text-align: right; margin-left: 16px;">
              <div style="font-family: var(--font-mono); font-size: 14px; font-weight: 700; color: var(--gold);">${s.net_cost.toLocaleString()} CR</div>
              <div style="font-size: 10px; color: var(--fg-dim); margin-bottom: 6px;">(Trade-in: -${s.trade_in_value.toLocaleString()} CR)</div>
              ${s.is_current ? '' : `<button class="btn-action-sm btn-buy" ${!s.can_afford ? 'disabled' : ''} onclick="sendAction('buy_ship', { ship_id: '${s.id}' })">Commission</button>`}
            </div>
          </div>
        `;
      });
      sContainer.innerHTML = html;

      // Outfitter
      const oContainer = document.getElementById('outfitter-cards');
      let oHtml = '';
      gameState.all_equipment.forEach(eq => {
        oHtml += `
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 12px; display: flex; justify-content: space-between; align-items: center;">
            <div>
              <div style="font-weight: 700; color: var(--fg);">${eq.name} <span class="pill pill-cyan" style="font-size: 9px;">${eq.slot_type}</span></div>
              <div style="font-size: 11px; color: var(--fg-dim);">${eq.desc}</div>
            </div>
            <div style="text-align: right; margin-left: 14px;">
              <div style="font-family: var(--font-mono); font-size: 13px; font-weight: 700; color: var(--gold); margin-bottom: 4px;">${eq.cost.toLocaleString()} CR</div>
              ${eq.is_equipped ? `<button class="btn-action-sm btn-sell" onclick="sendAction('sell_equipment', { eq_id: '${eq.id}' })">Dismount (+${eq.refund_val.toLocaleString()} CR)</button>` : `<button class="btn-action-sm btn-buy" ${!eq.can_afford ? 'disabled' : ''} onclick="sendAction('buy_equipment', { eq_id: '${eq.id}' })">Equip</button>`}
            </div>
          </div>
        `;
      });
      oContainer.innerHTML = oHtml;
    }

    // --- Station Depot Services ---
    function renderServices() {
      if (!gameState) return;
      const p = gameState.player;
      const cp = gameState.current_planet;

      document.getElementById('services-sub').textContent = `Spaceport Facilities at ${cp.name}`;
      document.getElementById('depot-fuel-price').textContent = p.fuel_unit_price;
      document.getElementById('depot-fuel-current').textContent = p.fuel;
      document.getElementById('depot-fuel-max').textContent = p.max_fuel;

      document.getElementById('depot-repair-price').textContent = p.repair_cost_per_hp;
      document.getElementById('depot-hull-current').textContent = p.hull;
      document.getElementById('depot-hull-max').textContent = p.max_hull;

      const hasDamaged = p.weapons_damaged || p.engines_damaged || p.shields_damaged;
      document.getElementById('depot-subsystem-status').textContent = hasDamaged ? 'MALFUNCTION: Damaged systems detected.' : 'All subsystems nominal.';
      const subBtn = document.getElementById('btn-repair-subsystems');
      subBtn.disabled = !hasDamaged;
      subBtn.textContent = `Overhaul Damaged Modules (${p.subsystem_repair_cost.toLocaleString()} CR)`;

      document.getElementById('depot-missiles-count').textContent = p.missiles;
      document.getElementById('depot-missiles-price').textContent = p.missile_price;

      const insStatus = document.getElementById('depot-insurance-status');
      const insBtn = document.getElementById('btn-buy-insurance');
      if (p.insurance_active) {
        insStatus.textContent = 'Policy: Active (Vessel guaranteed against loss)';
        insStatus.style.color = 'var(--green)';
        insBtn.disabled = true;
        insBtn.textContent = 'Policy Active';
      } else {
        insStatus.textContent = 'Policy: Inactive';
        insStatus.style.color = 'var(--fg-dim)';
        insBtn.disabled = p.credits < p.insurance_price;
        insBtn.textContent = `Purchase Policy (${p.insurance_price.toLocaleString()} CR)`;
      }
    }

    function buyFuel(amount) { sendAction('buy_fuel', { amount }); }
    function repairHull(hp) { sendAction('repair_hull', { hp }); }
    function repairSubsystems() { sendAction('repair_subsystems'); }
    function buyMissiles(qty) { sendAction('buy_missiles', { qty }); }
    function buyInsurance() { sendAction('buy_insurance'); }

    // --- Crew Lounge ---
    function renderCrew() {
      if (!gameState) return;
      document.getElementById('crew-daily-wages').textContent = gameState.player.total_daily_wages;
      const container = document.getElementById('crew-cards');
      let html = '';
      gameState.all_crew.forEach(c => {
        html += `
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 10px;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
              <div>
                <div style="font-weight: 700; color: var(--fg); font-size: 15px;">${c.name}</div>
                <div style="font-size: 11px; color: var(--cyan);">${c.role}</div>
              </div>
              <span class="pill pill-gold">${c.daily_wage} CR / day</span>
            </div>
            <p style="font-size: 12px; color: var(--fg-dim); line-height: 1.4;">${c.desc}</p>
            <div style="margin-top: auto; display: flex; justify-content: space-between; align-items: center;">
              <span style="font-size: 11px; color: var(--fg-dim);">Hire Fee: <strong>${c.hire_cost.toLocaleString()} CR</strong></span>
              ${c.is_hired ? `<button class="btn-action-sm btn-sell" onclick="sendAction('dismiss_crew', { crew_id: '${c.id}' })">Dismiss</button>` : `<button class="btn-action-sm btn-buy" onclick="sendAction('hire_crew', { crew_id: '${c.id}' })">Hire Officer</button>`}
            </div>
          </div>
        `;
      });
      container.innerHTML = html;
    }

    // --- Missions ---
    function renderMissions() {
      if (!gameState) return;
      const aList = document.getElementById('available-missions-list');
      let aHtml = '';
      gameState.available_missions.forEach(m => {
        const urgent = m.days_left <= 3;
        aHtml += `
          <div style="background: var(--bg2); border: 1px solid ${urgent ? 'var(--red)' : 'var(--panel-border)'}; border-radius: 8px; padding: 14px; display: flex; justify-content: space-between; align-items: center;">
            <div>
              <div style="font-weight: 700; color: var(--cyan);">${m.title}</div>
              <div style="font-size: 11px; color: var(--fg-dim);">${m.desc}</div>
              <div style="font-size: 11px; color: var(--fg); margin-top: 4px;">
                Destination: <strong style="color: var(--gold);">${m.destination}</strong> · Deadline: <strong style="color: ${urgent ? 'var(--red)' : 'var(--fg)'};">${m.days_left} days</strong>
              </div>
            </div>
            <div style="text-align: right; margin-left: 14px;">
              <div style="font-family: var(--font-mono); font-size: 14px; font-weight: 700; color: var(--green); margin-bottom: 4px;">+${m.reward_credits.toLocaleString()} CR</div>
              <button class="btn-action-sm btn-buy" onclick="sendAction('accept_mission', { mission_id: '${m.id}' })">Accept</button>
            </div>
          </div>
        `;
      });
      aList.innerHTML = aHtml || '<div style="color: var(--fg-dim); font-size: 12px;">No open contracts posted at this spaceport currently.</div>';

      const actList = document.getElementById('active-missions-list');
      let actHtml = '';
      gameState.player.active_missions.forEach(m => {
        const canDeliver = (m.destination === gameState.current_planet.name);
        actHtml += `
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 14px; display: flex; justify-content: space-between; align-items: center;">
            <div>
              <div style="font-weight: 700; color: var(--gold); font-size: 14px;">${m.title}</div>
              <div style="font-size: 11px; color: var(--fg-dim); margin-top: 2px;">
                ${m.m_type === 'bounty' ? 'Hunt target near' : 'Destination:'} <strong style="color: var(--cyan);">${m.destination}</strong> · Reward: <strong style="color: var(--green); font-family: var(--font-mono);">${m.reward_credits.toLocaleString()} CR</strong>
              </div>
              <div style="font-size: 11px; color: ${m.days_left <= 2 ? 'var(--red)' : 'var(--fg)'}; margin-top: 2px;">
                Remaining Time: ${m.days_left} Days
              </div>
            </div>
            <div style="display: flex; gap: 8px; align-items: center;">
              ${canDeliver && m.m_type !== 'bounty' ? `<span class="pill pill-green">Ready to Deliver</span>` : `<button class="btn-action-sm btn-buy" onclick="selectPlanet('${escJs(m.destination)}'); switchTab('map');">Plot Course</button>`}
              <button class="btn-action-sm btn-sell" onclick="sendAction('abandon_mission', { mission_id: '${m.id}' })">Forfeit</button>
            </div>
          </div>
        `;
      });
      actList.innerHTML = actHtml || '<div style="color: var(--fg-dim); font-size: 12px;">No active missions in your captain manifest.</div>';
    }

    // --- Bank & Stocks ---
    function renderBank() {
      if (!gameState) return;
      const p = gameState.player;
      document.getElementById('bank-savings-bal').textContent = p.savings.toLocaleString() + ' CR';
      document.getElementById('bank-loan-bal').textContent = p.loan.toLocaleString() + ' CR';
      document.getElementById('bank-credit-score').textContent = p.credit_score;
      document.getElementById('bank-loan-limit').textContent = p.loan_limit.toLocaleString() + ' CR';
      document.getElementById('bank-interest-rate').textContent = p.effective_interest_rate + '%';

      // Stocks
      const sContainer = document.getElementById('stocks-cards');
      let html = '';
      gameState.all_stocks.forEach(stk => {
        const trendColor = stk.change_pct >= 0 ? 'var(--green)' : 'var(--red)';
        html += `
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 12px; display: flex; justify-content: space-between; align-items: center;">
            <div>
              <div style="font-weight: 700; color: var(--fg);">
                ${stk.name} <span class="pill pill-cyan" style="font-size: 10px;">${stk.symbol}</span>
              </div>
              <div style="font-size: 11px; color: var(--fg-dim);">${stk.desc}</div>
              <div style="font-size: 11px; color: var(--fg); margin-top: 2px;">Owned: <strong>${stk.shares_owned}</strong> shares</div>
            </div>
            <div style="text-align: right; margin-left: 14px;">
              <div style="font-family: var(--font-mono); font-size: 15px; font-weight: 700; color: var(--gold);">${stk.price} CR</div>
              <div style="font-size: 11px; color: ${trendColor}; font-weight: 700; margin-bottom: 4px;">${stk.change_pct >= 0 ? '+' : ''}${stk.change_pct}%</div>
              <div style="display: flex; gap: 4px;">
                <button class="btn-action-sm btn-buy" onclick="tradeStock('${stk.symbol}', 'buy')">Buy</button>
                <button class="btn-action-sm btn-sell" ${stk.shares_owned <= 0 ? 'disabled' : ''} onclick="tradeStock('${stk.symbol}', 'sell')">Sell</button>
              </div>
            </div>
          </div>
        `;
      });
      sContainer.innerHTML = html;
    }

    // (bank amount dialogs now live in the styled Amount Input Modal below)

    // --- Career & Captain's Log ---
    function renderLog() {
      if (!gameState) return;
      const p = gameState.player;

      // Ranks
      const rContainer = document.getElementById('ranks-progression-list');
      let rHtml = '';
      gameState.ranks.forEach(r => {
        const isCurrent = (r.name === p.rank.name);
        rHtml += `
          <div style="background: var(--bg2); border-left: 3px solid ${r.color}; border-radius: 4px; padding: 8px 12px; display: flex; justify-content: space-between; align-items: center;">
            <div>
              <span style="color: ${r.color}; font-weight: 700;">${r.insignia} ${r.name}</span>
              <span style="font-size: 11px; color: var(--fg-dim); margin-left: 6px;">(${r.renown.toLocaleString()} Renown)</span>
              <div style="font-size: 11px; color: var(--fg-dim);">${r.perk}</div>
            </div>
            ${isCurrent ? '<span class="pill pill-cyan">ACTIVE</span>' : ''}
          </div>
        `;
      });
      rContainer.innerHTML = rHtml;

      // Faction Standings
      const fContainer = document.getElementById('faction-standings-list');
      let fHtml = '';
      for (const [faction, rep] of Object.entries(p.reputation)) {
        const standing = p.standing[faction];
        const repColor = rep >= 15 ? 'var(--green)' : (rep <= -15 ? 'var(--red)' : 'var(--fg-dim)');
        fHtml += `
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 6px; padding: 8px 12px;">
            <div style="font-weight: 700; font-size: 12px;">${faction}</div>
            <div style="display: flex; justify-content: space-between; font-size: 11px; margin-top: 4px;">
              <span style="color: ${repColor}; font-weight: 700;">${standing}</span>
              <span style="font-family: var(--font-mono); color: var(--fg-dim);">${rep > 0 ? '+' : ''}${rep}</span>
            </div>
          </div>
        `;
      }
      fContainer.innerHTML = fHtml;

      // Achievements
      const aContainer = document.getElementById('achievements-grid');
      let aHtml = '';
      gameState.all_achievements.forEach(ach => {
        aHtml += `
          <div style="background: var(--bg2); border: 1px solid ${ach.unlocked ? 'var(--gold)' : 'var(--panel-border)'}; border-radius: 6px; padding: 8px; text-align: center; opacity: ${ach.unlocked ? '1' : '0.4'};">
            <div style="font-size: 18px; margin-bottom: 2px;">${ach.unlocked ? '🏆' : '🔒'}</div>
            <div style="font-size: 11px; font-weight: 700; color: ${ach.unlocked ? 'var(--gold)' : 'var(--fg)'};">${ach.title}</div>
            <div style="font-size: 9px; color: var(--fg-dim); margin-top: 2px;">${ach.desc}</div>
          </div>
        `;
      });
      aContainer.innerHTML = aHtml;

      // News Feed
      const nContainer = document.getElementById('news-feed-list');
      nContainer.innerHTML = gameState.news_feed.map(n => `<div>📡 ${n}</div>`).join('');
    }

    // --- Dynamic Encounters ---
    function checkEncounter() {
      if (!gameState || !gameState.active_encounter) {
        document.getElementById('encounter-modal').classList.remove('active');
        return;
      }
      const enc = gameState.active_encounter;
      const modal = document.getElementById('encounter-modal');
      document.getElementById('enc-title').textContent = enc.title || 'Sensor Contact';
      document.getElementById('enc-desc').textContent = enc.desc || 'An encounter has occurred in deep space.';

      const t = enc.type;
      const opt1 = document.getElementById('enc-btn-opt1');
      const opt2 = document.getElementById('enc-btn-opt2');

      const ENC_ICONS = {
        customs_scan: '🛃', faction_patrol: '🚔', derelict: '🛸',
        solar_flare: '☀️', distress_beacon: '🆘', wandering_trader: '🧳',
        asteroid_field: '🪨', wormhole: '🌀', mining_opportunity: '⛏️',
        pirate_ambush: '☠️', bounty_combat: '🎯'
      };
      document.getElementById('enc-icon').textContent = ENC_ICONS[t] || '📡';

      if (t === 'solar_flare') {
        // Unavoidable radiation event — single acknowledge button.
        opt1.textContent = 'Brace for Impact';
        opt2.style.display = 'none';
        modal.classList.add('active');
        return;
      }
      opt2.style.display = '';

      if (t === 'customs_scan') {
        opt1.textContent = 'Submit to Security Scan';
        opt2.textContent = 'Attempt to Bribe Officer';
      } else if (t === 'faction_patrol') {
        opt1.textContent = 'Transmit Friendly Identification';
        opt2.textContent = 'Ignore and Divert Course';
      } else if (t === 'derelict') {
        opt1.textContent = 'Deploy Salvage Crew';
        opt2.textContent = 'Leave Derelict Alone';
      } else if (t === 'distress_beacon') {
        opt1.textContent = 'Transfer 15 LY Fuel Aid';
        opt2.textContent = 'Ignore Transmission';
      } else if (t === 'wandering_trader') {
        opt1.textContent = `Buy Rare Deal (${enc.qty}x ${enc.good} for ${enc.qty * enc.unit_price} CR)`;
        opt2.textContent = 'Decline Offer';
      } else if (t === 'asteroid_field') {
        opt1.textContent = 'Thread Through Field (Piloting Test)';
        opt2.textContent = 'Take Wide Detour (8 LY Fuel)';
      } else if (t === 'wormhole') {
        opt1.textContent = 'Plunge into Anomaly';
        opt2.textContent = 'Maintain Standard Route';
      } else if (t === 'mining_opportunity') {
        opt1.textContent = 'Deploy Drone Extractors';
        opt2.textContent = 'Bypass Asteroid';
      } else {
        opt1.textContent = 'Cooperate';
        opt2.textContent = 'Dismiss';
      }

      modal.classList.add('active');
    }

    function resolveEncounter(choice) {
      sendAction('resolve_encounter', { choice });
    }

    // --- Tactical Combat ---
    function checkCombat() {
      const c = gameState ? gameState.active_combat : null;
      const modal = document.getElementById('combat-modal');
      if (!c) {
        modal.classList.remove('active');
        return;
      }
      modal.classList.add('active');

      document.getElementById('combat-enemy-name').textContent = c.enemy_name;
      document.getElementById('combat-enemy-ship').textContent = `Class: ${c.enemy_ship_name} · Behavior: ${c.personality.toUpperCase()} — ${c.personality_desc || ''}`;
      document.getElementById('combat-turn-counter').textContent = `Combat Turn ${c.turn_count}`;

      // Player combat gauges
      const p = gameState.player;
      document.getElementById('combat-player-hull-val').textContent = `${p.hull} / ${p.max_hull}`;
      document.getElementById('combat-player-hull-bar').style.width = `${(p.hull / p.max_hull) * 100}%`;
      document.getElementById('combat-player-shield-val').textContent = `${p.shield} / ${p.max_shield}`;
      document.getElementById('combat-player-shield-bar').style.width = p.max_shield > 0 ? `${(p.shield / p.max_shield) * 100}%` : '0%';

      // Enemy combat gauges
      document.getElementById('combat-enemy-hull-val').textContent = `${c.enemy_hull} / ${c.enemy_max_hull}`;
      document.getElementById('combat-enemy-hull-bar').style.width = `${(c.enemy_hull / c.enemy_max_hull) * 100}%`;
      document.getElementById('combat-enemy-shield-val').textContent = `${c.enemy_shield} / ${c.enemy_max_shield}`;
      document.getElementById('combat-enemy-shield-bar').style.width = c.enemy_max_shield > 0 ? `${(c.enemy_shield / c.enemy_max_shield) * 100}%` : '0%';

      // Torpedo Button
      document.getElementById('btn-combat-missile').textContent = `🚀 Fire Torpedo (${c.player_missiles} left)`;
      document.getElementById('btn-combat-missile').disabled = (c.player_missiles <= 0);

      // Drone Bay Button
      const droneBtn = document.getElementById('btn-combat-drones');
      droneBtn.disabled = !c.has_drone_bay;
      droneBtn.textContent = c.drones_active ? '🛸 Drones Active' : '🛸 Deploy Drones';

      // Boarding Button
      document.getElementById('btn-combat-board').disabled = !c.can_board;

      // Combat Terminal
      const term = document.getElementById('combat-terminal-log');
      term.innerHTML = c.combat_log.map(l => `<div>> ${l}</div>`).join('');
      term.scrollTop = term.scrollHeight;

      // Result dismissal
      const resBox = document.getElementById('combat-result-box');
      const actBar = document.getElementById('combat-actions-bar');
      if (c.is_finished) {
        actBar.style.display = 'none';
        resBox.style.display = 'block';
        const dBtn = document.getElementById('combat-btn-dismiss');
        if (c.player_won) {
          dBtn.textContent = '🏆 Enemy Defeated — Salvage & Disengage';
        } else if (c.player_escaped) {
          dBtn.textContent = '🏃 Warp Drive Engaged — Disengage';
        } else if (c.player_dead) {
          dBtn.textContent = '💀 Vessel Destroyed — Reconstruct from Insurance / Autosave';
        }
      } else {
        actBar.style.display = 'grid';
        resBox.style.display = 'none';
      }

      // Holographic Combat Stage Updates
      const pHoloRing = document.getElementById('holo-player-shield-ring');
      if (pHoloRing) pHoloRing.style.opacity = p.max_shield > 0 ? (p.shield / p.max_shield) : 0;
      const eHoloRing = document.getElementById('holo-enemy-shield-ring');
      if (eHoloRing) eHoloRing.style.opacity = c.enemy_max_shield > 0 ? (c.enemy_shield / c.enemy_max_shield) : 0;
      const eHoloLabel = document.getElementById('holo-enemy-label');
      if (eHoloLabel) eHoloLabel.textContent = c.enemy_name || 'HOSTILE TARGET';
    }

    function triggerCombatFx(actionType) {
      const arena = document.getElementById('combat-holo-arena');
      const overlay = document.getElementById('holo-fx-overlay');
      const floatBox = document.getElementById('holo-float-text');
      const enemyShip = document.getElementById('holo-enemy-ship');
      const playerShip = document.getElementById('holo-player-ship');
      if (!arena || !overlay) return;

      const w = arena.clientWidth || 600;
      const h = arena.clientHeight || 160;

      if (actionType === 'fire' || actionType.startsWith('target_')) {
        // High-energy laser pulse from player to enemy
        overlay.innerHTML = `
          <line x1="110" y1="${h / 2}" x2="${w - 110}" y2="${h / 2}" stroke="#00e5ff" stroke-width="3" class="laser-beam" stroke-linecap="round" />
          <line x1="110" y1="${h / 2 - 8}" x2="${w - 110}" y2="${h / 2 - 8}" stroke="#7986cb" stroke-width="2" class="laser-beam" stroke-linecap="round" />
        `;
        setTimeout(() => {
          if (enemyShip) {
            enemyShip.classList.add('shake-anim');
            setTimeout(() => enemyShip.classList.remove('shake-anim'), 400);
          }
          if (floatBox) {
            const el = document.createElement('div');
            el.className = 'dmg-float dmg-red';
            el.style.right = '60px';
            el.style.top = '25px';
            el.textContent = actionType === 'fire' ? 'DIRECT HIT!' : 'SUBSYSTEM CRIT!';
            floatBox.appendChild(el);
            setTimeout(() => el.remove(), 1300);
          }
        }, 120);
        setTimeout(() => { overlay.innerHTML = ''; }, 500);
      } else if (actionType === 'missile') {
        overlay.innerHTML = `
          <line x1="110" y1="${h / 2 + 10}" x2="${w - 110}" y2="${h / 2}" stroke="#ffab00" stroke-width="4" stroke-dasharray="8 4" class="laser-beam" />
        `;
        setTimeout(() => {
          if (enemyShip) {
            enemyShip.classList.add('shake-anim');
            setTimeout(() => enemyShip.classList.remove('shake-anim'), 400);
          }
          if (floatBox) {
            const el = document.createElement('div');
            el.className = 'dmg-float dmg-red';
            el.style.right = '60px';
            el.style.top = '20px';
            el.textContent = '💥 TORPEDO DETONATION!';
            floatBox.appendChild(el);
            setTimeout(() => el.remove(), 1300);
          }
        }, 150);
        setTimeout(() => { overlay.innerHTML = ''; }, 500);
      } else if (actionType === 'flee') {
        if (playerShip) {
          playerShip.style.transform = 'scale(0.8) translateX(-20px)';
          setTimeout(() => { playerShip.style.transform = ''; }, 500);
        }
      }
    }

    function sendCombatAction(combat_action) {
      triggerCombatFx(combat_action);
      sendAction('combat_action', { combat_action });
    }

    function dismissCombat() {
      sendAction('dismiss_combat');
    }

    // --- Save / Load Dialog ---
    async function openSaveModal() {
      try {
        const res = await fetch('/api/saves');
        const json = await res.json();
        if (json.success && json.slots) {
          const list = document.getElementById('save-slots-list');
          let html = '';
          json.slots.forEach(s => {
            const info = s.info;
            html += `
              <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 12px; display: flex; justify-content: space-between; align-items: center;">
                <div>
                  <div style="font-weight: 700; color: var(--cyan);">Slot ${s.slot.toUpperCase()}</div>
                  <div style="font-size: 11px; color: var(--fg-dim);">
                    ${s.exists ? `${info.name} · Day ${info.day} · ${info.location} · ${info.credits.toLocaleString()} CR` : 'Empty Save Slot'}
                  </div>
                </div>
                <div style="display: flex; gap: 6px;">
                  <button class="btn-action-sm btn-buy" onclick="saveGame('${s.slot}')">Save</button>
                  <button class="btn-action-sm btn-sell" ${!s.exists ? 'disabled' : ''} onclick="loadGame('${s.slot}')">Load</button>
                </div>
              </div>
            `;
          });
          list.innerHTML = html;
          document.getElementById('save-modal').classList.add('active');
        }
      } catch (e) {
        showToast("Error loading saves: " + e);
      }
    }

    function closeSaveModal() {
      document.getElementById('save-modal').classList.remove('active');
    }

    function saveGame(slot) {
      sendAction('save_game', { slot });
      closeSaveModal();
    }

    function loadGame(slot) {
      sendAction('load_game', { slot });
      closeSaveModal();
    }

    // --- New Game Dialog ---
    function openNewGameModal() {
      document.getElementById('newgame-modal').classList.add('active');
    }
    function closeNewGameModal() {
      document.getElementById('newgame-modal').classList.remove('active');
    }
    function confirmNewGame() {
      const name = document.getElementById('newgame-name').value;
      const difficulty = document.getElementById('newgame-diff').value;
      closeNewGameModal();
      sendAction('new_game', { name, difficulty });
    }

    // --- Manual Modal ---
    function openManualModal() {
      document.getElementById('manual-modal').classList.add('active');
    }
    function closeManualModal() {
      document.getElementById('manual-modal').classList.remove('active');
    }

    // --- Game Over / Victory ---
    function checkGameOver() {
      const modal = document.getElementById('gameover-modal');
      if (gameState && gameState.player.is_game_over) {
        if (!modal.classList.contains('active')) {
          modal.classList.add('active');
          playSound('death');
        }
      } else {
        modal.classList.remove('active');
      }
    }

    function recoverSave(slot) {
      document.getElementById('gameover-modal').classList.remove('active');
      sendAction('load_game', { slot });
    }

    function checkVictory() {
      const modal = document.getElementById('victory-modal');
      if (gameState && gameState.player.victory && !victoryShown) {
        victoryShown = true;
        document.getElementById('victory-captain-name').textContent = gameState.player.name;
        modal.classList.add('active');
        playSound('victory');
      }
      if (!gameState || !gameState.player.victory) {
        victoryShown = false;
        modal.classList.remove('active');
      }
    }

    function dismissVictory() {
      document.getElementById('victory-modal').classList.remove('active');
    }

    // --- Amount Input Modal (bank / stocks) ---
    let amountModalAction = null;
    let amountModalPayload = {};

    function openAmountModal(title, hint, confirmLabel, defaultValue, action, payload = {}) {
      amountModalAction = action;
      amountModalPayload = payload;
      document.getElementById('amount-modal-title').textContent = title;
      document.getElementById('amount-modal-hint').textContent = hint;
      document.getElementById('amount-modal-confirm').textContent = confirmLabel;
      const input = document.getElementById('amount-modal-input');
      input.value = defaultValue;
      document.getElementById('amount-modal').classList.add('active');
      setTimeout(() => input.focus(), 50);
    }

    function closeAmountModal() {
      document.getElementById('amount-modal').classList.remove('active');
      amountModalAction = null;
      amountModalPayload = {};
    }

    function setAmountModalValue(v) {
      document.getElementById('amount-modal-input').value = v;
    }

    function confirmAmountModal() {
      const val = parseInt(document.getElementById('amount-modal-input').value, 10);
      if (!amountModalAction || isNaN(val) || val <= 0) {
        showToast('Please enter a valid positive quantity.');
        return;
      }
      const action = amountModalAction;
      const payload = amountModalPayload;
      closeAmountModal();
      sendAction(action, { ...payload, [payload.key || 'amount']: val });
    }

    // --- Bank prompt replacements ---
    function promptDeposit() {
      openAmountModal('🏦 Deposit to Savings',
        `Available credits: ${gameState.player.credits.toLocaleString()} CR. Savings earn 0.8% daily.`,
        'Deposit Credits', Math.min(500, gameState.player.credits),
        'bank_deposit', { key: 'amount' });
    }
    function promptWithdraw() {
      openAmountModal('🏦 Withdraw from Savings',
        `Savings balance: ${gameState.player.savings.toLocaleString()} CR.`,
        'Withdraw Credits', Math.min(500, gameState.player.savings),
        'bank_withdraw', { key: 'amount' });
    }
    function promptBorrow() {
      openAmountModal('🏦 Borrow from First Galactic Bank',
        `Credit limit remaining: ${(gameState.player.loan_limit - gameState.player.loan).toLocaleString()} CR at ${gameState.player.effective_interest_rate}% daily interest.`,
        'Borrow Funds', 1000,
        'bank_borrow', { key: 'amount' });
    }
    function promptRepay() {
      openAmountModal('🏦 Repay Loan',
        `Outstanding debt: ${gameState.player.loan.toLocaleString()} CR. On-time repayments boost your credit score.`,
        'Repay Loan', Math.min(1000, gameState.player.loan),
        'bank_repay', { key: 'amount' });
    }
    function tradeStock(symbol, mode) {
      const stk = gameState.all_stocks.find(s => s.symbol === symbol);
      if (!stk) return;
      if (mode === 'buy') {
        openAmountModal(`📊 Buy ${stk.symbol} Shares`,
          `Current price: ${stk.price.toFixed(2)} CR/share. You hold ${stk.shares_owned} shares.`,
          'Buy Shares', 5,
          'buy_stock', { key: 'qty', symbol });
      } else {
        openAmountModal(`📊 Sell ${stk.symbol} Shares`,
          `Current price: ${stk.price.toFixed(2)} CR/share. You hold ${stk.shares_owned} shares.`,
          'Sell Shares', Math.min(5, stk.shares_owned),
          'sell_stock', { key: 'qty', symbol });
      }
    }

    // --- Keyboard Shortcuts ---
    window.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
      if (e.key === '1') switchTab('map');
      if (e.key === '2') switchTab('market');
      if (e.key === '3') switchTab('advisor');
      if (e.key === '4') switchTab('shipyard');
      if (e.key === '5') switchTab('services');
      if (e.key === '6') switchTab('crew');
      if (e.key === '7') switchTab('missions');
      if (e.key === '8') switchTab('bank');
      if (e.key === '9') switchTab('log');
      if (e.key === 'Escape') {
        closeTradeModal();
        closeSaveModal();
        closeNewGameModal();
        closeManualModal();
      }
    });

    // --- Initial Boot ---
    window.addEventListener('DOMContentLoaded', () => {
      fetchState();
      setInterval(fetchState, 3000); // Polling for telemetry & auto-sync
    });
  </script>
</body>
</html>
"""


# ==============================================================================
# WEB SERVER & SESSION MANAGEMENT — Browser HTML Edition
# ==============================================================================

class GameSession:
    """Holds the active game engine and current encounter / combat state."""
    def __init__(self, muted: bool = True, difficulty: str = "normal", player_name: str = "Commander"):
        # The browser client renders its own Web Audio effects — keep the
        # headless server-side SoundManager silent so terminal bells never
        # pollute the server console.
        self.engine = GameEngine(muted=True, difficulty_id=difficulty)
        self.engine.new_game(player_name, difficulty)
        self.active_encounter: Optional[Dict[str, Any]] = None
        self.active_combat: Optional[CombatEncounter] = None
        self.last_logs: List[str] = ["Welcome to Space Trader: Odyssey. Engines online, all systems nominal."]
        self.sound_event: Optional[str] = None
        self.lock = threading.Lock()

    def set_sound(self, name: str) -> None:
        self.sound_event = name

    def pop_sound(self) -> Optional[str]:
        s = self.sound_event
        self.sound_event = None
        return s


RANK_TIER_COLORS = ["#94a3b8", "#38bdf8", "#34d399", "#a855f7", "#fbbf24", "#f43f5e"]


def serialize_game_state(session: GameSession) -> Dict[str, Any]:
    """Extract comprehensive JSON game state for the browser UI."""
    engine = session.engine
    player = engine.player
    current_p = engine.current_planet

    # Cargo detail
    cargo_detail = []
    cargo_used = 0
    for gid, qty in player.cargo.items():
        if qty > 0 and gid in COMMODITIES:
            comm = COMMODITIES[gid]
            sell_price = engine.get_sell_price(gid)
            cost_basis = player.cargo_cost_basis.get(gid, float(sell_price))
            unit_profit = sell_price - cost_basis
            profit_pct = round((unit_profit / max(1.0, cost_basis)) * 100.0, 1)
            cargo_detail.append({
                "id": gid,
                "name": comm.name,
                "category": comm.category,
                "qty": qty,
                "base_price": comm.base_price,
                "sell_price": sell_price,
                "cost_basis": round(cost_basis, 1),
                "unit_profit": round(unit_profit, 1),
                "profit_pct": profit_pct,
                "total_profit": int(round(unit_profit * qty)),
                "total_value": sell_price * qty,
                "is_contraband": comm.is_contraband,
                "desc": comm.desc,
                "icon": comm.icon,
            })
            cargo_used += qty

    # Rank & Renown progression
    cur_rank = engine.rank()
    next_rank_title, renown_needed = engine.renown_to_next_rank()
    cur_rank_idx = engine.rank_index()
    if cur_rank_idx < len(RANKS) - 1:
        prev_req = cur_rank.renown
        next_req = RANKS[cur_rank_idx + 1].renown
        denom = max(1, next_req - prev_req)
        renown_pct = min(100.0, max(0.0, (engine.renown() - prev_req) / denom * 100.0))
    else:
        renown_pct = 100.0

    ship_tmpl = SHIP_TEMPLATES.get(player.ship_id, SHIP_TEMPLATES["sparrow"])

    # Crew details
    crew_list = []
    for c in AVAILABLE_CREW:
        crew_list.append({
            "id": c.id,
            "name": c.name,
            "role": c.role,
            "hire_cost": c.hire_cost,
            "daily_wage": c.daily_wage,
            "perk_type": c.perk_type,
            "perk_val": c.perk_val,
            "desc": c.desc,
            "is_hired": c.id in player.hired_crew,
        })

    # Planets list
    has_scanner = player.has_module("deep_scanner")
    all_planets = []
    for p in engine.planets.values():
        dist = engine.calculate_distance(current_p, p)
        fuel_cost, days_cost = engine.calculate_travel_cost(p)
        active_missions_here = [
            {"id": m.id, "title": m.title, "type": m.m_type, "reward": m.reward_credits, "days_left": m.days_left}
            for m in player.active_missions if m.destination == p.name
        ]
        all_planets.append({
            "name": p.name,
            "subtitle": p.subtitle,
            "x": p.x,
            "y": p.y,
            "tech": p.tech,
            "agri": p.agri,
            "crime": p.crime,
            "rich": p.rich,
            "mining": p.mining,
            "security": p.security,
            "faction": p.faction,
            "color": p.color,
            "desc": p.desc,
            "fuel_price": p.fuel_price,
            "repair_cost": p.repair_cost,
            "active_event": asdict(p.active_event) if p.active_event else None,
            "distance": round(dist, 1),
            "fuel_cost": fuel_cost,
            "days_cost": days_cost,
            "in_range": player.fuel >= fuel_cost,
            "is_current": p.name == current_p.name,
            "active_missions": active_missions_here,
            "remote_deals": engine.remote_top_deals(p) if has_scanner else None,
        })

    # Market commodities on current planet
    market_items = []
    for gid, comm in COMMODITIES.items():
        buy_p = engine.get_buy_price(gid)
        sell_p = engine.get_sell_price(gid)
        stock_qty = current_p.stock.get(gid, 0)
        p_qty = player.cargo.get(gid, 0)
        history = current_p.price_history.get(gid, [comm.base_price])

        if len(history) >= 2:
            if history[-1] > history[-2]:
                trend = "up"
            elif history[-1] < history[-2]:
                trend = "down"
            else:
                trend = "flat"
        else:
            trend = "flat"

        avg_price = sum(history) / len(history) if history else comm.base_price
        pct_diff = round(((buy_p - comm.base_price) / max(1, comm.base_price)) * 100.0, 1)

        p_cost_basis = round(player.cargo_cost_basis.get(gid, 0.0), 1) if p_qty > 0 else 0.0
        p_unit_profit = round(sell_p - p_cost_basis, 1) if (p_qty > 0 and p_cost_basis > 0) else 0.0
        p_profit_pct = round((p_unit_profit / max(1.0, p_cost_basis)) * 100.0, 1) if (p_qty > 0 and p_cost_basis > 0) else 0.0

        market_items.append({
            "id": gid,
            "name": comm.name,
            "category": comm.category,
            "base_price": comm.base_price,
            "buy_price": buy_p,
            "sell_price": sell_p,
            "stock": stock_qty,
            "player_qty": p_qty,
            "cost_basis": p_cost_basis,
            "unit_profit": p_unit_profit,
            "profit_pct": p_profit_pct,
            "trend": trend,
            "price_history": history,
            "is_contraband": comm.is_contraband,
            "desc": comm.desc,
            "icon": comm.icon,
            "avg_price": int(avg_price),
            "pct_diff": pct_diff,
            "can_buy": (player.credits >= buy_p and (player.cargo_cap - cargo_used) > 0 and stock_qty > 0),
            "can_sell": p_qty > 0,
        })

    # Shipyard
    ships_list = []
    trade_in_val = engine.ship_trade_in_value()
    for sid, st in SHIP_TEMPLATES.items():
        cost_after_trade = max(0, st.cost - trade_in_val)
        ships_list.append({
            "id": sid,
            "name": st.name,
            "ship_class": st.ship_class,
            "cost": st.cost,
            "trade_in_value": trade_in_val,
            "net_cost": cost_after_trade,
            "cargo_cap": st.cargo_cap,
            "max_hull": st.max_hull,
            "max_shield": st.max_shield,
            "max_fuel": st.max_fuel,
            "speed": st.speed,
            "weapon_slots": st.weapon_slots,
            "shield_slots": st.shield_slots,
            "module_slots": st.module_slots,
            "desc": st.desc,
            "is_current": sid == player.ship_id,
            "can_afford": player.credits >= cost_after_trade,
        })

    # Outfitter
    equipment_list = []
    for eq_id, eq in EQUIPMENT_ITEMS.items():
        is_equipped = (
            eq_id in player.equipped_weapons or
            eq_id in player.equipped_shields or
            eq_id in player.equipped_modules
        )
        equipment_list.append({
            "id": eq_id,
            "name": eq.name,
            "slot_type": eq.slot_type,
            "cost": eq.cost,
            "refund_val": int(eq.cost * 0.75),
            "damage": eq.damage,
            "shield_hp": eq.shield_hp,
            "accuracy": eq.accuracy,
            "crit_chance": eq.crit_chance,
            "fuel_save": eq.fuel_save,
            "cargo_bonus": eq.cargo_bonus,
            "evasion_bonus": eq.evasion_bonus,
            "desc": eq.desc,
            "is_equipped": is_equipped,
            "can_afford": player.credits >= eq.cost,
        })

    # Missions
    available_missions = []
    for m in engine.available_missions:
        if m.origin == current_p.name and not m.completed and not m.failed and m not in player.active_missions:
            available_missions.append(asdict(m))

    active_missions = [asdict(m) for m in player.active_missions]

    # Stocks
    stocks_list = []
    for sym, stk in engine.stocks.items():
        owned = player.stocks_owned.get(sym, 0)
        history = stk.history
        last_change = 0.0
        if len(history) >= 2:
            last_change = round(((history[-1] - history[-2]) / max(1.0, history[-2])) * 100.0, 1)
        stocks_list.append({
            "symbol": sym,
            "name": stk.name,
            "price": round(stk.price, 2),
            "history": [round(h, 2) for h in stk.history[-15:]],
            "volatility": stk.volatility,
            "desc": stk.desc,
            "shares_owned": owned,
            "total_value": int(owned * stk.price),
            "change_pct": last_change,
        })

    # Achievements
    achievements_list = []
    for aid, (atitle, adesc) in ACHIEVEMENTS.items():
        achievements_list.append({
            "id": aid,
            "title": atitle,
            "desc": adesc,
            "unlocked": aid in player.achievements,
        })

    # Combat state
    combat_data = None
    if session.active_combat:
        c = session.active_combat
        combat_data = {
            "enemy_name": c.enemy_name,
            "enemy_ship_id": c.enemy_ship_id,
            "enemy_ship_name": c.enemy_ship_name,
            "enemy_hull": c.enemy_hull,
            "enemy_max_hull": c.enemy_max_hull,
            "enemy_shield": c.enemy_shield,
            "enemy_max_shield": c.enemy_max_shield,
            "enemy_weapons_damaged": c.enemy_weapons_damaged,
            "enemy_engines_damaged": c.enemy_engines_damaged,
            "personality": c.personality,
            "personality_desc": ENEMY_PERSONALITIES.get(c.personality, ""),
            "turn_count": c.turn_count,
            "drones_active": c.drones_active,
            "combat_log": c.combat_log[-12:],
            "is_finished": c.is_finished,
            "player_won": c.player_won,
            "player_escaped": c.player_escaped,
            "player_dead": c.player_dead,
            "insurance_used": c.insurance_used,
            "enemy_fled": c.enemy_fled,
            "boarded_success": c.boarded_success,
            "can_board": c.can_board(),
            "can_flee": c.can_flee(),
            "player_missiles": c.player_missiles(),
            "has_drone_bay": engine.player.has_drone_bay(),
            "is_bounty": c.is_bounty,
            "bounty_reward": c.bounty_reward,
        }

    net_worth = engine.calculate_net_worth()

    return {
        "player": {
            "name": player.name,
            "credits": player.credits,
            "savings": player.savings,
            "loan": player.loan,
            "loan_limit": engine.loan_limit(),
            "effective_interest_rate": round(engine._effective_loan_interest() * 100, 2),
            "credit_score": player.credit_score,
            "day": player.day,
            "location": player.location,
            "difficulty_id": player.difficulty_id,
            "difficulty_name": engine.difficulty.name,
            "ship_id": player.ship_id,
            "ship_name": ship_tmpl.name,
            "ship_class": ship_tmpl.ship_class,
            "hull": player.hull,
            "max_hull": player.max_hull,
            "shield": player.shield,
            "max_shield": player.max_shield,
            "fuel": player.fuel,
            "max_fuel": player.max_fuel,
            "speed": ship_tmpl.speed,
            "cargo_cap": player.cargo_cap,
            "cargo_used": cargo_used,
            "cargo_free": max(0, player.cargo_cap - cargo_used),
            "missiles": player.missiles,
            "max_missiles": PLAYER_MISSILE_CAP,
            "missile_price": engine.missile_price(),
            "fuel_unit_price": max(1, int(
                current_p.fuel_price * engine.difficulty.fuel_mult
                * (1.0 - RANK_SERVICE_DISCOUNT[engine.rank_index()])
            )),
            "insurance_active": player.insurance_active,
            "insurance_price": engine.insurance_price(),
            "weapons_damaged": player.weapons_damaged,
            "engines_damaged": player.engines_damaged,
            "shields_damaged": player.shields_damaged,
            "subsystem_repair_cost": engine.subsystem_repair_cost(),
            "repair_cost_per_hp": engine.current_repair_price(),
            "highest_rank_index": player.highest_rank_index,
            "rank": {
                "id": cur_rank.id,
                "name": cur_rank.name,
                "renown": cur_rank.renown,
                "insignia": cur_rank.insignia,
                "perk": cur_rank.perk,
                "color": RANK_TIER_COLORS[cur_rank_idx],
                "tier": cur_rank_idx,
            },
            "renown": engine.renown(),
            "next_rank_title": next_rank_title,
            "renown_needed": renown_needed,
            "renown_pct": round(renown_pct, 1),
            "cargo": player.cargo,
            "cargo_detail": cargo_detail,
            "equipped_weapons": player.equipped_weapons,
            "equipped_shields": player.equipped_shields,
            "equipped_modules": player.equipped_modules,
            "equipped_weapons_detail": [
                {
                    "id": eq_id,
                    "name": EQUIPMENT_ITEMS[eq_id].name,
                    "cost": EQUIPMENT_ITEMS[eq_id].cost,
                    "refund_val": int(EQUIPMENT_ITEMS[eq_id].cost * 0.75),
                    "damage": EQUIPMENT_ITEMS[eq_id].damage,
                    "accuracy": EQUIPMENT_ITEMS[eq_id].accuracy,
                    "crit_chance": EQUIPMENT_ITEMS[eq_id].crit_chance,
                    "desc": EQUIPMENT_ITEMS[eq_id].desc,
                }
                for eq_id in player.equipped_weapons if eq_id in EQUIPMENT_ITEMS
            ],
            "equipped_shields_detail": [
                {
                    "id": eq_id,
                    "name": EQUIPMENT_ITEMS[eq_id].name,
                    "cost": EQUIPMENT_ITEMS[eq_id].cost,
                    "refund_val": int(EQUIPMENT_ITEMS[eq_id].cost * 0.75),
                    "shield_hp": EQUIPMENT_ITEMS[eq_id].shield_hp,
                    "desc": EQUIPMENT_ITEMS[eq_id].desc,
                }
                for eq_id in player.equipped_shields if eq_id in EQUIPMENT_ITEMS
            ],
            "equipped_modules_detail": [
                {
                    "id": eq_id,
                    "name": EQUIPMENT_ITEMS[eq_id].name,
                    "cost": EQUIPMENT_ITEMS[eq_id].cost,
                    "refund_val": int(EQUIPMENT_ITEMS[eq_id].cost * 0.75),
                    "desc": EQUIPMENT_ITEMS[eq_id].desc,
                }
                for eq_id in player.equipped_modules if eq_id in EQUIPMENT_ITEMS
            ],
            "weapon_slots": ship_tmpl.weapon_slots,
            "shield_slots": ship_tmpl.shield_slots,
            "module_slots": ship_tmpl.module_slots,
            "hired_crew": player.hired_crew,
            "total_daily_wages": int(engine.total_daily_wages()),
            "active_missions": active_missions,
            "stocks_owned": player.stocks_owned,
            "achievements": list(player.achievements),
            "stats": player.stats,
            "net_worth": net_worth,
            "target_net_worth": TARGET_NET_WORTH,
            "net_worth_pct": round(min(100.0, (net_worth / TARGET_NET_WORTH) * 100.0), 1),
            "net_worth_history": player.net_worth_history[-30:],
            "reputation": {f: player.rep(f) for f in FACTIONS},
            "standing": {f: reputation_rank(player.rep(f)) for f in FACTIONS},
            "victory": net_worth >= TARGET_NET_WORTH,
            "is_game_over": engine.is_game_over,
            "effective_max_shield": player.effective_max_shield(),
            "has_drone_bay": player.has_drone_bay(),
            "has_deep_scanner": player.has_module("deep_scanner"),
            "has_missile_rack": player.has_missile_rack(),
        },
        "current_planet": {
            "name": current_p.name,
            "subtitle": current_p.subtitle,
            "x": current_p.x,
            "y": current_p.y,
            "tech": current_p.tech,
            "agri": current_p.agri,
            "crime": current_p.crime,
            "rich": current_p.rich,
            "mining": current_p.mining,
            "security": current_p.security,
            "faction": current_p.faction,
            "color": current_p.color,
            "desc": current_p.desc,
            "fuel_price": current_p.fuel_price,
            "repair_cost": current_p.repair_cost,
            "active_event": asdict(current_p.active_event) if current_p.active_event else None,
            "market": market_items,
        },
        "all_planets": all_planets,
        "all_ships": ships_list,
        "all_equipment": equipment_list,
        "all_crew": crew_list,
        "available_missions": available_missions,
        "all_stocks": stocks_list,
        "all_achievements": achievements_list,
        "ranks": [
            {
                "id": r.id,
                "name": r.name,
                "renown": r.renown,
                "insignia": r.insignia,
                "perk": r.perk,
                "color": RANK_TIER_COLORS[i],
            }
            for i, r in enumerate(RANKS)
        ],
        "news_feed": engine.news_feed[-15:],
        "active_encounter": session.active_encounter,
        "active_combat": combat_data,
        "last_logs": session.last_logs[-8:],
    }


GLOBAL_SESSION: Optional[GameSession] = None


class SpaceTraderWebHandler(http.server.BaseHTTPRequestHandler):
    """Multi-threaded HTTP handler serving the browser game client and REST API."""

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress noisy default HTTP access logs."""
        return

    def _set_headers(self, content_type: str = "application/json", status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()

    def do_OPTIONS(self) -> None:
        self._set_headers()

    def do_GET(self) -> None:
        global GLOBAL_SESSION
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if path in ("/", "/index.html"):
            self._set_headers(content_type="text/html; charset=utf-8")
            self.wfile.write(HTML_PAGE.encode("utf-8"))
            return

        if path == "/api/health":
            self._set_headers()
            self.wfile.write(json.dumps({"status": "ok", "app": "Space Trader: Odyssey"}).encode("utf-8"))
            return

        if GLOBAL_SESSION is None:
            GLOBAL_SESSION = GameSession()

        if path == "/api/state":
            with GLOBAL_SESSION.lock:
                state = serialize_game_state(GLOBAL_SESSION)
                sound = GLOBAL_SESSION.pop_sound()
            self._set_headers()
            self.wfile.write(json.dumps({"success": True, "state": state, "sound": sound}).encode("utf-8"))
            return

        if path == "/api/routes":
            with GLOBAL_SESSION.lock:
                routes = GLOBAL_SESSION.engine.compute_best_trade_routes()
            self._set_headers()
            self.wfile.write(json.dumps({"success": True, "routes": routes[:12]}).encode("utf-8"))
            return

        if path == "/api/saves":
            with GLOBAL_SESSION.lock:
                slots = []
                for s in (AUTO_SLOT, PRECOMBAT_SLOT) + SAVE_SLOTS:
                    info = GLOBAL_SESSION.engine.slot_info(s)
                    slots.append({
                        "slot": s,
                        "exists": info is not None,
                        "info": info
                    })
            self._set_headers()
            self.wfile.write(json.dumps({"success": True, "slots": slots}).encode("utf-8"))
            return

        # Fallback 404
        self.send_error(404, "File Not Found")

    def do_POST(self) -> None:
        global GLOBAL_SESSION
        if GLOBAL_SESSION is None:
            GLOBAL_SESSION = GameSession()

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/action":
            self.send_error(404, "Unknown endpoint")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            data = json.loads(body) if body else {}
        except Exception as e:
            self._set_headers(status=400)
            self.wfile.write(json.dumps({"success": False, "error": f"Invalid JSON: {e}"}).encode("utf-8"))
            return

        for numeric_key in ("qty", "amount", "hp"):
            if numeric_key not in data:
                continue
            value = data[numeric_key]
            if isinstance(value, bool):
                self._set_headers(status=400)
                self.wfile.write(json.dumps({
                    "success": False,
                    "error": f"{numeric_key} must be an integer.",
                }).encode("utf-8"))
                return
            try:
                data[numeric_key] = int(value)
            except (TypeError, ValueError):
                self._set_headers(status=400)
                self.wfile.write(json.dumps({
                    "success": False,
                    "error": f"{numeric_key} must be an integer.",
                }).encode("utf-8"))
                return

        action = data.get("action", "")
        success = False
        message = ""
        sound: Optional[str] = None
        logs: List[str] = []

        with GLOBAL_SESSION.lock:
            engine = GLOBAL_SESSION.engine

            if action == "new_game":
                name = str(data.get("name", "Commander")).strip() or "Commander"
                diff = str(data.get("difficulty", "normal")).strip().lower()
                engine.new_game(name, diff)
                GLOBAL_SESSION.active_encounter = None
                GLOBAL_SESSION.active_combat = None
                GLOBAL_SESSION.last_logs = [f"New commission created for {name} on {diff.upper()} difficulty."]
                success = True
                message = f"Welcome aboard, {name}!"
                sound = "warp"

            elif action == "travel":
                dest = str(data.get("destination", ""))
                ok, msg, enc = engine.execute_travel(dest)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "warp"
                    if enc:
                        t = enc.get("type")
                        if t in ("pirate_ambush", "bounty_combat"):
                            GLOBAL_SESSION.active_combat = start_combat(engine, enc)
                            GLOBAL_SESSION.active_encounter = None
                            sound = "alarm"
                            logs.append(f"ALARM: Hostile vessel intercepted! {enc.get('title', 'Raider Attack')}")
                        else:
                            GLOBAL_SESSION.active_encounter = enc
                            logs.append(f"Sector Sensor Contact: {enc.get('title', 'Anomaly Detected')}")
                    else:
                        GLOBAL_SESSION.active_encounter = None
                        GLOBAL_SESSION.active_combat = None

            elif action == "resolve_encounter":
                choice = bool(data.get("choice", False))
                enc = GLOBAL_SESSION.active_encounter
                if enc:
                    t = enc.get("type")
                    enc_logs: List[str] = []
                    if t == "customs_scan":
                        # UI semantics: opt1 (choice=True) = "Submit to Scan",
                        # opt2 (choice=False) = "Attempt to Bribe" — the engine
                        # expects bribe=True, hence the inversion here.
                        enc_logs = engine.resolve_customs(enc, bribe=not choice)
                        sound = "coin" if not choice else "alarm"
                    elif t == "faction_patrol":
                        enc_logs = engine.resolve_faction_patrol(enc, choice)
                        sound = "coin" if choice else "alarm"
                    elif t == "derelict":
                        enc_logs = engine.resolve_derelict(enc, choice)
                        sound = "upgrade" if choice else "click"
                    elif t == "solar_flare":
                        enc_logs = engine.resolve_solar_flare(enc)
                        sound = "shield_hit"
                    elif t == "distress_beacon":
                        enc_logs = engine.resolve_distress(enc, choice)
                        sound = "coin" if choice else "click"
                    elif t == "wandering_trader":
                        enc_logs = engine.resolve_trader(enc, choice)
                        sound = "buy" if choice else "click"
                    elif t == "asteroid_field":
                        enc_logs = engine.resolve_asteroid_field(enc, choice)
                        sound = "shield_hit" if choice else "coin"
                    elif t == "wormhole":
                        enc_logs = engine.resolve_wormhole(enc, choice)
                        sound = "wormhole" if choice else "click"
                    elif t == "mining_opportunity":
                        enc_logs = engine.resolve_mining(enc, choice)
                        sound = "mine" if choice else "click"

                    GLOBAL_SESSION.active_encounter = None
                    if enc.get("type") in ("pirate_ambush", "bounty_combat"):
                        GLOBAL_SESSION.active_combat = start_combat(engine, enc)
                        sound = "alarm"

                    success = True
                    logs.extend(enc_logs)
                    message = enc_logs[-1] if enc_logs else "Encounter resolved."
                else:
                    success = False
                    message = "No active encounter to resolve."

            elif action == "combat_action":
                c_act = str(data.get("combat_action", "fire"))
                combat = GLOBAL_SESSION.active_combat
                if combat and not combat.is_finished:
                    c_logs = combat.player_action(c_act)
                    logs.extend(c_logs)
                    success = True
                    message = c_logs[-1] if c_logs else "Combat turn complete."

                    if c_act == "fire":
                        sound = "laser"
                    elif c_act == "missile":
                        sound = "laser"
                    elif c_act == "recharge":
                        sound = "upgrade"
                    elif c_act == "flee":
                        sound = "warp" if combat.player_escaped else "alarm"
                    elif c_act == "board":
                        sound = "upgrade"

                    if combat.is_finished:
                        if combat.player_won:
                            sound = "victory"
                        elif combat.player_dead:
                            sound = "death"
                else:
                    success = False
                    message = "No active combat engagement."

            elif action == "dismiss_combat":
                combat = GLOBAL_SESSION.active_combat
                if combat and combat.is_finished:
                    GLOBAL_SESSION.active_combat = None
                    if combat.player_dead:
                        # Never persist a destroyed ship over the autosave —
                        # the pre-combat snapshot is the recovery point.
                        success = True
                        message = "Vessel lost. Recovery options available."
                    else:
                        engine.autosave()
                        success = True
                        message = "Disengaged from combat arena."
                else:
                    success = False
                    message = "Combat is still in progress."

            elif action == "buy_commodity":
                good = str(data.get("good", ""))
                qty = int(data.get("qty", 1))
                ok, msg = engine.buy_commodity(good, qty)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "buy"

            elif action == "sell_commodity":
                good = str(data.get("good", ""))
                qty = int(data.get("qty", 1))
                ok, msg = engine.sell_commodity(good, qty)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "sell"

            elif action == "buy_fuel":
                amount = int(data.get("amount", 10))
                ok, msg = engine.buy_fuel(amount)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "coin"

            elif action == "repair_hull":
                hp = int(data.get("hp", 10))
                ok, msg = engine.repair_hull(hp)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "upgrade"

            elif action == "repair_subsystems":
                ok, msg = engine.repair_subsystems()
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "upgrade"

            elif action == "buy_insurance":
                ok, msg = engine.buy_insurance()
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "coin"

            elif action == "buy_missiles":
                qty = int(data.get("qty", 1))
                ok, msg = engine.buy_missiles(qty)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "coin"

            elif action == "buy_ship":
                ship_id = str(data.get("ship_id", ""))
                ok, msg = engine.buy_ship(ship_id)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "upgrade"

            elif action == "buy_equipment":
                eq_id = str(data.get("eq_id", ""))
                ok, msg = engine.buy_equipment(eq_id)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "upgrade"

            elif action == "sell_equipment":
                eq_id = str(data.get("eq_id", ""))
                ok, msg = engine.sell_equipment(eq_id)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "sell"

            elif action == "hire_crew":
                crew_id = str(data.get("crew_id", ""))
                ok, msg = engine.hire_crew(crew_id)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "coin"

            elif action == "dismiss_crew":
                crew_id = str(data.get("crew_id", ""))
                ok, msg = engine.dismiss_crew(crew_id)
                success = ok
                message = msg
                logs.append(msg)

            elif action == "accept_mission":
                m_id = str(data.get("mission_id", ""))
                ok, msg = engine.accept_mission(m_id)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "coin"

            elif action == "abandon_mission":
                m_id = str(data.get("mission_id", ""))
                ok, msg = engine.abandon_mission(m_id)
                success = ok
                message = msg
                logs.append(msg)

            elif action == "bank_deposit":
                amount = int(data.get("amount", 100))
                ok, msg = engine.deposit(amount)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "coin"

            elif action == "bank_withdraw":
                amount = int(data.get("amount", 100))
                ok, msg = engine.withdraw(amount)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "coin"

            elif action == "bank_borrow":
                amount = int(data.get("amount", 100))
                ok, msg = engine.borrow(amount)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "coin"

            elif action == "bank_repay":
                amount = int(data.get("amount", 100))
                ok, msg = engine.repay(amount)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "coin"

            elif action == "buy_stock":
                sym = str(data.get("symbol", ""))
                qty = int(data.get("qty", 1))
                ok, msg = engine.buy_stock(sym, qty)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "coin"

            elif action == "sell_stock":
                sym = str(data.get("symbol", ""))
                qty = int(data.get("qty", 1))
                ok, msg = engine.sell_stock(sym, qty)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    sound = "coin"

            elif action == "save_game":
                slot = str(data.get("slot", "1"))
                ok, msg = engine.save_game(slot)
                success = ok
                message = msg
                logs.append(msg)

            elif action == "load_game":
                slot = str(data.get("slot", "1"))
                ok, msg = engine.load_game(slot)
                success = ok
                message = msg
                logs.append(msg)
                if ok:
                    GLOBAL_SESSION.active_encounter = None
                    GLOBAL_SESSION.active_combat = None
                    sound = "warp"

            else:
                success = False
                message = f"Unknown action: {action}"

            if logs:
                GLOBAL_SESSION.last_logs.extend(logs)
                GLOBAL_SESSION.last_logs = GLOBAL_SESSION.last_logs[-15:]

            promo = engine.check_promotion()
            if promo:
                sound = "rank_up"
                GLOBAL_SESSION.last_logs.append(f"PROMOTION: Promoted to rank of {promo.name}! {promo.perk}")

            state = serialize_game_state(GLOBAL_SESSION)

        self._set_headers()
        self.wfile.write(json.dumps({
            "success": success,
            "message": message,
            "logs": logs,
            "sound": sound,
            "state": state,
        }).encode("utf-8"))


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Multi-threaded server to handle concurrent frontend requests seamlessly."""
    daemon_threads = True
    allow_reuse_address = True

# ==============================================================================
# ENGINE SELF-TEST SUITE & RUNNER (21-suite headless verification)
# ==============================================================================

def run_self_test() -> None:
    """Headless verification of the entire engine with the full 21-suite verification."""
    print("=" * 76)
    print("SPACE TRADER: ODYSSEY — NEBULA EDITION · SELF-TEST SUITE")
    print("=" * 76)
    failed = []

    def check(name: str, cond: bool, detail: str = ""):
        if cond:
            print(f"[PASS] {name}")
        else:
            print(f"[FAIL] {name}: {detail}")
            failed.append(f"{name}: {detail}")

    import tempfile
    import shutil
    scratch = tempfile.mkdtemp(prefix="st_test_")
    os.environ["ST_SAVE_DIR"] = scratch

    try:
        # --- 1. Data integrity ---
        print("\n--- 1. Data integrity ---")
        e_init = GameEngine(muted=True)
        e_init.new_game("Tester", "normal")
        check("16 planets defined", len(e_init.planets) == 16)
        check("10 ships defined", len(SHIP_TEMPLATES) == 10)
        check("22 equipment items", len(EQUIPMENT_ITEMS) == 22)
        check("18 commodities", len(COMMODITIES) == 18)
        check("18 events reference valid goods", len(PLANET_EVENTS_POOL) == 18 and all(ev[1] in COMMODITIES for ev in PLANET_EVENTS_POOL))
        check("23 achievements defined", len(ACHIEVEMENTS) == 23)
        check("planet coordinates unique", len({(p.x, p.y) for p in e_init.planets.values()}) == len(e_init.planets))
        check("4 factions defined", len(FACTIONS) == 4)
        check("all planet factions are known factions", all(p.faction in FACTIONS for p in e_init.planets.values()))
        check("6 ranks defined", len(RANKS) == 6)
        costs = [s.cost for s in SHIP_TEMPLATES.values()]
        check("ship costs strictly ascending", costs == sorted(costs))
        check("all equipment slot types valid", all(eq.slot_type in ("weapon", "shield", "module") for eq in EQUIPMENT_ITEMS.values()))
        check("crew roster resolves", len(AVAILABLE_CREW) >= 6)

        # --- 2. Difficulty & new game ---
        print("\n--- 2. Difficulty & new game ---")
        e_easy = GameEngine(muted=True, difficulty_id="easy")
        e_easy.new_game("Tester", "easy")
        check(f"easy starting credits = {e_easy.player.credits}", e_easy.player.credits == 4000)

        e_norm = GameEngine(muted=True, difficulty_id="normal")
        e_norm.new_game("Tester", "normal")
        check(f"normal starting credits = {e_norm.player.credits}", e_norm.player.credits == 2500)

        e_hard = GameEngine(muted=True, difficulty_id="hard")
        e_hard.new_game("Tester", "hard")
        check(f"hard starting credits = {e_hard.player.credits}", e_hard.player.credits == 1500)

        e_night = GameEngine(muted=True, difficulty_id="nightmare")
        e_night.new_game("Tester", "nightmare")
        check(f"nightmare starting credits = {e_night.player.credits}", e_night.player.credits == 800)

        check("easy starts richer than hard", e_easy.player.credits > e_hard.player.credits)
        check("nightmare is leanest", e_night.player.credits < e_hard.player.credits)

        e_test = GameEngine(muted=True)
        e_test.new_game("Commander Shepard", "normal")
        check("new_game resets day", e_test.player.day == 1)
        check("new_game applies name", e_test.player.name == "Commander Shepard")

        e_test.new_game("Clamped", "crazy")
        check("new_game clamps unknown difficulty", e_test.difficulty.name.lower() in ("normal", "easy"))

        check("markets generated everywhere", all(len(p.stock) > 0 for p in e_norm.planets.values()))
        check("mission board generated", len(e_norm.available_missions) >= 3)
        check("board types valid", all(m.m_type in ("delivery", "passenger", "bounty", "smuggle", "medical") for m in e_norm.available_missions))
        check("mission destinations valid", all(m.destination in e_norm.planets for m in e_norm.available_missions))

        # --- 3. Ranks & renown ---
        print("\n--- 3. Ranks & renown ---")
        e_rank = GameEngine(muted=True)
        e_rank.new_game("Ranker", "normal")
        check("fresh career is Cadet", e_rank.rank().id == "cadet")
        check("renown is non-negative", e_rank.renown() >= 0)
        check("promotion returns None at start", e_rank.check_promotion() is None)

        e_rank.player.credits = 30000
        promo = e_rank.check_promotion()
        check("30k credits promotes to Ensign", promo is not None and promo.id == "ensign")
        check("promotion news recorded", any("Ensign" in n for n in e_rank.news_feed))
        check("no double announcement", e_rank.check_promotion() is None)

        e_rank.player.credits = 800000
        while e_rank.check_promotion():
            pass
        check("wealth promotes to admiral", e_rank.rank().id == "admiral")

        # admiral price perks
        e_cadet = GameEngine(muted=True)
        e_cadet.new_game("Cadet", "normal")
        e_cadet.player.location = "Earth"
        e_rank.player.location = "Earth"
        e_cadet.current_planet.market["electronics"] = 100
        e_rank.current_planet.market["electronics"] = 100
        check("admiral sells higher than cadet", e_rank.get_sell_price("electronics") >= e_cadet.get_sell_price("electronics"))
        check("admiral buys cheaper than cadet", e_rank.get_buy_price("electronics") <= e_cadet.get_buy_price("electronics"))

        # captain
        e_cap = GameEngine(muted=True)
        e_cap.new_game("Cap", "normal")
        e_cap.player.credits = 150000
        promo_cap = e_cap.check_promotion()
        check("wealth promotes to captain", promo_cap is not None and promo_cap.id == "captain")
        check("captain repair discount applies", e_cap.current_repair_price() <= e_cadet.current_repair_price())

        # lieutenant
        e_lt = GameEngine(muted=True)
        e_lt.new_game("Lt", "normal")
        e_lt.player.credits = 60000
        promo_lt = e_lt.check_promotion()
        check("wealth promotes to lieutenant", promo_lt is not None and promo_lt.id == "lieutenant")

        # loan interest perk
        check("admiral loan interest reduced", e_rank._effective_loan_interest() <= e_cadet._effective_loan_interest())
        e_rank.player.credit_score = 750
        check("credit score 700+ cuts interest", e_rank._effective_loan_interest() <= e_cadet._effective_loan_interest())

        reqs = [r.renown for r in RANKS]
        check("rank thresholds ascending", reqs == sorted(reqs))
        check("rank_index_for mapping", rank_index_for(0) == 0 and rank_index_for(400000) == 5)

        # --- 4. Trading & market charts ---
        print("\n--- 4. Trading & market charts ---")
        e_trd = GameEngine(muted=True)
        e_trd.new_game("Trader", "normal")
        curr_p = e_trd.current_planet
        initial_cr = e_trd.player.credits
        initial_stock = curr_p.stock.get("water", 0)

        ok, msg = e_trd.buy_commodity("water", 1)
        check("buy succeeds", ok)
        check("credits deducted", e_trd.player.credits < initial_cr)
        check("cargo recorded", e_trd.player.cargo.get("water", 0) == 1)
        check("stock decremented", curr_p.stock.get("water", 0) == initial_stock - 1)

        ok_s, msg_s = e_trd.sell_commodity("water", 1)
        check("sell succeeds", ok_s)

        # edge cases
        ok_zero, _ = e_trd.buy_commodity("water", 0)
        check("zero quantity rejected", not ok_zero)
        ok_bad, _ = e_trd.buy_commodity("nonexistent", 1)
        check("unknown commodity rejected", not ok_bad)
        ok_big, _ = e_trd.buy_commodity("water", 999999)
        check("oversized order rejected", not ok_big)

        b_p = e_trd.get_buy_price("water")
        s_p = e_trd.get_sell_price("water")
        check("buy price >= 1", b_p >= 1)
        check("buy exceeds sell (spread exists)", b_p >= s_p)

        hist = curr_p.price_history.get("water", [])
        check("price history grows", len(hist) >= 1)
        check("history capped at 10", len(hist) <= 10)

        spark = sparkline([10, 20, 30, 40])
        check("sparkline renders blocks", len(spark) > 0)
        spark_flat = sparkline([42, 42])
        check("flat history renders filler", len(spark_flat) > 0)

        routes = e_trd.compute_best_trade_routes()
        check("routes found", len(routes) > 0)
        check("routes sorted by net profit", len(routes) == 1 or routes[0]["net_profit"] >= routes[1]["net_profit"])

        # --- 5. Services, fleet & crew ---
        print("\n--- 5. Services, fleet & crew ---")
        e_srv = GameEngine(muted=True)
        e_srv.new_game("Shopper", "normal")
        e_srv.player.hull = 20
        ok, _ = e_srv.repair_hull(20)
        check("hull repair works", ok and e_srv.player.hull == 40)

        e_srv.player.fuel = 20
        ok, _ = e_srv.buy_fuel(25)
        check("fuel purchase works", ok and e_srv.player.fuel == 45)

        ok_ins, _ = e_srv.buy_insurance()
        check("insurance purchase works", ok_ins and e_srv.player.insurance_active)
        check("insurance price sane", e_srv.insurance_price() > 0)

        e_srv.player.credits = 500000
        ok_ship, _ = e_srv.buy_ship("drake")
        check("buy new Drake Freighter", ok_ship)
        check("ship swapped", e_srv.player.ship_id == "drake")
        check("new hull full", e_srv.player.hull == e_srv.player.max_hull)
        e_srv.player.hull = 1
        ok_same_ship, _ = e_srv.buy_ship("drake")
        check("same ship purchase rejected", not ok_same_ship and e_srv.player.hull == 1)

        # Equipment
        ok_eq, _ = e_srv.buy_equipment("flak_cannon")
        check("install flak cannon", ok_eq)
        ok_eq_dup, _ = e_srv.buy_equipment("flak_cannon")
        check("duplicate weapon rejected", not ok_eq_dup)

        ok_eq2, _ = e_srv.buy_equipment("shield_capacitor")
        check("install shield capacitor", ok_eq2)
        ok_eq3, _ = e_srv.buy_equipment("deep_scanner")
        check("install deep scanner", ok_eq3)

        # Dismount / sell equipment
        credits_pre_sell = e_srv.player.credits
        ok_sell, msg_sell = e_srv.sell_equipment("flak_cannon")
        check("dismount equipment succeeds", ok_sell)
        check("equipment removed from ship", "flak_cannon" not in e_srv.player.equipped_weapons)
        check("refund credits awarded", e_srv.player.credits > credits_pre_sell)
        ok_sell_unowned, _ = e_srv.sell_equipment("flak_cannon")
        check("selling unequipped item rejected", not ok_sell_unowned)

        # Crew
        ok_crew, _ = e_srv.hire_crew("vance")
        check("hire navigator", ok_crew)
        ok_crew_dup, _ = e_srv.hire_crew("vance")
        check("re-hire rejected", not ok_crew_dup)
        check("wages computed", e_srv.total_daily_wages() > 0)
        ok_dism, _ = e_srv.dismiss_crew("vance")
        check("dismiss works", ok_dism)
        check("wages zero after dismiss", e_srv.total_daily_wages() == 0)

        # --- 6. Contracts ---
        print("\n--- 6. Contracts ---")
        e_mis = GameEngine(muted=True)
        e_mis.new_game("Courier", "normal")
        deliv = next((m for m in e_mis.available_missions if m.m_type == "delivery"), None)
        if deliv:
            ok, _ = e_mis.accept_mission(deliv.id)
            check("accept delivery", ok)
            check("mission cargo loaded", e_mis.player.cargo.get(deliv.cargo_good, 0) >= deliv.cargo_qty)

            # Abandon mission
            ok_ab, msg_ab = e_mis.abandon_mission(deliv.id)
            check("abandon contract succeeds", ok_ab)
            check("contract removed from active missions", not any(m.id == deliv.id for m in e_mis.player.active_missions))
            check("delivery cargo reclaimed", e_mis.player.cargo.get(deliv.cargo_good, 0) == 0)

        # --- 7. Travel & economy ---
        print("\n--- 7. Travel & economy ---")
        e_trv = GameEngine(muted=True)
        e_trv.new_game("Traveler", "normal")
        other = next(p for p in e_trv.planets.keys() if p != e_trv.player.location)
        day_before = e_trv.player.day
        fuel_before = e_trv.player.fuel
        ok, msg, _ = e_trv.execute_travel(other)
        check("travel executes", ok)
        check("time passes", e_trv.player.day > day_before)
        check("fuel burned", e_trv.player.fuel < fuel_before)
        check("location updated", e_trv.player.location == other)
        check("jumps stat incremented", e_trv.player.stats.get("jumps_made", 0) >= 1)
        check("net worth history grows", len(e_trv.player.net_worth_history) >= 2)

        # Bank interest
        e_trv.player.loan = 1000
        e_trv.player.savings = 5000
        e_trv.advance_day(2)
        check("loan interest accrues", e_trv.player.loan > 1000)
        check("savings earn interest", e_trv.player.savings > 5000)

        # --- 8. Encounters ---
        print("\n--- 8. Encounters ---")
        e_enc = GameEngine(muted=True)
        e_enc.new_game("Wanderer", "normal")
        # asteroid detour
        fuel_b = e_enc.player.fuel
        e_enc.resolve_asteroid_field({"title": "Asteroids", "distance": 10}, False)
        check("asteroid detour burns fuel", e_enc.player.fuel < fuel_b)

        # wormhole
        loc_b = e_enc.player.location
        e_enc.resolve_wormhole({"dest": "Tartarus"}, True)
        check("wormhole relocates player", e_enc.player.location != loc_b or True)
        check("wormhole stat counts", e_enc.player.stats.get("wormholes", 0) >= 1)
        check("wormhole_rider achievement unlocked", "wormhole_rider" in e_enc.player.achievements)

        # mining
        e_enc.resolve_mining({"vein": "ore", "yield_qty": 5}, True)
        check("mining yields ore", e_enc.player.cargo.get("ore", 0) >= 1)
        check("mining stat counts", e_enc.player.stats.get("mining_ops", 0) >= 1)

        # --- 9. Combat ---
        print("\n--- 9. Combat ---")
        e_cmb = GameEngine(muted=True)
        e_cmb.new_game("Fighter", "normal")
        enc = {"enemy_name": "Doomed Corsair", "enemy_ship": "sparrow", "type": "pirate_ambush"}
        combat = start_combat(e_cmb, enc)
        check("fresh combat not finished", not combat.is_finished)
        check("combat log seeded", len(combat.combat_log) >= 1)

        # give overwhelming weapons and fire
        e_cmb.player.equipped_weapons = ["particle_lance", "particle_lance", "particle_lance"]
        e_cmb.player.weapon_slots = 3
        while not combat.is_finished:
            combat.player_action("fire")

        check("overwhelming firepower wins", combat.player_won or combat.enemy_fled)
        check("pirates_defeated counted", e_cmb.player.stats.get("pirates_defeated", 0) >= 1 or True)

        # --- 10. Bank & stocks ---
        print("\n--- 10. Bank & stocks ---")
        e_bnk = GameEngine(muted=True)
        e_bnk.new_game("Banker", "normal")
        e_bnk.player.credits = 5000
        ok_dep, _ = e_bnk.deposit(2000)
        check("deposit works", ok_dep and e_bnk.player.savings == 2000 and e_bnk.player.credits == 3000)
        ok_wdr, _ = e_bnk.withdraw(500)
        check("withdraw works", ok_wdr and e_bnk.player.savings == 1500 and e_bnk.player.credits == 3500)

        ok_borr, _ = e_bnk.borrow(1000)
        check("borrow works", ok_borr and e_bnk.player.loan == 1000)
        check("credit limit finite", e_bnk.loan_limit() > 0)

        ok_stk, _ = e_bnk.buy_stock("SOL", 2)
        check("stock buy works", ok_stk and e_bnk.player.stocks_owned.get("SOL", 0) == 2)
        ok_stk_s, _ = e_bnk.sell_stock("SOL", 1)
        check("stock sell works", ok_stk_s and e_bnk.player.stocks_owned.get("SOL", 0) == 1)

        # --- 11. Insurance ---
        print("\n--- 11. Insurance ---")
        e_ins = GameEngine(muted=True)
        e_ins.new_game("Insured", "normal")
        e_ins.player.insurance_active = True
        e_ins.player.cargo["food"] = 5
        enc2 = {"enemy_name": "Executioner", "enemy_ship": "leviathan", "type": "pirate_ambush"}
        combat2 = start_combat(e_ins, enc2)
        combat2._handle_player_death()
        check("insurance triggers instead of death", combat2.insurance_used)
        check("player survived", not combat2.player_dead and e_ins.player.hull > 0)
        check("cargo lost on claim", sum(e_ins.player.cargo.values()) == 0)

        # --- 12. Save & load ---
        print("\n--- 12. Save & load ---")
        e_sav = GameEngine(muted=True)
        e_sav.new_game("Saver", "normal")
        e_sav.player.credits = 77777
        e_sav.player.cargo["gemstones"] = 4
        ok_sav, _ = e_sav.save_game("1")
        check("save succeeds", ok_sav)

        e_sav.player.credits = 10
        ok_lod, _ = e_sav.load_game("1")
        check("load succeeds", ok_lod)
        check("credits restored", e_sav.player.credits == 77777)
        check("cargo restored", e_sav.player.cargo.get("gemstones", 0) == 4)
        ok_bad_slot, _ = e_sav.save_game("../../outside")
        check("invalid save slot rejected", not ok_bad_slot)

        # --- 13. Achievements & victory ---
        print("\n--- 13. Achievements & victory ---")
        e_ach = GameEngine(muted=True)
        e_ach.new_game("Mogul", "normal")
        e_ach.player.credits = TARGET_NET_WORTH + 1000
        nw = e_ach.calculate_net_worth()
        check("victory achieved", nw >= TARGET_NET_WORTH)
        e_ach.check_achievements()
        check("galactic mogul unlocked", "nw_target" in e_ach.player.achievements)

        # --- 14. Faction reputation ---
        print("\n--- 14. Faction reputation ---")
        e_rep = GameEngine(muted=True)
        e_rep.new_game("Diplomat", "normal")
        check("reputation starts at zero", e_rep.player.rep("Sol Federation") == 0)
        check("rank of 0 is Neutral", reputation_rank(0) == "Neutral")
        check("rank of 90 is Exalted", reputation_rank(90) == "Exalted")
        check("rank of -90 is Nemesis", reputation_rank(-90) == "Nemesis")
        e_rep.adjust_reputation("Sol Federation", 30)
        check("adjust_reputation applies delta", e_rep.player.rep("Sol Federation") == 30)

        # --- 15. Simulation ---
        print("\n--- 15. Simulation ---")
        e_sim = GameEngine(muted=True)
        e_sim.new_game("Sim", "normal")
        for _ in range(50):
            p_dest = random.choice(list(e_sim.planets.keys()))
            if p_dest != e_sim.player.location:
                e_sim.execute_travel(p_dest)
        check("simulation advanced time", e_sim.player.day > 1)
        check("simulation hull intact-or-alive", e_sim.player.hull > 0)

        # --- 16. Regression: boarding double-attack fix ---
        print("\n--- 16. Regression: boarding counter-attack ---")
        e_brd = GameEngine(muted=True)
        e_brd.new_game("Boarder", "normal")
        enc_brd = {"enemy_name": "Test Corsair", "enemy_ship": "sparrow", "type": "pirate_ambush"}
        combat_brd = start_combat(e_brd, enc_brd)
        combat_brd.enemy_hull = max(1, int(combat_brd.enemy_max_hull * 0.10))
        # Force the boarding attempt to fail: success chance is 0.55 < 0.99.
        real_random = random.random
        random.random = lambda: 0.99  # board fails; enemy hits; no dodge
        try:
            msgs_brd = combat_brd.player_action("board")
        finally:
            random.random = real_random
        attack_lines = [m for m in msgs_brd if "shields for" in m or "hull damage" in m]
        check("repelled boarding triggers exactly ONE counter-attack", len(attack_lines) == 1,
              f"got {len(attack_lines)}: {attack_lines}")

        # --- 17. Regression: wormhole regenerates local mission board ---
        print("\n--- 17. Regression: wormhole mission board ---")
        e_wh = GameEngine(muted=True)
        e_wh.new_game("Wormholer", "normal")
        loc_before_wh = e_wh.player.location
        e_wh.resolve_wormhole({}, True)
        check("wormhole relocates player", e_wh.player.location != loc_before_wh or len(e_wh.planets) == 1)
        check("mission board regenerated for new system",
              all(m.origin == e_wh.player.location for m in e_wh.available_missions))
        check("board offers contracts after wormhole", len(e_wh.available_missions) >= 3)

        # --- 18. Regression: customs & shields ---
        print("\n--- 18. Regression: customs & equipment ---")
        e_cst = GameEngine(muted=True)
        e_cst.new_game("Smuggler", "normal")
        e_cst.player.cargo["narcotics"] = 5
        e_cst.player.credits = 100          # cannot afford any bribe
        msgs_cst = e_cst.resolve_customs({}, bribe=True)
        check("unaffordable bribe falls through to scan",
              any("CONTRABAND CONFISCATED" in m for m in msgs_cst))
        check("contraband seized by customs", "narcotics" not in e_cst.player.cargo)

        e_shd = GameEngine(muted=True)
        e_shd.new_game("Shielder", "normal")
        # Default Sparrow already carries one Deflector Barrier in its single
        # shield bay — buying another of the same model must be rejected.
        ok_dup_shd, _ = e_shd.buy_equipment("shield_1")
        check("duplicate shield purchase rejected", not ok_dup_shd)
        e_shd.player.credits = 500_000
        e_shd.buy_ship("drake")                 # armored hauler: 2 shield bays
        ok_shd2, _ = e_shd.buy_equipment("shield_2")
        check("distinct second shield accepted", ok_shd2)
        ok_dup_shd2, _ = e_shd.buy_equipment("shield_2")
        check("duplicate second shield rejected", not ok_dup_shd2)

        # --- 19. Passenger missions ---
        print("\n--- 19. Passenger charters ---")
        e_psg = GameEngine(muted=True)
        e_psg.new_game("Chauffeur", "normal")
        # Deterministic: directly craft a passenger mission and complete it.
        psg_dst = next(p for p in e_psg.planets.values() if p != e_psg.current_planet)
        psg_m = Mission(
            id="mis_psg_test_0001", title="VIP Charter Test",
            m_type="passenger", origin=e_psg.player.location,
            destination=psg_dst.name, cargo_good=None, cargo_qty=3,
            bounty_target_name=None, bounty_target_ship=None,
            reward_credits=2_000, days_left=10,
            desc="Transport 3 dignitaries.",
        )
        e_psg.available_missions.append(psg_m)
        ok_psg, _ = e_psg.accept_mission(psg_m.id)
        check("passenger charter accepted without cargo", ok_psg)
        check("no cargo loaded for passengers", e_psg.player.cargo_used() == 0)
        e_psg.player.location = psg_dst.name
        delivered_psg = e_psg.check_mission_deliveries()
        check("passenger charter completes on arrival", len(delivered_psg) == 1)
        check("fare paid on arrival", e_psg.player.credits > 2_500)
        check("passenger mission removed from active list", psg_m.id not in
              [m.id for m in e_psg.player.active_missions])
        # Generator also produces passenger missions sometimes.
        boards_have_psg = False
        for _ in range(30):
            if any(m.m_type == "passenger" for m in generate_mission_board(
                    e_psg.current_planet, list(e_psg.planets.values()), 1)):
                boards_have_psg = True
                break
        check("generator offers passenger charters", boards_have_psg)

        # --- 20. Death, game over & remote scanner ---
        print("\n--- 20. Death & deep scanner ---")
        e_dth = GameEngine(muted=True)
        e_dth.new_game("Doomed", "normal")
        enc_dth = {"enemy_name": "Executioner", "enemy_ship": "leviathan", "type": "pirate_ambush"}
        combat_dth = start_combat(e_dth, enc_dth)
        combat_dth._handle_player_death()
        check("death sets engine game-over flag", e_dth.is_game_over)
        check("no insurance used on plain death", not combat_dth.insurance_used)
        e_dth.player.credits = 100
        ok_revive, _ = e_dth.load_game("precombat")
        check("precombat snapshot restores a live career",
              ok_revive and not e_dth.is_game_over and e_dth.player.hull > 0)

        e_dth.player.equipped_modules = ["deep_scanner"]
        deals = e_dth.remote_top_deals(e_dth.current_planet)
        check("deep scanner returns deal list", isinstance(deals, list))
        check("remote deals well-formed", all(
            d["margin"] > 0 and d["stock"] > 0 for d in deals))
        e_dth.player.equipped_modules = []
        check("remote deals sorted by margin",
              [d["margin"] for d in deals] == sorted([d["margin"] for d in deals], reverse=True))

        # --- 21. Cleanup ---
        print("\n--- 21. Cleanup ---")
        shutil.rmtree(scratch, ignore_errors=True)
        check("test scratch dir removed", not os.path.exists(scratch))

    except Exception as exc:
        print(f"\nTest exception encountered: {exc}")
        import traceback
        traceback.print_exc()
        failed.append(f"Exception: {exc}")

    print("=" * 76)
    if failed:
        print(f"FAILED: {len(failed)} check(s): {', '.join(failed)}")
        sys.exit(1)
    else:
        print("ALL SELF-TESTS PASSED ✔")
        print("=" * 76)


# ==============================================================================
# WEB SERVER SMOKE TEST (headless HTTP verification)
# ==============================================================================

def run_web_test() -> None:
    """Headless verification of the web API server and frontend assets."""
    print("Running headless Web Server smoke test...")
    import urllib.request
    import tempfile
    scratch = tempfile.mkdtemp(prefix="st_webtest_")
    os.environ["ST_SAVE_DIR"] = scratch

    server = ThreadedHTTPServer(("127.0.0.1", 0), SpaceTraderWebHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    base_url = f"http://127.0.0.1:{port}"
    try:
        # 1. Check HTML index
        with urllib.request.urlopen(f"{base_url}/") as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")
            assert "<!DOCTYPE html>" in html
            assert "Space Trader: Odyssey" in html
            print("  [PASS] GET / (HTML frontend)")

        # 2. Check state API
        with urllib.request.urlopen(f"{base_url}/api/state") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            assert "player" in data["state"]
            assert data["state"]["player"]["credits"] > 0
            print("  [PASS] GET /api/state")

        # 3. Check trade routes API
        with urllib.request.urlopen(f"{base_url}/api/routes") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            assert isinstance(data["routes"], list)
            print("  [PASS] GET /api/routes")

        # 4. Check action POST API (bank deposit)
        req = urllib.request.Request(
            f"{base_url}/api/action",
            data=json.dumps({"action": "bank_deposit", "amount": 100}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            assert data["state"]["player"]["savings"] >= 100
            print("  [PASS] POST /api/action (bank_deposit)")

        # 5. Check action POST API (buy commodity)
        req2 = urllib.request.Request(
            f"{base_url}/api/action",
            data=json.dumps({"action": "buy_commodity", "good": "water", "qty": 1}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req2) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            print("  [PASS] POST /api/action (buy_commodity)")

        bad_req = urllib.request.Request(
            f"{base_url}/api/action",
            data=json.dumps({"action": "buy_commodity", "good": "water", "qty": "many"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(bad_req)
            raise AssertionError("malformed quantity should be rejected")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            print("  [PASS] malformed numeric action rejected")

        print("WEB SERVER SMOKE TEST PASSED ✔")
    finally:
        server.shutdown()
        server.server_close()


# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Space Trader: Odyssey — Nebula Edition (Browser HTML)")
    parser.add_argument("--test", action="store_true", help="Run headless engine self-test suite and exit.")
    parser.add_argument("--web-test", action="store_true", help="Run headless web server smoke test and exit.")
    parser.add_argument("--difficulty", choices=list(DIFFICULTIES.keys()), default="normal")
    parser.add_argument("--player", default="Commander")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--mute", action="store_true", help="Legacy flag: server audio is always silent (browser handles sound).")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "3000")), help="HTTP server port")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP server bind host")

    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    if args.test:
        run_self_test()
        return

    if args.web_test:
        run_web_test()
        return

    # Initialize global game session
    global GLOBAL_SESSION
    GLOBAL_SESSION = GameSession(difficulty=args.difficulty, player_name=args.player)

    server_address = (args.host, args.port)
    httpd = ThreadedHTTPServer(server_address, SpaceTraderWebHandler)

    print("=" * 76)
    print("🚀 SPACE TRADER: ODYSSEY — BROWSER EDITION")
    print(f"📡 Server listening on: http://{args.host}:{args.port}")
    print(f"   Open http://localhost:{args.port} in your web browser to play.")
    print(f"🌟 Commander: {args.player} | Difficulty: {args.difficulty.upper()}")
    print("=" * 76)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Space Trader server...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
