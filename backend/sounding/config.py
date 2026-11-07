"""Settings read from the environment (the keys of .env.example).

Secrets are SecretStr so they never show up in repr() or logs. Call ``.get_secret_value()`` only
where the value is actually used.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_PREFIX = "change-me"


class Settings(BaseSettings):
    # compose passes .env through env_file / environment, so no dotenv file is read here.
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    database_url: SecretStr
    tz: str = "Asia/Tokyo"

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

    # Author keys are HMAC(identity_secret, address): changing it changes every key.
    identity_secret: SecretStr = SecretStr("")

    @property
    def fetch_contact_is_set(self) -> bool:
        value = self.fetch_contact.get_secret_value().strip()
        return bool(value) and not value.startswith(_PLACEHOLDER_PREFIX)

    @property
    def identity_secret_is_set(self) -> bool:
        value = self.identity_secret.get_secret_value().strip()
        return bool(value) and not value.startswith(_PLACEHOLDER_PREFIX)


@lru_cache
def get_settings() -> Settings:
    return Settings()
