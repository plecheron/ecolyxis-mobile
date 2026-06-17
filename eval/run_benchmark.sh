#!/bin/bash
# Ecolyxis Evaluation Harness — convenience wrapper
#
# Usage:
#   ./eval/run_benchmark.sh                 # Full custom benchmark (raw backend)
#   ./eval/run_benchmark.sh --smoke         # Quick smoke test (8 questions)
#   ./eval/run_benchmark.sh --ecolyxis      # Against production Ecolyxis API
#   ./eval/run_benchmark.sh --standard      # lm-eval-harness standard benchmarks
#   ./eval/run_benchmark.sh --all           # Both custom + standard

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

SMOKE=false
BACKEND="raw"
RUN_STANDARD=false
RUN_CUSTOM=true
LIMIT=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --smoke)     SMOKE=true; shift ;;
        --ecolyxis)  BACKEND="ecolyxis"; shift ;;
        --standard)  RUN_CUSTOM=false; RUN_STANDARD=true; shift ;;
        --all)       RUN_STANDARD=true; shift ;;
        --limit)     LIMIT="--limit $2"; shift 2 ;;
        *)           echo "Unknown arg: $1"; exit 1 ;;
    esac
done

if $SMOKE; then
    LIMIT="--limit 8"
fi

LIMIT_ARG="${LIMIT:-}"

echo "╔══════════════════════════════════════════════════════════╗"
echo "║       Ecolyxis Evaluation Harness                         ║"
echo "║  $(date)                                        ║"
echo "╚══════════════════════════════════════════════════════════╝"

FAILED=0

if $RUN_CUSTOM; then
    echo ""
    echo "━━━ Custom Intelligence Benchmark ━━━"
    python3 "$SCRIPT_DIR/runner.py" --backend "$BACKEND" $LIMIT_ARG || FAILED=$((FAILED+1))
fi

if $RUN_STANDARD; then
    echo ""
    echo "━━━ lm-eval-harness Standard Benchmarks ━━━"
    python3 "$SCRIPT_DIR/runner.py" --backend lm-eval || FAILED=$((FAILED+1))
fi

echo ""
if [ $FAILED -eq 0 ]; then
    echo "✅ All benchmarks completed successfully"
else
    echo "⚠️  $FAILED benchmark run(s) failed"
fi
