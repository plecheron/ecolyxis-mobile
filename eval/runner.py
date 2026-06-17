#!/usr/bin/env python3
"""
Ecolyxis Evaluation Harness
============================

Unified benchmark suite for the Ecolyxis LLM. Tests intelligence across
multiple domains using objectively-gradable questions with deterministic
scoring (no LLM judge).

Three backends:
  raw       — Direct llama.cpp OpenAI-compatible API (no auth)
  ecolyxis  — Production Ecolyxis API (API key + model modes)
  lm-eval   — Shell out to lm-eval-harness for standard benchmarks

Usage:
    # Self-test (offline grader validation)
    python3 eval/runner.py --self-test

    # Quick smoke test (1 question per category)
    python3 eval/runner.py --backend raw --limit 8

    # Full run against raw LLM API
    python3 eval/runner.py --backend raw

    # Against Ecolyxis production API
    ECOLYXIS_API_KEY=xxx python3 eval/runner.py --backend ecolyxis --model ecolyxis-precise

    # lm-eval-harness standard benchmarks
    python3 eval/runner.py --backend lm-eval --tasks gsm8k,arc,truthfulqa

    # Compare two results
    python3 eval/runner.py --diff results/run1.json results/run2.json
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library required. Install with: pip install requests")
    sys.exit(1)

HERE = Path(__file__).resolve().parent
QUESTIONS_FILE = HERE / "questions.json"
RESULTS_DIR = HERE / "results"
TASKS_DIR = HERE / "tasks"

# Default endpoints
RAW_API_URL = "http://10.0.0.6:8081/v1/chat/completions"
ECOLYXIS_API_URL = "http://127.0.0.1:8000/v1"
GPU_MANAGER_URL = "http://10.0.0.6:8090"

# lm-eval-harness config
LM_EVAL_MODEL = "local-chat-completions"
LM_EVAL_MODEL_ARGS_TEMPLATE = (
    "model={model},base_url={base_url},num_concurrent=4,max_retries=10,"
    "max_gen_toks=4096,timeout=300,tokenizer={tokenizer}"
)

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


# ─── Answer extraction & grading ──────────────────────────────────────────

def extract_answer(text):
    """Pull the model's final answer from the ANSWER: protocol."""
    if not text:
        return ""
    marker_lines = [ln for ln in text.splitlines() if "answer:" in ln.lower()]
    if marker_lines:
        line = marker_lines[-1]
        idx = line.lower().rindex("answer:")
        return line[idx + len("answer:"):].strip().strip("*` ").strip()
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


def _normalize(s):
    s = s.lower().strip()
    s = s.strip("\"'`*.! ")
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def grade(q, answer_text, full_text):
    """Return True if the extracted answer is correct."""
    qtype = q["type"]

    if qtype == "numeric":
        val = _first_number(answer_text)
        if val is None:
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
        cand = _normalize(answer_text)
        accepted = [_normalize(q["answer"])] + [_normalize(a) for a in q.get("aliases", [])]
        for a in accepted:
            if not a:
                continue
            if cand == a or re.search(r"\b" + re.escape(a) + r"\b", cand):
                return True
        return False

    if qtype == "regex":
        return bool(re.search(q["answer"], answer_text))

    raise ValueError(f"Unknown question type: {qtype}")


# ─── GPU warmup ───────────────────────────────────────────────────────────

def ensure_gpu_llm_mode():
    """Switch GPU manager to LLM mode and wait for readiness."""
    print("🔌 Ensuring GPU is in LLM mode...")
    try:
        r = requests.get(f"{GPU_MANAGER_URL}/status", timeout=5)
        status = r.json()
        if status.get("mode") == "llm" and status.get("ready"):
            print("  ✅ LLM already active")
            return
    except requests.RequestException:
        print("  ⚠ GPU manager unreachable — continuing")
        return

    print("  Switching to LLM mode...")
    try:
        requests.post(f"{GPU_MANAGER_URL}/switch", json={"mode": "llm"}, timeout=180)
    except requests.RequestException:
        print("  ⚠ Switch request failed — continuing")
        return

    for _ in range(60):
        try:
            r = requests.get(f"{GPU_MANAGER_URL}/status", timeout=5)
            if r.json().get("mode") == "llm" and r.json().get("ready"):
                print("  ✅ LLM mode ready")
                return
        except requests.RequestException:
            pass
        time.sleep(3)
    print("  ⚠ Timeout waiting for LLM mode — continuing anyway")


# ─── API client ────────────────────────────────────────────────────────────

def call_model(base_url, api_key, model, prompt, temperature=0.0,
               max_tokens=4096, timeout=300, retries=4, delay=0):
    """Send a chat completion request. Returns (content, error_string)."""
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    last_err = None
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 2 ** attempt))
                last_err = f"HTTP 429 (retrying in {wait}s)"
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"].get("content", "")
            return content, None
        except requests.RequestException as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(2 ** attempt)
    return None, last_err or "request failed"


# ─── Reporting ─────────────────────────────────────────────────────────────

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


def print_report(summary, model, backend, errors):
    bar = "=" * 56
    print(f"\n{bar}")
    print(f"  ECOLYXIS EVALUATION HARNESS  —  model: {model}  (backend: {backend})")
    print(bar)
    print("\n  Per-category:")
    for cat, v in summary["categories"].items():
        filled = int(round(v["pct"] / 5))
        meter = "#" * filled + "-" * (20 - filled)
        print(f"    {cat:<16} [{meter}] {v['pct']:5.1f}%  ({v['correct']}/{v['total']})")
    print(f"\n  {'-' * 52}")
    print(f"  Raw accuracy ......... {summary['raw_accuracy_pct']:5.1f}%  "
          f"({summary['correct']}/{summary['total']})")
    print(f"  INTELLIGENCE SCORE ... {summary['intelligence_score_pct']:5.1f}%  "
          f"(difficulty-weighted)")
    if errors:
        print(f"\n  ⚠ {errors} question(s) errored (counted as incorrect).")
    print(bar + "\n")


# ─── Self-test ────────────────────────────────────────────────────────────

def self_test():
    cases = [
        ({"type": "numeric", "answer": 391}, "The product is 391.\nANSWER: 391", True),
        ({"type": "numeric", "answer": 1024}, "ANSWER: 1,024", True),
        ({"type": "numeric", "answer": 0.05, "tolerance": 0.001}, "ANSWER: $0.05", True),
        ({"type": "numeric", "answer": 0.05, "tolerance": 0.001}, "ANSWER: 0.10", False),
        ({"type": "numeric", "answer": 0.5}, "ANSWER: 1/2", True),
        ({"type": "numeric", "answer": 47}, "...so the answer is 47.", True),
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


# ─── Diff mode ────────────────────────────────────────────────────────────

def diff_results(file_a, file_b):
    with open(file_a) as f:
        a = json.load(f)
    with open(file_b) as f:
        b = json.load(f)

    a_sum = a.get("summary", a)
    b_sum = b.get("summary", b)

    print(f"\n{'='*60}")
    print("  EVALUATION DIFF")
    print(f"{'='*60}")
    print(f"  A: {file_a} ({a.get('timestamp', 'n/a')})")
    print(f"  B: {file_b} ({b.get('timestamp', 'n/a')})")
    print()

    # Overall scores
    for key in ["intelligence_score_pct", "raw_accuracy_pct"]:
        av = a_sum.get(key, 0)
        bv = b_sum.get(key, 0)
        diff = bv - av
        icon = "📈" if diff > 0 else "📉" if diff < 0 else "➡️"
        print(f"  {icon} {key}: {av}% → {bv}% ({diff:+.1f}%)")

    # Per-category
    a_cats = a_sum.get("categories", {})
    b_cats = b_sum.get("categories", {})
    all_cats = sorted(set(list(a_cats.keys()) + list(b_cats.keys())))
    print("\n  Per-category changes:")
    for cat in all_cats:
        av = a_cats.get(cat, {}).get("pct", 0)
        bv = b_cats.get(cat, {}).get("pct", 0)
        d = bv - av
        if abs(d) > 0.1:
            arrow = "↑" if d > 0 else "↓"
            print(f"    {arrow} {cat}: {av}% → {bv}% ({d:+.1f}%)")

    # Per-question flips (if detailed results present)
    a_details = {r["id"]: r for r in a.get("results", [])}
    b_details = {r["id"]: r for r in b.get("results", [])}
    if a_details and b_details:
        flips = [
            (qid, a_details[qid]["correct"], b_details[qid]["correct"])
            for qid in a_details
            if qid in b_details and a_details[qid]["correct"] != b_details[qid]["correct"]
        ]
        if flips:
            print(f"\n  Question flips ({len(flips)}):")
            for qid, a_ok, b_ok in flips[:20]:
                icon = "✅" if b_ok else "❌"
                print(f"    {icon} {qid}: {'correct' if a_ok else 'wrong'} → {'correct' if b_ok else 'wrong'}")

    print(f"{'='*60}\n")


# ─── Custom benchmark run ─────────────────────────────────────────────────

def run_custom_benchmark(args):
    """Run the custom question bank against an LLM backend."""
    with open(args.questions) as f:
        bank = json.load(f)
    questions = bank["questions"]

    if args.category:
        questions = [q for q in questions if q["category"] == args.category]
    if args.limit:
        questions = questions[:args.limit]

    # Configure backend
    if args.backend == "raw":
        base_url = args.base_url or RAW_API_URL
        api_key = None
        model = "default"
        if not args.no_warmup:
            ensure_gpu_llm_mode()
    elif args.backend == "ecolyxis":
        base_url = args.base_url or ECOLYXIS_API_URL
        api_key = args.api_key or os.environ.get("ECOLYXIS_API_KEY")
        if not api_key:
            sys.exit("ERROR: --backend ecolyxis requires ECOLYXIS_API_KEY env or --api-key")
        model = args.model or "ecolyxis-standard"
    else:
        sys.exit(f"Unknown backend: {args.backend}")

    print(f"\n🔬 Ecolyxis Evaluation Harness")
    print(f"  Backend:    {args.backend}")
    print(f"  Model:      {model}")
    print(f"  Base URL:   {base_url}")
    print(f"  Questions:  {len(questions)}")
    if args.category:
        print(f"  Category:   {args.category}")
    print()

    results = []
    errors = 0
    start_time = time.time()

    for i, q in enumerate(questions, 1):
        content, err = call_model(
            base_url, api_key, model, q["prompt"],
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            delay=args.delay,
        )
        if err:
            errors += 1
            correct = False
            ans = f"<error: {err}>"
        else:
            ans = extract_answer(content)
            correct = grade(q, ans, content or "")

        results.append({
            "id": q["id"],
            "category": q["category"],
            "difficulty": q["difficulty"],
            "correct": correct,
            "extracted": ans,
            "expected": q["answer"],
        })

        mark = "✓" if correct else "✗"
        line = f"  [{i:>3}/{len(questions)}] {mark} {q['id']:<12} {q['category']:<16} -> {ans[:50]!r}"
        if args.verbose:
            line += f"  (expected {q['answer']!r})"
        print(line)

        if args.delay and i < len(questions):
            time.sleep(args.delay)

    elapsed = round(time.time() - start_time, 1)
    summary = summarize(results)
    print_report(summary, model, args.backend, errors)

    # Save results
    RESULTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_file = args.out or str(RESULTS_DIR / f"eval_{ts}.json")

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "backend": args.backend,
        "model": model,
        "base_url": base_url,
        "temperature": args.temperature,
        "elapsed_seconds": elapsed,
        "summary": summary,
        "results": results,
    }
    with open(out_file, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"  📁 Results saved: {out_file}")
    return 0


# ─── lm-eval-harness wrapper ─────────────────────────────────────────────

def run_lm_eval(args):
    """Run standard benchmarks via lm-eval-harness."""
    tasks = args.tasks.split(",") if args.tasks else ["gsm8k", "arc_challenge_ecolyxis", "truthfulqa_ecolyxis"]

    base_url = args.base_url or RAW_API_URL
    model_name = args.lm_eval_model or "Qwen_Qwen3.6-35B-A3B-Q4_0.gguf"
    tokenizer = args.tokenizer or "Qwen/Qwen3-30B-A3B"

    model_args = LM_EVAL_MODEL_ARGS_TEMPLATE.format(
        model=model_name, base_url=base_url, tokenizer=tokenizer,
    )

    RESULTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    print(f"\n🔬 lm-eval-harness wrapper")
    print(f"  Tasks:      {', '.join(tasks)}")
    print(f"  Model:      {model_name}")
    print(f"  Base URL:   {base_url}")
    if args.limit:
        print(f"  Limit:      {args.limit} samples/task")
    print()

    overall_start = time.time()
    results = {}

    for task in tasks:
        task_start = time.time()
        safe_name = task.replace("_", "-")
        output_file = str(RESULTS_DIR / f"lmeval_{safe_name}_{ts}.json")
        log_file = str(RESULTS_DIR / f"lmeval_{safe_name}_{ts}.log")

        print(f"  📋 Running: {task}")

        cmd = [
            "lm-eval", "run",
            "--model", LM_EVAL_MODEL,
            "--model_args", model_args,
            "--include_path", str(TASKS_DIR),
            "--tasks", task,
            "--apply_chat_template",
            "--gen_kwargs", "stop=[]",
            "--output_path", output_file,
            "--log_samples",
        ]
        if args.limit:
            cmd.extend(["--limit", str(args.limit)])

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=1800,
            )
            elapsed = round(time.time() - task_start, 1)

            with open(log_file, "w") as f:
                f.write(result.stdout)
                if result.stderr:
                    f.write("\n--- STDERR ---\n")
                    f.write(result.stderr)

            if result.returncode == 0:
                print(f"    ✅ {task}: completed ({elapsed}s)")
            else:
                print(f"    ❌ {task}: failed (exit {result.returncode}, {elapsed}s)")
                if result.stderr:
                    print(f"       {result.stderr[:200]}")
        except subprocess.TimeoutExpired:
            elapsed = round(time.time() - task_start, 1)
            print(f"    ❌ {task}: timed out ({elapsed}s)")
        except FileNotFoundError:
            print("    ❌ lm-eval not found. Install with: pip install lm-eval")
            return 1

    total_elapsed = round(time.time() - overall_start, 1)
    print(f"\n  ⏱ Total time: {total_elapsed}s")
    print(f"  📁 Results: {RESULTS_DIR}/")
    return 0


# ─── Main ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Ecolyxis Evaluation Harness — unified benchmark suite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Subcommands / modes
    ap.add_argument("--self-test", action="store_true",
                    help="Validate the grader offline and exit")
    ap.add_argument("--diff", nargs=2, metavar=("FILE_A", "FILE_B"),
                    help="Compare two result JSON files")
    ap.add_argument("--backend", choices=["raw", "ecolyxis", "lm-eval"],
                    default="raw",
                    help="API backend (default: raw)")
    ap.add_argument("--lm-eval-model",
                    help="GGUF model filename for lm-eval backend")

    # API config
    ap.add_argument("--base-url",
                    help="Override API base URL")
    ap.add_argument("--api-key",
                    help="API key (or set ECOLYXIS_API_KEY env)")
    ap.add_argument("--model",
                    help="Model name (ecolyxis-quick/standard/long/precise)")

    # Run config
    ap.add_argument("--questions", default=str(QUESTIONS_FILE),
                    help="Questions JSON file")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--delay", type=float, default=0.5,
                    help="Seconds between API calls")
    ap.add_argument("--limit", type=int,
                    help="Only run first N questions")
    ap.add_argument("--category",
                    help="Only run one category")
    ap.add_argument("--no-warmup", action="store_true",
                    help="Skip GPU warmup")
    ap.add_argument("--tokenizer",
                    help="Tokenizer for lm-eval backend")
    ap.add_argument("--tasks",
                    help="Comma-separated task list for lm-eval backend")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--out",
                    help="Output JSON file path")

    args = ap.parse_args()

    if args.self_test:
        sys.exit(self_test())

    if args.diff:
        diff_results(args.diff[0], args.diff[1])
        return

    if args.backend == "lm-eval":
        sys.exit(run_lm_eval(args))

    sys.exit(run_custom_benchmark(args))


if __name__ == "__main__":
    main()
