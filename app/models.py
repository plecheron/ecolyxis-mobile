import json
import uuid
from datetime import datetime, timezone
from app import db, login_manager
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_login = db.Column(db.DateTime)

    # Subscription
    tier = db.Column(db.String(20), default="free", nullable=False)  # "free" or "premium"
    stripe_customer_id = db.Column(db.String(120), nullable=True)
    stripe_subscription_id = db.Column(db.String(120), nullable=True)
    subscription_status = db.Column(db.String(30), nullable=True)  # active, past_due, canceled, etc.
    cancel_at_period_end = db.Column(db.Boolean, default=False, nullable=False)

    threads = db.relationship("Thread", backref="user", lazy=True, cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_premium(self):
        return self.tier == "premium" and self.subscription_status in ("active", "trialing")

    def messages_in_window(self, window_seconds=3600):
        """Count user messages across all threads in the last N seconds."""
        cutoff = datetime.now(timezone.utc) - __import__("datetime").timedelta(seconds=window_seconds)
        return (
            Message.query
            .join(Thread, Message.thread_id == Thread.id)
            .filter(Thread.user_id == self.id, Message.role == "user", Message.created_at >= cutoff)
            .count()
        )


class Thread(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    title = db.Column(db.String(200), default="New Chat")
    system_prompt = db.Column(db.Text, nullable=True)  # Custom system prompt (premium)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    messages = db.relationship("Message", backref="thread", lazy=True, cascade="all, delete-orphan")

    @staticmethod
    def _extract_text(content):
        """Extract human-readable text from content, handling multimodal JSON arrays."""
        if not content:
            return ""
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                parts = [p.get("text", "") for p in parsed if p.get("type") == "text" and p.get("text")]
                return " ".join(parts)
            elif isinstance(parsed, dict):
                return parsed.get("text", content)
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
        return content

    def update_title(self):
        first = Message.query.filter_by(thread_id=self.id).order_by(Message.created_at).first()
        if first and first.role == "user":
            text = self._extract_text(first.content)
            self.title = text[:50] + ("..." if len(text) > 50 else "") or "New Chat"


class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    thread_id = db.Column(db.String(36), db.ForeignKey("thread.id"), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # "user" or "assistant"
    content = db.Column(db.Text, nullable=False)
    tokens_used = db.Column(db.Integer, nullable=True)
    message_type = db.Column(db.String(10), default="text", nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class WebAuthnCredential(db.Model):
    """Stored FIDO2/WebAuthn credential for passwordless/biometric login."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    credential_id = db.Column(db.LargeBinary, nullable=False, unique=True)
    public_key = db.Column(db.LargeBinary, nullable=False)
    sign_count = db.Column(db.Integer, default=0)
    name = db.Column(db.String(80), nullable=True)  # e.g. "Pixel 8" or "iPhone"
    transports = db.Column(db.String(200), nullable=True)  # JSON list
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref=db.backref("webauthn_credentials", lazy=True, cascade="all, delete-orphan"))


import secrets
import hashlib


class ApiKey(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(80), nullable=False, default="Default")
    key_hash = db.Column(db.String(64), unique=True, nullable=False)
    key_prefix = db.Column(db.String(8), nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref=db.backref("api_keys", lazy=True, cascade="all, delete-orphan"))
    usage = db.relationship("ApiUsage", backref="api_key", lazy=True, cascade="all, delete-orphan")

    MAX_KEYS_PER_USER = 5

    @staticmethod
    def generate_key():
        raw = "ecolyx_" + secrets.token_urlsafe(32)
        hashed = hashlib.sha256(raw.encode()).hexdigest()
        prefix = raw[-4:]
        return raw, hashed, prefix

    @staticmethod
    def hash_token(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def __repr__(self):
        return f"<ApiKey {self.name} (...{self.key_prefix})>"


class ApiUsage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    api_key_id = db.Column(db.Integer, db.ForeignKey("api_key.id"), nullable=False)
    endpoint = db.Column(db.String(100), nullable=False)
    model = db.Column(db.String(100), nullable=True)
    tokens_prompt = db.Column(db.Integer, default=0)
    tokens_completion = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class Wallet(db.Model):
    """Prepaid credit balance for API usage. Balance stored in pence (£1 = 100 pence)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True, nullable=False)
    balance_pence = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", backref=db.backref("wallet", uselist=False, cascade="all, delete-orphan"))
    transactions = db.relationship("Transaction", backref="wallet", lazy=True, cascade="all, delete-orphan")

    @property
    def balance(self):
        """Return balance as a float in GBP."""
        return self.balance_pence / 100.0

    def can_afford(self, pence):
        return self.balance_pence >= pence

    def credit(self, pence, description, stripe_payment_intent_id=None):
        """Add credits to wallet."""
        self.balance_pence += pence
        txn = Transaction(
            wallet_id=self.id,
            type="topup",
            amount_pence=pence,
            description=description,
            stripe_payment_intent_id=stripe_payment_intent_id,
        )
        db.session.add(txn)
        return txn

    def debit(self, pence, description, api_key_id=None):
        """Deduct credits from wallet. Raises ValueError if insufficient."""
        if self.balance_pence < pence:
            raise ValueError("Insufficient balance")
        self.balance_pence -= pence
        txn = Transaction(
            wallet_id=self.id,
            type="usage",
            amount_pence=-pence,
            description=description,
            api_key_id=api_key_id,
        )
        db.session.add(txn)
        return txn


class Transaction(db.Model):
    """Audit trail for all wallet changes."""
    id = db.Column(db.Integer, primary_key=True)
    wallet_id = db.Column(db.Integer, db.ForeignKey("wallet.id"), nullable=False)
    type = db.Column(db.String(20), nullable=False)  # "topup", "usage", "refund"
    amount_pence = db.Column(db.Integer, nullable=False)  # positive=topup/refund, negative=usage
    description = db.Column(db.String(255), nullable=False)
    api_key_id = db.Column(db.Integer, db.ForeignKey("api_key.id"), nullable=True)
    stripe_payment_intent_id = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


class LLMQueueEntry(db.Model):
    """Priority queue for LLM requests."""
    __tablename__ = "llm_queue"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, nullable=False)
    is_premium = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="waiting")

    __table_args__ = (
        db.Index("idx_queue_status", "status", "is_premium", "created_at"),
    )


class GeneratedImage(db.Model):
    """Tracks generated images with seed/resolution for upscaling."""
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("message.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    thread_id = db.Column(db.String(36), db.ForeignKey("thread.id"), nullable=False)
    prompt = db.Column(db.Text, nullable=False)
    seed = db.Column(db.BigInteger, nullable=False)
    width = db.Column(db.Integer, nullable=False)
    height = db.Column(db.Integer, nullable=False)
    filename = db.Column(db.String(120), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("generated_image.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", backref=db.backref("generated_images", lazy=True))
    parent = db.relationship("GeneratedImage", remote_side=[id], backref="upscaled_versions")

    SIZES = [128, 256, 512, 1024, 2048]

    def next_size(self):
        """Return the next upscale size, or None if already at max."""
        idx = self.SIZES.index(self.width) if self.width in self.SIZES else -1
        if idx < len(self.SIZES) - 1:
            return self.SIZES[idx + 1]
        return None


class GeneratedVideo(db.Model):
    """Tracks generated videos."""
    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("message.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    thread_id = db.Column(db.String(36), db.ForeignKey("thread.id"), nullable=False)
    prompt = db.Column(db.Text, nullable=False)
    seed = db.Column(db.BigInteger, nullable=False, default=0)
    width = db.Column(db.Integer, nullable=False, default=480)
    height = db.Column(db.Integer, nullable=False, default=480)
    frames = db.Column(db.Integer, nullable=False, default=33)
    fps = db.Column(db.Integer, nullable=False, default=16)
    filename = db.Column(db.String(120), nullable=False)
    duration_s = db.Column(db.Float, nullable=True)
    model = db.Column(db.String(60), nullable=False, default="wan22-ti2v-5b-q4")
    parent_image_id = db.Column(db.Integer, db.ForeignKey("generated_image.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    user = db.relationship("User", backref=db.backref("generated_videos", lazy=True))
    parent_image = db.relationship("GeneratedImage", backref=db.backref("animated_videos", lazy=True))
