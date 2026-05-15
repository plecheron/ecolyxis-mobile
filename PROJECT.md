# Ecolyxis — Project Documentation

## What Is Ecolyxis?

Ecolyxis is a **sustainable AI chat platform** — a web-based LLM chatbot service branded around eco-friendly computing. It runs on last-generation hardware (Tesla P40 GPUs) powered by green energy, positioning itself as a carbon-neutral alternative to mainstream AI chat services.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3 |
| Framework | Flask + SQLAlchemy + Flask-Login |
| App Server | Gunicorn (3 workers, 2 threads each) |
| Reverse Proxy | Caddy (HTTP :80 → Gunicorn :8000) |
| Database | PostgreSQL 17 (via PgBouncer on 10.0.0.4:6432) |
| LLM | Qwen 3.6-35B-A3B (Q4_0) via LM Studio OpenAI-compatible API |
| Vision | HiDream model at 10.0.0.1:8083 |
| Payments | Stripe (live mode) — subscriptions + credit top-ups |
| Frontend | Jinja2 templates + HTMX + SSE streaming |
| PWA | Service worker + manifest.json |

## Architecture

```
Internet → VPS (77.68.76.216) → WireGuard → Caddy :80 → Gunicorn :8000 → Flask
                                                                    ↓
                                                            PostgreSQL (10.0.0.4)
                                                            LLM API (10.0.0.1:8081)
```

## Database Models

- **User** — accounts with password auth, premium status, credit wallet
- **Thread** — conversation containers per user
- **Message** — individual messages within threads (user/assistant roles)
- **WebAuthnCredential** — passkey support
- **ApiKey** — API access keys with permissions and usage tracking
- **ApiUsage** — per-key usage logging
- **Wallet / Transaction** — credit-based billing system
- **LLMQueueEntry** — request queuing for LLM inference

## Features

- User signup/login with password hashing + WebAuthn passkeys
- Multi-thread chat interface with SSE streaming
- Image upload and vision analysis (HiDream model)
- Free tier with rate limiting (5 messages/hour)
- Premium subscriptions via Stripe
- Credit-based wallet system (top-ups)
- API key management for programmatic access
- Thread compaction/summarization for long conversations
- Admin dashboard
- Blog and contact pages
- PWA support (installable)
- Legal pages (privacy, terms)

## Server Layout

| Server | IP | Role |
|--------|-----|------|
| ecolyxis_web1 | 192.168.122.162 | Flask app (this server) |
| ecolyxis_web2 | 192.168.122.221 | Secondary web server |
| ecolyxis_db1 | 192.168.122.163 | PostgreSQL + PgBouncer |
| host01 (LM Studio) | 10.0.0.1 | LLM inference (Qwen + HiDream) |
| VPS | 77.68.76.216 | Public gateway / WireGuard entry |

## File Structure

```
/opt/Ecolyxis/
├── run.py              # App entry point
├── config.py           # Configuration (DB, Stripe, LLM URLs)
├── SPEC.md             # Original project specification
├── cleanup_expired.py  # Expired data cleanup script
├── app/
│   ├── __init__.py     # Flask app factory
│   ├── models.py       # SQLAlchemy models
│   ├── auth.py         # Authentication (login, signup, passkeys)
│   ├── chat.py         # Chat logic + LLM streaming
│   ├── llm.py          # LLM client abstraction
│   ├── api.py          # API key endpoints
│   ├── apikeys.py      # API key management
│   ├── billing.py      # Stripe billing + webhooks
│   ├── wallet.py       # Credit wallet system
│   ├── dashboard.py    # User dashboard
│   ├── admin.py        # Admin panel
│   ├── blog.py         # Blog pages
│   ├── contact.py      # Contact form
│   ├── legal.py        # Privacy/terms pages
│   ├── queue.py        # LLM request queue
│   ├── post_model.py   # Blog post model
│   ├── templates/      # Jinja2 HTML templates
│   └── static/         # CSS, JS, PWA assets
├── uploads/            # User-uploaded images
└── venv/               # Python virtual environment
```

## Last Updated

2026-05-14
