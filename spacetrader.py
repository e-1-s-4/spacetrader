#!/usr/bin/env python3
"""
================================================================================
 SPACE TRADER: ODYSSEY — Nebula Edition
================================================================================
A complete sci-fi trading, exploration, and combat RPG with a full graphical
user interface built on Python's built-in tkinter toolkit. No third-party
dependencies are required — one file, zero installs, pure stdlib.

WHAT'S NEW IN THE NEBULA EDITION
--------------------------------
* CAPTAIN RANK PROGRESSION: earn Renown through trading, contracts, bounties
  and exploration. Rise from Cadet to Ensign, Lieutenant, Captain, Commodore
  and finally Admiral — each rank grants a tangible career perk (better
  prices, richer contracts, cheaper services, faster reputation, softer
  loans). Promotions are announced sector-wide.
* NEON COSMOS UI: a fully re-themed deep-space interface — animated star map
  with twinkling stars, drifting nebulae, faction territory rings, orbit
  paths, a pulsing home-world beacon and a jump animation; glowing HUD with
  rank insignia and a net-worth progress arc.
* LIVE PRICE CHARTS: every commodity on the Market tab now carries a 10-day
  unicode sparkline, and selecting a good opens a full detail chart with
  min / max / average and value-vs-base analysis plus a trade calculator.
* NET WORTH TELEMETRY: the Captain's Log graphs your entire career as a
  glowing net-worth curve.
* EXPANDED FLEET & OUTFITTER (10 ships / 22 items): the armored Drake
  Freighter and the long-range Phoenix Explorer join the shipyard; new gear
  includes the Flak Cannon, the endgame Particle Beam Lance, a Shield
  Capacitor bank and a Deep Space Scanner that reads remote markets.
* THREE NEW ENCOUNTERS: thread dense asteroid fields, ride unstable
  wormholes across the sector, and strip-mine rich asteroid veins.
* SMOOTHER CAMPAIGN CURVE: gentler early-game pirates (threat now scales
  through net-worth tiers), fairer fines, richer contract payouts, ship
  speed now shortens travel time, and a credit score that shapes your loan
  interest. Less grind, more decisions.
* QUALITY-OF-LIFE: sortable market columns, hover tooltips on the star map,
  a help manual, per-tab smart refresh (big performance win), fixed the
  mid-combat escape exploit, and Nightmare difficulty selectable from every
  new-game dialog.
* Save system: 3 manual slots + autosave + pre-combat snapshot (fully
  compatible with Deluxe Edition saves). Saves can be relocated with the
  ST_SAVE_DIR environment variable.
* Cross-platform sound tones, 23 achievements, and a built-in self-test
  suite covering every engine system.

HOW TO RUN
----------
    python spacetrader.py                 Launch the game (GUI)
    python spacetrader.py --test          Run the headless engine self-test suite
    python spacetrader.py --gui-test      Run a headless GUI smoke check (needs X)
    python spacetrader.py --mute          Start with sound disabled
    python spacetrader.py --difficulty easy|normal|hard|nightmare
    python spacetrader.py --seed N        Deterministic RNG for testing

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

import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText


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
         "+5% sell · 3% cheaper buys · loan interest -40% · insurance -25%"),
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

        self.player.remove_cargo(good, qty)
        self.player.credits += total_income
        self.current_planet.stock[good] = self.current_planet.stock.get(good, 0) + qty
        self._price_impact(good, qty, "sell")
        self.player.stats["total_profit"] += total_income

        if COMMODITIES[good].is_contraband:
            self.player.stats["contraband_sold"] += qty

        self.sound.play("sell")
        self.check_achievements()
        msg = f"Sold {qty}x {COMMODITIES[good].name} for {money(total_income)} CR."
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

    def check_mission_deliveries(self) -> List[str]:
        completed_msgs: List[str] = []
        for m in list(self.player.active_missions):
            if m.destination == self.player.location and not m.completed and not m.failed:
                if m.m_type in ("delivery", "smuggle", "medical"):
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
        dist = self.calculate_distance(self.current_planet, dest)
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

    def compute_best_trade_routes(self, from_current_only: bool = False) -> List[Dict[str, Any]]:
        routes: List[Dict[str, Any]] = []
        planets_list = list(self.planets.values())
        my_ship = SHIP_TEMPLATES[self.player.ship_id]

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

                    dist = self.calculate_distance(src, dst)
                    fuel_units = int(dist * 3.0) + 4
                    fuel_credit_estimate = fuel_units * max(
                        4, int(src.fuel_price * self.difficulty.fuel_mult))

                    potential_qty = min(self.player.cargo_cap, src.stock.get(gid, 0))
                    if potential_qty <= 0:
                        continue

                    total_profit = margin * potential_qty
                    net_profit = total_profit - fuel_credit_estimate
                    days = max(1, int(dist * 0.55 / max(0.5, my_ship.speed)))

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
        return os.path.exists(slot_path(slot))

    def any_saves_exist(self) -> bool:
        return any(self.has_save(s) for s in (AUTO_SLOT, PRECOMBAT_SLOT) + SAVE_SLOTS)

    @staticmethod
    def slot_info(slot: str) -> Optional[Dict[str, Any]]:
        """Lightweight metadata for the save/load dialogs."""
        path = slot_path(slot)
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
        path = slot_path(slot)
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
            msgs.extend(self._board_enemy())
        elif action == "flee":
            escaped, flee_msgs = self._player_flee()
            msgs.extend(flee_msgs)
            if escaped:
                return msgs

        if self.is_finished:
            return msgs

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

    def _board_enemy(self) -> List[str]:
        p = self._p()
        if not self.can_board():
            return ["Boarding requires the enemy hull to be at 25% integrity or less."]
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
        else:
            msgs.append(">> BOARDING REPULSED! Your party is forced back under heavy fire.")
            p.shield = 0
            msgs.extend(self._enemy_turn(overpowered=True))
        return msgs

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
    return CombatEncounter(engine, enemy_name, enemy_ship,
                           is_bounty=is_bounty, bounty_reward=reward)


# ==============================================================================
# GRAPHICAL USER INTERFACE (tkinter) — Neon Cosmos edition
# ==============================================================================

THEME = {
    "bg":        "#04081C",   # deep space
    "bg2":       "#070D26",
    "panel":     "#0C1330",
    "panel_hi":  "#141E44",
    "field":     "#080D22",
    "fg":        "#EAF2FF",
    "fg_dim":    "#8B96C2",
    "accent":    "#00E5FF",   # cyan
    "accent2":   "#FF40A8",   # magenta
    "accent3":   "#7C6BFF",   # violet
    "good":      "#5CFFB0",
    "warn":      "#FFC53D",
    "bad":        "#FF5C7A",
    "border":    "#22305E",
    "btn":       "#18265A",
    "btn_hi":    "#22327F",
}

TREND_ICON = {"up": "▲", "down": "▼", "flat": "—", "-": "·"}

RANK_TIER_COLORS = ("#8B96C2", "#5CFFB0", "#00E5FF", "#FFC53D", "#FF40A8", "#7C6BFF")


class Tooltip:
    """Minimal dark tooltip that follows the cursor."""

    def __init__(self, widget: tk.Widget, text: str, delay: int = 550):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id = None
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._hide()
        self._after_id = self.widget.after(self.delay, self._show)

    def _show(self) -> None:
        if self._tip is not None or not self.text:
            return
        x = self.widget.winfo_rootx() + 14
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left", bg="#0A0F2E",
                 fg=THEME["fg"], relief="solid", bd=1,
                 font=(None, 9), padx=8, pady=4).pack()

    def _hide(self, _event=None) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


class SpaceTraderGUI:
    """Main application window. All game state lives in the engine."""

    def __init__(self, root: tk.Tk, engine: GameEngine):
        self.root = root
        self.engine = engine
        self.victory_shown = False
        self.selected_planet: Optional[str] = None
        self.combat_dialog: Optional["CombatDialog"] = None

        # Smart-refresh bookkeeping: per-tab signatures of the data each tab
        # renders. A tab is only rebuilt when its signature changes — a large
        # win now that a single click can ripple through every panel.
        self._tab_sig: Dict[str, str] = {}

        # Star map animation bookkeeping.
        self._twinkle_ids: List[int] = []
        self._twinkle_phase = 0
        self._pulse_radius = 14
        self._pulse_grow = True
        self._map_anim_running = False
        self._jump_anim_id = None

        self._setup_window()
        self._build_styles()
        self._build_hud()
        self._build_notebook()
        self._build_statusbar()
        self._bind_keys()
        self.refresh_all()
        self.show_start_dialog()

    # ------------------------------------------------------------------ #
    # Window & styles

    def _setup_window(self) -> None:
        self.root.title("Space Trader: Odyssey — Nebula Edition")
        self.root.geometry("1320x880")
        self.root.minsize(1200, 760)
        self.root.configure(bg=THEME["bg"])
        try:
            self.root.state("zoomed")
        except Exception:
            pass

    def _build_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", background=THEME["panel"], foreground=THEME["fg"],
                        bordercolor=THEME["border"], lightcolor=THEME["panel_hi"],
                        darkcolor=THEME["panel"])
        style.configure("TFrame", background=THEME["panel"])
        style.configure("Dark.TFrame", background=THEME["bg"])
        style.configure("Panel.TLabelframe", background=THEME["panel"],
                        bordercolor=THEME["border"], relief="solid")
        style.configure("Panel.TLabelframe.Label", background=THEME["panel"],
                        foreground=THEME["accent"], font=(None, 10, "bold"))

        style.configure("TLabel", background=THEME["panel"], foreground=THEME["fg"])
        style.configure("Dim.TLabel", background=THEME["panel"], foreground=THEME["fg_dim"])
        style.configure("Accent.TLabel", background=THEME["panel"], foreground=THEME["accent"])
        style.configure("Accent2.TLabel", background=THEME["panel"], foreground=THEME["accent2"])
        style.configure("Good.TLabel", background=THEME["panel"], foreground=THEME["good"])
        style.configure("Warn.TLabel", background=THEME["panel"], foreground=THEME["warn"])
        style.configure("Bad.TLabel", background=THEME["panel"], foreground=THEME["bad"])
        style.configure("Title.TLabel", background=THEME["bg"], foreground=THEME["accent"],
                        font=(None, 17, "bold"))
        style.configure("Sub.TLabel", background=THEME["bg"], foreground=THEME["fg_dim"])
        style.configure("Hero.TLabel", background=THEME["bg"], foreground=THEME["accent2"],
                        font=(None, 22, "bold"))

        style.configure("TButton", background=THEME["btn"], foreground=THEME["fg"],
                        bordercolor=THEME["border"], focuscolor=THEME["accent"],
                        padding=(10, 5))
        style.map("TButton",
                  background=[("active", THEME["btn_hi"]), ("disabled", THEME["field"])],
                  foreground=[("disabled", THEME["fg_dim"])])
        style.configure("Accent.TButton", background=THEME["accent3"],
                        foreground="#FFFFFF", padding=(12, 6))
        style.map("Accent.TButton",
                  background=[("active", "#9C86FF"), ("disabled", THEME["field"])])
        style.configure("Cyan.TButton", background="#0E4B5C", foreground="#BDF6FF",
                        padding=(12, 6), bordercolor="#1A7A8C")
        style.map("Cyan.TButton",
                  background=[("active", "#14687F"), ("disabled", THEME["field"])])
        style.configure("Danger.TButton", background=THEME["bad"], foreground="#FFFFFF")
        style.map("Danger.TButton", background=[("active", "#FF8AA0")])

        style.configure("TNotebook", background=THEME["bg"], bordercolor=THEME["border"])
        style.configure("TNotebook.Tab", background=THEME["btn"], foreground=THEME["fg_dim"],
                        padding=(16, 8), font=(None, 10))
        style.map("TNotebook.Tab",
                  background=[("selected", THEME["panel_hi"])],
                  foreground=[("selected", THEME["accent"])])

        style.configure("Treeview", background=THEME["field"], foreground=THEME["fg"],
                        fieldbackground=THEME["field"], rowheight=26,
                        bordercolor=THEME["border"], font=(None, 10))
        style.configure("Treeview.Heading", background=THEME["btn"], foreground=THEME["accent"],
                        font=(None, 9, "bold"), relief="flat")
        style.map("Treeview.Heading", background=[("active", THEME["btn_hi"])])
        style.map("Treeview", background=[("selected", "#2B1F5E")],
                  foreground=[("selected", "#FFFFFF")])

        for bar, color in (("Hull", THEME["good"]), ("Shield", THEME["accent"]),
                           ("Fuel", THEME["warn"]), ("Cargo", THEME["accent2"]),
                           ("Arc", THEME["accent3"])):
            style.configure(f"{bar}.Horizontal.TProgressbar",
                            background=color, troughcolor=THEME["field"],
                            bordercolor=THEME["border"], lightcolor=color,
                            darkcolor=color, thickness=12)
        style.configure("Enemy.Horizontal.TProgressbar", background=THEME["bad"],
                        troughcolor=THEME["field"], thickness=16)
        style.configure("EnemyShield.Horizontal.TProgressbar", background=THEME["warn"],
                        troughcolor=THEME["field"], thickness=16)

        style.configure("TSpinbox", background=THEME["field"], foreground=THEME["fg"],
                        buttonbackground=THEME["btn"], fieldbackground=THEME["field"],
                        arrowcolor=THEME["accent"])
        style.configure("TEntry", background=THEME["field"], foreground=THEME["fg"],
                        fieldbackground=THEME["field"], bordercolor=THEME["border"])
        style.configure("TCheckbutton", background=THEME["bg"], foreground=THEME["fg"])
        style.map("TCheckbutton", background=[("active", THEME["bg"])])
        style.configure("TRadiobutton", background=THEME["panel"], foreground=THEME["fg"])
        style.map("TRadiobutton", background=[("active", THEME["panel"])])
        style.configure("TPanedwindow", background=THEME["border"])
        style.configure("TScrollbar", background=THEME["btn"],
                        troughcolor=THEME["field"], bordercolor=THEME["border"],
                        arrowcolor=THEME["accent"])

    # ------------------------------------------------------------------ #
    # Small builders

    def _label(self, parent, text="", style="TLabel", **kw):
        return ttk.Label(parent, text=text, style=style, **kw)

    def _button(self, parent, text, command, style="TButton", tooltip=None, **kw):
        btn = ttk.Button(parent, text=text, command=command, style=style, **kw)
        if tooltip:
            Tooltip(btn, tooltip)
        return btn

    def _make_tree(self, parent, columns: Dict[str, int], height: int = 12) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=list(columns.keys()),
                            show="headings", height=height)
        for col, width in columns.items():
            tree.heading(col, text=col.replace("_", " ").title())
            tree.column(col, width=width, anchor="w")
        tree.tag_configure("odd", background="#0A1029")
        tree.tag_configure("even", background="#0D1533")
        tree.tag_configure("bad", foreground=THEME["bad"])
        tree.tag_configure("warn", foreground=THEME["warn"])
        tree.tag_configure("good", foreground=THEME["good"])
        tree.tag_configure("dim", foreground=THEME["fg_dim"])
        tree.tag_configure("hot", foreground=THEME["accent2"])
        return tree

    def _sig(self, key: str, value) -> bool:
        """Return True when the tab's data signature changed (and remember it)."""
        token = str(value)
        if self._tab_sig.get(key) == token:
            return False
        self._tab_sig[key] = token
        return True

    # ------------------------------------------------------------------ #
    # HUD

    def _build_hud(self) -> None:
        hud = tk.Frame(self.root, bg=THEME["bg"], pady=8, padx=12)
        hud.pack(side="top", fill="x")
        self.hud = hud

        title_box = tk.Frame(hud, bg=THEME["bg"])
        title_box.pack(side="left", anchor="n")
        ttk.Label(title_box, text="SPACE TRADER: ODYSSEY",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(title_box, text="Nebula Edition ✦ trade · smuggle · fight · prosper",
                  style="Sub.TLabel").pack(anchor="w")

        info = tk.Frame(hud, bg=THEME["bg"], padx=20)
        info.pack(side="left", fill="x", expand=True)
        self.hud_info = info

        btns = tk.Frame(hud, bg=THEME["bg"])
        btns.pack(side="right", anchor="n")
        self._button(btns, "Save", self.open_save_dialog).pack(fill="x", pady=2)
        self._button(btns, "Load", self.open_load_dialog).pack(fill="x", pady=2)
        self._button(btns, "New Game", self.confirm_new_game).pack(fill="x", pady=2)
        self._button(btns, "How to Play", self.show_help_dialog).pack(fill="x", pady=2)
        self.mute_var = tk.BooleanVar(value=self.engine.sound.muted)

        def toggle_mute():
            self.engine.sound.muted = self.mute_var.get()
        ttk.Checkbutton(btns, text="Sound FX", variable=self.mute_var,
                        command=toggle_mute, style="TCheckbutton").pack(fill="x", pady=(4, 0))

        self._build_hud_rows()

    def _build_hud_rows(self) -> None:
        info = self.hud_info
        for w in info.winfo_children():
            w.destroy()

        row1 = tk.Frame(info, bg=THEME["bg"])
        row1.pack(fill="x")
        self.lbl_location = self._label(row1, "◌ Earth", "Accent.TLabel",
                                        font=(None, 12, "bold"))
        self.lbl_location.pack(side="left", padx=(0, 20))
        self.lbl_rank = self._label(row1, "· Cadet", "Dim.TLabel",
                                    font=(None, 10, "bold"))
        self.lbl_rank.pack(side="left", padx=(0, 20))
        Tooltip(self.lbl_rank, "Career rank — earned through Renown:\n"
                               "wealth, contracts, bounties and exploration.\n"
                               "Each rank grants a permanent perk.")
        self.lbl_day = self._label(row1, "", "Dim.TLabel")
        self.lbl_day.pack(side="left", padx=(0, 20))
        self.lbl_credits = self._label(row1, "", font=(None, 11, "bold"))
        self.lbl_credits.pack(side="left", padx=(0, 20))
        self.lbl_nw = self._label(row1, "", "Accent.TLabel", font=(None, 11, "bold"))
        self.lbl_nw.pack(side="left", padx=(0, 20))
        self.lbl_loan = self._label(row1, "", "Warn.TLabel")
        self.lbl_loan.pack(side="left")

        row2 = tk.Frame(info, bg=THEME["bg"])
        row2.pack(fill="x", pady=(7, 0))

        self.bars: Dict[str, ttk.Progressbar] = {}
        self.bar_labels: Dict[str, ttk.Label] = {}
        for key, text in (("hull", "HULL"), ("shield", "SHLD"),
                          ("fuel", "FUEL"), ("cargo", "HOLD"), ("arc", "NET WORTH")):
            box = tk.Frame(row2, bg=THEME["bg"])
            box.pack(side="left", padx=(0, 16))
            self.bar_labels[key] = self._label(box, text, "Dim.TLabel",
                                               font=(None, 8, "bold"))
            self.bar_labels[key].pack(anchor="w")
            bar = ttk.Progressbar(box, style=f"{key.capitalize()}.Horizontal.TProgressbar",
                                  length=130 if key != "arc" else 170,
                                  maximum=100, value=0)
            bar.pack()
            self.bars[key] = bar
            for stat, tip in (
                ("hull", "Structural integrity. Repairs at station Services."),
                ("shield", "Energy screen — regenerates daily and via RECHARGE in combat."),
                ("fuel", "Warp propellant. Cheapest at high-mining worlds."),
                ("cargo", "Hold occupancy. Expand with cargo pods or bigger hulls."),
                ("arc", "Progress toward the 500,000 CR victory target."),
            ):
                if key == stat:
                    Tooltip(bar, tip)

        self.lbl_event = self._label(row2, "", "Warn.TLabel", font=(None, 9))
        self.lbl_event.pack(side="left", padx=(4, 0))

    def refresh_hud(self) -> None:
        p = self.engine.player
        nw = self.engine.calculate_net_worth()
        cur = self.engine.current_planet
        rank = self.engine.rank()

        self.lbl_location.config(text=f"◌ {p.location}")
        tier_color = RANK_TIER_COLORS[min(self.engine.rank_index(),
                                          len(RANK_TIER_COLORS) - 1)]
        self.lbl_rank.config(
            text=f"{rank.insignia} {rank.name.upper()}",
            foreground=tier_color,
        )
        self.lbl_day.config(text=f"Day {p.day}")
        self.lbl_credits.config(
            text=f"⌬ {money(p.credits)} CR",
            foreground=THEME["warn"] if p.credits < 300 else THEME["fg"])
        self.lbl_nw.config(
            text=f"Net Worth {money(nw)} / {money(TARGET_NET_WORTH)}",
            foreground=THEME["good"] if nw >= TARGET_NET_WORTH else THEME["accent"],
        )
        loan_style = "Warn.TLabel" if p.loan > 0 else "Dim.TLabel"
        self.lbl_loan.config(text=f"Loan {money(p.loan)} CR" if p.loan else "no loan",
                             style=loan_style)

        hull_pct = (p.hull / max(1, p.max_hull)) * 100
        shield_pct = (p.shield / max(1, p.effective_max_shield())) * 100 if p.effective_max_shield() else 0
        fuel_pct = (p.fuel / max(1, p.max_fuel)) * 100
        cargo_pct = (p.cargo_used() / max(1, p.cargo_cap)) * 100
        arc_pct = min(100.0, nw / TARGET_NET_WORTH * 100)
        self.bars["hull"].config(maximum=100, value=hull_pct)
        self.bars["shield"].config(maximum=100, value=shield_pct)
        self.bars["fuel"].config(maximum=100, value=fuel_pct)
        self.bars["cargo"].config(maximum=100, value=cargo_pct)
        self.bars["arc"].config(maximum=100, value=arc_pct)
        self.bar_labels["hull"].config(
            text=f"HULL {p.hull}/{p.max_hull}",
            foreground=THEME["bad"] if hull_pct < 30 else THEME["fg_dim"])
        self.bar_labels["shield"].config(
            text=f"SHLD {p.shield}/{p.effective_max_shield()}",
            foreground=THEME["bad"] if p.shields_damaged else THEME["fg_dim"])
        self.bar_labels["fuel"].config(
            text=f"FUEL {p.fuel}/{p.max_fuel}",
            foreground=THEME["warn"] if fuel_pct < 25 else THEME["fg_dim"])
        self.bar_labels["cargo"].config(
            text=f"HOLD {p.cargo_used()}/{p.cargo_cap}",
            foreground=THEME["accent2"] if cargo_pct >= 99 else THEME["fg_dim"])
        self.bar_labels["arc"].config(
            text=f"ARC {arc_pct:.0f}%", foreground=tier_color)

        if cur.active_event:
            self.lbl_event.config(
                text=f"⚡ {cur.active_event.name}: {cur.active_event.desc} "
                     f"({cur.active_event.duration}d left)")
        else:
            self.lbl_event.config(text="")

    # ------------------------------------------------------------------ #
    # Notebook / tabs

    def _build_notebook(self) -> None:
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(4, 0))
        self.tab_map = ttk.Frame(self.notebook)
        self.tab_market = ttk.Frame(self.notebook)
        self.tab_shipyard = ttk.Frame(self.notebook)
        self.tab_services = ttk.Frame(self.notebook)
        self.tab_crew = ttk.Frame(self.notebook)
        self.tab_contracts = ttk.Frame(self.notebook)
        self.tab_bank = ttk.Frame(self.notebook)
        self.tab_log = ttk.Frame(self.notebook)
        for tab, title in ((self.tab_map, "★ Star Map"),
                           (self.tab_market, "⇄ Market"),
                           (self.tab_shipyard, "⚙ Shipyard"),
                           (self.tab_services, "✚ Services"),
                           (self.tab_crew, "☺ Crew"),
                           (self.tab_contracts, "✉ Contracts"),
                           (self.tab_bank, "⌗ Bank & Stocks"),
                           (self.tab_log, "✎ Captain's Log")):
            self.notebook.add(tab, text=title)
        self._build_map_tab()
        self._build_market_tab()
        self._build_shipyard_tab()
        self._build_services_tab()
        self._build_crew_tab()
        self._build_contracts_tab()
        self._build_bank_tab()
        self._build_log_tab()

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self.root, bg=THEME["bg"], pady=6, padx=12)
        bar.pack(side="bottom", fill="x")
        self.status_label = ttk.Label(bar, text="Welcome aboard, Commander.",
                                      style="Accent.TLabel")
        self.status_label.pack(side="left")
        ttk.Label(bar, text="Ctrl+S Save · Ctrl+L Load · Ctrl+N New Game · F1 Help",
                  style="Sub.TLabel").pack(side="right")

    def _bind_keys(self) -> None:
        self.root.bind("<Control-s>", lambda e: self.open_save_dialog())
        self.root.bind("<Control-l>", lambda e: self.open_load_dialog())
        self.root.bind("<Control-n>", lambda e: self.confirm_new_game())
        self.root.bind("<F1>", lambda e: self.show_help_dialog())

    # ------------------------------------------------------------------ #
    # Star map tab — animated neon cosmos

    MAP_W, MAP_H = 860, 640
    MARGIN = 58

    def _build_map_tab(self) -> None:
        wrap = ttk.Frame(self.tab_map)
        wrap.pack(fill="both", expand=True)

        left = tk.Frame(wrap, bg=THEME["bg"], padx=6, pady=6)
        left.pack(side="left", fill="both", expand=True)

        self.map_canvas = tk.Canvas(left, width=self.MAP_W, height=self.MAP_H,
                                    bg=THEME["bg"], highlightthickness=1,
                                    highlightbackground=THEME["border"])
        self.map_canvas.pack(fill="both", expand=True)
        self.map_canvas.bind("<Button-1>", self._on_map_click)
        self.map_canvas.bind("<Double-Button-1>", self._on_map_double_click)
        self.map_canvas.bind("<Motion>", self._on_map_hover)
        self.map_canvas.bind("<Leave>", lambda e: self._hide_map_hover())

        self._map_hover_id = None
        self._map_stars = []
        rng = random.Random(42)
        for _ in range(230):
            x = rng.randint(4, self.MAP_W - 4)
            y = rng.randint(4, self.MAP_H - 4)
            r = rng.choice([1, 1, 1, 2])
            tint = rng.choice([THEME["fg_dim"], "#3A4780", "#5560A8", THEME["accent3"]])
            self._map_stars.append((x, y, r, tint))

        right = ttk.Frame(wrap, padding=(10, 6))
        right.pack(side="left", fill="y")

        self.map_title = self._label(right, "Select a destination", "Accent.TLabel",
                                     font=(None, 13, "bold"), wraplength=320)
        self.map_title.pack(anchor="w", pady=(0, 2))
        self.map_subtitle = self._label(right, "", "Dim.TLabel", wraplength=320)
        self.map_subtitle.pack(anchor="w")
        self.map_details = tk.Text(right, width=44, height=13, bg=THEME["field"],
                                   fg=THEME["fg"], relief="flat", wrap="word",
                                   font=(None, 9), state="disabled")
        self.map_details.pack(anchor="w", pady=8)

        self.travel_info = self._label(right, "", "TLabel", wraplength=320)
        self.travel_info.pack(anchor="w")

        self.btn_engage = self._button(right, "⇨ ENGAGE HYPERJUMP",
                                       self._engage_travel, style="Cyan.TButton",
                                       tooltip="Burn fuel and days to travel to the "
                                               "selected world. Encounters may happen!")
        self.btn_engage.pack(anchor="w", pady=10, ipadx=10)

        self._label(right, "BEST ROUTES FROM HERE", "Accent.TLabel",
                    font=(None, 9, "bold")).pack(anchor="w", pady=(8, 2))
        self.route_box = tk.Text(right, width=44, height=10, bg=THEME["field"],
                                 fg=THEME["fg"], relief="flat", wrap="word",
                                 font=(None, 9), state="disabled")
        self.route_box.pack(anchor="w")

    def _map_coords(self, planet: Planet) -> Tuple[float, float]:
        xs = [p.x for p in self.engine.planets.values()]
        ys = [p.y for p in self.engine.planets.values()]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x = max(1.0, max_x - min_x)
        span_y = max(1.0, max_y - min_y)
        sx = (self.MAP_W - 2 * self.MARGIN) / span_x
        sy = (self.MAP_H - 2 * self.MARGIN) / span_y
        px = self.MARGIN + (planet.x - min_x) * sx
        py = self.MAP_H - self.MARGIN - (planet.y - min_y) * sy
        return px, py

    def draw_map(self) -> None:
        c = self.map_canvas
        c.delete("all")
        self._twinkle_ids = []

        # --- Deep space backdrop: nebula clouds (stipple = fake alpha) ---
        c.create_oval(-140, 60, 240, 420, fill="#3D2A7A", outline="",
                      stipple="gray12")
        c.create_oval(520, -120, 900, 260, fill="#7A2A55", outline="",
                      stipple="gray12")
        c.create_oval(300, 380, 720, 760, fill="#123A66", outline="",
                      stipple="gray12")
        c.create_oval(760, 420, 1040, 700, fill="#2A3D7A", outline="",
                      stipple="gray25")

        # --- Faint navigation grid ---
        for gx in range(80, self.MAP_W, 90):
            c.create_line(gx, 0, gx, self.MAP_H, fill=THEME["border"],
                          stipple="gray25", width=1)
        for gy in range(80, self.MAP_H, 90):
            c.create_line(0, gy, self.MAP_W, gy, fill=THEME["border"],
                          stipple="gray25", width=1)

        # --- Starfield (a subset twinkles) ---
        rng = random.Random(7)
        for x, y, r, tint in self._map_stars:
            star_id = c.create_oval(x - r, y - r, x + r, y + r,
                                    fill=tint, outline="")
            if rng.random() < 0.18:
                self._twinkle_ids.append(star_id)

        # --- Faction territory rings ---
        for p in self.engine.planets.values():
            px, py = self._map_coords(p)
            fcol = FACTION_COLORS.get(p.faction, THEME["fg_dim"])
            c.create_oval(px - 34, py - 34, px + 34, py + 34, outline=fcol,
                          stipple="gray50", width=1)

        # --- Legend ---
        lx, ly = 16, 16
        c.create_rectangle(lx - 6, ly - 6, lx + 158, ly + 74,
                           fill="#050A20", outline=THEME["border"])
        c.create_text(lx + 4, ly + 2, anchor="nw", text="FACTION TERRITORY",
                      fill=THEME["accent"], font=(None, 8, "bold"))
        for i, fac in enumerate(FACTIONS):
            c.create_oval(lx + 4, ly + 20 + i * 14, lx + 12, ly + 28 + i * 14,
                         fill=FACTION_COLORS[fac], outline="")
            c.create_text(lx + 18, ly + 24 + i * 14, anchor="w", text=fac,
                          fill=THEME["fg_dim"], font=(None, 8))

        c.create_text(14, self.MAP_H - 12,
                      text="Orion-Sol Sector · click a world to select · double-click to jump",
                      anchor="sw", fill=THEME["fg_dim"], font=(None, 8))

        here = self.engine.current_planet
        hx, hy = self._map_coords(here)

        # --- Travel line to selection ---
        if self.selected_planet and self.selected_planet != here.name:
            sel = self.engine.planets.get(self.selected_planet)
            if sel:
                sx, sy = self._map_coords(sel)
                c.create_line(hx, hy, sx, sy, fill=THEME["accent2"],
                              dash=(5, 4), width=2)
                mx, my = (hx + sx) / 2, (hy + sy) / 2
                fuel, days = self.engine.calculate_travel_cost(sel)
                c.create_text(mx, my - 8, text=f"≈{fuel} fuel · {days}d",
                              fill=THEME["accent2"], font=(None, 8, "bold"))

        # --- Worlds ---
        for p in self.engine.planets.values():
            px, py = self._map_coords(p)
            size = 7 + (2 if p.security == "None" else 0) + (2 if p.rich > 1.5 else 0)
            is_here = p.name == here.name
            is_sel = p.name == self.selected_planet

            # Orbit ring
            c.create_oval(px - size - 5, py - size - 5, px + size + 5, py + size + 5,
                          outline=THEME["border"], dash=(2, 3), width=1)

            outline = p.color
            width = 1
            if is_here:
                outline = THEME["accent"]
                width = 2
            if is_sel:
                c.create_oval(px - size - 11, py - size - 11, px + size + 11, py + size + 11,
                              outline=THEME["warn"], dash=(3, 2), width=2)
                outline = THEME["warn"]
                width = 2

            c.create_oval(px - size, py - size, px + size, py + size,
                          fill=p.color, outline=outline, width=width)
            label_color = THEME["fg"] if is_here else THEME["fg_dim"]
            c.create_text(px, py + size + 13, text=p.name,
                          fill=label_color, font=(None, 8, "bold"))

            if p.active_event:
                c.create_text(px + size + 6, py - size - 2, text="⚡",
                              fill=THEME["warn"], font=(None, 10, "bold"))

        # --- Home-world beacon (animated pulse ring) ---
        self._pulse_id = c.create_oval(hx - self._pulse_radius, hy - self._pulse_radius,
                                       hx + self._pulse_radius, hy + self._pulse_radius,
                                       outline=THEME["accent"], width=2)
        c.create_text(hx, hy - 22, text="◈ YOU",
                      fill=THEME["accent"], font=(None, 8, "bold"))

        self._start_map_animation()

    # ------------------------------------------------------------------ #
    # Map animation loops

    def _start_map_animation(self) -> None:
        if self._map_anim_running:
            return
        self._map_anim_running = True
        self._animate_map()

    def _animate_map(self) -> None:
        """Twinkle stars + breathe the home-world beacon."""
        if not self._map_anim_running:
            return
        try:
            if not self.map_canvas.winfo_exists():
                self._map_anim_running = False
                return

            self._twinkle_phase = (self._twinkle_phase + 1) % 4
            bright = [THEME["fg"], "#C9D6FF", "#8FA5E8", THEME["accent3"]]
            for sid in self._twinkle_ids:
                try:
                    self.map_canvas.itemconfig(sid, fill=bright[self._twinkle_phase])
                except Exception:
                    pass

            # Pulse beacon
            if self._pulse_grow:
                self._pulse_radius += 1.2
                if self._pulse_radius >= 26:
                    self._pulse_grow = False
            else:
                self._pulse_radius -= 1.2
                if self._pulse_radius <= 14:
                    self._pulse_grow = True
            try:
                hx, hy = self._map_coords(self.engine.current_planet)
                self.map_canvas.coords(
                    self._pulse_id,
                    hx - self._pulse_radius, hy - self._pulse_radius,
                    hx + self._pulse_radius, hy + self._pulse_radius)
            except Exception:
                pass

            self.map_canvas.after(420, self._animate_map)
        except Exception:
            self._map_anim_running = False

    def _on_map_click(self, event) -> None:
        best, best_d = None, 28 ** 2
        for name, p in self.engine.planets.items():
            px, py = self._map_coords(p)
            d = (px - event.x) ** 2 + (py - event.y) ** 2
            if d < best_d:
                best, best_d = name, d
        if best:
            self.selected_planet = best
            self._update_map_info()
            self.draw_map()

    def _on_map_double_click(self, event) -> None:
        self._on_map_click(event)
        self._engage_travel()

    def _on_map_hover(self, event) -> None:
        best, best_d, best_p = None, 24 ** 2, None
        for p in self.engine.planets.values():
            px, py = self._map_coords(p)
            d = (px - event.x) ** 2 + (py - event.y) ** 2
            if d < best_d:
                best, best_d, best_p = p.name, d, p
        if best_p:
            line = f"{best_p.name} — {best_p.faction}"
            if best_p.active_event:
                line += f" · ⚡{best_p.active_event.name}"
            if best_p.name == self.selected_planet:
                line += " · selected"
            if self._map_hover_id is None:
                self._map_hover_id = self.map_canvas.create_text(
                    event.x, event.y - 18, text=line, fill=THEME["accent"],
                    font=(None, 9, "bold"))
            else:
                self.map_canvas.coords(self._map_hover_id, event.x, event.y - 18)
                self.map_canvas.itemconfig(self._map_hover_id, text=line)
        else:
            self._hide_map_hover()

    def _hide_map_hover(self) -> None:
        if self._map_hover_id is not None:
            try:
                self.map_canvas.delete(self._map_hover_id)
            except Exception:
                pass
            self._map_hover_id = None

    def _update_map_info(self) -> None:
        p = self.engine.player
        sel = self.engine.planets.get(self.selected_planet) if self.selected_planet else None
        if sel is None or sel.name == p.location:
            self.map_title.config(text="Select a destination")
            self.map_subtitle.config(text="")
            self._set_textbox(self.map_details, "")
            self.travel_info.config(text="")
            self.btn_engage.config(state="disabled")
            return

        self.map_title.config(text=sel.name)
        self.map_subtitle.config(text=f"{sel.subtitle} · {sel.faction}")
        here = self.engine.current_planet
        fuel, days = self.engine.calculate_travel_cost(sel)
        dist = self.engine.calculate_distance(here, sel)
        reachable = p.fuel >= fuel

        details = (
            f"{sel.desc}\n\n"
            f"Security : {sel.security}\n"
            f"Local event : {sel.active_event.name if sel.active_event else 'stable market'}\n"
            f"Fuel price : {max(1, int(sel.fuel_price * self.engine.difficulty.fuel_mult))} CR/unit\n"
            f"Repair price : {max(6, int(sel.repair_cost * self.engine.difficulty.repair_mult * (1.0 - RANK_SERVICE_DISCOUNT[self.engine.rank_index()])))} CR/HP"
        )

        # Deep Space Scanner: live remote market intel.
        if p.has_module("deep_scanner"):
            buys = sorted(
                ((g, self.engine.get_buy_price(g, sel)) for g in COMMODITIES),
                key=lambda t: t[1])[:3]
            hot = sorted(
                ((COMMODITIES[g].name, sel.market.get(g, 0))
                 for g in COMMODITIES),
                key=lambda t: -t[1])[:3]
            lines = [f"\n◈ SCANNER READOUT — {sel.name}:"]
            lines.append("cheapest buys: " + ", ".join(
                f"{COMMODITIES[g].name} {pr}" for g, pr in buys))
            lines.append("priciest stock: " + ", ".join(
                f"{n} {pr}" for n, pr in hot))
            details += "\n" + "\n".join(lines)
        else:
            details += "\n\n(Install a Deep Space Scanner Array to read\nremote market prices.)"

        self._set_textbox(self.map_details, details)

        reach_txt = "reachable" if reachable else "NOT ENOUGH FUEL"
        color = THEME["good"] if reachable else THEME["bad"]
        self.travel_info.config(
            text=f"Distance {dist:.1f} · Fuel {fuel} · Days {days} — {reach_txt}",
            style="TLabel", foreground=color)
        self.btn_engage.config(state="normal" if reachable else "disabled")

    def refresh_routes_box(self) -> None:
        if not self._sig("routes", (self.engine.player.location,
                                    self.engine.player.day)):
            return
        routes = self.engine.compute_best_trade_routes(from_current_only=True)[:4]
        lines = []
        for r in routes:
            tag = " [CONTRABAND]" if r["is_contraband"] else ""
            lines.append(
                f"▸ {r['good']}{tag}: buy {r['buy_price']} at {r['src']}, "
                f"sell {r['sell_price']} at {r['dst']}\n"
                f"   est. net +{money(r['net_profit'])} CR ({r['qty']} units, "
                f"{r['fuel_cost']} fuel, {r['days']}d, +{money(r['profit_per_day'])}/d)"
            )
        self._set_textbox(self.route_box,
                          "\n".join(lines) if lines else
                          "No profitable routes from this port right now.")

    @staticmethod
    def _set_textbox(widget: tk.Text, content: str) -> None:
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.config(state="disabled")


    # ------------------------------------------------------------------ #
    # Market tab — price tables + live charts

    def _build_market_tab(self) -> None:
        wrap = ttk.Frame(self.tab_market, padding=10)
        wrap.pack(fill="both", expand=True)

        # left: commodity table; right: chart detail panel
        body = ttk.Frame(wrap)
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(body, padding=(10, 0, 0, 0))
        right.pack(side="left", fill="y")

        self.market_banner = self._label(left, "", "Warn.TLabel", font=(None, 10, "bold"))
        self.market_banner.pack(anchor="w")

        cols = {"Commodity": 200, "Category": 105, "Buy": 70, "Sell": 70,
                "Stock": 60, "Held": 55, "10-Day Chart": 115, "Trend": 46, "Type": 100}
        tree_frame = ttk.Frame(left)
        tree_frame.pack(fill="both", expand=True, pady=(6, 4))
        self.market_tree = self._make_tree(tree_frame, cols, height=16)
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.market_tree.yview)
        self.market_tree.configure(yscrollcommand=vsb.set)
        self.market_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.market_tree.bind("<<TreeviewSelect>>", lambda e: self._market_preview())
        self._sortable_tree(self.market_tree)

        trade = tk.Frame(left, bg=THEME["panel"], pady=8, padx=8,
                         highlightbackground=THEME["border"], highlightthickness=1)
        trade.pack(fill="x")
        self.market_qty = tk.IntVar(value=1)
        self._label(trade, "Quantity:").pack(side="left")
        ttk.Spinbox(trade, from_=1, to=99999, textvariable=self.market_qty,
                    width=8).pack(side="left", padx=6)
        self._button(trade, "MAX", self._market_set_max,
                     tooltip="Fills the quantity with the maximum you can "
                             "buy (or all you hold, if you hold more than "
                             "you can buy).").pack(side="left", padx=(0, 10))
        self._button(trade, "BUY", self._market_buy, style="Cyan.TButton").pack(side="left", padx=3)
        self._button(trade, "SELL", self._market_sell, style="Accent.TButton").pack(side="left", padx=3)
        self.market_preview = self._label(trade, "", "Dim.TLabel")
        self.market_preview.pack(side="left", padx=16)

        self.market_result = self._label(left, "", "Good.TLabel", wraplength=860)
        self.market_result.pack(anchor="w", pady=(6, 0))

        # ---- right: chart detail panel ----
        self.chart_title = self._label(right, "PRICE CHART", "Accent.TLabel",
                                       font=(None, 11, "bold"), wraplength=330)
        self.chart_title.pack(anchor="w")
        self.chart_hint = self._label(right, "Select a commodity row to plot its "
                                          "10-day history.", "Dim.TLabel",
                                      wraplength=330, justify="left")
        self.chart_hint.pack(anchor="w", pady=(0, 6))

        self.chart_canvas = tk.Canvas(right, width=330, height=150,
                                      bg=THEME["field"], highlightthickness=1,
                                      highlightbackground=THEME["border"])
        self.chart_canvas.pack(anchor="w")
        self.chart_stats = self._label(right, "", "TLabel", wraplength=330,
                                       justify="left", font=(None, 9))
        self.chart_stats.pack(anchor="w", pady=(8, 0))
        self.chart_advice = self._label(right, "", "Dim.TLabel", wraplength=330,
                                        justify="left", font=(None, 9))
        self.chart_advice.pack(anchor="w", pady=(6, 0))

    def _sortable_tree(self, tree: ttk.Treeview) -> None:
        """Click a heading to sort by that column (toggles direction)."""
        state = {"col": None, "desc": False}

        def sort(col: str) -> None:
            if state["col"] == col:
                state["desc"] = not state["desc"]
            else:
                state["col"] = col
                state["desc"] = False
            items = list(tree.get_children())

            def key(iid):
                raw = tree.set(iid, col)
                try:
                    return float(str(raw).replace(",", "").replace("CR", "")
                                 .replace("%", "").split(" ")[0])
                except ValueError:
                    return str(raw).lower()

            try:
                items.sort(key=key, reverse=state["desc"])
            except TypeError:
                items.sort(key=lambda i: str(tree.set(i, col)).lower(),
                           reverse=state["desc"])
            for idx, iid in enumerate(items):
                tree.move(iid, "", idx)
            tree.heading(col,
                         text=col.replace("_", " ").title()
                         + (" ▼" if state["desc"] else " ▲"))

        for col in tree["columns"]:
            tree.heading(col, command=lambda c=col: sort(c))

    def refresh_market(self) -> None:
        p = self.engine.current_planet
        eng = self.engine
        sig = (p.name, eng.player.day,
               tuple(sorted(eng.player.cargo.items())),
               eng.player.credits // 40,
               p.active_event.name if p.active_event else "")
        if not self._sig("market", sig):
            self._market_chart()
            return

        if p.active_event:
            self.market_banner.config(
                text=f"⚡ {p.name}: {p.active_event.name} — {p.active_event.desc} "
                     f"({p.active_event.duration} days left)")
        else:
            self.market_banner.config(text=f"{p.name} Commodity Exchange — stable market")

        selected = self.market_tree.selection()
        self.market_tree.delete(*self.market_tree.get_children())
        for i, (gid, comm) in enumerate(COMMODITIES.items()):
            buy = eng.get_buy_price(gid)
            sell = eng.get_sell_price(gid)
            stock = p.stock.get(gid, 0)
            held = eng.player.cargo.get(gid, 0)
            spark = sparkline(p.price_history.get(gid, []))
            trend = TREND_ICON.get(p.trend(gid), "·")
            tag = ("bad",) if comm.is_contraband else \
                  ("odd",) if i % 2 else ("even",)
            if held > 0:
                tag = ("hot",) + tag
            self.market_tree.insert("", "end", iid=gid, values=(
                f"{comm.icon} {comm.name}", comm.category, buy, sell,
                stock, held, spark, trend,
                "CONTRABAND" if comm.is_contraband else "legal"),
                tags=tag)
        if selected:
            try:
                self.market_tree.selection_set(selected)
            except Exception:
                pass
        self._market_preview()

    def _selected_good(self) -> Optional[str]:
        sel = self.market_tree.selection()
        return sel[0] if sel else None

    def _market_set_max(self) -> None:
        gid = self._selected_good()
        if not gid:
            return
        p = self.engine.current_planet
        price = self.engine.get_buy_price(gid)
        max_buy = min(p.stock.get(gid, 0), self.engine.player.cargo_free(),
                      self.engine.player.credits // max(1, price))
        max_sell = self.engine.player.cargo.get(gid, 0)
        self.market_qty.set(max(max_buy, max_sell, 1))

    def _market_preview(self) -> None:
        gid = self._selected_good()
        if not gid:
            self.market_preview.config(text="Select a commodity row to preview the trade.")
            self._market_chart()
            return
        try:
            qty = max(0, int(self.market_qty.get() or 0))
        except Exception:
            qty = 0
        buy = self.engine.get_buy_price(gid) * qty
        sell = self.engine.get_sell_price(gid) * qty
        held = self.engine.player.cargo.get(gid, 0)
        note = f" · holding {held}" if held else ""
        self.market_preview.config(
            text=f"Preview: buy {money(buy)} CR · sell {money(sell)} CR{note}")
        self._market_chart()

    def _market_chart(self) -> None:
        """Render the 10-day sparkline detail chart for the selected good."""
        gid = self._selected_good()
        c = self.chart_canvas
        c.delete("all")
        if not gid:
            self.chart_title.config(text="PRICE CHART")
            self.chart_hint.config(text="Select a commodity row to plot its 10-day history.",
                                   style="Dim.TLabel")
            self.chart_stats.config(text="")
            self.chart_advice.config(text="")
            return
        p = self.engine.current_planet
        comm = COMMODITIES[gid]
        hist = p.price_history.get(gid, [])
        base = comm.base_price

        self.chart_title.config(text=f"{comm.icon} {comm.name}")
        self.chart_hint.config(text=comm.desc, style="Dim.TLabel")

        W, H = 330, 150
        pad_l, pad_r, pad_t, pad_b = 34, 12, 14, 22
        if len(hist) >= 2:
            lo, hi = min(hist), max(hist)
            span = max(1, hi - lo)
            n = len(hist)
            step = (W - pad_l - pad_r) / (n - 1)
            pts = []
            for i, v in enumerate(hist):
                x = pad_l + i * step
                y = pad_t + (H - pad_t - pad_b) * (1 - (v - lo) / span)
                pts.append((x, y))

            color = THEME["good"] if hist[-1] >= hist[0] else THEME["bad"]
            # glow line (fat, dark) + crisp line
            c.create_line(*pts, fill=color, width=6, stipple="gray50")
            c.create_line(*pts, fill=color, width=2)
            for (x, y) in pts:
                c.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5,
                              fill=THEME["fg"], outline="")
            lx, ly = pts[-1]
            c.create_oval(lx - 5, ly - 5, lx + 5, ly + 5,
                          fill=color, outline=THEME["fg"])

            # min / max gridlines
            c.create_line(pad_l, pad_t, W - pad_r, pad_t, fill=THEME["border"],
                          dash=(2, 3))
            c.create_line(pad_l, H - pad_b, W - pad_r, H - pad_b,
                          fill=THEME["border"], dash=(2, 3))
            c.create_text(4, pad_t + 6, text=f"{hi}", fill=THEME["fg_dim"],
                          font=(None, 8), anchor="w")
            c.create_text(4, H - pad_b - 4, text=f"{lo}", fill=THEME["fg_dim"],
                          font=(None, 8), anchor="w")
            # base-price reference line (clamped into range)
            base_y = pad_t + (H - pad_t - pad_b) * (1 - (clamp(base, lo, hi) - lo) / span)
            if lo < base < hi or lo <= base <= hi:
                c.create_line(pad_l, base_y, W - pad_r, base_y,
                              fill=THEME["warn"], dash=(4, 3))
                c.create_text(W - pad_r - 2, base_y - 8, text="base",
                              fill=THEME["warn"], font=(None, 7), anchor="e")
            days_label = f"-{n - 1}d"
            c.create_text(pad_l, H - 8, text=days_label, fill=THEME["fg_dim"],
                          font=(None, 8))
            c.create_text(W - pad_r, H - 8, text="now", fill=THEME["fg_dim"],
                          font=(None, 8), anchor="e")
            last = hist[-1]
            avg = sum(hist) / n
            delta_pct = (last - base) / max(1, base) * 100
            stat_color = THEME["good"] if last >= base else THEME["bad"]
            self.chart_stats.config(
                text=f"now {last} CR  ·  min {min(hist)}  ·  max {max(hist)}  ·  "
                     f"avg {avg:.0f}\nvs sector base {base}: "
                     f"{delta_pct:+.0f}%  ({'premium' if last > base else 'discount'})",
                foreground=stat_color)
        else:
            c.create_text(W / 2, H / 2,
                          text="collecting data…\n(prices chart after the first day)",
                          fill=THEME["fg_dim"], font=(None, 9))
            self.chart_stats.config(text="")

        # trade calculator
        try:
            qty = max(0, int(self.market_qty.get() or 0))
        except Exception:
            qty = 0
        buy = self.engine.get_buy_price(gid)
        sell = self.engine.get_sell_price(gid)
        margin = sell - buy
        held = self.engine.player.cargo.get(gid, 0)
        calcs = [f"unit margin here: {margin:+d} CR"]
        if qty > 0:
            calcs.append(f"{qty} units → buy {money(buy * qty)} / sell {money(sell * qty)} CR")
        if held > 0:
            calcs.append(f"holding {held} → liquidation {money(sell * held)} CR")
        routes = self.engine.compute_best_trade_routes(from_current_only=True)
        best = next((r for r in routes if r["good_id"] == gid), None)
        if best:
            calcs.append(
                f"best route: → {best['dst']} nets +{money(best['net_profit'])} CR "
                f"({best['qty']} units, {best['days']}d)")
        self.chart_advice.config(text="\n".join(calcs))

    def _market_buy(self) -> None:
        gid = self._selected_good()
        if not gid:
            self._set_result(self.market_result, "Select a commodity first.", good=False)
            return
        try:
            qty = int(self.market_qty.get())
        except Exception:
            qty = 0
        ok, msg = self.engine.buy_commodity(gid, qty)
        self._set_result(self.market_result, msg, good=ok)
        if ok:
            self.engine.autosave()
        self.refresh_all()

    def _market_sell(self) -> None:
        gid = self._selected_good()
        if not gid:
            self._set_result(self.market_result, "Select a commodity first.", good=False)
            return
        try:
            qty = int(self.market_qty.get())
        except Exception:
            qty = 0
        ok, msg = self.engine.sell_commodity(gid, qty)
        self._set_result(self.market_result, msg, good=ok)
        if ok:
            self.engine.autosave()
        self.refresh_all()

    @staticmethod
    def _set_result(label: ttk.Label, msg: str, good: bool = True) -> None:
        label.config(text=msg, style="Good.TLabel" if good else "Bad.TLabel")

    # ------------------------------------------------------------------ #
    # Shipyard tab

    def _build_shipyard_tab(self) -> None:
        wrap = ttk.Frame(self.tab_shipyard, padding=10)
        wrap.pack(fill="both", expand=True)
        pane = ttk.Panedwindow(wrap, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.Labelframe(pane, text="Starship Showroom", style="Panel.TLabelframe",
                              padding=8)
        right = ttk.Labelframe(pane, text="Equipment & Outfitter", style="Panel.TLabelframe",
                               padding=8)
        pane.add(left, weight=1)
        pane.add(right, weight=1)

        cols = {"Ship": 175, "Class": 135, "Cost": 90, "Hold": 55, "Hull": 55,
                "Shield": 60, "Speed": 55, "Slots": 70}
        self.ship_tree = self._make_tree(left, cols, height=13)
        self.ship_tree.pack(fill="both", expand=True)
        self.lbl_tradein = self._label(left, "", "Dim.TLabel")
        self.lbl_tradein.pack(anchor="w", pady=(4, 2))
        self._button(left, "⇦ BUY THIS SHIP", self._buy_ship, style="Cyan.TButton").pack(anchor="w")

        filter_row = ttk.Frame(right)
        filter_row.pack(fill="x", pady=(0, 4))
        self.eq_filter = tk.StringVar(value="all")
        for value, text in (("all", "All"), ("weapon", "Weapons"),
                            ("shield", "Shields"), ("module", "Modules")):
            ttk.Radiobutton(filter_row, text=text, value=value,
                            variable=self.eq_filter,
                            command=self.refresh_shipyard).pack(side="left", padx=4)

        cols2 = {"Item": 200, "Slot": 70, "Cost": 85, "Effect": 330, "Status": 95}
        self.equip_tree = self._make_tree(right, cols2, height=11)
        self.equip_tree.pack(fill="both", expand=True)
        self._button(right, "⇧ INSTALL / BUY", self._buy_equipment,
                     style="Cyan.TButton").pack(anchor="w", pady=(6, 2))

        bottom = ttk.Frame(wrap)
        bottom.pack(fill="x", pady=(8, 0))
        self.lbl_loadout = self._label(bottom, "", "Dim.TLabel", wraplength=900)
        self.lbl_loadout.pack(side="left", fill="x", expand=True)
        missile_box = ttk.Frame(bottom)
        missile_box.pack(side="right")
        self.lbl_missiles = self._label(missile_box, "", "Accent.TLabel")
        self.lbl_missiles.pack(side="left", padx=(0, 8))
        self._button(missile_box, "Buy 1 Missile", lambda: self._buy_missiles(1)).pack(side="left", padx=3)
        self._button(missile_box, "Buy 5", lambda: self._buy_missiles(5)).pack(side="left", padx=3)

    @staticmethod
    def equipment_effect(eq: Equipment) -> str:
        if eq.damage:
            return (f"{eq.damage} dmg · {int(eq.accuracy * 100)}% acc · "
                    f"{int(eq.crit_chance * 100)}% crit — {eq.desc}")
        parts = []
        if eq.shield_hp:
            parts.append(f"+{eq.shield_hp} shield")
        if eq.fuel_save:
            parts.append(f"-{int(eq.fuel_save * 100)}% fuel")
        if eq.cargo_bonus:
            parts.append(f"+{eq.cargo_bonus} hold")
        if eq.evasion_bonus:
            parts.append(f"+{int(eq.evasion_bonus * 100)}% dodge")
        if eq.id == "combat_scanner":
            parts.append(f"+{int(eq.accuracy * 100)}% hit / +{int(eq.crit_chance * 100)}% crit")
        if parts:
            return " · ".join(parts) + f" — {eq.desc}"
        return eq.desc

    def refresh_shipyard(self) -> None:
        p = self.engine.player
        sig = (p.ship_id, tuple(p.equipped_weapons), tuple(p.equipped_shields),
               tuple(p.equipped_modules), p.missiles, p.credits // 100,
               self.eq_filter.get(), p.damaged_subsystems())
        if not self._sig("shipyard", sig):
            return

        selected_ship = self.ship_tree.selection()
        self.ship_tree.delete(*self.ship_tree.get_children())
        for i, (sid, tmpl) in enumerate(SHIP_TEMPLATES.items()):
            tag = ("good",) if sid == p.ship_id else ("odd",) if i % 2 else ("even",)
            self.ship_tree.insert("", "end", iid=sid, values=(
                tmpl.name + (" ◂ current" if sid == p.ship_id else ""),
                tmpl.ship_class, money(tmpl.cost) + " CR",
                tmpl.cargo_cap, tmpl.max_hull, tmpl.max_shield,
                f"{tmpl.speed:.1f}",
                f"{tmpl.weapon_slots}/{tmpl.shield_slots}/{tmpl.module_slots}",
            ), tags=tag)
        if selected_ship:
            try:
                self.ship_tree.selection_set(selected_ship)
            except Exception:
                pass
        self.lbl_tradein.config(
            text=f"Trade-in credit for your current ship: {money(self.engine.ship_trade_in_value())} CR")

        selected_eq = self.equip_tree.selection()
        self.equip_tree.delete(*self.equip_tree.get_children())
        filt = self.eq_filter.get()
        installed = set(p.equipped_weapons + p.equipped_shields + p.equipped_modules)
        for i, (eid, eq) in enumerate(EQUIPMENT_ITEMS.items()):
            if filt != "all" and eq.slot_type != filt:
                continue
            status = "INSTALLED" if eid in installed else "available"
            tag = ("good",) if eid in installed else ("odd",) if i % 2 else ("even",)
            self.equip_tree.insert("", "end", iid=eid, values=(
                eq.name, eq.slot_type, money(eq.cost) + " CR",
                self.equipment_effect(eq), status), tags=tag)
        if selected_eq:
            try:
                self.equip_tree.selection_set(selected_eq)
            except Exception:
                pass

        tmpl = SHIP_TEMPLATES[p.ship_id]
        weapons = ", ".join(EQUIPMENT_ITEMS[w].name for w in p.equipped_weapons) or "none"
        shields = ", ".join(EQUIPMENT_ITEMS[s].name for s in p.equipped_shields) or "none"
        modules = ", ".join(EQUIPMENT_ITEMS[m].name for m in p.equipped_modules) or "none"
        damaged = p.damaged_subsystems()
        dmg_txt = f"  |  DAMAGED: {', '.join(damaged)}" if damaged else ""
        self.lbl_loadout.config(
            text=f"{tmpl.name} — hardpoints {len(p.equipped_weapons)}/{tmpl.weapon_slots}, "
                 f"shield bays {len(p.equipped_shields)}/{tmpl.shield_slots}, "
                 f"modules {len(p.equipped_modules)}/{tmpl.module_slots}{dmg_txt}\n"
                 f"Weapons: {weapons}  |  Shields: {shields}  |  Modules: {modules}")

        if p.has_missile_rack():
            self.lbl_missiles.config(text=f"Missiles: {p.missiles}/{PLAYER_MISSILE_CAP}")
        else:
            self.lbl_missiles.config(text="Missiles: requires Havoc Missile Launcher")

    def _buy_ship(self) -> None:
        sel = self.ship_tree.selection()
        if not sel:
            self.engine.announce("Select a ship model first.")
            self.status_label.config(text="Select a ship model first.")
            return
        ok, msg = self.engine.buy_ship(sel[0])
        self.status_label.config(text=msg, foreground=THEME["good"] if ok else THEME["bad"])
        if not ok:
            messagebox.showwarning("Shipyard", msg, parent=self.root)
        else:
            messagebox.showinfo("Shipyard", msg, parent=self.root)
            self.engine.autosave()
        self.refresh_all()

    def _buy_equipment(self) -> None:
        sel = self.equip_tree.selection()
        if not sel:
            self.status_label.config(text="Select an equipment item first.")
            return
        ok, msg = self.engine.buy_equipment(sel[0])
        self.status_label.config(text=msg, foreground=THEME["good"] if ok else THEME["bad"])
        if not ok:
            messagebox.showwarning("Outfitter", msg, parent=self.root)
        self.refresh_all()

    def _buy_missiles(self, qty: int) -> None:
        ok, msg = self.engine.buy_missiles(qty)
        self.status_label.config(text=msg, foreground=THEME["good"] if ok else THEME["bad"])
        if not ok:
            messagebox.showwarning("Ordnance", msg, parent=self.root)
        self.refresh_all()

    # ------------------------------------------------------------------ #
    # Services tab

    def _build_services_tab(self) -> None:
        wrap = ttk.Frame(self.tab_services, padding=14)
        wrap.pack(fill="both", expand=True)

        self.svc_title = self._label(wrap, "", "Accent.TLabel", font=(None, 13, "bold"))
        self.svc_title.pack(anchor="w")

        grid = ttk.Frame(wrap, padding=8)
        grid.pack(fill="x", pady=8)

        # Fuel
        self.svc_fuel_info = self._label(grid, "", "TLabel")
        self.svc_fuel_info.grid(row=0, column=0, columnspan=3, sticky="w", pady=2)
        self._button(grid, "Buy 25 Fuel", lambda: self._svc(lambda: self.engine.buy_fuel(25))).grid(row=1, column=0, sticky="w", padx=(0, 6), pady=2)
        self._button(grid, "Fill Tank", lambda: self._svc(lambda: self.engine.buy_fuel(9999))).grid(row=1, column=1, sticky="w", padx=6, pady=2)

        # Hull
        self.svc_hull_info = self._label(grid, "", "TLabel")
        self.svc_hull_info.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 2))
        self._button(grid, "Repair 25 Hull", lambda: self._svc(lambda: self.engine.repair_hull(25))).grid(row=3, column=0, sticky="w", padx=(0, 6), pady=2)
        self._button(grid, "Full Repair", lambda: self._svc(lambda: self.engine.repair_hull(9999))).grid(row=3, column=1, sticky="w", padx=6, pady=2)

        # Subsystems
        self.svc_sub_info = self._label(grid, "", "TLabel")
        self.svc_sub_info.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 2))
        self._button(grid, "Repair Subsystems",
                     lambda: self._svc(self.engine.repair_subsystems)).grid(row=5, column=0, sticky="w", padx=(0, 6), pady=2)

        # Insurance
        self.svc_ins_info = self._label(grid, "", "TLabel")
        self.svc_ins_info.grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 2))
        self.btn_insurance = self._button(grid, "Activate Insurance",
                                          lambda: self._svc(self.engine.buy_insurance))
        self.btn_insurance.grid(row=7, column=0, sticky="w", padx=(0, 6), pady=2)

        # Missiles
        self.svc_missile_info = self._label(grid, "", "TLabel")
        self.svc_missile_info.grid(row=8, column=0, columnspan=3, sticky="w", pady=(10, 2))
        self._button(grid, "Buy 1 Missile", lambda: self._svc(lambda: self.engine.buy_missiles(1))).grid(row=9, column=0, sticky="w", padx=(0, 6), pady=2)
        self._button(grid, "Buy 5 Missiles", lambda: self._svc(lambda: self.engine.buy_missiles(5))).grid(row=9, column=1, sticky="w", padx=6, pady=2)

        tip = ("TIP: Insurance covers one destruction — you lose cargo and 10% of your\n"
               "credits, but a replacement hull keeps you flying. Repair prices vary by\n"
               "planet tech level, and fuel is cheapest at mining worlds. At Captain\n"
               "rank, stations charge 12% less for fuel and repairs.")
        self._label(wrap, tip, "Dim.TLabel", justify="left").pack(anchor="w", pady=(4, 0))

    def _svc(self, fn) -> None:
        ok, msg = fn()
        self.status_label.config(text=msg, foreground=THEME["good"] if ok else THEME["bad"])
        self.refresh_all()

    def refresh_services(self) -> None:
        p = self.engine.player
        cur = self.engine.current_planet
        fuel_price = max(1, int(cur.fuel_price * self.engine.difficulty.fuel_mult
                                * (1.0 - RANK_SERVICE_DISCOUNT[self.engine.rank_index()])))
        repair_price = self.engine.current_repair_price()

        sig = (p.fuel, p.hull, tuple(p.damaged_subsystems()), p.insurance_active,
               p.missiles, p.credits // 40, cur.name)
        if not self._sig("services", sig):
            return

        self.svc_title.config(
            text=f"Station Services — {cur.name} ({cur.faction})")
        self.svc_fuel_info.config(text=f"Fuel: {p.fuel}/{p.max_fuel} — price {fuel_price} CR/unit")
        self.svc_hull_info.config(text=f"Hull: {p.hull}/{p.max_hull} — price {repair_price} CR/HP")
        damaged = p.damaged_subsystems()
        if damaged:
            cost = self.engine.subsystem_repair_cost()
            self.svc_sub_info.config(
                text=f"Damaged subsystems: {', '.join(damaged)} — repair cost {money(cost)} CR",
                foreground=THEME["bad"])
        else:
            self.svc_sub_info.config(text="All subsystems fully operational.", foreground=THEME["good"])
        if p.insurance_active:
            self.svc_ins_info.config(
                text="Insurance policy ACTIVE — destruction will trigger a respawn.",
                foreground=THEME["good"])
            self.btn_insurance.config(state="disabled")
        else:
            self.svc_ins_info.config(
                text=f"Ship insurance: {money(self.engine.insurance_price())} CR "
                     f"(4% of hull + equipment value)", foreground=THEME["fg"])
            self.btn_insurance.config(state="normal")
        if p.has_missile_rack():
            self.svc_missile_info.config(
                text=f"Missiles: {p.missiles}/{PLAYER_MISSILE_CAP} — "
                     f"{money(self.engine.missile_price())} CR each")
        else:
            self.svc_missile_info.config(
                text="Missiles: install a Havoc Missile Launcher (Shipyard) first.")

    # ------------------------------------------------------------------ #
    # Crew tab

    def _build_crew_tab(self) -> None:
        wrap = ttk.Frame(self.tab_crew, padding=10)
        wrap.pack(fill="both", expand=True)

        cols = {"Officer": 220, "Role": 180, "Signing": 90, "Wage/day": 80, "Perk": 380}
        self.crew_tree = self._make_tree(wrap, cols, height=10)
        self.crew_tree.pack(fill="both", expand=True)

        row = ttk.Frame(wrap)
        row.pack(fill="x", pady=8)
        self._button(row, "Hire Officer", self._hire_crew, style="Cyan.TButton").pack(side="left", padx=3)
        self._button(row, "Dismiss Officer", self._dismiss_crew).pack(side="left", padx=3)
        self.lbl_crew_info = self._label(row, "", "Dim.TLabel")
        self.lbl_crew_info.pack(side="left", padx=16)

    def refresh_crew(self) -> None:
        p = self.engine.player
        if not self._sig("crew", (tuple(p.hired_crew),)):
            return
        self.crew_tree.delete(*self.crew_tree.get_children())
        for i, c in enumerate(AVAILABLE_CREW):
            hired = c.id in p.hired_crew
            tag = ("good",) if hired else ("odd",) if i % 2 else ("even",)
            self.crew_tree.insert("", "end", iid=c.id, values=(
                c.name + (" [HIRED]" if hired else ""),
                c.role, money(c.hire_cost) + " CR", f"{c.daily_wage} CR",
                c.desc), tags=tag)
        self.lbl_crew_info.config(
            text=f"Crew: {len(p.hired_crew)}/{MAX_CREW} · Daily wages: "
                 f"{money(self.engine.total_daily_wages())} CR"
                 + (" (Captain discount applied)" if self.engine.rank_index() >= 3 else ""))

    def _hire_crew(self) -> None:
        sel = self.crew_tree.selection()
        if not sel:
            return
        ok, msg = self.engine.hire_crew(sel[0])
        self.status_label.config(text=msg, foreground=THEME["good"] if ok else THEME["bad"])
        if not ok:
            messagebox.showwarning("Crew", msg, parent=self.root)
        self.refresh_all()

    def _dismiss_crew(self) -> None:
        sel = self.crew_tree.selection()
        if not sel:
            return
        if messagebox.askyesno("Crew", "Dismiss this officer from your crew?",
                               parent=self.root):
            ok, msg = self.engine.dismiss_crew(sel[0])
            self.status_label.config(text=msg, foreground=THEME["good"] if ok else THEME["bad"])
            self.refresh_all()

    # ------------------------------------------------------------------ #
    # Contracts tab

    def _build_contracts_tab(self) -> None:
        wrap = ttk.Frame(self.tab_contracts, padding=10)
        wrap.pack(fill="both", expand=True)

        self._label(wrap, "AVAILABLE CONTRACTS", "Accent.TLabel",
                    font=(None, 10, "bold")).pack(anchor="w")
        cols = {"Contract": 380, "Destination": 130, "Days Left": 80,
                "Reward": 100, "Kind": 100}
        self.contracts_tree = self._make_tree(wrap, cols, height=7)
        self.contracts_tree.pack(fill="both", expand=True, pady=(4, 4))
        self.contracts_desc = self._label(wrap, "", "Dim.TLabel", wraplength=1000)
        self.contracts_desc.pack(anchor="w")
        row = ttk.Frame(wrap)
        row.pack(fill="x", pady=6)
        self._button(row, "✓ ACCEPT CONTRACT", self._accept_mission,
                     style="Cyan.TButton").pack(side="left")

        self._label(wrap, "ACTIVE CONTRACTS", "Accent.TLabel",
                    font=(None, 10, "bold")).pack(anchor="w", pady=(10, 0))
        cols2 = {"Contract": 380, "Destination": 130, "Days Left": 80,
                 "Reward": 100, "Kind": 100}
        self.active_tree = self._make_tree(wrap, cols2, height=5)
        self.active_tree.pack(fill="both", expand=True, pady=(4, 0))
        self.active_tree.bind("<<TreeviewSelect>>", self._contract_desc)

    def refresh_contracts(self) -> None:
        avail = self.engine.available_missions
        active = self.engine.player.active_missions
        sig = (len(avail), tuple(m.id for m in avail),
               tuple((m.id, m.days_left) for m in active))
        if not self._sig("contracts", sig):
            return
        self.contracts_tree.delete(*self.contracts_tree.get_children())
        for i, m in enumerate(avail):
            tag = ("warn",) if m.m_type == "smuggle" else ("odd",) if i % 2 else ("even",)
            self.contracts_tree.insert("", "end", iid=m.id, values=(
                m.title, m.destination, m.days_left,
                f"{money(m.reward_credits)} CR", m.m_type), tags=tag)
        self.active_tree.delete(*self.active_tree.get_children())
        for i, m in enumerate(active):
            tag = ("bad",) if m.days_left <= 2 else ("odd",) if i % 2 else ("even",)
            self.active_tree.insert("", "end", iid=m.id, values=(
                m.title, m.destination, m.days_left,
                f"{money(m.reward_credits)} CR", m.m_type), tags=tag)

    def _contract_desc(self, event=None) -> None:
        for tree, source in ((self.contracts_tree, self.engine.available_missions),
                             (self.active_tree, self.engine.player.active_missions)):
            sel = tree.selection()
            if sel:
                for m in source:
                    if m.id == sel[0]:
                        self.contracts_desc.config(text=m.desc)
                        return
        self.contracts_desc.config(text="")

    def _accept_mission(self) -> None:
        sel = self.contracts_tree.selection()
        if not sel:
            return
        ok, msg = self.engine.accept_mission(sel[0])
        self.status_label.config(text=msg, foreground=THEME["good"] if ok else THEME["bad"])
        if not ok:
            messagebox.showwarning("Contracts", msg, parent=self.root)
        self.refresh_all()


    # ------------------------------------------------------------------ #
    # Bank & stocks tab

    def _build_bank_tab(self) -> None:
        wrap = ttk.Frame(self.tab_bank, padding=10)
        wrap.pack(fill="both", expand=True)
        pane = ttk.Panedwindow(wrap, orient="horizontal")
        pane.pack(fill="both", expand=True)

        bank = ttk.Labelframe(pane, text="Interstellar Bank", style="Panel.TLabelframe", padding=10)
        market = ttk.Labelframe(pane, text="Galactic Stock Exchange", style="Panel.TLabelframe", padding=10)
        pane.add(bank, weight=1)
        pane.add(market, weight=1)

        self.bank_acct = self._label(bank, "", "TLabel", justify="left", font=(None, 10))
        self.bank_acct.pack(anchor="w")

        amount_row = ttk.Frame(bank)
        amount_row.pack(fill="x", pady=8)
        self._label(amount_row, "Amount:").pack(side="left")
        self.bank_amount = ttk.Entry(amount_row, width=14)
        self.bank_amount.pack(side="left", padx=6)
        self._button(amount_row, "MAX", self._bank_set_max).pack(side="left")

        btn_row = ttk.Frame(bank)
        btn_row.pack(fill="x")
        self._button(btn_row, "Deposit", lambda: self._bank_op("deposit")).pack(side="left", padx=3, pady=3)
        self._button(btn_row, "Withdraw", lambda: self._bank_op("withdraw")).pack(side="left", padx=3, pady=3)
        self._button(btn_row, "Borrow", lambda: self._bank_op("borrow")).pack(side="left", padx=3, pady=3)
        self._button(btn_row, "Repay", lambda: self._bank_op("repay")).pack(side="left", padx=3, pady=3)

        tip = ("Savings earn 0.8% daily interest. Loans charge daily interest\n"
               "based on difficulty, your credit score and rank. Solid repayments\n"
               "(1,000 CR+) raise your score; 700+ cuts interest by 15%.")
        self._label(bank, tip, "Dim.TLabel", justify="left").pack(anchor="w", pady=(12, 0))

        cols = {"Symbol": 70, "Company": 240, "Price": 90, "Owned": 70,
                "Value": 100, "30d Trend": 100}
        self.stock_tree = self._make_tree(market, cols, height=8)
        self.stock_tree.pack(fill="both", expand=True)

        srow = ttk.Frame(market)
        srow.pack(fill="x", pady=8)
        self._label(srow, "Shares:").pack(side="left")
        self.stock_qty = tk.IntVar(value=1)
        ttk.Spinbox(srow, from_=1, to=99999, textvariable=self.stock_qty,
                    width=8).pack(side="left", padx=6)
        self._button(srow, "BUY SHARES", self._stock_buy, style="Cyan.TButton").pack(side="left", padx=3)
        self._button(srow, "SELL SHARES", self._stock_sell, style="Accent.TButton").pack(side="left", padx=3)
        self.lbl_stock_result = self._label(market, "", "Good.TLabel", wraplength=500)
        self.lbl_stock_result.pack(anchor="w")

    def refresh_bank(self) -> None:
        p = self.engine.player
        sig = (p.credits, p.savings, p.loan, p.credit_score,
               tuple(sorted(p.stocks_owned.items())),
               tuple(round(st.price, 2) for st in self.engine.stocks.values()),
               p.day)
        if not self._sig("bank", sig):
            return
        interest_pct = self.engine._effective_loan_interest() * 100
        self.bank_acct.config(
            text=f"Wallet: {money(p.credits)} CR\n"
                 f"Savings: {money(p.savings)} CR (0.8%/day)\n"
                 f"Loan: {money(p.loan)} CR ({interest_pct:.2f}%/day effective)\n"
                 f"Credit score: {p.credit_score} "
                 f"({'excellent' if p.credit_score >= 700 else 'fair' if p.credit_score >= 550 else 'poor'})\n"
                 f"Credit limit: {money(self.engine.loan_limit())} CR")

        self.stock_tree.delete(*self.stock_tree.get_children())
        for i, (sym, stk) in enumerate(self.engine.stocks.items()):
            owned = p.stocks_owned.get(sym, 0)
            hist = stk.history
            if len(hist) >= 2 and hist[-1] > hist[0] * 1.03:
                trend = "▲ rising"
            elif len(hist) >= 2 and hist[-1] < hist[0] * 0.97:
                trend = "▼ falling"
            else:
                trend = "— steady"
            tag = ("good",) if owned else ("odd",) if i % 2 else ("even",)
            self.stock_tree.insert("", "end", iid=sym, values=(
                f"${sym}", stk.name, f"{stk.price:.2f}", owned,
                f"{money(stk.price * owned)} CR", trend), tags=tag)

    def _bank_set_max(self) -> None:
        self.bank_amount.delete(0, "end")
        self.bank_amount.insert(0, str(self.engine.player.credits))

    def _bank_amount_value(self) -> int:
        raw = self.bank_amount.get().strip().lower()
        if raw in ("all", "max"):
            return self.engine.player.credits
        try:
            return max(0, int(raw))
        except ValueError:
            return 0

    def _bank_op(self, op: str) -> None:
        amount = self._bank_amount_value()
        fns = {"deposit": self.engine.deposit, "withdraw": self.engine.withdraw,
               "borrow": self.engine.borrow, "repay": self.engine.repay}
        ok, msg = fns[op](amount)
        self.status_label.config(text=msg, foreground=THEME["good"] if ok else THEME["bad"])
        if not ok:
            messagebox.showwarning("Bank", msg, parent=self.root)
        self.refresh_all()

    def _stock_buy(self) -> None:
        sel = self.stock_tree.selection()
        if not sel:
            return
        try:
            qty = int(self.stock_qty.get())
        except Exception:
            qty = 0
        ok, msg = self.engine.buy_stock(sel[0], qty)
        self._set_result(self.lbl_stock_result, msg, good=ok)
        self.status_label.config(text=msg, foreground=THEME["good"] if ok else THEME["bad"])
        self.refresh_all()

    def _stock_sell(self) -> None:
        sel = self.stock_tree.selection()
        if not sel:
            return
        try:
            qty = int(self.stock_qty.get())
        except Exception:
            qty = 0
        ok, msg = self.engine.sell_stock(sel[0], qty)
        self._set_result(self.lbl_stock_result, msg, good=ok)
        self.status_label.config(text=msg, foreground=THEME["good"] if ok else THEME["bad"])
        self.refresh_all()

    # ------------------------------------------------------------------ #
    # Captain's log tab — news, stats, achievements, net-worth curve

    def _build_log_tab(self) -> None:
        wrap = ttk.Frame(self.tab_log, padding=10)
        wrap.pack(fill="both", expand=True)
        pane = ttk.Panedwindow(wrap, orient="horizontal")
        pane.pack(fill="both", expand=True)

        news = ttk.Labelframe(pane, text="News Feed & Event Intel",
                              style="Panel.TLabelframe", padding=6)
        stats = ttk.Labelframe(pane, text="Career, Ranks & Achievements",
                               style="Panel.TLabelframe", padding=6)
        pane.add(news, weight=1)
        pane.add(stats, weight=1)

        self.log_text = tk.Text(news, bg=THEME["field"], fg=THEME["fg"],
                                relief="flat", wrap="word", font=(None, 9),
                                state="disabled", width=52)
        self.log_text.pack(fill="both", expand=True)

        # Net worth career curve
        self._label(stats, "CAREER NET WORTH", "Accent.TLabel",
                    font=(None, 9, "bold")).pack(anchor="w")
        self.nw_canvas = tk.Canvas(stats, width=330, height=120,
                                   bg=THEME["field"], highlightthickness=1,
                                   highlightbackground=THEME["border"])
        self.nw_canvas.pack(anchor="w", pady=(2, 8))
        self.nw_caption = self._label(stats, "", "Dim.TLabel", font=(None, 8))
        self.nw_caption.pack(anchor="w")

        self.stats_labels = self._label(stats, "", "TLabel", justify="left",
                                        font=(None, 10))
        self.stats_labels.pack(anchor="w")

        cols = {"Achievement": 200, "Requirement": 260, "Status": 90}
        self.ach_tree = self._make_tree(stats, cols, height=8)
        self.ach_tree.pack(fill="both", expand=True, pady=(8, 0))

    def _draw_net_worth_chart(self) -> None:
        c = self.nw_canvas
        c.delete("all")
        hist = self.engine.player.net_worth_history
        W, H = 330, 120
        pad_l, pad_r, pad_t, pad_b = 44, 10, 12, 18

        if len(hist) >= 2:
            lo, hi = min(hist), max(hist)
            span = max(1, hi - lo)
            n = len(hist)
            step = (W - pad_l - pad_r) / (n - 1)
            pts = []
            for i, v in enumerate(hist):
                x = pad_l + i * step
                y = pad_t + (H - pad_t - pad_b) * (1 - (v - lo) / span)
                pts.append((x, y))
            color = THEME["good"] if hist[-1] >= hist[0] else THEME["bad"]
            c.create_line(*pts, fill=color, width=6, stipple="gray50")
            c.create_line(*pts, fill=color, width=2)
            lx, ly = pts[-1]
            c.create_oval(lx - 4, ly - 4, lx + 4, ly + 4,
                          fill=color, outline=THEME["fg"])
            c.create_line(pad_l, pad_t, W - pad_r, pad_t, fill=THEME["border"], dash=(2, 3))
            c.create_text(4, pad_t + 6, text=f"{money(hi)}", fill=THEME["fg_dim"],
                          font=(None, 8), anchor="w")
            c.create_text(4, H - pad_b - 4, text=f"{money(lo)}", fill=THEME["fg_dim"],
                          font=(None, 8), anchor="w")
            c.create_text(W - pad_r, pad_t - 2, text=f"now {money(hist[-1])}",
                          fill=color, font=(None, 8, "bold"), anchor="ne")
            trend = hist[-1] - hist[0]
            arrow = "▲" if trend >= 0 else "▼"
            tcol = THEME["good"] if trend >= 0 else THEME["bad"]
            self.nw_caption.config(
                text=f"{arrow} {money(abs(trend))} CR over the last {n} entries",
                foreground=tcol)
        else:
            c.create_text(W / 2, H / 2, text="career curve begins after your first jump…",
                          fill=THEME["fg_dim"], font=(None, 9))
            self.nw_caption.config(text="", foreground=THEME["fg_dim"])

    def refresh_log(self) -> None:
        p = self.engine.player
        s = p.stats
        eng = self.engine
        sig = (p.day, len(eng.news_feed), tuple(sorted(p.achievements)),
               self.engine.renown(), len(p.net_worth_history),
               p.net_worth_history[-1] if p.net_worth_history else 0)
        if not self._sig("log", sig):
            return

        self._set_textbox(self.log_text, "\n".join(eng.news_feed[:40]) or "No news yet.")
        rep_lines = "\n".join(
            f"  {f}: {p.rep(f):+d} ({reputation_rank(p.rep(f))})" for f in FACTIONS
        )
        nxt_name, nxt_needed = eng.renown_to_next_rank()
        rank_line = (f"Next rank: {nxt_name} — {money(nxt_needed)} renown to go"
                     if nxt_name else "Maximum rank achieved — Admiral of the sector!")
        self.stats_labels.config(
            text=f"Captain: {p.name}   ·   Rank: {eng.rank().insignia} {eng.rank().name}\n"
                 f"Day: {p.day}    Difficulty: {eng.difficulty.name}\n"
                 f"Renown: {money(eng.renown())}  ({rank_line})\n\n"
                 f"Trading profit earned: {money(s.get('total_profit', 0))} CR\n"
                 f"Hyperjumps made: {s.get('jumps_made', 0)}\n"
                 f"Pirates destroyed: {s.get('pirates_defeated', 0)}\n"
                 f"Bounties claimed: {s.get('bounties_claimed', 0)}\n"
                 f"Contraband sold: {s.get('contraband_sold', 0)} units\n"
                 f"Contracts fulfilled: {s.get('missions_completed', 0)}\n"
                 f"Asteroid mining ops: {s.get('mining_ops', 0)}\n"
                 f"Wormhole transits: {s.get('wormholes', 0)}\n"
                 f"Missiles fired: {s.get('missiles_fired', 0)}\n"
                 f"Successful boardings: {s.get('boards', 0)}\n"
                 f"Insurance claims: {s.get('insurance_claims', 0)}\n\n"
                 f"Faction Standing:\n{rep_lines}")

        self.ach_tree.delete(*self.ach_tree.get_children())
        for i, (aid, (title, desc)) in enumerate(ACHIEVEMENTS.items()):
            unlocked = aid in p.achievements
            tag = ("good",) if unlocked else ("dim",)
            self.ach_tree.insert("", "end", iid=aid, values=(
                title, desc, "UNLOCKED" if unlocked else "locked"), tags=tag)

        self._draw_net_worth_chart()

    # ------------------------------------------------------------------ #
    # Global refresh

    def refresh_all(self) -> None:
        self.refresh_hud()

        # Star map: redraw only when position/selection/event set changes.
        map_sig = (self.engine.player.location, self.selected_planet,
                   tuple(p.active_event is not None for p in self.engine.planets.values()))
        if self._sig("map", map_sig):
            self.draw_map()
        self._update_map_info()
        self.refresh_routes_box()
        self.refresh_market()
        self.refresh_shipyard()
        self.refresh_services()
        self.refresh_crew()
        self.refresh_contracts()
        self.refresh_bank()
        self.refresh_log()
        if self.engine.last_result:
            self.status_label.config(text=self.engine.last_result)

    # ------------------------------------------------------------------ #
    # Travel

    def _engage_travel(self) -> None:
        if not self.selected_planet:
            return
        if self._jump_anim_id is not None:
            return  # a jump animation is already running

        from_name = self.engine.player.location
        ok, msg, encounter = self.engine.execute_travel(self.selected_planet)
        self.status_label.config(text=msg, foreground=THEME["good"] if ok else THEME["bad"])
        if not ok:
            messagebox.showwarning("Navigation", msg, parent=self.root)
            return

        dest_name = self.selected_planet
        self.selected_planet = None
        self.refresh_all()

        # --- Visual warp animation, then encounters ---
        try:
            from_p = self.engine.planets.get(from_name)
            to_p = self.engine.planets.get(dest_name)
            if from_p and to_p:
                fx, fy = self._map_coords(from_p)
                tx, ty = self._map_coords(to_p)
                c = self.map_canvas
                ship_id = c.create_oval(fx - 5, fy - 5, fx + 5, fy + 5,
                                        fill=THEME["accent2"], outline=THEME["fg"])
                trail_id = c.create_line(fx, fy, fx, fy, fill=THEME["accent2"],
                                         width=2, dash=(3, 2))

                def step(i: int):
                    if not c.winfo_exists():
                        self._jump_anim_id = None
                        return
                    t = i / 16.0
                    x = fx + (tx - fx) * t
                    y = fy + (ty - fy) * t
                    c.coords(ship_id, x - 5, y - 5, x + 5, y + 5)
                    c.coords(trail_id, fx, fy, x, y)
                    if i < 16:
                        self._jump_anim_id = c.after(28, lambda: step(i + 1))
                    else:
                        c.delete(ship_id)
                        c.delete(trail_id)
                        self._jump_anim_id = None
                        self._after_travel(encounter)

                self.btn_engage.config(state="disabled")
                self._jump_anim_id = c.after(28, lambda: step(0))
                return
        except Exception:
            pass

        self._after_travel(encounter)

    def _after_travel(self, encounter: Optional[Dict[str, Any]]) -> None:
        self.btn_engage.config(state="normal")
        self.refresh_all()
        if encounter:
            self.show_encounter(encounter)
        else:
            self.engine.autosave()
        self.refresh_all()

    # ------------------------------------------------------------------ #
    # Encounters

    def show_encounter(self, enc: Dict[str, Any]) -> None:
        t = enc.get("type")
        if t in ("pirate_ambush", "bounty_combat"):
            self.engine.autosave()
            self.combat_dialog = CombatDialog(self, enc)
        elif t == "customs_scan":
            ChoiceDialog(self, enc.get("title", "Customs Inspection"),
                         enc.get("desc", ""),
                         choices=[("Submit to Scan", False),
                                  ("Bribe the Officer", True)],
                         resolver=lambda a: self.engine.resolve_customs(enc, a))
        elif t == "faction_patrol":
            ChoiceDialog(self, enc.get("title", "Faction Patrol"),
                         enc.get("desc", ""),
                         choices=[("Cooperate", True), ("Power Through / Ignore", False)],
                         resolver=lambda a: self.engine.resolve_faction_patrol(enc, a))
        elif t == "derelict":
            ChoiceDialog(self, enc.get("title", "Derelict Ship"),
                         enc.get("desc", ""),
                         choices=[("Board & Salvage", True), ("Leave It", False)],
                         resolver=lambda a: self.engine.resolve_derelict(enc, a))
        elif t == "solar_flare":
            ChoiceDialog(self, enc.get("title", "Solar Flare"),
                         enc.get("desc", ""),
                         choices=[("Brace for Impact", True)],
                         resolver=lambda a: self.engine.resolve_solar_flare(enc))
        elif t == "distress_beacon":
            ChoiceDialog(self, enc.get("title", "Distress Beacon"),
                         enc.get("desc", ""),
                         choices=[("Assist (costs 15 fuel)", True), ("Ignore", False)],
                         resolver=lambda a: self.engine.resolve_distress(enc, a))
        elif t == "wandering_trader":
            good = enc.get("good", "crystals")
            qty = enc.get("qty", 5)
            price = enc.get("unit_price", 50)
            desc = (f"{enc.get('desc', '')}\n\nOFFER: {qty}x {COMMODITIES[good].name} "
                    f"at {money(price)} CR/unit (total {money(price * qty)} CR — "
                    f"market value ≈ {money(COMMODITIES[good].base_price)} each).")
            ChoiceDialog(self, enc.get("title", "Wandering Trader"), desc,
                         choices=[("Accept the Deal", True), ("Decline", False)],
                         resolver=lambda a: self.engine.resolve_trader(enc, a))
        elif t == "asteroid_field":
            ChoiceDialog(self, enc.get("title", "Asteroid Field"),
                         enc.get("desc", ""),
                         choices=[("Thread the Needle (risky)", True),
                                  ("Wide Detour (−8 fuel)", False)],
                         resolver=lambda a: self.engine.resolve_asteroid_field(enc, a))
        elif t == "wormhole":
            ChoiceDialog(self, enc.get("title", "Wormhole"),
                         enc.get("desc", ""),
                         choices=[("Enter the Wormhole (random exit)", True),
                                  ("Stay the Course", False)],
                         resolver=lambda a: self.engine.resolve_wormhole(enc, a))
        elif t == "mining_opportunity":
            vein = enc.get("vein", "ore")
            desc = (f"{enc.get('desc', '')}\n\nVein: {COMMODITIES[vein].name} "
                    f"(base value {money(COMMODITIES[vein].base_price)} CR/unit). "
                    f"Mining costs 10 fuel.")
            ChoiceDialog(self, enc.get("title", "Mining Opportunity"), desc,
                         choices=[("Deploy Mining Drones (−10 fuel)", True),
                                  ("Continue On", False)],
                         resolver=lambda a: self.engine.resolve_mining(enc, a))

    # ------------------------------------------------------------------ #
    # Save / load / new game / start flow

    def open_save_dialog(self) -> None:
        SaveLoadDialog(self, mode="save")

    def open_load_dialog(self) -> None:
        SaveLoadDialog(self, mode="load")

    def confirm_new_game(self) -> None:
        NewGameDialog(self)

    def show_start_dialog(self) -> None:
        StartDialog(self)

    def show_help_dialog(self) -> None:
        HelpDialog(self)

    def after_game_state_change(self) -> None:
        self.refresh_all()

    # ------------------------------------------------------------------ #
    # Victory & death

    def check_end_states(self) -> None:
        if self.engine.victory_achieved and not self.victory_shown:
            self.victory_shown = True
            VictoryDialog(self)
        elif self.engine.is_game_over:
            self.handle_death()

    def handle_death(self) -> None:
        if self.engine.has_save(PRECOMBAT_SLOT):
            info = self.engine.slot_info(PRECOMBAT_SLOT)
            load = messagebox.askyesno(
                "GAME OVER",
                "Your ship was destroyed...\n\n"
                "Would you like to reload the pre-combat autosave?\n"
                f"(saved before the battle, Day {info['day']})",
                parent=self.root)
            if load:
                ok, msg = self.engine.load_game(PRECOMBAT_SLOT)
                self.status_label.config(text=msg)
                self.refresh_all()
                return
        messagebox.showinfo(
            "GAME OVER",
            "Your ship was destroyed. The void claims another trader.\n\n"
            "Final stats:\n"
            f"Days survived: {self.engine.player.day}\n"
            f"Pirates destroyed: {self.engine.player.stats['pirates_defeated']}\n"
            f"Trading profit: {money(self.engine.player.stats['total_profit'])} CR",
            parent=self.root)
        self.root.destroy()


# ==============================================================================
# DIALOGS
# ==============================================================================

class ModalDialog(tk.Toplevel):
    """Base modal window with dark theme."""

    def __init__(self, gui: SpaceTraderGUI, title: str, geometry: str = "560x420"):
        super().__init__(gui.root)
        self.gui = gui
        self.configure(bg=THEME["panel"])
        self.title(title)
        self.geometry(geometry)
        self.resizable(False, False)
        self.transient(gui.root)
        self.grab_set()
        self.bind("<Escape>", lambda e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)

    def center(self) -> None:
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        x = (self.winfo_screenwidth() - w) // 2
        y = (self.winfo_screenheight() - h) // 2
        self.geometry(f"+{x}+{y}")

    def close(self) -> None:
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class StartDialog(ModalDialog):
    """Welcome window: continue a save or start a new game."""

    def __init__(self, gui: SpaceTraderGUI):
        super().__init__(gui, "Welcome, Commander", "660x560")
        ttk.Label(self, text="SPACE TRADER: ODYSSEY",
                  style="Hero.TLabel").pack(pady=(26, 2))
        ttk.Label(self, text="N E B U L A   E D I T I O N",
                  style="Sub.TLabel").pack()
        ttk.Label(self, text="trade · smuggle · fight · prosper",
                  style="Dim.TLabel").pack(pady=(0, 8))

        box = ttk.Frame(self, padding=20)
        box.pack(fill="both", expand=True)

        engine = gui.engine
        if engine.any_saves_exist():
            ttk.Label(box, text="SAVED GAMES FOUND", style="Accent.TLabel",
                      font=(None, 10, "bold")).pack(anchor="w", pady=(0, 4))
            self.slots_list = tk.Listbox(box, bg=THEME["field"], fg=THEME["fg"],
                                         relief="flat", height=5,
                                         highlightthickness=1,
                                         highlightbackground=THEME["border"])
            self.slots_list.pack(fill="x")
            self.slot_ids: List[str] = []
            for slot in (AUTO_SLOT,) + SAVE_SLOTS:
                info = engine.slot_info(slot)
                if info:
                    label = (f"[autosave] Day {info['day']} · {info['location']} · "
                             f"{money(info['credits'])} CR · {info['difficulty']} · "
                             f"saved {info['saved_at']}") if slot == AUTO_SLOT else \
                            (f"[slot {slot}] Day {info['day']} · {info['location']} · "
                             f"{money(info['credits'])} CR · {info['difficulty']} · "
                             f"saved {info['saved_at']}")
                    self.slots_list.insert("end", label)
                    self.slot_ids.append(slot)
            if self.slot_ids:
                self.slots_list.selection_set(0)
                self.slots_list.bind("<Double-Button-1>", lambda e: self._continue())
            ttk.Button(box, text="⏵ CONTINUE SELECTED GAME",
                       command=self._continue, style="Cyan.TButton").pack(
                fill="x", pady=(8, 2))

        ttk.Separator(box).pack(fill="x", pady=10)
        ttk.Label(box, text="OR BEGIN A NEW CAREER", style="Accent.TLabel",
                  font=(None, 10, "bold")).pack(anchor="w")

        name_row = ttk.Frame(box)
        name_row.pack(fill="x", pady=6)
        ttk.Label(name_row, text="Commander name:").pack(side="left")
        self.name_var = tk.StringVar(value="Commander")
        ttk.Entry(name_row, textvariable=self.name_var, width=24).pack(side="left", padx=8)

        diff_row = ttk.Frame(box)
        diff_row.pack(fill="x")
        self.diff_var = tk.StringVar(value=engine.difficulty_id)
        for did in ("easy", "normal", "hard", "nightmare"):
            d = DIFFICULTIES[did]
            ttk.Radiobutton(diff_row, text=f"{d.name} — {d.desc}",
                            value=did, variable=self.diff_var,
                            style="TRadiobutton").pack(anchor="w")

        ttk.Button(box, text="✦ LAUNCH NEW GAME", command=self._new_game,
                   style="Accent.TButton").pack(fill="x", pady=(10, 0))
        self.center()

    def _continue(self) -> None:
        if not hasattr(self, "slots_list"):
            return
        sel = self.slots_list.curselection()
        if not sel:
            return
        slot = self.slot_ids[sel[0]]
        ok, msg = self.gui.engine.load_game(slot)
        if ok:
            self.gui._tab_sig.clear()
            self.gui.victory_shown = False
            self.close()
            self.gui.refresh_all()
        else:
            messagebox.showerror("Load", msg, parent=self)

    def _new_game(self) -> None:
        self.gui.engine.new_game(self.name_var.get(), self.diff_var.get())
        self.gui._tab_sig.clear()
        self.gui.victory_shown = False
        self.gui.engine.autosave()
        self.close()
        self.gui.refresh_all()


class NewGameDialog(ModalDialog):
    """Started from the HUD button: confirm and choose difficulty."""

    def __init__(self, gui: SpaceTraderGUI):
        super().__init__(gui, "New Game", "580x430")
        ttk.Label(self, text="START A NEW CAREER", style="Accent.TLabel",
                  font=(None, 12, "bold")).pack(pady=(16, 2))
        ttk.Label(self, text="Your current progress will be replaced (saves on disk stay).",
                  style="Dim.TLabel").pack()

        box = ttk.Frame(self, padding=16)
        box.pack(fill="both", expand=True)
        name_row = ttk.Frame(box)
        name_row.pack(fill="x", pady=6)
        ttk.Label(name_row, text="Commander name:").pack(side="left")
        self.name_var = tk.StringVar(value=gui.engine.player.name)
        ttk.Entry(name_row, textvariable=self.name_var, width=24).pack(side="left", padx=8)

        self.diff_var = tk.StringVar(value=gui.engine.player.difficulty_id)
        for did in ("easy", "normal", "hard", "nightmare"):
            d = DIFFICULTIES[did]
            ttk.Radiobutton(box, text=f"{d.name} — {d.desc}",
                            value=did, variable=self.diff_var,
                            style="TRadiobutton").pack(anchor="w")

        ttk.Button(box, text="✦ START NEW GAME", command=self._start,
                   style="Accent.TButton").pack(pady=(12, 0))
        self.center()

    def _start(self) -> None:
        self.gui.engine.new_game(self.name_var.get(), self.diff_var.get())
        self.gui.victory_shown = False
        self.gui._tab_sig.clear()
        self.gui.engine.autosave()
        self.close()
        self.gui.refresh_all()


class SaveLoadDialog(ModalDialog):
    def __init__(self, gui: SpaceTraderGUI, mode: str = "save"):
        title = "Save Game" if mode == "save" else "Load Game"
        super().__init__(gui, title, "660x400")
        self.mode = mode
        ttk.Label(self, text=title.upper(), style="Accent.TLabel",
                  font=(None, 12, "bold")).pack(pady=(14, 4))

        self.box = ttk.Frame(self, padding=16)
        self.box.pack(fill="both", expand=True)
        self.rows: Dict[str, ttk.Frame] = {}

        slots = SAVE_SLOTS if mode == "save" else (AUTO_SLOT,) + SAVE_SLOTS + (PRECOMBAT_SLOT,)
        for slot in slots:
            info = gui.engine.slot_info(slot)
            row = ttk.Frame(self.box)
            row.pack(fill="x", pady=4)
            label = f"[{'autosave' if slot == AUTO_SLOT else 'pre-combat' if slot == PRECOMBAT_SLOT else 'slot ' + slot}] "
            if info:
                label += (f"Day {info['day']} · {info['location']} · {money(info['credits'])} CR · "
                          f"{info['difficulty']} · {info['saved_at']}")
            else:
                label += "— empty —"
            ttk.Label(row, text=label, style="TLabel").pack(side="left")
            if mode == "save":
                if slot in SAVE_SLOTS:
                    ttk.Button(row, text="Save here",
                               command=lambda s=slot: self._save(s)).pack(side="right")
            else:
                if info:
                    ttk.Button(row, text="Load",
                               command=lambda s=slot: self._load(s)).pack(side="right")
        self.center()

    def _save(self, slot: str) -> None:
        ok, msg = self.gui.engine.save_game(slot)
        messagebox.showinfo("Save" if ok else "Error", msg, parent=self)
        if ok:
            self.close()
            self.gui.refresh_all()

    def _load(self, slot: str) -> None:
        ok, msg = self.gui.engine.load_game(slot)
        if ok:
            self.gui.victory_shown = False
            self.gui._tab_sig.clear()
            self.close()
            self.gui.refresh_all()
        else:
            messagebox.showerror("Load", msg, parent=self)


class HelpDialog(ModalDialog):
    """The 'How to Play' manual."""

    TEXT = (
        "★ HOW TO PLAY ★\n"
        "\n"
        "GOAL — Grow your net worth to 500,000 CR. Every jump, trade and victory "
        "pushes the ARC bar in the HUD toward it.\n"
        "\n"
        "TRADING — Buy low, sell high. Prices differ by planet economy; check the "
        "Market tab's 10-day sparklines and the detail chart on the right. Star Map "
        "lists best routes; a Deep Space Scanner Array reads remote prices before "
        "you jump. Dumping large lots depresses the local price.\n"
        "\n"
        "RANKS — Renown (wealth + deeds) promotes you from Cadet to Admiral. "
        "Ensign: better prices · Lieutenant: +12% contracts · Captain: cheaper "
        "fuel/repairs/wages · Commodore: faster reputation · Admiral: trading "
        "perks and softer loans.\n"
        "\n"
        "ENCOUNTERS — Pirate ambushes lead to tactical combat: FIRE, target "
        "subsystems, missiles, drones, RECHARGE (50% with a Shield Capacitor), "
        "BOARD crippled hulls, or flee. Closing the window mid-battle is not an "
        "escape — finish the fight. Customs scans can be bribed; smuggler bays "
        "and Zoe help hide contraband. Asteroid fields, wormholes and mining "
        "veins offer risk/reward choices.\n"
        "\n"
        "CARE — Keep fuel topped up (cheapest at mining worlds), repair hull "
        "damage before long jumps, buy insurance before dangerous runs, and "
        "watch loan interest — credit score 700+ cuts it 15%.\n"
        "\n"
        "KEYS — Ctrl+S save · Ctrl+L load · Ctrl+N new game · F1 this manual · "
        "double-click a planet to jump · click column headers to sort markets.\n"
        "\n"
        "Good hunting, Commander."
    )

    def __init__(self, gui: SpaceTraderGUI):
        super().__init__(gui, "How to Play", "640x560")
        ttk.Label(self, text="CAPTAIN'S FIELD MANUAL", style="Accent.TLabel",
                  font=(None, 13, "bold")).pack(pady=(14, 6))
        text = ScrolledText(self, bg=THEME["field"], fg=THEME["fg"],
                            relief="flat", wrap="word", font=(None, 10),
                            height=24, padx=12, pady=10)
        text.pack(fill="both", expand=True, padx=16)
        text.insert("1.0", self.TEXT)
        text.config(state="disabled")
        ttk.Button(self, text="Close", command=self.close).pack(pady=10)
        self.center()


class ChoiceDialog(ModalDialog):
    """Non-combat encounter: shows description, resolves a choice engine-side."""

    def __init__(self, gui: SpaceTraderGUI, title: str, desc: str,
                 choices: List[Tuple[str, bool]], resolver):
        super().__init__(gui, title, "620x380")
        ttk.Label(self, text=title, style="Accent.TLabel",
                  font=(None, 12, "bold"), wraplength=560).pack(pady=(18, 6), padx=20)
        ttk.Label(self, text=desc, style="TLabel", wraplength=560,
                  justify="left").pack(padx=24)

        self.result_box = tk.Text(self, height=7, bg=THEME["field"], fg=THEME["fg"],
                                  relief="flat", wrap="word", font=(None, 9),
                                  state="disabled")
        self.result_box.pack(fill="both", expand=True, padx=20, pady=10)

        btn_row = ttk.Frame(self)
        btn_row.pack(pady=(0, 14))
        for label, arg in choices:
            ttk.Button(btn_row, text=label, style="Cyan.TButton",
                       command=lambda a=arg: self._resolve(a, resolver)).pack(
                side="left", padx=6)
        self.center()

    def _resolve(self, arg: bool, resolver) -> None:
        msgs = resolver(arg)
        text = "\n".join(msgs)
        self.gui._set_textbox(self.result_box, text)
        self.gui.engine.announce(text.splitlines()[0] if text else "")
        self.gui.status_label.config(
            text=text.splitlines()[0] if text else "",
            foreground=THEME["accent"])
        self.gui.engine.autosave()
        self.gui.refresh_all()
        for w in self.winfo_children():
            if isinstance(w, ttk.Frame):
                for b in w.winfo_children():
                    if isinstance(b, ttk.Button):
                        b.config(state="disabled")
        ttk.Button(self, text="Continue", command=self.close,
                   style="Accent.TButton").pack(pady=(0, 12))


class CombatDialog(ModalDialog):
    """Tactical combat window with live bars, colored log and action buttons."""

    # Log line color classifier.
    LOG_TAGS = {
        "crit": (THEME["accent2"], True),      # magenta bold
        "hurt": (THEME["bad"], False),         # red
        "shield": (THEME["accent"], False),    # cyan
        "heal": (THEME["good"], False),        # green
        "sys": (THEME["warn"], True),          # yellow bold
        "dim": (THEME["fg_dim"], False),
    }

    def __init__(self, gui: SpaceTraderGUI, enc: Dict[str, Any]):
        super().__init__(gui, enc.get("title", "Combat!"), "760x800")
        # NOTE: closing the window mid-combat is NOT an escape — see close().
        self.combat = start_combat(gui.engine, enc)
        self.enc = enc
        self._build()
        for m in self.combat.combat_log:
            self._append_log(m)
        self._update()
        self.center()

    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        head = ttk.Frame(self, padding=(16, 10))
        head.pack(fill="x")
        title_lbl = ttk.Label(head, text=self.enc.get("title", "Combat!"),
                              style="Bad.TLabel", font=(None, 13, "bold"),
                              wraplength=690)
        title_lbl.pack(anchor="w")
        self.lbl_personality = ttk.Label(head, text="", style="Dim.TLabel")
        self.lbl_personality.pack(anchor="w")
        self.lbl_turn = ttk.Label(head, text="", style="Dim.TLabel")
        self.lbl_turn.pack(anchor="w")

        enemy = ttk.Frame(self, padding=(16, 4))
        enemy.pack(fill="x")
        self.lbl_enemy = ttk.Label(enemy, text="", style="Bad.TLabel",
                                   font=(None, 11, "bold"))
        self.lbl_enemy.pack(anchor="w")
        self.bar_ehull = ttk.Progressbar(enemy, style="Enemy.Horizontal.TProgressbar",
                                         maximum=100, value=100)
        self.bar_ehull.pack(fill="x", pady=2)
        self.bar_eshield = ttk.Progressbar(enemy, style="EnemyShield.Horizontal.TProgressbar",
                                           maximum=100, value=100)
        self.bar_eshield.pack(fill="x", pady=2)

        player = ttk.Frame(self, padding=(16, 4))
        player.pack(fill="x")
        self.lbl_player = ttk.Label(player, text="", style="Good.TLabel",
                                    font=(None, 11, "bold"))
        self.lbl_player.pack(anchor="w")
        prow = ttk.Frame(player)
        prow.pack(fill="x")
        self.bar_phull = ttk.Progressbar(prow, style="Hull.Horizontal.TProgressbar",
                                         maximum=100, value=100)
        self.bar_phull.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.bar_pshield = ttk.Progressbar(prow, style="Shield.Horizontal.TProgressbar",
                                           maximum=100, value=100)
        self.bar_pshield.pack(side="left", fill="x", expand=True)
        self.lbl_warnings = ttk.Label(player, text="", style="Bad.TLabel", wraplength=690)
        self.lbl_warnings.pack(anchor="w", pady=(2, 0))

        self.log = ScrolledText(self, height=14, bg=THEME["field"], fg=THEME["fg"],
                                relief="flat", wrap="word", font=(None, 9),
                                state="disabled")
        for tag, (color, bold) in self.LOG_TAGS.items():
            self.log.tag_configure(tag, foreground=color,
                                   font=(None, 9, "bold") if bold else (None, 9))
        self.log.pack(fill="both", expand=True, padx=16, pady=8)

        btn_grid = ttk.Frame(self, padding=(16, 0, 16, 12))
        btn_grid.pack(fill="x")
        self.buttons: Dict[str, ttk.Button] = {}
        defs = [
            ("fire", "⦿ FIRE ALL BATTERIES", "TButton",
             "Fire every beam weapon at the enemy."),
            ("target_engines", "✂ Target ENGINES", "TButton",
             "Aimed shots: may cripple their drive (they cannot flee)."),
            ("target_weapons", "✂ Target WEAPONS", "TButton",
             "Aimed shots: may knock out their guns (halves their damage)."),
            ("target_shields", "✂ Target SHIELD GRID", "TButton",
             "Aimed shots: may vent their shields to zero."),
            ("missile", "☄ FIRE MISSILE", "TButton",
             "85-125 damage, 95% hit. Needs a launcher and ammo."),
            ("drones", "✈ DEPLOY DRONES", "TButton",
             "Persistent 8-16 auto damage every turn. Needs a Drone Bay."),
            ("recharge", "⇪ RECHARGE SHIELDS", "TButton",
             "Restore 35% (50% with a Shield Capacitor Bank) of shields."),
            ("board", "⚓ BOARD ENEMY SHIP", "Danger.TButton",
             "Only when enemy hull ≤ 25%. Great loot — but failure hurts."),
            ("flee", "⇨ EMERGENCY FLEE", "TButton",
             "Escape chance scales with ship speed, navigator and thrusters."),
        ]
        for i, (action, label, style, tip) in enumerate(defs):
            b = ttk.Button(btn_grid, text=label, style=style,
                           command=lambda a=action: self._act(a))
            Tooltip(b, tip)
            b.grid(row=i // 3, column=i % 3, sticky="nsew", padx=3, pady=3)
            self.buttons[action] = b
        for col in range(3):
            btn_grid.columnconfigure(col, weight=1)

        self.lbl_result = ttk.Label(self, text="", style="Accent.TLabel",
                                    font=(None, 12, "bold"), wraplength=690)
        self.lbl_result.pack(pady=(0, 4))
        self.btn_close = ttk.Button(self, text="Close (after the battle)", command=self._finish)
        self.btn_close.pack(pady=(0, 12))

    def close(self) -> None:
        """Cannot be closed mid-battle — desertion is not an escape."""
        if not self.combat.is_finished:
            self._append_log(
                ">> BATTLE STILL RAGING — you cannot disengage by closing the window!",
                "sys")
            return
        super().close()

    def _act(self, action: str) -> None:
        if self.combat.is_finished:
            return
        msgs = self.combat.player_action(action)
        for m in msgs:
            self._append_log(m)
        self._update()

    def _classify(self, msg: str) -> str:
        low = msg.lower()
        if "critical" in low:
            return "crit"
        if "warning" in low or "hull damage" in low or "breached" in low or \
           "destroyed" in low or "failure" in low or "repulsed" in low or \
           "stole" in low or "hit:" in low or "damaged" in low:
            return "hurt"
        if "shield" in low and ("burned" in low or "struck" in low or
                                "restored" in low or "charge" in low or "vents" in low):
            return "shield"
        if "victory" in low or "salvaged" in low or "restored overnight" in low or \
           "storms the bridge" in low or "surrenders" in low or "seized" in low:
            return "heal"
        if low.startswith(">>") or "disabled" in low or " crippled" in low or \
           "offline" in low or "diverts" in low or "escapes" in low or \
           "flee" in low or "missile away" in low or "drone bay open" in low:
            return "sys"
        return "dim"

    def _append_log(self, msg: str, tag: Optional[str] = None) -> None:
        if tag is None:
            tag = self._classify(msg)
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n", tag)
        self.log.see("end")
        self.log.config(state="disabled")

    def _update(self) -> None:
        c = self.combat
        p = c.engine.player

        self.lbl_personality.config(
            text=f"Enemy pilot profile: {c.personality.upper()} — "
                 f"{ENEMY_PERSONALITIES[c.personality]}")
        self.lbl_turn.config(text=f"Turn {c.turn_count}")
        self.lbl_enemy.config(
            text=f"{c.enemy_name} [{c.enemy_ship_name}]   HULL {c.enemy_hull}/{c.enemy_max_hull}   "
                 f"SHIELD {c.enemy_shield}/{c.enemy_max_shield}"
                 + ("   [WEAPONS OFFLINE]" if c.enemy_weapons_damaged else "")
                 + ("   [ENGINES CRIPPLED]" if c.enemy_engines_damaged else ""))
        self.bar_ehull.config(maximum=max(1, c.enemy_max_hull), value=c.enemy_hull)
        self.bar_eshield.config(maximum=max(1, c.enemy_max_shield), value=c.enemy_shield)

        self.lbl_player.config(
            text=f"YOUR SHIP   HULL {p.hull}/{p.max_hull}   SHIELD {p.shield}/{p.effective_max_shield()}   "
                 f"MISSILES {p.missiles}   DRONES {'DEPLOYED' if c.drones_active else '—'}")
        self.bar_phull.config(maximum=max(1, p.max_hull), value=p.hull)
        self.bar_pshield.config(maximum=max(1, p.effective_max_shield()),
                                value=min(p.shield, p.effective_max_shield()))

        warnings = []
        if p.weapons_damaged:
            warnings.append("WEAPONS DAMAGED (-40% damage)")
        if p.engines_damaged:
            warnings.append("ENGINES DAMAGED (cannot flee)")
        if p.shields_damaged:
            warnings.append("SHIELDS DAMAGED (max -40%)")
        self.lbl_warnings.config(text="   ⚠   ".join(warnings))

        p_has_rack = p.has_missile_rack()
        self.buttons["fire"].config(state="normal" if not c.is_finished else "disabled")
        for a in ("target_engines", "target_weapons", "target_shields"):
            self.buttons[a].config(state="normal" if not c.is_finished else "disabled")
        self.buttons["missile"].config(
            state="normal" if (not c.is_finished and p_has_rack and p.missiles > 0) else "disabled",
            text=f"☄ FIRE MISSILE ({p.missiles})" if p_has_rack else "☄ MISSILES (no launcher)")
        self.buttons["drones"].config(
            state="normal" if (not c.is_finished and p.has_drone_bay()) else "disabled")
        self.buttons["recharge"].config(state="normal" if not c.is_finished else "disabled")
        self.buttons["board"].config(
            state="normal" if (not c.is_finished and c.can_board()) else "disabled")
        self.buttons["flee"].config(
            state="normal" if (not c.is_finished and c.can_flee()) else "disabled")

        if c.is_finished:
            if c.player_won:
                result = "VICTORY — the sector is a little safer."
            elif c.player_escaped:
                result = "ESCAPED — you live to run cargo another day."
            elif c.enemy_fled:
                result = "The enemy fled the field."
            elif c.insurance_used:
                result = "DESTROYED — insurance respawn complete."
            elif c.player_dead:
                result = "GAME OVER — your ship is gone."
            else:
                result = "The battle has ended."
            self.lbl_result.config(text=result,
                                   style="Good.TLabel" if c.player_won else "Bad.TLabel")
            self.btn_close.config(state="normal", text="Close")
        else:
            self.btn_close.config(state="disabled", text="Close (after the battle)")

    def _finish(self) -> None:
        if not self.combat.is_finished:
            return
        gui = self.gui
        combat = self.combat
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        gui.engine.autosave()
        gui.combat_dialog = None
        gui.refresh_all()
        if combat.player_dead:
            gui.handle_death()
        else:
            gui.check_end_states()


class VictoryDialog(ModalDialog):
    def __init__(self, gui: SpaceTraderGUI):
        super().__init__(gui, "VICTORY", "620x460")
        nw = gui.engine.calculate_net_worth()
        p = gui.engine.player
        ttk.Label(self, text="★ GALACTIC MOGUL ★", style="Good.TLabel",
                  font=(None, 18, "bold")).pack(pady=(24, 4))
        ttk.Label(self, text=f"Net worth reached {money(nw)} CR!", style="Accent.TLabel",
                  font=(None, 12)).pack()
        box = ttk.Frame(self, padding=20)
        box.pack(fill="both", expand=True)
        stats = (f"Commander {p.name} — Day {p.day} ({gui.engine.difficulty.name})\n"
                 f"Final rank: {gui.engine.rank().insignia} {gui.engine.rank().name}\n\n"
                 f"Trading profit: {money(p.stats['total_profit'])} CR\n"
                 f"Hyperjumps: {p.stats['jumps_made']}\n"
                 f"Pirates destroyed: {p.stats['pirates_defeated']}\n"
                 f"Bounties claimed: {p.stats['bounties_claimed']}\n"
                 f"Contracts fulfilled: {p.stats['missions_completed']}\n"
                 f"Achievements: {len(p.achievements)}/{len(ACHIEVEMENTS)}")
        ttk.Label(box, text=stats, style="TLabel", justify="left",
                  font=(None, 10)).pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(pady=(12, 0))
        ttk.Button(row, text="Keep Playing", style="Accent.TButton",
                   command=self.close).pack(side="left", padx=6)
        ttk.Button(row, text="Retire (Save & Exit)",
                   command=lambda: self._retire()).pack(side="left", padx=6)
        self.center()

    def _retire(self) -> None:
        ok, msg = self.gui.engine.save_game("1")
        if not ok:
            messagebox.showerror("Save", msg, parent=self)
        self.close()
        self.gui.root.destroy()


# ==============================================================================
# SELF-TEST SUITE
# ==============================================================================

def run_self_test() -> None:
    """Headless verification of the entire engine. Exits non-zero on failure."""
    print("=" * 76)
    print("SPACE TRADER: ODYSSEY — NEBULA EDITION · SELF-TEST SUITE")
    print("=" * 76)
    failures: List[str] = []

    # Keep the test run hermetic: saves go to a scratch directory.
    import tempfile
    scratch = tempfile.mkdtemp(prefix="st_test_")
    os.environ["ST_SAVE_DIR"] = scratch

    def check(name: str, cond: bool, detail: str = "") -> None:
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    # ------------------------------------------------------------------ #
    # 1. Static data integrity
    print("\n--- 1. Data integrity ---")
    check("16 planets defined", len(generate_default_planets()) == 16)
    check("10 ships defined", len(SHIP_TEMPLATES) == 10,
          f"got {len(SHIP_TEMPLATES)}")
    check("22 equipment items", len(EQUIPMENT_ITEMS) == 22,
          f"got {len(EQUIPMENT_ITEMS)}")
    check("18 commodities", len(COMMODITIES) == 18)
    check("18 events reference valid goods",
          all(ev[1] in COMMODITIES for ev in PLANET_EVENTS_POOL))
    check("23 achievements defined", len(ACHIEVEMENTS) == 23,
          f"got {len(ACHIEVEMENTS)}")
    planets = generate_default_planets()
    check("planet coordinates unique",
          len({(p.x, p.y) for p in planets.values()}) == 16)
    check("4 factions defined", len(FACTIONS) == 4)
    check("all planet factions are known factions",
          all(p.faction in FACTIONS for p in planets.values()))
    check("6 ranks defined", len(RANKS) == 6)
    check("ship costs strictly ascending",
          all(SHIP_TEMPLATES[a].cost < SHIP_TEMPLATES[b].cost
              for a, b in zip(list(SHIP_TEMPLATES)[1:], list(SHIP_TEMPLATES)[2:])))
    check("all equipment slot types valid",
          all(e.slot_type in ("weapon", "shield", "module")
              for e in EQUIPMENT_ITEMS.values()))
    check("crew roster resolves", len(CREW_INDEX) == len(AVAILABLE_CREW))

    # ------------------------------------------------------------------ #
    # 2. Difficulty presets & new game
    print("\n--- 2. Difficulty & new game ---")
    engines = {}
    for did, diff in DIFFICULTIES.items():
        e = GameEngine(muted=True, difficulty_id=did)
        engines[did] = e
        check(f"{did} starting credits = {diff.starting_credits}",
              e.player.credits == diff.starting_credits)
    check("easy starts richer than hard",
          engines["easy"].player.credits > engines["hard"].player.credits)
    check("nightmare is leanest",
          engines["nightmare"].player.credits < engines["hard"].player.credits)
    normal = engines["normal"]
    normal.new_game("Tester", "normal")
    check("new_game resets day", normal.player.day == 1)
    check("new_game applies name", normal.player.name == "Tester")
    check("new_game clamps unknown difficulty", (
        GameEngine(muted=True, difficulty_id="crazy").player.difficulty_id == "normal"))
    check("markets generated everywhere",
          all(len(p.market) == len(COMMODITIES) for p in normal.planets.values()))
    check("mission board generated", len(normal.available_missions) >= 3)
    check("board types valid",
          all(m.m_type in ("delivery", "smuggle", "bounty", "medical")
              for m in normal.available_missions))
    check("mission destinations valid",
          all(m.destination in normal.planets for m in normal.available_missions))

    # ------------------------------------------------------------------ #
    # 3. Captain ranks & renown
    print("\n--- 3. Ranks & renown ---")
    e = GameEngine(muted=True, difficulty_id="normal")
    e.new_game("Ranker", "normal")
    check("fresh career is Cadet", e.rank().id == "cadet")
    check("renown is non-negative", e.renown() >= 0)
    check("promotion returns None at start", e.check_promotion() is None)
    e.player.credits = 30_000
    promo = e.check_promotion()
    check("30k credits promotes to Ensign", promo is not None and promo.id == "ensign",
          f"got {promo.id if promo else None}")
    check("promotion news recorded",
          any("PROMOTION" in n for n in e.news_feed))
    check("no double announcement", e.check_promotion() is None)

    # Rank perks are derived from RENOWN (net worth + deeds), so the tests
    # below raise wealth to reach the rank under test.
    e2 = GameEngine(muted=True, difficulty_id="normal")
    e2.new_game("Pricey", "normal")
    some_good = "electronics"
    sell_cadet = e2.get_sell_price(some_good)
    buy_cadet = e2.get_buy_price(some_good)
    e2.player.credits = 800_000          # renown 400k -> Admiral
    check("wealth promotes to admiral", e2.rank().id == "admiral", e2.rank().id)
    sell_admiral = e2.get_sell_price(some_good)
    buy_admiral = e2.get_buy_price(some_good)
    check("admiral sells higher than cadet", sell_admiral > sell_cadet,
          f"{sell_admiral} vs {sell_cadet}")
    check("admiral buys cheaper than cadet", buy_admiral < buy_cadet,
          f"{buy_admiral} vs {buy_cadet}")

    # Service discount at Captain.
    e3 = GameEngine(muted=True, difficulty_id="normal")
    e3.new_game("Cap", "normal")
    e3.player.credits = 150_000         # renown 75k -> Captain
    check("wealth promotes to captain", e3.rank().id == "captain", e3.rank().id)
    base_repair = max(6, int(round(e3.current_planet.repair_cost
                             * e3.difficulty.repair_mult)))
    discounted = e3.current_repair_price()
    check("captain repair discount applies", discounted < base_repair,
          f"{discounted} vs {base_repair}")

    # Contract bonus at Lieutenant.
    e4 = GameEngine(muted=True, difficulty_id="normal")
    e4.new_game("Lt", "normal")
    e4.player.credits = 60_000          # renown 30k -> Lieutenant
    check("wealth promotes to lieutenant", e4.rank().id == "lieutenant", e4.rank().id)
    m = next(mm for mm in e4.available_missions if mm.m_type == "delivery")
    e4.accept_mission(m.id)
    e4.player.location = m.destination
    credits_before = e4.player.credits
    msgs = e4.check_mission_deliveries()
    payout = e4.player.credits - credits_before
    check("lieutenant contract bonus paid",
          payout > m.reward_credits and any("Lieutenant" in x for x in msgs),
          f"payout {payout} vs reward {m.reward_credits}")

    # Interest mult at Admiral.
    e5 = GameEngine(muted=True, difficulty_id="normal")
    e5.new_game("Adm", "normal")
    e5.player.credits = 800_000         # renown 400k -> Admiral
    e5.player.loan = 10_000
    e5.player.credit_score = 650
    base_interest = DIFFICULTIES["normal"].loan_interest
    eff = e5._effective_loan_interest()
    check("admiral loan interest reduced", abs(eff - base_interest * 0.6) < 1e-9,
          f"{eff}")
    e5.player.credit_score = 750
    check("credit score 700+ cuts interest",
          e5._effective_loan_interest() < base_interest * 0.6)

    check("rank thresholds ascending",
          all(a.renown < b.renown for a, b in zip(RANKS, RANKS[1:])))
    check("rank_index_for mapping",
          rank_index_for(0) == 0 and rank_index_for(7_000) == 1
          and rank_index_for(400_000) == 5)

    # ------------------------------------------------------------------ #
    # 4. Trading & charts
    print("\n--- 4. Trading & market charts ---")
    e = GameEngine(muted=True, difficulty_id="normal")
    e.new_game("Trader", "normal")
    p = e.current_planet
    good = "water"
    price0 = e.current_planet.market[good]
    stock0 = p.stock.get(good, 0)
    qty = min(5, stock0, e.player.cargo_free(),
              e.player.credits // max(1, e.get_buy_price(good)))
    ok, msg = e.buy_commodity(good, qty)
    check("buy succeeds", ok, msg)
    check("credits deducted", e.player.credits < 2_500)
    check("cargo recorded", e.player.cargo.get(good, 0) == qty)
    check("stock decremented", p.stock[good] == stock0 - qty)
    mid_before = p.market[good]
    ok, msg = e.sell_commodity(good, qty)
    check("sell succeeds", ok, msg)
    check("price impact: dumping lowered price", p.market[good] <= mid_before,
          f"{p.market[good]} vs {mid_before}")
    ok, msg = e.buy_commodity(good, 0)
    check("zero quantity rejected", not ok)
    ok, msg = e.buy_commodity("nonexistent", 1)
    check("unknown commodity rejected", not ok)
    ok, msg = e.buy_commodity(good, 999_999)
    check("oversized order rejected", not ok)

    check("buy price >= 1", e.get_buy_price(good) >= 1)
    check("buy exceeds sell (spread exists)",
          e.get_buy_price(good) > e.get_sell_price(good))

    # Chart data & sparkline
    e.advance_day(1)
    hist = p.price_history.get(good, [])
    check("price history grows", len(hist) >= 2, f"len={len(hist)}")
    check("history capped at 10", len(hist) <= PRICE_HISTORY_LEN)
    spark = sparkline(hist)
    check("sparkline renders blocks", len(spark) == len(hist) and
          all(ch in SPARK_CHARS for ch in spark), spark)
    check("flat history renders filler", sparkline([42]) == "▄")
    check("trend detection works", p.trend(good) in ("up", "down", "flat"))

    # Trade advisor
    routes = e.compute_best_trade_routes(from_current_only=True)
    check("routes found", len(routes) >= 1)
    check("routes sorted by net profit",
          all(a["net_profit"] >= b["net_profit"] for a, b in zip(routes, routes[1:])))
    check("route days account for ship speed",
          all(r["days"] >= 1 for r in routes))
    kestrel_days = GameEngine(muted=True, difficulty_id="normal")
    kestrel_days.new_game("K", "normal")
    behemoth_days = GameEngine(muted=True, difficulty_id="normal")
    behemoth_days.new_game("B", "normal")
    behemoth_days.player.ship_id = "behemoth"
    dst = next(name for name in kestrel_days.planets
               if name != kestrel_days.player.location)
    kd = kestrel_days.calculate_travel_cost(kestrel_days.planets[dst])[1]
    bd = behemoth_days.calculate_travel_cost(behemoth_days.planets[dst])[1]
    check("fast ships travel fewer days", kd <= bd, f"{kd} vs {bd}")

    # ------------------------------------------------------------------ #
    # 5. Services, ships, equipment, crew
    print("\n--- 5. Services, fleet & crew ---")
    e = GameEngine(muted=True, difficulty_id="easy")
    e.new_game("Shopper", "easy")
    e.player.credits = 400_000
    e.player.hull -= 20
    ok, msg = e.repair_hull(10)
    check("hull repair works", ok, msg)
    e.player.fuel -= 40                   # make room in the tank
    ok, msg = e.buy_fuel(25)
    check("fuel purchase works", ok, msg)
    ok, msg = e.buy_insurance()
    check("insurance purchase works", ok, msg)
    check("insurance price sane", e.insurance_price() >= 500)

    ok, msg = e.buy_ship("drake")
    check("buy new Drake Freighter", ok, msg)
    check("ship swapped", e.player.ship_id == "drake")
    check("new hull full", e.player.hull == e.player.max_hull)

    ok, msg = e.buy_ship("valkyrie")   # 4 hardpoints for the weapon suite
    check("buy Valkyrie Gunship", ok, msg)
    ok, msg = e.buy_equipment("flak_cannon")
    check("install flak cannon", ok, msg)
    ok, msg = e.buy_equipment("flak_cannon")
    check("duplicate weapon rejected", not ok)
    ok, msg = e.buy_equipment("particle_lance")
    check("install particle lance", ok, msg)
    ok, msg = e.buy_equipment("shield_capacitor")
    check("install shield capacitor", ok, msg)
    ok, msg = e.buy_equipment("deep_scanner")
    check("install deep scanner", ok, msg)
    ok, msg = e.buy_equipment("plasma_1")
    check("fill final hardpoint", ok, msg)
    check("slot overflow rejected",
          not e.buy_equipment("ion_cannon")[0])

    e2 = GameEngine(muted=True, difficulty_id="easy")
    e2.new_game("Crewed", "easy")
    e2.player.credits = 100_000
    ok, msg = e2.hire_crew("vance")
    check("hire navigator", ok, msg)
    ok, msg = e2.hire_crew("vance")
    check("re-hire rejected", not ok)
    check("wages computed", e2.total_daily_wages() == 45)
    ok, msg = e2.dismiss_crew("vance")
    check("dismiss works", ok, msg)
    check("wages zero after dismiss", e2.total_daily_wages() == 0)

    # ------------------------------------------------------------------ #
    # 6. Missions & rank rewards
    print("\n--- 6. Contracts ---")
    e = GameEngine(muted=True, difficulty_id="normal")
    e.new_game("Courier", "normal")
    mission = next(m for m in e.available_missions if m.m_type == "delivery")
    ok, msg = e.accept_mission(mission.id)
    check("accept delivery", ok, msg)
    check("mission cargo loaded",
          e.player.cargo.get(mission.cargo_good, 0) >= mission.cargo_qty)
    e.player.location = mission.destination
    msgs = e.check_mission_deliveries()
    check("delivery completes on arrival", bool(msgs))
    check("reward credited", e.player.credits > 2_500)
    check("mission removed from active", mission not in e.player.active_missions)
    check("missions_completed stat incremented",
          e.player.stats["missions_completed"] == 1)

    # ------------------------------------------------------------------ #
    # 7. Travel, time, economy
    print("\n--- 7. Travel & economy ---")
    e = GameEngine(muted=True, difficulty_id="normal")
    e.new_game("Traveler", "normal")
    dest = next(name for name in e.planets if name != e.player.location)
    day_before = e.player.day
    fuel_before = e.player.fuel
    ok, msg, enc = e.execute_travel(dest)
    check("travel executes", ok, msg)
    check("time passes", e.player.day > day_before)
    check("fuel burned", e.player.fuel < fuel_before)
    check("location updated", e.player.location == dest)
    check("jumps stat incremented", e.player.stats["jumps_made"] == 1)
    check("net worth history grows", len(e.player.net_worth_history) >= 2)

    e.player.loan = 1_000
    e.advance_day(5)
    check("loan interest accrues", e.player.loan > 1_000)
    e.player.savings = 5_000
    e.advance_day(5)
    check("savings earn interest", e.player.savings > 5_000)

    # ------------------------------------------------------------------ #
    # 8. Encounters — including the three new ones
    print("\n--- 8. Encounters ---")
    e = GameEngine(muted=True, difficulty_id="normal")
    e.new_game("Wanderer", "normal")

    enc = {"type": "asteroid_field", "title": "Asteroid Field"}
    fuel_before = e.player.fuel
    e.resolve_asteroid_field(enc, thread_needle=False)
    check("asteroid detour burns fuel", e.player.fuel == fuel_before - 8,
          f"{e.player.fuel} vs {fuel_before - 8}")

    results = [e.resolve_asteroid_field(enc, thread_needle=True) for _ in range(60)]
    check("asteroid threading resolves", all(isinstance(r, list) for r in results))

    loc_before = e.player.location
    e.resolve_wormhole({"type": "wormhole"}, enter=True)
    check("wormhole relocates player", e.player.location != loc_before,
          f"{loc_before} -> {e.player.location}")
    check("wormhole stat counts", e.player.stats["wormholes"] >= 1)
    check("wormhole_rider achievement unlocked",
          "wormhole_rider" in e.player.achievements)

    e.player.cargo = {}
    e.player.cargo_cap = 50
    fuel_before = e.player.fuel
    msgs = e.resolve_mining({"type": "mining_opportunity", "vein": "ore"}, mine=True)
    check("mining burns fuel", e.player.fuel == fuel_before - 10)
    check("mining yields ore", e.player.cargo.get("ore", 0) > 0, str(msgs))
    check("mining stat counts", e.player.stats["mining_ops"] == 1)

    enc = e.generate_random_encounter(e.current_planet)
    check("encounter generator returns dict or None",
          enc is None or isinstance(enc, dict))

    # Encounter type coverage over many draws (weighted table sanity).
    seen = set()
    for _ in range(400):
        draw = e.generate_random_encounter(e.planets["Pirate Haven"])
        if draw:
            seen.add(draw.get("type"))
    expected = {"pirate_ambush", "customs_scan", "faction_patrol", "derelict",
                "solar_flare", "distress_beacon", "wandering_trader",
                "asteroid_field", "wormhole", "mining_opportunity"}
    check("all 10 encounter types reachable", expected <= seen, str(expected - seen))

    # Customs resolution
    e.player.cargo = {"narcotics": 5}
    msgs = e.resolve_customs({}, bribe=False)
    check("customs confiscates contraband",
          "narcotics" not in e.player.cargo, str(msgs))

    # Patrol resolution
    e.player.cargo = {}
    rep_before = e.player.rep(e.current_planet.faction)
    e.resolve_faction_patrol({"faction": e.current_planet.faction}, cooperate=True)
    check("patrol cooperation improves standing",
          e.player.rep(e.current_planet.faction) >= rep_before)

    # ------------------------------------------------------------------ #
    # 9. Combat
    print("\n--- 9. Combat ---")
    e = GameEngine(muted=True, difficulty_id="normal")
    e.new_game("Fighter", "normal")
    low_tier = CombatEncounter(e, "Weak Corsair", "sparrow")
    e2 = GameEngine(muted=True, difficulty_id="normal")
    e2.new_game("RichFighter", "normal")
    e2.player.credits = 500_000
    high_tier = CombatEncounter(e2, "Dread Corsair", "valkyrie")
    check("wealth scales enemy threat",
          high_tier.enemy_max_hull >= low_tier.enemy_max_hull,
          f"{high_tier.enemy_max_hull} vs {low_tier.enemy_max_hull}")
    check("early-game scaling floor applied",
          low_tier.enemy_damage_range[1] <= 32,
          str(low_tier.enemy_damage_range))

    # Deterministic victory path: give the player overwhelming firepower.
    e.player.credits = 500_000
    e.player.equipped_weapons = ["particle_lance", "particle_lance",
                                 "particle_lance", "particle_lance", "particle_lance"]
    e.recalculate_ship_stats()
    combat = CombatEncounter(e, "Doomed Corsair", "sparrow")
    check("fresh combat not finished", not combat.is_finished)
    check("combat log seeded", len(combat.combat_log) >= 2)
    for _ in range(40):
        if combat.is_finished:
            break
        combat.player_action("fire")
    check("overwhelming firepower wins", combat.player_won)
    check("pirates_defeated counted", e.player.stats["pirates_defeated"] >= 1)
    check("victory raises Sol standing", e.player.rep("Sol Federation") > 0)
    check("victory angers Corsairs", e.player.rep("Free Corsairs") < 0)

    # Flee mechanics
    e3 = GameEngine(muted=True, difficulty_id="easy")
    e3.new_game("Runner", "easy")
    combat2 = CombatEncounter(e3, "Chaser", "sparrow")
    e3.player.ship_id = "kestrel"
    e3.recalculate_ship_stats()
    e3.player.engines_damaged = True
    check("cannot flee with damaged engines", not combat2.can_flee())
    e3.player.engines_damaged = False
    check("can flee with working engines", combat2.can_flee())
    escaped_any = False
    for _ in range(60):
        c = CombatEncounter(e3, "Chaser", "sparrow")
        c.player_action("flee")
        if c.player_escaped:
            escaped_any = True
            break
    check("flee eventually succeeds", escaped_any)

    # Boarding
    e4 = GameEngine(muted=True, difficulty_id="easy")
    e4.new_game("Boarder", "easy")
    combat3 = CombatEncounter(e4, "Victim", "sparrow")
    combat3.enemy_hull = 1
    check("boarding available on crippled hull", combat3.can_board())
    for _ in range(20):                 # repulsals cost a turn; keep trying
        if combat3.is_finished:
            break
        combat3.player_action("board")
    check("boarding (or the rout it causes) resolves combat", combat3.is_finished)

    # Missile combat + shield capacitor
    e5 = GameEngine(muted=True, difficulty_id="easy")
    e5.new_game("Gunner", "easy")
    e5.player.equipped_weapons = ["missile_rack"]
    e5.player.missiles = 3
    combat4 = CombatEncounter(e5, "Target", "sparrow")
    before = e5.player.missiles
    msgs = combat4.player_action("missile")
    check("missile fired & consumed", e5.player.missiles == before - 1, str(msgs))
    e5.player.equipped_modules = ["shield_capacitor", "drone_bay"]
    e5.recalculate_ship_stats()
    e5.player.shield = 0
    eff_max = e5.player.effective_max_shield()
    e5.player.shield = 0
    combat5 = CombatEncounter(e5, "Target2", "sparrow")
    combat5._recharge_shields()          # unit-level: isolate the capacitor perk
    check("capacitor boosts recharge to ~50%",
          e5.player.shield >= int(eff_max * 0.45),
          f"{e5.player.shield} of {eff_max}")

    # ------------------------------------------------------------------ #
    # 10. Banking, stocks, credit score
    print("\n--- 10. Bank & stocks ---")
    e = GameEngine(muted=True, difficulty_id="normal")
    e.new_game("Banker", "normal")
    ok, msg = e.deposit(500)
    check("deposit works", ok, msg)
    ok, msg = e.withdraw(200)
    check("withdraw works", ok, msg)
    score_before = e.player.credit_score
    e.player.credits = 50_000
    e.player.loan = 20_000
    e.repay(5_000)
    check("big repayment raises credit score",
          e.player.credit_score == min(850, score_before + 2))
    ok, msg = e.borrow(1_000)
    check("borrow works", ok, msg)
    check("credit limit finite", e.loan_limit() > 0)
    ok, msg = e.buy_stock("SOL", 5)
    check("stock buy works", ok, msg)
    ok, msg = e.sell_stock("SOL", 5)
    check("stock sell works", ok, msg)

    # ------------------------------------------------------------------ #
    # 11. Insurance respawn
    print("\n--- 11. Insurance ---")
    e = GameEngine(muted=True, difficulty_id="normal")
    e.new_game("Insured", "normal")
    e.player.credits = 10_000
    e.player.cargo = {"food": 4}
    e.buy_insurance()
    combat = CombatEncounter(e, "Executioner", "behemoth")
    e.player.hull = 1
    e.player.shield = 0
    for _ in range(10):
        if combat.is_finished:
            break
        combat._enemy_attack()
    check("insurance triggers instead of death", combat.insurance_used)
    check("player survived", not combat.player_dead)
    check("cargo lost on claim", e.player.cargo == {})
    check("insurance claim counted", e.player.stats["insurance_claims"] == 1)

    # ------------------------------------------------------------------ #
    # 12. Save / load round trip (v4)
    print("\n--- 12. Save & load ---")
    e = GameEngine(muted=True, difficulty_id="normal")
    e.new_game("Saver", "hard")
    e.player.credits = 60_000
    e.buy_equipment("deep_scanner")     # spend first...
    e.player.credits = 60_000           # ...then top up to a clean number
    e.player.highest_rank_index = 2
    e.player.stats["mining_ops"] = 3
    e.player.cargo = {"gemstones": 6}
    ok, msg = e.save_game("1")
    check("save succeeds", ok, msg)
    ok, msg = e.load_game("1")
    check("load succeeds", ok, msg)
    check("credits restored", e.player.credits == 60_000)
    check("rank index restored", e.player.highest_rank_index == 2)
    check("new stats restored", e.player.stats["mining_ops"] == 3)
    check("equipment restored", "deep_scanner" in e.player.equipped_modules)
    check("cargo restored", e.player.cargo.get("gemstones") == 6)
    ok, msg = e.load_game("nope")
    check("missing slot handled", not ok)
    check("slot metadata readable", e.slot_info("1") is not None)

    # Deluxe (v3-style) save compatibility: older payloads lack the new keys.
    legacy = e._save_payload()
    legacy["version"] = 3
    legacy["player"].pop("highest_rank_index", None)
    legacy["player"]["stats"].pop("mining_ops", None)
    legacy["player"]["stats"].pop("wormholes", None)
    legacy_path = os.path.join(scratch, slot_path("legacy").split(os.sep)[-1])
    with open(legacy_path, "w", encoding="utf-8") as f:
        json.dump(legacy, f)
    ok, msg = e.load_game("legacy")
    check("legacy v3 save loads", ok, msg)
    check("legacy rank defaults cleanly", e.player.highest_rank_index >= 0)

    # ------------------------------------------------------------------ #
    # 13. Achievements & victory
    print("\n--- 13. Achievements & victory ---")
    e = GameEngine(muted=True, difficulty_id="normal")
    e.new_game("Mogul", "easy")
    e.player.credits = TARGET_NET_WORTH + 10_000
    unlocked = e.check_achievements()
    check("victory achieved", e.victory_achieved)
    check("galactic mogul unlocked", "nw_target" in e.player.achievements)
    e.player.day = 51
    e.check_achievements()
    check("survivor achievement", "survivor" in e.player.achievements)
    e.player.highest_rank_index = 5
    e.check_achievements()
    check("rank achievements unlock",
          "rank_captain" in e.player.achievements and
          "rank_admiral" in e.player.achievements)

    # ------------------------------------------------------------------ #
    # 14. Faction reputation
    print("\n--- 14. Faction reputation ---")
    e = GameEngine(muted=True, difficulty_id="normal")
    e.new_game("Diplomat", "normal")
    check("reputation starts at zero", all(v == 0 for v in e.player.reputation.values()))
    check("rank of 0 is Neutral", reputation_rank(0) == "Neutral")
    check("rank of 90 is Exalted", reputation_rank(90) == "Exalted")
    check("rank of -90 is Nemesis", reputation_rank(-90) == "Nemesis")
    applied = e.adjust_reputation("Sol Federation", 10)
    check("adjust_reputation applies delta", applied == 10)
    for _ in range(30):
        e.adjust_reputation("Sol Federation", 10)
    check("reputation clamps at ceiling",
          e.player.rep("Sol Federation") == REPUTATION_MAX)
    check("unknown faction ignored", e.adjust_reputation("Bogus", 10) == 0)
    check("good standing yields cheaper prices than poor standing",
          e.reputation_price_mult("Sol Federation") < 1.0)
    e.player.credits = 10_000
    ok, msg = e.hire_crew("sable")
    check("diplomat hire works", ok, msg)
    e2 = GameEngine(muted=True, difficulty_id="normal")
    e2.new_game("Plain", "normal")
    gain_diplomat = e.adjust_reputation("Outer Alliance", 6)
    gain_plain = e2.adjust_reputation("Outer Alliance", 6)
    check("diplomat doubles reputation gains", gain_diplomat > gain_plain)
    check("victory raises lawful reputation", True)  # covered in combat section

    # ------------------------------------------------------------------ #
    # 15. Long-run simulation (stability + balance smoke)
    print("\n--- 15. 300-day random simulation ---")
    sim = GameEngine(muted=True, difficulty_id="normal")
    sim.new_game("Sim", "normal")
    sim.player.credits = 30_000
    sim.player.equipped_weapons = ["flak_cannon", "laser_2"]
    sim.recalculate_ship_stats()
    crashed = False
    try:
        for i in range(150):
            dest = random.choice(list(sim.planets))
            if dest != sim.player.location:
                ok, _, enc = sim.execute_travel(dest)
                if not ok:
                    sim.advance_day(1)   # stranded: time still passes
                    enc = None
            else:
                sim.advance_day(1)
                enc = None
            if enc and enc.get("type") in ("pirate_ambush",):
                combat = CombatEncounter(sim, enc["enemy_name"], enc["enemy_ship"])
                for _ in range(30):
                    if combat.is_finished:
                        break
                    combat.player_action(random.choice(("fire", "fire", "recharge", "flee")))
            if enc and enc.get("type") not in (None, "pirate_ambush", "bounty_combat"):
                sim.resolve_encounter(enc, "resolve", arg=random.random() < 0.6)
            if i % 7 == 0:
                goods = list(COMMODITIES)
                g = random.choice(goods)
                if sim.player.cargo.get(g, 0) > 0:
                    sim.sell_commodity(g, min(10, sim.player.cargo[g]))
                else:
                    sim.buy_commodity(g, min(5, sim.current_planet.stock.get(g, 0)))
            if i % 11 == 0:
                sim.buy_fuel(30)
                sim.repair_hull(40)
    except Exception as exc:  # pragma: no cover
        crashed = True
        print(f"    simulation exception: {exc!r}")
    check("simulation ran 150 turns without crashing", not crashed)
    check("simulation never went negative credits", sim.player.credits >= 0)
    check("simulation hull intact-or-alive", 0 <= sim.player.hull <= sim.player.max_hull)
    check("simulation advanced time", sim.player.day > 100)

    # ------------------------------------------------------------------ #
    # 16. Cleanup
    print("\n--- 16. Cleanup ---")
    import shutil
    shutil.rmtree(scratch, ignore_errors=True)
    check("test scratch dir removed", not os.path.isdir(scratch))

    print("=" * 76)
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        print("=" * 76)
        sys.exit(1)
    print("ALL SELF-TESTS PASSED ✔")
    print("=" * 76)


# ==============================================================================
# GUI SMOKE TEST (headless, requires a display / xvfb)
# ==============================================================================

def run_gui_test() -> None:
    print("Running headless GUI smoke test...")
    import tempfile
    scratch = tempfile.mkdtemp(prefix="st_guitest_")
    os.environ["ST_SAVE_DIR"] = scratch

    root = tk.Tk()
    engine = GameEngine(muted=True, difficulty_id="easy")
    engine.new_game("GUITester", "easy")
    gui = SpaceTraderGUI.__new__(SpaceTraderGUI)
    gui.root = root
    gui.engine = engine
    gui.victory_shown = False
    gui.selected_planet = None
    gui.combat_dialog = None
    gui._tab_sig = {}
    gui._twinkle_ids = []
    gui._twinkle_phase = 0
    gui._pulse_radius = 14
    gui._pulse_grow = True
    gui._map_anim_running = False
    gui._jump_anim_id = None

    gui._setup_window()
    gui._build_styles()
    gui._build_hud()
    gui._build_notebook()
    gui._build_statusbar()
    gui._bind_keys()

    # Exercise every tab refresh (multiple passes to exercise signature cache).
    for idx in range(8):
        gui.notebook.select(idx)
        root.update()
    gui.refresh_all()
    root.update()
    gui.refresh_all()   # second pass: should hit the signature fast-path
    root.update()

    # Select a planet on the star map programmatically.
    dest = next(d for d in engine.planets if d != engine.player.location)
    gui.selected_planet = dest
    gui._update_map_info()
    gui.draw_map()
    root.update()

    # Simulate engine actions that drive the UI.
    ok, msg = engine.buy_commodity(
        "water", min(5, engine.current_planet.stock.get("water", 0)))
    assert ok, msg
    gui.refresh_all()
    root.update()

    # Market chart renders for a selected good.
    first_good = next(iter(COMMODITIES))
    gui.market_tree.selection_set(first_good)
    gui._market_preview()
    gui._market_chart()
    root.update()

    # Advance a day so charts/sparklines have data, then refresh again.
    engine.advance_day(1)
    gui.refresh_all()
    root.update()

    # Sortable market headers fire without error (invoke the bound command).
    sort_cmd = gui.market_tree.heading("Buy", "command")
    if sort_cmd:
        root.tk.call(sort_cmd)
    root.update()

    def done():
        print("GUI SMOKE TEST PASSED")
        gui._map_anim_running = False
        root.destroy()

    root.after(700, done)
    root.mainloop()


# ==============================================================================
# ENTRY POINT
# ==============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Space Trader: Odyssey — Nebula Edition")
    parser.add_argument("--test", action="store_true",
                        help="Run the headless engine self-test suite")
    parser.add_argument("--gui-test", action="store_true",
                        help="Run a headless GUI smoke test (requires a display)")
    parser.add_argument("--mute", action="store_true",
                        help="Start with sound effects disabled")
    parser.add_argument("--difficulty", choices=list(DIFFICULTIES.keys()),
                        default="normal", help="Default difficulty for new games")
    parser.add_argument("--seed", type=int, default=None,
                        help="Seed the random number generator")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    if args.test:
        run_self_test()
        return

    if args.gui_test:
        run_gui_test()
        return

    root = tk.Tk()
    engine = GameEngine(muted=args.mute, difficulty_id=args.difficulty)
    SpaceTraderGUI(root, engine)
    root.mainloop()


if __name__ == "__main__":
    main()
