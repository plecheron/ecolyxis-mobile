# Changelog

All notable changes to Ecolyxis will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with a `-beta` suffix during the beta phase.

## [0.4.0-beta] - 2026-06-13

### Testing
- **Test coverage raised from 49% to 72%** (181 → 444 tests).
- New test suites for chat routes, LLM client, dashboard, TTS, export,
  summaries, billing, Redis client, jobs API client, jobs routes, worker,
  workspace routes, video routes, blog, and WebAuthn.
- **Bug fix**: `compact_save` route was missing `thread_id` on Message
  creation — would crash on every full conversation compact.

### PWA
- **Offline support**: Service worker v11 with offline fallback page,
  stale-while-revalidate caching for static assets, network-first for
  navigation requests.
- **Installable**: Maskable icons, `display_override`, `beforeinstallprompt`
  capture for custom install button. Apple-touch-icon and favicon added.
- Manifest updated with `scope`, `id`, and `display_override`.

### Mobile UX
- **Pull-to-refresh disabled** via `overscroll-behavior-y: contain`.
- **iOS auto-zoom fixed**: All input/textarea font-sizes set to ≥1rem (16px).
- **Smooth scrolling**: `touch-action: pan-y` on chat messages during streaming.
- **Sidebar drawer animation**: CSS transition on transform for mobile slide-in.

### Infrastructure
- All 7 stale GitHub issues (#100–#108) closed with commit references.
- Video generation UI disabled (Wan2.2 backend non-functional).
- WebAuthn passkeys confirmed functional (webauthn 2.7.1).

## [0.2.0-beta] - 2026-06-13

### Security
- **XSS fix**: All LLM markdown output now sanitized through DOMPurify before
  rendering. Previously, `marked.parse()` output was inserted via `innerHTML`
  with no sanitization, allowing script injection from model responses.
- **Self-hosted CDN scripts**: `marked.js` (pinned v12.0.2) and `DOMPurify`
  (v3.1.6) are now served locally from `/static/js/` instead of jsdelivr CDN.
  Eliminates supply-chain risk and enables PWA precaching.

### Fixed
- **#111**: Workspace routes now properly scope all queries by `current_user.id`.
  Any authenticated user could previously access any workspace.
- **#113**: Cascade delete (Thread → Messages, GeneratedImage, GeneratedVideo,
  GenerationJob) verified working on production PostgreSQL.
- **#112**: Deploy process now uses `systemctl restart` via `deploy.sh` instead
  of unreliable `kill -USR2` which didn't reload code.

### Infrastructure
- **Formal versioning**: Added `VERSION` file and this changelog.
- **Deploy script**: `deploy.sh` with optional `--test` flag and health check.
- **HTTPS verified**: Caddy auto-TLS on VPS edge with `SESSION_COOKIE_SECURE=1`,
  `Secure; HttpOnly; SameSite=Lax` cookies, HTTP→HTTPS 308 redirect.

## [0.1.0] - 2026-06-12

Initial beta release with chat modes, workspaces, image/video/edit generation,
wallet + Stripe, public API, TTS, PWA, export, passkeys, search, admin panel.

## [0.3.0-beta] - 2026-06-13

### Infrastructure
- **#100** Monitoring & alerting: systemd timer checks health, Redis, queue depth, worker count every 60s
- **#101** Redis HA: web2 now replicates web1 (live replica), manual failover script installed
- **#102** Shared uploads: NFS mount web1→web2, orphan pruning script (weekly cron, found 67 orphans/61MB)
- **#103** PostgreSQL backups: daily 3am cron on db1, 7-day retention + 4 weekly snapshots
- **#104** Log rotation: logrotate on both servers (14-day app, 6-month backup), journald capped 100M

### Quality
- **#106** Test coverage: +19 tests (wallet routes: 7, chat routes: 12). Suite now 181 tests passing
- **#107** CI pipeline: multi-version Python (3.13+3.14), strict ruff, pip-audit, coverage reporting with 40% floor
- **#108** Nightly benchmark: automated LLM latency/throughput benchmark, results logged to JSONL
