# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
semantic versioning.

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
