# Ecolyxis — Full Functionality Investigation

Started 2026-06-06. Live prod + throwaway accounts; billing via mocks; GPU thorough/concurrent.

## Prep state (Phase 0 ✓)
- DB snapshot: `/tmp/ecolyxis_snapshot_20260606_114405.sql` (pre-investigation rollback point)
- Migration head: `006_thread_last_mode`
- Services: `ecolyxis`, `ecolyxis-worker`, `redis-server` all active
- Test accounts (password `InvTest!2026pw`, marked `invtest_` for cleanup):
  - id 20 `invtest_alice` — free
  - id 21 `invtest_bob` — free (for IDOR/authz)
  - id 22 `invtest_prem` — premium (tier=premium, status=active)

## Status legend
✓ working · ⚠ works with caveat · ✗ broken · ⏳ pending

## Phase 1 — coverage + static (✓)
- Full suite: **145 passed**, total line coverage **50%** (`pytest --cov=app`).
- Well-tested: auth 96%, models 97%, billing/webhook 88%, api/completions 85%, video 84%, jobs/handlers 84%.
- Low coverage → live-verification targets: chat/routes 27%, queue 19%, export 24%, tts 25%, dashboard 29%, wallet 32%, billing/routes 34%, webauthn 22%, images 48%, llm 44%.
- CSRF: enforced globally via `before_request` hook in app/__init__.py (per-session token, header `X-CSRFToken` or form field). Exemptions: GET/HEAD/OPTIONS, `/v1/`, `/health`, **`/billing/` (whole prefix)**.

## Findings

| Area | Status | Notes |
|------|--------|-------|
| Auth (password login/logout) | ✓ | login 303; anon → /login redirect on all protected routes |
| CSRF global enforcement | ✓ | before_request hook; header `X-CSRFToken` |
| Threads CRUD | ✓ | create→303 chat; rename(PATCH); delete; ownership 404 cross-user |
| Chat durable job (LLM) | ✓ | end-to-end "PING" persisted, keyed by job_id; worker reached LLM at 10.0.0.6:8081 |
| Thread ops | ✓ | mode (valid 204 / invalid 400), generate-title, clear, compact, progressive-compact graceful |
| Message ops | ✓ | delete 204; ownership enforced |
| Search | ✓ | premium-gated (free→403, prem→200) |
| Export (JSON/MD) | ✓ | premium-gated (free→403), empty-thread→400, fmt-validated |
| API keys | ✓ | create via one-time flash (form UI), max-keys cap, revoke/delete |
| Public /v1 API | ✓ | bearer auth (401 bad key), **402 insufficient_credits**, completions 200, usage logged, **wallet debited 1p**, rate headers |
| Wallet / billing (read) | ✓ | GET pages 200 |
| Content pages | ✓ | landing, pricing, /legal/privacy, /legal/terms, blog, contact all 200 |
| Image upload | ✓ | field `file`; cross-user thread→404 (ownership ok) |
| Blog admin | ✓ | gated to user id==1 (non-admin redirected, no post created) |
| WebAuthn / passkeys | ✗ | **501 — `webauthn` pkg not installed; passkey auth non-functional in prod** |

## Phase 3 — backends + resilience
| Backend | /health | Generation | Notes |
|---------|---------|-----------|-------|
| LLM (8081) | 200 | ✓ working | chat jobs complete + persist |
| Image (8083) | 200 | ✗ **hangs** | jobs stick at "starting", end "image generation did not complete"; direct `/generate-stream` yields no output in 20s |
| Edit (8087) | 200 | ~ | 1 historical success; not re-tested live |
| Video (8085) | 200 | ? | 0 successes ever in DB |
| TTS (8091) | 200 | ? | not exercised |
- **Concurrent queueing (live):** 3 image jobs (free+premium) all claimed onto separate worker threads simultaneously; durable-job error path handled the backend failure correctly (status→error, error event published, no partial persist). Priority-lane ordering not conclusively observable live (4 threads absorb the load) — covered by unit test instead.
- **Resilience:** reaper + redis-down drills NOT run live (would disrupt all prod users); reaper logic verified by passing unit test `test_reaper_requeues_stranded_job`.

## Phase 4 — security + data integrity
- **IDOR/authz matrix (bob→alice):** ✓ all cross-user ops denied (404/403): chat view, message, rename, mode, clear, delete-thread, export, title, system-prompt, image-job, job-status, job-stream, apikey-revoke. `/jobs/active` correctly returns each user's own jobs (owner-scoped).
- **Upload:** ✓ ownership-checked, extension-whitelisted, size-limited, server-generated UUID filenames (no path traversal).
- **Media fetch (`_save_remote_image`):** ✓ only fetches from trusted config backend; no user-controlled URL → no SSRF.
- **Stripe webhook:** ✓ signature verified (`construct_event`, 400 on bad sig) — justifies its CSRF exemption.
- **CSRF:** ✗ confirmed live — `/billing/cancel-subscription` reached its handler with NO token (the `/billing/` prefix exemption is too broad).
- **Session cookie:** ⚠ `SESSION_COOKIE_SECURE=False`, `SESSION_COOKIE_SAMESITE` unset (None).
- **Data integrity:** ✓ CLEAN — 0 orphans, 0 dangling FKs, 0 thread/job mismatches, 0 duplicate job_ids, 0 negative wallets, 0 stuck jobs; UNIQUE(job_id) on message/generated_image/generated_video (exactly-once enforced).

## Issues log (severity-ranked)
- **[HIGH] Image generation non-functional** — backend at `10.0.0.6:8083` reports `/health` 200 but `/generate-stream` produces no output; live image jobs (single + concurrent) all error "image generation did not complete". Core advertised feature down. App handles the failure correctly — this is backend/infra (model not loaded / GPU stuck). Also a **monitoring gap**: health check doesn't reflect generation capability. Video/TTS unverified (0 video successes ever in DB).
- **[MED-HIGH] CSRF hole on billing (CONFIRMED live)** — `app/__init__.py:97` exempts the whole `/billing/` prefix; `/billing/cancel-subscription` (state-changing POST) reached its handler with no CSRF token. Aggravated by `SESSION_COOKIE_SAMESITE` unset + `SECURE=False`; currently only browser-default-Lax blunts exploitation. Fix = exempt only `/billing/webhook` and set `SAMESITE='Lax'`, `SECURE=True`.
- **[MED] Session cookie not hardened** — `SESSION_COOKIE_SECURE=False` (cookie may travel over HTTP) and `SESSION_COOKIE_SAMESITE` unset. Behind HTTPS (Caddy), set `SECURE=True` + `SAMESITE='Lax'`. This is the implicit-only mitigation currently masking the billing CSRF hole.
- **[MED] Passkeys non-functional** — `webauthn` package not installed; all `/webauthn/*` register/authenticate endpoints return **501**. Feature is documented (PROJECT.md/SPEC "password auth + passkeys") but dead in prod. Fix = install `webauthn` or stop advertising. Code degrades gracefully (501, password auth unaffected).
- **[MED] requirements.txt incomplete** — missing `flask-migrate`, `psycopg2`, `alembic`, `Pillow` (all installed manually in venv but undeclared). A clean `pip install -r requirements.txt` deploy would fail at runtime (no DB driver / migrations / image lib). Also `webauthn` absent. Deploy-reproducibility risk.
- **[LOW] Dead code** — `csrf_protect` decorator in `app/csrf.py` is never applied to any route (global hook is the real mechanism). Remove or wire intentionally.
- **[LOW] Doc drift** — PROJECT.md lists LLM at `10.0.0.1:8081`; live `.env` uses `10.0.0.6:8081` (verified working via live chat job). Update doc.
- **[LOW] Admin via magic id** — blog admin gated by hardcoded `current_user.id == 1`; functional but fragile (no role flag).

## Config drift
- `.env` `LLM_BASE_URL=http://10.0.0.6:8081/v1` vs PROJECT.md documents LLM at `10.0.0.1:8081` — to confirm which is authoritative.
