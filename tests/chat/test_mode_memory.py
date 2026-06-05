"""Per-thread mode memory: POST /chat/<id>/mode persists Thread.last_mode and
the chat view restores it as the selected option."""
import uuid

import pytest

from app.models import Thread


def _thread(db, user, **kw):
    t = Thread(id=str(uuid.uuid4()), user_id=user.id, title="T", **kw)
    db.session.add(t)
    db.session.commit()
    return t


def test_set_mode_persists(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)

    resp = client.post(f"/chat/{thread.id}/mode", json={"mode": "long"})
    assert resp.status_code == 204
    with app.app_context():
        assert db.session.get(Thread, thread.id).last_mode == "long"


def test_invalid_mode_rejected(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)
    login_as(user)

    resp = client.post(f"/chat/{thread.id}/mode", json={"mode": "bogus"})
    assert resp.status_code == 400
    with app.app_context():
        assert db.session.get(Thread, thread.id).last_mode is None


def test_other_users_thread_is_404(app, db, make_user, login_as, client):
    owner = make_user(email="owner@example.com", username="owner")
    intruder = make_user(email="intruder@example.com", username="intruder")
    thread = _thread(db, owner)
    login_as(intruder)

    resp = client.post(f"/chat/{thread.id}/mode", json={"mode": "long"})
    assert resp.status_code == 404


def test_view_restores_selected_mode(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user, last_mode="long")
    login_as(user)

    html = client.get(f"/chat/{thread.id}").get_data(as_text=True)
    assert '<option value="long" selected>' in html
    assert '<option value="standard" selected>' not in html


def test_view_defaults_to_standard(app, db, make_user, login_as, client):
    user = make_user()
    thread = _thread(db, user)  # last_mode is None
    login_as(user)

    html = client.get(f"/chat/{thread.id}").get_data(as_text=True)
    assert '<option value="standard" selected>' in html


def test_non_premium_vision_falls_back_to_standard(app, db, make_user, login_as, client):
    user = make_user()  # default user is not premium
    thread = _thread(db, user, last_mode="vision")
    login_as(user)

    html = client.get(f"/chat/{thread.id}").get_data(as_text=True)
    # Vision option isn't rendered for non-premium; selection falls back.
    assert '<option value="standard" selected>' in html
    assert 'value="vision"' not in html
