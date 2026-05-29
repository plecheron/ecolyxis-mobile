"""
Lance unified generation: text-to-image, image-to-image, image-to-video.

Routes through the existing gpu-manager infrastructure on gpu1:
  - Lance T2I  -> ZImage (:8083) via gpu-manager "image" mode
  - Lance I2I  -> Step1X-Edit (:8087) via gpu-manager "edit" mode (already running)
  - Lance I2V  -> WAN 2.2 (:8085) via gpu-manager "video" mode

Only ONE model can run at a time on the P40. Mode switching is automatic
but will unload the previous model.
"""

import base64
import io
import json
import os
import uuid
import threading
import time as _time
from flask import request, Response, current_app
from flask_login import login_required, current_user
import requests as req_lib

from app import db
from app.models import Thread, GeneratedImage, GeneratedVideo
from app.chat import chat_bp, check_rate_limit, _ensure_upload_dir, UPLOAD_FOLDER

GPU_MANAGER_URL = "http://192.168.122.5:8090"


def _get_backend_url(config_key):
    url = current_app.config.get(config_key)
    if not url:
        return None
    return url.rstrip("/")


def _ensure_mode(mode):
    """Ask gpu-manager to switch to the given mode. Returns True on success."""
    try:
        resp = req_lib.post(
            f"{GPU_MANAGER_URL}/switch",
            json={"mode": mode},
            timeout=180,
        )
        data = resp.json()
        return data.get("status") == "ok" and data.get("mode") == mode
    except Exception as e:
        current_app.logger.error(f"GPU manager switch failed: {e}")
        return False


def _wait_healthy(url, timeout=120, interval=3):
    """Poll a health endpoint until it responds 200 or timeout."""
    health_url = f"{url}/health"
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            r = req_lib.get(health_url, timeout=5)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        _time.sleep(interval)
    return False


def _save_remote_image(remote_url):
    _ensure_upload_dir()
    img_resp = req_lib.get(remote_url, timeout=60)
    if img_resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch image: HTTP {img_resp.status_code}")
    local_name = f"{uuid.uuid4().hex[:12]}.png"
    local_path = os.path.join(UPLOAD_FOLDER, local_name)
    with open(local_path, "wb") as f:
        f.write(img_resp.content)
    return local_name, local_path


def _save_remote_video(remote_url):
    _ensure_upload_dir()
    vid_resp = req_lib.get(remote_url, timeout=120)
    if vid_resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch video: HTTP {vid_resp.status_code}")
    local_name = f"{uuid.uuid4().hex[:12]}.mp4"
    local_path = os.path.join(UPLOAD_FOLDER, local_name)
    with open(local_path, "wb") as f:
        f.write(vid_resp.content)
    return local_name, local_path


# ─── Lance Text-to-Image (ZImage) ──────────────────────────────────────────

@chat_bp.route("/chat/<string:thread_id>/lance-t2i", methods=["POST"])
@login_required
def lance_t2i(thread_id):
    """Lance text-to-image via ZImage. SSE stream with progress."""
    Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    allowed, _, limit = check_rate_limit()
    if not allowed:
        err = json.dumps({"error": "rate_limited", "message": f"Free tier limit reached ({limit} messages per hour)."})
        def _e():
            yield "data: " + err + "\n\n"
        return Response(_e(), mimetype="text/event-stream")

    data = request.get_json()
    prompt = data.get("prompt", "").strip()
    if not prompt:
        err = json.dumps({"error": "Empty prompt"})
        def _e2():
            yield "data: " + err + "\n\n"
        return Response(_e2(), mimetype="text/event-stream")

    hidream_url = _get_backend_url("HIDREAM_URL")
    if not hidream_url:
        err = json.dumps({"error": "Image generation is not configured on this server."})
        def _e3():
            yield "data: " + err + "\n\n"
        return Response(_e3(), mimetype="text/event-stream")

    width = int(data.get("width", 1024))
    height = int(data.get("height", 1024))
    _gen_user_id = current_user.id
    _app = current_app._get_current_object()

    def stream_t2i():
        with _app.app_context():
            try:
                # Switch to image mode
                yield "data: " + json.dumps({"stage": "loading", "message": "Switching GPU to image mode..."}) + "\n\n"
                if not _ensure_mode("image"):
                    yield "data: " + json.dumps({"error": "Failed to switch GPU to image mode"}) + "\n\n"
                    return

                if not _wait_healthy(hidream_url, timeout=120):
                    yield "data: " + json.dumps({"error": "Image model failed to start within timeout"}) + "\n\n"
                    return

                yield "data: " + json.dumps({"stage": "encoding", "message": "Model loaded, encoding prompt..."}) + "\n\n"

                resp = req_lib.post(
                    f"{hidream_url}/generate-stream",
                    json={"prompt": prompt, "width": width, "height": height},
                    timeout=600,
                    stream=True,
                )

                final_event = None
                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    if event.get("stage") == "done" and event.get("filename"):
                        final_event = event
                        try:
                            local_name, _ = _save_remote_image(f"{hidream_url}/outputs/{event['filename']}")
                            img_record = GeneratedImage(
                                user_id=_gen_user_id,
                                thread_id=thread_id,
                                prompt=prompt,
                                seed=event.get("seed", 0),
                                width=event.get("width", width),
                                height=event.get("height", height),
                                filename=local_name,
                            )
                            db.session.add(img_record)
                            db.session.flush()

                            msg_content = json.dumps([
                                {"type": "text", "text": f"Lance T2I: {prompt[:100]}"},
                                {"type": "image", "file": local_name, "name": local_name,
                                 "image_id": img_record.id, "seed": event.get("seed", 0),
                                 "width": event.get("width", width), "height": event.get("height", height)},
                            ])
                            from app.models import Message
                            msg = Message(
                                thread_id=thread_id, role="assistant",
                                content=msg_content, tokens_used=0, message_type="mixed",
                            )
                            db.session.add(msg)
                            img_record.message_id = msg.id
                            db.session.commit()

                            event["url"] = f"/uploads/{local_name}"
                            event["image_id"] = img_record.id
                        except Exception as e:
                            event["error"] = f"Failed to save image: {e}"

                    yield "data: " + json.dumps(event) + "\n\n"

            except req_lib.RequestException as e:
                yield "data: " + json.dumps({"stage": "error", "error": f"Image service unavailable: {e}"}) + "\n\n"

    return Response(stream_t2i(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── Lance Image-to-Image (Step1X-Edit) ────────────────────────────────────

LANCE_I2I_JOBS = {}
LANCE_I2I_LOCK = threading.Lock()


def _run_lance_i2i(app, user_id, thread_id, image_b64, prompt, size, steps, seed,
                   job_id, edit_url, source_image_id):
    """Run Lance I2I via Step1X-Edit /edit-json in background."""
    with app.app_context():
        try:
            with LANCE_I2I_LOCK:
                LANCE_I2I_JOBS[job_id]["status"] = "running"

            resp = req_lib.post(
                f"{edit_url}/edit-json",
                json={"image": image_b64, "prompt": prompt, "size": size,
                      "steps": steps, "cfg": 4.5, "seed": seed},
                timeout=600,
            )

            if resp.status_code != 200:
                err_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                raise RuntimeError(err_data.get("error", f"Edit failed: HTTP {resp.status_code}"))

            result = resp.json()
            remote_filename = result.get("url", "").split("/")[-1]
            elapsed = result.get("elapsed_seconds", 0)
            local_name, _ = _save_remote_image(f"{edit_url}/outputs/{remote_filename}")

            from app.models import Message
            img_record = GeneratedImage(
                user_id=user_id,
                thread_id=thread_id,
                prompt=f"[lance-i2i] {prompt[:200]}",
                seed=seed if seed >= 0 else 0,
                width=size, height=size,
                filename=local_name,
                parent_id=source_image_id,
            )
            db.session.add(img_record)
            db.session.flush()

            msg_content = json.dumps([
                {"type": "text", "text": f"Lance I2I: {prompt[:100]}"},
                {"type": "image", "file": local_name, "name": local_name,
                 "image_id": img_record.id, "seed": seed if seed >= 0 else 0,
                 "width": size, "height": size},
            ])
            msg = Message(
                thread_id=thread_id, role="assistant",
                content=msg_content, tokens_used=0, message_type="mixed",
            )
            db.session.add(msg)
            img_record.message_id = msg.id
            db.session.commit()

            with LANCE_I2I_LOCK:
                LANCE_I2I_JOBS[job_id].update({
                    "status": "done",
                    "url": f"/uploads/{local_name}",
                    "filename": local_name,
                    "image_id": img_record.id,
                    "elapsed_seconds": elapsed,
                })

        except Exception as e:
            with LANCE_I2I_LOCK:
                LANCE_I2I_JOBS[job_id].update({"status": "error", "error": str(e)})


@chat_bp.route("/chat/<string:thread_id>/lance-i2i", methods=["POST"])
@login_required
def lance_i2i(thread_id):
    """Lance image-to-image via Step1X-Edit. SSE stream with progress."""
    Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    allowed, _, limit = check_rate_limit()
    if not allowed:
        err = json.dumps({"error": "rate_limited", "message": f"Free tier limit reached ({limit} messages per hour)."})
        def _e():
            yield "data: " + err + "\n\n"
        return Response(_e(), mimetype="text/event-stream")

    data = request.get_json()
    prompt = data.get("prompt", "").strip()
    if not prompt:
        err = json.dumps({"error": "Prompt required"})
        def _e2():
            yield "data: " + err + "\n\n"
        return Response(_e2(), mimetype="text/event-stream")

    image_b64 = data.get("image", "")
    if not image_b64:
        err = json.dumps({"error": "Source image required"})
        def _e3():
            yield "data: " + err + "\n\n"
        return Response(_e3(), mimetype="text/event-stream")

    edit_url = _get_backend_url("EDIT_URL")
    if not edit_url:
        err = json.dumps({"error": "Image editing is not configured on this server."})
        def _e4():
            yield "data: " + err + "\n\n"
        return Response(_e4(), mimetype="text/event-stream")

    _ensure_upload_dir()
    _user_id = current_user.id
    _app = current_app._get_current_object()
    _source_image_id = data.get("source_image_id")

    size = int(data.get("width", 512))
    steps = int(data.get("steps", 28))
    seed = int(data.get("seed", -1))

    job_id = uuid.uuid4().hex[:12]
    with LANCE_I2I_LOCK:
        LANCE_I2I_JOBS[job_id] = {"status": "pending", "type": "lance-i2i", "prompt": prompt}

    t = threading.Thread(
        target=_run_lance_i2i,
        args=(_app, _user_id, thread_id, image_b64, prompt, size, steps, seed,
              job_id, edit_url, _source_image_id),
        daemon=True,
    )
    t.start()

    def stream_i2i_status():
        last_status = None
        while True:
            with LANCE_I2I_LOCK:
                job = LANCE_I2I_JOBS.get(job_id, {})
            status = job.get("status", "pending")

            if status != last_status:
                if status == "pending":
                    yield "data: " + json.dumps({"stage": "processing", "message": "Queuing...", "job_id": job_id}) + "\n\n"
                elif status == "running":
                    yield "data: " + json.dumps({"stage": "processing", "message": "Processing image...", "job_id": job_id}) + "\n\n"
                elif status == "done":
                    yield "data: " + json.dumps({
                        "stage": "done",
                        "url": job.get("url", ""),
                        "filename": job.get("filename", ""),
                        "image_id": job.get("image_id"),
                        "elapsed_seconds": job.get("elapsed_seconds", 0),
                    }) + "\n\n"
                    break
                elif status == "error":
                    yield "data: " + json.dumps({"error": job.get("error", "Unknown error")}) + "\n\n"
                    break
                last_status = status
            _time.sleep(1)

        _time.sleep(5)
        with LANCE_I2I_LOCK:
            LANCE_I2I_JOBS.pop(job_id, None)

    return Response(stream_i2i_status(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── Lance Image-to-Video (WAN 2.2) ────────────────────────────────────────

@chat_bp.route("/chat/<string:thread_id>/lance-i2v", methods=["POST"])
@login_required
def lance_i2v(thread_id):
    """Lance image-to-video via WAN 2.2 /animate. SSE stream with progress."""
    Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    allowed, _, limit = check_rate_limit()
    if not allowed:
        err = json.dumps({"error": "rate_limited", "message": f"Free tier limit reached ({limit} messages per hour)."})
        def _e():
            yield "data: " + err + "\n\n"
        return Response(_e(), mimetype="text/event-stream")

    data = request.get_json()
    prompt = data.get("prompt", "").strip()
    if not prompt:
        err = json.dumps({"error": "Prompt required"})
        def _e2():
            yield "data: " + err + "\n\n"
        return Response(_e2(), mimetype="text/event-stream")

    image_b64 = data.get("image", "")
    if not image_b64:
        err = json.dumps({"error": "Source image required"})
        def _e3():
            yield "data: " + err + "\n\n"
        return Response(_e3(), mimetype="text/event-stream")

    wan22_url = _get_backend_url("WAN22_URL")
    if not wan22_url:
        err = json.dumps({"error": "Video generation is not configured on this server."})
        def _e4():
            yield "data: " + err + "\n\n"
        return Response(_e4(), mimetype="text/event-stream")

    width = int(data.get("width", 480))
    height = int(data.get("height", 480))
    frames = int(data.get("frames", 33))
    _gen_user_id = current_user.id
    _app = current_app._get_current_object()

    def stream_i2v():
        with _app.app_context():
            try:
                # Switch to video mode
                yield "data: " + json.dumps({"stage": "loading", "message": "Switching GPU to video mode..."}) + "\n\n"
                if not _ensure_mode("video"):
                    yield "data: " + json.dumps({"error": "Failed to switch GPU to video mode"}) + "\n\n"
                    return

                if not _wait_healthy(wan22_url, timeout=120):
                    yield "data: " + json.dumps({"error": "Video model failed to start within timeout"}) + "\n\n"
                    return

                # Decode base64 image to bytes for multipart upload
                image_bytes = base64.b64decode(image_b64)

                yield "data: " + json.dumps({"stage": "encoding", "message": "Model loaded, encoding..."}) + "\n\n"

                # WAN 2.2 /animate accepts multipart form data
                resp = req_lib.post(
                    f"{wan22_url}/animate",
                    files={"image": ("image.png", image_bytes, "image/png")},
                    data={"prompt": prompt, "width": str(width), "height": str(height), "frames": str(frames)},
                    timeout=600,
                    stream=True,
                )

                for line in resp.iter_lines(decode_unicode=True):
                    if not line or not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    if event.get("stage") == "done" and event.get("filename"):
                        try:
                            local_name, _ = _save_remote_video(f"{wan22_url}/outputs/{event['filename']}")
                            vid_record = GeneratedVideo(
                                user_id=_gen_user_id,
                                thread_id=thread_id,
                                prompt=prompt,
                                seed=event.get("seed", 0),
                                width=width,
                                height=height,
                                frames=frames,
                                filename=local_name,
                            )
                            db.session.add(vid_record)
                            db.session.flush()

                            msg_content = json.dumps([
                                {"type": "text", "text": f"Lance I2V: {prompt[:100]}"},
                                {"type": "video", "file": local_name, "name": local_name,
                                 "video_id": vid_record.id, "width": width, "height": height, "frames": frames},
                            ])
                            from app.models import Message
                            msg = Message(
                                thread_id=thread_id, role="assistant",
                                content=msg_content, tokens_used=0, message_type="mixed",
                            )
                            db.session.add(msg)
                            vid_record.message_id = msg.id
                            db.session.commit()

                            event["url"] = f"/uploads/{local_name}"
                            event["video_id"] = vid_record.id
                        except Exception as e:
                            event["error"] = f"Failed to save video: {e}"

                    yield "data: " + json.dumps(event) + "\n\n"

            except req_lib.RequestException as e:
                yield "data: " + json.dumps({"stage": "error", "error": f"Video service unavailable: {e}"}) + "\n\n"

    return Response(stream_i2v(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
