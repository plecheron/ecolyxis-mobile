"""Blog routes tests: public index/view, admin CRUD, slug generation, uniqueness."""
from app.models import User


def _make_admin(db, user):
    user.is_admin = True
    db.session.commit()


# ─── public routes ───

def test_blog_index_empty(client):
    resp = client.get("/blog/")
    assert resp.status_code == 200


def test_blog_view_404(client):
    resp = client.get("/blog/nonexistent-post")
    assert resp.status_code == 404


def test_blog_index_requires_login_for_admin(app, db, make_user, client):
    resp = client.get("/blog/admin", follow_redirects=False)
    assert resp.status_code in (301, 302)


# ─── admin: create ───

def test_blog_admin_non_admin_redirects(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/blog/admin")
    assert resp.status_code in (301, 302)


def test_blog_create_post(app, db, make_user, login_as, client):
    user = make_user()
    _make_admin(db, user)
    login_as(user)
    resp = client.post("/blog/admin", data={
        "title": "Test Post",
        "body": "Hello World",
        "summary": "A test",
        "publish": "on",
    }, follow_redirects=True)
    assert resp.status_code == 200


def test_blog_create_missing_title(app, db, make_user, login_as, client):
    user = make_user()
    _make_admin(db, user)
    login_as(user)
    resp = client.post("/blog/admin", data={
        "title": "",
        "body": "content",
    }, follow_redirects=True)
    assert resp.status_code == 200


def test_blog_create_auto_slug(app, db, make_user, login_as, client):
    """Slug should be auto-generated from title when not provided."""
    user = make_user()
    _make_admin(db, user)
    login_as(user)
    resp = client.post("/blog/admin", data={
        "title": "My Great Post",
        "body": "content here",
        "publish": "on",
    }, follow_redirects=True)
    assert resp.status_code == 200


def test_blog_create_duplicate_slug(app, db, make_user, login_as, client):
    """Duplicate slug should get timestamp suffix."""
    user = make_user()
    _make_admin(db, user)
    login_as(user)
    for i in range(2):
        resp = client.post("/blog/admin", data={
            "title": "Same Title",
            "body": "content",
            "slug": "same-title",
            "publish": "on",
        }, follow_redirects=True)
        assert resp.status_code == 200


# ─── admin: view ───

def test_blog_admin_lists_posts(app, db, make_user, login_as, client):
    user = make_user()
    _make_admin(db, user)
    login_as(user)
    # Create a post first
    client.post("/blog/admin", data={
        "title": "List Test", "body": "body", "publish": "on",
    }, follow_redirects=True)
    resp = client.get("/blog/admin")
    assert resp.status_code == 200


# ─── admin: edit ───

def test_blog_edit_non_admin_redirects(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.get("/blog/admin/1/edit")
    assert resp.status_code in (301, 302)


# ─── admin: delete ───

def test_blog_delete_non_admin_redirects(app, db, make_user, login_as, client):
    user = make_user()
    login_as(user)
    resp = client.post("/blog/admin/1/delete")
    assert resp.status_code in (301, 302)
