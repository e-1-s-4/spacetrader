# Changelog

## 2.1.0 — Hardening & rules audit

### Exploits and game-logic bugs fixed
- **Infinite money**: `buy_fuel` / `repair_hull` accepted negative amounts, paying the player and driving fuel/hull below zero. All amount-taking actions now reject non-positive input.
- **Risk-free arbitrage**: trader officer / rank perks shifted both prices the same way, making an instant buy→sell at one station profitable. Perks now narrow the spread (floor 1%) and large orders pay a market-depth fee. Verified unprofitable across 56,400 reachable market states.
- **Free contract cargo**: contract goods could be sold at market and the contract abandoned/expired. Contract cargo is now sealed and repossessed on expiry.
- **Runaway savings**: 0.8%/day uncapped interest let idle cash win the game. Now 0.2%/day on the first 60,000 CR.
- **Downgrade destroyed value**: buying a cheaper ship discarded the trade-in surplus (~245k CR in the worst case). The difference is now refunded; missiles are refunded when the launcher is dropped; new hulls arrive with healthy subsystems.
- Selling a cargo expander with a full hold left cargo above capacity.
- Forced asteroid detour announced hull damage that was never applied.
- Wormhole arrivals didn't count toward the "visit every system" achievement.
- Combat: a drone kill no longer wastes a missile/volley; remaining weapons stop firing at a wreck; an insured "death" no longer writes a Hall of Fame entry (and no longer blocks the real one later).

### Server / security
- `1e999` (→ `inf`) caused an uncaught `OverflowError`; all numeric input is now strictly parsed.
- Table-driven, HTTP-independent `handle_action()` replaces the 300-line `if/elif` chain; exceptions return JSON 500.
- Trading, travelling or saving during a battle or pending encounter is refused server-side.
- Removed wildcard CORS; added same-origin JSON-only writes, `Sec-Fetch-Site` check, body-size limit, CSP, `nosniff`.
- Session creation is thread-safe; default bind is localhost.
- Saves and the Hall of Fame are written atomically; `load_game` rolls back on failure and sanitises tampered/legacy saves.

### Client
- Request locking (no double-submitted purchases/jumps), key auto-repeat and Ctrl/Cmd-shortcut guards (Ctrl+R used to fire "Recharge" mid-battle).
- Revision-stamped state: stale poll responses are discarded and unchanged state isn't re-rendered.
- Error toasts, connection-lost banner, live trade quotes that match the server to the credit, sealed-cargo display, refund display for downgrades, reduced-motion and focus styles.

### Tests / repo
- Added `tests/` (46 regression, property, fuzz and HTTP tests), a jsdom UI smoke test, GitHub Actions CI and `.gitignore`.
