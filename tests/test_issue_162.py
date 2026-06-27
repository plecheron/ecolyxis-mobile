"""Tests for #162: server-side markdown rendering survives page reload."""
import re
import uuid
from app.models import Thread, Message
from datetime import datetime, timezone


def _thread_with_msg(db, make_user, login_as, client, role, content):
    """Create a thread with one message, return thread."""
    user = make_user()
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title="Test Chat")
    db.session.add(t)
    m = Message(
        thread_id=t.id, role=role, content=content,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    db.session.add(m)
    db.session.commit()
    login_as(user)
    return t


def _get_message_text(resp):
    """Extract the inner HTML of the .message-text div from the response."""
    match = re.search(r'<div class="message-text">(.*?)</div>\s*', resp.text, re.DOTALL)
    return match.group(1) if match else resp.text


def test_bold_survives_reload(app, db, make_user, login_as, client):
    """**bold** should render as <strong> on server-side reload (#162)."""
    thread = _thread_with_msg(db, make_user, login_as, client, "assistant", "**bold text**")
    resp = client.get(f'/chat/{thread.id}')
    assert resp.status_code == 200
    msg_html = _get_message_text(resp)
    assert '<strong>bold text</strong>' in msg_html, \
        f"Bold not rendered server-side, got: {msg_html[:200]}"


def test_list_recognized_without_blank_line(app, db, make_user, login_as, client):
    """Lists without preceding blank line should render as <ul> (#162 core symptom)."""
    content = "Here is a list:\n- item one\n- item two\n- item three"
    thread = _thread_with_msg(db, make_user, login_as, client, "assistant", content)
    resp = client.get(f'/chat/{thread.id}')
    assert resp.status_code == 200
    msg_html = _get_message_text(resp)
    assert '<ul>' in msg_html, f"List not rendered — should be <ul>, got: {msg_html[:200]}"
    assert '<li>item one</li>' in msg_html, "List items not rendered"
    # The old bug: everything was collapsed to one <p> with <br> tags
    assert '- item one' not in msg_html, "Raw markdown leaked through"


def test_paragraphs_render(app, db, make_user, login_as, client):
    """Paragraphs separated by blank lines should render as <p> tags."""
    content = "First paragraph.\n\nSecond paragraph."
    thread = _thread_with_msg(db, make_user, login_as, client, "assistant", content)
    resp = client.get(f'/chat/{thread.id}')
    assert resp.status_code == 200
    msg_html = _get_message_text(resp)
    assert '<p>First paragraph.</p>' in msg_html
    assert '<p>Second paragraph.</p>' in msg_html


def test_code_block_render(app, db, make_user, login_as, client):
    """Fenced code blocks should render server-side."""
    content = "```python\nprint('hello')\n```"
    thread = _thread_with_msg(db, make_user, login_as, client, "assistant", content)
    resp = client.get(f'/chat/{thread.id}')
    assert resp.status_code == 200
    msg_html = _get_message_text(resp)
    assert '<code' in msg_html.lower()
    assert '<pre>' in msg_html


def test_raw_html_escaped_in_message(app, db, make_user, login_as, client):
    """Raw HTML in message content should be escaped (XSS protection)."""
    content = '<script>alert("xss")</script>'
    thread = _thread_with_msg(db, make_user, login_as, client, "assistant", content)
    resp = client.get(f'/chat/{thread.id}')
    assert resp.status_code == 200
    msg_html = _get_message_text(resp)
    assert '<script>' not in msg_html, "Raw <script> tag leaked through in message"
    assert '&lt;script&gt;' in msg_html, "HTML not escaped properly"


def test_user_message_also_renders(app, db, make_user, login_as, client):
    """User messages should also render markdown on reload."""
    thread = _thread_with_msg(db, make_user, login_as, client, "user", "**important** question")
    resp = client.get(f'/chat/{thread.id}')
    assert resp.status_code == 200
    msg_html = _get_message_text(resp)
    assert '<strong>important</strong>' in msg_html
