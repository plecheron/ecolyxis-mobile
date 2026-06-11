import requests
import logging
import json
import time

from app.utils.tokens import count_tokens, WORKSPACE_CONTEXT_BUDGET
logger = logging.getLogger('ecolyxis.llm')

# Throttle for live thinking-token progress: emit at most every N reasoning
# deltas or every T seconds, whichever comes first. Keeps the durable Redis
# event stream lean (reasoning can run to thousands of tokens).
_THINK_EMIT_EVERY_TOKENS=8
_THINK_EMIT_EVERY_SECONDS = 0.2


def get_workspace_context(thread, max_tokens=WORKSPACE_CONTEXT_BUDGET):
    """Build a string of workspace description + sibling thread summaries.

    Returns None if the thread has no workspace or workspace context is disabled.
    Returns workspace description alone if there are no siblings but a description exists.
    """
    if not thread.workspace_id:
        return None
    if thread.use_workspace_context is False:
        return None

    from app.models import Thread, Message, Workspace

    workspace = Workspace.query.get(thread.workspace_id)
    if not workspace:
        return None

    # Build header with workspace name and description
    header = f"## Workspace: {workspace.name}"
    if workspace.description:
        header += f"\n{workspace.description}"

    siblings = (
        Thread.query
        .filter(
            Thread.workspace_id == thread.workspace_id,
            Thread.id != thread.id,
        )
        .order_by(Thread.updated_at.desc())
        .all()
    )

    sections = []
    total_tokens = 0

    for sib in siblings:
        summary_text = ""
        if sib.summary:
            summary_text = sib.summary
        else:
            # Fallback: title + first user message preview
            first_msg = (
                Message.query
                .filter_by(thread_id=sib.id, role="user")
                .order_by(Message.created_at)
                .first()
            )
            if first_msg and first_msg.content:
                preview = Thread._extract_text(first_msg.content)[:200]
                summary_text = f"{preview}"
            # If we have at least a title, use it
            if not summary_text and sib.title and sib.title != "New Chat":
                summary_text = sib.title

        if not summary_text:
            continue

        section = f"### {sib.title or 'Untitled'}\n{summary_text}"
        section_tokens = count_tokens(section) + 1  # +1 for newline

        if total_tokens + section_tokens > max_tokens:
            break

        sections.append(section)
        total_tokens += section_tokens

    # Build the full context string
    parts = [header]

    if sections:
        parts.append("")
        parts.append("## Related Conversations")
        parts.append("You are in a workspace with multiple related conversations. "
                      "Here are summaries of the other conversations in this workspace:")
        parts.append("")
        parts.append("\n\n".join(sections))
        parts.append("")
        parts.append("Use this context to provide consistent, informed responses across conversations.")

    return "\n".join(parts)


class LLMClient:
    """LLM client that stores config at init time (avoids app context issues in generators)."""

    def __init__(self, base_url, model, system_prompt, max_history=20):
        self.base_url = base_url
        self.model = model
        self.system_prompt = system_prompt
        self.max_history = max_history

    def stream_chat(self, messages, mode="standard"):
        """Stream chat completion. Yields content strings, then a final usage dict.
        
        mode: "standard" (64k, 4 parallel), "long" (200k, 1 parallel), "vision" (64k, mmproj),
              "quick" (64k, 4 parallel, no thinking)
        Sends X-Context-Mode header to trigger the proxy to switch to the right config.
        Thinking tokens (reasoning_content) text is never yielded, but a running
        count is: ``{"thinking_start": True}`` on the first reasoning delta,
        throttled ``{"thinking_progress": n}`` updates as it reasons, and
        ``{"thinking_end": True, "tokens": n}`` when the answer begins. This drives
        a live "still thinking — N tokens" indicator without exposing the reasoning.
        """
        url = f"{self.base_url}/chat/completions"
        enable_thinking = mode != "quick"
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": 65536,
            "temperature": 0.7,
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
        }
        headers = {}
        # quick uses standard backend config (same parallel/ctx)
        proxy_mode = mode if mode not in ("quick", "precise") else "standard"
        if proxy_mode != "standard":
            headers["X-Context-Mode"] = proxy_mode
        thinking_active = False
        reasoning_count = 0
        last_emit_count = 0
        last_emit_t = 0.0
        try:
            resp = requests.post(url, json=payload, headers=headers, stream=True, timeout=300)
            if resp.status_code >= 400:
                error_body = ""
                try:
                    error_body = resp.text[:500]
                except Exception:
                    pass
                logger.error("LLM backend returned %d: %s", resp.status_code, error_body)
                yield f"\n\n⚠️ LLM backend error (HTTP {resp.status_code}). Please try again."
                return
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    # Check for usage info in final chunk
                    usage = chunk.get("usage")
                    if usage:
                        yield {
                            "prompt_tokens": usage.get("prompt_tokens", 0),
                            "completion_tokens": usage.get("completion_tokens", 0),
                        }
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    reasoning = delta.get("reasoning_content", "")
                    content = delta.get("content") or ""
                    # Track thinking state — yield a running token count (a proxy:
                    # one streamed reasoning delta ≈ one token) but never the text.
                    if reasoning:
                        if not thinking_active:
                            thinking_active = True
                            reasoning_count = 0
                            last_emit_count = 0
                            last_emit_t = time.monotonic()
                            yield {"thinking_start": True}
                        reasoning_count += 1
                        now = time.monotonic()
                        if (reasoning_count - last_emit_count >= _THINK_EMIT_EVERY_TOKENS
                                or now - last_emit_t >= _THINK_EMIT_EVERY_SECONDS):
                            last_emit_count = reasoning_count
                            last_emit_t = now
                            yield {"thinking_progress": reasoning_count}
                    if content:
                        if thinking_active:
                            thinking_active = False
                            yield {"thinking_end": True, "tokens": reasoning_count}
                        yield content
                except json.JSONDecodeError:
                    continue
            # Reasoning ran to the end of the stream without any answer content
            # (rare) — still close out the indicator with the final count.
            if thinking_active:
                thinking_active = False
                yield {"thinking_end": True, "tokens": reasoning_count}
        except (requests.RequestException, requests.ConnectionError) as e:
            logger.error("LLM backend error: %s", e)
            yield f"\n\n⚠️ Error contacting LLM: {e}"

    def _parse_content(self, content_text, include_images=True):
        """Parse message content. Returns either a string or an OpenAI content array.
        
        include_images: if True, images are converted to OpenAI vision format with data URLs.
                        if False, images are replaced with [image: filename] placeholders.
        """
        if not content_text:
            return content_text
        
        stripped = content_text.strip()
        if not stripped.startswith('['):
            return content_text
        
        try:
            parts = json.loads(stripped)
            if not isinstance(parts, list):
                return content_text
            
            has_image = any(p.get("type") == "image" for p in parts)
            if not has_image:
                return content_text
            
            if not include_images:
                # Replace images with placeholders, keep text
                text_parts = []
                for p in parts:
                    if p.get("type") == "text":
                        text_parts.append(p.get("text", ""))
                    elif p.get("type") == "image":
                        name = p.get("name", p.get("file", "image"))
                        text_parts.append(f"[image: {name}]")
                return " ".join(t for t in text_parts if t) or content_text
            
            # Include images — convert file references to data URLs for OpenAI API
            import os, base64
            openai_parts = []
            for p in parts:
                if p.get("type") == "text":
                    openai_parts.append({"type": "text", "text": p["text"]})
                elif p.get("type") == "image":
                    data_url = self._resolve_image_url(p)
                    if data_url:
                        openai_parts.append({
                            "type": "image_url",
                            "image_url": {"url": data_url}
                        })
                    else:
                        name = p.get("name", p.get("file", "image"))
                        openai_parts.append({"type": "text", "text": f"[image: {name}]"})
            
            return openai_parts if openai_parts else content_text
        except (json.JSONDecodeError, KeyError, TypeError):
            return content_text

    def _resolve_image_url(self, image_part):
        """Resolve an image part to a data URL. Handles both file references and legacy data URLs."""
        # Legacy: already a data URL
        url = image_part.get("url", "")
        if url.startswith("data:"):
            # Convert webp data URLs to PNG since llama-server can't decode WebP
            if "image/webp" in url:
                try:
                    import base64, io
                    from PIL import Image
                    b64_start = url.index("base64,") + 7
                    img_data = base64.b64decode(url[b64_start:])
                    img = Image.open(io.BytesIO(img_data))
                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    png_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                    return f"data:image/png;base64,{png_b64}"
                except Exception as e:
                    logger.warning("Failed to convert webp data URL to PNG: %s", e)
            return url
        
        # New format: file reference
        filename = image_part.get("file", "")
        if not filename:
            return None
        
        import os, base64, io
        filepath = os.path.join("/opt/Ecolyxis/uploads", filename)
        if not os.path.isfile(filepath):
            logger.warning("Image file not found: %s", filepath)
            return None
        
        ext = filename.rsplit('.', 1)[-1].lower()
        
        # Convert WebP (and other unsupported formats) to PNG since llama-server
        # only supports PNG and JPEG image decoding
        unsupported = {"webp", "gif", "bmp", "tiff", "tif", "svg"}
        if ext in unsupported:
            try:
                from PIL import Image
                img = Image.open(filepath)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
                logger.info("Converted %s to PNG for vision (%d bytes → %d b64)", ext, os.path.getsize(filepath), len(b64))
                return f"data:image/png;base64,{b64}"
            except Exception as e:
                logger.warning("Failed to convert %s to PNG: %s, sending raw", ext, e)
        
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
        
        with open(filepath, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')
        
        return f"data:{mime};base64,{b64}"

    def build_messages(self, thread, mode="standard", workspace_context=None):
        """Build messages from thread history (all DB messages).
        
        mode: determines whether images are included or replaced with placeholders.
              Only "vision" mode includes images; all others strip them.
        
        workspace_context: optional string of sibling thread summaries to inject
                           into the system prompt.
        """
        from app.models import Message

        prompt = thread.system_prompt if thread.system_prompt else self.system_prompt
        
        # Append workspace context to system prompt if provided
        if workspace_context:
            prompt = prompt + "\n\n" + workspace_context
        
        msgs = [{"role": "system", "content": prompt}]

        history = (
            Message.query.filter_by(thread_id=thread.id)
            .order_by(Message.created_at)
            .all()
        )
        recent = history[-self.max_history:] if len(history) > self.max_history else history

        include_images = (mode == "vision")

        for m in recent:
            # Use message_type for fast-path skipping
            if hasattr(m, 'message_type') and m.message_type == 'text':
                msgs.append({"role": m.role, "content": m.content})
            else:
                parsed = self._parse_content(m.content, include_images=include_images)
                msgs.append({"role": m.role, "content": parsed})

        return msgs
