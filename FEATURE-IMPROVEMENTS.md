# Ecolyxis — Feature Improvements

*2026-06-12. Product-feature companion to `IMPROVEMENTS.md` (infra) and
`UI-IMPROVEMENTS.md` (frontend). Grounded in the current feature surface:
chat modes (quick/standard/long/precise/vision + image/edit/video), durable
jobs, workspaces, per-thread system prompts, wallet + public `/v1` API,
export, TTS, PWA.*

## Brand-defining

1. **Sustainability dashboard — make the green promise visible.** The entire
   brand is "carbon-neutral AI", yet the product shows the user nothing about
   it: no energy, carbon, or efficiency stat appears anywhere in the app. The
   data to estimate it already exists (`Message.tokens_used`,
   `reasoning_tokens`, `ApiUsage` per key). Show per-account and per-thread
   estimated Wh and CO₂e vs. a mainstream-cloud baseline, and a site-wide
   counter on the landing page. No mainstream competitor can copy this
   honestly — it's the differentiator, currently unshipped.

## Monetization & API

2. **Expose image generation through the public `/v1` API.** The wallet,
   per-key usage logging, and credit debiting all exist, but the API offers
   only `/v1/chat/completions`, `/v1/models`, `/v1/balance`. Add an
   OpenAI-compatible `/v1/images/generations` (and edit) backed by the same
   durable-job path. New revenue from infrastructure that's already built —
   gated on the image backend actually being reliable (#99/#109).

3. **Wallet guardrails: spend caps, low-balance alerts, auto top-up.** Today
   the only budget control is hitting a 402 when credits run out. Add
   per-API-key spend caps, an email alert at a configurable low-balance
   threshold, and opt-in Stripe auto top-up. Caps make the API safe to
   embed in someone else's product; auto top-up removes the most common
   involuntary-churn point.

## Chat capabilities

4. **Document upload for workspace context (RAG-lite).** Uploads are
   images-only, so the obvious "chat with my PDF/notes" use case is
   unserved. Workspaces already maintain a 16k-token shared context budget
   from thread summaries — extend the same pipeline to extract text from
   uploaded PDFs/markdown/txt into per-workspace context. The strongest
   premium upsell on this list, and it reuses the existing budget machinery
   in `app/utils/tokens.py` + `workspace/summaries.py`.

5. **Account-level custom instructions and saved personas.** Premium users
   can set a per-thread system prompt (`/chat/<id>/system-prompt`), but must
   re-enter it for every new thread. Add an account-wide default plus named,
   reusable personas selectable when creating a thread. Small schema change,
   large quality-of-life gain for the heaviest users.

6. **Shareable read-only conversation links.** Export (JSON/MD download,
   premium) exists, but there's no way to *show* anyone a conversation. A
   public tokenized read-only thread view ("anyone with the link") is the
   standard growth loop for chat products — every shared thread is a landing
   page with a sign-up CTA. Needs an unguessable slug, owner revocation, and
   the XSS fix from UI-IMPROVEMENTS #1 first.

## Generation & jobs

7. **Push notifications when long jobs finish.** The durable-job system's
   whole point is that generations survive the user leaving — but nothing
   tells them it finished. The PWA scaffolding (service worker, manifest,
   installable) is already shipped; add Web Push on job completion ("Your
   video is ready"), with the existing sidebar pulse as the in-app fallback.
   This turns the architecture's best property into a feature users can feel.

8. **A media gallery ("My creations").** `GeneratedImage`/`GeneratedVideo`
   rows carry seed, size, and upscale lineage, but generated media is only
   reachable by scrolling the thread that produced it. Add a gallery page
   with re-use actions — re-edit, upscale, animate, download — wired to the
   existing job kinds. Makes the media features feel like a product rather
   than chat side-effects.

9. **TTS voice and speed selection.** Read-aloud is single-voice with no
   controls. Qwen3-TTS supports multiple voices; expose a voice picker and
   playback speed (persist per user). Cheap, and pairs well with
   accessibility work in UI-IMPROVEMENTS #6.

## Organization

10. **Thread pinning and archiving.** No `pinned`/`archived` exists on
    `Thread` — power users' sidebars are a single chronological list, and
    the only cleanup tool is deletion. Pin-to-top plus archive (hidden from
    sidebar, still searchable/exportable) is a small migration and keeps
    long-time users' workspaces usable.

## Bigger bets

11. **Team workspaces.** Workspaces are strictly single-user. A multi-seat
    tier — shared workspace context, shared threads, per-seat billing — is
    the natural ARPU step above individual premium, and the workspace model
    is the right foundation for it. Significant authz surface (every
    ownership check in the app assumes user == owner), so cost it honestly.

12. **Web search / current-information tool.** The model is offline and
    cutoff-bound; "what happened this week" questions fail silently with
    stale answers. An opt-in search tool (self-hosted SearXNG would fit the
    independent/green brand) injected into context closes the most common
    capability gap vs. mainstream assistants. Largest item here — needs
    prompt-injection thinking before it ships.
