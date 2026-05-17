from flask import Blueprint
from app import db
from app.models import Wallet

billing_bp = Blueprint("billing", __name__)


def _sd(stripe_obj, key, default=None):
    """Safely get a value from a Stripe object or dict."""
    try:
        return stripe_obj[key]
    except (KeyError, TypeError, IndexError):
        return default


def _get_or_create_wallet(user):
    """Get or create a wallet for the user."""
    w = Wallet.query.filter_by(user_id=user.id).first()
    if not w:
        w = Wallet(user_id=user.id)
        db.session.add(w)
        db.session.commit()
    return w


from app.billing import routes, webhook  # noqa: E402,F401 — register routes
