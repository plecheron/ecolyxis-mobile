# Changelog

All notable changes to Ecolyxis will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
with a `-beta` suffix during the beta phase.

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
