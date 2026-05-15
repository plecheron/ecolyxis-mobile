from flask import Blueprint, render_template

pricing_bp = Blueprint("pricing", __name__)


@pricing_bp.route("/pricing")
def index():
    return render_template("pricing.html")
