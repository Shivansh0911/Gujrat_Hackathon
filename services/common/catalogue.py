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

import requests

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
        return bool(
            self.declared_codec and self.declared_width and self.declared_height
        )


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


def parse_catalogue(payload: dict[str, Any], settings: Settings) -> list[CameraDescriptor]:
    """Parse an /api/ingest response. Skips malformed rows rather than failing the poll."""
    cameras: list[CameraDescriptor] = []
    rows = payload.get("cameras")
    if not isinstance(rows, list):
        raise ValueError("catalogue payload has no 'cameras' list")

    base = f"{settings.gateway_scheme}://{settings.gateway_host}"
    for row in rows:
        if not isinstance(row, dict):
            log.warning("skipping non-object catalogue row: %r", type(row))
            continue
        external_id = _opt_str(row.get("id"))
        rtsp_url = _opt_str(row.get("rtsp_url"))
        if not external_id or not rtsp_url:
            log.warning("skipping catalogue row without id or rtsp_url")
            continue

        hls = _opt_str(row.get("hls_live_url"))
        # The catalogue returns HLS as a site-relative path; resolve it against the
        # configured gateway rather than assuming a scheme or host.
        hls_abs = urljoin(base, hls) if hls else None

        w = _opt_pos_num(row.get("width"))
        h = _opt_pos_num(row.get("height"))
        br = _opt_pos_num(row.get("bitrate_kbps"))

        cameras.append(
            CameraDescriptor(
                external_id=external_id,
                name=_opt_str(row.get("name")) or f"Camera {external_id}",
                location_text=_opt_str(row.get("location")) or "",
                live=bool(row.get("live", False)),
                rtsp_url=rtsp_url,
                whep_url=_opt_str(row.get("webrtc_url")),
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
    """GET /api/ingest and parse it. TLS verification is always on."""
    resp = requests.get(settings.catalogue_url, timeout=timeout)  # verify defaults True
    resp.raise_for_status()
    return parse_catalogue(resp.json(), settings)
