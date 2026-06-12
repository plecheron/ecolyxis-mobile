# Ecolyxis — UI Improvements

*2026-06-12. Frontend-focused companion to `IMPROVEMENTS.md`, based on the
current templates (`chat.html` 3,533 lines, `base.html`, `landing.html`,
`dashboard.html`), `static/css/style.css` (2,234 lines), and the PWA assets.*

## Security & correctness

1. **Sanitize rendered markdown — XSS via LLM output.** `renderMd()` in
   `chat.html` returns `marked.parse(text)` straight into `innerHTML`, and
   marked passes raw HTML through by default. A user can make the model echo
   `<img onerror=…>` and have it execute — stored in the thread, re-executed
   on every page load, and also reachable through workspace summaries. Add
   DOMPurify over the parsed output (or marked's no-HTML mode). The
   `escapeHtml()` fallback only runs when parsing *throws*, which it
   essentially never does.

2. **Pin and self-host the CDN scripts; remove `.bak` files from `static/`.**
   `base.html` loads marked from jsdelivr at the **floating latest version**
   (an upstream release can break chat rendering overnight) and highlight.js
   11.9.0, neither with an `integrity` attribute — a CDN compromise is a full
   XSS. Self-hosting also fixes the PWA story (the service worker can't
   precache cross-origin scripts, so "offline" chat has no markdown renderer)
   and fits the privacy-leaning eco brand. Separately,
   `static/css/style.css.bak` and `.bak2` are **publicly served** by Flask —
   delete them (plus `chat.html.bak/.bak2`, `dashboard.html.bak` in
   templates/).

## Architecture & maintainability

3. **Break up the `chat.html` monolith.** ~3,260 of its 3,533 lines are one
   inline `<script>` block: the SSE client, job polling, markdown rendering,
   image preview, modals, mode switching — everything. Inline JS is re-sent
   on every page load (no browser caching, no service-worker precache) and
   can't be linted or unit-tested. Extract to `static/js/chat/*.js` modules;
   keep only bootstrap data (thread id, CSRF, flags) inline. Do this *before*
   the legacy-path removal (#105) so the diff is reviewable.

## Theming & visual polish

4. **Add dark mode.** No `prefers-color-scheme` handling anywhere, yet code
   blocks already use highlight.js's **github-dark** theme — dark islands in
   a light UI. `style.css` is plain values, so step one is lifting colors
   into CSS custom properties, then a `@media (prefers-color-scheme: dark)`
   block + manual toggle. On-brand for a sustainability product (OLED energy)
   and fixes the code-block mismatch either way.

5. **Add a favicon.** `base.html` links the PWA manifest and icons
   (icon-192/512) but has no `<link rel="icon">`, so browser tabs show the
   default globe. One line plus a small SVG/ICO.

## Accessibility

6. **Do an accessibility pass.** `chat.html` has 24 aria attributes;
   `base.html`, `dashboard.html`, and `landing.html` have **zero**. Specific
   gaps: flash messages auto-dismiss after 4 s with no `role="alert"`/
   `aria-live` (screen readers may never see them, and 4 s is short for
   anyone); streaming status changes ("Thinking…" → answer) aren't announced;
   the rate-limit modal is injected via `innerHTML` with no focus trap or
   Escape handling; sidebar thread list keyboard navigation is untested.
   `prefers-reduced-motion` is already handled in `style.css` — good
   foundation to build on.

## Chat UX

7. **Handle long threads.** The full message history renders in one go on
   thread load. Threads with months of history (and inline generated images)
   pay that cost on every visit. Paginate or lazy-render older messages —
   load the latest N and fetch upward on scroll. (Generated images already
   get `loading="lazy"`, which helps but doesn't bound DOM size.)

8. **Add a "jump to latest" affordance during streaming.** The scroll-pinning
   logic is already right (scrolling up isn't yanked back per token), but
   once unpinned there's no button to return — users scroll-drag through a
   long streaming answer. Standard floating ↓ button when `userPinned` is
   false and new tokens arrive.

9. **One-click retry on failed generations.** When a job errors (image
   backend down, see #99/#109), the user gets an error bubble and has to
   re-type or re-select the mode. Failed jobs should render a retry button
   that re-submits the same params — the durable-job API already has
   everything needed (`POST /jobs/<kind>/<thread>` with the stored params).

## Landing & PWA

10. **Beef up the landing page.** `landing.html` is 48 lines: hero, three
    eco-themed feature cards, footer. It never mentions what the product
    *does* — image generation, image editing, video, TTS, workspaces, and the
    API are invisible to a visitor, and there's no screenshot or demo of the
    chat itself. Add a product section (screenshots/short clips) and a
    pricing teaser; conversion depends on it.

11. **Tidy the PWA/service-worker story.** `sw.js` precaches `style.css` but
    not `billing.css`/`admin.css` (both loaded by `base.html` on every page —
    consolidate or scope them while at it), and the cache version is a
    manually-bumped string (`ecolyxis-v10`) that's easy to forget on deploy —
    derive it from a build/deploy stamp. There's also no offline fallback
    page: a navigation while offline that misses the cache just fails.
