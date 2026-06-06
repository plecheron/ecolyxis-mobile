# Ecolyxis — Project Documentation

## What Is Ecolyxis?

Ecolyxis is a **sustainable AI chat platform** — a web-based LLM chatbot service branded around eco-friendly computing. It runs on last-generation hardware (Tesla P40 GPUs) powered by green energy, positioning itself as a carbon-neutral alternative to mainstream AI chat services. Alongside text chat it offers image generation, image editing, video generation, and text-to-speech.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3 |
| Framework | Flask + SQLAlchemy + Flask-Login + Flask-Migrate |
| Web server | Gunicorn (4 workers, 32 threads, 1800s timeout) → `:8000` |
| Worker | `ecolyxis-worker` — dedicated process running generations (4 threads) |
| Reverse Proxy | Caddy (HTTP `:80` → Gunicorn `:8000`) |
| Database | PostgreSQL (via `DATABASE_URL`) |
| Job queue + event log | Redis (localhost, password-protected) |
| LLM | Qwen3.6-35B-A3B (Q4_0) via llama.cpp OpenAI-compatible API at `10.0.0.6:8081` |
| Image Gen | HiDream (`10.0.0.6:8083`) + Step1X-Edit image editing (`10.0.0.6:8087`) |
| Video Gen | WAN 2.2 (`10.0.0.6:8085`) |
| TTS | Qwen3-TTS (`10.0.0.6:8091`) |
| Payments | Stripe (live mode) — subscriptions + credit top-ups |
| Frontend | Jinja2 templates + EventSource (SSE) streaming |
| PWA | Service worker + manifest.json |

## Architecture

Generation is **decoupled from the client connection**. The web tier only enqueues jobs and streams events; a dedicated worker owns the GPU connections and runs the work to completion regardless of whether the client (or the web tier) is still around.

```
Internet → VPS (77.68.76.216) → WireGuard → Caddy :80 → Gunicorn :8000 (web)
                                                              │
            POST /jobs/<kind>/<thread>  ──enqueue──►  Redis priority queue (premium/free lanes)
            GET  /jobs/<id>/stream  ◄──XREAD events──  Redis Stream  job:<id>:events  (durable, TTL ~1h)
                                                              ▲ XADD token/progress/result events
                                          ecolyxis-worker ────┘ (claims job, owns GPU connections)
                                                              │ final artifact
                                                              ▼
                                          PostgreSQL  +  GPU backends:
                                            LLM 10.0.0.6:8081 · media 10.0.0.6 (image/video/edit/tts)
```

**Why this matters — connection-drop resilience.** A dropped connection, tab reload, or even a web/worker restart never costs the customer their work:

- **Submit ≠ stream.** A POST creates a durable `GenerationJob`, enqueues it, and returns a `job_id` immediately. Streaming is a separate resumable `GET /jobs/<id>/stream`.
- **Resumable SSE.** Each event is appended to a Redis Stream with a sequence id; the browser's `EventSource` reconnects with `Last-Event-ID` and the server replays only the missed tail.
- **Exactly-once persistence.** The worker writes the final artifact (Message / GeneratedImage / GeneratedVideo) keyed by `job_id` (UNIQUE), so retries never duplicate.
- **Crash recovery.** Jobs are claimed via an atomic `LMOVE` into a per-worker processing list; a reaper re-enqueues any job stranded by a dead worker (detected via a process-level heartbeat).

A `JOBS_ENABLED` flag gates the cutover; the legacy in-request SSE paths remain as a fallback when it is off. (TTS is intentionally still on the legacy path — it is a read-aloud fetch, not a persisted generation.)

## Database Models

- **User** — accounts (password auth + passkeys), premium/subscription status, credit wallet
- **Thread** — conversation containers per user (custom system prompt for premium); `last_mode` remembers the chat/media mode last selected in the thread, restored on load
- **Message** — user/assistant messages; `job_id` links assistant messages to their producing job (UNIQUE); `reasoning_tokens` records how many "thinking" tokens preceded the answer (count only — reasoning text is never stored)
- **GenerationJob** — durable lifecycle record for every async generation (`kind`: chat/image/video/edit/upscale/animate; `status`, `params`, `result`, `worker_id`, `heartbeat_at`); the source of truth, with the live event stream in Redis
- **GeneratedImage** — generated/edited/upscaled images (seed, size, upscale lineage); `job_id` (UNIQUE)
- **GeneratedVideo** — generated/animated videos; `job_id` (UNIQUE)
- **WebAuthnCredential** — passkey support
- **ApiKey / ApiUsage** — programmatic API keys + per-key usage logging
- **Wallet / Transaction** — credit-based billing
- **RateLimitBucket** — DB-backed token-bucket rate limiter (API)
- **LLMQueueEntry** — legacy Postgres LISTEN/NOTIFY queue (used by the legacy in-request path; superseded by the Redis queue)
- **Post** — blog posts

## Features

- User signup/login with password hashing + WebAuthn passkeys
- Multi-thread chat with resumable SSE streaming (durable jobs)
- **Per-thread mode memory** — each thread remembers its last selected mode (quick/standard/long/precise/image/edit/video/vision) server-side and restores it on load
- **Live thinking-token counter** — while the model reasons, chat shows a running "Thinking… N tokens" count that collapses to a "Thought for N tokens" chip when the answer begins (reasoning text is never shown or stored)
- **Cross-thread generation status** — threads generating in the background show a pulsing sidebar indicator, and opening any actively-generating thread resumes its live stream (server truth via `GET /jobs/active`, so it works across tabs/devices)
- Image generation, image editing, image upscaling, and video generation — all via durable jobs (survive disconnects)
- Text-to-speech read-aloud for messages
- Free tier with rate limiting (5 messages/hour); premium subscriptions via Stripe
- Credit-based wallet (top-ups) + public OpenAI-compatible API (`/v1/chat/completions`)
- Thread compaction/summarization for long conversations
- Conversation export (JSON / Markdown, premium)
- Blog, contact, legal, and pricing pages; PWA (installable)
- Admin dashboard runs as a standalone controller service (not in this app)

## Server Layout

| Component | Where | Role |
|-----------|-------|------|
| ecolyxis_web1 | 192.168.122.162 | Flask app (`ecolyxis`) + worker (`ecolyxis-worker`) + Redis (this server) |
| ecolyxis_web2 | 192.168.122.221 | Secondary web server |
| ecolyxis_db1 | 192.168.122.163 | PostgreSQL |
| gpu-manager (GPU) | 10.0.0.6 | LLM inference (Qwen, :8081) + Image (HiDream/Step1X-Edit), video (WAN 2.2), TTS (Qwen3-TTS) |
| VPS | 77.68.76.216 | Public gateway / WireGuard entry |

## File Structure

```
/opt/Ecolyxis/
├── run.py                  # Web entry point (Gunicorn → run:app)
├── worker.py               # Worker entry point (python worker.py)
├── config.py               # Config (DB, Redis, Stripe, LLM/media URLs, JOBS_ENABLED)
├── cleanup_expired.py      # Expired-data cleanup (hourly cron)
├── app/
│   ├── __init__.py         # Flask app factory
│   ├── models.py           # SQLAlchemy models (incl. GenerationJob)
│   ├── redis_client.py     # Shared Redis client
│   ├── llm.py              # LLM client (streaming)
│   ├── jobs/               # Durable job system
│   │   ├── __init__.py     # Redis priority queue + per-job event log
│   │   ├── routes.py       # POST /jobs/<kind>/<thread>, GET /jobs/<id>, GET /jobs/<id>/stream, GET /jobs/active
│   │   ├── worker.py       # Claim/run loop, heartbeat, crash-recovery reaper
│   │   └── handlers/       # chat.py + media.py (image/upscale/edit/video/animate)
│   ├── chat/               # Chat blueprint (+ legacy in-request media/SSE fallback)
│   ├── auth/ · api/ · billing/   # Auth (passkeys), public /v1 API, Stripe
│   ├── apikeys.py · wallet.py · dashboard.py · blog.py · legal.py
│   ├── contact.py · pricing.py · health.py · csrf.py · queue.py
│   ├── templates/          # Jinja2 templates (chat.html drives the job EventSource client)
│   └── static/             # CSS, JS, PWA assets
├── migrations/             # Flask-Migrate/Alembic (004 = generation_job + job_id links; 005 = Message.reasoning_tokens; 006 = Thread.last_mode)
├── deploy/                 # systemd units: ecolyxis-worker.service, secret-key.conf drop-in
├── uploads/                # Generated/uploaded media
└── venv/                   # Python virtual environment
```

## Operations

- **Services:** `ecolyxis.service` (web) and `ecolyxis-worker.service` (worker), both systemd, both reading config/secrets from `/opt/Ecolyxis/.env`.
- **Redis:** local, `requirepass` set, bound to localhost; `REDIS_URL` (with password) in `.env`.
- **Secrets:** `SECRET_KEY` is sourced from `.env`; the `secret-key.conf` systemd drop-in (`UnsetEnvironment=SECRET_KEY`) stops the unit from injecting a weak value.
- **Deploying code:** restart `ecolyxis` (Jinja caches templates) and/or `ecolyxis-worker` (to load new handlers) after pulling changes.

## Last Updated

2026-06-05
