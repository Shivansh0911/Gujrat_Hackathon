"""Environment-driven configuration. No secrets, no hosts, no ports in source."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.common.paths import ENV_FILE


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SETU_",
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Deliberately no default. A default here would be a hardcoded host by another
    # name, and Phase 2 adds a second gateway -- this must fail loudly if unset
    # rather than quietly pointing at the wrong estate.
    gateway_host: str = Field(min_length=1)
    gateway_scheme: Literal["http", "https"] = "https"
    gateway_rtsp_port: int = Field(default=8554, ge=1, le=65535)
    gateway_whep_port: int = Field(default=8889, ge=1, le=65535)

    rtsp_transport: Literal["tcp", "udp"] = "tcp"
    join_timeout_s: float = Field(default=20.0, gt=0)
    backoff_min_s: float = Field(default=2.0, gt=0)
    backoff_max_s: float = Field(default=30.0, gt=0)
    max_concurrent_captures: int = Field(default=6, ge=1, le=64)
    ffmpeg_loglevel: int = 16

    @field_validator("gateway_host")
    @classmethod
    def _host_only(cls, v: str) -> str:
        # Guard against someone pasting a full URL into the host field, which would
        # produce silently malformed stream URLs that fail far from the cause.
        if "://" in v or "/" in v:
            raise ValueError(
                f"SETU_GATEWAY_HOST must be a bare host, not a URL: {v!r}"
            )
        return v.strip()

    @field_validator("backoff_max_s")
    @classmethod
    def _backoff_ordered(cls, v: float, info) -> float:
        lo = info.data.get("backoff_min_s")
        if lo is not None and v < lo:
            raise ValueError("SETU_BACKOFF_MAX_S must be >= SETU_BACKOFF_MIN_S")
        return v

    # --- Derived endpoints. The catalogue is the contract; these patterns are not,
    # --- so nothing outside adapters may build a stream URL from them. ---

    @property
    def catalogue_url(self) -> str:
        return f"{self.gateway_scheme}://{self.gateway_host}/api/ingest"

    def rtsp_url(self, external_id: str) -> str:
        return f"rtsp://{self.gateway_host}:{self.gateway_rtsp_port}/stream/{external_id}"

    def whep_url(self, external_id: str) -> str:
        return f"http://{self.gateway_host}:{self.gateway_whep_port}/stream/{external_id}/whep"

    def hls_url(self, external_id: str) -> str:
        return f"{self.gateway_scheme}://{self.gateway_host}/live/stream/{external_id}/index.m3u8"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  -- values come from env/.env
