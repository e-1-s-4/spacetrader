# Changelog

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
