"""API configuration. Secrets come from the environment, never from source."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.common.paths import ENV_FILE


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SETU_", env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = Field(min_length=1)

    # No default. A default signing key is a backdoor with a changelog entry.
    jwt_secret: str = Field(min_length=16)
    jwt_issuer: str = "setu-local"
    access_token_ttl_min: int = Field(default=30, ge=1, le=1440)

    evidence_dir: str = "data/evidence"

    # Interim operator accounts. No defaults: without these the API refuses to
    # issue tokens rather than falling back to a known credential.
    admin_password: str | None = None
    operator_password: str | None = None

    # The console's origin. Never "*": credentialed requests with a wildcard origin
    # are rejected by browsers anyway, and the combination is a standing §0 violation.
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # --- Route plausibility (T4). Configuration, not constants, because the right
    # --- ceiling differs between a highway corridor and a city centre.
    max_speed_highway_kmph: float = Field(default=140.0, gt=0)
    max_speed_urban_kmph: float = Field(default=90.0, gt=0)
    # Straight-line distance underestimates road distance; 1.3 is the standard
    # detour factor used when no road graph is loaded.
    detour_factor: float = Field(default=1.3, ge=1.0)
    # A hop implying a speed below this is a dwell, not travel, and is not penalised.
    min_transit_speed_kmph: float = Field(default=2.0, ge=0)

    @field_validator("jwt_secret")
    @classmethod
    def _reject_placeholder_secret(cls, v: str) -> str:
        # Catches a .env copied from .env.example and never edited, which would
        # otherwise ship a publicly known signing key.
        if v.strip().lower() in {"change-me-locally", "changeme", "secret", "dev"}:
            raise ValueError(
                "SETU_JWT_SECRET is still the placeholder value; generate one with "
                "python -c \"import secrets;print(secrets.token_urlsafe(48))\""
            )
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache(maxsize=1)
def get_api_settings() -> ApiSettings:
    return ApiSettings()  # type: ignore[call-arg]
