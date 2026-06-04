"""Entrypoint for the Ecolyxis generation worker.

Run with: ``venv/bin/python worker.py`` (systemd: ecolyxis-worker.service).
Concurrency via the WORKER_CONCURRENCY env var (default 4).
"""
from app.jobs.worker import main

if __name__ == "__main__":
    main()
