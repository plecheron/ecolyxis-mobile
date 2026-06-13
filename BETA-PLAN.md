# Ecolyxis Beta Plan

*2026-06-13. Roadmap for the next beta release, grounded in current open issues (#100–#113), improvement docs, and the feature surface.*

---

## Current State

- **Last commit**: `f2bf981` — cascade delete fix + 33 new tests (162 total)
- **Servers**: web1, web2 (Caddy → gunicorn), gpu1, gpu2 (P40 Tesla), db1 (PostgreSQL), controller, api
- **Features shipped**: chat modes, workspaces, image/video/edit generation, wallet + Stripe, public `/v1` API, TTS, PWA, export, passkeys, search, admin
- **Known gaps**: no HTTPS, no monitoring, workspace authz bug, no CI, single-Redis SPOF

---

## β2 Theme: **Production Hardening**

Ship a version that's safe to put real users on — HTTPS, monitoring, reliable deploys, and no authz holes.

### P0 — Must-ship (security & reliability)

| # | Issue | What | Effort |
|---|-------|------|--------|
| 1 | #98 | **HTTPS end-to-end.** Caddy auto-TLS on the VPS → flip `SESSION_COOKIE_SECURE=1`. Login cookies and chat content currently travel in plaintext. | S (config change) |
| 2 | #111 | **Workspace user-scoping.** Any authenticated user can view/edit/delete any workspace. Add `user_id` filter to all workspace queries. | S (routes fix + tests exist) |
| 3 | #113 | **Verify cascade fix on PostgreSQL.** The chat deletion fix was tested on SQLite only. Confirm FK constraints pass on db1. | XS (manual test) |
| 4 | #112 | **Fix deploy process.** `kill -USR2` doesn't reload code. Switch to `systemctl restart ecolyxis` in deploy scripts (or add post-merge hook). | S |
| 5 | UI #1 | **Sanitize markdown rendering.** `marked.parse()` → innerHTML with no DOMPurify. LLM output can inject `<script>`. XSS on every thread load. | S (add DOMPurify) |

### P1 — Should-ship (observability & infrastructure)

| # | Issue | What | Effort |
|---|-------|------|--------|
| 6 | #100 | **Monitoring & alerting.** Wire `/health` to alerts: worker heartbeat staleness, queue depth, error rate. Revive `admin/metrics.py` or use simple cron + webhook. | M |
| 7 | #107 | **CI pipeline.** 162 tests run in ~20s, no GPU/Redis needed. GitHub Actions on push to master — fail the build, block merging. | S |
| 8 | #102 | **Shared uploads storage.** `uploads/` is on web1 local disk. Web2 serves 404s for any generated content. Move to NFS or S3-compatible. | M |
| 9 | #103 | **PostgreSQL backups.** No automated `pg_dump`. Add daily cron on db1 or controller with retention + offsite copy. | S |
| 10 | #101 | **Redis resilience.** Single instance on web1 = SPOF for all durable jobs. At minimum: document the blast radius. Ideally add replication or move to db1. | M |

### P2 — Nice-to-have (feature & UX polish)

| # | Issue | What | Effort |
|---|-------|------|--------|
| 11 | UI #2 | **Self-host CDN scripts.** marked.js + highlight.js loaded from jsdelivr at floating version — supply chain risk + PWA can't precache. Pin + self-host. | S |
| 12 | UI #3 | **Break up chat.html monolith.** 3,260-line inline `<script>`. Extract to `static/js/chat/` modules. Enables caching, linting, testing. | L |
| 13 | #106 | **Raise test coverage.** chat/routes 27%, wallet 32%, dashboard 29%. Target ≥60% on thin modules. | M |
| 14 | #104 | **Log rotation + cleanup job scheduling.** No logrotate, cleanup.py not in any cron/systemd timer. | S |
| 15 | #108 | **Nightly benchmark run.** `benchmark/` suite exists, not scheduled. Cron on controller against live API. | S |
| 16 | FEAT #1 | **Sustainability dashboard.** Brand differentiator — show Wh/CO₂e per thread vs cloud baseline. Data exists in `Message.tokens_used`. | M |
| 17 | FEAT #2 | **Image generation via `/v1` API.** `/v1/images/generations` backed by existing job path. New revenue from built infra. | M |
| 18 | FEAT #3 | **Document upload for workspace context.** Currently images-only. Allow PDF/txt → extract text into workspace context window. | L |

---

## Suggested Release Order

1. **P0 items 1–5** — security blockers, do first
2. **P1 items 6–7** — CI + monitoring, prevents regressions going forward
3. **P1 items 8–10** — infrastructure reliability
4. **P2 items 11–12** — frontend hardening
5. **P2 items 13–15** — test coverage & ops
6. **P2 items 16–18** — new features for the next cycle

---

## Decisions Needed

- **HTTPS approach**: Caddy auto-TLS on the VPS (easiest), or terminate TLS at a different edge?
- **Shared uploads**: NFS mount from controller/db1, or migrate to object storage (MinIO on controller)?
- **Redis strategy**: Add replica on db1, or accept SPOF and focus on monitoring/alerting instead?
- **β2 scope**: Ship P0 only as a hotfix, or bundle P0 + P1 as the beta release?
- **Version numbering**: Start formal versioning (e.g. v0.2.0-beta), or keep commit-based?
