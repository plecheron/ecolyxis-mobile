"""Client for the central ecolyxis-api GPU job queue."""
import json
import logging
import os

import requests
from flask import current_app

log = logging.getLogger("ecolyxis.jobs.api_client")


def _cfg(key, default=""):
    val = os.environ.get(key) or current_app.config.get(key, default)
    if not val:
        raise RuntimeError(f"{key} is not configured")
    return val


def _base_url():
    return _cfg("ECOLYXIS_API_URL").rstrip("/")


def _headers():
    return {"X-Ecolyxis-Internal": _cfg("ECOLYXIS_INTERNAL_TOKEN")}


def submit_job(kind, params, *, client_ref=None, priority=0):
    url = f"{_base_url()}/jobs"
    payload = {"kind": kind, "params": params, "priority": priority}
    if client_ref:
        payload["client_ref"] = client_ref
    resp = requests.post(url, json=payload, headers=_headers(), timeout=30)
    if resp.status_code != 202:
        raise RuntimeError(f"API submit failed: HTTP {resp.status_code} {resp.text[:300]}")
    return resp.json()["job_id"]


def get_job(api_job_id):
    resp = requests.get(f"{_base_url()}/jobs/{api_job_id}", headers=_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()


def stream_remote_job(kind, params, publish, *, client_ref=None):
    """Submit to ecolyxis-api, forward SSE events to publish(), return final result."""
    api_job_id = submit_job(kind, params, client_ref=client_ref)
    log.info("remote job %s -> api %s (%s)", client_ref, api_job_id, kind)

    result = None
    resp = requests.get(
        f"{_base_url()}/jobs/{api_job_id}/stream",
        headers=_headers(),
        stream=True,
        timeout=3600,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"API stream failed: HTTP {resp.status_code}")

    resp.encoding = "utf-8"
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw:
            continue
        if raw.startswith(":"):
            continue
        if raw.startswith("data: "):
            try:
                event = json.loads(raw[6:])
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype in ("assigned", "queued"):
                continue
            if etype == "error":
                raise RuntimeError(event.get("message", "remote job failed"))
            if etype == "done":
                result = event
                publish(event)
                break
            publish(event)

    if result is None:
        status = get_job(api_job_id)
        if status.get("status") == "error":
            raise RuntimeError(status.get("error") or "remote job failed")
        result = {"type": "done", **(status.get("result") or {})}
        publish(result)
    return result
