from flask import Blueprint, render_template

contact_bp = Blueprint("contact", __name__, url_prefix="/contact")


@contact_bp.route("/")
def index():
    return render_template("contact/index.html")
