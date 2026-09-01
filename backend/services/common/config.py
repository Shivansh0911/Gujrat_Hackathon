"""Environment-driven configuration. No secrets, no hosts, no ports in source."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationInfo, field_validator
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

    #: Host carrying RTSP and WebRTC, when that is not the host serving the catalogue.
    #:
    #: A CDN can proxy HLS and cannot proxy RTSP, so a real estate tends to publish the
    #: catalogue and HLS behind the CDN name while RTSP and WHEP sit on a bare address.
    #: Empty means "same host", which is what every deployment before this one assumed.
    gateway_media_host: str = ""

    #: Path to the camera catalogue on the gateway host. The estate renamed this from
    #: `/api/ingest` to `/cameras.json`; it is configuration rather than a constant so
    #: the next rename is a deployment change and not a code change.
    catalogue_path: str = "/api/ingest"

    #: URL template for a camera's HLS playlist, `{id}` substituted. Configurable for
    #: the same reason: the estate moved from `/live/stream/{id}/index.m3u8` to
    #: `/{id}/index.m3u8`.
    hls_path_template: str = "/live/stream/{id}/index.m3u8"

    #: Access code for a catalogue behind a login. Empty when the estate is open.
    #:
    #: Held as the code rather than a session cookie on purpose: a cookie expires and
    #: then every poll fails with a redirect that looks like an outage, whereas a code
    #: lets the client re-authenticate itself. Secret, and redacted like every other.
    gateway_access_code: str = ""

    #: Path the access code is posted to, and the form field it goes in.
    gateway_login_path: str = "/auth/login"

    rtsp_transport: Literal["tcp", "udp"] = "tcp"
    join_timeout_s: float = Field(default=20.0, gt=0)
    backoff_min_s: float = Field(default=2.0, gt=0)
    backoff_max_s: float = Field(default=30.0, gt=0)
    max_concurrent_captures: int = Field(default=6, ge=1, le=64)
    ffmpeg_loglevel: int = 16

    @field_validator("gateway_host", "gateway_media_host")
    @classmethod
    def _host_only(cls, v: str) -> str:
        if not v:
            return v
        # Guard against someone pasting a full URL into the host field, which would
        # produce silently malformed stream URLs that fail far from the cause.
        if "://" in v or "/" in v:
            raise ValueError(f"gateway host must be a bare host, not a URL: {v!r}")
        return v.strip()

    @field_validator("backoff_max_s")
    @classmethod
    def _backoff_ordered(cls, v: float, info: ValidationInfo) -> float:
        lo = info.data.get("backoff_min_s")
        if lo is not None and v < lo:
            raise ValueError("SETU_BACKOFF_MAX_S must be >= SETU_BACKOFF_MIN_S")
        return v

    # --- Derived endpoints. The catalogue is the contract; these patterns are not,
    # --- so nothing outside adapters may build a stream URL from them. ---

    @property
    def media_host(self) -> str:
        """Where RTSP and WebRTC live. The catalogue host unless one is configured."""
        return self.gateway_media_host or self.gateway_host

    @property
    def catalogue_url(self) -> str:
        path = (
            self.catalogue_path
            if self.catalogue_path.startswith("/")
            else f"/{self.catalogue_path}"
        )
        return f"{self.gateway_scheme}://{self.gateway_host}{path}"

    def rtsp_url(self, external_id: str) -> str:
        return f"rtsp://{self.media_host}:{self.gateway_rtsp_port}/stream/{external_id}"

    def whep_url(self, external_id: str) -> str:
        return f"http://{self.media_host}:{self.gateway_whep_port}/stream/{external_id}/whep"

    def hls_url(self, external_id: str) -> str:
        path = self.hls_path_template.format(id=external_id)
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.gateway_scheme}://{self.gateway_host}{path}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Every field is populated from the environment or .env, which mypy cannot see.
    return Settings()  # type: ignore[call-arg]
