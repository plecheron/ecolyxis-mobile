"""Wan2.2 video generation: text-to-video and image-to-video."""
import io
import json
import os
import uuid
from flask import request, Response, current_app
from flask_login import login_required, current_user
import requests as req_lib

from app import db
from app.models import Thread, GeneratedImage, GeneratedVideo
from app.chat import chat_bp, check_rate_limit, _ensure_upload_dir, UPLOAD_FOLDER


@chat_bp.route("/chat/<string:thread_id>/generate-video-stream", methods=["POST"])
@login_required
def generate_video_stream(thread_id):
    """SSE proxy: stream video generation progress from Wan2.2 server."""
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

    width = int(data.get("width", 480))
    height = int(data.get("height", 480))
    frames = int(data.get("frames", 33))

    wan22_url = current_app.config.get("WAN22_URL", "http://10.0.0.1:8085")
    gen_url = f"{wan22_url}/generate-stream"

    _ensure_upload_dir()
    _gen_user_id = current_user.id
    _app = current_app._get_current_object()

    def stream_video():
        with _app.app_context():
            try:
                resp = req_lib.post(
                    gen_url,
                    json={"prompt": prompt, "width": width, "height": height, "frames": frames},
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
                        vid_url = f"{wan22_url}/outputs/{event['filename']}"
                        try:
                            vid_resp = req_lib.get(vid_url, timeout=30)
                            if vid_resp.status_code == 200:
                                local_name = f"{uuid.uuid4().hex[:12]}.mp4"
                                local_path = os.path.join(UPLOAD_FOLDER, local_name)
                                with open(local_path, "wb") as vidf:
                                    vidf.write(vid_resp.content)
                                event["url"] = f"/uploads/{local_name}"
                                event["filename"] = local_name

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
                                event["video_id"] = vid_record.id
                                db.session.commit()
                        except Exception as e:
                            event["error"] = f"Failed to save video: {e}"

                    yield "data: " + json.dumps(event) + "\n\n"

            except req_lib.RequestException as e:
                yield "data: " + json.dumps({"stage": "error", "error": f"Video service unavailable: {e}"}) + "\n\n"

    return Response(stream_video(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@chat_bp.route("/chat/<string:thread_id>/animate-image", methods=["POST"])
@login_required
def animate_image(thread_id):
    """SSE proxy: animate an existing image using Wan2.2 I2V."""
    Thread.query.filter_by(id=thread_id, user_id=current_user.id).first_or_404()

    allowed, _, _ = check_rate_limit()
    if not allowed:
        err = json.dumps({"error": "rate_limited"})
        def err_stream():
            yield "data: " + err + "\n\n"
        return Response(err_stream(), mimetype="text/event-stream")

    data = request.get_json()
    prompt = data.get("prompt", "").strip()
    image_url = data.get("image_url", "").strip()
    if not prompt or not image_url:
        return {"error": "Missing prompt or image_url"}, 400

    image_filename = image_url.split("/")[-1]
    image_path = os.path.join(UPLOAD_FOLDER, image_filename)
    if not os.path.exists(image_path):
        return {"error": "Image not found"}, 404

    wan22_url = current_app.config.get("WAN22_URL", "http://10.0.0.1:8085")
    gen_url = f"{wan22_url}/animate"

    _ensure_upload_dir()
    _gen_user_id = current_user.id
    _app = current_app._get_current_object()

    def stream_animate():
        with _app.app_context():
            try:
                with open(image_path, "rb") as imgf:
                    img_data = imgf.read()

                files = {"image": (image_filename, io.BytesIO(img_data))}
                data_dict = {"prompt": prompt}

                resp = req_lib.post(
                    gen_url,
                    files=files,
                    data=data_dict,
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
                        vid_url = f"{wan22_url}/outputs/{event['filename']}"
                        try:
                            vid_resp = req_lib.get(vid_url, timeout=30)
                            if vid_resp.status_code == 200:
                                local_name = f"{uuid.uuid4().hex[:12]}.mp4"
                                local_path = os.path.join(UPLOAD_FOLDER, local_name)
                                with open(local_path, "wb") as vidf:
                                    vidf.write(vid_resp.content)
                                event["url"] = f"/uploads/{local_name}"
                                event["filename"] = local_name

                                parent_img = GeneratedImage.query.filter_by(filename=image_filename).first()

                                vid_record = GeneratedVideo(
                                    user_id=_gen_user_id,
                                    thread_id=thread_id,
                                    prompt=prompt,
                                    width=480,
                                    height=480,
                                    frames=33,
                                    fps=16,
                                    filename=local_name,
                                    duration_s=event.get("elapsed_s"),
                                    parent_image_id=parent_img.id if parent_img else None,
                                )
                                db.session.add(vid_record)
                                db.session.flush()
                                event["video_id"] = vid_record.id
                                db.session.commit()
                        except Exception as e:
                            event["error"] = f"Failed to save video: {e}"

                    yield "data: " + json.dumps(event) + "\n\n"

            except req_lib.RequestException as e:
                yield "data: " + json.dumps({"stage": "error", "error": f"Video service unavailable: {e}"}) + "\n\n"

    return Response(stream_animate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
