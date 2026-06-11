# Ecolyxis Intelligence Benchmark

A self-contained benchmark that scores the model behind Ecolyxis as a
**percentage**, by sending objectively-gradable questions through the live
OpenAI-compatible API (`POST /v1/chat/completions`).

It uses **no LLM judge** — every question has a deterministic answer pulled
from a strict `ANSWER:` protocol and graded by code, so the score is
reproducible.

## What it measures

50 questions across 8 domains, each tagged with a difficulty (1–3):

| Category      | What it probes                                  |
|---------------|-------------------------------------------------|
| `math`        | arithmetic, algebra, percentages, word problems |
| `logic`       | deduction, syllogisms, validity                 |
| `sequence`    | numeric / symbolic pattern completion           |
| `knowledge`   | factual recall                                  |
| `coding`      | code tracing / program output                   |
| `reasoning`   | classic traps (bat-and-ball, lily-pad, …)       |
| `reading`     | passage comprehension + inference               |
| `instruction` | precise instruction following                   |

**Two headline numbers:**
- **Intelligence Score** — difficulty-weighted % correct (harder = worth more). This is the headline.
- **Raw Accuracy** — plain % correct.

Plus a per-category breakdown.

## Running it

### 1. Validate the grader offline (no API, no billing)
```bash
python3 benchmark/intelligence_benchmark.py --self-test
```

### 2. Get an API key with wallet balance
```bash
cd /opt/Ecolyxis
venv/bin/python benchmark/bootstrap_key.py --email you@example.com --topup-pence 500
```
(Omit `--email` to use the first user. `--topup-pence` only tops up if the
balance is below that amount; it credits internal wallet pence, nothing hits
Stripe.)

### 3. Run the benchmark
```bash
export ECOLYXIS_API_KEY=ecolyx_...
python3 benchmark/intelligence_benchmark.py
```

## Useful flags

| Flag | Purpose |
|------|---------|
| `--model ecolyxis-precise` | use a smarter mode (`quick`/`standard`/`long`/`precise`) |
| `--out report.json` | write a full JSON report (per-question results) |
| `--category math` | run a single category |
| `--limit 5` | smoke test on the first N questions |
| `--verbose` | print expected answers alongside results |
| `--temperature 0` | default; deterministic |
| `--delay 1.1` | seconds between calls (stays under 60 completions/min) |
| `--base-url` | override API base (default `http://127.0.0.1:8000/v1`) |

## How grading works

The system prompt forces every reply to end with `ANSWER: <x>`. The harness
extracts that and grades by type:

- **numeric** — parses the number (handles `1,024`, `$0.05`, `1/2`, fallbacks
  to the last number in the reply); compares within a tolerance.
- **mcq** — first standalone `A`–`D` letter.
- **exact** — normalised (lowercased, punctuation-stripped) equality or
  whole-word match against the answer + accepted aliases.

## Extending the bank

Add objects to `questions.json` → `questions[]`:
```json
{"id": "math-09", "category": "math", "difficulty": 2, "type": "numeric",
 "answer": 42, "tolerance": 0.001, "prompt": "..."}
```
`type` is one of `numeric` | `mcq` | `exact` | `regex`. `aliases` (exact) and
`tolerance` (numeric) are optional. Re-run `--self-test` after editing to make
sure nothing broke.
