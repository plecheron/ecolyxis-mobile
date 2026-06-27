"""Tests for #167 (404 branding), #168 (blog empty state), #169 (highlight.js scope)."""
import re


# ─── #167: 404 page has branding and extends base.html ───

def test_404_has_branding(app, client):
    """404 page should have Ecolyxis branding (🌿 logo link) (#167)."""
    resp = client.get('/this-page-does-not-exist')
    assert resp.status_code == 404
    assert '🌿' in resp.text, "Ecolyxis brand not found on 404 page"
    assert 'Ecolyxis' in resp.text


def test_404_has_nav_links(app, client):
    """404 page should have navigation links (#167)."""
    resp = client.get('/this-page-does-not-exist')
    assert resp.status_code == 404
    assert 'Go Home' in resp.text or 'href="/"' in resp.text
    assert 'Contact' in resp.text


def test_404_has_theme_toggle(app, client):
    """404 page should have the theme toggle from base.html (#167)."""
    resp = client.get('/this-page-does-not-exist')
    assert resp.status_code == 404
    assert 'toggleTheme' in resp.text, "Theme toggle from base.html missing on 404"


# ─── #168: Blog empty state improved ───

def test_blog_empty_state_has_cta(app, client):
    """Blog page with no posts should show a CTA, not just 'No posts yet' (#168)."""
    resp = client.get('/blog/')
    assert resp.status_code == 200
    # The old dead-end message should be gone
    assert 'No posts yet' not in resp.text
    # New improved empty state with CTAs
    assert 'Get Started' in resp.text or 'signup' in resp.text.lower()


# ─── #169: highlight.js not loaded on non-chat pages ───

def test_highlightjs_not_on_landing(app, client):
    """Landing page should not load highlight.js CDN (#169)."""
    resp = client.get('/')
    assert resp.status_code == 200
    assert 'highlightjs' not in resp.text, "highlight.js loaded on landing page"
    assert 'highlight.min.js' not in resp.text, "highlight.js loaded on landing page"


def test_highlightjs_not_on_contact(app, client):
    """Contact page should not load highlight.js CDN (#169)."""
    resp = client.get('/contact/')
    assert resp.status_code == 200
    assert 'highlightjs' not in resp.text, "highlight.js loaded on contact page"
