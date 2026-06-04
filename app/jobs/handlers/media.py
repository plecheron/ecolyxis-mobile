"""Media generation handlers (image / upscale / edit / video / animate).

Each runs one generation against the GPU backends, publishes progress events
into the job's Redis Stream, and persists the artifact idempotently keyed by
``job_id`` (UNIQUE on GeneratedImage / GeneratedVideo). Because they run in the
worker, the work survives client disconnects and web restarts — a gap the
legacy in-request SSE proxies (esp. video/animate, which had no background
fallback) could not close.

Must be called inside an app context (the worker provides one).
"""
import json
import os
import uuid

import requests
from flask import current_app

from app import db
from app.models import Message, GeneratedImage, GeneratedVideo


# --- progress + persistence helpers ---------------------------------------

def _forward_progress(publish, ev):
    """Normalize a remote SSE event into a progress event for the client."""
    out = {"type": "progress"}
    for k in ("stage", "step", "total_steps", "message"):
        if k in ev:
            out[k] = ev[k]
    publish(out)


def _image_result(img):
    return {
        "kind": "image",
        "message_id": img.message_id,
        "image_id": img.id,
        "url": f"/uploads/{img.filename}",
        "filename": img.filename,
        "seed": img.seed,
        "width": img.width,
        "height": img.height,
    }


def _persist_image(job, *, prompt, seed, width, height, local_name, label, parent_id=None):
    """Insert GeneratedImage + assistant Message once for this job (idempotent)."""
    existing = GeneratedImage.query.filter_by(job_id=job.id).first()
    if existing:
        return _image_result(existing)

    img = GeneratedImage(
        job_id=job.id, user_id=job.user_id, thread_id=job.thread_id,
        prompt=prompt, seed=seed, width=width, height=height,
        filename=local_name, parent_id=parent_id,
    )
    db.session.add(img)
    db.session.flush()

    msg = Message(
        job_id=job.id, thread_id=job.thread_id, role="assistant",
        tokens_used=0, message_type="mixed",
        content=json.dumps([
            {"type": "text", "text": label},
            {"type": "image", "file": local_name, "name": local_name,
             "image_id": img.id, "seed": seed, "width": width, "height": height},
        ]),
    )
    db.session.add(msg)
    db.session.flush()
    img.message_id = msg.id
    db.session.commit()
    return _image_result(img)


def _video_result(vid):
    return {
        "kind": "video",
        "video_id": vid.id,
        "url": f"/uploads/{vid.filename}",
        "filename": vid.filename,
        "width": vid.width,
        "height": vid.height,
        "frames": vid.frames,
        "fps": vid.fps,
    }


def _persist_video(job, *, prompt, seed, width, height, frames, fps, local_name,
                   duration_s=None, parent_image_id=None):
    existing = GeneratedVideo.query.filter_by(job_id=job.id).first()
    if existing:
        return _video_result(existing)
    vid = GeneratedVideo(
        job_id=job.id, user_id=job.user_id, thread_id=job.thread_id,
        prompt=prompt, seed=seed or 0, width=width, height=height,
        frames=frames, fps=fps, filename=local_name, duration_s=duration_s,
        parent_image_id=parent_image_id,
    )
    db.session.add(vid)
    db.session.commit()
    return _video_result(vid)


def _stream_remote(url, *, json_body=None, files=None, data=None, timeout=600):
    """POST to a backend SSE endpoint and yield decoded event dicts."""
    resp = requests.post(url, json=json_body, files=files, data=data,
                         timeout=timeout, stream=True)
    if resp.status_code != 200:
        raise RuntimeError(f"backend HTTP {resp.status_code}: {resp.text[:200]}")
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        try:
            yield json.loads(line[6:])
        except json.JSONDecodeError:
            continue


# --- handlers -------------------------------------------------------------

def _run_image_like(app, job, publish, *, width, height, seed, prompt, parent_id, label):
    """Shared generate/upscale path (Z-Image /generate-stream)."""
    from app.chat.images import _get_image_url, _save_remote_image

    existing = GeneratedImage.query.filter_by(job_id=job.id).first()
    if existing:
        return _image_result(existing)

    image_url = _get_image_url()
    if not image_url:
        raise RuntimeError("Image generation is not configured on this server.")

    publish({"type": "progress", "stage": "starting"})
    final = None
    for ev in _stream_remote(f"{image_url}/generate-stream",
                             json_body={"prompt": prompt, "width": width,
                                        "height": height, "seed": seed},
                             timeout=300):
        if ev.get("error"):
            raise RuntimeError(ev["error"])
        if ev.get("stage") == "done" and ev.get("filename"):
            final = ev
            break
        _forward_progress(publish, ev)
    if not final:
        raise RuntimeError("image generation did not complete")

    local_name, _ = _save_remote_image(f"{image_url}/outputs/{final['filename']}")
    return _persist_image(
        job, prompt=prompt,
        seed=final.get("seed", seed if seed and seed >= 0 else 0),
        width=final.get("width", width), height=final.get("height", height),
        local_name=local_name, label=label, parent_id=parent_id,
    )


def run_image(app, job, publish):
    p = job.params or {}
    prompt = (p.get("prompt") or "").strip()
    return _run_image_like(
        app, job, publish,
        width=p.get("width", 1024), height=p.get("height", 1024),
        seed=p.get("seed", -1), prompt=prompt, parent_id=None,
        label=f"Generated image: {prompt}",
    )


def run_upscale(app, job, publish):
    p = job.params or {}
    size = p["next_size"]
    return _run_image_like(
        app, job, publish,
        width=size, height=size, seed=p.get("seed", 0), prompt=p.get("prompt", ""),
        parent_id=p.get("parent_image_id"),
        label=f"Upscaled image ({size}x{size})",
    )


def run_edit(app, job, publish):
    from app.chat.images import _get_edit_url, _save_remote_image

    existing = GeneratedImage.query.filter_by(job_id=job.id).first()
    if existing:
        return _image_result(existing)

    p = job.params or {}
    prompt = (p.get("prompt") or "").strip()
    size = int(p.get("size", 512))
    seed = int(p.get("seed", -1))

    edit_url = _get_edit_url()
    if not edit_url:
        raise RuntimeError("Image editing is not configured on this server.")

    publish({"type": "progress", "stage": "editing", "message": "Editing image…"})
    resp = requests.post(
        f"{edit_url}/edit-json",
        json={"image": p.get("image", ""), "prompt": prompt, "size": size,
              "steps": int(p.get("steps", 4)), "cfg": float(p.get("cfg", 6.0)),
              "seed": seed},
        timeout=3600,
    )
    if resp.status_code != 200:
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        raise RuntimeError(body.get("error", f"Edit failed: HTTP {resp.status_code}"))

    remote_filename = resp.json().get("url", "").split("/")[-1]
    local_name, _ = _save_remote_image(f"{edit_url}/outputs/{remote_filename}")
    return _persist_image(
        job, prompt=f"[edit] {prompt}", seed=seed if seed >= 0 else 0,
        width=size, height=size, local_name=local_name,
        label=f"Edited image: {prompt}", parent_id=p.get("source_image_id"),
    )


def _run_video_like(app, job, publish, *, url, json_body=None, files=None, data=None,
                    prompt, width, height, frames, parent_image_id=None):
    from app.chat import UPLOAD_FOLDER, _ensure_upload_dir

    existing = GeneratedVideo.query.filter_by(job_id=job.id).first()
    if existing:
        return _video_result(existing)

    _ensure_upload_dir()
    publish({"type": "progress", "stage": "starting"})
    final = None
    for ev in _stream_remote(url, json_body=json_body, files=files, data=data, timeout=600):
        if ev.get("error"):
            raise RuntimeError(ev["error"])
        if ev.get("stage") == "done" and ev.get("filename"):
            final = ev
            break
        _forward_progress(publish, ev)
    if not final:
        raise RuntimeError("video generation did not complete")

    wan22_url = current_app.config.get("WAN22_URL").rstrip("/")
    vid_resp = requests.get(f"{wan22_url}/outputs/{final['filename']}", timeout=60)
    if vid_resp.status_code != 200:
        raise RuntimeError(f"failed to fetch video: HTTP {vid_resp.status_code}")
    local_name = f"{uuid.uuid4().hex[:12]}.mp4"
    with open(os.path.join(UPLOAD_FOLDER, local_name), "wb") as f:
        f.write(vid_resp.content)

    return _persist_video(
        job, prompt=prompt, seed=final.get("seed", 0), width=width, height=height,
        frames=frames, fps=final.get("fps", 16), local_name=local_name,
        duration_s=final.get("elapsed_s"), parent_image_id=parent_image_id,
    )


def run_video(app, job, publish):
    p = job.params or {}
    width = int(p.get("width", 480)); height = int(p.get("height", 480))
    frames = int(p.get("frames", 33))
    wan22_url = current_app.config.get("WAN22_URL")
    if not wan22_url:
        raise RuntimeError("Video generation is not configured on this server.")
    return _run_video_like(
        app, job, publish,
        url=f"{wan22_url.rstrip('/')}/generate-stream",
        json_body={"prompt": p.get("prompt", ""), "width": width,
                   "height": height, "frames": frames},
        prompt=p.get("prompt", ""), width=width, height=height, frames=frames,
    )


def run_animate(app, job, publish):
    from app.chat import UPLOAD_FOLDER

    p = job.params or {}
    prompt = (p.get("prompt") or "").strip()
    image_filename = (p.get("image_url", "") or "").split("/")[-1]
    image_path = os.path.join(UPLOAD_FOLDER, image_filename)
    if not os.path.exists(image_path):
        raise RuntimeError("source image not found")

    wan22_url = current_app.config.get("WAN22_URL")
    if not wan22_url:
        raise RuntimeError("Video generation is not configured on this server.")

    parent_img = GeneratedImage.query.filter_by(filename=image_filename).first()
    with open(image_path, "rb") as f:
        img_bytes = f.read()

    import io
    return _run_video_like(
        app, job, publish,
        url=f"{wan22_url.rstrip('/')}/animate",
        files={"image": (image_filename, io.BytesIO(img_bytes))},
        data={"prompt": prompt},
        prompt=prompt, width=480, height=480, frames=33,
        parent_image_id=parent_img.id if parent_img else None,
    )
