"""Media generation handlers — GPU work delegated to ecolyxis-api."""
import base64
import json
import os
import uuid

import requests

from app import db
from app.models import Message, GeneratedImage, GeneratedVideo


def _forward_progress(publish, ev):
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


def _fetch_artifact(remote_url, *, suffix):
    from app.chat import UPLOAD_FOLDER, _ensure_upload_dir

    _ensure_upload_dir()
    resp = requests.get(remote_url, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"failed to fetch artifact: HTTP {resp.status_code}")
    local_name = f"{uuid.uuid4().hex[:12]}{suffix}"
    with open(os.path.join(UPLOAD_FOLDER, local_name), "wb") as f:
        f.write(resp.content)
    return local_name


def _run_image_via_api(job, publish, *, kind, params, label, parent_id=None):
    from app.chat import _save_remote_image
    from app.jobs.api_client import stream_remote_job

    existing = GeneratedImage.query.filter_by(job_id=job.id).first()
    if existing:
        return _image_result(existing)

    result = stream_remote_job(kind, params, publish, client_ref=str(job.id))
    remote_url = result.get("url")
    if not remote_url:
        raise RuntimeError("API did not return image url")

    local_name, _ = _save_remote_image(remote_url)
    seed = result.get("seed", params.get("seed", 0))
    if seed is None or seed < 0:
        seed = 0
    return _persist_image(
        job,
        prompt=params.get("prompt", result.get("prompt", "")),
        seed=seed,
        width=result.get("width", params.get("width", 1024)),
        height=result.get("height", params.get("height", 1024)),
        local_name=local_name,
        label=label or result.get("label", "Generated image"),
        parent_id=parent_id,
    )


def run_image(app, job, publish):
    p = job.params or {}
    prompt = (p.get("prompt") or "").strip()
    return _run_image_via_api(
        job, publish,
        kind="image",
        params={"prompt": prompt, "width": p.get("width", 1024),
                "height": p.get("height", 1024), "seed": p.get("seed", -1)},
        label=f"Generated image: {prompt}",
    )


def run_upscale(app, job, publish):
    p = job.params or {}
    size = p["next_size"]
    return _run_image_via_api(
        job, publish,
        kind="upscale",
        params={"prompt": p.get("prompt", ""), "next_size": size,
                "seed": p.get("seed", 0), "parent_image_id": p.get("parent_image_id")},
        label=f"Upscaled image ({size}x{size})",
        parent_id=p.get("parent_image_id"),
    )


def run_edit(app, job, publish):
    p = job.params or {}
    prompt = (p.get("prompt") or "").strip()
    return _run_image_via_api(
        job, publish,
        kind="edit",
        params={
            "image": p.get("image", ""),
            "prompt": prompt,
            "size": int(p.get("size", 512)),
            "steps": int(p.get("steps", 4)),
            "cfg": float(p.get("cfg", 6.0)),
            "seed": int(p.get("seed", -1)),
            "source_image_id": p.get("source_image_id"),
        },
        label=f"Edited image: {prompt}",
        parent_id=p.get("source_image_id"),
    )


def _run_video_via_api(job, publish, *, kind, params, width, height, frames,
                       parent_image_id=None):
    from app.jobs.api_client import stream_remote_job

    existing = GeneratedVideo.query.filter_by(job_id=job.id).first()
    if existing:
        return _video_result(existing)

    result = stream_remote_job(kind, params, publish, client_ref=str(job.id))
    remote_url = result.get("url")
    if not remote_url:
        raise RuntimeError("API did not return video url")

    local_name = _fetch_artifact(remote_url, suffix=".mp4")
    return _persist_video(
        job,
        prompt=params.get("prompt", result.get("prompt", "")),
        seed=result.get("seed", 0),
        width=result.get("width", width),
        height=result.get("height", height),
        frames=result.get("frames", frames),
        fps=result.get("fps", 16),
        local_name=local_name,
        duration_s=result.get("elapsed_s"),
        parent_image_id=parent_image_id,
    )


def run_video(app, job, publish):
    p = job.params or {}
    width = int(p.get("width", 480))
    height = int(p.get("height", 480))
    frames = int(p.get("frames", 33))
    return _run_video_via_api(
        job, publish,
        kind="video",
        params={"prompt": p.get("prompt", ""), "width": width,
                "height": height, "frames": frames},
        width=width, height=height, frames=frames,
    )


def run_animate(app, job, publish):
    from app.chat import UPLOAD_FOLDER

    p = job.params or {}
    prompt = (p.get("prompt") or "").strip()
    image_filename = (p.get("image_url", "") or "").split("/")[-1]
    image_path = os.path.join(UPLOAD_FOLDER, image_filename)
    if not os.path.exists(image_path):
        raise RuntimeError("source image not found")

    parent_img = GeneratedImage.query.filter_by(filename=image_filename).first()
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("ascii")

    return _run_video_via_api(
        job, publish,
        kind="animate",
        params={"prompt": prompt, "image_b64": image_b64,
                "image_filename": image_filename},
        width=480, height=480, frames=33,
        parent_image_id=parent_img.id if parent_img else None,
    )
