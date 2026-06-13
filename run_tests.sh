#!/bin/bash
set -euo pipefail
cd /opt/Ecolyxis
source venv/bin/activate
echo "Running Ecolyxis test suite..."
python -m pytest tests/ -v --tb=short "$@"
