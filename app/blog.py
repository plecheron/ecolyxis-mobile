from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from app import db
from datetime import datetime, timezone

blog_bp = Blueprint("blog", __name__, url_prefix="/blog")


@blog_bp.route("/")
def index():
    from app.post_model import Post
    posts = Post.query.filter_by(published=True).order_by(Post.created_at.desc()).all()
    return render_template("blog/index.html", posts=posts)


@blog_bp.route("/<slug>")
def view(slug):
    from app.post_model import Post
    post = Post.query.filter_by(slug=slug, published=True).first_or_404()
    return render_template("blog/post.html", post=post)


# --- Admin: manage posts ---

@blog_bp.route("/admin", methods=["GET", "POST"])
@login_required
def admin():
    if not current_user.is_admin:
        return redirect(url_for("blog.index"))
    from app.post_model import Post
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        slug = request.form.get("slug", "").strip().lower()
        body = request.form.get("body", "").strip()
        summary = request.form.get("summary", "").strip()
        published = "publish" in request.form

        if not title or not body:
            flash("Title and body are required.", "error")
        else:
            if not slug:
                slug = title.lower().replace(" ", "-").replace("/", "-")[:80]
                # Remove non-alphanumeric
                import re
                slug = re.sub(r"[^a-z0-9\-]", "", slug).strip("-")

            # Check slug uniqueness
            existing = Post.query.filter_by(slug=slug).first()
            if existing:
                slug = slug + "-" + str(int(datetime.now(timezone.utc).timestamp()))

            post = Post(
                title=title,
                slug=slug,
                body=body,
                summary=summary or body[:160],
                author_id=current_user.id,
                published=published,
            )
            db.session.add(post)
            db.session.commit()
            flash(f"Post '{title}' created.", "success")
            return redirect(url_for("blog.admin"))

    posts = Post.query.order_by(Post.created_at.desc()).all()
    return render_template("blog/admin.html", posts=posts)


@blog_bp.route("/admin/<int:post_id>/edit", methods=["GET", "POST"])
@login_required
def edit(post_id):
    if not current_user.is_admin:
        return redirect(url_for("blog.index"))
    from app.post_model import Post
    post = Post.query.get_or_404(post_id)

    if request.method == "POST":
        post.title = request.form.get("title", post.title).strip()
        new_slug = request.form.get("slug", post.slug).strip().lower()
        post.body = request.form.get("body", post.body).strip()
        post.summary = request.form.get("summary", post.summary).strip() or post.body[:160]
        post.published = "publish" in request.form

        if new_slug and new_slug != post.slug:
            import re
            new_slug = re.sub(r"[^a-z0-9\-]", "", new_slug).strip("-")
            if Post.query.filter(Post.slug == new_slug, Post.id != post.id).first():
                new_slug = new_slug + "-" + str(int(datetime.now(timezone.utc).timestamp()))
            post.slug = new_slug

        db.session.commit()
        flash("Post updated.", "success")
        return redirect(url_for("blog.admin"))

    return render_template("blog/edit.html", post=post)


@blog_bp.route("/admin/<int:post_id>/delete", methods=["POST"])
@login_required
def delete(post_id):
    if not current_user.is_admin:
        return redirect(url_for("blog.index"))
    from app.post_model import Post
    post = Post.query.get_or_404(post_id)
    db.session.delete(post)
    db.session.commit()
    flash("Post deleted.", "success")
    return redirect(url_for("blog.admin"))
