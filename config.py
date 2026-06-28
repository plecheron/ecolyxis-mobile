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
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Session/auth cookie hardening. SameSite=Lax blocks the cookie on cross-site
    # POSTs (defence-in-depth for CSRF). Secure is env-gated (default off) because
    # the edge currently serves HTTP — flip SESSION_COOKIE_SECURE=1 once the
    # public endpoint is end-to-end HTTPS, or login cookies won't be sent.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "1") not in ("0", "", "false", "False")
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    # Redis — durable job queue + resumable event log (see app/redis_client.py)
    REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    # Route the chat frontend through the durable worker/job path. Off by
    # default; the new endpoints exist regardless — this flips the UI over.
    JOBS_ENABLED = os.environ.get("JOBS_ENABLED", "0") not in ("0", "", "false", "False")
    LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://10.0.0.6:8081/v1")
    LLM_MODEL = "ecolyxis-standard"
    LLM_MAX_HISTORY = 20
    LLM_SYSTEM_PROMPT = (
        "You are Ecolyxis AI, a helpful, knowledgeable assistant. "
        "You are powered by sustainable computing — running on green energy."
    )
    # Stripe
    STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
    STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
    STRIPE_COUPON_ID = os.environ.get("STRIPE_COUPON_ID", "")
    STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    # Rate limiting (free tier)
    RATE_LIMIT_MESSAGES = 5
    RATE_LIMIT_GENERATIONS = 5  # GPU-bound media jobs (image/video/edit/upscale/animate)
    RATE_LIMIT_WINDOW_SECONDS = 3600  # 60 minutes
    HIDREAM_URL = os.environ.get("HIDREAM_URL", "http://10.0.0.6:8083")
    WAN22_URL = os.environ.get("WAN22_URL", "http://10.0.0.6:8085")
    EDIT_URL = os.environ.get("EDIT_URL", "http://10.0.0.6:8087")

    # Sprint model — conductor model (Qwen3.6-35B-A3B)
    SPRINT_LLM_BASE_URL = os.environ.get("SPRINT_LLM_BASE_URL", "http://192.168.122.5:8081/v1")
    SPRINT_LLM_MODEL = os.environ.get("SPRINT_LLM_MODEL", "Qwen3.5-0.8B-Q4_K_M.gguf")
    # Escalation target — the stronger model Sprint consults when unsure
    SPRINT_ESCALATION_BASE_URL = os.environ.get("SPRINT_ESCALATION_BASE_URL", "http://192.168.122.5:8081/v1")
    SPRINT_ESCALATION_MODEL = os.environ.get("SPRINT_ESCALATION_MODEL", "GLM-4.7-Flash-Q4_K_M.gguf")

    # TTS (Qwen3-TTS via gpu-manager proxy)
    TTS_URL = os.environ.get("TTS_URL", "http://10.0.0.6:8091")
