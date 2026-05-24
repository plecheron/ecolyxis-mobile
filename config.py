import os

basedir = os.path.abspath(os.path.dirname(__file__))


def _load_dotenv(path):
    """Populate os.environ from a KEY=VALUE file. Real env vars win (setdefault)."""
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv(os.path.join(basedir, ".env"))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "ecolyxis-dev-key-change-in-production")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "postgresql://ecolyxis:changeme@localhost/ecolyxis")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://10.0.0.1:8081/v1")
    LLM_MODEL = "Qwen_Qwen3.6-35B-A3B-Q4_0.gguf"
    LLM_MAX_HISTORY = 20
    LLM_SYSTEM_PROMPT = (
        "You are Ecolyxis AI, a helpful, knowledgeable assistant. "
        "You are powered by sustainable computing — running on green energy."
    )
    # Stripe
    STRIPE_PUBLISHABLE_KEY = os.environ.get(
        "STRIPE_PUBLISHABLE_KEY",
        "pk_live_51TSwOL6naLy0BWdwLFrjBP8aCdZhDPLWx4KztoN0SsyachNzywLfyhPipo6vr26jQxSZYpWSy744CtQWA8zJ30aP00SGMyZiGw",
    )
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "sk_test_placeholder")
    STRIPE_PRICE_ID = "price_1TT0Dt6naLy0BWdwOZcb1fN6"
    STRIPE_COUPON_ID = "lRP5xwv0"
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "whsec_placeholder")
    # Rate limiting (free tier)
    RATE_LIMIT_MESSAGES = 5
    RATE_LIMIT_WINDOW_SECONDS = 3600  # 60 minutes
    HIDREAM_URL = os.environ.get("HIDREAM_URL", "http://192.168.122.5:8083")
    WAN22_URL = os.environ.get("WAN22_URL", "http://192.168.122.5:8085")
