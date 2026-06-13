#!/bin/bash
set -euo pipefail

# Ecolyxis Deploy Script
# Usage: ./deploy.sh [--test] [--no-restart]
#   --test       Run test suite before deploying
#   --no-restart Pull code only, don't restart services

REPO_DIR="/opt/Ecolyxis"
SERVICES=("ecolyxis" "ecolyxis-worker")

cd "$REPO_DIR"

echo "=== Ecolyxis Deploy ==="
echo "Time: $(date)"
echo "Branch: $(git branch --show-current)"
echo "Current: $(git rev-parse --short HEAD)"

# Pull latest
echo ""
echo "[1/4] Pulling latest code..."
git pull origin main
echo "New: $(git rev-parse --short HEAD)"

# Optional test run
if [[ "${1:-}" == "--test" ]]; then
    echo ""
    echo "[2/4] Running test suite..."
    source venv/bin/activate
    python -m pytest tests/ -q --tb=short
    echo "Tests passed."
    shift
fi

# Skip restart?
if [[ "${1:-}" == "--no-restart" ]]; then
    echo ""
    echo "[3/4] Skipping service restart (--no-restart)"
    echo "Done. Restart manually with: sudo systemctl restart ecolyxis ecolyxis-worker"
    exit 0
fi

# Restart services
echo ""
echo "[3/4] Restarting services..."
for svc in "${SERVICES[@]}"; do
    echo "  Restarting $svc..."
    sudo systemctl restart "$svc"
    sleep 2
    if systemctl is-active --quiet "$svc"; then
        echo "  $svc: active"
    else
        echo "  ERROR: $svc failed to start!"
        sudo systemctl status "$svc" --no-pager | tail -10
        exit 1
    fi
done

# Health check
echo ""
echo "[4/4] Health check..."
sleep 3
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health 2>/dev/null || echo "000")
if [[ "$HEALTH" == "200" ]]; then
    echo "  Health: OK (HTTP 200)"
    echo ""
    echo "=== Deploy complete ==="
else
    echo "  WARNING: Health check returned HTTP $HEALTH"
    echo "  Check: journalctl -u ecolyxis -n 20"
fi
