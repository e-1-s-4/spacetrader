# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
semantic versioning.

## 2.4.0 — The audit release: 30+ fixes, a new frontier encounter, and CI

### Fixed

- **Critical — client crash on every mission-destination render.** The star-map
  dossier read `m.reward_credits` while the serializer emitted `reward`, so
  selecting (or arriving at) any planet with an active contract threw a
  `TypeError` inside `renderAll`, aborted every later renderer, and surfaced as
  a bogus "Communication error" toast with a stuck red connection banner. The
  serializer now emits both keys, and `renderAll()` is fault-isolated per
  renderer so one bad panel can never again take the whole UI down.
- **Market inventories drifted ~1.9× above design targets.** The stock
  mean-reversion smoother's weights summed to 1.4 (every sibling sums to 1.0),
  so station stockpiles inflated forever and the trade advisor overestimated
  available supply. Weights now sum to 1.0 and inventories relax onto target.
- **Encounter loot reported negative trading profit.** Derelict salvage,
  nomad-trader crates, asteroid ore, mining and comet yields, ghost-freighter
  AI cores, cargo-breach and boarding loot all entered the hold without a cost
  basis, so selling them showed "Profit: −N CR", drained renown, and blocked the
  Trade Baron achievement. All of it is now recorded at its true (usually zero)
  basis, and the trader's paid crates at their real price.
- **One corrupt Hall of Fame entry bricked all future recordings.** Loading
  validated key presence but not types; a single bad `net_worth` poisoned the
  sort inside the write path, the `TypeError` was swallowed, and the file was
  never rewritten again. Entries are now type-validated on load, the sort key
  is type-safe, and de-duplication includes the difficulty.
- **Concurrent requests could 500.** POST responses were serialized (and GET
  state dumps transmitted) while iterating live engine containers; a second
  tab mutating cargo or price history mid-dump raised "dictionary changed size
  during iteration". Snapshots are now deep-copied out of the session lock, and
  transmission happens outside the lock so a stalled client can't block other
  requests.
- **"Start New Career" was unreachable from the death screen.** The new-game
  modal stacked *under* the vessel-loss modal, whose focus trap kept yanking
  keyboard focus back. It now stacks above.
- **Contract deadlines lost their final day.** Missions failed at
  `days_left <= 0` before arrival deliveries were checked, so "within N days"
  really granted N−1, and landing on deadline day repossessed the cargo.
  Deadlines are now inclusive (and covered by a new regression test).
- **Smuggling at reputation ≥ 15 was risk-free and even paid a stipend.** High
  standing now lowers patrol detection odds (never below 30%) instead of
  granting diplomatic immunity; the smuggler perk also helps.
- **The derby could stake the captain's entire balance.** A wager above the
  advertised buy-in is now refused instead of silently clamping upward.
- **Pirates and hull breaches could steal sealed contract cargo**, making the
  contract undeliverable through no fault of the player. Transponder-locked
  goods are now off-limits; only unsealed cargo changes hands.
- **Inapplicable combat maneuvers burned the turn.** Firing a torpedo with no
  rack, evading on dead thrusters, boarding too early, or fleeing with damaged
  warp drives consumed the turn AND provoked a full enemy volley (hotkeys
  bypassed the disabled buttons). They are now free refusals, identical to the
  buttons.
- **Trade-in exploit.** Selling a battered ship back paid 70% of list price
  regardless of condition, making a downgrade → upgrade cycle a cheap full
  repair plus free refuel. Trade-in value is now prorated by hull condition.
- **Fuel estimates for senior officers were wrong.** `fuel_credit_cost` ignored
  the rank service discount and used a different floor than `buy_fuel`, so the
  advisor and GUI overstated fuel prices from Captain rank up. Both paths now
  share the same math.
- **Mission IDs could collide** (same board, same day, 4-digit suffix),
  corrupting abandon/delivery lookups. IDs now carry a unique token.
- **A new enemy raid used to fire twice** — fixed in 2.3.0 — **and stock
  histories double-appended on Market Wire shock days**, distorting the 30-day
  window. Shock days now replace the day's sample.
- **Save-version safety.** The `version` field was written but never checked;
  a save from a newer build would mis-load field by field. Newer saves are now
  refused with a clear message (older saves keep loading via defaults).
- **Perk stacking.** Hiring two officers of the same perk type silently kept
  only the first hire's value; the strongest perk now applies.
- **Test-hygiene fixes:** vacuous `or True` self-test checks made real; the
  web smoke test's temp dir no longer leaks every run; the engine suite cleans
  up even when a mid-suite failure aborts; `dismiss_combat` with no combat no
  longer claims a battle is raging.
- **UI details:** the market table's empty-state `colspan` matched its real 8
  columns; the previously-undefined `.dmg-red` class exists so combat damage
  floats red; the HUD security pill and dossier readout colour by actual
  threat level instead of hardcoded green; blank captain names are trimmed to
  the default; the star-map ping animates via transform (works on engines that
  can't animate SVG geometry); audio unlocks from a real user gesture (Safari's
  strict autoplay policy could permanently mute the game); keyboard focus is
  restored after re-renders that replace the focused element.

### Added

- **Uncharted Cache — a 14th deep-space encounter.** A shielded drop pod bolted
  to a nameless rock: crack it open for bearer chips, contraband, AI cores, or
  a fellow runner's courtesy stash — or eat a booby trap, trip a decoy
  transponder, or find it already gutted. Odds shift with local crime level,
  and a new **Cache Hunter** achievement rewards three clean finds.
- **Logistics Director achievement** for completing 15 contracts (31 total),
  and the Career tab's achievement counter is now live instead of hardcoded.
- **Net Worth Trend chart** in the Career tab: a dependency-free SVG sparkline
  of the last 30 days with start/today/peak callouts.
- **GitHub Actions CI** (`.github/workflows/ci.yml`): compiles, version-checks,
  runs both built-in suites, the 67-test regression suite, and the jsdom
  browser smoke test on Python 3.8–3.13 × Node 20 — the pipeline the README
  always promised.
- Encounters now autosave on resolution, so quitting after a find keeps it.

## 2.3.0 — Correctness, honest numbers, and an accessible client

### Fixed

- **Post-death soft-lock (critical).** A destroyed ship left the player stuck behind
  the tactical combat overlay. The career-over gate refused `dismiss_combat`, so the
  finished encounter was never cleared from the session; the overlay paints above the
  game-over modal, which meant the only recovery buttons on the screen were unreachable.
  The combat dismissal is now always permitted, the game-over modal stacks above the
  overlay, and the overlay's primary button routes a destroyed vessel to the recovery
  screen.
- **The test suite no longer contradicts the engine.** `to_int()` rejects fractional
  values on purpose (2.2.0); the regression suite still expected them to be truncated, so
  `npm test` failed out of the box. The suite now asserts the documented behaviour.
- **The trade advisor now quotes what the trade actually pays.** It ignored the station's
  market-depth fee, charged only one leg of fuel instead of both, and divided profit-per-day
  by one leg's travel time — over-stating profit by roughly 10% and profit-per-day by up to
  3.7×. Routes are now priced with the engine's own quoting helpers, both fuel legs are
  charged, both legs' days are counted, the margin floor is proportional rather than a flat
  3 CR, and routes that lose money are no longer advertised.
- **A defensive raider is no longer an impassable shield wall.** It skipped its attack
  entirely while recharging, handing the player a free turn, and rebuilt shields faster
  than any single weapon could strip them. It now returns fire (softly) while charging, and
  venting its emitter bank now *wrecks* the emitters so the shield can never cycle back up.
  A single railgun against a defensive behemoth went from 1 win in 20 to 20 in 20.
- **Insurance no longer strands contracts.** A respawn after an insured wreck vaporised
  the hold while leaving the affected contracts on the log, where they could never be
  delivered and quietly expired later. Contracts whose cargo went down with the ship are
  now voided and reported.
- **Contract cargo carries a real cost basis.** The hold readout fell back to the live
  market price, so the profit shown for contract goods was meaningless. Purchases and
  contract cargo now share one blending routine.
- **The starter hull is worth something.** The Star Sparrow had a listed price of zero, so
  it contributed nothing to net worth and traded in for nothing. It now has a real listed
  price, which makes "70% of listed price" meaningful.

### Added

- GitHub Actions CI running compilation, both built-in suites, the regression suite and the
  browser smoke test on Python 3.8 through 3.13, for pushes, pull requests and manual runs.
- An MIT `LICENSE` and a `CONTRIBUTING.md` describing the house rules.
- A full cost breakdown in the Trade Advisor — purchase, market-depth fee, fuel for both
  legs — plus a note that prices drift while you are in transit.
- Browser accessibility: every overlay is a labelled `role="dialog"` with a focus trap
  that hands focus back on close, `Escape` closes dismissable dialogs, the tab strip
  implements the ARIA tabs pattern with arrow-key navigation, the star map is fully
  keyboard-operable, the combat log and toasts announce themselves to screen readers,
  icon-only buttons carry accessible names, and the mute preference survives a reload.
- A regression test for every fix above, at engine, HTTP and browser level.

### Changed

- `package.json` version is synchronised with the application version, `npm test` now runs
  all four suites, the meaningless `build` script is gone, and the lockfile is committed.
- The `Server` header and `/api/health` version now derive from `APP_VERSION` instead of a
  hard-coded literal, so they cannot drift.
- The deep-scanner self-test now inspects a genuinely remote world; it previously scanned
  the player's own station, which made the assertion pass vacuously.
- The stray `metadata.json` was removed — nothing in the repository referenced it and its
  schema belonged to another project.

## 2.2.0 — Reliability, hardening, and repository automation

- Reject fractional numeric API input instead of silently truncating it; preserve accepted integral numeric strings.
- Require state-changing requests to use the exact `application/json` media type.
- Validate browser `Origin` values when supplied and reject cross-site fetches.
- Reject duplicate/missing `Content-Length`, unsupported `Transfer-Encoding`, invalid body framing, and payloads over 64 KiB before parsing JSON.
- Add clickjacking, MIME-sniffing, referrer, cross-origin, and browser-permission response protections; hide the Python runtime version.
- Bound client socket wait time and use a small request backlog for the threaded local server.
- Make Web Audio initialization and playback best-effort so unsupported or blocked audio does not interrupt gameplay.
- Validate port values, add `--version`, and expose the app version through `/api/health`.
- Extend the built-in engine and HTTP smoke tests for the new input, framing, origin, size, and header checks.
- Add GitHub Actions CI and ignore personal save files and local Python artifacts.
- Refresh usage, safety, and test documentation while retaining the no-dependency, single-file game architecture.

## 2.1.0 — Deep-space encounter pass

- Added thirteen deep-space encounter types, a tactical combat bridge with subsystem
  targeting, boarding actions, drones and missiles, a trade advisor with risk ratings, a
  persistent Hall of Fame, and faction reputation.
