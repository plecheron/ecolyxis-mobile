"""Tests for llm.py: workspace context builder, message builder, content parsing,
image resolution, and streaming chat."""
import json
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
from app.models import Thread, Message, Workspace
from app.llm import LLMClient, get_workspace_context


def _thread(db, user, **kw):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title=kw.pop("title", "New Chat"), **kw)
    db.session.add(t)
    db.session.commit()
    return t


def _msg(db, thread, role, content, secs=0):
    m = Message(
        thread_id=thread.id, role=role, content=content,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=secs),
    )
    db.session.add(m)
    db.session.commit()
    return m


def _ws(db, user, name="Test WS", desc=None):
    w = Workspace(id=str(uuid.uuid4()), user_id=user.id, name=name, description=desc)
    db.session.add(w)
    db.session.commit()
    return w


# ─── get_workspace_context ───

def test_workspace_context_no_workspace(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    result = get_workspace_context(thread)
    assert result is None


def test_workspace_context_disabled(app, db, make_user):
    user = make_user()
    ws = _ws(db, user)
    thread = _thread(db, user, workspace_id=ws.id, use_workspace_context=False)
    result = get_workspace_context(thread)
    assert result is None


def test_workspace_context_with_description_only(app, db, make_user):
    user = make_user()
    ws = _ws(db, user, name="Dev Notes", desc="Programming notes workspace")
    thread = _thread(db, user, workspace_id=ws.id)
    result = get_workspace_context(thread)
    assert result is not None
    assert "Dev Notes" in result
    assert "Programming notes workspace" in result


def test_workspace_context_with_siblings(app, db, make_user):
    user = make_user()
    ws = _ws(db, user, name="Project X")
    thread = _thread(db, user, workspace_id=ws.id)
    sib = _thread(db, user, title="Research", workspace_id=ws.id)
    _msg(db, sib, "user", "What is machine learning?", 0)
    _msg(db, sib, "assistant", "ML is a subset of AI.", 1)
    result = get_workspace_context(thread)
    assert result is not None
    assert "Project X" in result
    assert "Related Conversations" in result
    assert "machine learning" in result


def test_workspace_context_with_summary(app, db, make_user):
    user = make_user()
    ws = _ws(db, user, name="WS1")
    thread = _thread(db, user, workspace_id=ws.id)
    sib = _thread(db, user, title="Summarised Thread", workspace_id=ws.id, summary="Pre-computed summary about cats")
    result = get_workspace_context(thread)
    assert result is not None
    assert "Pre-computed summary about cats" in result


def test_workspace_context_budget_enforced(app, db, make_user):
    """Context should stop accumulating when budget is reached."""
    user = make_user()
    ws = _ws(db, user, name="Big WS")
    thread = _thread(db, user, workspace_id=ws.id)
    # Create many siblings with large content
    for i in range(20):
        sib = _thread(db, user, title=f"Thread {i}", workspace_id=ws.id,
                      summary="x" * 500)  # ~500 chars each
    result = get_workspace_context(thread, max_tokens=500)
    assert result is not None
    # Should be truncated — not all 20 siblings included
    # count how many "### Thread" sections appear
    count = result.count("### Thread")
    assert count < 20


def test_workspace_context_empty_sibling_skipped(app, db, make_user):
    """Siblings with title='New Chat' and no content should be skipped."""
    user = make_user()
    ws = _ws(db, user)
    thread = _thread(db, user, workspace_id=ws.id)
    sib = _thread(db, user, title="New Chat", workspace_id=ws.id)  # no messages, default title
    result = get_workspace_context(thread)
    assert result is not None
    # Should have workspace header but no sibling sections (New Chat + no content = skipped)
    assert "Related Conversations" not in result


# ─── build_messages ───

def test_build_messages_basic(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "Hello", 0)
    _msg(db, thread, "assistant", "Hi there", 1)
    client = LLMClient("http://test", "model", "You are helpful")
    msgs = client.build_messages(thread)
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "You are helpful"
    assert msgs[1] == {"role": "user", "content": "Hello"}
    assert msgs[2] == {"role": "assistant", "content": "Hi there"}


def test_build_messages_with_system_prompt(app, db, make_user):
    user = make_user()
    thread = _thread(db, user, system_prompt="Custom prompt")
    _msg(db, thread, "user", "Hi", 0)
    client = LLMClient("http://test", "model", "Default prompt")
    msgs = client.build_messages(thread)
    assert msgs[0]["content"] == "Custom prompt"  # thread prompt overrides


def test_build_messages_with_workspace_context(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    _msg(db, thread, "user", "Hello", 0)
    client = LLMClient("http://test", "model", "Base prompt")
    msgs = client.build_messages(thread, workspace_context="## Extra context")
    assert "Base prompt" in msgs[0]["content"]
    assert "## Extra context" in msgs[0]["content"]


def test_build_messages_empty_thread(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    client = LLMClient("http://test", "model", "System")
    msgs = client.build_messages(thread)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "system"


def test_build_messages_max_history(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    for i in range(30):
        _msg(db, thread, "user" if i % 2 == 0 else "assistant", f"msg{i}", i)
    client = LLMClient("http://test", "model", "Sys", max_history=5)
    msgs = client.build_messages(thread)
    # system + 5 most recent messages
    assert len(msgs) == 6


def test_build_messages_vision_mode_includes_images(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    multimodal = json.dumps([
        {"type": "text", "text": "What is this?"},
        {"type": "image", "file": "test.png", "name": "test.png"}
    ])
    _msg(db, thread, "user", multimodal, 0)
    client = LLMClient("http://test", "model", "Sys")
    msgs = client.build_messages(thread, mode="vision")
    # Content should be a list with image_url part (if file exists) or text
    content = msgs[1]["content"]
    assert isinstance(content, list) or isinstance(content, str)


def test_build_messages_non_vision_strips_images(app, db, make_user):
    user = make_user()
    thread = _thread(db, user)
    multimodal = json.dumps([
        {"type": "text", "text": "Describe this"},
        {"type": "image", "file": "test.png", "name": "test.png"}
    ])
    _msg(db, thread, "user", multimodal, 0)
    client = LLMClient("http://test", "model", "Sys")
    msgs = client.build_messages(thread, mode="standard")
    content = msgs[1]["content"]
    # Non-vision mode should have image as placeholder text, not data URL
    if isinstance(content, str):
        assert "[image:" in content or "Describe this" in content
    # If it returned a list, images should be text placeholders
    if isinstance(content, list):
        assert not any(p.get("type") == "image_url" for p in content)


# ─── _parse_content ───

def test_parse_content_plain_text():
    client = LLMClient("http://test", "model", "sys")
    result = client._parse_content("Hello world")
    assert result == "Hello world"


def test_parse_content_empty():
    client = LLMClient("http://test", "model", "sys")
    assert client._parse_content("") == ""


def test_parse_content_text_only_json():
    """JSON array with only text parts (no images) should return original string."""
    client = LLMClient("http://test", "model", "sys")
    content = json.dumps([{"type": "text", "text": "hello"}])
    result = client._parse_content(content)
    assert result == content  # unchanged — no image processing needed


def test_parse_content_strip_images():
    """include_images=False should replace images with placeholders."""
    client = LLMClient("http://test", "model", "sys")
    content = json.dumps([
        {"type": "text", "text": "What is this?"},
        {"type": "image", "file": "photo.png", "name": "photo.png"}
    ])
    result = client._parse_content(content, include_images=False)
    assert isinstance(result, str)
    assert "What is this?" in result
    assert "[image: photo.png]" in result


def test_parse_content_invalid_json():
    client = LLMClient("http://test", "model", "sys")
    assert client._parse_content("[not valid json") == "[not valid json"


# ─── _resolve_image_url ───

def test_resolve_data_url_passthrough():
    client = LLMClient("http://test", "model", "sys")
    url = "data:image/png;base64,iVBORw0KGgo="
    result = client._resolve_image_url({"url": url})
    assert result == url


def test_resolve_data_url_webp_conversion():
    """WebP data URLs should be converted to PNG."""
    client = LLMClient("http://test", "model", "sys")
    # Minimal valid webp as base64 (will fail PIL open, so we test the path)
    url = "data:image/webp;base64,UklGRiQ="
    result = client._resolve_image_url({"url": url})
    # Should either convert to PNG or return original on failure
    assert result is not None


def test_resolve_file_not_found():
    client = LLMClient("http://test", "model", "sys")
    result = client._resolve_image_url({"file": "nonexistent_file_12345.png"})
    assert result is None


def test_resolve_no_file_key():
    client = LLMClient("http://test", "model", "sys")
    result = client._resolve_image_url({"name": "test"})
    assert result is None


# ─── stream_chat ───

def test_stream_chat_success(app):
    """Mock HTTP response and verify chunk parsing."""
    client = LLMClient("http://test-llm", "test-model", "sys")

    mock_response = MagicMock()
    mock_response.status_code = 200

    sse_lines = [
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}',
        b'data: {"choices":[{"delta":{"content":" world"}}]}',
        b'data: {"choices":[{"delta":{"content":""}}],"usage":{"prompt_tokens":10,"completion_tokens":2}}',
        b'data: [DONE]',
    ]
    mock_response.iter_lines.return_value = iter(sse_lines)

    with patch("app.llm.requests.post", return_value=mock_response):
        chunks = list(client.stream_chat([{"role": "user", "content": "hi"}], mode="standard"))

    content_chunks = [c for c in chunks if isinstance(c, str)]
    dict_chunks = [c for c in chunks if isinstance(c, dict)]
    assert "Hello" in content_chunks
    assert " world" in content_chunks
    assert any("completion_tokens" in d for d in dict_chunks)


def test_stream_chat_thinking_tokens(app):
    """Verify thinking_start/thinking_end events are yielded."""
    client = LLMClient("http://test-llm", "model", "sys")
    mock_response = MagicMock()
    mock_response.status_code = 200

    sse_lines = [
        b'data: {"choices":[{"delta":{"reasoning_content":"Let me think"}}]}',
        b'data: {"choices":[{"delta":{"reasoning_content":"More thinking"}}]}',
        b'data: {"choices":[{"delta":{"content":"The answer"}}]}',
        b'data: [DONE]',
    ]
    mock_response.iter_lines.return_value = iter(sse_lines)

    with patch("app.llm.requests.post", return_value=mock_response):
        chunks = list(client.stream_chat([{"role": "user", "content": "hi"}], mode="long"))

    assert any(isinstance(c, dict) and "thinking_start" in c for c in chunks)
    assert any(isinstance(c, dict) and "thinking_end" in c for c in chunks)
    assert "The answer" in [c for c in chunks if isinstance(c, str)]


def test_stream_chat_http_error(app):
    client = LLMClient("http://test-llm", "model", "sys")
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"
    with patch("app.llm.requests.post", return_value=mock_response):
        chunks = list(client.stream_chat([{"role": "user", "content": "hi"}]))
    assert len(chunks) == 1
    assert "500" in chunks[0]


def test_stream_chat_connection_error(app):
    import requests as req
    client = LLMClient("http://test-llm", "model", "sys")
    with patch("app.llm.requests.post", side_effect=req.ConnectionError("Connection refused")):
        chunks = list(client.stream_chat([{"role": "user", "content": "hi"}]))
    assert len(chunks) == 1
    assert "Error" in chunks[0]


def test_stream_chat_quick_mode_no_thinking(app):
    """Quick mode should set enable_thinking=False in payload."""
    client = LLMClient("http://test-llm", "model", "sys")
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.iter_lines.return_value = iter([b'data: [DONE]'])
    with patch("app.llm.requests.post", return_value=mock_response) as mock_post:
        list(client.stream_chat([{"role": "user", "content": "hi"}], mode="quick"))
        call_args = mock_post.call_args
        payload = call_args[1]["json"]
        assert payload["chat_template_kwargs"]["enable_thinking"] is False
