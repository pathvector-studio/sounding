"""Settings read from the environment (the keys of .env.example).

Secrets are SecretStr so they never show up in repr() or logs. Call ``.get_secret_value()`` only
where the value is actually used.
"""

from __future__ import annotations

from functools import lru_cache

from decimal import Decimal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_PREFIX = "change-me"


def is_set(value: str | None) -> bool:
    """True for a real value: not empty and not a ``change-me-...`` placeholder from .env.example."""
    value = (value or "").strip()
    return bool(value) and not value.startswith(_PLACEHOLDER_PREFIX)


class Settings(BaseSettings):
    # compose passes .env through env_file / environment, so no dotenv file is read here.
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    database_url: SecretStr
    tz: str = "Asia/Tokyo"
    profile_path: str = "config/profile.yaml"
    public_base_url: str = "http://localhost:8080"  # links in the digest point here (no trailing slash)

    # Web UI (sounding.web; the web container gets these and no other secret)
    session_secret: SecretStr = SecretStr("")  # signs the login cookie and derives the Atom token; >= 32 bytes
    web_password_hash: SecretStr = SecretStr("")  # scrypt$n$r$p$salt$hash (hash-password); empty = no login

    # Fetching
    fetch_interval_minutes: int = 60
    fetch_contact: SecretStr = SecretStr("")
    external_timeout_seconds: int = 30
    content_retention_days: int = 90

    # IETF
    ietf_mail_fetch: str = "imap"
    ietf_imap_host: str = "imap.ietf.org"
    ietf_rsync_base: str = "rsync.ietf.org::mailman-archive"
    ietf_datatracker_base: str = "https://datatracker.ietf.org"
    ietf_backfill_days: int = 30
    ietf_max_messages_per_run: int = 500
    ietf_ancestor_max_depth: int = 50
    ietf_ancestor_max_age_days: int = 365

    # LLM (the key is read by the worker only; never logged or stored)
    llm_provider: str = "none"  # none | anthropic | openai
    llm_api_key: SecretStr = SecretStr("")
    llm_model_cheap: str = ""  # empty / change-me: anthropic default, openai = configuration error
    llm_model_main: str = ""
    llm_api_base: str = ""  # openai only (compatible servers); empty = the provider's default
    llm_daily_budget_usd: Decimal = Decimal("1.00")  # 0 = unlimited
    llm_max_output_tokens: int = 16000

    # Delivery (worker only). The webhook URL is itself the credential. A sending channel that is enabled
    # but left empty / change-me is a configuration error for that channel only (deliveries.error = not_configured).
    delivery_channels: str = "web,atom"  # comma-separated subset of web,email,webhook,atom
    webhook_url: SecretStr = SecretStr("")
    smtp_host: str = ""
    smtp_port: int = 587  # 465 = implicit TLS; anything else = STARTTLS (never plaintext)
    smtp_user: SecretStr = SecretStr("")  # empty = no login
    smtp_password: SecretStr = SecretStr("")
    smtp_from: SecretStr = SecretStr("")
    digest_to: SecretStr = SecretStr("")  # comma-separated recipients

    # Author keys are HMAC(identity_secret, address): changing it changes every key.
    identity_secret: SecretStr = SecretStr("")

    @field_validator("public_base_url")
    @classmethod
    def _strip_base_url(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def enabled_channels(self) -> frozenset[str]:
        """``delivery_channels`` parsed: lower-cased, trimmed, empty entries dropped."""
        return frozenset(c.strip().lower() for c in self.delivery_channels.split(",") if c.strip())

    @property
    def fetch_contact_is_set(self) -> bool:
        return is_set(self.fetch_contact.get_secret_value())

    @property
    def identity_secret_is_set(self) -> bool:
        return is_set(self.identity_secret.get_secret_value())

    @property
    def llm_api_key_is_set(self) -> bool:
        return is_set(self.llm_api_key.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
