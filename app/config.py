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
    # llama-3.3-70b-versatile was retired on Groq on 2026-08-16; gpt-oss supports strict JSON schema.
    groq_model: str = field(default_factory=lambda: _env("GROQ_MODEL", "openai/gpt-oss-120b"))
    groq_reply_model: str = field(default_factory=lambda: _env("GROQ_REPLY_MODEL", "openai/gpt-oss-20b"))

    # Google (Gmail + Calendar) - used from Step 2 / Step 4
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    # Only mail sent to this address (e.g. you+lumen@gmail.com) is treated as an inquiry.
    # Everything else in the connected mailbox is ignored and never logged.
    email_inbox_address: str = field(default_factory=lambda: _env("EMAIL_INBOX_ADDRESS").lower())

    # Meta (WhatsApp + Instagram) - used from Step 5 / Step 6
    meta_app_secret: str = field(default_factory=lambda: _env("META_APP_SECRET"))
    meta_verify_token: str = field(default_factory=lambda: _env("META_VERIFY_TOKEN"))
    whatsapp_token: str = field(default_factory=lambda: _env("WHATSAPP_ACCESS_TOKEN"))
    whatsapp_phone_number_id: str = field(default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID"))
    instagram_token: str = field(default_factory=lambda: _env("INSTAGRAM_ACCESS_TOKEN"))
    # Instagram API with Instagram Login has its own app secret (App Dashboard > Instagram > API setup).
    instagram_app_secret: str = field(default_factory=lambda: _env("INSTAGRAM_APP_SECRET"))
    # Comma-separated Instagram usernames (or IGSIDs) the assistant may answer. When set, DMs from
    # anyone else are ignored and never stored, so a personal account's friends never get bot replies.
    instagram_allowed_senders: str = field(default_factory=lambda: _env("INSTAGRAM_ALLOWED_SENDERS"))

    @property
    def instagram_allowlist(self) -> set[str]:
        return {x.strip().lstrip("@").lower() for x in self.instagram_allowed_senders.split(",") if x.strip()}

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


settings = Settings()
