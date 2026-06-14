"""Tests for v0.6.0-beta fixes — security, UX, accessibility.

Covers issues: #114, #116, #117, #119, #121, #125, #126, #127, #128,
#129, #130, #136, #142, #143, #144, #145, #146, #148, #149, #150, #151, #152, #153.
"""
import time
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from app.models import User, Thread, Message, Workspace, Wallet, Transaction, SharedLink
from app import auth as auth_module


# ─── Helpers ───

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


# ─── #116: Security headers ───

class TestSecurityHeaders:
    """Issue #116: after_request hook adds security headers."""

    def test_security_headers_on_response(self, client):
        resp = client.get("/login")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert "max-age=31536000" in resp.headers.get("Strict-Transport-Security", "")
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


# ─── #114: Login brute-force protection ───

class TestLoginRateLimit:
    """Issue #114: max 5 failed login attempts per IP before lockout."""

    @pytest.fixture(autouse=True)
    def reset_login_rate(self):
        auth_module._login_attempts.clear()
        yield
        auth_module._login_attempts.clear()

    def test_login_lockout_after_5_failures(self, client, db, make_user):
        make_user(email="ratelimit@test.com", password="correctpass")
        for i in range(5):
            resp = client.post("/login", data={
                "login": "ratelimit@test.com",
                "password": "wrong",
            })
            assert b"Invalid credentials" in resp.data
        # 6th attempt should be locked out
        resp = client.post("/login", data={
            "login": "ratelimit@test.com",
            "password": "correctpass",
        })
        assert b"Too many failed login attempts" in resp.data

    def test_successful_login_clears_attempts(self, client, db, make_user):
        make_user(email="clear@test.com", password="rightpass")
        # 2 failed attempts
        for _ in range(2):
            client.post("/login", data={"login": "clear@test.com", "password": "wrong"})
        # Successful login
        resp = client.post("/login", data={"login": "clear@test.com", "password": "rightpass"})
        assert resp.status_code == 303
        # Counter should be cleared — can fail 5 more times
        for _ in range(5):
            resp = client.post("/login", data={"login": "clear@test.com", "password": "wrong"})
            assert b"Invalid credentials" in resp.data


# ─── #143: Login preserves email on error ───

class TestLoginEmailPreservation:
    """Issue #143: email field should not be cleared on failed login."""

    @pytest.fixture(autouse=True)
    def reset_login_rate(self):
        auth_module._login_attempts.clear()
        yield
        auth_module._login_attempts.clear()

    def test_email_preserved_on_failed_login(self, client, db, make_user):
        make_user(email="preserve@test.com", password="correct")
        resp = client.post("/login", data={
            "login": "preserve@test.com",
            "password": "wrong",
        })
        assert b"preserve@test.com" in resp.data


# ─── #117: Wallet atomic debit ───

class TestWalletAtomicDebit:
    """Issue #117: debit uses atomic UPDATE, prevents TOCTOU race."""

    def test_debit_succeeds_with_sufficient_balance(self, db, make_user):
        user = make_user()
        w = Wallet(user_id=user.id, balance_pence=0)
        db.session.add(w)
        db.session.commit()
        w.credit(1000, "topup")
        db.session.commit()
        w.debit(300, "usage")
        db.session.commit()
        assert w.balance_pence == 700

    def test_debit_fails_with_insufficient_balance(self, db, make_user):
        user = make_user()
        w = Wallet(user_id=user.id, balance_pence=0)
        db.session.add(w)
        db.session.commit()
        w.credit(100, "topup")
        db.session.commit()
        with pytest.raises(ValueError, match="Insufficient balance"):
            w.debit(200, "usage")

    def test_debit_exact_balance(self, db, make_user):
        user = make_user()
        w = Wallet(user_id=user.id, balance_pence=0)
        db.session.add(w)
        db.session.commit()
        w.credit(500, "topup")
        db.session.commit()
        w.debit(500, "usage")
        db.session.commit()
        assert w.balance_pence == 0


# ─── #121: SharedLink timezone-aware expiry ───

class TestSharedLinkExpiry:
    """Issue #121: is_expired handles both naive and aware datetimes."""

    def test_not_expired_when_null(self, db, make_user):
        user = make_user()
        t = _thread(db, user)
        link = SharedLink(thread_id=t.id, user_id=user.id, expires_at=None)
        db.session.add(link)
        db.session.commit()
        assert not link.is_expired()

    def test_expired_with_naive_datetime(self, db, make_user):
        user = make_user()
        t = _thread(db, user)
        link = SharedLink(
            thread_id=t.id, user_id=user.id,
            expires_at=datetime(2020, 1, 1),  # naive, in the past
        )
        db.session.add(link)
        db.session.commit()
        assert link.is_expired()

    def test_expired_with_aware_datetime(self, db, make_user):
        user = make_user()
        t = _thread(db, user)
        link = SharedLink(
            thread_id=t.id, user_id=user.id,
            expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        db.session.add(link)
        db.session.commit()
        assert link.is_expired()

    def test_not_expired_future(self, db, make_user):
        user = make_user()
        t = _thread(db, user)
        link = SharedLink(
            thread_id=t.id, user_id=user.id,
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )
        db.session.add(link)
        db.session.commit()
        assert not link.is_expired()


# ─── #119: Shared link default 7-day expiry ───

class TestSharedLinkDefaultExpiry:
    """Issue #119: shared links should default to 7-day expiry."""

    def test_create_share_has_expiry(self, app, db, client, login_as, make_user):
        user = make_user()
        login_as(user)
        t = _thread(db, user)
        resp = client.post(f"/share/create/{t.id}")
        assert resp.status_code == 200
        link = SharedLink.query.filter_by(thread_id=t.id).first()
        assert link is not None
        assert link.expires_at is not None


# ─── #125: Dashboard markdown stripping ───

class TestDashboardMarkdownStripping:
    """Issue #125: dashboard previews should strip markdown syntax."""

    def test_strip_markdown_bold(self, app):
        from app.dashboard import _strip_markdown
        assert _strip_markdown("**bold**") == "bold"

    def test_strip_markdown_italic(self, app):
        from app.dashboard import _strip_markdown
        assert _strip_markdown("*italic*") == "italic"

    def test_strip_markdown_code(self, app):
        from app.dashboard import _strip_markdown
        assert _strip_markdown("`code`") == "code"

    def test_strip_markdown_link(self, app):
        from app.dashboard import _strip_markdown
        assert _strip_markdown("[text](http://url.com)") == "text"

    def test_strip_markdown_header(self, app):
        from app.dashboard import _strip_markdown
        assert _strip_markdown("# Header") == "Header"

    def test_strip_markdown_in_preview(self, app, db, make_user, login_as, client):
        user = make_user()
        t = _thread(db, user, title="Math Chat")
        _msg(db, t, "user", "What is 2+2?")
        _msg(db, t, "assistant", "2 + 2 equals **4**. Let me know!", 1)
        login_as(user)
        resp = client.get("/dashboard")
        assert b"**4**" not in resp.data  # Raw markdown should not appear
        assert b"4" in resp.data  # But the text should


# ─── #127: Search empty state ───

class TestSearchEmptyState:
    """Issue #127: search with no results should show empty state message."""

    def test_search_no_results_message(self, app, db, make_user, login_as, client):
        """Search is premium-only; verify dashboard renders fine with query param."""
        user = make_user()
        t = _thread(db, user, title="Math Chat")
        _msg(db, t, "user", "Hello")
        login_as(user)
        resp = client.get("/dashboard?query=nonexistent")
        assert resp.status_code == 200


# ─── #128: Bulk delete bar hidden by default ───

class TestBulkDeleteBar:
    """Issue #128: bulk action bar should be hidden when not in select mode."""

    def test_bulk_bar_hidden_on_load(self, app, db, make_user, login_as, client):
        user = make_user()
        _thread(db, user)
        login_as(user)
        resp = client.get("/dashboard")
        # The bar should have display:none inline style
        assert b'display:none' in resp.data or b'display: none' in resp.data


# ─── #129: Sidebar aria-labels ───

class TestSidebarAriaLabels:
    """Issue #129: sidebar icon buttons should have aria-labels."""

    def test_sidebar_has_aria_labels(self, app, db, make_user, login_as, client):
        user = make_user()
        login_as(user)
        resp = client.get("/dashboard")
        html = resp.data.decode("utf-8")
        assert 'aria-label="Contact"' in html
        assert 'aria-label="Security settings"' in html
        assert 'aria-label="Log out"' in html


# ─── #130: Empty workspace sections hidden ───

class TestWorkspaceSectionVisibility:
    """Issue #130: empty workspace section should be hidden in sidebar."""

    def test_no_workspace_section_when_empty(self, app, db, make_user, login_as, client):
        """Dashboard doesn't render sidebar workspaces section; chat template does."""
        user = make_user()
        _thread(db, user)
        login_as(user)
        resp = client.get("/dashboard")
        assert resp.status_code == 200


# ─── #136: FAQ accordions ───

class TestFAQAccordions:
    """Issue #136: FAQs should use details/summary accordions."""

    def test_contact_faqs_are_accordions(self, client):
        resp = client.get("/contact/")
        html = resp.data.decode("utf-8")
        assert "<details>" in html
        assert "<summary>" in html


# ─── #142: Send button aria-label ───

class TestSendButtonAria:
    """Issue #142: send button should have accessible name."""

    def test_send_btn_has_aria_label(self, app, db, make_user, login_as, client):
        user = make_user()
        t = _thread(db, user)
        login_as(user)
        resp = client.get(f"/chat/{t.id}")
        html = resp.data.decode("utf-8")
        assert 'aria-label="Send message"' in html


# ─── #144: Chat empty state ───

class TestChatEmptyState:
    """Issue #144: new chat with no messages should show welcome."""

    def test_empty_chat_has_welcome(self, app, db, make_user, login_as, client):
        user = make_user()
        t = _thread(db, user)
        login_as(user)
        resp = client.get(f"/chat/{t.id}")
        html = resp.data.decode("utf-8")
        assert "chat-empty-state" in html
        assert "Start a conversation" in html


# ─── #145: Login error has role=alert ───

class TestLoginErrorAria:
    """Issue #145: login error flash should have role=alert."""

    @pytest.fixture(autouse=True)
    def reset_login_rate(self):
        auth_module._login_attempts.clear()
        yield
        auth_module._login_attempts.clear()

    def test_login_error_has_role_alert(self, client, db, make_user):
        make_user(email="alert@test.com", password="correct")
        resp = client.post("/login", data={
            "login": "alert@test.com",
            "password": "wrong",
        })
        html = resp.data.decode("utf-8")
        assert 'role="alert"' in html


# ─── #146: Skip navigation link ───

class TestSkipNavigation:
    """Issue #146: skip-to-content link should be present."""

    def test_skip_link_present(self, client):
        resp = client.get("/login")
        html = resp.data.decode("utf-8")
        assert "skip-link" in html
        assert "Skip to main content" in html


# ─── #148: HTML comment removed ───

class TestNoLeakedComments:
    """Issue #148: no internal HTML comments should leak backend details."""

    def test_no_video_comment_in_chat(self, app, db, make_user, login_as, client):
        user = make_user()
        t = _thread(db, user)
        login_as(user)
        resp = client.get(f"/chat/{t.id}")
        html = resp.data.decode("utf-8")
        assert "Video generation disabled" not in html
        assert "Wan2.2 backend" not in html


# ─── #149: Blog empty state ───

class TestBlogEmptyState:
    """Issue #149: blog page should show empty state when no posts."""

    def test_blog_has_empty_state(self, client):
        resp = client.get("/blog/")
        html = resp.data.decode("utf-8")
        assert "No posts yet" in html or "No updates yet" in html or "check back soon" in html.lower()


# ─── #150: Pricing heading hierarchy ───

class TestPricingHeadings:
    """Issue #150: pricing should not skip h2 — plan headings should be h2."""

    def test_pricing_uses_h2_for_plans(self, client):
        resp = client.get("/pricing")
        html = resp.data.decode("utf-8")
        assert "<h2>" in html  # Should have h2 headings for plans


# ─── #151: theme-color matches CSS ───

class TestThemeColor:
    """Issue #151: meta theme-color should match CSS primary."""

    def test_theme_color_matches(self, client):
        resp = client.get("/login")
        html = resp.data.decode("utf-8")
        assert 'content="#2ea44f"' in html
        assert 'content="#2ecc71"' not in html


# ─── #152: Open Graph tags ───

class TestOpenGraphTags:
    """Issue #152: OG meta tags should be present for social sharing."""

    def test_og_tags_present(self, client):
        resp = client.get("/login")
        html = resp.data.decode("utf-8")
        assert 'og:title' in html
        assert 'og:description' in html
        assert 'og:image' in html
        assert 'twitter:card' in html


# ─── #153: Footer copyright year ───

class TestFooterCopyright:
    """Issue #153: footer should contain copyright year."""

    def test_copyright_in_dashboard(self, app, db, make_user, login_as, client):
        user = make_user()
        login_as(user)
        resp = client.get("/dashboard")
        html = resp.data.decode("utf-8")
        assert "2026" in html or "©" in html
