"""Z-Image Turbo image generation and upscaling.

Three endpoints: synchronous generate, SSE-streamed generate, and SSE
upscale (reuses seed and steps up to the next size in GeneratedImage.SIZES).

Backend: Z-Image Turbo server (Flask) on gpu1, managed by gpu-manager.

Image generation runs in a background thread so it completes even if the
client disconnects. A generation status dict is stored in the
GENERATION_JOBS module-level dict, keyed by job_id. The SSE endpoint
streams progress from this dict; on reconnect the client can poll
/chat/<id>/generation-status/<job_id> to get the result.
"""

import json
import os
import uuid
import threading
from flask import request, Response, current_app
from flask_login import login_required, current_user
import requests as req_lib

from app import db
from app.models import Thread, Message, GeneratedImage
from app.chat import chat_bp, check_rate_limit, _ensure_upload_dir, UPLOAD_FOLDER


def _get_image_url():
    """Return the configured image generation backend URL."""
    url = current_app.config.get("HIDREAM_URL") or current_app.config.get("IMAGE_URL")
    if not url:
        return None
    return url.rstrip("/")


def _save_remote_image(remote_url):
    """Fetch an image from the remote server and save it locally.

    Returns (local_filename, local_path) or raises.
    """
    _ensure_upload_dir()
    img_resp = req_lib.get(remote_url, timeout=60)
    if img_resp.status_code != 200:
        raise RuntimeError(f"Failed to fetch image: HTTP {img_resp.status_code}")
    local_name = f"{uuid.uuid4().hex[:12]}.png"
    local_path = os.path.join(UPLOAD_FOLDER, local_name)
    with open(local_path, "wb") as f:
        f.write(img_resp.content)
    return local_name, local_path


# ─── Background generation with completion guarantee ─────────────────────────

# In-memory job tracking: {job_id: {"status": "pending"|"running"|"done"|"error", ...}}
GENERATION_JOBS = {}
GENERATION_JOBS_LOCK = threading.Lock()

EDIT_JOBS = {}
EDIT_JOBS_LOCK = threading.Lock()


def _run_generation(app, user_id, thread_id, prompt, width, height, seed, job_id, image_url):
    """Run image generation in a background thread — always saves result to DB."""
    with app.app_context():
        try:
            with GENERATION_JOBS_LOCK:
                GENERATION_JOBS[job_id]["status"] = "running"

            gen_url = f"{image_url}/generate"

            resp = req_lib.post(
                gen_url,
                json={"prompt": prompt, "width": width, "height": height, "seed": seed},
                timeout=300,
            )

            if resp.status_code != 200:
                raise RuntimeError(f"Image generation failed: HTTP {resp.status_code} {resp.text[:200]}")

            result = resp.json()
            remote_filename = result.get("filename")
            actual_seed = result.get("seed", seed)
            gen_width = result.get("width", width)
            gen_height = result.get("height", height)

            # Save image locally
            local_name, _ = _save_remote_image(f"{image_url}/outputs/{remote_filename}")

            # Create DB records
            img_record = GeneratedImage(
                user_id=user_id,
                thread_id=thread_id,
                prompt=prompt,
                seed=actual_seed,
                width=gen_width,
                height=gen_height,
                filename=local_name,
            )
            db.session.add(img_record)
            db.session.flush()

            # Save an assistant message so the image appears when loading the thread
            msg_content = json.dumps([
                {"type": "text", "text": f"Generated image: {prompt[:100]}"},
                {"type": "image", "file": local_name, "name": local_name,
                 "image_id": img_record.id, "seed": actual_seed,
                 "width": gen_width, "height": gen_height},
            ])
            msg = Message(
                thread_id=thread_id,
                role="assistant",
                content=msg_content,
                tokens_used=0,
                message_type="mixed",
            )
            db.session.add(msg)

            # Update the GeneratedImage with message_id
            img_record.message_id = msg.id
            db.session.commit()

            with GENERATION_JOBS_LOCK:
                GENERATION_JOBS[job_id].update({
                    "status": "done",
                    "url": f"/uploads/{local_name}",
                    "filename": local_name,
                    "image_id": img_record.id,
                    "seed": actual_seed,
                    "width": gen_width,
                    "height": gen_height,
                    "message_id": msg.id,
                })

        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            with GENERATION_JOBS_LOCK:
                GENERATION_JOBS[job_id].update({
                    "status": "error",
                    "error": str(e),
                })


def _run_upscale(app, user_id, thread_id, prompt, seed, next_size, parent_image_id, job_id, image_url):
    """Run image upscale in a background thread — always saves result to DB."""
    with app.app_context():
        try:
            with GENERATION_JOBS_LOCK:
                GENERATION_JOBS[job_id]["status"] = "running"

            gen_url = f"{image_url}/generate"

            resp = req_lib.post(
                gen_url,
                json={"prompt": prompt, "width": next_size, "height": next_size, "seed": seed},
                timeout=300,
            )

            if resp.status_code != 200:
                raise RuntimeError(f"Upscale failed: HTTP {resp.status_code} {resp.text[:200]}")

            result = resp.json()
            remote_filename = result.get("filename")
            local_name, _ = _save_remote_image(f"{image_url}/outputs/{remote_filename}")

            new_img = GeneratedImage(
                user_id=user_id,
                thread_id=thread_id,
                prompt=prompt,
                seed=seed,
                width=next_size,
                height=next_size,
                filename=local_name,
                parent_id=parent_image_id,
            )
            db.session.add(new_img)
            db.session.flush()

            msg_content = json.dumps([
                {"type": "text", "text": f"Upscaled image ({next_size}x{next_size})"},
                {"type": "image", "file": local_name, "name": local_name,
                 "image_id": new_img.id, "seed": seed,
                 "width": next_size, "height": next_size},
            ])
            msg = Message(
                thread_id=thread_id,
                role="assistant",
                content=msg_content,
                tokens_used=0,
                message_type="mixed",
            )
            db.session.add(msg)
            new_img.message_id = msg.id
            db.session.commit()

            with GENERATION_JOBS_LOCK:
                GENERATION_JOBS[job_id].update({
                    "status": "done",
                    "url": f"/uploads/{local_name}",
                    "filename": local_name,
                    "image_id": new_img.id,
                    "seed": seed,
                    "width": next_size,
                    "height": next_size,
                    "message_id": msg.id,
                })

        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass
            with GENERATION_JOBS_LOCK:
                GENERATION_JOBS[job_id].update({
                    "status": "error",
                    "error": str(e),
                })


# ─── Endpoints ────────────────────────────────────────────────────────────────

@chat_bp.route("/chat/<string:thread_id>/generate-image", methods=["POST"])
@login_required
def generate_image_endpoint(thread_id):
    """Generate an image using Z-Image Turbo (synchronous)."""
    Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    allowed, _, limit = check_rate_limit()
    if not allowed:
        return {"error": "rate_limited", "message": f"Free tier limit reached ({limit} messages per hour)."}, 429

    data = request.get_json()
    prompt = data.get("prompt", "").strip()
    if not prompt:
        return {"error": "Empty prompt"}, 400

    width = data.get("width", 1024)
    height = data.get("height", 1024)

    image_url = _get_image_url()
    if not image_url:
        return {"error": "Image generation is not configured on this server."}, 503

    try:
        resp = req_lib.post(
            f"{image_url}/generate",
            json={"prompt": prompt, "width": width, "height": height},
            timeout=300,
        )
        if resp.status_code != 200:
            return {"error": f"Image generation failed: {resp.text[:200]}"}, 502
    except req_lib.RequestException as e:
        return {"error": f"Image service unavailable: {e}"}, 503

    result = resp.json()
    remote_filename = result.get("filename")
    actual_seed = result.get("seed", 42)
    gen_width = result.get("width", width)
    gen_height = result.get("height", height)

    try:
        local_name, _ = _save_remote_image(f"{image_url}/outputs/{remote_filename}")
    except Exception as e:
        return {"error": f"Failed to save generated image: {e}"}, 502

    img_record = GeneratedImage(
        user_id=current_user.id,
        thread_id=thread_id,
        prompt=prompt,
        seed=actual_seed,
        width=gen_width,
        height=gen_height,
        filename=local_name,
    )
    db.session.add(img_record)
    db.session.commit()

    return {
        "url": f"/uploads/{local_name}",
        "filename": local_name,
        "size": [gen_width, gen_height],
        "seed": actual_seed,
        "image_id": img_record.id,
    }


@chat_bp.route("/chat/<string:thread_id>/generate-image-stream", methods=["POST"])
@login_required
def generate_image_stream(thread_id):
    """Start image generation in background, stream progress via SSE.

    Generation runs in a background thread and will complete + save to DB
    even if the client disconnects. Returns a job_id the client can use
    to reconnect and check status.
    """
    Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    allowed, _, limit = check_rate_limit()
    if not allowed:
        err = json.dumps({"error": "rate_limited", "message": f"Free tier limit reached ({limit} messages per hour)."})
        def err_stream():
            yield "data: " + err + "\n\n"
        return Response(err_stream(), mimetype="text/event-stream")

    data = request.get_json()
    prompt = data.get("prompt", "").strip()
    if not prompt:
        err = json.dumps({"error": "Empty prompt"})
        def err_stream2():
            yield "data: " + err + "\n\n"
        return Response(err_stream2(), mimetype="text/event-stream")

    width = data.get("width", 1024)
    height = data.get("height", 1024)
    seed = data.get("seed", -1)

    image_url = _get_image_url()
    if not image_url:
        err = json.dumps({"error": "Image generation is not configured on this server."})
        def err_stream3():
            yield "data: " + err + "\n\n"
        return Response(err_stream3(), mimetype="text/event-stream")

    _ensure_upload_dir()
    _gen_user_id = current_user.id
    _app = current_app._get_current_object()

    # Create a job and start background generation
    job_id = uuid.uuid4().hex[:12]
    with GENERATION_JOBS_LOCK:
        GENERATION_JOBS[job_id] = {
            "status": "pending",
            "type": "generate",
            "prompt": prompt,
            "width": width,
            "height": height,
        }

    # Also send to the SSE stream endpoint on zimage for progress
    gen_stream_url = f"{image_url}/generate-stream"

    # Background watchdog: only falls back to sync if SSE didn't complete after 300s
    t = threading.Thread(
        target=_wait_for_sse_completion,
        args=(_app, _gen_user_id, thread_id, prompt, width, height, seed, job_id, image_url),
        daemon=True,
    )
    t.start()

    # Stream SSE progress to the client
    def stream_proxy():
        import time as _time
        try:
            # Stream the SSE from zimage directly while tracking progress
            resp = req_lib.post(
                gen_stream_url,
                json={"prompt": prompt, "width": width, "height": height, "seed": seed},
                timeout=300,
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

                # If done, save to DB (idempotent — background thread also saves)
                if event.get("stage") == "done" and event.get("filename"):
                    _finalize_generation(job_id, event, image_url, _app, _gen_user_id, thread_id, prompt)

                yield "data: " + json.dumps(event) + "\n\n"

                if event.get("stage") == "done" or event.get("error"):
                    break
        except (req_lib.RequestException, GeneratorExit):
            # Client disconnected — generation continues in background thread
            pass

    return Response(stream_proxy(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _finalize_generation(job_id, event, image_url, app, user_id, thread_id, prompt):
    """Save image to DB. Called from whichever path finishes first (SSE stream or background thread)."""
    with GENERATION_JOBS_LOCK:
        job = GENERATION_JOBS.get(job_id, {})
        if job.get("saved"):
            return  # Already saved by background thread
        job["saved"] = True

    with app.app_context():
        try:
            remote_file = event["filename"]
            local_name, _ = _save_remote_image(f"{image_url}/outputs/{remote_file}")
            actual_seed = event.get("seed", 42)
            gen_w = event.get("width", 1024)
            gen_h = event.get("height", 1024)

            img_record = GeneratedImage(
                user_id=user_id,
                thread_id=thread_id,
                prompt=prompt,
                seed=actual_seed,
                width=gen_w,
                height=gen_h,
                filename=local_name,
            )
            db.session.add(img_record)
            db.session.flush()

            msg_content = json.dumps([
                {"type": "text", "text": f"Generated image: {prompt[:100]}"},
                {"type": "image", "file": local_name, "name": local_name,
                 "image_id": img_record.id, "seed": actual_seed,
                 "width": gen_w, "height": gen_h},
            ])
            msg = Message(
                thread_id=thread_id,
                role="assistant",
                content=msg_content,
                tokens_used=0,
                message_type="mixed",
            )
            db.session.add(msg)
            img_record.message_id = msg.id
            db.session.commit()

            event["url"] = f"/uploads/{local_name}"
            event["filename"] = local_name
            event["image_id"] = img_record.id
            event["width"] = gen_w
            event["height"] = gen_h

            with GENERATION_JOBS_LOCK:
                GENERATION_JOBS[job_id].update({
                    "status": "done",
                    "url": f"/uploads/{local_name}",
                    "filename": local_name,
                    "image_id": img_record.id,
                    "message_id": msg.id,
                })
        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass


def _wait_for_sse_completion(app, user_id, thread_id, prompt, width, height, seed, job_id, image_url):
    """Background watchdog: waits for SSE stream to complete, falls back only if needed.

    Unlike the old approach, this does NOT send a duplicate /generate request
    while the SSE stream is running. It waits for the SSE path to finish, and
    only falls back to sync generate after 300 seconds if SSE didn't complete.
    """
    import time as _time

    # Wait up to 300s for the SSE path to complete
    for _ in range(60):
        _time.sleep(5)
        with GENERATION_JOBS_LOCK:
            job = GENERATION_JOBS.get(job_id, {})
            if job.get("saved") or job.get("status") == "done":
                return

    # SSE didn't complete in time - fallback to sync generate
    with GENERATION_JOBS_LOCK:
        job = GENERATION_JOBS.get(job_id, {})
        if job.get("saved") or job.get("status") == "done":
            return
        job["status"] = "running"

    try:
        with app.app_context():
            _run_generation(app, user_id, thread_id, prompt, width, height, seed, job_id, image_url)
    except Exception as e:
        with GENERATION_JOBS_LOCK:
            GENERATION_JOBS[job_id].update({"status": "error", "error": str(e)})

@chat_bp.route("/chat/<string:thread_id>/generation-status/<job_id>", methods=["GET"])
@login_required
def generation_status(thread_id, job_id):
    """Check the status of a background image generation job."""
    Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    with GENERATION_JOBS_LOCK:
        job = GENERATION_JOBS.get(job_id)
        if job is None:
            return {"error": "Job not found"}, 404
        result = dict(job)
    return result


@chat_bp.route("/chat/<string:thread_id>/upscale-image", methods=["POST"])
@login_required
def upscale_image(thread_id):
    """Upscale a previously generated image to the next size using the same seed."""
    Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    data = request.get_json()
    image_id = data.get("image_id")
    if not image_id:
        return {"error": "image_id required"}, 400

    img = GeneratedImage.query.filter_by(id=image_id, user_id=current_user.id).first_or_404()
    next_size = img.next_size()
    if next_size is None:
        return {"error": "Already at maximum size"}, 400

    image_url = _get_image_url()
    if not image_url:
        err = json.dumps({"error": "Image generation is not configured on this server."})
        def err_stream():
            yield "data: " + err + "\n\n"
        return Response(err_stream(), mimetype="text/event-stream")

    _ensure_upload_dir()
    _user_id = current_user.id
    _prompt = img.prompt
    _seed = img.seed
    _parent_id = img.id
    _app = current_app._get_current_object()

    gen_stream_url = f"{image_url}/generate-stream"

    # Create background job
    job_id = uuid.uuid4().hex[:12]
    with GENERATION_JOBS_LOCK:
        GENERATION_JOBS[job_id] = {
            "status": "pending",
            "type": "upscale",
            "prompt": _prompt,
            "width": next_size,
            "height": next_size,
        }

    # Start background fallback thread
    t = threading.Thread(
        target=_run_upscale_bg,
        args=(_app, _user_id, thread_id, _prompt, _seed, next_size, _parent_id, job_id, image_url),
        daemon=True,
    )
    t.start()

    # Stream SSE progress
    def stream_upscale():
        try:
            resp = req_lib.post(
                gen_stream_url,
                json={"prompt": _prompt, "width": next_size, "height": next_size, "seed": _seed},
                timeout=300,
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
                    _finalize_upscale(job_id, event, image_url, _app, _user_id, thread_id, _prompt, _seed, next_size, _parent_id)

                yield "data: " + json.dumps(event) + "\n\n"

                if event.get("stage") == "done" or event.get("error"):
                    break
        except (req_lib.RequestException, GeneratorExit):
            pass

    return Response(stream_upscale(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _finalize_upscale(job_id, event, image_url, app, user_id, thread_id, prompt, seed, next_size, parent_id):
    """Save upscaled image to DB."""
    with GENERATION_JOBS_LOCK:
        job = GENERATION_JOBS.get(job_id, {})
        if job.get("saved"):
            return
        job["saved"] = True

    with app.app_context():
        try:
            remote_file = event["filename"]
            local_name, _ = _save_remote_image(f"{image_url}/outputs/{remote_file}")

            new_img = GeneratedImage(
                user_id=user_id,
                thread_id=thread_id,
                prompt=prompt,
                seed=seed,
                width=next_size,
                height=next_size,
                filename=local_name,
                parent_id=parent_id,
            )
            db.session.add(new_img)
            db.session.flush()

            msg_content = json.dumps([
                {"type": "text", "text": f"Upscaled image ({next_size}x{next_size})"},
                {"type": "image", "file": local_name, "name": local_name,
                 "image_id": new_img.id, "seed": seed,
                 "width": next_size, "height": next_size},
            ])
            msg = Message(
                thread_id=thread_id,
                role="assistant",
                content=msg_content,
                tokens_used=0,
                message_type="mixed",
            )
            db.session.add(msg)
            new_img.message_id = msg.id
            db.session.commit()

            event["url"] = f"/uploads/{local_name}"
            event["filename"] = local_name
            event["image_id"] = new_img.id
            event["width"] = next_size
            event["height"] = next_size

            with GENERATION_JOBS_LOCK:
                GENERATION_JOBS[job_id].update({
                    "status": "done",
                    "url": f"/uploads/{local_name}",
                    "filename": local_name,
                    "image_id": new_img.id,
                    "message_id": msg.id,
                })
        except Exception as e:
            try:
                db.session.rollback()
            except Exception:
                pass


def _run_upscale_bg(app, user_id, thread_id, prompt, seed, next_size, parent_id, job_id, image_url):
    """Background fallback for upscale."""
    import time as _time
    _time.sleep(5)

    with GENERATION_JOBS_LOCK:
        job = GENERATION_JOBS.get(job_id, {})
        if job.get("saved") or job.get("status") == "done":
            return
        job["status"] = "running"

    try:
        with app.app_context():
            _run_upscale(app, user_id, thread_id, prompt, seed, next_size, parent_id, job_id, image_url)
    except Exception as e:
        with GENERATION_JOBS_LOCK:
            GENERATION_JOBS[job_id].update({"status": "error", "error": str(e)})



# ─── Image editing (Step1X-Edit) ─────────────────────────────────────────────

def _run_edit(app, user_id, thread_id, image_b64, prompt, size, steps, cfg, seed, job_id, edit_url, source_image_id):
    """Run image edit in a background thread — always saves result to DB."""
    with app.app_context():
        try:
            with EDIT_JOBS_LOCK:
                EDIT_JOBS[job_id]["status"] = "running"

            resp = req_lib.post(
                f"{edit_url}/edit-json",
                json={"image": image_b64, "prompt": prompt, "size": size, "steps": steps, "cfg": cfg, "seed": seed},
                timeout=3600,
            )

            if resp.status_code != 200:
                err_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                error_msg = err_data.get("error", f"Edit failed: HTTP {resp.status_code}")
                with EDIT_JOBS_LOCK:
                    EDIT_JOBS[job_id].update({"status": "error", "error": error_msg})
                return

            result = resp.json()
            remote_filename = result.get("url", "").split("/")[-1]
            elapsed = result.get("elapsed_seconds", 0)

            # Fetch and save the edited image locally
            local_name, _ = _save_remote_image(f"{edit_url}/outputs/{remote_filename}")

            # Create DB record
            img_record = GeneratedImage(
                user_id=user_id,
                thread_id=thread_id,
                prompt=f"[edit] {prompt[:200]}",
                seed=seed if seed >= 0 else 0,
                width=size,
                height=size,
                filename=local_name,
                parent_id=source_image_id,
            )
            db.session.add(img_record)
            db.session.flush()

            # Save assistant message
            msg_content = json.dumps([
                {"type": "text", "text": f"Edited image: {prompt[:100]}"},
                {"type": "image", "file": local_name, "name": local_name,
                 "image_id": img_record.id, "seed": seed if seed >= 0 else 0,
                 "width": size, "height": size},
            ])
            msg = Message(
                thread_id=thread_id,
                role="assistant",
                content=msg_content,
                tokens_used=0,
                message_type="mixed",
            )
            db.session.add(msg)
            img_record.message_id = msg.id
            db.session.commit()

            with EDIT_JOBS_LOCK:
                EDIT_JOBS[job_id].update({
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
            with EDIT_JOBS_LOCK:
                EDIT_JOBS[job_id].update({"status": "error", "error": str(e)})


def _get_edit_url():
    """Return the configured image editing backend URL."""
    url = current_app.config.get("EDIT_URL")
    if not url:
        return None
    return url.rstrip("/")


@chat_bp.route("/chat/<string:thread_id>/edit-image", methods=["POST"])
@login_required
def edit_image_endpoint(thread_id):
    """Edit an image using Step1X-Edit.

    Accepts a JSON body with:
      - image: base64-encoded source image (required)
      - prompt: text edit instruction (required)
      - source_image_id: ID of a GeneratedImage to track lineage (optional)
      - size: output size, one of [512, 768, 1024] (default 512)
      - steps: inference steps (default 8)
      - cfg: CFG scale (default 6.0)
      - seed: reproducibility seed (default -1 = random)

    Returns the edited image as an SSE stream with progress, or a JSON error.
    """
    Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    allowed, _, limit = check_rate_limit()
    if not allowed:
        err = json.dumps({"error": "rate_limited", "message": f"Free tier limit reached ({limit} messages per hour)."})
        def err_stream():
            yield "data: " + err + "\n\n"
        return Response(err_stream(), mimetype="text/event-stream")

    data = request.get_json()
    prompt = data.get("prompt", "").strip()
    if not prompt:
        err = json.dumps({"error": "Edit instruction required"})
        def err_stream2():
            yield "data: " + err + "\n\n"
        return Response(err_stream2(), mimetype="text/event-stream")

    image_b64 = data.get("image", "")
    if not image_b64:
        err = json.dumps({"error": "Source image required"})
        def err_stream3():
            yield "data: " + err + "\n\n"
        return Response(err_stream3(), mimetype="text/event-stream")

    edit_url = _get_edit_url()
    if not edit_url:
        err = json.dumps({"error": "Image editing is not configured on this server."})
        def err_stream4():
            yield "data: " + err + "\n\n"
        return Response(err_stream4(), mimetype="text/event-stream")

    _ensure_upload_dir()
    _edit_user_id = current_user.id
    _app = current_app._get_current_object()
    _source_image_id = data.get("source_image_id")

    # Edit doesn't stream progress from step1x-edit (it's a single request),
    # so we stream our own progress indicator while waiting.
    size = int(data.get("size", 512))
    steps = int(data.get("steps", 4))
    cfg = float(data.get("cfg", 6.0))
    seed = int(data.get("seed", -1))

    # Start background edit — runs independently of client connection
    job_id = uuid.uuid4().hex[:12]
    with EDIT_JOBS_LOCK:
        EDIT_JOBS[job_id] = {
            "status": "pending",
            "type": "edit",
            "prompt": prompt,
        }

    t = threading.Thread(
        target=_run_edit,
        args=(_app, _edit_user_id, thread_id, image_b64, prompt, size, steps, cfg, seed, job_id, edit_url, _source_image_id),
        daemon=True,
    )
    t.start()

    # SSE stream polls the background job — client can disconnect safely
    import time as _time
    def stream_edit_status():
        last_status = None
        while True:
            with EDIT_JOBS_LOCK:
                job = EDIT_JOBS.get(job_id, {})
            status = job.get("status", "pending")

            if status != last_status:
                if status == "pending":
                    yield "data: " + json.dumps({"stage": "editing", "message": "Queuing edit...", "job_id": job_id}) + "\n\n"
                elif status == "running":
                    yield "data: " + json.dumps({"stage": "editing", "message": "Editing image...", "job_id": job_id}) + "\n\n"
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

        # Clean up completed job after a short delay
        _time.sleep(5)
        with EDIT_JOBS_LOCK:
            EDIT_JOBS.pop(job_id, None)

    return Response(stream_edit_status(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@chat_bp.route("/chat/<string:thread_id>/edit-status/<job_id>", methods=["GET"])
@login_required
def edit_status(thread_id, job_id):
    """SSE stream that reconnects to an in-progress edit job.

    Returns the same SSE events as the edit endpoint. Used when the client
    reconnects after a disconnect (screen lock, tab refresh, etc).
    """
    Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    import time as _time

    def stream_reconnect():
        last_status = None
        while True:
            with EDIT_JOBS_LOCK:
                job = EDIT_JOBS.get(job_id)
            if job is None:
                # Job already cleaned up — must have completed. Check DB for result.
                yield "data: " + json.dumps({"stage": "done", "reconnected": True, "message": "Edit already completed — refresh to see result."}) + "\n\n"
                break

            status = job.get("status", "pending")
            if status != last_status:
                if status == "pending":
                    yield "data: " + json.dumps({"stage": "editing", "message": "Edit queued..."}) + "\n\n"
                elif status == "running":
                    yield "data: " + json.dumps({"stage": "editing", "message": "Editing image..."}) + "\n\n"
                elif status == "done":
                    yield "data: " + json.dumps({
                        "stage": "done",
                        "url": job.get("url", ""),
                        "filename": job.get("filename", ""),
                        "image_id": job.get("image_id"),
                        "elapsed_seconds": job.get("elapsed_seconds", 0),
                    }) + "\n\n"
                    # Clean up
                    _time.sleep(5)
                    with EDIT_JOBS_LOCK:
                        EDIT_JOBS.pop(job_id, None)
                    break
                elif status == "error":
                    yield "data: " + json.dumps({"error": job.get("error", "Unknown error")}) + "\n\n"
                    with EDIT_JOBS_LOCK:
                        EDIT_JOBS.pop(job_id, None)
                    break
                last_status = status
            _time.sleep(1)

    return Response(stream_reconnect(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
