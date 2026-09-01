"""Client for the gateway camera catalogue (/api/ingest).

§2.1: "Always start from the catalogue, never hardcode endpoints. Camera ids and the
set of available cameras can change. The catalogue is the contract; the URL pattern
is not."

Observed reality on 2026-08-25, which the parser is built around: the live catalogue
returns 30 cameras, all `live: true`, but **20 of the 30 report `codec: ""`,
`width/height: 0` and `fps: 0.0`**. The catalogue is authoritative for *which cameras
exist and how to reach them*, and unreliable for *what they contain*. So every field
here is optional, zero is normalised to None rather than propagated as a real value,
and stream properties are discovered by probing (scripts/probe_catalogue.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin


from services.common import gateway_auth
from services.common.config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CameraDescriptor:
    """One camera as the source system describes it. Adapter-agnostic (§6)."""

    external_id: str
    name: str
    location_text: str
    live: bool
    rtsp_url: str
    whep_url: str | None
    hls_url: str | None
    declared_codec: str | None
    declared_width: int | None
    declared_height: int | None
    declared_fps: float | None  # reference only -- never used for timing (§2.2)
    declared_bitrate_kbps: int | None

    @property
    def properties_known(self) -> bool:
        """False when the catalogue gave us zeros; such cameras MUST be probed."""
        return bool(self.declared_codec and self.declared_width and self.declared_height)


def _opt_str(v: Any) -> str | None:
    s = str(v).strip() if v is not None else ""
    return s or None


def _opt_pos_num(v: Any) -> float | None:
    """Normalise the catalogue's 0/0.0 'unknown' sentinel to None."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def parse_catalogue(
    payload: dict[str, Any] | list[Any], settings: Settings
) -> list[CameraDescriptor]:
    """Parse a catalogue response. Skips malformed rows rather than failing the poll.

    Two shapes are accepted, because the estate has published both.

    The original `/api/ingest` returned ``{"cameras": [...]}`` where each row carried its
    own stream URLs and declared properties. The current `/cameras.json` returns a bare
    list of ``{"id", "name"}`` and nothing else -- no URLs, no codec, no resolution, no
    `live` flag.

    That thinner document is handled by *deriving* the URLs from configuration and
    leaving every declared property as None. It costs nothing, because the pipeline
    already refuses to trust declared properties: `properties_known` goes False and the
    camera is probed, which is what DISCOVERY finding 1 required when 19 of 30 cameras
    declared nothing, and what finding 9 required when the `live` flag turned out to be a
    claim rather than a health signal. An estate that now declares nothing at all changes
    the numbers and not the policy.
    """
    cameras: list[CameraDescriptor] = []
    if isinstance(payload, list):
        rows: list[Any] = payload
    else:
        maybe = payload.get("cameras")
        if not isinstance(maybe, list):
            raise ValueError("catalogue payload has no 'cameras' list")
        rows = maybe

    base = f"{settings.gateway_scheme}://{settings.gateway_host}"
    for row in rows:
        if not isinstance(row, dict):
            log.warning("skipping non-object catalogue row: %r", type(row))
            continue
        external_id = _opt_str(row.get("id"))
        if not external_id:
            log.warning("skipping catalogue row without an id")
            continue

        # A row that names no stream URL is not malformed any more -- the current
        # catalogue names none for anybody. Fall back to the configured pattern, which
        # is the only thing that knows RTSP lives on a different host from the
        # catalogue when a CDN is in front.
        rtsp_url = _opt_str(row.get("rtsp_url")) or settings.rtsp_url(external_id)

        hls = _opt_str(row.get("hls_live_url"))
        # The catalogue returns HLS as a site-relative path; resolve it against the
        # configured gateway rather than assuming a scheme or host.
        hls_abs = urljoin(base, hls) if hls else settings.hls_url(external_id)

        w = _opt_pos_num(row.get("width"))
        h = _opt_pos_num(row.get("height"))
        br = _opt_pos_num(row.get("bitrate_kbps"))

        cameras.append(
            CameraDescriptor(
                external_id=external_id,
                name=_opt_str(row.get("name")) or f"Camera {external_id}",
                location_text=_opt_str(row.get("location")) or "",
                # Absent means "the catalogue does not say", and the honest default is
                # to consider it a candidate and probe it. Treating silence as dead
                # would drop the entire estate, since the current catalogue carries no
                # flag at all; and the flag was never a health signal anyway.
                live=bool(row.get("live", True)),
                rtsp_url=rtsp_url,
                whep_url=_opt_str(row.get("webrtc_url")) or settings.whep_url(external_id),
                hls_url=hls_abs,
                declared_codec=_opt_str(row.get("codec")),
                declared_width=int(w) if w else None,
                declared_height=int(h) if h else None,
                declared_fps=_opt_pos_num(row.get("fps")),
                declared_bitrate_kbps=int(br) if br else None,
            )
        )
    return cameras


def fetch_catalogue(settings: Settings, timeout: float = 20.0) -> list[CameraDescriptor]:
    """GET the catalogue and parse it, authenticating if the estate requires it.

    TLS verification is always on. Signing in, recognising a sign-in page and sharing
    one session across polls all live in `gateway_auth`, because the HLS resolver needs
    exactly the same three things and a second implementation would drift.
    """
    resp = gateway_auth.get(settings, settings.catalogue_url, timeout)
    resp.raise_for_status()
    return parse_catalogue(resp.json(), settings)
