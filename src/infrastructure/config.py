"""Centralised configuration using Pydantic Settings."""

from functools import lru_cache

from pydantic import SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── WhatsApp Business API ──────────────────────────────────────────────
    whatsapp_token: SecretStr
    phone_number_id: str
    waba_id: str = ""
    verify_token: str
    bot_phone_number: str = ""

    # ── MercadoPago ───────────────────────────────────────────────────────
    mp_access_token: str = ""
    mp_public_key: str = ""

    # ── Admin ─────────────────────────────────────────────────────────────
    admin_phone: str = ""

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://postgres:postgres@postgres:5432/whatsapp_bot"

    # ── AI / OpenAI ───────────────────────────────────────────────────────
    openai_api_key: SecretStr = SecretStr("")
    openai_api_key_secondary: SecretStr = SecretStr("")
    openai_model: str = "gpt-4o-mini"
    ai_enabled: bool = True

    # ── Memory ────────────────────────────────────────────────────────────
    memory_enabled: bool = True
    memory_max_memories: int = 20
    memory_max_tokens: int = 2000
    memory_summarize_after: int = 50
    memory_importance_threshold: float = 0.7

    # ── App ───────────────────────────────────────────────────────────────
    debug: bool = False
    port: int = 8000
    tmp_dir: str = "/tmp/wpbase"
    base_url: str = "https://your-domain.com"

    # ── Agent logging ─────────────────────────────────────────────────────
    agent_logging_enabled: bool = True

    # ── Rate limiting ─────────────────────────────────────────────────────
    rate_limit: int = 20
    rate_window: int = 60

    # ── Subscription plan IDs ─────────────────────────────────────────────
    # Pro plans
    mp_plan_monthly_trial: str = ""
    mp_plan_monthly_no_trial: str = ""
    mp_plan_annual_trial: str = ""
    mp_plan_annual_no_trial: str = ""
    mp_plan_id: str = ""
    mp_plan_id_no_trial: str = ""
    # Premium plans
    mp_premium_monthly_trial: str = ""
    mp_premium_monthly_no_trial: str = ""
    mp_premium_annual_trial: str = ""
    mp_premium_annual_no_trial: str = ""
    mp_webhook_secret: str = ""

    # ── Subscription prices (ARS) ─────────────────────────────────────────
    pro_monthly_price: int = 3999
    pro_annual_price: int = 39990
    premium_monthly_price: int = 6999
    premium_annual_price: int = 69990

    # ── Computed ──────────────────────────────────────────────────────────
    @computed_field  # type: ignore[misc]
    @property
    def meta_api_url(self) -> str:
        return f"https://graph.facebook.com/v23.0/{self.phone_number_id}/messages"

    @computed_field  # type: ignore[misc]
    @property
    def meta_media_url(self) -> str:
        return f"https://graph.facebook.com/v22.0/{self.phone_number_id}/media"

    @computed_field  # type: ignore[misc]
    @property
    def meta_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.whatsapp_token.get_secret_value()}",
            "Content-Type": "application/json",
        }

    def openai_api_keys(self) -> tuple[str, ...]:
        """Return deduplicated configured OpenAI API keys in retry order."""
        keys: list[str] = []
        for secret in (self.openai_api_key, self.openai_api_key_secondary):
            value = secret.get_secret_value().strip()
            if value and value not in keys:
                keys.append(value)
        return tuple(keys)


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()
