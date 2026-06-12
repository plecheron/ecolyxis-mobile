# Ecolyxis — Suggested Improvements

*2026-06-12. Replaces the previous list, which was almost entirely done (CSRF,
health endpoint, migrations, Redis, systemd units, tests, export, pricing page,
secrets in `.env`, rate limiting). Items below are verified against the current
code, the 2026-06-06 investigation (`INVESTIGATION.md`), and the fixes that
landed since (#86–#97).*

## High priority

1. **Serve HTTPS end-to-end and flip `SESSION_COOKIE_SECURE=1`** (#98). Caddy still
   listens on `:80` only, and `.env` does not set `SESSION_COOKIE_SECURE`, so
   login cookies and chat content travel in plaintext over the public
   internet — for a live product taking Stripe payments. `config.py` already
   gates the flag and documents the cutover (`config.py:28-37`); the work is
   getting a cert onto the edge (Caddy auto-TLS on the VPS or the gateway),
   then setting the env var. Everything else security-wise from the
   investigation has been fixed; this is the last big one.

2. **Make health checks reflect generation capability, not just process
   liveness** (#99). During the investigation, the image backend returned `/health`
   200 while every generation job hung and failed; video has **zero successful
   generations ever** in the DB. `app/health.py` only pings `ECOLYXIS_API_URL
   /health`. Add a synthetic canary — a periodic tiny generation per kind
   (image/video/edit) via the normal job path — and surface per-kind
   pass/fail + last-success timestamp in `/health`. Until then, two of the
   four advertised media features can be silently down for weeks.

3. **Add monitoring and alerting** (#100). `/health` exists but nothing watches it.
   Alert on: `/health` degraded, worker heartbeat staleness (the reaper's
   signal already exists in `app/jobs/worker.py`), Redis queue depth, job
   error rate, and disk usage. A `metrics.py` already sits in
   `app/admin.disabled/` — revive it or wire basic metrics into the admin
   controller service.

## Reliability & architecture

4. **Remove Redis as a single point of failure** (#101). Redis on web1 holds the job
   queue, event streams, and claim lists — if web1's Redis dies, all durable
   generation stops and in-flight job events are lost. At minimum enable AOF
   persistence; better, move Redis to its own host (or add a replica) so web1
   and web2 are symmetric. Also worth a deliberate answer: what should the app
   do when Redis is unreachable (fail the POST? fall back to the legacy
   path?) — today that's untested (`INVESTIGATION.md` Phase 3 note).

5. **Decide web2's role and fix local-disk uploads** (#102). `uploads/` (80 MB,
   growing) lives on web1's local disk. If web2 (192.168.122.221) ever serves
   traffic, every generated image/video 404s there. Either retire web2,
   or move media to shared/object storage. While at it: nothing prunes
   orphaned uploads — add that to `cleanup_expired.py`.

6. **Automate PostgreSQL backups** (#103). The investigation's rollback point was a
   manual `pg_dump` to `/tmp`. Schedule dumps (or WAL archiving) on
   ecolyxis_db1 with retention and off-host copies, and test a restore once.
   Wallet balances and Stripe transaction records live in this DB.

7. **Make the cleanup job's scheduling discoverable, and rotate logs**
   (#104). `cleanup.log` is being written (so something runs `cleanup_expired.py`),
   but there's no systemd timer and no user crontab entry — whatever schedules
   it is invisible to the next operator. Move it to a systemd timer next to
   the units in `deploy/`. Separately, neither `cleanup.log` (2.4 MB) nor app
   logging has rotation; add `logrotate` configs or `RotatingFileHandler`.

## Code quality

8. **Retire the legacy generation paths** (#105). `JOBS_ENABLED=1` is live, so after
   a soak period delete: the legacy Postgres LISTEN/NOTIFY queue
   (`app/queue.py` + `LLMQueueEntry` model + `instance/queue.db`), the legacy
   in-request SSE fallbacks woven through the chat blueprint
   (`app/chat/images.py` is 923 lines and `routes.py` 832 largely because both
   paths coexist), and `app/admin.disabled/`. This roughly halves the chat
   blueprint and removes the most confusing code in the repo. Keep the
   TTS direct path — it's intentionally legacy (read-aloud fetch).

9. **Raise test coverage on the thin modules** (#106). Overall line coverage is 50%;
   auth (96%), models (97%), and billing webhook (88%) are solid, but
   chat/routes 27%, queue 19%, export 24%, tts 25%, dashboard 29%, wallet 32%.
   Wallet especially — it moves money. (Deleting the legacy paths in #8
   improves this for free.) Also do one live passkey register/login check:
   `webauthn` is installed now, but the feature returned 501 in prod as of
   2026-06-06 and the fix (#89–#97) hasn't been verified end-to-end.

10. **Add CI and script the deploy ritual** (#107). The hermetic pytest suite (148
    tests, ~20 s, no GPU/Redis needed) is ideal CI material, but nothing runs
    it automatically. Add a CI job on push, and turn the documented manual
    ritual (pytest → `flask db upgrade` → restart `ecolyxis` +
    `ecolyxis-worker`) into a single `deploy/deploy.sh` so a step can't be
    forgotten — restarting web but not the worker after a handler change is an
    easy silent failure.

11. **Run the benchmark suite on a schedule** (#108). `benchmark/` is a deterministic
    intelligence benchmark against the live `/v1` API with no LLM judge —
    perfect for a nightly run to catch model/backend regressions (quantization
    changes, llama.cpp upgrades, GPU issues). Today it only runs when someone
    remembers it exists. Pairs naturally with #2 and #3.

## Product

12. **Either make video generation work or unship it** (#109). Zero successes ever
    in the DB, yet it's an advertised feature with a UI mode. Once the canary
    from #2 exists you'll know within a day whether the WAN 2.2 backend is
    fixed; until it's reliable, hiding the mode beats charging premium users
    for guaranteed failures.
