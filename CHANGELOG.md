# Changelog
## v0.8.0-beta (2026-06-16) — "Mission Control"

### Main App (ecolyxis)

#### Carbon Offsets & Sustainability
- **Carbon offset tracking**: Log carbon capture purchases (e.g., 1 tonne CO₂e DAC)
  and tree planting events. CO₂ reclaimed shown separately from savings on
  all display surfaces (landing page, sustainability dashboard, API).
- **Tree calculation**: Conservative 21 kg CO₂/year per tree (EPA/UK forestry avg),
  live from purchase date, capped at 40-year lifetime (840 kg/tree).
- **Carbon capture**: Full amount recognized immediately at purchase.
- **Migration 012**: `carbon_offset` table for offset records.
- **Migration 013**: `is_banned` column on User table.

#### Admin Integration
- **Feature flags**: `admin_integration.py` reads from `admin_feature_flag` table
  with 30-second in-process cache. `@feature_required` decorator gates routes.
- **Ban enforcement**: Before-request hook checks `is_banned` on authenticated
  users, logs them out and redirects with flash message. Exempt paths configured.
- **Audit webhook**: `/admin/audit-ingest` endpoint receives events from the
  admin dashboard, authenticated via `ADMIN_AUDIT_KEY` env var.

#### Code Quality
- Fixed `test_v061_beta.py`: Replaced hardcoded `/opt/Ecolyxis` paths with
  `REPO_ROOT`-relative paths for portable local testing.
- 8 new admin integration tests (feature flags, ban check, audit endpoint,
  CarbonOffset model). Total: 664 passing + 44 admin dashboard tests.

### Admin Dashboard (ecolyxis-admin) — "Mission Control"

#### Already at v0.9.0-beta on GitHub (`plecheron/ecolyxis-admin`)

**Tier 1 — Foundation & Security:**
- Session-based auth with 2FA (TOTP + backup codes)
- CSRF protection (Flask-WTF)
- Rate limiting (Flask-Limiter)
- IP allowlist support
- Secret extraction to `.env` (python-dotenv)
- SSH connection pooling (ControlMaster)
- HTTPS via Caddy at `admin.ecolyxis.co.uk`

**Tier 2 — Infrastructure Control:**
- Remote service management (start/stop/restart across all VMs)
- VM management via KVM/virsh (start/stop/force-off)
- Deploy pipeline (pull, migrate, restart, smoke test, rollback)
- Log viewer (journalctl tail + live SSE streaming)
- Backup management (status, manual trigger, history)

**Tier 3 — Application Management:**
- User management (view, ban/unban, change tier, reset password)
- Sustainability dashboard integration (energy/CO₂e from GPU telemetry)
- Feature flags (toggle features without redeploying)
- Alert notifications (Telegram, webhook, email)
- Uptime Kuma integration
- Tests runner (remote pytest via SSH)
- Auto-remediation (disk cleanup, service restart)
- Prometheus export endpoint
- Carbon offset CRUD management
- WireGuard topology viewer

## v0.5.0-beta (2026-06-13)

### Tier 1 — DB Hardening
- Physical replication slot `db2_standby` on db1 (WAL retention)
- Synchronous replication enabled (zero data loss, sync_state=sync)

### Tier 2 — Coverage → 82%
- Admin module tests: 0% → 80% (33 new tests)
- Health, WebAuthn, Worker, API routes gap-closing tests (+37 tests)
- Total: 533 tests, 82% coverage (was 444 tests, 72%)

### Tier 3 — New Features
- **Conversation sharing**: public read-only links at /s/<id> with create/revoke UI
- **Usage analytics dashboard**: 30-day token chart, message/job/wallet stats at /analytics
- **Multi-model selector**: GET /api/models with 5 tiers (standard/quick/long/precise/vision)
- API key management UI (pre-existing, confirmed working)



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
