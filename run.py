from app import create_app

app = create_app()


@app.route("/")
def landing():
    from flask import render_template
    from flask_login import current_user
    if current_user.is_authenticated:
        from flask import redirect, url_for
        return redirect(url_for("dashboard.index"))
    return render_template("landing.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, debug=False)
