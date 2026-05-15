# Ecolyxis — 25 Possible Improvements

## Security

1. **Move secrets to environment variables** — Stripe keys, DB credentials, SECRET_KEY, and webhook secrets are hardcoded in `config.py`. Use `.env` files or a secrets manager. Never commit live Stripe keys to code.

2. **Enable HTTPS on Caddy** — Currently only listening on `:80`. If this server receives direct traffic (or even just internally), Caddy should serve TLS. Add the domain and let Caddy's auto-TLS handle it, or terminate at the VPS with proper cert forwarding.

3. **Remove the stale SQLite database** — `instance/ecolyxis.db` (30MB) appears unused since the PostgreSQL migration. Remove it to reduce confusion and avoid accidental fallback.

4. **Rotate exposed credentials** — The Stripe secret key, webhook secret, and DB password were in `config.py`. Even after moving to env vars, rotate all of them since they've been in plaintext on disk.

5. **Add CSRF protection** — Flask forms should have CSRF tokens. Use Flask-WTF or similar to prevent cross-site request forgery on all POST routes.

## Reliability

6. **Add a systemd service for Gunicorn** — Gunicorn appears to be running but not via systemd. Create a proper service unit so it auto-restarts on crash and starts on boot.

7. **Set up the cleanup cron job** — `cleanup_expired.py` exists but isn't in crontab. Expired sessions, old API usage records, and stale data should be purged automatically.

8. **Add health check endpoints** — A `/health` endpoint that verifies DB connectivity and LLM API availability. Useful for monitoring and alerts.

9. **Implement proper error logging** — Use structured logging (JSON) with rotation. `server.log` is currently unstructured and will grow indefinitely.

10. **Add database migrations** — No migration tool detected (no Alembic/Flask-Migrate). Schema changes risk data loss. Add Flask-Migrate before the next model change.

## Performance

11. **Add Redis for session/cache** — Flask sessions and rate limiting could use Redis instead of hitting PostgreSQL for every request. Would also enable proper SSE connection tracking.

12. **Implement response caching** — Cache repeated LLM responses or at minimum cache the landing page and static assets more aggressively via Caddy.

13. **Optimize Gunicorn worker count** — 3 workers with 2 threads on 2 vCPU/2GB RAM is reasonable, but monitor if async workers (gevent/eventlet) would handle SSE streaming better than threads.

14. **Add database connection pooling tuning** — PgBouncer is there but verify pool sizes match Gunicorn worker count to avoid connection starvation under load.

15. **Lazy-load images in chat** — If chat history includes uploaded images, load them on scroll rather than all at once for long threads.

## User Experience

16. **Add conversation export** — Let users export threads as Markdown, PDF, or JSON. Useful for paying customers who want records.

17. **Improve mobile responsiveness** — Audit the chat interface on mobile. SSE streaming + HTMX can be janky on slow connections; add loading states and reconnection logic.

18. **Add a pricing page** — The landing page mentions "Get Started — It's Free" but there's no visible pricing for premium. Users won't convert if they can't see the value.

19. **Implement dark mode** — A sustainability-themed brand should offer dark mode (saves energy on OLED screens and fits the aesthetic).

20. **Add typing indicators and better streaming UX** — Show a typing animation while waiting for first token. Handle disconnections gracefully with reconnection and message recovery.

## DevOps & Code Quality

21. **Set up CI/CD** — No evidence of automated testing or deployment. Add GitHub Actions (or similar) for linting, tests, and automated deploys to the VMs.

22. **Add .gitignore and proper repo structure** — Ensure `instance/`, `uploads/`, `venv/`, and config with secrets are gitignored. The `.bak` cleanup showed these weren't being managed well.

23. **Write tests** — No test files found. Add pytest with coverage for auth flows, billing webhooks, rate limiting, and chat endpoints. Critical for a live product handling payments.

24. **Monitor and alert** — Set up uptime monitoring (UptimeRobot, or self-hosted) for the public endpoint. Monitor Gunicorn worker health, DB connections, and disk space.

25. **Rate limit the API endpoints** — Free-tier chat is rate limited, but ensure signup, login, password reset, and contact form are also rate limited to prevent abuse and brute force.
