# Ecolyxis — Business Plan

*2026-06-12. Companion to `PROJECT.md` (what exists), `IMPROVEMENTS.md`
(infra, filed as #98–#109), `UI-IMPROVEMENTS.md`, and
`FEATURE-IMPROVEMENTS.md` (the product roadmap candidates). Numbers below
come from the production database and live pricing — including the
unflattering ones, because a plan built on real numbers can actually be
executed.*

## 1. What Ecolyxis is

A sustainable AI platform: LLM chat, image generation/editing, video, and
TTS, self-hosted on last-generation hardware (Tesla P40s) running on green
energy, sold as a carbon-neutral alternative to mainstream AI services.
Solo-operated. Technically real — durable job queue, resumable streaming,
Stripe billing, OpenAI-compatible API are all live in production.

## 2. Where it actually is today (2026-06-12)

| Metric | Value |
|---|---|
| Live since | 2026-05-02 (6 weeks) |
| Accounts | 9 — of which ~6 are test/admin accounts |
| Real outside users | ~2 |
| Premium subscribers | 2 (one is the operator) |
| Messages, last 30 days | 308 |
| API tokens, last 30 days | ~3.1M (7 keys, mostly own testing) |
| Lifetime top-up revenue | £10.00 |
| Video generations ever | 0 (feature broken — #109) |

**Honest read: this is a friends-and-family alpha, pre-revenue.** That is
fine at week 6 — but the plan's job is the path from here to the first 100
real users and first £1k MRR, not projections built on air.

## 3. Market & positioning

- **The niche:** users and small businesses who want AI without the
  hyperscalers — for environmental, privacy, or data-sovereignty reasons.
  Adjacent proof points exist (GreenPT, Windcloud, the broader "sovereign /
  green cloud" movement), but no strong consumer brand owns "sustainable AI
  chat" yet.
- **The wedge is the story, not the model.** Qwen3.6-35B will never beat
  frontier models on capability. It doesn't have to: the pitch is "good
  enough AI, genuinely green, your data stays in the UK on hardware you can
  point at." Capability-insensitive, values-sensitive customers are the
  target — eco-conscious individuals, sustainability-reporting SMEs, B-Corps,
  agencies serving green brands.
- **Defensibility is credibility.** Anyone can claim green; a solo operator
  with named hardware, a public energy dashboard (FEATURE-IMPROVEMENTS #1),
  and per-account CO₂e numbers is *verifiable* in a way AWS-hosted wrappers
  are not. Ship the receipts.

## 4. Business model (already built)

| Stream | Pricing | Status |
|---|---|---|
| Premium subscription | £4.99 first month, then £9.99/mo | Live (Stripe) |
| API credits (pay-as-you-go) | £2.78 per million tokens, wallet top-ups | Live |
| Free tier | 5 messages + 5 generations/hour | Live (the funnel) |

Near-term additions, in order of effort-to-revenue:
1. **Image API** (FEATURE #2) — sell existing media infra through the
   existing wallet; blocked only on backend reliability (#99/#109).
2. **Team workspaces** (FEATURE #11) — the ARPU step above £9.99; later.

## 5. Unit economics (estimates — assumptions stated)

*Assume: P40 ≈ 250W under load, UK green tariff ≈ £0.25/kWh, llama.cpp
serving the 35B-A3B (MoE, ~3B active) quant at an effective 30–80 tok/s
aggregate. These need measuring — see "Do next."*

- **API:** 1M tokens ≈ 3.5–9.5 GPU-hours ≈ £0.22–£0.60 electricity against
  £2.78 revenue → **~78–92% gross margin** on energy. Real margin is lower
  (hardware amortization, VPS ~£5–15/mo, the operator's time) but the price
  point is sound.
- **Premium:** a heavy user sending 1k messages/mo at ~1k tokens each costs
  pennies in energy against £9.99 → margin is not the problem.
- **The real constraint is capacity, not cost.** One GPU box serves
  everything (LLM + image + video + TTS). The ceiling is concurrent
  premium users at acceptable latency — measure it (the `benchmark/` suite
  is the tool, #108) before any marketing push, because the brand cannot
  afford "green but unusably slow."

## 6. Go-to-market — first 100 real users

Spending nothing, in order:

1. **Fix trust blockers first.** No HTTPS (#98) kills every signup from
   anyone technical enough to notice; broken image/video gen (#99/#109)
   churns the curious. A week of reliability work outranks any marketing.
2. **Ship the sustainability dashboard** (FEATURE #1) — the differentiator
   is currently invisible in-product, and it's the screenshot that makes
   every post below land.
3. **Build in public.** The architecture story (durable jobs on recycled
   P40s, solo operator) is genuinely good content for HN/Lobsters/r/selfhosted
   and the UK eco-tech scene. The blog exists; use it. Shareable conversation
   links (FEATURE #6) turn users into the funnel.
4. **Direct outreach to ~20 green-aligned UK SMEs/agencies** offering the
   API + a "powered by carbon-neutral AI" badge. One logo customer is worth
   more than a month of posts.

KPIs that matter at this stage: real weekly-active users, free→premium
conversion, first stranger-paid £ (not friends), API tokens from keys that
aren't the operator's.

## 7. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Single operator (bus factor 1) | High | Automate ops (#100, #103, #107); document everything (largely done) |
| Single GPU box = capacity + availability ceiling | High | Measure capacity; queue degrades gracefully (already does); second box only after revenue justifies it |
| Capability gap vs. frontier models widens | Medium | Compete on values/sovereignty, never on benchmarks; track open-model releases (the stack is model-agnostic) |
| Greenwashing scrutiny | Medium | Publish methodology + real numbers (FEATURE #1); never claim more than measured |
| Stripe/regulatory (UK consumer law, VAT) | Low-Med | Terms/privacy pages exist; add VAT handling before non-trivial revenue |
| "Free tier abuse" / GPU DoS | Low | Rate limits exist and were hardened (#91); keep them in front of every job kind |

## 8. Milestones

- **By end of June 2026:** #98 (HTTPS) + image generation reliable + capacity
  benchmark done. *Exit: a stranger can sign up and every advertised feature
  works.*
- **By end of August 2026:** sustainability dashboard + share links + image
  API live; build-in-public cadence started. *Exit: 100 registered, 25
  weekly-active, first stranger revenue.*
- **By end of 2026:** *Exit: £500–1,000 MRR (~50–100 premium or equivalent
  API), one named business customer.* Decide then — and only then — whether
  this funds a second GPU box, stays a profitable side business, or winds
  down gracefully.

## 9. Do next (this month, in order)

1. Close #98 (HTTPS) — trust blocker for everything else.
2. Fix or unship image/video generation (#99, #109) — honesty blocker.
3. Run `benchmark/` under concurrency; write down the real capacity number.
4. Measure actual GPU power draw and replace §5's assumptions with data.
5. Ship the sustainability dashboard (FEATURE #1) — then start telling
   people Ecolyxis exists.
