"""Runtime configuration, read from environment variables only.

No secret has a default value. Missing integrations show up as
"not configured" in the UI instead of failing silently.
"""
import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _normalise_db_url(url: str) -> str:
    # Render hands out postgres:// or postgresql:// URLs; SQLAlchemy needs a driver.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@dataclass(frozen=True)
class Settings:
    app_env: str = field(default_factory=lambda: _env("APP_ENV", "development"))
    database_url: str = field(
        default_factory=lambda: _normalise_db_url(_env("DATABASE_URL", "sqlite:///./local.db"))
    )
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://localhost:8000"))

    # Dashboard login (HTTP Basic). Required in production.
    dashboard_user: str = field(default_factory=lambda: _env("DASHBOARD_USER", "admin"))
    dashboard_password: str = field(default_factory=lambda: _env("DASHBOARD_PASSWORD"))

    # LLM
    groq_api_key: str = field(default_factory=lambda: _env("GROQ_API_KEY"))
    groq_model: str = field(default_factory=lambda: _env("GROQ_MODEL", "llama-3.3-70b-versatile"))

    # Google (Gmail + Calendar) - used from Step 2 / Step 4
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))

    # Meta (WhatsApp + Instagram) - used from Step 5 / Step 6
    meta_app_secret: str = field(default_factory=lambda: _env("META_APP_SECRET"))
    meta_verify_token: str = field(default_factory=lambda: _env("META_VERIFY_TOKEN"))
    whatsapp_token: str = field(default_factory=lambda: _env("WHATSAPP_ACCESS_TOKEN"))
    whatsapp_phone_number_id: str = field(default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID"))
    instagram_token: str = field(default_factory=lambda: _env("INSTAGRAM_ACCESS_TOKEN"))

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


settings = Settings()
