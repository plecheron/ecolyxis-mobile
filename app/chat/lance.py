""""Lance unified generation: text-to-image, image-to-image, image-to-video."

Backend: Lance server (bytedance-research/Lance) on gpu1.

All three endpoints follow the SSE streaming pattern used by the existing
image/video generation routes, proxying progress events from the Lance
backend and saving results to the local DB on completion.
"""

import base64
import io
import json
import os
import uuid
import threading
from flask import request, Response, current_app
from flask_login import login_required, current_user
import requests as req_lib

from app import db
from app.models import Thread, GeneratedImage, GeneratedVideo
from app.chat import chat_bp, check_rate_limit, _ensure_upload_dir, UPLOAD_FOLDER


def _get_lance_url():
    """Return the configured Lance backend URL."""
    url = current_app.config.get("LANCE_URL")
    if not url:
        return None
    return url.rstrip("/")


def _save_remote_image(remote_url):
    """Fetch an image from the remote server and save it locally."""
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
    """Fetch a video from the remote server and save it locally."""
    _ensure_upload_dir()
    vid_resp = req_lib.get(remote_url, timeout=120)
    if vid_resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch video: HTTP {vid_resp.status_code}")
    local_name = f"{uuid.uuid4().hex[:12]}.mp4"
    local_path = os.path.join(UPLOAD_FOLDER, local_name)
    with open(local_path, "wb") as f:
        f.write(vid_resp.content)
    return local_name, local_path


# ─── Lance Text-to-Image ─────────────────────────────────────────────────────

@chat_bp.route("/chat/<string:thread_id>/lance-t2i", methods=["POST"])
@login_required
def lance_t2i(thread_id):
    """Lance text-to-image generation. SSE stream with progress."""
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

    lance_url = _get_lance_url()
    if not lance_url:
        err = json.dumps({"error": "Lance text-to-image is not configured on this server."})
        def _e3():
            yield "data: " + err + "\n\n"
        return Response(_e3(), mimetype="text/event-stream")

    width = int(data.get("width", 1024))
    height = int(data.get("height", 1024))
    _gen_user_id = current_user.id
    _app = current_app._get_current_object()

    gen_stream_url = f"{lance_url}/t2i-stream"

    def stream_t2i():
        with _app.app_context():
            try:
                resp = req_lib.post(
                    gen_stream_url,
                    json={"prompt": prompt, "width": width, "height": height},
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
                            local_name, _ = _save_remote_image(f"{lance_url}/outputs/{event['filename']}")
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
                yield "data: " + json.dumps({"stage": "error", "error": f"Lance service unavailable: {e}"}) + "\n\n"

    return Response(stream_t2i(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── Lance Image-to-Image ────────────────────────────────────────────────────

LANCE_I2I_JOBS = {}
LANCE_I2I_LOCK = threading.Lock()


def _run_lance_i2i(app, user_id, thread_id, image_b64, prompt, width, height,
                   strength, seed, job_id, lance_url, source_image_id):
    """Run Lance image-to-image in background, always saves result."""
    with app.app_context():
        try:
            with LANCE_I2I_LOCK:
                LANCE_I2I_JOBS[job_id]["status"] = "running"

            resp = req_lib.post(
                f"{lance_url}/i2i",
                json={"image": image_b64, "prompt": prompt,
                      "width": width, "height": height,
                      "strength": strength, "seed": seed},
                timeout=600,
            )

            if resp.status_code != 200:
                err_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                raise RuntimeError(err_data.get("error", f"Lance I2I failed: HTTP {resp.status_code}"))

            result = resp.json()
            remote_filename = result.get("filename")
            elapsed = result.get("elapsed_seconds", 0)
            local_name, _ = _save_remote_image(f"{lance_url}/outputs/{remote_filename}")

            from app.models import Message
            img_record = GeneratedImage(
                user_id=user_id,
                thread_id=thread_id,
                prompt=f"[lance-i2i] {prompt[:200]}",
                seed=seed if seed >= 0 else 0,
                width=result.get("width", width),
                height=result.get("height", height),
                filename=local_name,
                parent_id=source_image_id,
            )
            db.session.add(img_record)
            db.session.flush()

            msg_content = json.dumps([
                {"type": "text", "text": f"Lance I2I: {prompt[:100]}"},
                {"type": "image", "file": local_name, "name": local_name,
                 "image_id": img_record.id, "seed": seed if seed >= 0 else 0,
                 "width": result.get("width", width), "height": result.get("height", height)},
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
            try:
                db.session.rollback()
            except Exception:
                pass
            with LANCE_I2I_LOCK:
                LANCE_I2I_JOBS[job_id].update({"status": "error", "error": str(e)})


@chat_bp.route("/chat/<string:thread_id>/lance-i2i", methods=["POST"])
@login_required
def lance_i2i(thread_id):
    """Lance image-to-image. SSE stream with progress."""
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

    lance_url = _get_lance_url()
    if not lance_url:
        err = json.dumps({"error": "Lance image-to-image is not configured on this server."})
        def _e4():
            yield "data: " + err + "\n\n"
        return Response(_e4(), mimetype="text/event-stream")

    _ensure_upload_dir()
    _user_id = current_user.id
    _app = current_app._get_current_object()
    _source_image_id = data.get("source_image_id")

    width = int(data.get("width", 1024))
    height = int(data.get("height", 1024))
    strength = float(data.get("strength", 0.8))
    seed = int(data.get("seed", -1))

    job_id = uuid.uuid4().hex[:12]
    with LANCE_I2I_LOCK:
        LANCE_I2I_JOBS[job_id] = {"status": "pending", "type": "lance-i2i", "prompt": prompt}

    t = threading.Thread(
        target=_run_lance_i2i,
        args=(_app, _user_id, thread_id, image_b64, prompt, width, height,
              strength, seed, job_id, lance_url, _source_image_id),
        daemon=True,
    )
    t.start()

    import time as _time
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


# ─── Lance Image-to-Video ────────────────────────────────────────────────────

@chat_bp.route("/chat/<string:thread_id>/lance-i2v", methods=["POST"])
@login_required
def lance_i2v(thread_id):
    """Lance image-to-video generation. SSE stream with progress."""
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

    lance_url = _get_lance_url()
    if not lance_url:
        err = json.dumps({"error": "Lance image-to-video is not configured on this server."})
        def _e4():
            yield "data: " + err + "\n\n"
        return Response(_e4(), mimetype="text/event-stream")

    width = int(data.get("width", 480))
    height = int(data.get("height", 480))
    frames = int(data.get("frames", 33))
    _gen_user_id = current_user.id
    _app = current_app._get_current_object()

    gen_stream_url = f"{lance_url}/i2v-stream"

    def stream_i2v():
        with _app.app_context():
            try:
                resp = req_lib.post(
                    gen_stream_url,
                    json={"image": image_b64, "prompt": prompt,
                          "width": width, "height": height, "frames": frames},
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
                            local_name, _ = _save_remote_video(f"{lance_url}/outputs/{event['filename']}")
                            vid_record = GeneratedVideo(
                                user_id=_gen_user_id,
                                thread_id=thread_id,
                                prompt=prompt,
                                seed=event.get("seed", 0),
                                width=width,
                                height=height,
                                frames=frames,
                                fps=event.get("fps", 16),
                                filename=local_name,
                                duration_s=event.get("elapsed_s"),
                            )
                            db.session.add(vid_record)
                            db.session.flush()
                            event["url"] = f"/uploads/{local_name}"
                            event["filename"] = local_name
                            event["video_id"] = vid_record.id
                            db.session.commit()
                        except Exception as e:
                            event["error"] = f"Failed to save video: {e}"

                    yield "data: " + json.dumps(event) + "\n\n"

            except req_lib.RequestException as e:
                yield "data: " + json.dumps({"stage": "error", "error": f"Lance service unavailable: {e}"}) + "\n\n"

    return Response(stream_i2v(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
