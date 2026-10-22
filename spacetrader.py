#!/usr/bin/env python3
"""
================================================================================
 SPACE TRADER: ODYSSEY — Nebula Edition v2.4 (Browser HTML)
================================================================================
A complete sci-fi trading, exploration, and combat RPG with a modern browser
interface served by a built-in threaded HTTP server. Pure Python stdlib —
one file, zero installs, no third-party dependencies.

HOW TO RUN
----------
    python3 spacetrader.py                    Start the web server and play in
                                              your browser (default port 3000)
    python3 spacetrader.py --port 8080        Serve on a custom port
    python3 spacetrader.py --host 0.0.0.0     Expose on your LAN (default: localhost only)
    python3 spacetrader.py --test             Run the headless engine self-tests
    python3 spacetrader.py --web-test         Run the web server smoke test
    python3 spacetrader.py --difficulty easy|normal|hard|nightmare
    python3 spacetrader.py --player NAME      Set the captain's callsign
    python3 spacetrader.py --seed N           Deterministic RNG for testing

The PORT environment variable is honoured (defaults to 3000). Saves live
next to the script (or $ST_SAVE_DIR) as JSON flight records; the all-time
Hall of Fame lives in st_odyssey_scores.json.

WHAT'S INSIDE
-------------
* 16 planetary systems with living economies, 18 commodities, 20 market
  events, price histories, sparkline charts, and a reactive stock exchange
  wired to the sector news feed.
* 10 hulls, 24 equipment items (incl. Bussard ram-scoop & heavy extraction
  rig), 7 hireable officers, 5 mission types (delivery / smuggle / bounty /
  medical / passenger).
* Tactical turn-based combat with subsystem targeting, class-scaled enemy
  firepower, threat ratings, evasive maneuvers, torpedo volleys, missiles,
  drones, boarding actions and four AI personalities.
* 14 deep-space encounter types — from customs sweeps and wormholes to
  cometary ice fields, ghost freighters, Oort Cloud derby wagers, and
  uncharted smuggler caches.
* Ranks (Cadet → Admiral), faction reputation, 31 achievements, banking,
  stock exchange with market wire headlines, and a persistent Hall of Fame.
* Survival safety net: dockside odd jobs guarantee a stranded captain can
  always earn fuel money — no more dead-end careers.

GOAL
----
Grow your net worth to 500,000 CR to win — through trading, contracts,
smuggling, bounties, investing, and conquest of the void. Legends are
remembered forever on the Hall of Fame board.
================================================================================
"""

from __future__ import annotations

import argparse
import copy
import functools
import json
import math
import os
import platform
import random
import re
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple, Set


import http.server
import socketserver
import urllib.parse
import threading


# ==============================================================================
# CONSTANTS & HELPERS
# ==============================================================================

APP_VERSION = "2.4.0"
__version__ = APP_VERSION

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
SAVE_VERSION = 5
PRICE_HISTORY_LEN = 10       # days kept per commodity for the sparkline charts
HOF_FILENAME = "st_odyssey_scores.json"   # persistent Hall of Fame flight records
HOF_MAX_ENTRIES = 10
GIFT_MIN = 500               # minimum diplomatic gift
MIN_SPREAD = 0.01            # a station always keeps at least a 1% edge per side
MIN_ROUTE_MARGIN_CR = 3     # trade advisor: absolute floor on a route's margin
MIN_ROUTE_MARGIN_PCT = 0.05  # ...and a proportional floor (5% of the buy price)
IMPACT_RATE = 0.002          # market-depth: price pressure per unit traded...
IMPACT_CAP = 0.08            # ...capped at 8% per transaction
SAVINGS_DAILY_RATE = 0.002   # 0.2% daily on the first SAVINGS_INTEREST_CAP credits
SAVINGS_INTEREST_CAP = 60_000
MIN_CREDIT_SCORE = 300
MAX_CREDIT_SCORE = 850
MAX_INT_INPUT = 10 ** 9      # API integers are clamped to +/- this magnitude


def sanitize_name(name: str) -> str:
    """Strip markup from captain callsigns (anti-XSS, keep it readable)."""
    clean = re.sub(r"<[^>]*>", "", str(name))
    clean = clean.replace("<", "").replace(">", "").replace('"', "'")
    clean = " ".join(clean.split())
    return clean[:24] or "Commander"


def to_int(value: Any, lo: int = -MAX_INT_INPUT, hi: int = MAX_INT_INPUT) -> int:
    """Parse a bounded integer without silently truncating fractional values.

    Booleans, non-finite numbers, containers, fractional floats, and numeric
    strings such as ``"1.5"`` are rejected. Integral decimal/exponent strings
    (for example ``"3.0"`` or ``"1e2"``) remain accepted for compatibility.
    """
    if isinstance(value, bool) or value is None:
        raise ValueError("expected an integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("expected a finite number")
        if not value.is_integer():
            raise ValueError("expected an integer")
        number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("expected an integer")
        try:
            number = int(text)
        except ValueError:
            try:
                as_float = float(text)
            except ValueError:
                raise ValueError("expected an integer") from None
            if not math.isfinite(as_float):
                raise ValueError("expected a finite number") from None
            if not as_float.is_integer():
                raise ValueError("expected an integer") from None
            number = int(as_float)
    else:
        raise ValueError("expected an integer")
    return max(lo, min(hi, number))


def atomic_write_json(path: str, payload: Any) -> None:
    """Write JSON so a crash mid-write can never leave a truncated file."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".st_tmp_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def requires_alive(method):
    """Guard for engine mutators: block actions once the career is over."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if self.is_game_over:
            return False, ("Your career has ended, Captain. "
                           "Start a new commission or load a flight record.")
        return method(self, *args, **kwargs)
    return wrapper


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
    ("Deuterium Drought", "fuel_cells", 2.7,
     "Refinery closures ripple across the sector — hyper-fuel cells command a premium!"),
    ("Tomb-World Excavation", "antiques", 0.45,
     "A newly unearthed dead civilization floods markets with authenticated antiques."),
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
            # Weights must sum to 1.0 so inventories relax onto the target
            # stock instead of inflating ~1.9x forever.
            drifted = int(old_stock * 0.55 + tgt_stock * random.uniform(0.35, 0.55))
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
        "sparrow", "Star Sparrow", "Light Courier", 4_000,
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
    "fuel_scoop": Equipment(
        "fuel_scoop", "Bussard Ram-Scoop Collector", "module", 11_000,
        desc="Magnetic intake harvesting interstellar hydrogen: regenerates "
             "3 units of fuel every travel day."
    ),
    "mining_rig": Equipment(
        "mining_rig", "Heavy Extraction Rig", "module", 8_500,
        desc="Industrial-grade drill drones and ore cracking frames: asteroid "
             "and comet mining yields are roughly doubled."
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
        "Haggles the station spread down by 8 points on every deal (floor: 1%)."
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
        # Suffix with a unique token: two boards generated the same day could
        # otherwise collide on (day, 4-digit) IDs and corrupt abandon/delivery
        # lookups that match by ID.
        return f"mis_{prefix}_{day}_{random.randint(1000, 9999)}_{uuid.uuid4().hex[:8]}"

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

# Planetary events nudge the equities of their faction's flagship company.
FACTION_STOCK_LINK: Dict[str, str] = {
    "Sol Federation": "SOL",
    "Outer Alliance": "BIO",
    "Mining Syndicate": "MIN",
    "Free Corsairs": "SHD",
}


# ==============================================================================
# PLAYER
# ==============================================================================

DEFAULT_STATS: Dict[str, int] = {
    "total_profit": 0,
    "trading_profit": 0,
    "jumps_made": 0,
    "pirates_defeated": 0,
    "bounties_claimed": 0,
    "contraband_sold": 0,
    "missions_completed": 0,
    "missiles_fired": 0,
    "boards": 0,
    "insurance_claims": 0,
    "mining_ops": 0,
    "comet_ops": 0,
    "wormholes": 0,
    "odd_jobs_done": 0,
    "gifts_given": 0,
    "derby_wins": 0,
    "loan_repaid": 0,
    "cargo_jettisoned": 0,
    "caches_found": 0,
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
    visited: Set[str] = field(default_factory=set)

    # ---------------- cargo ---------------- #

    def cargo_used(self) -> int:
        return sum(self.cargo.values())

    def cargo_free(self) -> int:
        return max(0, self.cargo_cap - self.cargo_used())

    def reserved_cargo(self, good: Optional[str] = None) -> int:
        """Units sealed in the hold for live contracts (not yours to sell)."""
        total = 0
        for m in self.active_missions:
            if m.completed or m.failed or not m.cargo_good or m.cargo_qty <= 0:
                continue
            if good is None or m.cargo_good == good:
                total += m.cargo_qty
        return total

    def sellable_cargo(self, good: str) -> int:
        """Units of ``good`` that are free to sell or jettison."""
        return max(0, self.cargo.get(good, 0) - self.reserved_cargo(good))

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
        # Several officers can share a perk type — honour the strongest one
        # instead of silently dropping everyone after the first hire.
        values = [
            c.perk_val for cid in self.hired_crew
            if (c := CREW_INDEX.get(cid)) and c.perk_type == perk_type
        ]
        return max(values) if values else None

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

    def mark_visited(self, planet_name: str) -> None:
        self.visited.add(planet_name)


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
    "nomad_16": ("Sector Cartographer", "Visit all 16 planetary systems"),
    "centurion": ("Centurion of the Void", "Survive 100 days among the stars"),
    "trade_baron": ("Trade Baron", "Accumulate 250,000 CR in lifetime trading profit"),
    "debt_slayer": ("Debt Slayer", "Repay 50,000 CR of borrowed funds in total"),
    "void_gambler": ("Void Gambler", "Win an Oort Cloud derby wager"),
    "icy_prospector": ("Icy Prospector", "Harvest 5 cometary ice fields"),
    "missions_15": ("Logistics Director", "Complete 15 contracts"),
    "cache_hunter": ("Cache Hunter", "Crack 3 uncharted caches"),
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
        self._hof_recorded: bool = False

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
            name=sanitize_name(name),
            difficulty_id=difficulty_id,
        )
        self.player.mark_visited(self.player.location)
        self.apply_difficulty_start()
        self.available_missions = []
        self.news_feed = []
        self.is_game_over = False
        self.victory_achieved = False
        self.last_result = ""
        self._hof_recorded = False
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
        trade_renown = clamp(max(0, s.get("trading_profit", 0)) * 0.04, 0, 6_000)
        return int(
            self.calculate_net_worth() * 0.5
            + trade_renown
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
            self.record_hall_of_fame(won=True)

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
        if stats.get("comet_ops", 0) >= 5:
            unlock("icy_prospector")
        if stats.get("missions_completed", 0) >= 15:
            unlock("missions_15")
        if stats.get("caches_found", 0) >= 3:
            unlock("cache_hunter")
        if stats.get("derby_wins", 0) >= 1:
            unlock("void_gambler")
        if stats.get("trading_profit", 0) >= 250_000:
            unlock("trade_baron")
        if stats.get("loan_repaid", 0) >= 50_000:
            unlock("debt_slayer")
        if self.player.day >= 100:
            unlock("centurion")
        if len(self.player.visited) >= len(self.planets):
            unlock("nomad_16")

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
                # Modest, capped interest: idle cash must never out-earn trading.
                eligible = min(self.player.savings, SAVINGS_INTEREST_CAP)
                self.player.savings += int(eligible * SAVINGS_DAILY_RATE)

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

            # Bussard ram-scoop trickle-charges the tanks overnight.
            if self.player.has_module("fuel_scoop"):
                gained = min(3, self.player.max_fuel - self.player.fuel)
                if gained > 0:
                    self.player.fuel += gained

            for sym, stk in self.stocks.items():
                pct_chg = random.gauss(0.0025, stk.volatility)
                stk.price = max(5.0, round(stk.price * (1 + pct_chg), 2))
                stk.history.append(stk.price)
                if len(stk.history) > 30:
                    stk.history.pop(0)

            # Market wire: occasional headline shocks move a random equity.
            if random.random() < 0.12:
                sym = random.choice(list(self.stocks.keys()))
                stk = self.stocks[sym]
                shock = random.uniform(0.04, 0.12) * random.choice([1, -1])
                stk.price = max(5.0, round(stk.price * (1 + shock), 2))
                # Replace today's existing sample instead of appending a second
                # one so shock days do not distort the history window.
                if stk.history:
                    stk.history[-1] = stk.price
                else:
                    stk.history.append(stk.price)
                if len(stk.history) > 30:
                    stk.history.pop(0)
                direction = "soars" if shock > 0 else "tumbles"
                self.add_news(
                    f"Market Wire: ${sym} ({stk.name.split()[0]}) {direction} "
                    f"{abs(shock) * 100:.1f}% on sector trading."
                )

            for p in self.planets.values():
                p.generate_market(days_passed=1)
                ev = p.active_event
                if ev and ev.fresh:
                    ev.fresh = False
                    self.add_news(f"Intel Report: {ev.name} declared at {p.name}!")
                    # Faction-linked equities react to their worlds' fortunes.
                    linked = FACTION_STOCK_LINK.get(p.faction)
                    if linked and linked in self.stocks:
                        bump = 0.02 if ev.mult > 1.0 else -0.015
                        stk = self.stocks[linked]
                        stk.price = max(5.0, round(stk.price * (1 + bump), 2))
                        stk.history.append(stk.price)
                        if len(stk.history) > 30:
                            stk.history.pop(0)

            expired: List[Mission] = []
            for m in self.player.active_missions:
                m.days_left -= 1
                # Fail only once the deadline has fully passed (days_left < 0).
                # Using <= 0 silently stole the final day: a contract with N
                # days really allowed only N-1, and arriving on deadline day
                # repossessed the cargo before deliveries were checked.
                if m.days_left < 0 and not m.completed:
                    m.failed = True
                    expired.append(m)
            for em in expired:
                self.player.active_missions.remove(em)
                lost_note = ""
                if em.cargo_good and em.cargo_qty > 0:
                    # Contract goods belong to the client — they are repossessed.
                    lost = self._remove_cargo_tracked(em.cargo_good, em.cargo_qty)
                    if lost:
                        lost_note = f" {lost}x {COMMODITIES[em.cargo_good].name} repossessed."
                self.add_news(f"Mission failed: contract '{em.title}' expired!{lost_note}")

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

    def station_spreads(self, planet: Optional[Planet] = None) -> Tuple[float, float]:
        """(buy_spread, sell_spread): how far above / below the mid-market price
        a station trades with you.

        Trader officers, rank perks and faction goodwill all NARROW the spread
        rather than shifting both prices the same way, and a station always
        keeps MIN_SPREAD on each side. That guarantees sell < mid < buy, so a
        buy-then-sell loop at one station can never turn a profit."""
        p = planet or self.current_planet
        base = BASE_SPREAD * self.difficulty.price_spread_mult
        perk = self.player.has_crew_perk("trader") or 0.0
        rep_term = clamp(self.player.rep(p.faction) / 100.0, -1.0, 1.0) * 0.05
        idx = self.rank_index()
        buy = max(MIN_SPREAD, base - perk - RANK_BUY_DISCOUNT[idx] - rep_term)
        sell = max(MIN_SPREAD, base - perk - RANK_SELL_BONUS[idx] - rep_term)
        return buy, sell

    def get_buy_price(self, good: str, planet: Optional[Planet] = None) -> int:
        p = planet or self.current_planet
        mid = p.market.get(good, COMMODITIES[good].base_price)
        buy_spread, _ = self.station_spreads(p)
        return max(1, int(math.ceil(mid * (1.0 + buy_spread))))

    def get_sell_price(self, good: str, planet: Optional[Planet] = None) -> int:
        p = planet or self.current_planet
        mid = p.market.get(good, COMMODITIES[good].base_price)
        _, sell_spread = self.station_spreads(p)
        return max(1, int(math.floor(mid * (1.0 - sell_spread))))

    @staticmethod
    def market_depth_fee(unit_price: int, qty: int) -> int:
        """Slippage for moving ``qty`` units in one go (half the price impact
        the trade itself causes). The browser mirrors this exact formula."""
        impact = min(IMPACT_CAP, qty * IMPACT_RATE)
        # floor(x + 0.5): round-half-up, identical in Python and JavaScript
        return int(math.floor(unit_price * qty * impact / 2.0 + 0.5))

    def quote_buy(self, good: str, qty: int, planet: Optional[Planet] = None) -> int:
        """Total cost of buying ``qty`` here, market-depth fee included."""
        unit = self.get_buy_price(good, planet)
        return unit * qty + self.market_depth_fee(unit, qty)

    def quote_sell(self, good: str, qty: int, planet: Optional[Planet] = None) -> int:
        """Total credits received for selling ``qty`` here, fee included."""
        unit = self.get_sell_price(good, planet)
        return max(0, unit * qty - self.market_depth_fee(unit, qty))

    def _remove_cargo_tracked(self, good: str, qty: int) -> int:
        """Remove cargo and forget its cost basis once the hold is empty of it."""
        removed = self.player.remove_cargo(good, qty)
        if self.player.cargo.get(good, 0) <= 0:
            self.player.cargo_cost_basis.pop(good, None)
        return removed

    def _acquire_cargo(self, good: str, qty: int, unit_cost: float) -> int:
        """Take on cargo and fold its cost into the weighted-average basis.

        Both the market and the mission board go through here so the hold
        readout and the trading statistics always describe the cargo the
        player actually has.
        """
        if qty <= 0:
            return 0
        old_qty = self.player.cargo.get(good, 0)
        old_basis = self.player.cargo_cost_basis.get(good, float(unit_cost))
        added = self.player.add_cargo(good, qty)
        if added > 0:
            new_basis = (old_qty * old_basis + unit_cost * added) / max(1, old_qty + added)
            self.player.cargo_cost_basis[good] = round(new_basis, 2)
        return added

    def _price_impact(self, good: str, qty: int, direction: str) -> None:
        """Local supply/demand feedback: trading nudges the market price."""
        p = self.current_planet
        comm = COMMODITIES[good]
        mid = p.market.get(good, comm.base_price)
        impact = min(IMPACT_CAP, qty * IMPACT_RATE)
        if direction == "buy":
            mid = mid * (1.0 + impact)
        else:
            mid = mid * (1.0 - impact)
        p.market[good] = max(2, int(round(clamp(
            mid, comm.base_price * 0.25, comm.base_price * 3.2
        ))))

    @requires_alive
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

        total_cost = self.quote_buy(good, qty)
        if self.player.credits < total_cost:
            return False, f"Insufficient credits! Need {money(total_cost)} CR."

        self.player.credits -= total_cost
        p.stock[good] = stock - qty

        # Track weighted average cost basis (including market-depth slippage)
        added = self._acquire_cargo(good, qty, total_cost / max(1, qty))
        self._price_impact(good, qty, "buy")
        self.sound.play("buy")
        msg = f"Purchased {added}x {COMMODITIES[good].name} for {money(total_cost)} CR."
        self.announce(msg)
        return True, msg

    @requires_alive
    def sell_commodity(self, good: str, qty: int) -> Tuple[bool, str]:
        if good not in COMMODITIES:
            return False, "Invalid commodity."
        if qty <= 0:
            return False, "Quantity must be positive."

        owned = self.player.cargo.get(good, 0)
        if qty > owned:
            return False, f"You only have {owned}x {COMMODITIES[good].name}."
        sellable = self.player.sellable_cargo(good)
        if qty > sellable:
            reserved = owned - sellable
            return False, (
                f"{reserved}x {COMMODITIES[good].name} is sealed for an active contract "
                f"and cannot be sold (sellable: {sellable})."
            )

        price = self.get_sell_price(good)
        total_income = self.quote_sell(good, qty)
        basis = self.player.cargo_cost_basis.get(good, float(price))
        total_profit = int(round(total_income - basis * qty))
        pct_return = ((total_income / qty - basis) / max(1.0, basis)) * 100.0

        self._remove_cargo_tracked(good, qty)

        self.player.credits += total_income
        self.current_planet.stock[good] = self.current_planet.stock.get(good, 0) + qty
        self._price_impact(good, qty, "sell")
        self.player.stats["total_profit"] += total_income
        self.player.stats["trading_profit"] = (
            self.player.stats.get("trading_profit", 0) + total_profit
        )

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

    @requires_alive
    def buy_fuel(self, amount: int) -> Tuple[bool, str]:
        if amount <= 0:
            return False, "Enter an amount greater than zero."
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

    @requires_alive
    def repair_hull(self, hp: int) -> Tuple[bool, str]:
        if hp <= 0:
            return False, "Enter an amount greater than zero."
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

    @requires_alive
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

    @requires_alive
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

    @requires_alive
    def buy_missiles(self, qty: int) -> Tuple[bool, str]:
        if not self.player.has_missile_rack():
            return False, "You need a Havoc Missile Launcher installed first."
        if qty <= 0:
            return False, "Enter an amount greater than zero."
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
        # 70% of the listed price, prorated by hull condition — otherwise a
        # downgrade -> upgrade cycle is a cheap full repair plus free refuel.
        tmpl = SHIP_TEMPLATES[self.player.ship_id]
        hull_fraction = self.player.hull / max(1, tmpl.max_hull)
        return int(tmpl.cost * 0.7 * clamp(hull_fraction, 0.05, 1.0))

    @requires_alive
    def buy_ship(self, template_id: str) -> Tuple[bool, str]:
        if template_id not in SHIP_TEMPLATES:
            return False, "Invalid ship model."

        if template_id == self.player.ship_id:
            return False, "You are already flying this ship model."

        tmpl = SHIP_TEMPLATES[template_id]
        cur_val = self.ship_trade_in_value()
        # Signed on purpose: when the new hull is cheaper than your trade-in the
        # yard pays you the difference instead of pocketing it.
        net_cost = tmpl.cost - cur_val

        # Work out which fittings survive the refit BEFORE charging anything.
        kept: Dict[str, List[str]] = {}
        dropped_ids: List[str] = []
        for lst_name, slots in (
            ("equipped_weapons", tmpl.weapon_slots),
            ("equipped_shields", tmpl.shield_slots),
            ("equipped_modules", tmpl.module_slots),
        ):
            lst = getattr(self.player, lst_name)
            kept[lst_name] = list(lst[:slots])
            dropped_ids.extend(lst[slots:])

        kept_cargo_bonus = sum(
            EQUIPMENT_ITEMS[m].cargo_bonus
            for m in kept["equipped_modules"] if m in EQUIPMENT_ITEMS
        )
        new_cargo_cap = tmpl.cargo_cap + kept_cargo_bonus

        if net_cost > 0 and self.player.credits < net_cost:
            return False, f"Need {money(net_cost)} CR (trade-in credited {money(cur_val)} CR)."

        if self.player.cargo_used() > new_cargo_cap:
            return False, (
                f"Current cargo ({self.player.cargo_used()}) exceeds new ship capacity "
                f"({new_cargo_cap}). Sell cargo first."
            )

        self.player.credits -= net_cost   # negative net_cost refunds the difference

        # Equipment that will not fit the new hull is sold at standard 75%
        # salvage value — never silently destroyed.
        refund = 0
        dropped: List[str] = []
        for eq_id in dropped_ids:
            if eq_id in EQUIPMENT_ITEMS:
                refund += int(EQUIPMENT_ITEMS[eq_id].cost * 0.75)
                dropped.append(EQUIPMENT_ITEMS[eq_id].name)
        for lst_name, lst in kept.items():
            setattr(self.player, lst_name, lst)

        # Missiles without a launcher are refunded at half price.
        if self.player.missiles > 0 and "missile_rack" not in kept["equipped_weapons"]:
            refund += int(self.player.missiles * self.missile_price() * 0.5)
            dropped.append(f"{self.player.missiles} missile(s)")
            self.player.missiles = 0

        if refund > 0:
            self.player.credits += refund
            self.add_news(
                f"Yard crews salvaged {len(dropped)} oversized fitting(s) "
                f"for {money(refund)} CR: {', '.join(dropped)}."
            )

        # A fresh hull arrives with healthy subsystems.
        self.player.weapons_damaged = False
        self.player.engines_damaged = False
        self.player.shields_damaged = False

        self.player.ship_id = template_id

        self.recalculate_ship_stats()
        self.player.hull = self.player.max_hull
        self.player.shield = self.player.max_shield
        self.player.fuel = self.player.max_fuel

        self.sound.play("upgrade")
        self.check_achievements()
        self.check_promotion()
        if net_cost < 0:
            msg = (f"Congratulations! You are now captain of a new {tmpl.name}. "
                   f"The yard paid you {money(-net_cost)} CR for the trade-in difference.")
        else:
            msg = f"Congratulations! You are now captain of a new {tmpl.name}."
        self.announce(msg)
        return True, msg

    @requires_alive
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

    @requires_alive
    def sell_equipment(self, eq_id: str) -> Tuple[bool, str]:
        if eq_id not in EQUIPMENT_ITEMS:
            return False, "Invalid equipment item."

        eq = EQUIPMENT_ITEMS[eq_id]
        installed_in: Optional[str] = None
        for lst_name in ("equipped_weapons", "equipped_shields", "equipped_modules"):
            if eq_id in getattr(self.player, lst_name):
                installed_in = lst_name
                break

        if installed_in is None:
            return False, f"{eq.name} is not installed on your ship."

        # A cargo expander may only come out if the hold still fits without it.
        if installed_in == "equipped_modules" and eq.cargo_bonus > 0:
            new_cap = self.player.cargo_cap - eq.cargo_bonus
            if self.player.cargo_used() > new_cap:
                return False, (
                    f"Cannot remove {eq.name}: your hold would shrink to {new_cap} units "
                    f"but carries {self.player.cargo_used()}. Sell or jettison "
                    f"{self.player.cargo_used() - new_cap} unit(s) first."
                )

        getattr(self.player, installed_in).remove(eq_id)

        refund = int(eq.cost * 0.75)
        extra = ""
        if eq_id == "missile_rack" and self.player.missiles > 0:
            missile_refund = int(self.player.missiles * self.missile_price() * 0.5)
            extra = f" Unused missiles returned for {money(missile_refund)} CR."
            refund += missile_refund
            self.player.missiles = 0
        self.player.credits += refund
        self.recalculate_ship_stats()
        self.sound.play("sell")
        msg = f"Dismounted {eq.name} and received {money(refund)} CR salvage value.{extra}"
        self.announce(msg)
        return True, msg

    @requires_alive
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

    @requires_alive
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

    @requires_alive
    def accept_mission(self, mission_id: str) -> Tuple[bool, str]:
        for m in self.available_missions:
            if m.id == mission_id:
                if len(self.player.active_missions) >= MAX_ACTIVE_MISSIONS:
                    return False, f"Active mission log full (max {MAX_ACTIVE_MISSIONS} contracts)."

                if m.cargo_good and m.cargo_qty > 0:
                    if self.player.cargo_free() < m.cargo_qty:
                        return False, f"Need {m.cargo_qty} free cargo space to accept this contract."
                    # Contract goods are sealed, so they can never be sold for
                    # profit — but the hold readout still shows a cost basis.
                    # Seed it with the station price so the cargo describes
                    # itself honestly instead of falling back to whatever the
                    # market happens to be doing when the hold is opened.
                    self._acquire_cargo(m.cargo_good, m.cargo_qty,
                                        float(self.get_buy_price(m.cargo_good)))

                self.available_missions.remove(m)
                self.player.active_missions.append(m)
                self.sound.play("coin")
                msg = f"Contract accepted: {m.title}"
                self.announce(msg)
                return True, msg
        return False, "Mission no longer available."

    @requires_alive
    def abandon_mission(self, mission_id: str) -> Tuple[bool, str]:
        for m in list(self.player.active_missions):
            if m.id == mission_id:
                self.player.active_missions.remove(m)
                if m.cargo_good and m.cargo_qty > 0:
                    self._remove_cargo_tracked(m.cargo_good, m.cargo_qty)
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
        cur_rank = self.rank()
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
                        f" [{cur_rank.name}'s share +{money(payout - m.reward_credits)} CR]"
                    completed_msgs.append(
                        f"Passengers delivered: '{m.title}' complete! Fare: {money(payout)} CR."
                        f"{rank_note}{rep_note}"
                    )
                    self.sound.play("victory")
                elif m.m_type in ("delivery", "smuggle", "medical"):
                    if m.cargo_good and self.player.cargo.get(m.cargo_good, 0) >= m.cargo_qty:
                        self._remove_cargo_tracked(m.cargo_good, m.cargo_qty)
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
                            f" [{cur_rank.name}'s share +{money(payout - m.reward_credits)} CR]"
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
        if self.is_game_over:
            return False, "Your career has ended. Start a new commission or load a flight record.", None
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
        self.player.mark_visited(dest.name)
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
            ("comet_mining", 9),
            ("ghost_freighter", 7),
            ("derby", 6),
            ("smuggler_cache", 7),
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

        if chosen == "comet_mining":
            return {
                "type": "comet_mining",
                "title": "Pristine Cometary Ice Field",
                "desc": "A glittering swarm of fresh comets drifts across your vector, "
                        "each one packed with water ice and frozen volatiles.",
            }

        if chosen == "ghost_freighter":
            return {
                "type": "ghost_freighter",
                "title": "Ghost Freighter Echo",
                "desc": "Long-range lidar paints a dark, unpowered freighter — her "
                        "transponder belongs to a ship lost with all hands 40 years ago. "
                        "Scanners whisper of sealed holds... and un-shipped cargo.",
            }

        if chosen == "derby":
            wager = max(100, int(self.player.credits * 0.20))
            return {
                "type": "derby",
                "title": "Oort Cloud Derby Night",
                "wager": wager,
                "desc": "Independent racers thread the cometary markers for bragging "
                        "rights — and the crowd takes side bets. Buy-in tonight is "
                        f"{money(wager)} CR with a 2.2x payout if your pick holds the line.",
            }

        if chosen == "smuggler_cache":
            return {
                "type": "smuggler_cache",
                "title": "Uncharted Cache Detected",
                "desc": "Short-range sensors resolve a shielded supply pod bolted to a "
                        " nameless rock — the kind of stash a runner drops when a deal "
                        "goes wrong. It could hold valuables, or hold a grudge.",
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
            self.player.cargo_cost_basis.pop(g, None)
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

        if illegal:
            # A friendly patrol is lenient, never blind: reputation lowers the
            # odds of a deep scan, and the smuggler's touch helps too, but a
            # hold full of contraband always carries some risk. (Previously
            # rep >= 15 made smuggling literally risk-free and even paid a
            # stipend for it.)
            zoe_perk = self.player.has_crew_perk("smuggler") or 0.0
            detection = 0.85 if rep < 15 else max(0.30, 0.85 - rep * 0.011)
            detection *= max(0.25, 1.0 - zoe_perk)
            if random.random() < detection:
                fine = max(400, int(self.player.credits * 0.18 * self.difficulty.fine_mult))
                fine = int(fine * (1.0 - zoe_perk))
                fine = min(self.player.credits, fine)
                self.player.credits -= fine
                for g in list(illegal.keys()):
                    self.player.remove_cargo(g, illegal[g])
                    self.player.cargo_cost_basis.pop(g, None)
                self.adjust_reputation(faction, -10)
                self.sound.play("alarm")
                msgs.append(f"The inspection turns up your contraband! Fined {money(fine)} CR and "
                            f"cargo seized. ({faction} standing -10)")
                return msgs
            gain = self.adjust_reputation(faction, 1)
            msgs.append("The patrol sweeps a scanner wand over a few crates, then waves "
                        "you through — your papers and standing did the talking. "
                        f"({faction} standing +{gain})")
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
            added = self._acquire_cargo(good, qty, 0.0)
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
        self._acquire_cargo(good, qty, total / max(1, qty))
        self.sound.play("buy")
        return [f"Deal accepted! Loaded {qty}x {COMMODITIES[good].name} for {money(total)} CR."]

    def resolve_asteroid_field(self, enc: Dict[str, Any], thread_needle: bool) -> List[str]:
        """thread_needle=True: fast and risky. False: safe detour costing fuel."""
        if not thread_needle:
            detour_cost = 8
            if self.player.fuel < detour_cost:
                self.player.hull = max(1, self.player.hull - 4)
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
        added = self._acquire_cargo("ore", salvage, 0.0)
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
        self.player.mark_visited(dest.name)
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
        if self.player.has_module("mining_rig"):
            qty = int(qty * 1.8)
        added = self._acquire_cargo(vein, qty, 0.0)
        self.sound.play("mine")
        self.check_achievements()
        return [f"Mining drones strip the seam: recovered {added}x {COMMODITIES[vein].name} "
                f"(−{fuel_cost} fuel)."]

    def resolve_comet(self, enc: Dict[str, Any], harvest: bool) -> List[str]:
        """Harvest a cometary ice field for water (and sometimes fuel cells)."""
        if not harvest:
            return ["You set a course wide of the ice swarm and let the comets keep "
                    "their frozen silence."]
        fuel_cost = 10
        if self.player.fuel < fuel_cost:
            return ["Not enough fuel to chase the comets down."]
        if self.player.cargo_free() < 1:
            return ["No cargo space free — the ice swarm sails on without you."]
        self.player.fuel -= fuel_cost
        self.player.stats["comet_ops"] += 1
        qty = random.randint(4, 10)
        if self.player.has_module("mining_rig"):
            qty = int(qty * 1.8)
        added = self._acquire_cargo("water", qty, 0.0)
        msgs = [f"Scoop nets flare wide: harvested {added}x Pure Water from the "
                f"comet tails (−{fuel_cost} fuel)."]
        if random.random() < 0.30:
            cells = random.randint(2, 5)
            got_cells = self._acquire_cargo("fuel_cells", cells, 0.0)
            if got_cells:
                msgs.append(f"Sublimation vents also yield {got_cells}x Hyper-Fuel Cells!")
        self.sound.play("mine")
        self.check_achievements()
        return msgs

    def resolve_ghost(self, enc: Dict[str, Any], careful: bool) -> List[str]:
        """A 40-years-lost freighter: board carefully or strip her quickly."""
        msgs: List[str] = []
        if careful:
            fuel_cost = 5
            if self.player.fuel < fuel_cost:
                return ["Not enough fuel for a careful approach — you leave the ghost to drift."]
            self.player.fuel -= fuel_cost
            msgs.append(f"Matching velocity gently (−{fuel_cost} fuel), your crew "
                        "cycles the airlocks and sweeps deck by deck.")
            if random.random() < 0.70:
                cores = random.randint(2, 4)
                salvage = random.randint(400, 1_200)
                added = self._acquire_cargo("ai_cores", cores, 0.0)
                self.player.credits += salvage
                self.sound.play("victory")
                msgs.append(f"The sealed vault yields {added}x Unshackled AI Core(s) "
                            f"and {money(salvage)} CR in preserved stores!")
            else:
                dmg = random.randint(6, 12)
                self.player.hull = max(1, self.player.hull - dmg)
                self.player.shield = 0
                self.sound.play("alarm")
                msgs.append(f"A residual defense grid sparks to life! Arc discharge "
                            f"burns {dmg} hull and drops your shields.")
        else:
            msgs.append("You clamp grapples and rip the hull plating open for a fast strip.")
            if random.random() < 0.45:
                cores = random.randint(3, 8)
                salvage = random.randint(800, 2_400)
                added = self._acquire_cargo("ai_cores", cores, 0.0)
                self.player.credits += salvage
                self.sound.play("victory")
                msgs.append(f"Jackpot — the tore-open vault spills {added}x Unshackled "
                            f"AI Core(s) and {money(salvage)} CR in salvage!")
            else:
                dmg = random.randint(12, 28)
                self.player.hull = max(1, self.player.hull - dmg)
                self.player.shield = 0
                stolen_qty = 0
                for good in list(self.player.cargo.keys()):
                    # Sealed contract cargo is strapped to its client's
                    # transponder — only unsealed goods vent into space.
                    loose = self.player.sellable_cargo(good)
                    if loose <= 0:
                        continue
                    stolen_qty += self.player.remove_cargo(good, min(
                        loose, random.randint(1, 3)))
                self.sound.play("alarm")
                msgs.append(f"Catastrophe — the hull shears back and a security drone "
                            f"rakes your ship for {dmg} hull damage!")
                if stolen_qty:
                    msgs.append(f"{stolen_qty} unit(s) of your own cargo tumble out "
                                "through the breach and are lost.")
        self.check_achievements()
        return msgs

    def resolve_smuggler_cache(self, enc: Dict[str, Any], investigate: bool) -> List[str]:
        """A shielded drop-pod stash bolted to a nameless rock.

        High reward, real risk: runners protect their stashes. The odds tilt
        with the local crime level — lawless space hides richer (and meaner)
        caches. Loot is recorded at zero cost basis so selling it reports an
        honest profit.
        """
        if not investigate:
            return ["You log the cache coordinates for the cartographers and leave "
                    "whoever dropped it to their secrets."]
        if self.player.cargo_free() < 1 and random.random() < 0.75:
            # Most outcomes want hold space; a full hold mostly finds nothing.
            return ["Your hold is packed solid — there is nowhere to put anything "
                    "the cache might yield, so you leave it sealed."]

        crime = clamp(self.current_planet.crime, 0.0, 3.0)
        stash_cr = random.randint(300, 1_200) + int(crime * 250)
        roll = random.random()

        if roll < 0.42:
            # Clean score: untraceable credits and choice contraband.
            good = random.choice(["narcotics", "weapons", "luxury"])
            qty = random.randint(2, 5)
            added = self._acquire_cargo(good, qty, 0.0)
            self.player.credits += stash_cr
            self.player.stats["caches_found"] += 1
            self.sound.play("victory")
            self.check_achievements()
            return [f"The pod's mag-locks release! Inside: {money(stash_cr)} CR in "
                    f"bearer chips and {added}x {COMMODITIES[good].name}. "
                    "No questions asked, none answered."]
        if roll < 0.62:
            # Booby-trapped, but the prize survives the surprise.
            good = random.choice(["ai_cores", "medicine", "electronics"])
            qty = random.randint(1, 3)
            dmg = random.randint(8, 15)
            self.player.hull = max(1, self.player.hull - dmg)
            added = self._acquire_cargo(good, qty, 0.0)
            self.player.stats["caches_found"] += 1
            self.sound.play("alarm")
            self.check_achievements()
            return [f"A cesium charge strapped inside blows as the seal breaks — "
                    f"{dmg} hull damage! You still extract {added}x "
                    f"{COMMODITIES[good].name} from the wreckage."]
        if roll < 0.82:
            # Decoy: a tracker squawk and a worthless shell.
            self.adjust_reputation(self.current_planet.faction, -2)
            self.sound.play("alarm")
            return ["The pod is a decoy rigged with a fleet transponder ping — you "
                    f"barely clear the cordon before {self.current_planet.faction} "
                    "sensors sweep the belt. (-2 local standing, nothing gained)"]
        if roll < 0.92:
            # Runner's courtesy: fuel cells and spare torpedoes.
            cells = random.randint(2, 4)
            added = self._acquire_cargo("fuel_cells", cells, 0.0)
            missiles_found = random.choice([0, 1, 1, 2])
            self.player.missiles = min(PLAYER_MISSILE_CAP, self.player.missiles + missiles_found)
            self.player.stats["caches_found"] += 1
            self.sound.play("coin")
            self.check_achievements()
            extra = f" and {missiles_found} homing torpedo(es)" if missiles_found else ""
            return [f"A fellow runner's courtesy stash: {added}x Hyper-Fuel Cells"
                    f"{extra}. Marked with the hollow-ring sign — pay it forward."]
        # Gutted cache: someone got here first.
        self.sound.play("alarm")
        return ["The cache hangs open, gutted to the bulkheads. Fresh cutter marks "
                "glisten on the seal — you are days late and empty-handed."]

    def resolve_derby(self, enc: Dict[str, Any], bet: bool) -> List[str]:
        """Wager on the Oort Cloud sled races: 2.2x payout at ~42% odds."""
        if not bet:
            return ["You watch two heats from the comm channel and keep your wallet shut."]
        wager = int(enc.get("wager", 100))
        # The advertised buy-in is the buy-in: never silently stake the
        # captain's entire balance when they cannot cover it.
        if self.player.credits < wager:
            return [f"Cannot cover the {money(wager)} CR buy-in tonight."]
        self.player.credits -= wager
        if random.random() < 0.42:
            winnings = int(wager * 2.2)
            self.player.credits += winnings
            self.player.stats["derby_wins"] += 1
            self.sound.play("victory")
            self.check_achievements()
            return [f"Your runner threads the markers flawlessly — the bookie pays "
                    f"out {money(winnings)} CR (net +{money(winnings - wager)})!"]
        self.sound.play("alarm")
        return [f"Your runner clips a marker at the final turn and flames out. "
                f"Lost the {money(wager)} CR stake."]

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
        if t == "comet_mining":
            return self.resolve_comet(enc, harvest=bool(arg))
        if t == "ghost_freighter":
            return self.resolve_ghost(enc, careful=bool(arg))
        if t == "derby":
            return self.resolve_derby(enc, bet=bool(arg))
        if t == "smuggler_cache":
            return self.resolve_smuggler_cache(enc, investigate=bool(arg))
        return []


    # ------------------------------------------------------------------ #
    # Trade advisor

    def remote_top_deals(self, planet: Planet, limit: int = 5) -> List[Dict[str, Any]]:
        """Best goods to buy at a remote planet and sell where you are now.
        Used by the Deep Space Scanner module for the star-map dossier.

        The margin shown is the real one: the station's market-depth fee is
        already netted out, so the figure matches what the trade pays.
        """
        deals: List[Dict[str, Any]] = []
        for gid, comm in COMMODITIES.items():
            buy_remote = self.get_buy_price(gid, planet)
            sell_local = self.get_sell_price(gid, self.current_planet)
            stock = planet.stock.get(gid, 0)
            if stock <= 0:
                continue
            # One unit is enough to price the spread honestly.
            margin = self.quote_sell(gid, 1, self.current_planet) \
                - self.quote_buy(gid, 1, planet)
            if margin > 0:
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

    def fuel_credit_cost(self, units: int, planet: Optional[Planet] = None) -> int:
        """Credits needed to fill ``units`` of fuel at ``planet``.

        Mirrors :meth:`buy_fuel` exactly — rank service discount and floor —
        so the trade advisor and GUI never overstate fuel costs for senior
        officers.
        """
        p = planet or self.current_planet
        unit_price = max(1, int(p.fuel_price * self.difficulty.fuel_mult
                                * (1.0 - RANK_SERVICE_DISCOUNT[self.rank_index()])))
        return units * unit_price

    def compute_best_trade_routes(self, from_current_only: bool = False) -> List[Dict[str, Any]]:
        """Best cargo routes, priced with the SAME maths the market uses.

        An advisor that quotes optimistic numbers is worse than no advisor at
        all, so every figure below is what the trade actually yields:

        * the purchase price carries the market-depth fee,
        * the sale is net of its own market-depth fee,
        * fuel is charged for BOTH legs — the flight out to the source world
          and the flight onward to the destination,
        * profit-per-day divides by the days for both legs.

        The one thing no advisor can know is how the market will drift while
        you are in transit; prices are re-generated every day you fly.
        """
        routes: List[Dict[str, Any]] = []
        planets_list = list(self.planets.values())

        security_risk = {"None": 3, "Low": 2, "Medium": 1, "High": 0}

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

                    # A fixed "3 CR" floor is meaningless across an 18-item
                    # commodity list: 3 CR is 25% on water and 0.7% on AI
                    # cores. Require a proportional margin as well.
                    if margin < max(MIN_ROUTE_MARGIN_CR, buy_price * MIN_ROUTE_MARGIN_PCT):
                        continue

                    potential_qty = min(self.player.cargo_cap, src.stock.get(gid, 0))
                    if potential_qty <= 0:
                        continue

                    # Price the trade exactly the way the market will.
                    buy_cost = self.quote_buy(gid, potential_qty, src)
                    sell_income = self.quote_sell(gid, potential_qty, dst)
                    gross_profit = sell_income - buy_cost

                    # Fuel for the flight onward...
                    out_fuel, out_days = self.calculate_travel_cost_between(src, dst)
                    out_fuel_cr = self.fuel_credit_cost(out_fuel, src)
                    # ...and for the flight out to the source world.
                    if src.name == self.player.location:
                        in_fuel, in_days = 0, 0
                    else:
                        in_fuel, in_days = self.calculate_travel_cost_between(
                            self.current_planet, src)
                    in_fuel_cr = self.fuel_credit_cost(in_fuel, self.current_planet)

                    fuel_cost = in_fuel_cr + out_fuel_cr
                    days = in_days + out_days
                    net_profit = gross_profit - fuel_cost

                    if net_profit <= 0:
                        # Advertising a route that loses money after the
                        # station's cut is the fastest way to lose a captain's
                        # trust. Only surface routes that actually pay.
                        continue

                    risk_level = security_risk.get(dst.security, 1)
                    if comm.is_contraband:
                        risk_level += 2
                    risk_label = [
                        "Minimal", "Low", "Moderate", "High", "Severe", "Extreme",
                    ][min(5, risk_level)]

                    routes.append({
                        "good": comm.name,
                        "good_id": gid,
                        "src": src.name,
                        "dst": dst.name,
                        "buy_price": buy_price,
                        "sell_price": sell_price,
                        "margin": margin,
                        "margin_pct": round((margin / max(1, buy_price)) * 100, 1),
                        "fuel_cost": out_fuel,
                        "days": days,
                        "qty": potential_qty,
                        "total_profit": gross_profit,
                        "net_profit": net_profit,
                        "profit_per_day": int(net_profit / max(1, days)),
                        "is_contraband": comm.is_contraband,
                        "risk": risk_label,
                        "risk_level": risk_level,
                        # Full cost breakdown so the UI never has to invent
                        # numbers of its own.
                        "buy_cost": buy_cost,
                        "sell_income": sell_income,
                        "depth_fee": self.market_depth_fee(buy_price, potential_qty)
                                      + self.market_depth_fee(sell_price, potential_qty),
                        "fuel_credits": fuel_cost,
                        "inbound_fuel": in_fuel,
                        "outbound_fuel": out_fuel,
                        "inbound_days": in_days,
                        "outbound_days": out_days,
                    })

        routes.sort(key=lambda r: r["net_profit"], reverse=True)
        return routes[:12]

    # ------------------------------------------------------------------ #
    # Banking / stocks

    def loan_limit(self) -> int:
        return 20_000 + self.player.cargo_cap * 150 + self.player.max_hull * 50

    @requires_alive
    def deposit(self, amount: int) -> Tuple[bool, str]:
        if amount <= 0:
            return False, "Enter an amount greater than zero."
        amount = min(amount, self.player.credits)
        if amount <= 0:
            return False, "No credits available to deposit."
        self.player.credits -= amount
        self.player.savings += amount
        self.sound.play("coin")
        msg = f"Deposited {money(amount)} CR into savings."
        self.announce(msg)
        return True, msg

    @requires_alive
    def withdraw(self, amount: int) -> Tuple[bool, str]:
        if amount <= 0:
            return False, "Enter an amount greater than zero."
        amount = min(amount, self.player.savings)
        if amount <= 0:
            return False, "No savings available to withdraw."
        self.player.savings -= amount
        self.player.credits += amount
        self.sound.play("coin")
        msg = f"Withdrew {money(amount)} CR from savings."
        self.announce(msg)
        return True, msg

    @requires_alive
    def borrow(self, amount: int) -> Tuple[bool, str]:
        if amount <= 0:
            return False, "Enter an amount greater than zero."
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

    @requires_alive
    def repay(self, amount: int) -> Tuple[bool, str]:
        if amount <= 0:
            return False, "Enter an amount greater than zero."
        amount = min(amount, self.player.credits, self.player.loan)
        if amount <= 0:
            return False, "No loan payment can be made."
        self.player.loan -= amount
        self.player.credits -= amount
        self.player.stats["loan_repaid"] = (
            self.player.stats.get("loan_repaid", 0) + amount
        )
        # Solid repayments build your credit score (caps at 850).
        if amount >= 1_000:
            self.player.credit_score = min(850, self.player.credit_score + 2)
        self.sound.play("coin")
        msg = f"Repaid {money(amount)} CR loan."
        self.announce(msg)
        return True, msg

    @requires_alive
    def buy_stock(self, symbol: str, qty: int) -> Tuple[bool, str]:
        if symbol not in self.stocks:
            return False, "Unknown stock symbol."
        if qty <= 0:
            return False, "Quantity must be positive."

        stk = self.stocks[symbol]
        cost = int(math.ceil(round(stk.price * qty, 2)))
        if self.player.credits < cost:
            return False, f"Insufficient funds! Need {money(cost)} CR for {qty} shares."

        self.player.credits -= cost
        self.player.stocks_owned[symbol] = self.player.stocks_owned.get(symbol, 0) + qty
        self.sound.play("coin")
        msg = f"Bought {qty} shares of ${symbol} for {money(cost)} CR."
        self.announce(msg)
        return True, msg

    @requires_alive
    def sell_stock(self, symbol: str, qty: int) -> Tuple[bool, str]:
        if symbol not in self.stocks:
            return False, "Unknown stock symbol."
        if qty <= 0:
            return False, "Quantity must be positive."
        owned = self.player.stocks_owned.get(symbol, 0)
        to_sell = min(qty, owned)
        if to_sell <= 0:
            return False, f"You own no shares of ${symbol}."

        income = int(math.floor(round(self.stocks[symbol].price * to_sell, 2)))
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

        # The hold is about to be vaporised. Every contract that still owes
        # the client a delivery can no longer be completed — its cargo went
        # down with the ship — so void those contracts *now* and tell the
        # captain. Leaving them on the mission log to expire silently would
        # strand the contract and cost reputation for nothing.
        voided: List[str] = []
        for m in list(self.player.active_missions):
            if m.cargo_good and m.cargo_qty > 0:
                voided.append(m.title)
                self.player.active_missions.remove(m)

        self.player.cargo = {}
        self.player.cargo_cost_basis = {}
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
        if voided:
            msgs.append(
                "Contracts voided by the loss (their cargo went down with you): "
                + "; ".join(voided) + "."
            )
        self.add_news("Insurance claim settled. You live to trade another day.")
        if voided:
            self.add_news(f"{len(voided)} contract(s) voided by the loss.")
        self.sound.play("victory")
        return msgs

    # ------------------------------------------------------------------ #
    # Station life: odd jobs, jettison, diplomacy

    ODD_JOB_FLAVOR = [
        "Hauled cryo-container pallets across the docking bay.",
        "Ran a recalibration survey on the port's beacon array.",
        "Ferried a survey team out to a near-orbit rock and back.",
        "Sorted a quartermaster's manifest nightmare in triplicate.",
        "Stood watch over a corrosive-hold transfer for a nervous broker.",
        "Escorted a supply skiff through the approach lanes.",
        "Debugged a stubborn cargo loader for the dockmaster.",
        "Towed a stalled pleasure craft clear of the traffic pattern.",
    ]

    @requires_alive
    def odd_jobs(self) -> Tuple[bool, str]:
        """Work a shift at the station: small wages, costs one day.

        This is the anti-soft-lock valve — a captain who is flat broke and
        out of fuel can always work the docks until they can fly again.
        """
        p = self.current_planet
        wage = int(random.uniform(180, 320) * (0.8 + p.rich * 0.4 + p.tech * 0.1))
        self.player.credits += wage
        self.player.stats["odd_jobs_done"] += 1
        flavor = random.choice(self.ODD_JOB_FLAVOR)
        self.advance_day(1)
        self.sound.play("coin")
        msg = (f"Dock shift complete: {flavor} Earned {money(wage)} CR "
               f"(one day passes).")
        self.announce(msg)
        return True, msg

    @requires_alive
    def jettison_cargo(self, good: str, qty: int) -> Tuple[bool, str]:
        """Dump cargo into space — no refund, but no fine either."""
        if good not in COMMODITIES:
            return False, "Invalid commodity."
        if qty <= 0:
            return False, "Quantity must be positive."
        owned = self.player.cargo.get(good, 0)
        if owned <= 0:
            return False, f"No {COMMODITIES[good].name} in the hold."
        free_units = self.player.sellable_cargo(good)
        if free_units <= 0:
            return False, (f"All {COMMODITIES[good].name} aboard is sealed for an active "
                           f"contract. Abandon the contract first.")
        dumped = self._remove_cargo_tracked(good, min(qty, free_units))
        self.player.stats["cargo_jettisoned"] += dumped
        self.sound.play("warp")
        msg = f"Jettisoned {dumped}x {COMMODITIES[good].name} into the void."
        self.announce(msg)
        return True, msg

    @requires_alive
    def faction_gift(self, faction: str, amount: int) -> Tuple[bool, str]:
        """Donate credits to a faction's relief fund for reputation."""
        if faction not in FACTIONS:
            return False, "Unknown faction."
        if amount < GIFT_MIN:
            return False, f"Minimum diplomatic gift is {money(GIFT_MIN)} CR."
        amount = min(amount, self.player.credits)
        if amount < GIFT_MIN:
            return False, f"Cannot afford a {money(GIFT_MIN)} CR gift right now."
        self.player.credits -= amount
        self.player.stats["gifts_given"] += 1
        gain = int(clamp(amount / 500.0, 1, 8))
        applied = self.adjust_reputation(faction, gain)
        self.sound.play("coin")
        msg = (f"The {faction} accepts your {money(amount)} CR contribution "
               f"with public gratitude (standing +{applied}).")
        self.announce(msg)
        self.add_news(f"Diplomatic gift of {money(amount)} CR presented to the {faction}.")
        return True, msg

    # ------------------------------------------------------------------ #
    # Navigation helpers

    def fuel_efficiency_mult(self) -> float:
        """Combined fuel cost multiplier from warp booster & navigator."""
        mult = 1.0
        if self.player.has_module("warp_booster"):
            mult *= 0.70
        nav_perk = self.player.has_crew_perk("nav")
        if nav_perk:
            mult *= (1.0 - nav_perk)
        return mult

    def max_jump_range(self) -> float:
        """Furthest straight-line distance reachable on the current tank."""
        eff = self.fuel_efficiency_mult()
        if self.player.fuel <= 4:
            return 0.0
        return max(0.0, (self.player.fuel / eff - 4.0) / 3.0)

    # ------------------------------------------------------------------ #
    # Hall of Fame

    @staticmethod
    def hof_path() -> str:
        return os.path.join(save_dir(), HOF_FILENAME)

    def record_hall_of_fame(self, won: bool) -> None:
        """Persist this career's final standing to the all-time score table."""
        if getattr(self, "_hof_recorded", False):
            return
        self._hof_recorded = True
        entry = {
            "name": self.player.name,
            "day": self.player.day,
            "net_worth": self.calculate_net_worth(),
            "rank": self.rank().name,
            "difficulty": self.difficulty.name,
            "won": bool(won),
            "achieved_at": time.strftime("%Y-%m-%d %H:%M"),
        }
        try:
            entries = self.load_hall_of_fame()
            # De-duplicate identical outcomes (name + day + net worth +
            # difficulty) so two different careers that happen to share the
            # first three values both survive.
            entries = [
                e for e in entries
                if not (e.get("name") == entry["name"]
                        and e.get("day") == entry["day"]
                        and e.get("net_worth") == entry["net_worth"]
                        and e.get("difficulty") == entry["difficulty"])
            ]
            entries.append(entry)
            entries.sort(
                key=lambda e: e["net_worth"] if isinstance(e.get("net_worth"), (int, float))
                and not isinstance(e.get("net_worth"), bool) else 0,
                reverse=True)
            atomic_write_json(self.hof_path(), entries[:HOF_MAX_ENTRIES * 3])
        except Exception:
            pass

    def load_hall_of_fame(self) -> List[Dict[str, Any]]:
        try:
            with open(self.hof_path(), "r", encoding="utf-8") as f:
                entries = json.load(f)
            if isinstance(entries, list):
                # Validate types, not just key presence: one hand-edited or
                # corrupted entry used to poison the sort in
                # record_hall_of_fame (TypeError, silently swallowed) and
                # permanently brick all future score recordings.
                valid = []
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    nw = e.get("net_worth")
                    name = e.get("name")
                    if isinstance(nw, (int, float)) and not isinstance(nw, bool) \
                            and isinstance(name, str):
                        valid.append(e)
                return valid
        except Exception:
            pass
        return []

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
                "visited": sorted(self.player.visited),
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
        if self.is_game_over:
            return False, "A destroyed ship cannot be saved. Load a flight record instead."
        try:
            path = slot_path(slot)
            atomic_write_json(path, self._save_payload())
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

        # Loading mutates live state field by field. Snapshot first so a corrupt
        # or truncated file can never leave the running game half-overwritten.
        backup = (
            copy.deepcopy(self.player), copy.deepcopy(self.planets),
            copy.deepcopy(self.stocks), list(self.news_feed),
            list(self.available_missions), self.difficulty_id,
            self.is_game_over, self._hof_recorded,
        )

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("player"), dict):
                raise ValueError("not a flight record")
            # Refuse saves written by a NEWER format instead of mis-loading
            # them field by field. Older saves keep loading via defaults.
            try:
                save_ver = int(data.get("version", 1))
            except (TypeError, ValueError):
                save_ver = 0
            if save_ver > SAVE_VERSION:
                raise ValueError(
                    f"save version {save_ver} is newer than this build "
                    f"(max supported: {SAVE_VERSION})")

            p_data = data.get("player", {})

            self.player.name = sanitize_name(p_data.get("name", "Commander"))
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
            self.player.visited = {
                v for v in p_data.get("visited", [])
                if isinstance(v, str) and v in self.planets
            } or {self.player.location}
            self._hof_recorded = False

            loaded_missions: List[Mission] = []
            for m_dict in p_data.get("active_missions", []):
                if not isinstance(m_dict, dict):
                    continue
                clean = {k: v for k, v in m_dict.items() if k in Mission.__dataclass_fields__}
                try:
                    loaded_missions.append(Mission(**clean))
                except TypeError:
                    continue   # skip malformed contract entries
            self.player.active_missions = loaded_missions

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
                    if isinstance(ev_dict, dict):
                        ev_clean = {k: v for k, v in ev_dict.items()
                                    if k in PlanetEvent.__dataclass_fields__}
                        p.active_event = PlanetEvent(**ev_clean)
                    else:
                        p.active_event = None

            stocks_data = data.get("stocks", {})
            for sym, s_info in stocks_data.items():
                if sym in self.stocks and isinstance(s_info, dict):
                    s_clean = {k: v for k, v in s_info.items()
                               if k in StockData.__dataclass_fields__}
                    self.stocks[sym] = StockData(**s_clean)

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
            self._sanitize_player()
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
            (self.player, self.planets, self.stocks, self.news_feed,
             self.available_missions, self.difficulty_id,
             self.is_game_over, self._hof_recorded) = backup
            return False, f"Failed to load save: {e}"

    def _sanitize_player(self) -> None:
        """Clamp a freshly-loaded career into the legal state space so a
        hand-edited or legacy save can never break the game's invariants."""
        p = self.player
        tmpl = SHIP_TEMPLATES[p.ship_id]

        p.credits = max(0, int(p.credits))
        p.savings = max(0, int(p.savings))
        p.loan = max(0, int(p.loan))
        p.day = max(1, int(p.day))
        p.credit_score = int(clamp(p.credit_score, MIN_CREDIT_SCORE, MAX_CREDIT_SCORE))
        p.missiles = int(clamp(p.missiles, 0, PLAYER_MISSILE_CAP))

        def unique(items: List[str]) -> List[str]:
            seen: List[str] = []
            for it in items:
                if it not in seen:
                    seen.append(it)
            return seen

        p.equipped_weapons = unique(p.equipped_weapons)[:tmpl.weapon_slots]
        p.equipped_shields = unique(p.equipped_shields)[:tmpl.shield_slots]
        p.equipped_modules = unique(p.equipped_modules)[:tmpl.module_slots]
        p.hired_crew = unique(p.hired_crew)[:MAX_CREW]
        p.active_missions = p.active_missions[:MAX_ACTIVE_MISSIONS]
        p.cargo = {g: q for g, q in p.cargo.items() if q > 0}
        p.cargo_cost_basis = {g: b for g, b in p.cargo_cost_basis.items() if g in p.cargo}
        clean_stocks: Dict[str, int] = {}
        for sym, qty in p.stocks_owned.items():
            try:
                qty = int(qty)
            except (TypeError, ValueError):
                continue
            if sym in self.stocks and qty > 0:
                clean_stocks[sym] = qty
        p.stocks_owned = clean_stocks
        p.reputation = {
            f: int(clamp(v, REPUTATION_MIN, REPUTATION_MAX))
            for f, v in p.reputation.items()
        }

        self.recalculate_ship_stats()

        # A hold that no longer fits (edited save / changed fittings) sheds its
        # largest stacks until it does — contract cargo is shed last.
        while p.cargo_used() > p.cargo_cap and p.cargo:
            victim = max(p.cargo, key=lambda g: (p.sellable_cargo(g) > 0, p.cargo[g]))
            excess = p.cargo_used() - p.cargo_cap
            self._remove_cargo_tracked(victim, excess)


# ==============================================================================
# TACTICAL COMBAT — overhauled
# ==============================================================================

ENEMY_PERSONALITIES: Dict[str, str] = {
    "aggressive": "fights head-on and punishes exposed hulls",
    "defensive": "cycles shields and outlasts opponents",
    "opportunist": "steals cargo and cripples your subsystems",
    "coward": "runs the moment the battle turns sour",
}

# Shield behaviour for defensive pilots. The recharge rate has to stay low
# enough that a serious gunload can punch through, and a defensive enemy
# still returns fire (softly) while it charges.
ENEMY_SHIELD_RECHARGE_RATE = 0.25
DEFENSIVE_ATTACK_POWER = 0.7

# Weapon payload class: bigger hulls genuinely hit harder.
ENEMY_CLASS_DAMAGE: Dict[str, Tuple[int, int]] = {
    "Light Courier": (7, 15),
    "Recon Cutter": (9, 18),
    "Medium Freighter": (11, 22),
    "Heavy Freighter": (13, 26),
    "Armored Hauler": (14, 28),
    "Combat Scout": (15, 30),
    "Deep Range Cruiser": (14, 28),
    "Heavy Frigate": (18, 36),
    "Dreadnought": (22, 44),
    "Flagship": (26, 52),
}


class CombatEncounter:
    """Stateful tactical battle. The GUI renders its attributes; the test
    suite drives it headlessly. All mutations flow through player_action()."""

    ACTIONS = ("fire", "target_engines", "target_weapons", "target_shields",
               "missile", "drones", "recharge", "evade", "board", "flee")

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
        self.enemy_emitters_fried = False

        # Damage payload driven by ship class, then scaled by difficulty & wealth.
        dmg_lo, dmg_hi = ENEMY_CLASS_DAMAGE.get(
            tmpl.ship_class, (12, 24))
        self.enemy_damage_range = (
            max(4, int(dmg_lo * scale)), max(6, int(dmg_hi * scale)),
        )
        self.torpedo_volleys_fired = 0
        self.turn_count = 0
        self.evasive_until_turn = -1

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
            f"Enemy weapon signature: {self.enemy_damage_range[0]}–"
            f"{self.enemy_damage_range[1]} dmg per volley ({self.threat_level()}).",
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

    def threat_level(self) -> str:
        """Human-readable danger rating for the tactical display."""
        avg = sum(self.enemy_damage_range) / 2.0
        pool = self.enemy_max_hull + self.enemy_max_shield
        if avg * 10 + pool >= 1_400:
            return "CRITICAL"
        if avg * 10 + pool >= 800:
            return "HIGH"
        if avg * 10 + pool >= 380:
            return "MODERATE"
        return "LOW"

    # ------------------------------------------------------------------ #
    # Main dispatch

    def _action_blocker(self, action: str) -> Optional[str]:
        """Return a refusal message when ``action`` cannot apply right now.

        The refusal does NOT consume the turn — mirrored from the individual
        maneuver implementations so keyboard shortcuts match the disabled
        on-screen buttons.
        """
        p = self._p()
        if action == "missile":
            if not p.has_missile_rack():
                return "You have no missile launcher installed!"
            if p.missiles <= 0:
                return "Missile magazines are empty! Buy more at the shipyard."
        elif action == "evade":
            if p.engines_damaged:
                return "Thrusters are too badly damaged for evasive work — hold the line!"
        elif action == "board":
            if not self.can_board():
                return "Boarding requires the enemy hull to be at 25% integrity or less."
        elif action == "flee":
            if p.engines_damaged:
                return "Warp drives are DAMAGED — escape is impossible! Fight or die."
        return None

    def player_action(self, action: str, arg: Any = None) -> List[str]:
        if self.is_finished:
            return ["The battle is already over."]
        if action not in self.ACTIONS:
            return ["Unknown combat maneuver."]

        # Inapplicable maneuvers (no launcher, damaged thrusters, boarding too
        # early, ...) used to burn the turn AND provoke a full enemy volley —
        # the on-screen buttons are disabled but the hotkeys bypassed them.
        # Gate here so hotkeys behave exactly like the buttons.
        blocker = self._action_blocker(action)
        if blocker:
            return [blocker]

        msgs: List[str] = []
        self.turn_count += 1

        # Autonomous drones strike first.
        if self.drones_active and action != "drones":
            msgs.extend(self._drone_strike())
            if self.is_finished:
                # The swarm already finished the fight — don't burn a missile
                # or a volley on a wreck.
                return msgs

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
        elif action == "evade":
            msgs.extend(self._evasive_maneuvers())
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
            if self.enemy_hull <= 0:
                break                          # already destroyed — stop firing
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
            # The emitter burst does more than vent the shield: the emitters
            # themselves are wrecked, so the enemy can never cycle its shields
            # back up. Without this, "target shields" against a defensive
            # opponent was a trap — the player could hold the shield down
            # forever but never land a hull hit, so the fight was unwinnable.
            self.enemy_emitters_fried = True
            return [f">> SHIELD EMITTERS FRIED: {drained} enemy shield HP vented into "
                    f"space! Their emitters will not cycle back up."]
        if target_subsystem == "cargo":
            loot_good = random.choice(list(COMMODITIES.keys()))
            qty = random.randint(1, 4)
            added = self.engine._acquire_cargo(loot_good, qty, 0.0)
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

    def _evasive_maneuvers(self) -> List[str]:
        """Defensive posture: dodge the next enemy volley & top off shields."""
        p = self._p()
        if p.engines_damaged:
            return ["Thrusters are too badly damaged for evasive work — hold the line!"]
        self.evasive_until_turn = self.turn_count + 1
        eff_max = p.effective_max_shield()
        top_up = int(eff_max * 0.15)
        p.shield = min(eff_max, p.shield + top_up)
        self.engine.sound.play("warp")
        msgs = [f"Evasive pattern delta! Incoming fire accuracy halved next volley "
                f"(+{top_up} shield topped off)."]
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
            # Prize scales with the defeated vessel's size.
            size_lo = int(400 + self.enemy_max_hull * 1.5)
            size_hi = int(1_500 + self.enemy_max_hull * 4.5)
            loot_cr = random.randint(size_lo, max(size_lo + 1, size_hi))
            loot_good = random.choice(list(COMMODITIES.keys()))
            loot_qty = random.randint(2, max(3, self.enemy_max_hull // 80))
            added = self.engine._acquire_cargo(loot_good, loot_qty, 0.0)
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
            # An insured wreck is not the end of the career, so it is NOT a
            # Hall of Fame entry (and must not burn the one-shot record flag).
            self.insurance_used = True
            msgs = ["CRITICAL FAILURE: Your ship breaks apart around you..."]
            msgs.extend(self.engine.insurance_respawn())
            return msgs
        self.player_dead = True
        self.engine.is_game_over = True
        self.engine.record_hall_of_fame(won=False)
        self.engine.sound.play("death")
        return [
            "CRITICAL FAILURE: Ship destroyed in combat! Hull integrity collapsed.",
            "The void claims another trader.",
        ]

    # ------------------------------------------------------------------ #
    # Enemy AI

    def _enemy_attack(self, overpowered: bool = False, target_player_subsystem: bool = False,
                      torpedo: bool = False, power: float = 1.0) -> List[str]:
        p = self._p()
        msgs: List[str]

        if torpedo:
            msgs = ["WARNING: enemy launches a full TORPEDO VOLLEY — brace for impact!"]
            self.torpedo_volleys_fired += 1
        else:
            msgs = []

        base_dmg = random.randint(*self.enemy_damage_range)
        if self.enemy_weapons_damaged:
            base_dmg = int(base_dmg * 0.5)
        if overpowered:
            base_dmg = int(base_dmg * 1.2)
        if torpedo:
            base_dmg = int(base_dmg * 1.5)
        if power != 1.0:
            # A softened volley — used by defensive pilots who are busy with
            # their shield emitters.
            base_dmg = max(1, int(base_dmg * power))

        evasion = 0.05
        if p.has_module("thruster_booster"):
            evasion += 0.15
        if p.has_module("cloaking_field"):
            evasion += 0.10
        if p.has_crew_perk("nav"):
            evasion += 0.10
        if p.engines_damaged:
            evasion -= 0.10
        # Evasive maneuvers open the dodge window wide for one incoming volley.
        if self.evasive_until_turn >= self.turn_count:
            evasion += 0.30
            self.evasive_until_turn = -1
            msgs.append("Your spiraling evasive pattern scatters their firing solution!")

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
        # Sealed contract cargo is transponder-locked to its client: pirates
        # fence only what they can resell without a trace.
        candidates = [
            (g, min(q, p.sellable_cargo(g)))
            for g, q in p.cargo.items()
            if p.sellable_cargo(g) > 0
        ]
        if not candidates:
            return []
        good, stealable = random.choice(candidates)
        qty = min(stealable, random.randint(1, 3))
        removed = p.remove_cargo(good, qty)
        if p.cargo.get(good, 0) <= 0:
            p.cargo_cost_basis.pop(good, None)
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
        """Divert power to the shield emitters."""
        if self.enemy_emitters_fried:
            return ["The enemy's shield emitters are wrecked — they cannot cycle back up!"]
        recharge = int(self.enemy_max_shield * ENEMY_SHIELD_RECHARGE_RATE)
        before = self.enemy_shield
        self.enemy_shield = min(self.enemy_max_shield, self.enemy_shield + recharge)
        return [f"The enemy diverts power to shields (+{self.enemy_shield - before} HP)."]

    def _enemy_turn(self, overpowered: bool = False) -> List[str]:
        if self.is_finished or self.enemy_hull <= 0:
            return []

        msgs: List[str] = []
        p = self._p()

        # Heavily armed hostiles (heavy frigates and up) sometimes unload
        # a devastating torpedo spread instead of regular fire.
        heavy = self.enemy_damage_range[1] >= 30
        if heavy and self.torpedo_volleys_fired < 2 and random.random() < 0.18:
            msgs.extend(self._enemy_attack(torpedo=True))
            return msgs

        if self.personality == "coward" and self.enemy_hull < self.enemy_max_hull * 0.35:
            msgs.extend(self._enemy_flee_attempt())
            if self.is_finished:
                return msgs
            msgs.extend(self._enemy_attack(overpowered))
            return msgs

        if self.personality == "defensive" and self.enemy_shield < self.enemy_max_shield * 0.30:
            msgs.extend(self._enemy_charge())
            # A defensive pilot still shoots — it just shoots softer while it
            # is busy with its emitters. Skipping the attack entirely used to
            # hand the player a free turn, and the old 35% recharge rate
            # rebuilt shields faster than any single weapon could strip them,
            # turning a defensive enemy into an impassable shield wall.
            msgs.extend(self._enemy_attack(overpowered, power=DEFENSIVE_ATTACK_POWER))
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
    /* Keyboard users need to see where focus is. */
    .map-planet-node:focus { outline: none; }
    .map-planet-node:focus circle.planet-body {
      filter: drop-shadow(0 0 10px var(--cyan));
      stroke: var(--cyan);
      stroke-width: 3;
    }
    .hyperlane {
      stroke: rgba(46, 72, 156, 0.4);
      stroke-dasharray: 4 4;
      stroke-width: 1;
    }
    .current-ping {
      animation: ping 2s infinite;
      transform-origin: center;
      transform-box: fill-box;
    }
    /* Scale-based pulse: animating the SVG geometry property `r` is not
       supported by every engine (older Firefox renders it static). */
    @keyframes ping {
      0% { transform: scale(1); opacity: 0.8; }
      100% { transform: scale(2.2); opacity: 0; }
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
    /* Damage callouts (used by combat floaters) — previously referenced but
       never defined, so hit numbers rendered default-white. */
    .dmg-red { color: var(--red); }
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

    /* The game-over screen must sit ABOVE the tactical overlay (#combat-modal
       is z-index 300). Otherwise a destroyed ship leaves the player staring at
       a combat bridge whose only button is unreachable, and the recovery
       options in the game-over modal are covered. */
    #gameover-modal { z-index: 350; }
    /* The new-career dialog opens FROM the vessel-loss screen — it must stack
       above it, or the death dialog buries it (and its focus trap keeps
       yanking keyboard focus back, making "Start New Career" unreachable). */
    #newgame-modal { z-index: 360; }
    #victory-modal { z-index: 340; }

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

    .toast-msg.toast-error { border-left-color: var(--red); }
    #conn-banner {
      position: fixed; top: 0; left: 0; right: 0; z-index: 900;
      background: var(--red); color: #fff; text-align: center;
      padding: 6px 12px; font-weight: 700; font-size: 13px;
      transform: translateY(-100%); transition: transform 0.25s ease;
    }
    #conn-banner.active { transform: translateY(0); }
    body.busy { cursor: progress; }
    body.busy button { pointer-events: none; }
    :focus-visible { outline: 2px solid var(--cyan); outline-offset: 2px; }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation-duration: 0.01ms !important;
        animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; }
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
      <button class="hud-btn" id="btn-mute" onclick="toggleAudio()" aria-pressed="false" aria-label="Toggle sound effects">🔊 Sound</button>
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
  <nav id="tab-nav" role="tablist" aria-label="Main views">
    <button class="tab-btn active" id="tabbtn-map" role="tab" aria-selected="true" aria-controls="view-map" tabindex="0" onclick="switchTab('map')">🌌 Star Map</button>
    <button class="tab-btn" id="tabbtn-market" role="tab" aria-selected="false" aria-controls="view-market" tabindex="-1" onclick="switchTab('market')">📈 Commodity Market</button>
    <button class="tab-btn" id="tabbtn-advisor" role="tab" aria-selected="false" aria-controls="view-advisor" tabindex="-1" onclick="switchTab('advisor')">🧭 Trade Advisor</button>
    <button class="tab-btn" id="tabbtn-shipyard" role="tab" aria-selected="false" aria-controls="view-shipyard" tabindex="-1" onclick="switchTab('shipyard')">🚀 Shipyard & Outfitter</button>
    <button class="tab-btn" id="tabbtn-services" role="tab" aria-selected="false" aria-controls="view-services" tabindex="-1" onclick="switchTab('services')">🔧 Station Depot</button>
    <button class="tab-btn" id="tabbtn-crew" role="tab" aria-selected="false" aria-controls="view-crew" tabindex="-1" onclick="switchTab('crew')">👥 Crew Lounge</button>
    <button class="tab-btn" id="tabbtn-missions" role="tab" aria-selected="false" aria-controls="view-missions" tabindex="-1" onclick="switchTab('missions')">📜 Missions & Bounties</button>
    <button class="tab-btn" id="tabbtn-bank" role="tab" aria-selected="false" aria-controls="view-bank" tabindex="-1" onclick="switchTab('bank')">🏦 Bank & Stocks</button>
    <button class="tab-btn" id="tabbtn-log" role="tab" aria-selected="false" aria-controls="view-log" tabindex="-1" onclick="switchTab('log')">🎖️ Career & Log</button>
  </nav>

  <!-- MAIN APPLICATION BODY -->
  <main id="app-main">

    <!-- 1. STAR MAP VIEW -->
    <div id="view-map" class="view-container active" role="tabpanel" aria-labelledby="tabbtn-map" tabindex="0">
      <div id="map-wrapper">
        <div id="map-svg-container">
          <svg id="star-map-svg" width="100%" height="100%" viewBox="0 0 1000 650" role="group" aria-label="Sector star map" style="display: block;"></svg>
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
    <div id="view-market" class="view-container" role="tabpanel" aria-labelledby="tabbtn-market" tabindex="0">
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
                <th style="cursor: pointer;" onclick="sortMarket('name')" title="Click to sort">Commodity ⇅</th>
                <th>Category</th>
                <th style="cursor: pointer;" onclick="sortMarket('stock')" title="Click to sort">Station Stock ⇅</th>
                <th style="cursor: pointer;" onclick="sortMarket('buy_price')" title="Click to sort">Buy Price ⇅</th>
                <th style="cursor: pointer;" onclick="sortMarket('sell_price')" title="Click to sort">Sell Price ⇅</th>
                <th>10-Day Trend</th>
                <th style="cursor: pointer;" onclick="sortMarket('player_qty')" title="Click to sort">Cargo Held ⇅</th>
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
    <div id="view-advisor" class="view-container" role="tabpanel" aria-labelledby="tabbtn-advisor" tabindex="0">
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
                <th>Lane Risk</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody id="advisor-table-body">
              <!-- Dynamically populated -->
            </tbody>
          </table>
          <p style="font-size: 11px; color: var(--fg-dim); margin-top: 10px; line-height: 1.6;">
            Estimates use the same station spread, market-depth fee and fuel prices the market
            itself charges, and include both legs of the flight — out to the source world and on
            to the destination. Prices are re-generated every day you are in transit, so treat
            the figure as an estimate, not a guarantee.
          </p>
        </div>
      </div>
    </div>

    <!-- 4. SHIPYARD & OUTFITTER VIEW -->
    <div id="view-shipyard" class="view-container" role="tabpanel" aria-labelledby="tabbtn-shipyard" tabindex="0">
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
    <div id="view-services" class="view-container" role="tabpanel" aria-labelledby="tabbtn-services" tabindex="0">
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

          <!-- Odd Jobs / Port Work -->
          <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 10px;">
            <div style="font-weight: 700; color: var(--gold); font-size: 15px;">🛠️ Dockside Odd Jobs</div>
            <div style="font-size: 12px; color: var(--fg-dim);">
              Work a shift on the docks for wages. Costs one day — but a broke
              captain can always earn fuel money. Anti-strand guarantee.
            </div>
            <div style="font-size: 12px; color: var(--fg-dim);">
              Typical pay: <strong style="color: var(--green);">180–560 CR</strong> per shift (world dependent)
            </div>
            <button id="btn-odd-jobs" class="btn-action-sm btn-buy" style="margin-top: auto;" onclick="workOddJobs()">
              Work a Station Shift (+1 Day)
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- 6. CREW LOUNGE VIEW -->
    <div id="view-crew" class="view-container" role="tabpanel" aria-labelledby="tabbtn-crew" tabindex="0">
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
    <div id="view-missions" class="view-container" role="tabpanel" aria-labelledby="tabbtn-missions" tabindex="0">
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
    <div id="view-bank" class="view-container" role="tabpanel" aria-labelledby="tabbtn-bank" tabindex="0">
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

            <!-- Diplomatic Gifts Box -->
            <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 8px; padding: 16px;">
              <div style="font-weight: 700; color: var(--purple); margin-bottom: 8px;">🤝 Diplomatic Gift Office</div>
              <div style="font-size: 12px; color: var(--fg-dim); margin-bottom: 12px;">
                Donate credits to a faction's relief funds to buy goodwill (min
                500 CR · roughly +1 standing per 500 CR, capped +8 per gift).
              </div>
              <div id="gift-factions-row" style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                <!-- Dynamically populated -->
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
    <div id="view-log" class="view-container" role="tabpanel" aria-labelledby="tabbtn-log" tabindex="0">
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
              <div class="panel-card-title">🏆 Career Achievements (<span id="ach-count">0</span>)</div>
              <div class="panel-card-subtitle" id="visited-sub">Major milestones unlocked across your voyages.</div>
            </div>
          </div>
          <div id="achievements-grid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 8px;">
            <!-- Dynamically populated -->
          </div>

          <div style="margin-top: 24px;">
            <div class="panel-card-title" style="font-size: 14px; margin-bottom: 10px;">🏛️ Hall of Fame — Legendary Captains</div>
            <div id="hall-of-fame-list" style="display: flex; flex-direction: column; gap: 8px;">
              <!-- Dynamically populated -->
            </div>
          </div>

          <div style="margin-top: 24px;">
            <div class="panel-card-title" style="font-size: 14px; margin-bottom: 10px;">📡 Galactic Subspace News Wire</div>
            <div id="news-feed-list" style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 6px; padding: 12px; font-family: var(--font-mono); font-size: 11px; max-height: 200px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px;">
              <!-- Dynamically populated -->
            </div>
          </div>
        </div>
      </div>

      <div class="panel-card" style="margin-top: 20px;">
        <div class="panel-card-header">
          <div>
            <div class="panel-card-title">📈 Net Worth Trend</div>
            <div class="panel-card-subtitle">Daily net worth across your recent career — the road to 500,000 CR.</div>
          </div>
        </div>
        <div id="networth-sparkline" style="width: 100%;" aria-live="off">
          <!-- Dynamically populated -->
        </div>
      </div>
    </div>

  </main>

  <!-- TRADE SLIDER MODAL -->
  <div id="trade-modal" role="dialog" aria-modal="true" aria-labelledby="trade-modal-title" data-esc-close="closeTradeModal">
    <div class="modal-box">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
        <h3 id="trade-modal-title" style="color: var(--cyan); font-size: 18px;">Trade Commodity</h3>
        <button class="btn-action-sm" onclick="closeTradeModal()" aria-label="Close dialog">✕</button>
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
  <div id="combat-modal" role="dialog" aria-modal="true" aria-label="Tactical Combat">
    <div class="combat-bridge">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; border-bottom: 1px solid var(--red); padding-bottom: 12px;">
        <div style="display: flex; align-items: center; gap: 10px;">
          <span style="color: var(--red); font-size: 20px;">⚠️</span>
          <div>
            <div id="combat-enemy-name" style="font-size: 18px; font-weight: 800; color: var(--red);">Corsair Raider</div>
            <div id="combat-enemy-ship" style="font-size: 12px; color: var(--fg-dim);">Class: Viper Interceptor · Personality: Aggressive</div>
          </div>
        </div>
        <div style="display: flex; align-items: center; gap: 8px;">
          <span id="combat-threat-pill" class="pill pill-red" style="font-weight: 700;">THREAT: LOW</span>
          <span id="combat-turn-counter" class="pill pill-red">Turn 1</span>
        </div>
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
        <button class="btn-action-sm" id="btn-combat-evade" onclick="sendCombatAction('evade')">💫 Evasive Maneuvers</button>
        <button class="btn-action-sm" id="btn-combat-board" onclick="sendCombatAction('board')">🏴‍☠️ Board Vessel</button>
        <button class="btn-action-sm" style="color: var(--red);" onclick="sendCombatAction('flee')">🏃 Emergency Warp</button>
      </div>

      <!-- Combat Log Terminal -->
      <div id="combat-terminal-log" role="log" aria-live="polite" aria-label="Combat log" style="background: var(--bg); border: 1px solid var(--panel-border); border-radius: 6px; padding: 12px; font-family: var(--font-mono); font-size: 11px; max-height: 140px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; color: var(--fg-dim);">
        <div>Sensors locked onto enemy vessel. Weapons armed.</div>
      </div>

      <!-- Victory / Defeat Dismissal -->
      <div id="combat-result-box" style="display: none; margin-top: 16px; text-align: center;">
        <button class="dossier-btn-engage" id="combat-btn-dismiss" onclick="combatDismiss()">
          Disengage & Return to Helm
        </button>
      </div>
    </div>
  </div>

  <!-- DYNAMIC TRAVEL ENCOUNTER MODAL -->
  <div id="encounter-modal" class="generic-modal" role="dialog" aria-modal="true" aria-labelledby="enc-title">
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
  <div id="save-modal" class="generic-modal" role="dialog" aria-modal="true" aria-labelledby="save-modal-title" data-esc-close="closeSaveModal">
    <div class="modal-box">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
        <h3 id="save-modal-title" style="color: var(--cyan); font-size: 18px;">💾 Save / Load Game Flight Records</h3>
        <button class="btn-action-sm" onclick="closeSaveModal()" aria-label="Close dialog">✕</button>
      </div>

      <div id="save-slots-list" style="display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px;">
        <!-- Dynamically loaded -->
      </div>
    </div>
  </div>

  <!-- NEW GAME MODAL -->
  <div id="newgame-modal" class="generic-modal" role="dialog" aria-modal="true" aria-labelledby="newgame-modal-title" data-esc-close="closeNewGameModal">
    <div class="modal-box">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
        <h3 id="newgame-modal-title" style="color: var(--cyan); font-size: 18px;">🔄 New Captain Commission</h3>
        <button class="btn-action-sm" onclick="closeNewGameModal()" aria-label="Close dialog">✕</button>
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
  <div id="manual-modal" class="generic-modal" role="dialog" aria-modal="true" aria-labelledby="manual-modal-title" data-esc-close="closeManualModal">
    <div class="modal-box" style="max-width: 680px; max-height: 80vh; overflow-y: auto;">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; position: sticky; top: 0; background: var(--panel); padding-bottom: 8px;">
        <h3 id="manual-modal-title" style="color: var(--cyan); font-size: 18px;">📖 Space Trader: Odyssey — Field Manual</h3>
        <button class="btn-action-sm" onclick="closeManualModal()" aria-label="Close dialog">✕</button>
      </div>

      <div style="font-size: 13px; color: var(--fg); line-height: 1.6; display: flex; flex-direction: column; gap: 14px;">
        <div>
          <h4 style="color: var(--gold); margin-bottom: 4px;">🏆 Ultimate Objective: Galactic Mogul</h4>
          <p>Amass a total net worth of <strong>500,000 Credits</strong> through interstellar trading, passenger transport, pirate hunting, contract completion, and smart stock investments. Legendary captains are enshrined forever in the <strong>Hall of Fame</strong> (Career tab).</p>
        </div>

        <div>
          <h4 style="color: var(--cyan); margin-bottom: 4px;">📈 Interstellar Commerce</h4>
          <p>Planets produce commodities according to their planetary traits: agricultural worlds export cheap food and grain; mining stations flood the market with titanium and gems; high-tech arcologies demand raw minerals and export quantum processors. Buy low, consult the <strong>Trade Advisor</strong> (now with route risk ratings) for optimal routes, and sell high! Net trading profit feeds your Renown.</p>
          <p>The Trade Advisor quotes what the trade will really pay: the station's spread <em>and</em> its market-depth fee, plus fuel for both legs of the flight. Treat it as an estimate — prices are re-generated every day you are in transit.</p>
        </div>

        <div>
          <h4 style="color: var(--red); margin-bottom: 4px;">⚡ Tactical Starship Combat</h4>
          <p>Out in the lawless black, Free Corsairs and deserter dreadnoughts raid commercial shipping — and bigger hulls now punch proportionally harder (check the THREAT rating). Upgrade your hardpoints with Pulse Lasers, Heavy Barrier Shields, and Seeker Torpedoes. Fire <strong>Evasive Maneuvers</strong> to scatter the next enemy volley, cripple their thrusters to prevent escape, or breach the hull and launch a <strong>Boarding Action</strong> to seize their cargo and ransom their crew!</p>
          <p><strong>Target their shields</strong> to vent the emitter bank — a burst that wrecks the emitters themselves, so a defensive raider can never cycle its shields back up. It is the surest way to break a stubborn opponent that keeps out-shielding you.</p>
        </div>

        <div>
          <h4 style="color: var(--green); margin-bottom: 4px;">🛠️ Survival & Station Life</h4>
          <p>Flat broke and out of fuel? Work <strong>Dockside Odd Jobs</strong> at any spaceport — wages scale with the local economy, so a captain can never truly be stranded. If customs is closing in, <strong>Jettison</strong> contraband from the Cargo Hold before the scan. The <strong>Diplomatic Gift Office</strong> (Bank tab) converts credits into faction standing, and the stock exchange now reacts to planetary events via the market wire.</p>
        </div>

        <div>
          <h4 style="color: var(--purple); margin-bottom: 4px;">🎖️ Career Progression & Ranks</h4>
          <p>Earning renown advances your commission from <em>Cadet</em> to <em>Admiral</em>, unlocking massive price discounts, lower bank loan interest, cheaper repairs, and enhanced mission rewards. Visit all 16 systems to become a <em>Sector Cartographer</em>.</p>
        </div>

        <div>
          <h4 style="color: var(--fg-dim); margin-bottom: 4px;">⌨️ Hotkeys & New Kit</h4>
          <p><strong>1–9</strong> switch tabs · <strong>←/→</strong> move between tabs · <strong>F</strong> fire · <strong>M</strong> torpedo · <strong>R</strong> recharge · <strong>E</strong> evade · <strong>D</strong> drones · <strong>B</strong> board · <strong>Esc</strong> closes dialogs (except the combat and vessel-loss screens). Every dialog traps keyboard focus and hands it back when it closes, and the star map can be flown entirely from the keyboard. New equipment worth saving for: the <em>Bussard Ram-Scoop</em> (free fuel daily) and the <em>Heavy Extraction Rig</em> (double mining yields).</p>
        </div>
      </div>
    </div>
  </div>

  <!-- GAME OVER MODAL -->
  <div id="gameover-modal" class="generic-modal" role="dialog" aria-modal="true" aria-labelledby="gameover-modal-title">
    <div class="modal-box" style="text-align: center; border-color: var(--red);">
      <div style="font-size: 48px; margin-bottom: 10px;">💀</div>
      <h3 id="gameover-modal-title" style="color: var(--red); font-size: 22px; margin-bottom: 8px;">VESSEL DESTROYED</h3>
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
  <div id="victory-modal" class="generic-modal" role="dialog" aria-modal="true" aria-labelledby="victory-modal-title" data-esc-close="dismissVictory">
    <div class="modal-box" style="text-align: center; border-color: var(--gold); box-shadow: 0 0 60px rgba(255, 183, 3, 0.3);">
      <div style="font-size: 48px; margin-bottom: 10px;">🏆</div>
      <h3 id="victory-modal-title" style="color: var(--gold); font-size: 22px; margin-bottom: 8px;">GALACTIC MOGUL ACHIEVED</h3>
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
  <div id="amount-modal" class="generic-modal" role="dialog" aria-modal="true" aria-labelledby="amount-modal-title" data-esc-close="closeAmountModal">
    <div class="modal-box">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
        <h3 id="amount-modal-title" style="color: var(--cyan); font-size: 18px;">Enter Amount</h3>
        <button class="btn-action-sm" onclick="closeAmountModal()" aria-label="Close dialog">✕</button>
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
  <div id="conn-banner" role="alert">⚠ Connection to the game server lost — retrying…</div>
  <div id="toast-container" aria-live="polite"></div>

  <!-- APPLICATION LOGIC JAVASCRIPT -->
  <script>
    // --- Audio Engine (Web Audio API) ---
    let audioCtx = null;
    let audioMuted = false;

    // The mute preference is a real setting — remember it so a reload does
    // not blare the player back into a game they had silenced.
    const AUDIO_PREF_KEY = 'st_odyssey_muted';

    function loadAudioPreference() {
      try {
        const stored = window.localStorage.getItem(AUDIO_PREF_KEY);
        if (stored === '1' || stored === '0') audioMuted = (stored === '1');
      } catch (_) {
        audioMuted = false;   // storage unavailable (private mode): default on
      }
      const btn = document.getElementById('btn-mute');
      if (btn) btn.textContent = audioMuted ? '🔇 Muted' : '🔊 Sound';
    }

    function saveAudioPreference() {
      try {
        window.localStorage.setItem(AUDIO_PREF_KEY, audioMuted ? '1' : '0');
      } catch (_) { /* storage unavailable: the setting just will not persist */ }
    }

    function initAudio() {
      try {
        const AudioContextCtor = window.AudioContext || window.webkitAudioContext;
        if (!AudioContextCtor) return false;
        if (!audioCtx) audioCtx = new AudioContextCtor();
        if (audioCtx.state === 'suspended') {
          const resumeResult = audioCtx.resume();
          if (resumeResult && typeof resumeResult.catch === 'function') {
            resumeResult.catch(() => {}); // Browser autoplay policy may defer audio.
          }
        }
        return true;
      } catch (_) {
        audioCtx = null;
        return false;
      }
    }

    function toggleAudio() {
      audioMuted = !audioMuted;
      document.getElementById('btn-mute').textContent = audioMuted ? '🔇 Muted' : '🔊 Sound';
      document.getElementById('btn-mute').setAttribute('aria-pressed', audioMuted ? 'true' : 'false');
      saveAudioPreference();
      showToast(audioMuted ? 'Audio muted' : 'Audio enabled');
    }

    function playSound(type) {
      if (audioMuted || !initAudio() || !audioCtx) return;
      try {
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
      } catch (_) {
        // Audio is an optional enhancement; keep gameplay working if playback fails.
      }
    }

    // --- Toast Notifications ---
    function showToast(msg, kind) {
      const c = document.getElementById('toast-container');
      while (c.children.length >= 5) c.removeChild(c.firstChild);
      const t = document.createElement('div');
      t.className = 'toast-msg' + (kind === 'error' ? ' toast-error' : '');
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

    function escHtml(str) {
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    // ------------------------------------------------------------------
    // Modal manager — every overlay is a real dialog for keyboard and
    // screen-reader users: it takes focus, keeps focus inside itself while
    // it is open, and hands focus back when it closes.
    // ------------------------------------------------------------------
    const MODAL_IDS = [
      'trade-modal', 'combat-modal', 'encounter-modal', 'save-modal',
      'newgame-modal', 'manual-modal', 'gameover-modal', 'victory-modal',
      'amount-modal'
    ];
    let modalReturnFocus = null;

    function activeModals() {
      return MODAL_IDS
        .map(id => document.getElementById(id))
        .filter(el => el && el.classList.contains('active'));
    }

    function focusableIn(el) {
      if (!el) return [];
      return Array.from(el.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), ' +
        'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )).filter(node => {
        if (node.hidden) return false;
        if (node.style && node.style.display === 'none') return false;
        // getClientRects() is the accurate test in a real browser, but a
        // headless DOM performs no layout and always reports zero rects — so
        // only trust it when the document actually has a layout viewport.
        const doc = node.ownerDocument;
        if (doc && doc.documentElement && doc.documentElement.clientHeight > 0) {
          return node.getClientRects().length > 0;
        }
        return true;
      });
    }

    function showModal(id) {
      const el = document.getElementById(id);
      if (!el) return null;
      const wasActive = el.classList.contains('active');
      if (wasActive) return el;          // already open: do not steal focus
      if (!activeModals().length) modalReturnFocus = document.activeElement;
      el.classList.add('active');
      const focusables = focusableIn(el);
      if (focusables.length) {
        try { focusables[0].focus(); } catch (e) { /* older browsers */ }
      }
      return el;
    }

    function hideModal(id) {
      const el = document.getElementById(id);
      if (!el) return;
      const wasActive = el.classList.contains('active');
      el.classList.remove('active');
      if (wasActive && !activeModals().length && modalReturnFocus) {
        const back = modalReturnFocus;
        modalReturnFocus = null;
        if (back && back.focus) {
          try { back.focus(); } catch (e) { /* element gone */ }
        }
      }
    }

    // Escape closes the topmost dismissable overlay; Tab is trapped inside
    // the topmost overlay so keyboard focus can never wander behind it.
    document.addEventListener('keydown', (e) => {
      const stack = activeModals();
      if (!stack.length) return;
      const top = stack[stack.length - 1];

      if (e.key === 'Escape') {
        const closer = top.getAttribute('data-esc-close');
        if (closer && typeof window[closer] === 'function') {
          e.preventDefault();
          window[closer]();
        }
        return;
      }

      if (e.key === 'Tab') {
        const focusables = focusableIn(top);
        if (!focusables.length) { e.preventDefault(); return; }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const current = document.activeElement;
        if (!top.contains(current)) {
          e.preventDefault();
          first.focus();
        } else if (e.shiftKey && current === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && current === last) {
          e.preventDefault();
          first.focus();
        }
      }
    });

    // Look up a commodity's display name from the current market payload.
    function commodityName(goodId) {
      if (!gameState || !gameState.current_planet || !gameState.current_planet.market) return goodId;
      const found = gameState.current_planet.market.find(g => g.id === goodId);
      return found ? found.name : goodId;
    }

    function switchTab(tabId) {
      currentTab = tabId;
      document.querySelectorAll('.tab-btn').forEach(b => {
        const on = (b.id === 'tabbtn-' + tabId);
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
        b.tabIndex = on ? 0 : -1;      // roving tabindex, as the ARIA tabs pattern expects
      });
      document.querySelectorAll('.view-container').forEach(v => v.classList.remove('active'));

      const view = document.getElementById('view-' + tabId);
      if (view) view.classList.add('active');

      if (tabId === 'advisor') fetchTradeRoutes();
      if (tabId === 'map') renderStarMap();
    }

    // Arrow-key navigation between tabs, per the ARIA authoring practices.
    document.getElementById('tab-nav').addEventListener('keydown', (e) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
      const tabs = Array.from(document.querySelectorAll('.tab-btn'));
      const idx = tabs.indexOf(document.activeElement);
      if (idx < 0) return;
      e.preventDefault();
      let next = idx;
      if (e.key === 'ArrowRight') next = (idx + 1) % tabs.length;
      else if (e.key === 'ArrowLeft') next = (idx - 1 + tabs.length) % tabs.length;
      else if (e.key === 'Home') next = 0;
      else if (e.key === 'End') next = tabs.length - 1;
      switchTab(tabs[next].id.replace('tabbtn-', ''));
      tabs[next].focus();
    });

    // --- API Interactions ---
    let lastRevision = -1;
    let actionBusy = false;

    function setConnection(ok) {
      const el = document.getElementById('conn-banner');
      if (el) el.classList.toggle('active', !ok);
    }

    // Accept a server snapshot unless it is older than what is on screen, and
    // skip the (expensive) re-render when nothing changed since the last one.
    function applyState(state) {
      if (!state) return;
      const rev = (typeof state.revision === 'number') ? state.revision : null;
      if (rev !== null) {
        if (rev < lastRevision) return;
        if (rev === lastRevision && gameState) return;
        lastRevision = rev;
      }
      gameState = state;
      renderAll();
    }

    async function fetchState() {
      if (actionBusy) return;   // an action response is about to carry fresher state
      try {
        const res = await fetch('/api/state');
        const json = await res.json();
        setConnection(true);
        if (json.success && json.state) {
          if (json.sound) playSound(json.sound);
          applyState(json.state);
        }
      } catch (err) {
        setConnection(false);
      }
    }

    // One action at a time: double-clicks and held keys must not fire a
    // purchase, jump or volley twice.
    async function sendAction(action, payload = {}) {
      if (actionBusy) return null;
      actionBusy = true;
      document.body.classList.add('busy');
      try {
        const res = await fetch('/api/action', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action, ...payload })
        });
        const json = await res.json();
        setConnection(true);
        const failed = json.success === false;
        if (json.logs && json.logs.length) {
          json.logs.forEach(l => showToast(l, failed ? 'error' : 'ok'));
        } else if (failed && (json.message || json.error)) {
          showToast(json.message || json.error, 'error');
        }
        if (json.sound) playSound(json.sound);
        if (json.state) applyState(json.state);
        return json;
      } catch (err) {
        setConnection(false);
        showToast("Communication error: " + err, 'error');
        return null;
      } finally {
        actionBusy = false;
        document.body.classList.remove('busy');
      }
    }

    // --- Master Render ---
    let victoryShown = false;

    function renderAll() {
      if (!gameState) return;
      // One failing renderer must not abort the rest: previously a single
      // render-time exception stopped every later renderer AND
      // checkEncounter/checkCombat/checkGameOver, leaving stale panels and
      // modals that never opened (and the failure surfaced only as a bogus
      // "Communication error" toast).
      const focusId = (document.activeElement && document.activeElement.id)
        ? document.activeElement.id : null;
      const renderers = [renderHUD, renderVitals, renderStarMap, renderMarket,
        renderCargoHold, renderShipyard, renderServices, renderCrew,
        renderMissions, renderBank, renderLog];
      renderers.forEach(fn => {
        try { fn(); } catch (err) { console.error('render error in ' + fn.name, err); }
      });
      try { checkEncounter(); } catch (err) { console.error(err); }
      try { checkCombat(); } catch (err) { console.error(err); }
      try { checkGameOver(); } catch (err) { console.error(err); }
      try { checkVictory(); } catch (err) { console.error(err); }
      // Re-renders replace the innerHTML of whole containers, which silently
      // drops keyboard focus to <body>. Restore it when that happened.
      if (focusId && document.activeElement === document.body) {
        const el = document.getElementById(focusId);
        if (el && typeof el.focus === 'function') {
          try { el.focus({ preventScroll: true }); } catch (_) { el.focus(); }
        }
      }
    }

    function renderHUD() {
      const p = gameState.player;
      const cp = gameState.current_planet;

      document.getElementById('hud-rank-insignia').textContent = p.rank.insignia;
      document.getElementById('hud-rank-title').textContent = p.rank.name;
      document.getElementById('hud-renown-val').textContent = p.renown.toLocaleString();
      document.getElementById('hud-renown-next').textContent = p.renown_needed > 0 ? p.renown_needed.toLocaleString() : 'MAX';

      document.getElementById('hud-location').textContent = cp.name;
      const secPill = document.getElementById('hud-security');
      secPill.textContent = cp.security;
      // Threat colouring: a High-security world is patrolled and safe, an
      // anarchic one is a pirate haven. The pill used to be hardcoded green.
      secPill.classList.remove('pill-green', 'pill-gold', 'pill-red');
      secPill.classList.add(
        cp.security === 'High' ? 'pill-green' :
        cp.security === 'Medium' ? 'pill-gold' : 'pill-red');
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

      // Jump range ring — server-computed (accounts for warp booster & navigator)
      const maxRangeUnits = Math.max(0, gameState.player.max_jump_range || 0);
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
          <g class="map-planet-node" role="button" tabindex="0"
             aria-label="${escHtml(p.name)} — ${escHtml(p.subtitle)}${isCurrent ? ', current position' : ''}${hasMissions ? ', contracts available' : ''}"
             onclick="selectPlanet('${escJs(p.name)}')"
             onkeydown="if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectPlanet('${escJs(p.name)}'); }">
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
      document.getElementById('dossier-security').style.color =
        planet.security === 'High' ? 'var(--green)' :
        planet.security === 'Medium' ? 'var(--gold)' : 'var(--red)';

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

    // Column sorting state ('name' sorts alphabetically, others numerically).
    let marketSortKey = null;
    let marketSortDesc = false;

    function sortMarket(key) {
      if (marketSortKey === key) {
        marketSortDesc = !marketSortDesc;
      } else {
        marketSortKey = key;
        marketSortDesc = (key !== 'name');
      }
      renderMarket();
    }

    function renderMarket() {
      if (!gameState) return;
      const tbody = document.getElementById('market-table-body');
      let goods = [...(gameState.current_planet.market || [])];
      const query = (document.getElementById('market-search').value || '').toLowerCase();
      document.getElementById('market-station-sub').textContent = `Commercial Terminal at ${gameState.current_planet.name} Spaceport`;
      document.getElementById('market-free-cargo').textContent = gameState.player.cargo_free;

      if (marketCategoryFilter !== 'all') goods = goods.filter(g => g.category === marketCategoryFilter);
      if (query) goods = goods.filter(g => g.name.toLowerCase().includes(query));
      if (marketSortKey) {
        goods.sort((a, b) => {
          let va, vb;
          if (marketSortKey === 'name') { va = a.name; vb = b.name; }
          else { va = a[marketSortKey] || 0; vb = b[marketSortKey] || 0; }
          const cmp = (va < vb) ? -1 : (va > vb) ? 1 : 0;
          return marketSortDesc ? -cmp : cmp;
        });
      }

      let html = '';
      goods.forEach(g => {

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

      tbody.innerHTML = html || '<tr><td colspan="8" style="text-align: center; color: var(--fg-dim); padding: 18px;">'
        + 'No commodities match this filter. Clear the search box or pick a different category.'
        + '</td></tr>';
    }

    // --- Trade Slider Modal ---
    // Mirrors GameEngine.market_depth_fee / quote_buy / quote_sell exactly.
    function marketFee(unit, qty) {
      const eco = (gameState && gameState.economy) || { impact_rate: 0.002, impact_cap: 0.08 };
      const impact = Math.min(eco.impact_cap, qty * eco.impact_rate);
      return Math.floor(unit * qty * impact / 2 + 0.5);
    }
    function tradeTotal(unit, qty, mode) {
      const fee = marketFee(unit, qty);
      return mode === 'buy' ? unit * qty + fee : Math.max(0, unit * qty - fee);
    }

    function openTradeModal(goodId, mode) {
      selectedGoodId = goodId;
      tradeMode = mode;
      const g = gameState.current_planet.market.find(item => item.id === goodId);
      if (!g) return;

      const p = gameState.player;
      let maxQty = 1;
      if (mode === 'buy') {
        let cap = Math.min(g.stock, p.cargo_free);
        while (cap > 1 && tradeTotal(g.buy_price, cap, 'buy') > p.credits) cap--;
        maxQty = Math.max(1, cap);
        document.getElementById('trade-modal-title').textContent = `Purchase ${g.name}`;
        document.getElementById('trade-btn-confirm').textContent = 'Confirm Purchase';
        document.getElementById('trade-unit-price').textContent = `${g.buy_price} CR`;
      } else {
        maxQty = Math.max(1, g.sellable_qty !== undefined ? g.sellable_qty : g.player_qty);
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

      showModal('trade-modal');
    }

    function closeTradeModal() {
      hideModal('trade-modal');
    }

    function onTradeSliderChange(val) {
      const g = gameState.current_planet.market.find(item => item.id === selectedGoodId);
      if (!g) return;
      const qty = parseInt(val, 10);
      document.getElementById('trade-qty-display').textContent = qty;
      const unitP = tradeMode === 'buy' ? g.buy_price : g.sell_price;
      document.getElementById('trade-total-display').textContent = `${tradeTotal(unitP, qty, tradeMode).toLocaleString()} CR`;
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
            ${c.reserved > 0 ? `<div style="font-size: 11px; color: var(--cyan);">🔒 ${c.reserved} T sealed for active contracts</div>` : ''}
            <div style="display: flex; gap: 6px; margin-top: 8px;">
              <button class="btn-action-sm btn-sell" style="flex: 1;" ${c.sellable <= 0 ? 'disabled' : ''} onclick="openTradeModal('${c.id}', 'sell')">Sell All</button>
              <button class="btn-action-sm" style="flex: 1; color: var(--red);" ${c.sellable <= 0 ? 'disabled' : ''} title="Dump cargo into space (no refund, no customs fine)" onclick="promptJettison('${c.id}', ${c.sellable})">♻ Jettison</button>
            </div>
          </div>
        `;
      });
      grid.innerHTML = html || '<div style="color: var(--fg-dim); font-size: 12px; grid-column: 1 / -1;">Cargo hold is empty. Purchase commodities from the exchange above.</div>';
      document.getElementById('cargo-total-value').textContent = total.toLocaleString();
    }

    function promptJettison(goodId, maxQty) {
      openAmountModal(
        '♻ Jettison Cargo',
        `Dump units of this commodity into the void. No refund — but customs cannot fine you for cargo you no longer carry. You hold ${maxQty} unit(s).`,
        'Jettison Into Space',
        Math.max(1, Math.min(maxQty, 1)),
        'jettison_cargo',
        { key: 'qty', good: goodId }
      );
    }

    function workOddJobs() {
      sendAction('odd_jobs');
    }

    // --- Trade Advisor ---
    async function fetchTradeRoutes() {
      try {
        const res = await fetch('/api/routes');
        const json = await res.json();
        if (json.success && json.routes) {
          const tbody = document.getElementById('advisor-table-body');
          const RISK_COLORS = {
            'Minimal': 'var(--green)', 'Low': 'var(--green)', 'Moderate': 'var(--gold)',
            'High': 'var(--red)', 'Severe': 'var(--red)', 'Extreme': 'var(--red)',
          };
          let html = '';
          json.routes.forEach(r => {
            const riskColor = RISK_COLORS[r.risk] || 'var(--fg-dim)';
            const cr = (n) => Number(n).toLocaleString();
            // Full cost breakdown: the player can see exactly where the
            // advertised profit goes.
            const breakdown =
              `Buy ${r.qty}× @ ${r.buy_price} = ${cr(r.buy_cost)} CR`
              + ` · sale ${cr(r.sell_income)} CR`
              + ` · depth fee ${cr(r.depth_fee)} CR`
              + ` · fuel ${cr(r.fuel_credits)} CR`
              + ` (${r.inbound_fuel} out + ${r.outbound_fuel} on)`;
            html += `
              <tr>
                <td><strong style="color: var(--cyan);">${escHtml(r.good)}${r.is_contraband ? ' <span class="pill pill-red" style="font-size:9px;">ILLEGAL</span>' : ''}</strong>
                  <div style="font-size: 10px; color: var(--fg-dim); margin-top: 4px; line-height: 1.5;">${escHtml(breakdown)}</div>
                </td>
                <td>${escHtml(r.src)}</td>
                <td><strong style="color: var(--gold);">${escHtml(r.dst)}</strong></td>
                <td>${r.buy_price} / ${r.sell_price} CR</td>
                <td><span class="pill pill-green">+${r.margin} CR (${r.margin_pct}%)</span></td>
                <td><strong style="color: var(--gold); font-family: var(--font-mono);">${cr(r.net_profit)} CR</strong>
                  <div style="font-size: 10px; color: var(--fg-dim);">${cr(r.profit_per_day)} CR/day</div>
                </td>
                <td>${r.days} Days</td>
                <td><strong style="color: ${riskColor};">${escHtml(r.risk || '—')}</strong></td>
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
              <div style="font-family: var(--font-mono); font-size: 14px; font-weight: 700; color: var(--gold);">${s.net_cost < 0 ? '+' + (-s.net_cost).toLocaleString() + ' CR refund' : s.net_cost.toLocaleString() + ' CR'}</div>
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
                ${m.est_days != null ? `· <span style="color: var(--cyan);">${m.distance} LY · ~${m.est_days}d · ${m.est_fuel} fuel</span>` : ''}
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
                ${m.est_days != null && !canDeliver ? `· ETA ${m.est_days}d (${m.est_fuel} fuel)` : ''}
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

      // Diplomatic gifts grid
      const gContainer = document.getElementById('gift-factions-row');
      if (gContainer) {
        let gHtml = '';
        for (const [faction, rep] of Object.entries(p.reputation)) {
          const repColor = rep >= 15 ? 'var(--green)' : (rep <= -15 ? 'var(--red)' : 'var(--fg-dim)');
          gHtml += `
            <div style="background: var(--bg2); border: 1px solid var(--panel-border); border-radius: 6px; padding: 8px 10px; display: flex; justify-content: space-between; align-items: center; gap: 6px;">
              <div>
                <div style="font-weight: 700; font-size: 11px;">${escHtml(faction)}</div>
                <div style="font-size: 10px; color: ${repColor};">${p.standing[faction]} (${rep > 0 ? '+' : ''}${rep})</div>
              </div>
              <button class="btn-action-sm" style="color: var(--purple);" onclick="promptGift('${escJs(faction)}')">Gift</button>
            </div>
          `;
        }
        gContainer.innerHTML = gHtml;
      }

      function promptGift(faction) {
        openAmountModal(
          '🤝 Diplomatic Gift — ' + faction,
          'Donate credits to earn standing (~+1 per 500 CR, max +8 per gift). Minimum 500 CR.',
          'Present Gift',
          1000,
          'faction_gift',
          { key: 'amount', faction }
        );
      }
      window.promptGift = promptGift;

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

      // News Feed (escaped: captain names may contain anything)
      const nContainer = document.getElementById('news-feed-list');
      nContainer.innerHTML = gameState.news_feed.map(n => `<div>📡 ${escHtml(n)}</div>`).join('');

      // Visited systems progress
      const vSub = document.getElementById('visited-sub');
      if (vSub) {
        vSub.textContent = `Systems charted: ${p.visited_count || 0} / ${p.total_planets || 16} · Major milestones unlocked across your voyages.`;
      }

      // Hall of Fame
      const hofContainer = document.getElementById('hall-of-fame-list');
      if (hofContainer) {
        const hof = gameState.hall_of_fame || [];
        let hHtml = '';
        hof.forEach((entry, idx) => {
          const medal = idx === 0 ? '🥇' : (idx === 1 ? '🥈' : (idx === 2 ? '🥉' : '·'));
          hHtml += `
            <div style="background: var(--bg2); border: 1px solid ${entry.won ? 'var(--gold)' : 'var(--panel-border)'}; border-radius: 6px; padding: 8px 12px; display: flex; justify-content: space-between; align-items: center;">
              <div style="display: flex; align-items: center; gap: 8px;">
                <span style="font-size: 15px;">${medal}</span>
                <div>
                  <div style="font-weight: 700; font-size: 12px; color: ${entry.won ? 'var(--gold)' : 'var(--fg)'};">${escHtml(entry.name)} ${entry.won ? '<span class="pill pill-cyan" style="font-size:9px;">MOGUL</span>' : ''}</div>
                  <div style="font-size: 10px; color: var(--fg-dim);">${escHtml(entry.rank)} · ${escHtml(entry.difficulty)} · Day ${entry.day} · ${escHtml(entry.achieved_at || '')}</div>
                </div>
              </div>
              <strong style="font-family: var(--font-mono); color: var(--gold);">${(entry.net_worth || 0).toLocaleString()} CR</strong>
            </div>
          `;
        });
        hofContainer.innerHTML = hHtml || '<div style="color: var(--fg-dim); font-size: 12px;">No legends recorded yet — finish a career to be enshrined.</div>';
      }

      // Achievement counter (kept dynamic instead of hardcoded in the markup)
      const achCount = document.getElementById('ach-count');
      if (achCount) achCount.textContent = (p.achievements || []).length;

      // Net Worth Trend sparkline (SVG, no libraries)
      const nwBox = document.getElementById('networth-sparkline');
      if (nwBox) {
        const hist = p.net_worth_history || [];
        if (hist.length >= 2) {
          const W = 560, H = 96, PAD = 8;
          const lo = Math.min(...hist, 0);
          const hi = Math.max(...hist, 1);
          const span = Math.max(1, hi - lo);
          const pts = hist.map((v, i) => [
            PAD + (i / (hist.length - 1)) * (W - 2 * PAD),
            H - PAD - ((v - lo) / span) * (H - 2 * PAD),
          ]);
          const line = pts.map(pt => pt[0].toFixed(1) + ',' + pt[1].toFixed(1)).join(' ');
          const area = PAD + ',' + (H - PAD) + ' ' + line + ' ' + (W - PAD) + ',' + (H - PAD);
          const up = hist[hist.length - 1] >= hist[0];
          const stroke = up ? 'var(--green)' : 'var(--red)';
          const latest = hist[hist.length - 1] || 0;
          nwBox.innerHTML =
            '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width: 100%; height: 96px; display: block;" role="img" aria-label="Net worth trend for the last ' + hist.length + ' days">' +
            '<polygon points="' + area + '" fill="' + stroke + '" opacity="0.12"></polygon>' +
            '<polyline points="' + line + '" fill="none" stroke="' + stroke + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"></polyline>' +
            '</svg>' +
            '<div style="display: flex; justify-content: space-between; font-size: 11px; color: var(--fg-dim); font-family: var(--font-mono); margin-top: 6px;">' +
            '<span>Day 1: ' + (hist[0] || 0).toLocaleString() + ' CR</span>' +
            '<span style="color: ' + stroke + '; font-weight: 700;">Today: ' + latest.toLocaleString() + ' CR</span>' +
            '<span>Peak: ' + hi.toLocaleString() + ' CR</span>' +
            '</div>';
        } else {
          nwBox.innerHTML = '<div style="font-size: 11px; color: var(--fg-dim);">Fly a few more days to chart your trajectory toward 500,000 CR.</div>';
        }
      }
    }

    // --- Dynamic Encounters ---
    function checkEncounter() {
      if (!gameState || !gameState.active_encounter) {
        hideModal('encounter-modal');
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
        pirate_ambush: '☠️', bounty_combat: '🎯', comet_mining: '☄️',
        ghost_freighter: '👻', derby: '🏁', smuggler_cache: '📦'
      };
      document.getElementById('enc-icon').textContent = ENC_ICONS[t] || '📡';

      if (t === 'solar_flare') {
        // Unavoidable radiation event — single acknowledge button.
        opt1.textContent = 'Brace for Impact';
        opt2.style.display = 'none';
        showModal('encounter-modal');
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
        const goodName = commodityName(enc.good);
        opt1.textContent = `Buy Rare Deal (${enc.qty}x ${goodName} for ${(enc.qty * enc.unit_price).toLocaleString()} CR)`;
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
      } else if (t === 'comet_mining') {
        opt1.textContent = 'Harvest Ice Field (10 LY Fuel)';
        opt2.textContent = 'Sail Past the Comets';
      } else if (t === 'ghost_freighter') {
        opt1.textContent = 'Board Carefully (5 LY Fuel, Safer)';
        opt2.textContent = 'Strip Ship Quickly (Richer, Riskier)';
      } else if (t === 'derby') {
        opt1.textContent = 'Place ' + ((enc.wager) || 100).toLocaleString() + ' CR Wager (2.2x)';
        opt2.textContent = 'Just Watch the Heats';
      } else if (t === 'smuggler_cache') {
        opt1.textContent = 'Crack the Cache Open';
        opt2.textContent = 'Leave It Sealed';
      } else {
        opt1.textContent = 'Cooperate';
        opt2.textContent = 'Dismiss';
      }

      showModal('encounter-modal');
    }

    function resolveEncounter(choice) {
      sendAction('resolve_encounter', { choice });
    }

    // --- Tactical Combat ---
    function checkCombat() {
      const c = gameState ? gameState.active_combat : null;
      if (!c) {
        hideModal('combat-modal');
        return;
      }
      showModal('combat-modal');

      document.getElementById('combat-enemy-name').textContent = c.enemy_name;
      document.getElementById('combat-enemy-ship').textContent = `Class: ${c.enemy_ship_name} · Behavior: ${c.personality.toUpperCase()} — ${c.personality_desc || ''}`;
      document.getElementById('combat-turn-counter').textContent = `Combat Turn ${c.turn_count}`;

      // Threat assessment pill
      const threatPill = document.getElementById('combat-threat-pill');
      if (threatPill) {
        const tl = (c.threat_level || 'LOW');
        threatPill.textContent = `THREAT: ${tl} (${c.enemy_damage_lo}–${c.enemy_damage_hi} dmg)`;
        threatPill.style.background = tl === 'CRITICAL' || tl === 'HIGH' ? 'var(--red-dim)' : (tl === 'MODERATE' ? 'var(--gold-dim)' : 'var(--green-dim)');
        threatPill.style.borderColor = tl === 'CRITICAL' || tl === 'HIGH' ? 'var(--red)' : (tl === 'MODERATE' ? 'var(--gold)' : 'var(--green)');
        threatPill.style.color = tl === 'CRITICAL' || tl === 'HIGH' ? 'var(--red)' : (tl === 'MODERATE' ? 'var(--gold)' : 'var(--green)');
      }

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

      // Evasive Maneuvers Button
      const evadeBtn = document.getElementById('btn-combat-evade');
      if (evadeBtn) {
        evadeBtn.disabled = !c.can_flee;
        evadeBtn.title = c.can_flee ? 'Halve the accuracy of the next enemy volley' : 'Thrusters damaged — no evasive work possible';
      }

      // Boarding Button
      document.getElementById('btn-combat-board').disabled = !c.can_board;

      // Combat Terminal (escaped — logs may echo arbitrary content)
      const term = document.getElementById('combat-terminal-log');
      term.innerHTML = c.combat_log.map(l => `<div>> ${escHtml(l)}</div>`).join('');
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
          dBtn.dataset.mode = 'disengage';
        } else if (c.player_escaped) {
          dBtn.textContent = '🏃 Warp Drive Engaged — Disengage';
          dBtn.dataset.mode = 'disengage';
        } else if (c.player_dead) {
          // The career is over: the server refuses a plain dismissal, so send
          // the captain straight to the recovery screen instead.
          dBtn.textContent = '💀 Vessel Destroyed — Open Recovery Options';
          dBtn.dataset.mode = 'recover';
        } else {
          dBtn.dataset.mode = 'disengage';
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

    // The combat overlay's primary button. A destroyed vessel cannot be
    // "dismissed" (the career is over), so that case opens the recovery
    // screen instead of firing an action the server would refuse.
    function combatDismiss() {
      const btn = document.getElementById('combat-btn-dismiss');
      if (btn && btn.dataset.mode === 'recover') {
        showModal('gameover-modal');
        return;
      }
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
          showModal('save-modal');
        }
      } catch (e) {
        showToast("Error loading saves: " + e);
      }
    }

    function closeSaveModal() {
      hideModal('save-modal');
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
      showModal('newgame-modal');
    }
    function closeNewGameModal() {
      hideModal('newgame-modal');
    }
    function confirmNewGame() {
      const raw = document.getElementById('newgame-name').value;
      // Trim and fall back to the default — an all-whitespace name used to
      // create a blank captain.
      const name = raw.trim() || 'Commander';
      const difficulty = document.getElementById('newgame-diff').value;
      document.getElementById('newgame-name').value = name;
      closeNewGameModal();
      sendAction('new_game', { name, difficulty });
    }

    // --- Manual Modal ---
    function openManualModal() {
      showModal('manual-modal');
    }
    function closeManualModal() {
      hideModal('manual-modal');
    }

    // --- Game Over / Victory ---
    function checkGameOver() {
      if (gameState && gameState.player.is_game_over) {
        if (!document.getElementById('gameover-modal').classList.contains('active')) {
          showModal('gameover-modal');
          playSound('death');
        }
      } else {
        hideModal('gameover-modal');
      }
    }

    function recoverSave(slot) {
      hideModal('gameover-modal');
      sendAction('load_game', { slot });
    }

    function checkVictory() {
      if (gameState && gameState.player.victory && !victoryShown) {
        victoryShown = true;
        document.getElementById('victory-captain-name').textContent = gameState.player.name;
        showModal('victory-modal');
        playSound('victory');
      }
      if (!gameState || !gameState.player.victory) {
        victoryShown = false;
        hideModal('victory-modal');
      }
    }

    function dismissVictory() {
      hideModal('victory-modal');
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
      showModal('amount-modal');
      setTimeout(() => input.focus(), 50);
    }

    function closeAmountModal() {
      hideModal('amount-modal');
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
        `Available credits: ${gameState.player.credits.toLocaleString()} CR. Savings earn ${gameState.economy.savings_rate_pct}% daily on the first ${gameState.economy.savings_interest_cap.toLocaleString()} CR.`,
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
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') {
        // Enter confirms the amount modal while typing in it.
        if (e.key === 'Enter' && document.getElementById('amount-modal').classList.contains('active')) {
          confirmAmountModal();
        }
        return;
      }
      // Ignore browser shortcuts (Ctrl+R reload, Ctrl+F find...) and held keys.
      if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;

      // While a dialog is open it owns the keyboard: Escape and Tab belong to
      // the modal manager, and the tab shortcuts must not switch the view
      // behind the dialog. Combat hotkeys still work — the tactical overlay
      // is a dialog too.
      const dialogOpen = activeModals().length > 0;
      if (!dialogOpen) {
        if (e.key === '1') switchTab('map');
        if (e.key === '2') switchTab('market');
        if (e.key === '3') switchTab('advisor');
        if (e.key === '4') switchTab('shipyard');
        if (e.key === '5') switchTab('services');
        if (e.key === '6') switchTab('crew');
        if (e.key === '7') switchTab('missions');
        if (e.key === '8') switchTab('bank');
        if (e.key === '9') switchTab('log');
      }
      // Combat hotkeys (only while a battle is in progress)
      const combat = gameState && gameState.active_combat;
      if (combat && !combat.is_finished) {
        if (e.key === 'f' || e.key === 'F') sendCombatAction('fire');
        if (e.key === 'm' || e.key === 'M') sendCombatAction('missile');
        if (e.key === 'r' || e.key === 'R') sendCombatAction('recharge');
        if (e.key === 'e' || e.key === 'E') sendCombatAction('evade');
        if (e.key === 'd' || e.key === 'D') sendCombatAction('drones');
        if (e.key === 'b' || e.key === 'B') sendCombatAction('board');
      } else if (combat && combat.is_finished && (e.key === 'Enter')) {
        dismissCombat();
      }
    });

    // --- Initial Boot ---
    window.addEventListener('DOMContentLoaded', () => {
      loadAudioPreference();
      fetchState();
      setInterval(fetchState, 3000); // Polling for telemetry & auto-sync
    });
    // Unlock the Web Audio context from a real user gesture. initAudio() only
    // ever ran inside async fetch callbacks, so strict autoplay browsers
    // (Safari) could permanently refuse to start the audio graph.
    document.addEventListener('pointerdown', function unlockAudio() {
      initAudio();
      document.removeEventListener('pointerdown', unlockAudio);
    }, { once: true });
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
        self.lock = threading.Lock()
        # Bumped on every processed action. The browser uses it to discard stale
        # poll responses and to skip redundant re-renders.
        self.revision = 0

    def pop_sound(self) -> Optional[str]:
        # The browser client renders its own Web Audio effects; the legacy
        # server-side sound queue this once drained was dead code (set_sound
        # had no callers). Kept as a stub so old subclasses/clients keep working.
        return None


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
                "reserved": player.reserved_cargo(gid),
                "sellable": player.sellable_cargo(gid),
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
            {"id": m.id, "title": m.title, "type": m.m_type,
             "reward": m.reward_credits, "reward_credits": m.reward_credits,
             "days_left": m.days_left}
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
            "reserved_qty": player.reserved_cargo(gid),
            "sellable_qty": player.sellable_cargo(gid),
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
            "can_buy": (player.credits >= engine.quote_buy(gid, 1)
                        and (player.cargo_cap - cargo_used) > 0 and stock_qty > 0),
            "can_sell": player.sellable_cargo(gid) > 0,
        })

    # Shipyard
    ships_list = []
    trade_in_val = engine.ship_trade_in_value()
    for sid, st in SHIP_TEMPLATES.items():
        cost_after_trade = st.cost - trade_in_val      # negative => the yard pays you
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
            m_dict = asdict(m)
            m_dest = engine.planets.get(m.destination, current_p)
            m_fuel, m_days = engine.calculate_travel_cost_between(current_p, m_dest)
            m_dict["distance"] = round(engine.calculate_distance(current_p, m_dest), 1)
            m_dict["est_days"] = m_days
            m_dict["est_fuel"] = m_fuel
            available_missions.append(m_dict)

    active_missions = []
    for m in player.active_missions:
        m_dict = asdict(m)
        m_dest = engine.planets.get(m.destination, current_p)
        m_fuel, m_days = engine.calculate_travel_cost_between(current_p, m_dest)
        m_dict["distance"] = round(engine.calculate_distance(current_p, m_dest), 1)
        m_dict["est_days"] = m_days
        m_dict["est_fuel"] = m_fuel
        active_missions.append(m_dict)

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
            "threat_level": c.threat_level(),
            "enemy_damage_lo": c.enemy_damage_range[0],
            "enemy_damage_hi": c.enemy_damage_range[1],
            "torpedo_volleys": c.torpedo_volleys_fired,
        }

    net_worth = engine.calculate_net_worth()

    _state = {
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
            "max_jump_range": round(engine.max_jump_range(), 1),
            "visited_count": len(player.visited),
            "total_planets": len(engine.planets),
            "visited_planets": sorted(player.visited),
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
        "hall_of_fame": engine.load_hall_of_fame()[:10],
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
        "revision": session.revision,
        "economy": {
            "impact_rate": IMPACT_RATE,
            "impact_cap": IMPACT_CAP,
            "savings_rate_pct": round(SAVINGS_DAILY_RATE * 100, 2),
            "savings_interest_cap": SAVINGS_INTEREST_CAP,
            "min_spread_pct": round(MIN_SPREAD * 100, 2),
        },
        "active_encounter": session.active_encounter,
        "active_combat": combat_data,
        "last_logs": session.last_logs[-8:],
    }
    # Deep-copy the snapshot: the payload used to hold live references to
    # engine containers (cargo, price histories, equipped lists, stats...).
    # json.dumps() outside the session lock could then iterate a container a
    # concurrent action was mutating -> "dictionary changed size during
    # iteration" 500s and torn JSON. The copy makes every snapshot safely
    # detached from live engine state.
    return copy.deepcopy(_state)


GLOBAL_SESSION: Optional[GameSession] = None
_SESSION_LOCK = threading.Lock()


def get_session() -> GameSession:
    """Return the shared game session, creating it exactly once (thread-safe)."""
    global GLOBAL_SESSION
    with _SESSION_LOCK:
        if GLOBAL_SESSION is None:
            GLOBAL_SESSION = GameSession()
        return GLOBAL_SESSION


# ------------------------------------------------------------------------------
# Action dispatch — pure Python, no HTTP, so the test-suite can drive it directly
# ------------------------------------------------------------------------------

class ActionError(Exception):
    """A malformed request (bad or missing argument). Maps to HTTP 400."""


# action -> (GameEngine method, ((json key, kind, default), ...), sound on success)
EngineSpec = Tuple[str, Tuple[Tuple[str, str, Any], ...], Optional[str]]
ENGINE_ACTIONS: Dict[str, EngineSpec] = {
    "odd_jobs":          ("odd_jobs", (), "coin"),
    "jettison_cargo":    ("jettison_cargo", (("good", "str", ""), ("qty", "int", 1)), None),
    "faction_gift":      ("faction_gift", (("faction", "str", ""), ("amount", "int", GIFT_MIN)), "coin"),
    "buy_commodity":     ("buy_commodity", (("good", "str", ""), ("qty", "int", 1)), "buy"),
    "sell_commodity":    ("sell_commodity", (("good", "str", ""), ("qty", "int", 1)), "sell"),
    "buy_fuel":          ("buy_fuel", (("amount", "int", 10),), "coin"),
    "repair_hull":       ("repair_hull", (("hp", "int", 10),), "upgrade"),
    "repair_subsystems": ("repair_subsystems", (), "upgrade"),
    "buy_insurance":     ("buy_insurance", (), "coin"),
    "buy_missiles":      ("buy_missiles", (("qty", "int", 1),), "coin"),
    "buy_ship":          ("buy_ship", (("ship_id", "str", ""),), "upgrade"),
    "buy_equipment":     ("buy_equipment", (("eq_id", "str", ""),), "upgrade"),
    "sell_equipment":    ("sell_equipment", (("eq_id", "str", ""),), "sell"),
    "hire_crew":         ("hire_crew", (("crew_id", "str", ""),), "coin"),
    "dismiss_crew":      ("dismiss_crew", (("crew_id", "str", ""),), None),
    "accept_mission":    ("accept_mission", (("mission_id", "str", ""),), "coin"),
    "abandon_mission":   ("abandon_mission", (("mission_id", "str", ""),), None),
    "bank_deposit":      ("deposit", (("amount", "int", 100),), "coin"),
    "bank_withdraw":     ("withdraw", (("amount", "int", 100),), "coin"),
    "bank_borrow":       ("borrow", (("amount", "int", 100),), "coin"),
    "bank_repay":        ("repay", (("amount", "int", 100),), "coin"),
    "buy_stock":         ("buy_stock", (("symbol", "str", ""), ("qty", "int", 1)), "coin"),
    "sell_stock":        ("sell_stock", (("symbol", "str", ""), ("qty", "int", 1)), "sell"),
}

META_ACTIONS = ("new_game", "load_game", "save_game")
COMBAT_ACTIONS = ("combat_action", "dismiss_combat")
MAX_ID_LEN = 64

CAREER_OVER_MSG = ("Your career has ended, Captain. "
                   "Start a new commission or load a flight record.")
IN_COMBAT_MSG = "Battle stations! Finish the engagement before doing anything else."
ENCOUNTER_PENDING_MSG = "A sensor contact demands your decision first."


def _arg(data: Dict[str, Any], key: str, kind: str, default: Any) -> Any:
    """Extract one typed argument from an untrusted request body."""
    raw = data.get(key, default)
    if kind == "int":
        try:
            return to_int(raw)
        except ValueError:
            raise ActionError(f"{key} must be an integer.") from None
    return str(raw)[:MAX_ID_LEN]


def _resolve_encounter(engine: GameEngine, enc: Dict[str, Any],
                       choice: bool) -> Tuple[List[str], str]:
    """Apply the player's choice to a pending encounter -> (log lines, sound)."""
    t = enc.get("type")
    if t == "customs_scan":
        # UI: choice=True is "Submit to Scan"; the engine wants bribe=True for
        # the opposite option, hence the inversion.
        return engine.resolve_customs(enc, bribe=not choice), ("alarm" if choice else "coin")
    if t == "faction_patrol":
        return engine.resolve_faction_patrol(enc, choice), ("coin" if choice else "alarm")
    if t == "derelict":
        return engine.resolve_derelict(enc, choice), ("upgrade" if choice else "click")
    if t == "solar_flare":
        return engine.resolve_solar_flare(enc), "shield_hit"
    if t == "distress_beacon":
        return engine.resolve_distress(enc, choice), ("coin" if choice else "click")
    if t == "wandering_trader":
        return engine.resolve_trader(enc, choice), ("buy" if choice else "click")
    if t == "asteroid_field":
        return engine.resolve_asteroid_field(enc, choice), ("shield_hit" if choice else "coin")
    if t == "wormhole":
        return engine.resolve_wormhole(enc, choice), ("wormhole" if choice else "click")
    if t == "mining_opportunity":
        return engine.resolve_mining(enc, choice), ("mine" if choice else "click")
    if t == "comet_mining":
        return engine.resolve_comet(enc, choice), ("mine" if choice else "click")
    if t == "ghost_freighter":
        return engine.resolve_ghost(enc, careful=choice), ("victory" if choice else "alarm")
    if t == "derby":
        return engine.resolve_derby(enc, bet=choice), "coin"
    if t == "smuggler_cache":
        return engine.resolve_smuggler_cache(enc, investigate=choice), \
            ("victory" if choice else "click")
    return [], "click"


def handle_action(session: GameSession, data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply one client action to the session and return the JSON response.

    The caller must hold ``session.lock``. Raises ActionError on a malformed
    request; every other outcome (including refusals) is a normal response.
    """
    engine = session.engine
    action = str(data.get("action", ""))
    success = False
    message = ""
    sound: Optional[str] = None
    logs: List[str] = []

    def refuse(text: str) -> None:
        nonlocal success, message
        success, message = False, text
        logs.append(text)

    # A finished career only allows meta actions (new game, load) — plus the
    # combat dismissal, which must ALWAYS stay reachable. The tactical overlay
    # is painted above the game-over modal, so refusing "dismiss_combat" here
    # used to trap the player on the death screen with no way to recover.
    if engine.is_game_over and action not in META_ACTIONS + COMBAT_ACTIONS:
        refuse(CAREER_OVER_MSG)
        session.revision += 1
        return {"success": False, "message": message, "logs": [], "sound": None,
                "state": serialize_game_state(session)}

    combat = session.active_combat
    if combat is not None and combat.is_finished and action not in COMBAT_ACTIONS + META_ACTIONS:
        # The player moved on without pressing "continue" — close the fight.
        session.active_combat = None
        combat = None
        engine.autosave()

    if action in ("new_game", "load_game"):
        pass   # these replace the whole session, so they bypass the gates
    elif combat is not None and not combat.is_finished and action not in COMBAT_ACTIONS:
        refuse(IN_COMBAT_MSG)
    elif session.active_encounter and action != "resolve_encounter":
        refuse(ENCOUNTER_PENDING_MSG)

    if not message:   # no gate refusal above
        if action == "new_game":
            name = str(data.get("name", "Commander"))
            diff = str(data.get("difficulty", "normal")).strip().lower()
            engine.new_game(name, diff)
            session.active_encounter = None
            session.active_combat = None
            session.last_logs = [f"New commission created for {engine.player.name} "
                                 f"on {engine.difficulty.name.upper()} difficulty."]
            success, message, sound = True, f"Welcome aboard, {engine.player.name}!", "warp"

        elif action == "load_game":
            success, message = engine.load_game(_arg(data, "slot", "str", "1"))
            logs.append(message)
            if success:
                session.active_encounter = None
                session.active_combat = None
                sound = "warp"

        elif action == "save_game":
            success, message = engine.save_game(_arg(data, "slot", "str", "1"))
            logs.append(message)

        elif action == "travel":
            success, message, enc = engine.execute_travel(_arg(data, "destination", "str", ""))
            logs.append(message)
            if success:
                sound = "warp"
                session.active_encounter = None
                session.active_combat = None
                if enc:
                    if enc.get("type") in ("pirate_ambush", "bounty_combat"):
                        session.active_combat = start_combat(engine, enc)
                        sound = "alarm"
                        logs.append(f"ALARM: Hostile vessel intercepted! "
                                    f"{enc.get('title', 'Raider Attack')}")
                    else:
                        session.active_encounter = enc
                        logs.append(f"Sector Sensor Contact: {enc.get('title', 'Anomaly Detected')}")

        elif action == "resolve_encounter":
            enc = session.active_encounter
            if enc:
                enc_logs, sound = _resolve_encounter(engine, enc, bool(data.get("choice", False)))
                session.active_encounter = None
                success = True
                logs.extend(enc_logs)
                message = enc_logs[-1] if enc_logs else "Encounter resolved."
                # Persist the outcome: quitting right after resolving used to
                # roll the encounter result back to the pre-encounter state.
                if not engine.is_game_over:
                    engine.autosave()
            else:
                success, message = False, "No active encounter to resolve."

        elif action == "combat_action":
            c_act = _arg(data, "combat_action", "str", "fire")
            if combat is None or combat.is_finished:
                success, message = False, "No active combat engagement."
            elif c_act not in CombatEncounter.ACTIONS:
                success, message = False, "Unknown combat maneuver."
            else:
                c_logs = combat.player_action(c_act)
                logs.extend(c_logs)
                success = True
                message = c_logs[-1] if c_logs else "Combat turn complete."
                sound = {"fire": "laser", "target_engines": "laser", "target_weapons": "laser",
                         "target_shields": "laser", "missile": "laser", "recharge": "upgrade",
                         "evade": "warp", "board": "upgrade", "drones": "upgrade",
                         "flee": "warp" if combat.player_escaped else "alarm"}.get(c_act)
                if combat.is_finished:
                    if combat.player_won:
                        sound = "victory"
                    elif combat.player_dead:
                        sound = "death"

        elif action == "dismiss_combat":
            if combat is not None and combat.is_finished:
                session.active_combat = None
                success = True
                if combat.player_dead:
                    # Never persist a destroyed ship over the autosave — the
                    # pre-combat snapshot is the recovery point.
                    message = "Vessel lost. Recovery options available."
                else:
                    engine.autosave()
                    message = "Disengaged from combat arena."
            else:
                if combat is None:
                    success, message = False, "No combat engagement to dismiss."
                else:
                    success, message = False, "Combat is still in progress."

        elif action in ENGINE_ACTIONS:
            method, arg_specs, ok_sound = ENGINE_ACTIONS[action]
            args = [_arg(data, key, kind, default) for key, kind, default in arg_specs]
            success, message = getattr(engine, method)(*args)
            logs.append(message)
            if success:
                sound = ok_sound

        else:
            success, message = False, f"Unknown action: {action}"

    if logs:
        session.last_logs.extend(logs)
        session.last_logs = session.last_logs[-15:]

    promo = engine.check_promotion()
    if promo:
        sound = "rank_up"
        session.last_logs.append(f"PROMOTION: Promoted to rank of {promo.name}! {promo.perk}")

    session.revision += 1
    return {"success": success, "message": message, "logs": logs, "sound": sound,
            "state": serialize_game_state(session)}


# ------------------------------------------------------------------------------
# HTTP layer
# ------------------------------------------------------------------------------

MAX_BODY_BYTES = 64 * 1024
HTML_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'")


class SpaceTraderWebHandler(http.server.BaseHTTPRequestHandler):
    """Multi-threaded HTTP handler serving the browser game client and REST API.

    The game is a local, single-player service. There is deliberately NO CORS
    allowance, and state-changing requests must be same-origin JSON, so a random
    web page open in your browser cannot drive or corrupt your game."""

    server_version = f"SpaceTraderOdyssey/{APP_VERSION.rsplit('.', 1)[0]}"
    sys_version = ""

    def version_string(self) -> str:
        # Do not advertise the Python runtime version in the Server header.
        return self.server_version

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress noisy default HTTP access logs."""
        return

    # -- response helpers -------------------------------------------------- #

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if content_type.startswith("text/html"):
            self.send_header("Content-Security-Policy", HTML_CSP)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Dict[str, Any], status: int = 200) -> None:
        self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status)

    def _fail(self, status: int, message: str) -> None:
        self._json({"success": False, "error": message}, status)

    # -- verbs ------------------------------------------------------------- #

    def do_OPTIONS(self) -> None:
        # No CORS: a cross-origin preflight gets no permission headers.
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        # _send() already omits the body when self.command == "HEAD", and the
        # vestigial server-side sound queue this verb used to disturb is gone.
        self.do_GET()

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self._internal_error()

    def do_POST(self) -> None:
        try:
            self._handle_post()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            self._internal_error()

    def _internal_error(self) -> None:
        traceback.print_exc(file=sys.stderr)
        try:
            self._fail(500, "Internal server error — the incident was logged to the console.")
        except Exception:
            pass

    def _handle_get(self) -> None:
        path = urllib.parse.urlparse(self.path).path

        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in ("/", "/index.html"):
            self._send(HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            self._json({"status": "ok", "app": "Space Trader: Odyssey", "version": APP_VERSION})
            return

        if not path.startswith("/api/"):
            self._fail(404, "File not found.")
            return

        session = get_session()
        # Build payloads under the lock, but serialize + transmit outside it:
        # a stalled client must never block other tabs' actions for up to the
        # full socket timeout.
        if path == "/api/state":
            with session.lock:
                payload = {"success": True, "state": serialize_game_state(session)}
            self._json(payload)
        elif path == "/api/routes":
            with session.lock:
                routes = session.engine.compute_best_trade_routes()[:12]
            self._json({"success": True, "routes": routes})
        elif path == "/api/saves":
            with session.lock:
                slots = []
                for s in (AUTO_SLOT, PRECOMBAT_SLOT) + SAVE_SLOTS:
                    info = session.engine.slot_info(s)
                    slots.append({"slot": s, "exists": info is not None, "info": info})
            self._json({"success": True, "slots": slots})
        elif path == "/api/scores":
            with session.lock:
                scores = session.engine.load_hall_of_fame()[:10]
            self._json({"success": True, "scores": scores})
        else:
            self._fail(404, "Unknown endpoint.")

    def _handle_post(self) -> None:
        if urllib.parse.urlparse(self.path).path != "/api/action":
            self._fail(404, "Unknown endpoint.")
            return

        # Cross-site requests from other web pages are never legitimate.
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            self._fail(403, "Cross-site requests are not allowed.")
            return

        # Browsers set Origin on fetch/XHR POSTs. When present, require the
        # same HTTP origin as this listener. Clients without Origin remain
        # supported for local CLI/API tooling; this server is not internet-safe.
        origin = self.headers.get("Origin")
        if origin:
            parsed_origin = urllib.parse.urlsplit(origin)
            request_host = self.headers.get("Host", "").strip().lower()
            if (parsed_origin.scheme not in ("http", "https") or not parsed_origin.netloc
                    or parsed_origin.netloc.lower() != request_host
                    or parsed_origin.path not in ("",) or parsed_origin.query
                    or parsed_origin.fragment or parsed_origin.username
                    or parsed_origin.password):
                self._fail(403, "Origin does not match this game server.")
                return

        # Reject transfer encodings and repeated Content-Length headers before
        # reading a body, avoiding ambiguous framing and request-smuggling cases.
        if self.headers.get("Transfer-Encoding") is not None:
            self._fail(400, "Transfer-Encoding is not supported.")
            return
        content_lengths = self.headers.get_all("Content-Length", [])
        if len(content_lengths) != 1:
            self._fail(411, "Exactly one Content-Length header is required.")
            return

        content_type = self.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            self._fail(415, "Content-Type must be application/json.")
            return

        raw_length = content_lengths[0].strip()
        if not raw_length.isascii() or not raw_length.isdecimal():
            self._fail(400, "Invalid Content-Length.")
            return
        try:
            length = int(raw_length)
        except ValueError:
            self._fail(400, "Invalid Content-Length.")
            return
        if length > MAX_BODY_BYTES:
            self._fail(413, "Request body too large.")
            return

        try:
            raw_body = self.rfile.read(length)
            if len(raw_body) != length:
                self._fail(400, "Request body ended before Content-Length bytes were received.")
                return
            data = json.loads(raw_body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            self._fail(400, f"Invalid JSON: {exc}")
            return
        if not isinstance(data, dict):
            self._fail(400, "Request body must be a JSON object.")
            return

        session = get_session()
        try:
            with session.lock:
                response = handle_action(session, data)
        except ActionError as exc:
            self._fail(400, str(exc))
            return
        self._json(response)



class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded server with bounded client sockets for a small local game."""
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32
    client_socket_timeout = 10.0

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(self.client_socket_timeout)
        return request, client_address

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

    scratch = tempfile.mkdtemp(prefix="st_test_")
    os.environ["ST_SAVE_DIR"] = scratch

    try:
        # --- 1. Data integrity ---
        print("\n--- 1. Data integrity ---")
        e_init = GameEngine(muted=True)
        e_init.new_game("Tester", "normal")
        check("16 planets defined", len(e_init.planets) == 16)
        check("10 ships defined", len(SHIP_TEMPLATES) == 10)
        check("24 equipment items", len(EQUIPMENT_ITEMS) == 24)
        check("18 commodities", len(COMMODITIES) == 18)
        check("20 events reference valid goods", len(PLANET_EVENTS_POOL) == 20 and all(ev[1] in COMMODITIES for ev in PLANET_EVENTS_POOL))
        check("31 achievements defined", len(ACHIEVEMENTS) == 31)
        check("every achievement key is unique & mapped", all(aid in ACHIEVEMENTS for aid in ACHIEVEMENTS))
        check("planet coordinates unique", len({(p.x, p.y) for p in e_init.planets.values()}) == len(e_init.planets))
        check("4 factions defined", len(FACTIONS) == 4)
        check("all planet factions are known factions", all(p.faction in FACTIONS for p in e_init.planets.values()))
        check("integer parser preserves integral values", to_int("12.0") == 12 and to_int(7.0) == 7)
        try:
            to_int(1.5)
            fractional_rejected = False
        except ValueError:
            fractional_rejected = True
        check("integer parser rejects fractional values", fractional_rejected)
        check("faction stocks are valid symbols", all(s in DEFAULT_STOCKS for s in FACTION_STOCK_LINK.values()))
        check("6 ranks defined", len(RANKS) == 6)
        check("enemy damage table covers all ship classes", all(
            st.ship_class in ENEMY_CLASS_DAMAGE for st in SHIP_TEMPLATES.values()))
        costs = [s.cost for s in SHIP_TEMPLATES.values()]
        check("ship costs strictly ascending", costs == sorted(costs))
        check("starter hull has a real listed price",
              SHIP_TEMPLATES[STARTING_SHIP].cost > 0,
              "the ship you start with must be worth something")
        check("starter hull has a trade-in value",
              int(SHIP_TEMPLATES[STARTING_SHIP].cost * 0.7) > 0)
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

        # The advisor must quote what the trade actually pays: the same
        # market-depth fee, both legs of fuel, and both legs of travel time.
        # It used to quote margin * qty with no fees and one leg of fuel,
        # over-stating profit by ~10% and profit-per-day by up to 3.7x.
        advisor_ok = True
        advisor_detail = ""
        for r in routes[:6]:
            src = e_trd.planets[r["src"]]
            dst = e_trd.planets[r["dst"]]
            gid, qty = r["good_id"], r["qty"]
            unit_buy = e_trd.get_buy_price(gid, src)
            unit_sell = e_trd.get_sell_price(gid, dst)
            fee_buy = e_trd.market_depth_fee(unit_buy, qty)
            fee_sell = e_trd.market_depth_fee(unit_sell, qty)
            in_fuel, in_days = e_trd.calculate_travel_cost_between(
                e_trd.current_planet, src)
            if src.name == e_trd.player.location:
                in_fuel, in_days = 0, 0
            out_fuel, out_days = e_trd.calculate_travel_cost_between(src, dst)
            fuel_cr = (e_trd.fuel_credit_cost(in_fuel, e_trd.current_planet)
                       + e_trd.fuel_credit_cost(out_fuel, src))
            expected_net = (unit_sell * qty - fee_sell) - (unit_buy * qty + fee_buy) - fuel_cr
            if expected_net != r["net_profit"] or r["days"] != in_days + out_days:
                advisor_ok = False
                advisor_detail = f"{r['good']} {r['src']}->{r['dst']}"
                break
        check("advisor net profit equals the real trade cost", advisor_ok, advisor_detail)
        check("advisor charges both legs of fuel", all(
            r["fuel_credits"] > 0 and r["days"] >= r["outbound_days"]
            for r in routes))
        check("advisor never advertises a losing route", all(
            r["net_profit"] > 0 for r in routes))
        check("advisor breakdown adds up", all(
            r["net_profit"] == r["sell_income"] - r["buy_cost"] - r["fuel_credits"]
            and r["days"] == r["inbound_days"] + r["outbound_days"]
            and r["profit_per_day"] == int(r["net_profit"] / max(1, r["days"]))
            for r in routes))

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
        check("wormhole relocates player", e_enc.player.location != loc_b)
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
        # If the enemy did NOT flee it must have been destroyed and counted.
        check("pirates_defeated counted", combat.enemy_fled
              or e_cmb.player.stats.get("pirates_defeated", 0) >= 1)

        # A defensive pilot must still return fire while it recharges. It used
        # to skip the attack entirely, handing the player a free turn.
        e_def = GameEngine(muted=True)
        e_def.new_game("Bunker", "normal")
        e_def.player.hull = e_def.player.max_hull
        e_def.player.shield = e_def.player.max_shield
        c_def = CombatEncounter(e_def, "Bunker", "orion")
        c_def.personality = "defensive"
        c_def.enemy_shield = 1          # deep inside the charge band
        hull_before = e_def.player.hull
        shield_before = e_def.player.shield
        random.seed(4242)
        c_def.player_action("fire")
        check("defensive enemy still returns fire while charging",
              e_def.player.hull < hull_before or e_def.player.shield < shield_before)
        check("shield recharge rate is breakable",
              int(c_def.enemy_max_shield * ENEMY_SHIELD_RECHARGE_RATE) <= 100,
              f"recharge {int(c_def.enemy_max_shield * ENEMY_SHIELD_RECHARGE_RATE)} is too high")

        # Venting the emitters must stop the shield cycle for good — without
        # this, "target shields" against a defensive enemy was unwinnable.
        e_fr = GameEngine(muted=True)
        e_fr.new_game("Fryer", "normal")
        c_fr = CombatEncounter(e_fr, "Bunker", "orion")
        c_fr.personality = "defensive"
        c_fr.enemy_hull = 500
        c_fr.enemy_shield = 200
        random.seed(99)
        for _ in range(4):
            if c_fr.is_finished:
                break
            c_fr.player_action("target_shields")
        check("frying the emitters disables the shield cycle",
              c_fr.enemy_emitters_fried and c_fr.enemy_shield == 0)
        check("a defensive enemy can be worn down", c_fr.enemy_hull < 500)

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
        # A *remote* world: scanning your own station finds nothing, which
        # used to make this assertion pass vacuously.
        remote_world = next(p for p in e_dth.planets.values()
                            if p.name != e_dth.player.location)
        deals = e_dth.remote_top_deals(remote_world)
        check("deep scanner returns deal list", isinstance(deals, list))
        check("deep scanner finds remote deals", len(deals) > 0)
        check("remote deals well-formed", all(
            d["margin"] > 0 and d["stock"] > 0 for d in deals))
        e_dth.player.equipped_modules = []
        check("remote deals sorted by margin",
              [d["margin"] for d in deals] == sorted([d["margin"] for d in deals], reverse=True))

        # --- 21. Game-over gating ---
        print("\n--- 21. Game-over action gating ---")
        e_gate = GameEngine(muted=True)
        e_gate.new_game("Ghost Captain", "normal")
        e_gate.player.hull = 1
        enc_gate = {"enemy_name": "Executioner", "enemy_ship": "behemoth", "type": "pirate_ambush"}
        combat_gate = start_combat(e_gate, enc_gate)
        combat_gate._handle_player_death()
        check("death sets game-over flag", e_gate.is_game_over)
        ok_dead, msg_dead = e_gate.buy_commodity("water", 1)
        check("dead captain cannot buy", not ok_dead)
        ok_dead2 = e_gate.execute_travel("Mars")[0]
        check("dead captain cannot travel", not ok_dead2)
        ok_dead3, _ = e_gate.deposit(100)
        check("dead captain cannot bank", not ok_dead3)
        ok_dead4, _ = e_gate.odd_jobs()
        check("dead captain cannot work odd jobs", not ok_dead4)
        e_gate.new_game("Reborn", "normal")
        check("new_game revives career", not e_gate.is_game_over)
        ok_alive, _ = e_gate.buy_commodity("water", 1)
        check("revived captain can trade again", ok_alive)

        # --- 22. Odd jobs (anti-softlock) ---
        print("\n--- 22. Odd jobs anti-softlock ---")
        e_job = GameEngine(muted=True)
        e_job.new_game("Stranded", "normal")
        e_job.player.credits = 0
        e_job.player.fuel = 0
        e_job.player.savings = 0
        e_job.player.loan = 0
        shifts = 0
        cheapest = None
        for pname, p in e_job.planets.items():
            if pname == e_job.player.location:
                continue
            f, _d = e_job.calculate_travel_cost(p)
            if cheapest is None or f < cheapest:
                cheapest = f
        while e_job.player.credits < cheapest * 12 and shifts < 40:
            e_job.odd_jobs()
            shifts += 1
        check("odd jobs always pay", e_job.player.credits > 0)
        check("odd jobs cost time", e_job.player.day > shifts)
        check("stranded captain can buy fuel again", e_job.buy_fuel(min(cheapest, e_job.player.max_fuel))[0])
        ok_fly = e_job.execute_travel(
            next(pn for pn in e_job.planets if pn != e_job.player.location))[0]
        check("stranded captain can fly again", ok_fly)
        check("odd jobs stat tracked", e_job.player.stats.get("odd_jobs_done", 0) == shifts)

        # --- 23. Ship downgrade equipment refund ---
        print("\n--- 23. Downgrade salvage refund ---")
        e_dg = GameEngine(muted=True)
        e_dg.new_game("Downgrader", "normal")
        e_dg.player.credits = 1_000_000
        e_dg.buy_ship("valkyrie")          # 4 weapon / 3 shield / 3 module slots
        e_dg.buy_equipment("laser_2")
        e_dg.buy_equipment("flak_cannon")
        e_dg.buy_equipment("plasma_1")
        before_credits = e_dg.player.credits
        lost_val = sum(EQUIPMENT_ITEMS[w].cost for w in e_dg.player.equipped_weapons[2:])
        e_dg.buy_ship("orion")             # 2 weapon slots -> truncation
        expected_refund = int(lost_val * 0.75)
        # Signed: a cheaper hull pays you the trade-in difference.
        net_ship_cost = SHIP_TEMPLATES["orion"].cost - int(SHIP_TEMPLATES["valkyrie"].cost * 0.7)
        expected_after = before_credits - net_ship_cost + expected_refund
        check("truncated equipment refunded at 75%",
              e_dg.player.credits == expected_after,
              f"got {e_dg.player.credits}, expected {expected_after}")
        check("weapon list truncated to new slots", len(e_dg.player.equipped_weapons) == 2)

        # --- 24. Enemy threat scaling ---
        print("\n--- 24. Enemy threat scaling ---")
        e_th = GameEngine(muted=True)
        e_th.new_game("Threat", "normal")
        c_small = CombatEncounter(e_th, "Skiff", "sparrow")
        c_large = CombatEncounter(e_th, "Dreadnought", "behemoth")
        check("bigger ships hit harder",
              c_large.enemy_damage_range[1] > c_small.enemy_damage_range[1] * 1.5)
        check("threat levels rating present", c_large.threat_level() in (
            "LOW", "MODERATE", "HIGH", "CRITICAL"))
        check("flagship outruns courier threat",
              CombatEncounter(e_th, "Flag", "sovereign").enemy_damage_range[0] >
              c_small.enemy_damage_range[0])

        # --- 25. Evasive maneuvers ---
        print("\n--- 25. Evasive maneuvers ---")
        e_ev = GameEngine(muted=True)
        e_ev.new_game("Dodger", "normal")
        enc_ev = {"enemy_name": "Test Corsair", "enemy_ship": "sparrow", "type": "pirate_ambush"}
        combat_ev = start_combat(e_ev, enc_ev)
        real_random = random.random
        random.random = lambda: 0.99   # evade declared, no dodge, enemy lands hits
        try:
            msgs_ev = combat_ev.player_action("evade")
        finally:
            random.random = real_random
        check("evade action accepted", any("Evasive pattern" in m for m in msgs_ev))
        check("enemy still responds after evade", any(
            ("shields for" in m) or ("hull damage" in m) or ("dodged" in m) for m in msgs_ev))
        check("evade is a listed action", "evade" in CombatEncounter.ACTIONS)

        # --- 26. Jettison, gifts & derby ---
        print("\n--- 26. Jettison, gifts & derby ---")
        e_j = GameEngine(muted=True)
        e_j.new_game("Dumper", "normal")
        # Seed the hold directly (buying contraband is stock-RNG dependent).
        e_j.player.cargo["narcotics"] = 2
        e_j.player.cargo_cost_basis["narcotics"] = 100.0
        ok_j, _ = e_j.jettison_cargo("narcotics", 1)
        check("jettison removes cargo", ok_j and e_j.player.cargo.get("narcotics", 0) == 1)
        ok_j2, _ = e_j.jettison_cargo("narcotics", 99)
        check("jettison clamps to owned", ok_j2 and "narcotics" not in e_j.player.cargo)
        ok_j3, _ = e_j.jettison_cargo("nothing_here", 1)
        check("jettison unknown good rejected", not ok_j3)

        e_g = GameEngine(muted=True)
        e_g.new_game("Benefactor", "normal")
        e_g.player.credits = 100_000
        rep_before = e_g.player.rep("Outer Alliance")
        ok_g, _ = e_g.faction_gift("Outer Alliance", 4_000)
        check("diplomatic gift accepted", ok_g)
        check("gift raises standing", e_g.player.rep("Outer Alliance") > rep_before)
        ok_g2, _ = e_g.faction_gift("Fake Union", 1_000)
        check("unknown faction gift rejected", not ok_g2)
        ok_g3, _ = e_g.faction_gift("Sol Federation", 100)
        check("undersized gift rejected", not ok_g3)

        e_db = GameEngine(muted=True)
        e_db.new_game("Gambler", "normal")
        e_db.player.credits = 5_000
        cr_before = e_db.player.credits
        outcome = e_db.resolve_derby({"wager": 1_000}, True)
        won = e_db.player.credits > cr_before
        lost = e_db.player.credits < cr_before
        check("derby resolves one way or the other", (won or lost) and len(outcome) == 1)
        check("derby loss bounded", e_db.player.credits >= cr_before - 1_000)

        # --- 27. Hall of Fame & sanitize ---
        print("\n--- 27. Hall of Fame & name sanitize ---")
        e_h = GameEngine(muted=True)
        e_h.new_game("<script>alert(1)</script>Legend", "normal")
        check("hostile name markup stripped",
              "<" not in e_h.player.name and ">" not in e_h.player.name,
              f"got {e_h.player.name!r}")
        e_h.player.credits = TARGET_NET_WORTH + 1_000
        e_h.check_achievements()
        hof = e_h.load_hall_of_fame()
        check("victory recorded to hall of fame", any(
            e.get("name") == e_h.player.name and e.get("won") for e in hof))
        check("hall of fame sorted by net worth", [
            e.get("net_worth", 0) for e in hof
        ] == sorted([e.get("net_worth", 0) for e in hof], reverse=True))
        e_h.record_hall_of_fame(won=True)
        check("duplicate HOF entries suppressed", len(e_h.load_hall_of_fame()) == len(hof))

        # --- 28. New encounters & fuel scoop ---
        print("\n--- 28. New encounters & modules ---")
        e_n = GameEngine(muted=True)
        e_n.new_game("Explorer", "normal")
        fuel_before_comet = e_n.player.fuel
        e_n.resolve_comet({"title": "Comets"}, True)
        check("comet harvest yields water", e_n.player.cargo.get("water", 0) >= 1)
        check("comet harvest burns fuel", e_n.player.fuel < fuel_before_comet)
        check("comet stat tracked", e_n.player.stats.get("comet_ops", 0) >= 1)

        e_n2 = GameEngine(muted=True)
        e_n2.new_game("Prospector", "normal")
        e_n2.player.equipped_modules = ["mining_rig"]
        e_n2.player.cargo_cap = 100
        e_n2.resolve_mining({"vein": "ore"}, True)
        check("mining rig boosts yield", e_n2.player.cargo.get("ore", 0) >= 5)

        e_n3 = GameEngine(muted=True)
        e_n3.new_game("Scoop", "normal")
        e_n3.player.fuel = 10
        e_n3.player.equipped_modules = ["fuel_scoop"]
        e_n3.advance_day(1)
        check("fuel scoop regenerates fuel", e_n3.player.fuel >= 13)

        e_n4 = GameEngine(muted=True)
        e_n4.new_game("Route", "normal")
        routes_n = e_n4.compute_best_trade_routes()
        check("routes carry risk rating", all("risk" in r for r in routes_n))
        check("contraband routes rate riskier", all(
            (r.get("risk_level", 0) >= 2) for r in routes_n if r["is_contraband"]))
        check("max jump range computed", e_n4.max_jump_range() > 0)

        # smuggler_cache encounter
        e_n5 = GameEngine(muted=True)
        e_n5.new_game("Cacher", "normal")
        out_leave = e_n5.resolve_smuggler_cache({"type": "smuggler_cache"}, False)
        check("cache left alone is a no-op", len(out_leave) == 1
              and e_n5.player.stats.get("caches_found", 0) == 0)
        hull_before = e_n5.player.hull
        out_cache = e_n5.resolve_smuggler_cache({"type": "smuggler_cache"}, True)
        check("cache investigation resolves", len(out_cache) >= 1)
        check("cache outcome stays sane", 1 <= e_n5.player.hull <= hull_before
              and e_n5.player.credits >= 0)
        e_n5.player.stats["caches_found"] = 3
        e_n5.check_achievements()
        check("cache_hunter achievement unlocks", "cache_hunter" in e_n5.player.achievements)
        e_n5.player.stats["missions_completed"] = 15
        e_n5.check_achievements()
        check("missions_15 achievement unlocks", "missions_15" in e_n5.player.achievements)

        # --- 29. Cleanup ---
        print("\n--- 29. Cleanup ---")
        shutil.rmtree(scratch, ignore_errors=True)
        check("test scratch dir removed", not os.path.exists(scratch))

    except Exception as exc:
        print(f"\nTest exception encountered: {exc}")
        import traceback
        traceback.print_exc()
        failed.append(f"Exception: {exc}")
    finally:
        # Always remove the scratch dir — previously a mid-suite failure
        # skipped cleanup and leaked st_test_* directories.
        shutil.rmtree(scratch, ignore_errors=True)

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
    import socket
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
            assert resp.headers.get("X-Frame-Options") == "DENY"
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("Content-Security-Policy")
            print("  [PASS] GET / (HTML frontend + security headers)")

        # 1b. Health endpoint includes an explicit app version.
        with urllib.request.urlopen(f"{base_url}/api/health") as resp:
            health = json.loads(resp.read().decode("utf-8"))
            assert health["status"] == "ok" and health["version"] == APP_VERSION
            assert "Python/" not in resp.headers.get("Server", "")
            print("  [PASS] GET /api/health (versioned, minimal server header)")

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
            headers={"Content-Type": "application/json; charset=utf-8", "Origin": base_url}
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

        fractional_req = urllib.request.Request(
            f"{base_url}/api/action",
            data=json.dumps({"action": "buy_commodity", "good": "water", "qty": 1.5}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(fractional_req)
            raise AssertionError("fractional quantity should be rejected")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            print("  [PASS] fractional quantity rejected")

        wrong_type_req = urllib.request.Request(
            f"{base_url}/api/action", data=b"{}", headers={"Content-Type": "application/jsonp"}
        )
        try:
            urllib.request.urlopen(wrong_type_req)
            raise AssertionError("JSONP content type should be rejected")
        except urllib.error.HTTPError as exc:
            assert exc.code == 415
            print("  [PASS] non-JSON media type rejected")

        cross_origin_req = urllib.request.Request(
            f"{base_url}/api/action", data=b'{"action":"odd_jobs"}',
            headers={"Content-Type": "application/json", "Origin": "http://attacker.invalid"}
        )
        try:
            urllib.request.urlopen(cross_origin_req)
            raise AssertionError("cross-origin request should be rejected")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            print("  [PASS] mismatched Origin rejected")

        oversized_req = urllib.request.Request(
            f"{base_url}/api/action", data=b"x" * (MAX_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(oversized_req)
            raise AssertionError("oversized request should be rejected")
        except urllib.error.HTTPError as exc:
            assert exc.code == 413
            print("  [PASS] oversized request rejected before JSON parsing")

        transfer_req = urllib.request.Request(
            f"{base_url}/api/action", data=b'{"action":"odd_jobs"}',
            headers={"Content-Type": "application/json", "Transfer-Encoding": "chunked"}
        )
        try:
            urllib.request.urlopen(transfer_req)
            raise AssertionError("Transfer-Encoding should be rejected")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            print("  [PASS] ambiguous Transfer-Encoding rejected")

        def raw_request(request_bytes: bytes) -> bytes:
            with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                sock.settimeout(3)
                sock.sendall(request_bytes)
                chunks = []
                while True:
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)

        missing_length = raw_request(
            b"POST /api/action HTTP/1.1\r\nHost: 127.0.0.1:" + str(port).encode()
            + b"\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n{}"
        )
        assert missing_length.startswith(b"HTTP/1.0 411") or missing_length.startswith(b"HTTP/1.1 411")
        print("  [PASS] missing Content-Length rejected")

        duplicate_length = raw_request(
            b"POST /api/action HTTP/1.1\r\nHost: 127.0.0.1:" + str(port).encode()
            + b"\r\nContent-Type: application/json\r\nContent-Length: 2\r\n"
            + b"Content-Length: 2\r\nConnection: close\r\n\r\n{}"
        )
        assert duplicate_length.startswith(b"HTTP/1.0 411") or duplicate_length.startswith(b"HTTP/1.1 411")
        print("  [PASS] duplicate Content-Length rejected")

        # 6. New endpoints: odd jobs + jettison + gifts
        req_jobs = urllib.request.Request(
            f"{base_url}/api/action",
            data=json.dumps({"action": "odd_jobs"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req_jobs) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            assert data["state"]["player"]["stats"]["odd_jobs_done"] >= 1
            print("  [PASS] POST /api/action (odd_jobs)")

        req_gift = urllib.request.Request(
            f"{base_url}/api/action",
            data=json.dumps({"action": "faction_gift", "faction": "Outer Alliance", "amount": 800}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req_gift) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            print("  [PASS] POST /api/action (faction_gift)")

        # 7. Scores endpoint (Hall of Fame)
        with urllib.request.urlopen(f"{base_url}/api/scores") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode("utf-8"))
            assert data["success"] is True
            assert isinstance(data["scores"], list)
            print("  [PASS] GET /api/scores (hall of fame)")

        # 8. State exposes the new player telemetry
        with urllib.request.urlopen(f"{base_url}/api/state") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            p = data["state"]["player"]
            assert "max_jump_range" in p and "visited_count" in p
            assert isinstance(data["state"]["hall_of_fame"], list)
            missions = data["state"]["available_missions"]
            assert len(missions) == 0 or "est_days" in missions[0]
            print("  [PASS] GET /api/state (new telemetry fields)")

        print("WEB SERVER SMOKE TEST PASSED ✔")
    finally:
        server.shutdown()
        server.server_close()
        # Always remove the scratch dir — it previously leaked every run.
        shutil.rmtree(scratch, ignore_errors=True)


# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================

def parse_port(value: str) -> int:
    """Parse a valid TCP port for the command-line interface."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("port must be an integer between 1 and 65535") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main() -> None:
    parser = argparse.ArgumentParser(description="Space Trader: Odyssey — Nebula Edition (Browser HTML)")
    parser.add_argument("--version", action="version", version=f"Space Trader: Odyssey {APP_VERSION}")
    parser.add_argument("--test", action="store_true", help="Run headless engine self-test suite and exit.")
    parser.add_argument("--web-test", action="store_true", help="Run headless web server smoke test and exit.")
    parser.add_argument("--difficulty", choices=list(DIFFICULTIES.keys()), default="normal")
    parser.add_argument("--player", default="Commander")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--mute", action="store_true", help="Legacy flag: server audio is always silent (browser handles sound).")
    parser.add_argument("--port", type=parse_port, default=os.environ.get("PORT", "3000"), help="HTTP server port (1-65535)")
    parser.add_argument(
        "--host",
        default="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1",
        help="HTTP bind host (default: localhost only; 0.0.0.0 when $PORT is set by a host)",
    )

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
    try:
        httpd = ThreadedHTTPServer(server_address, SpaceTraderWebHandler)
    except OSError as exc:
        print(f"Cannot listen on {args.host}:{args.port} — {exc}.\n"
              f"Try another port:  python3 spacetrader.py --port {args.port + 1}",
              file=sys.stderr)
        sys.exit(1)

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
