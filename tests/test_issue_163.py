"""Test that JavaScript doesn't leak into the <title> tag (#163)."""
import re
import uuid
from app.models import Thread


def _thread(db, make_user, login_as, client):
    user = make_user()
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title="My Chat")
    db.session.add(t)
    db.session.commit()
    login_as(user)
    return t


def test_chat_title_has_no_js(app, db, make_user, login_as, client):
    """Chat page <title> should contain only the thread title, no JavaScript."""
    thread = _thread(db, make_user, login_as, client)
    resp = client.get(f'/chat/{thread.id}')
    assert resp.status_code == 200

    title_match = re.search(r'<title>(.*?)</title>', resp.text, re.DOTALL)
    assert title_match, "No <title> tag found"
    title_content = title_match.group(1)

    assert '<script' not in title_content, f"Script tag leaked into title: {title_content[:100]}"
    assert 'function ' not in title_content, f"Function leaked into title: {title_content[:100]}"
    assert 'async ' not in title_content, f"Async keyword leaked into title: {title_content[:100]}"
    assert 'My Chat' in title_content


def test_signup_title_has_no_js(app, client):
    """Signup page <title> should contain only 'Sign Up — Ecolyxis', no JavaScript."""
    resp = client.get('/signup')
    assert resp.status_code == 200

    title_match = re.search(r'<title>(.*?)</title>', resp.text, re.DOTALL)
    assert title_match, "No <title> tag found"
    title_content = title_match.group(1)

    assert '<script' not in title_content, f"Script tag leaked into title: {title_content[:100]}"
    assert 'addEventListener' not in title_content, f"JS leaked into title: {title_content[:100]}"
    assert 'Sign Up' in title_content
