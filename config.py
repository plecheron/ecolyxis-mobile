import os

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "ecolyxis-dev-key-change-in-production")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "postgresql://ecolyxis:changeme@localhost/ecolyxis")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:8081/v1")
    LLM_MODEL = "Qwen_Qwen3.6-35B-A3B-Q4_0.gguf"
    LLM_MAX_HISTORY = 20
    LLM_SYSTEM_PROMPT = (
        "You are Ecolyxis AI, a helpful, knowledgeable assistant. "
        "You are powered by sustainable computing — running on green energy."
    )
    # Stripe
    STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "pk_test_placeholder")
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "sk_test_placeholder")
    STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "price_placeholder")
    STRIPE_COUPON_ID = os.environ.get("STRIPE_COUPON_ID", "coupon_placeholder")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_placeholder")
    # Rate limiting (free tier)
    RATE_LIMIT_MESSAGES = 5
    RATE_LIMIT_WINDOW_SECONDS = 3600  # 60 minutes
    HIDREAM_URL = os.environ.get("HIDREAM_URL", "http://localhost:8083")
