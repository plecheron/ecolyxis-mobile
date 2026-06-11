#!/usr/bin/env python3
"""
Ecolyxis Intelligence Benchmark
================================

Runs a multi-domain, objectively-gradable test suite through the Ecolyxis
OpenAI-compatible API (`/v1/chat/completions`) and reports the model's
"intelligence" as a percentage.

No LLM judge is used: every question has a deterministic answer that is
extracted from a strict `ANSWER:` protocol and graded by code, so the score
is reproducible.

Usage
-----
    # Offline: prove the grader is correct (no API, no billing) — run this first.
    python3 intelligence_benchmark.py --self-test

    # Live run through Ecolyxis (needs an API key with wallet balance):
    export ECOLYXIS_API_KEY=ecolyx_xxx
    python3 intelligence_benchmark.py
    python3 intelligence_benchmark.py --model ecolyxis-precise --out report.json

Scoring
-------
  Intelligence Score  = difficulty-weighted % of questions answered correctly
                        (harder questions count more)
  Raw Accuracy        = plain % correct
  Plus a per-category breakdown.

Stdlib only — depends on nothing outside the Python standard library.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_QUESTIONS = os.path.join(HERE, "questions.json")
DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"

SYSTEM_PROMPT = (
    "You are taking a written intelligence test. Read each question carefully. "
    "You may reason briefly, but you MUST end your reply with a final line in "
    "exactly this format:\n"
    "ANSWER: <your answer>\n"
    "Rules for the answer line: for multiple choice give only the letter "
    "(A, B, C or D); for numbers give only the number with no units, words, or "
    "thousands separators; for one-word answers give only that word. Put nothing "
    "after the ANSWER line."
)

NUM_RE = re.compile(r"-?\d*\.?\d+(?:/\d+)?")
MCQ_RE = re.compile(r"\b([A-D])\b")


# --------------------------------------------------------------------------- #
# Answer extraction + grading
# --------------------------------------------------------------------------- #
def extract_answer(text):
    """Pull the model's final answer out of its reply."""
    if not text:
        return ""
    marker_lines = [ln for ln in text.splitlines() if "answer:" in ln.lower()]
    if marker_lines:
        line = marker_lines[-1]
        idx = line.lower().rindex("answer:")
        return line[idx + len("answer:"):].strip().strip("*` ").strip()
    # Fallback: last non-empty line
    for ln in reversed(text.splitlines()):
        if ln.strip():
            return ln.strip().strip("*` ").strip()
    return ""


def _to_float(token):
    token = token.strip()
    if "/" in token:
        num, den = token.split("/", 1)
        return float(num) / float(den)
    return float(token)


def _first_number(s):
    s = s.replace(",", "")
    m = NUM_RE.search(s)
    return _to_float(m.group()) if m else None


def normalize(s):
    s = s.lower().strip()
    s = s.strip("\"'`*.! ")
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def grade(q, answer_text, full_text):
    """Return True if the extracted answer is correct for question `q`."""
    qtype = q["type"]

    if qtype == "numeric":
        val = _first_number(answer_text)
        if val is None:  # model ignored the protocol — try the whole reply
            nums = NUM_RE.findall(full_text.replace(",", ""))
            val = _to_float(nums[-1]) if nums else None
        if val is None:
            return False
        tol = q.get("tolerance", 0.001)
        return abs(val - float(q["answer"])) <= tol

    if qtype == "mcq":
        m = MCQ_RE.search(answer_text.upper())
        if not m:
            m = MCQ_RE.search(full_text.upper())
        return bool(m) and m.group(1) == str(q["answer"]).upper()

    if qtype == "exact":
        cand = normalize(answer_text)
        accepted = [normalize(q["answer"])] + [normalize(a) for a in q.get("aliases", [])]
        for a in accepted:
            if not a:
                continue
            if cand == a or re.search(r"\b" + re.escape(a) + r"\b", cand):
                return True
        return False

    if qtype == "regex":
        return bool(re.search(q["answer"], answer_text))

    raise ValueError(f"Unknown question type: {qtype}")


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #
def call_model(base_url, api_key, model, prompt, temperature, max_tokens, timeout=300, retries=4):
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode()

    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            return data["choices"][0]["message"]["content"], None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if e.code == 429:  # rate / daily cap — back off and retry
                wait = int(e.headers.get("Retry-After", 2 ** attempt))
                last_err = f"HTTP 429 (retrying in {wait}s): {detail}"
                time.sleep(wait)
                continue
            return None, f"HTTP {e.code}: {detail}"
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(2 ** attempt)
    return None, last_err or "request failed"


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def summarize(results):
    total = len(results)
    correct = sum(1 for r in results if r["correct"])
    w_total = sum(r["difficulty"] for r in results)
    w_correct = sum(r["difficulty"] for r in results if r["correct"])

    cats = {}
    for r in results:
        c = cats.setdefault(r["category"], {"n": 0, "ok": 0})
        c["n"] += 1
        c["ok"] += 1 if r["correct"] else 0

    return {
        "intelligence_score_pct": round(100 * w_correct / w_total, 1) if w_total else 0.0,
        "raw_accuracy_pct": round(100 * correct / total, 1) if total else 0.0,
        "correct": correct,
        "total": total,
        "categories": {
            c: {"correct": v["ok"], "total": v["n"], "pct": round(100 * v["ok"] / v["n"], 1)}
            for c, v in sorted(cats.items())
        },
    }


def print_report(summary, model, errors):
    bar = "=" * 56
    print("\n" + bar)
    print(f"  ECOLYXIS INTELLIGENCE BENCHMARK  —  model: {model}")
    print(bar)
    print("\n  Per-category:")
    for cat, v in summary["categories"].items():
        filled = int(round(v["pct"] / 5))
        meter = "#" * filled + "-" * (20 - filled)
        print(f"    {cat:<13} [{meter}] {v['pct']:5.1f}%  ({v['correct']}/{v['total']})")
    print("\n  " + "-" * 52)
    print(f"  Raw accuracy ......... {summary['raw_accuracy_pct']:5.1f}%  "
          f"({summary['correct']}/{summary['total']})")
    print(f"  INTELLIGENCE SCORE ... {summary['intelligence_score_pct']:5.1f}%  "
          f"(difficulty-weighted)")
    if errors:
        print(f"\n  ⚠ {errors} question(s) errored (counted as incorrect).")
    print(bar + "\n")


# --------------------------------------------------------------------------- #
# Self-test (offline grader validation)
# --------------------------------------------------------------------------- #
def self_test():
    cases = [
        ({"type": "numeric", "answer": 391}, "The product is 391.\nANSWER: 391", True),
        ({"type": "numeric", "answer": 1024}, "ANSWER: 1,024", True),
        ({"type": "numeric", "answer": 0.05, "tolerance": 0.001}, "ANSWER: $0.05", True),
        ({"type": "numeric", "answer": 0.05, "tolerance": 0.001}, "ANSWER: 0.10", False),
        ({"type": "numeric", "answer": 0.5}, "ANSWER: 1/2", True),
        ({"type": "numeric", "answer": 47}, "...so the answer is 47.", True),  # no marker, fallback
        ({"type": "mcq", "answer": "C"}, "I think it is C.\nANSWER: C", True),
        ({"type": "mcq", "answer": "C"}, "ANSWER: B", False),
        ({"type": "exact", "answer": "canberra"}, "ANSWER: Canberra", True),
        ({"type": "exact", "answer": "shakespeare", "aliases": ["william shakespeare"]},
         "ANSWER: William Shakespeare", True),
        ({"type": "exact", "answer": "yes", "aliases": ["true"]}, "ANSWER: Yes.", True),
        ({"type": "exact", "answer": "au"}, "ANSWER: Ag", False),
        ({"type": "exact", "answer": "n"}, "ANSWER: n", True),
    ]
    passed = 0
    for q, reply, expected in cases:
        got = grade(q, extract_answer(reply), reply)
        ok = got == expected
        passed += ok
        if not ok:
            print(f"  FAIL: {q} | reply={reply!r} expected={expected} got={got}")
    print(f"\nGrader self-test: {passed}/{len(cases)} cases passed.")
    return 0 if passed == len(cases) else 1


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Ecolyxis intelligence benchmark")
    ap.add_argument("--base-url", default=os.environ.get("ECOLYXIS_BASE_URL", DEFAULT_BASE_URL))
    ap.add_argument("--api-key", default=os.environ.get("ECOLYXIS_API_KEY"))
    ap.add_argument("--model", default="ecolyxis-standard",
                    help="ecolyxis-quick|standard|long|precise (default: standard)")
    ap.add_argument("--questions", default=DEFAULT_QUESTIONS)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=2048)
    ap.add_argument("--delay", type=float, default=1.1, help="seconds between calls (rate-limit safety)")
    ap.add_argument("--category", help="only run one category")
    ap.add_argument("--limit", type=int, help="only run the first N questions")
    ap.add_argument("--out", help="write a JSON report to this path")
    ap.add_argument("--verbose", action="store_true", help="print every question result")
    ap.add_argument("--self-test", action="store_true", help="validate the grader offline and exit")
    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    if not args.api_key:
        sys.exit("ERROR: no API key. Set ECOLYXIS_API_KEY or pass --api-key. "
                 "Use bootstrap_key.py to mint one.")

    with open(args.questions) as f:
        bank = json.load(f)
    questions = bank["questions"]
    if args.category:
        questions = [q for q in questions if q["category"] == args.category]
    if args.limit:
        questions = questions[:args.limit]

    print(f"Running {len(questions)} questions against {args.base_url} "
          f"(model={args.model}, temp={args.temperature})...")

    results, errors = [], 0
    for i, q in enumerate(questions, 1):
        content, err = call_model(args.base_url, args.api_key, args.model,
                                   q["prompt"], args.temperature, args.max_tokens)
        if err:
            errors += 1
            correct, ans = False, f"<error: {err}>"
        else:
            ans = extract_answer(content)
            correct = grade(q, ans, content)
        results.append({
            "id": q["id"], "category": q["category"], "difficulty": q["difficulty"],
            "correct": correct, "extracted": ans, "expected": q["answer"],
        })
        mark = "✓" if correct else "✗"
        line = f"  [{i:>2}/{len(questions)}] {mark} {q['id']:<10} {q['category']:<12} -> {ans[:40]!r}"
        if args.verbose:
            line += f"  (expected {q['answer']!r})"
        print(line)
        if i < len(questions):
            time.sleep(args.delay)

    summary = summarize(results)
    print_report(summary, args.model, errors)

    if args.out:
        with open(args.out, "w") as f:
            json.dump({"model": args.model, "base_url": args.base_url,
                       "summary": summary, "results": results}, f, indent=2)
        print(f"Report written to {args.out}")


if __name__ == "__main__":
    main()
