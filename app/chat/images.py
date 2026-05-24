"""Z-Image Turbo image generation and upscaling.

Three endpoints: synchronous generate, SSE-streamed generate, and SSE
upscale (reuses seed and steps up to the next size in GeneratedImage.SIZES).

Backend: Z-Image Turbo server (Flask) on host01, tunnelled to VPS via
image-tunnel.service on port 8083.
"""
import json
import os
import uuid
from flask import request, Response, current_app
from flask_login import login_required, current_user
import requests as req_lib

from app import db
from app.models import Thread, GeneratedImage
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

    # Fetch the generated image from the backend and save locally
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
    """SSE proxy: stream image generation progress from Z-Image Turbo."""
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

    image_url = _get_image_url()
    if not image_url:
        err = json.dumps({"error": "Image generation is not configured on this server."})
        def err_stream3():
            yield "data: " + err + "\n\n"
        return Response(err_stream3(), mimetype="text/event-stream")

    gen_url = f"{image_url}/generate-stream"

    _ensure_upload_dir()
    _gen_user_id = current_user.id
    _app = current_app._get_current_object()

    def stream_proxy():
        with _app.app_context():
            try:
                resp = req_lib.post(
                    gen_url,
                    json={"prompt": prompt, "width": width, "height": height},
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
                        remote_file = event["filename"]
                        try:
                            local_name, _ = _save_remote_image(
                                f"{image_url}/outputs/{remote_file}"
                            )
                            actual_seed = event.get("seed", 42)
                            gen_w = event.get("width", width)
                            gen_h = event.get("height", height)
                            img_record = GeneratedImage(
                                user_id=_gen_user_id,
                                thread_id=thread_id,
                                prompt=prompt,
                                seed=actual_seed,
                                width=gen_w,
                                height=gen_h,
                                filename=local_name,
                            )
                            db.session.add(img_record)
                            db.session.flush()
                            event["url"] = f"/uploads/{local_name}"
                            event["filename"] = local_name
                            event["image_id"] = img_record.id
                            event["width"] = gen_w
                            event["height"] = gen_h
                            db.session.commit()
                        except Exception as e:
                            event["error"] = f"Failed to save image: {e}"

                    yield "data: " + json.dumps(event) + "\n\n"

                    if event.get("stage") == "done" or event.get("error"):
                        break
            except req_lib.RequestException as e:
                err_payload = json.dumps({"error": "Image service unavailable: " + str(e)})
                yield "data: " + err_payload + "\n\n"

    return Response(stream_proxy(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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

    gen_url = f"{image_url}/generate-stream"

    _ensure_upload_dir()
    _user_id = current_user.id
    _prompt = img.prompt
    _seed = img.seed
    _parent_id = img.id
    _app = current_app._get_current_object()

    def stream_upscale():
        with _app.app_context():
            try:
                resp = req_lib.post(
                    gen_url,
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
                        remote_file = event["filename"]
                        try:
                            local_name, _ = _save_remote_image(
                                f"{image_url}/outputs/{remote_file}"
                            )
                            new_img = GeneratedImage(
                                user_id=_user_id,
                                thread_id=thread_id,
                                prompt=_prompt,
                                seed=_seed,
                                width=next_size,
                                height=next_size,
                                filename=local_name,
                                parent_id=_parent_id,
                            )
                            db.session.add(new_img)
                            db.session.flush()
                            event["url"] = f"/uploads/{local_name}"
                            event["filename"] = local_name
                            event["image_id"] = new_img.id
                            event["width"] = next_size
                            event["height"] = next_size
                            db.session.commit()
                        except Exception as e:
                            event["error"] = f"Failed to save image: {e}"

                    yield "data: " + json.dumps(event) + "\n\n"

                    if event.get("stage") == "done" or event.get("error"):
                        break
            except req_lib.RequestException as e:
                err_payload = json.dumps({"error": "Image service unavailable: " + str(e)})
                yield "data: " + err_payload + "\n\n"

    return Response(stream_upscale(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
