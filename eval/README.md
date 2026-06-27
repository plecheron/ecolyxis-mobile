# Ecolyxis Evaluation Harness

Unified benchmark suite for the Ecolyxis LLM. Two evaluation modes:

1. **Custom Intelligence Benchmark** — 64 objectively-gradable questions across
   10 domains (math, logic, sequences, knowledge, coding, reasoning, reading,
   instruction following, sustainability), graded deterministically via the
   `ANSWER:` protocol. No LLM judge needed.

2. **Standard Benchmarks** — lm-eval-harness wrapper for GSM8K, ARC-Challenge,
   and TruthfulQA, running against the raw llama.cpp API.

## Quick Start

```bash
# Self-test (offline, no API needed)
python3 eval/runner.py --self-test

# Smoke test (8 questions, ~2 min)
python3 eval/runner.py --backend raw --limit 8

# Full custom benchmark (~15-20 min)
python3 eval/runner.py --backend raw

# Against production Ecolyxis API
ECOLYXIS_API_KEY=ecolyx_xxx python3 eval/runner.py --backend ecolyxis --model ecolyxis-precise

# Standard benchmarks (lm-eval-harness)
python3 eval/runner.py --backend lm-eval --tasks gsm8k,arc_challenge_ecolyxis

# Shell wrapper
./eval/run_benchmark.sh --smoke
./eval/run_benchmark.sh --all
```

## Scoring

**Custom benchmark** produces two headline numbers:
- **Intelligence Score** — difficulty-weighted % (harder questions count more)
- **Raw Accuracy** — plain % correct

Plus per-category breakdown.

| Category       | Questions | Difficulty |
|----------------|-----------|------------|
| math           | 12        | 1-3        |
| logic          | 8         | 1-3        |
| sequence       | 6         | 1-3        |
| knowledge      | 12        | 1-3        |
| coding         | 6         | 1-3        |
| reasoning      | 6         | 2-3        |
| reading        | 4         | 2-3        |
| instruction    | 6         | 1-2        |
| sustainability  | 5         | 1-3        |

## Backends

| Backend   | Endpoint                        | Auth  | Use case                    |
|-----------|---------------------------------|-------|-----------------------------|
| `raw`     | llama.cpp on 10.0.0.6:8081    | None  | Direct GPU testing           |
| `ecolyxis`| Ecolyxis API at 127.0.0.1:8000 | API key| Production model testing    |
| `lm-eval` | llama.cpp (via lm-eval CLI)    | None  | Standard academic benchmarks |

## CLI Flags

| Flag | Purpose |
|------|---------|
| `--backend raw\|ecolyxis\|lm-eval` | API backend |
| `--model` | Model name (ecolyxis-quick/standard/long/precise) |
| `--category math` | Run single category only |
| `--limit N` | Run first N questions |
| `--out report.json` | Save JSON report |
| `--verbose` | Show expected answers |
| `--no-warmup` | Skip GPU warmup |
| `--diff a.json b.json` | Compare two runs |
| `--self-test` | Offline grader validation |

## lm-eval Custom Tasks

Custom task definitions in `eval/tasks/` handle Qwen3 thinking-token quirks:
- `arc_challenge_ecolyxis.yaml` — regex letter extraction, no gen_prefix
- `truthfulqa_ecolyxis/` — 4096 max_gen_toks, no "\\n\\n" stop

## Extending

Add questions to `eval/questions.json`:
```json
{"id": "math-13", "category": "math", "difficulty": 2, "type": "numeric",
 "answer": 42, "prompt": "What is 6 multiplied by 7?"}
```

Types: `numeric`, `mcq`, `exact`, `regex`. Run `--self-test` after editing.

## Requirements

- `requests` library (`pip install requests`)
- Network access to GPU (10.0.0.6) via WireGuard
- For lm-eval backend: `pip install lm-eval transformers`
